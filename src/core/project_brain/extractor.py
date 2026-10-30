"""Deterministic multi-language facts and conservative reference resolution."""

import ast
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path, PurePosixPath

from src.core.code_graph.parser import detect_language, parse_file
from src.core.project_brain.schema import Fact, FactBundle, Relation, SourceRef, stable_id
from src.core.project_brain.snapshot import RepoSnapshot, source_lines

_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "api_route"}
_PY_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_PY_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _scope_bindings(node: ast.AST) -> set[str]:
    """Collect bindings owned by a body, excluding nested lexical scopes."""
    bindings: set[str] = set()

    def visit(current: ast.AST) -> None:
        if isinstance(current, _PY_SCOPES):
            if not isinstance(current, ast.Lambda):
                bindings.add(current.name)
            return
        if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Store):
            bindings.add(current.id)
        elif isinstance(current, (ast.Import, ast.ImportFrom)):
            bindings.update(alias.asname or alias.name.split(".")[0] for alias in current.names)
        elif isinstance(current, ast.ExceptHandler) and current.name:
            bindings.add(current.name)
        if isinstance(current, _PY_COMPREHENSIONS):
            # Iteration targets belong to the comprehension. Named expressions
            # still bind in the containing scope, so visit its value branches.
            for generator in current.generators:
                visit(generator.iter)
                for condition in generator.ifs:
                    visit(condition)
            for field in ("elt", "key", "value"):
                value = getattr(current, field, None)
                if value is not None:
                    visit(value)
            return
        for child in ast.iter_child_nodes(current):
            visit(child)

    for statement in getattr(node, "body", []):
        visit(statement)
    return bindings


def _literal(node: ast.AST | None):
    try:
        return ast.literal_eval(node) if node is not None else None
    except (ValueError, TypeError, SyntaxError):
        return None


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _join_path(*parts: str) -> str:
    result = "/" + "/".join(p.strip("/") for p in parts if p.strip("/"))
    return result + "/" if result != "/" and parts and parts[-1].endswith("/") else result


class FactExtractor:
    """从固定快照提取声明、框架事实和带解析状态的关系。"""

    def __init__(self, snapshot: RepoSnapshot, previous: FactBundle | None = None) -> None:
        self.snapshot = snapshot
        self.root = Path(snapshot.path)
        self.facts: dict[str, Fact] = {}
        self.refs: dict[str, SourceRef] = {}
        self.relations: list[Relation] = []
        self._relations_by_id: dict[str, Relation] = {}
        self.modules: dict[str, str] = {}
        self.issues = list(snapshot.issues)
        self.frameworks: set[str] = set()
        self.symbols: dict[str, list[Fact]] = defaultdict(list)
        self.imports: dict[str, dict[str, str]] = {}
        self.global_bindings: dict[str, list[str]] = {}
        self.calls: list[tuple[str, Fact, str, str]] = []
        self.routes: list[dict] = []
        self.routers: dict[str, str | None] = {}
        self.mounts: list[tuple[str, str, str | None]] = []
        self.apps: set[str] = set()
        self.tables: dict[str, list[Fact]] = defaultdict(list)
        self.accesses: list[tuple[Fact, str, str, list[str], str]] = []
        self.previous = previous
        self.reused_files = 0
        self.observations: dict[str, dict] = {}
        self.build_roots: set[str] = set()
        self.java_imports: dict[str, dict[str, str]] = {}
        self.py_modules = {
            path: path[:-3].replace("/", ".").removesuffix(".__init__")
            for path in snapshot.files if path.endswith(".py")
        }

    def _module(self, path: str) -> str:
        parents = [r for r in self.build_roots if path.startswith(r + "/")]
        if parents:
            name = max(parents, key=len)
        else:
            parts = PurePosixPath(path).parts
            offset = 1 if parts[0] in {"src", "app", "lib"} else 0
            name = "/".join(parts[:offset + 1]) if len(parts) > offset + 1 else "root"
        module_id = stable_id("module", name)
        self.modules[module_id] = name
        return module_id

    def _ref(self, path: str, start: int, end: int, symbol: str | None = None) -> str:
        meta = self.snapshot.files[path]
        start = max(1, min(start, meta["lines"]))
        end = max(start, min(end, meta["lines"]))
        rid = stable_id("evidence", path, str(start), str(end), symbol or "")
        self.refs[rid] = SourceRef(
            id=rid, commit_sha=self.snapshot.commit_sha, path=path, blob_sha=meta["blob_sha"],
            line_start=start, line_end=end, symbol_id=symbol,
        )
        return rid

    def _fact(self, path: str, kind: str, name: str, start: int, end: int, **attrs) -> Fact:
        fid = stable_id(kind, path, name, str(attrs.get("symbol_kind", "")), str(attrs.get("signature", "")))
        ref = self._ref(path, start, end, fid if kind == "symbol" else None)
        fact = Fact(
            id=fid, kind=kind, name=name, module_id=self._module(path),
            source_ref_ids=[ref], extractor=f"brain.{detect_language(path) or 'engineering'}/1",
            attributes={"path": path, **attrs},
        )
        previous = self.facts.get(fid)
        if previous:
            fact.source_ref_ids = sorted(set(previous.source_ref_ids + fact.source_ref_ids))
            if kind == "symbol" and len(fact.source_ref_ids) > 1:
                fact.attributes["redefined"] = True
        self.facts[fid] = fact
        return fact

    def _relation(
        self, source: Fact, kind: str, target: Fact | None, name: str,
        candidates: list[Fact] | None = None, evidence: list[str] | None = None,
    ) -> None:
        candidates = candidates or []
        resolution = "resolved" if target else "candidate" if candidates else "unresolved"
        rid = stable_id("relation", source.id, kind, target.id if target else name)
        previous = self._relations_by_id.get(rid)
        if previous:
            previous.source_ref_ids = sorted(set(previous.source_ref_ids + (evidence or source.source_ref_ids)))
            previous.candidates = sorted(set(previous.candidates + [f.id for f in candidates]))
            if previous.resolution == "unresolved" and previous.candidates:
                previous.resolution = "candidate"
            return
        relation = Relation(
            id=rid, from_id=source.id, type=kind, to_id=target.id if target else None,
            target_name=name, resolution=resolution,
            source_ref_ids=evidence or source.source_ref_ids,
            candidates=[f.id for f in candidates],
        )
        self.relations.append(relation)
        self._relations_by_id[rid] = relation

    def _find_python_path(self, module: str) -> str | None:
        exact = [p for p, m in self.py_modules.items() if m == module]
        matches = exact or [p for p, m in self.py_modules.items() if m.endswith("." + module)]
        return matches[0] if len(matches) == 1 else None

    def _python_imports(self, path: str, tree: ast.AST) -> dict[str, str]:
        aliases: dict[str, str] = {}
        package = self.py_modules[path].split(".")
        if not path.endswith("/__init__.py"):
            package = package[:-1]
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                prefix = ".".join(package[:len(package) - node.level + 1]) if node.level else ""
                module = ".".join(p for p in (prefix, node.module or "") if p)
                for alias in node.names:
                    if alias.name != "*":
                        aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        aliases.pop(target.id, None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                aliases.pop(node.name, None)
        return aliases

    def _expand_name(self, path: str, name: str) -> str:
        first, _, rest = name.partition(".")
        return self.imports.get(path, {}).get(first, first) + ("." + rest if rest else "")

    def _router_key(self, path: str, expression: str) -> str | None:
        expanded = self._expand_name(path, expression)
        if expanded != expression or "." in expanded:
            module, _, obj = expanded.rpartition(".")
            target = self._find_python_path(module)
            return f"{target}:{obj}" if target else None
        return f"{path}:{expression}"

    def _python_entrypoints(self, path: str, tree: ast.Module) -> None:
        """Record physical guard lines and explicitly imported startup calls."""
        def statements(body: list[ast.stmt], aliases: dict[str, str]) -> None:
            for statement in body:
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                    continue
                if isinstance(statement, ast.ImportFrom):
                    for alias in statement.names:
                        if alias.name != "*":
                            aliases[alias.asname or alias.name] = f"{statement.module or ''}.{alias.name}" if not statement.level else ""
                    continue
                if (isinstance(statement, ast.If) and isinstance(statement.test, ast.Compare)
                        and _name(statement.test.left) == "__name__" and len(statement.test.ops) == 1
                        and isinstance(statement.test.ops[0], ast.Eq) and len(statement.test.comparators) == 1
                        and _literal(statement.test.comparators[0]) == "__main__"):
                    self._fact(path, "interface", "Python 启动入口", statement.lineno, statement.lineno, interface_type="cli")
                    statements(statement.body, dict(aliases))
                elif isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)) and isinstance(statement.value, ast.Call):
                    call = statement.value
                    expression = _name(call.func)
                    first, _, remainder = expression.partition(".")
                    expanded = aliases.get(first, "") + ("." + remainder if remainder else "")
                    if expanded == "uvicorn.run":
                        application = call.args[0] if call.args else next((keyword.value for keyword in call.keywords if keyword.arg == "app"), None)
                        literal = _literal(application)
                        reference = _name(application)
                        if isinstance(literal, str) and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", literal):
                            value, application_kind = literal, "import_string"
                        elif reference:
                            value, application_kind = reference, "reference"
                        else:
                            self.issues.append(f"ASGI 启动参数动态或缺失，未提取：{path}:{call.lineno}")
                            value = ""
                        if value:
                            self.frameworks.add("Uvicorn")
                            self._fact(path, "entrypoint", "uvicorn.run", call.lineno, call.end_lineno or call.lineno,
                                       entry_type="asgi", application=value, application_kind=application_kind)
                # Any other conditional import/binding has no fixed startup
                # outcome. Nested definitions are never treated as startup.
                written = _scope_bindings(ast.Module(body=[statement], type_ignores=[]))
                for child in ast.walk(statement):
                    if isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del)):
                        written.add(_name(child).partition(".")[0])
                    elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Del):
                        written.add(child.id)
                for binding in written:
                    aliases.pop(binding, None)

        statements(tree.body, {})

    def _python(self, path: str, text: str) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            raise ValueError(f"Python 文件无法解析：{path}") from None
        self.imports[path] = self._python_imports(path, tree)
        self._python_entrypoints(path, tree)
        global_bindings = _scope_bindings(tree)
        # Plain module declarations are potential targets, not reassignment.
        global_bindings.difference_update(n.name for n in tree.body if isinstance(n, _PY_SCOPES) and not isinstance(n, ast.Lambda))
        global_bindings.difference_update(self.imports[path])
        self.global_bindings[path] = sorted(global_bindings)
        top_level_assignments = {id(n) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
        top_level_calls = {id(n.value) for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)}
        node_symbols: dict[int, Fact] = {}

        def visit(node: ast.AST, scope: str = "", scope_kind: str = "module", enclosing_bindings: set[str] | None = None) -> None:
            lexical_bindings = enclosing_bindings or set()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    qualified = f"{scope}.{child.name}" if scope else child.name
                    if isinstance(child, ast.ClassDef):
                        kind, signature = "class", qualified
                    else:
                        kind = "method" if scope_kind == "class" else "function"
                        # 默认参数值不进入事实和模型输出。
                        args = [a.arg for a in child.args.posonlyargs + child.args.args + child.args.kwonlyargs]
                        signature = f"{qualified}({','.join(args)})"
                    fact = self._fact(path, "symbol", qualified, child.lineno, child.end_lineno or child.lineno,
                                      symbol_kind=kind, signature=signature, language="python")
                    if not isinstance(child, ast.ClassDef):
                        assigned = _scope_bindings(child)
                        parameters = {a.arg for a in child.args.posonlyargs + child.args.args + child.args.kwonlyargs}
                        if child.args.vararg:
                            parameters.add(child.args.vararg.arg)
                        if child.args.kwarg:
                            parameters.add(child.args.kwarg.arg)
                        fact.attributes["assigned_names"] = sorted(assigned)
                        fact.attributes["shadowed_names"] = sorted(assigned | parameters)
                        if lexical_bindings:
                            fact.attributes["closure_bindings"] = sorted(lexical_bindings)
                    self.symbols[child.name].append(fact)
                    node_symbols[id(child)] = fact
                    if child.name.startswith("test_") or child.name.endswith("Test"):
                        self._fact(path, "test", qualified, child.lineno, child.end_lineno or child.lineno)
                    if isinstance(child, ast.ClassDef):
                        # A class namespace is not an enclosing lexical scope
                        # for methods; existing function closures remain valid.
                        visit(child, qualified, "class", lexical_bindings)
                    else:
                        visit(child, qualified, "function", lexical_bindings | assigned | parameters)
                else:
                    visit(child, scope, scope_kind, lexical_bindings)

        visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                constructor = self._expand_name(path, _name(node.value.func))
                prefix_node = next((k.value for k in node.value.keywords if k.arg == "prefix"), None)
                if constructor in {"fastapi.FastAPI", "fastapi.APIRouter"}:
                    self.frameworks.add("FastAPI")
                    prefix = _literal(prefix_node) if prefix_node else ""
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        if isinstance(target, ast.Name):
                            key = f"{path}:{target.id}"
                            valid = id(node) in top_level_assignments and isinstance(prefix, str) and (not prefix or (prefix.startswith("/") and not prefix.endswith("/")))
                            if key in self.routers:
                                valid = False
                            self.routers[key] = prefix if valid else None
                            if not valid:
                                self.issues.append(f"路由实例动态、重复或前缀无效，未展开：{path}:{node.lineno}")
                            if constructor == "fastapi.FastAPI" and valid:
                                self.apps.add(key)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router":
                if id(node) not in top_level_calls:
                    self.issues.append(f"路由挂载处于动态/局部作用域，未展开：{path}:{node.lineno}")
                    continue
                if not node.args:
                    continue
                parent = self._router_key(path, _name(node.func.value))
                child = self._router_key(path, _name(node.args[0]))
                prefix_node = next((k.value for k in node.keywords if k.arg == "prefix"), None)
                prefix = _literal(prefix_node) if prefix_node else ""
                if parent and child:
                    self.mounts.append((parent, child, prefix if isinstance(prefix, str) else None))
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
                    if targets and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in targets):
                        table = _literal(stmt.value)
                        if isinstance(table, str):
                            self.frameworks.add("SQLAlchemy")
                            f = self._fact(path, "table", table, stmt.lineno, stmt.end_lineno or stmt.lineno,
                                           model=node_symbols[id(node)].name, declaration="__tablename__")
                            self.tables[table].append(f)
                            self._relation(node_symbols[id(node)], "MAPS_TO", f, table)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = node_symbols[id(node)]
                for decorator in node.decorator_list:
                    call = decorator if isinstance(decorator, ast.Call) else None
                    if call and isinstance(call.func, ast.Attribute) and call.func.attr in _ROUTE_METHODS:
                        route_node = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "path"), None)
                        route = _literal(route_node)
                        if "." in owner.name or (isinstance(route, str) and route and not route.startswith("/")):
                            self.issues.append(f"局部或无效路由声明，未展开：{path}:{decorator.lineno}")
                            continue
                        router = self._router_key(path, _name(call.func.value))
                        methods = [call.func.attr.upper()]
                        if call.func.attr == "api_route":
                            method_value = next((k.value for k in call.keywords if k.arg == "methods"), None)
                            methods = _literal(method_value) if method_value else ["GET"]
                        self.routes.append({"path": path, "owner": owner, "router": router, "route": route,
                                            "methods": methods, "line": decorator.lineno})
                    decorator_name = self._expand_name(path, _name(call.func if call else decorator))
                    if decorator_name.startswith("celery.") or decorator_name.endswith("celery_app.task"):
                        f = self._fact(path, "interface", "任务 " + owner.name, decorator.lineno,
                                       decorator.end_lineno or decorator.lineno, interface_type="task")
                        self._relation(f, "HANDLED_BY", owner, owner.name)
                # 只收集当前作用域，不把嵌套函数的调用归属外层。
                def collect(current: ast.AST, comprehension_bindings: set[str] | None = None) -> None:
                    hidden_names = comprehension_bindings or set()
                    if current is not node and isinstance(current, _PY_SCOPES):
                        return
                    if isinstance(current, _PY_COMPREHENSIONS):
                        # The first iterable is evaluated in the containing
                        # scope. Later generators and result expressions use
                        # the progressively bound comprehension names.
                        nested_names = set(hidden_names)
                        for generator in current.generators:
                            collect(generator.iter, nested_names)
                            nested_names.update(n.id for n in ast.walk(generator.target) if isinstance(n, ast.Name))
                            for condition in generator.ifs:
                                collect(condition, nested_names)
                        for field in ("elt", "key", "value"):
                            value = getattr(current, field, None)
                            if value is not None:
                                collect(value, nested_names)
                        return
                    if isinstance(current, ast.Call):
                        expression = _name(current.func)
                        shadowed = expression.split(".")[0] in set(owner.attributes.get("shadowed_names", []) + owner.attributes.get("closure_bindings", [])) | hidden_names
                        expanded = "" if shadowed else self._expand_name(path, expression)
                        ref = self._ref(path, current.lineno, current.end_lineno or current.lineno)
                        self.calls.append((path, owner, expression, ref))
                        if expression.split(".")[0] in hidden_names:
                            owner.attributes.setdefault("scope_shadowed_calls", []).append(ref)
                        if expanded in {"sqlalchemy.select", "sqlalchemy.update", "sqlalchemy.delete", "sqlalchemy.insert"}:
                            if current.args:
                                model = self._expand_name(path, _name(current.args[0]))
                                if _name(current.args[0]).split(".")[0] in set(owner.attributes.get("shadowed_names", []) + owner.attributes.get("closure_bindings", []) + self.global_bindings.get(path, [])) | hidden_names:
                                    model = ""
                                access = "READS_TABLE" if expanded.endswith("select") else "WRITES_TABLE"
                                if model:
                                    self.accesses.append((owner, model, access, [ref], "model"))
                                else:
                                    self.issues.append(f"SQLAlchemy 模型参数动态或被遮蔽：{path}:{current.lineno}")
                        if expanded == "sqlalchemy.text" and current.args:
                            sql = _literal(current.args[0])
                            if isinstance(sql, str):
                                self._sql_access(owner, sql, [ref])
                        if isinstance(current.func, ast.Attribute) and current.func.attr in {"add", "add_all", "delete"}:
                            # session 实例必须有明确 Session/AsyncSession 类型标注。
                            receiver = _name(current.func.value)
                            typed = receiver not in owner.attributes.get("assigned_names", []) and any(
                                a.arg == receiver and self._expand_name(path, _name(a.annotation)) in {
                                    "sqlalchemy.orm.Session", "sqlalchemy.ext.asyncio.AsyncSession",
                                } for a in node.args.args + node.args.kwonlyargs
                            )
                            if typed and current.args and isinstance(current.args[0], ast.Call):
                                model = self._expand_name(path, _name(current.args[0].func))
                                if _name(current.args[0].func).split(".")[0] in set(owner.attributes.get("shadowed_names", []) + owner.attributes.get("closure_bindings", []) + self.global_bindings.get(path, [])) | hidden_names:
                                    model = ""
                                if model:
                                    self.accesses.append((owner, model, "WRITES_TABLE", [ref], "model"))
                                else:
                                    self.issues.append(f"SQLAlchemy 写入模型动态或被遮蔽：{path}:{current.lineno}")
                    for sub in ast.iter_child_nodes(current):
                        collect(sub, hidden_names)
                for statement in node.body:
                    collect(statement)
        # 环境变量使用与 Settings 声明，仅保留键和位置。
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                constructor = self._expand_name(path, _name(statement.value.func)) if isinstance(statement.value, ast.Call) else ""
                if constructor not in {"fastapi.FastAPI", "fastapi.APIRouter"}:
                    for target in targets:
                        key = f"{path}:{target.id}" if isinstance(target, ast.Name) else ""
                        if key in self.routers:
                            self.routers[key] = None
                            self.issues.append(f"路由实例被重新赋值，未展开：{path}:{statement.lineno}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) in {"os.getenv", "os.environ.get"} and node.args:
                key = _literal(node.args[0])
                if isinstance(key, str):
                    self._fact(path, "config", key, node.lineno, node.end_lineno or node.lineno, usage="environment")
            if isinstance(node, ast.ClassDef) and any(_name(b).endswith("BaseSettings") for b in node.bases):
                for sub in node.body:
                    if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                        self._fact(path, "config", sub.target.id, sub.lineno, sub.end_lineno or sub.lineno,
                                   value_type=_name(sub.annotation), usage="settings")

    def _generic(self, path: str, text: str) -> None:
        parsed = parse_file(str(self.root / path), path)
        if parsed is None:
            raise ValueError(f"语言解析器不可用：{path}")
        classes = [n for n in parsed.nodes if n.kind == "class"]
        package = re.search(r"\bpackage\s+([\w.]+)\s*;", text) if path.endswith(".java") else None
        prefix = package.group(1) + "." if package else ""
        for node in parsed.nodes:
            parents = sorted([c for c in classes if c.line_start < node.line_start <= c.line_end], key=lambda c: c.line_start)
            scope = ".".join(c.name for c in parents)
            # Go receiver is explicit in signature; JS/TS class scope follows source intervals.
            if parsed.language == "go" and node.kind == "method" and "." in node.signature:
                scope = node.signature.split("(")[0].split(".")[0]
            name = prefix + (scope + "." if scope else "") + node.name
            # 参数中的字符串、默认值及注解值不成为持久化签名。
            signature = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''', "<literal>", node.signature)
            signature = re.sub(r"=\s*[^,)]+", "", signature)
            f = self._fact(path, "symbol", name, node.line_start, node.line_end,
                           symbol_kind=node.kind, signature=signature, language=parsed.language)
            self.symbols[node.name].append(f)
            if parsed.language != "java":
                for call in node.calls:
                    self.calls.append((path, f, call, f.source_ref_ids[0]))
            for base in node.bases:
                self._relation(f, "EXTENDS", None, base)
            if node.name.startswith("test") or node.name.endswith("Test"):
                self._fact(path, "test", name, node.line_start, node.line_end)
        if parsed.language == "java":
            self._java_frameworks(path, text)
        elif parsed.language in {"javascript", "typescript", "go"}:
            self.issues.append(f"仅提供通用符号和候选关系，框架级提取未适配：{path}")

    def _annotations(self, text: str, name: str) -> list[str] | None:
        match = re.search(r"@(?:[\w.]+\.)?" + name + r"\s*(?:\((.*?)\))?", text, re.S)
        if not match:
            return None
        body = match.group(1) or ""
        # 只接受直接字符串及其数组，不把常量或表达式猜成路由。
        path_value = re.search(r"(?:value|path)\s*=\s*(\{[^}]*\}|\"(?:\\.|[^\"])*\")", body)
        value = path_value.group(1) if path_value else body.strip()
        if not path_value and "=" in value:
            value = ""
        if not value:
            return [""]
        strings = re.findall(r'"(?:\\.|[^"\\])*"', value)
        if not strings or re.sub(r'"(?:\\.|[^"\\])*"|[{},\s]', "", value):
            return []
        return [json.loads(s) for s in strings]

    def _java_frameworks(self, path: str, text: str) -> None:
        # tree-sitter 用于隔离类/方法的注解区域，避免相邻声明的正则污染。
        from src.core.code_graph.parser import _get_parser

        source = text.encode()
        self.java_imports[path] = {name.rsplit(".", 1)[-1]: name for name in re.findall(r"\bimport\s+([\w.]+)\s*;", text)}
        wildcards = re.findall(r"\bimport\s+([\w.]+)\.\*\s*;", text)
        def framework_annotation(name: str, package: str) -> bool:
            return self.java_imports[path].get(name, "").startswith(package + ".") or package in wildcards or "@" + package + "." + name in text
        tree = _get_parser("java").parse(source)
        if tree.root_node.has_error:
            raise ValueError(f"Java 文件存在语法错误：{path}")
        file_symbols = [f for f in self.facts.values() if f.kind == "symbol" and f.attributes["path"] == path]

        def walk(node, class_prefixes: list[str] | None = None, class_methods: list[str] | None = None) -> None:
            prefixes = class_prefixes if class_prefixes is not None else [""]
            permitted_methods = class_methods
            modifiers = next((c for c in node.children if c.type == "modifiers"), None)
            annotations = source[modifiers.start_byte:modifiers.end_byte].decode() if modifiers else ""
            if node.type in {"class_declaration", "interface_declaration"}:
                mapped = self._annotations(annotations, "RequestMapping") if framework_annotation("RequestMapping", "org.springframework.web.bind.annotation") else None
                if mapped is not None:
                    self.frameworks.add("Spring")
                    prefixes = mapped
                    permitted_methods = re.findall(r"RequestMethod\.(\w+)", annotations) or None
                table_match = re.search(r"@(?:[\w.]+\.)?Table\s*\(.*?name\s*=\s*\"([^\"]+)\"", annotations, re.S)
                entities = [f for f in file_symbols if self.refs[f.source_ref_ids[0]].line_start == node.start_point[0] + 1]
                if table_match and any(framework_annotation("Entity", package) and framework_annotation("Table", package) for package in ("jakarta.persistence", "javax.persistence")) and re.search(r"@(?:[\w.]+\.)?Entity\b", annotations):
                    self.frameworks.add("JPA")
                    model = entities[0].name if entities else ""
                    schema = re.search(r"\bschema\s*=\s*\"([^\"]+)\"", annotations)
                    table_name = (schema.group(1) + "." if schema else "") + table_match.group(1)
                    f = self._fact(path, "table", table_name, node.start_point[0] + 1,
                                   modifiers.end_point[0] + 1, model=model, declaration="@Table")
                    self.tables[f.name].append(f)
                    if entities:
                        self._relation(entities[0], "MAPS_TO", f, f.name)
            if node.type == "method_declaration":
                owners = [f for f in file_symbols if self.refs[f.source_ref_ids[0]].line_start == node.start_point[0] + 1]
                if owners:
                    owner = owners[0]
                    owner.attributes["dispatch_fixed"] = bool(re.search(r"\b(?:private|static|final)\b", annotations))
                    owner.attributes["is_static"] = bool(re.search(r"\bstatic\b", annotations))
                    params = node.child_by_field_name("parameters")
                    owner.attributes["parameter_count"] = len([c for c in params.named_children if c.type in {"formal_parameter", "spread_parameter"}]) if params else 0
                    owner.attributes["varargs"] = bool(params and any(c.type == "spread_parameter" for c in params.named_children))
                    return_type = node.child_by_field_name("type")
                    param_text = source[params.start_byte:params.end_byte].decode() if params else ""
                    owner.attributes["cli_main"] = bool(
                        owner.name.endswith(".main") and owner.attributes["is_static"]
                        and re.search(r"\bpublic\b", annotations)
                        and return_type and source[return_type.start_byte:return_type.end_byte] == b"void"
                        and re.fullmatch(r"\(\s*(?:java\.lang\.)?String\s*(?:\[\s*\]|\.\.\.)\s*\w+\s*\)", param_text))
                    def calls(current) -> None:
                        if current.type in {"class_declaration", "lambda_expression"}:
                            return
                        if current.type == "method_invocation":
                            name_node = current.child_by_field_name("name")
                            object_node = current.child_by_field_name("object")
                            arguments = current.child_by_field_name("arguments")
                            name = source[name_node.start_byte:name_node.end_byte].decode() if name_node else ""
                            obj = source[object_node.start_byte:object_node.end_byte].decode() if object_node else ""
                            ref = self._ref(path, current.start_point[0] + 1, current.end_point[0] + 1)
                            owner.attributes.setdefault("call_arities", {})[ref] = len(arguments.named_children) if arguments else 0
                            self.calls.append((path, owner, f"{obj}.{name}" if obj else name, ref))
                        for child in current.named_children:
                            calls(child)
                    body = node.child_by_field_name("body")
                    if body:
                        calls(body)
                    for annotation in ("GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping", "RequestMapping"):
                        if not framework_annotation(annotation, "org.springframework.web.bind.annotation"):
                            continue
                        routes = self._annotations(annotations, annotation)
                        if routes is None:
                            continue
                        self.frameworks.add("Spring")
                        methods = [annotation.removesuffix("Mapping").upper()]
                        if annotation == "RequestMapping":
                            methods = re.findall(r"RequestMethod\.(\w+)", annotations) or ["ANY"]
                        if permitted_methods:
                            methods = permitted_methods if methods == ["ANY"] else [m for m in methods if m in permitted_methods]
                        if not routes or not prefixes:
                            self.issues.append(f"Spring 路由含动态常量，未展开：{path}:{node.start_point[0] + 1}")
                        for prefix_path in prefixes:
                            for route in routes:
                                for method in methods:
                                    name = f"{method} {_join_path(prefix_path, route)}"
                                    f = self._fact(path, "interface", name, node.start_point[0] + 1,
                                                   modifiers.end_point[0] + 1, interface_type="http")
                                    self._relation(f, "HANDLED_BY", owner, owner.name)
                    for annotation in ("Select", "Insert", "Update", "Delete"):
                        if not framework_annotation(annotation, "org.apache.ibatis.annotations"):
                            continue
                        match = re.search(r"@" + annotation + r"\s*\((.*?)\)", annotations, re.S)
                        if match:
                            self.frameworks.add("MyBatis")
                            strings = re.findall(r'"(?:\\.|[^"\\])*"', match.group(1))
                            if strings:
                                self._sql_access(owner, " ".join(json.loads(s) for s in strings), owner.source_ref_ids)
            for child in node.children:
                walk(child, prefixes, permitted_methods)

        walk(tree.root_node)

    def _sql_access(self, owner: Fact, sql: str, evidence: list[str]) -> None:
        cleaned = re.sub(r"/\*.*?\*/|--[^\n]*|'(?:''|[^'])*'", " ", sql, flags=re.S)
        if "${" in cleaned:
            self.issues.append(f"SQL 包含动态标识符：{owner.attributes['path']}")
            return
        if re.search(r"\bWITH\b", cleaned, re.I):
            self.issues.append(f"SQL 包含 CTE，未把别名视为数据库表：{owner.attributes['path']}")
            return
        if re.search(r"\b(?:FROM|JOIN|INTO|UPDATE)\s*[\"`\[]", cleaned, re.I):
            self.issues.append(f"SQL 带方言引用标识符，未提取：{owner.attributes['path']}")
            return
        writes = re.findall(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([\w.]+)", cleaned, re.I)
        reads = re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", cleaned, re.I)
        for name in sorted(set(writes)):
            self.accesses.append((owner, name, "WRITES_TABLE", evidence, "table"))
        for name in sorted(set(reads) - set(writes)):
            self.accesses.append((owner, name, "READS_TABLE", evidence, "table"))

    def _engineering(self, path: str, text: str) -> None:
        name = PurePosixPath(path).name
        def declaration(pattern: str) -> tuple[int, int]:
            matches = list(re.finditer(pattern, text))
            if len(matches) != 1:
                return 1, self.snapshot.files[path]["lines"]
            match = matches[0]
            return text.count("\n", 0, match.start()) + 1, text.count("\n", 0, match.end()) + 1
        for symbol in [f for f in self.facts.values() if f.kind == "symbol" and f.attributes["path"] == path]:
            if ((path.endswith(".go") and symbol.name == "main") or
                    (path.endswith(".java") and symbol.attributes.get("cli_main"))):
                ref = self.refs[symbol.source_ref_ids[0]]
                entry = self._fact(path, "interface", "启动 " + symbol.name, ref.line_start, ref.line_start, interface_type="cli")
                self._relation(entry, "HANDLED_BY", symbol, symbol.name)
        if name in {"Dockerfile", "Procfile", "Makefile"}:
            for line_no, line in enumerate(source_lines(text), 1):
                match = re.match(r"\s*(CMD|ENTRYPOINT)\b", line) if name == "Dockerfile" else re.match(r"([\w.-]+)\s*:", line)
                if match:
                    self._fact(path, "entrypoint", match.group(1), line_no, line_no, entry_type="deployment")
        if name == "package.json":
            try:
                data = json.loads(text)
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    for dependency in data.get(section, {}):
                        start, end = declaration(re.escape(json.dumps(dependency, ensure_ascii=False)) + r"\s*:")
                        self._fact(path, "dependency", dependency, start, end, package_manager="npm")
                for script in data.get("scripts", {}):
                    start, end = declaration(re.escape(json.dumps(script, ensure_ascii=False)) + r"\s*:")
                    self._fact(path, "entrypoint", script, start, end, entry_type="npm_script")
            except (json.JSONDecodeError, AttributeError, TypeError):
                self.issues.append(f"工程清单无法解析：{path}")
        elif name == "pyproject.toml":
            try:
                data = tomllib.loads(text)
                for dependency in data.get("project", {}).get("dependencies", []):
                    package = re.split(r"[<>=!~\[\s;@]", dependency, 1)[0]
                    if package:
                        start, end = declaration(r"['\"]" + re.escape(dependency) + r"['\"]")
                        self._fact(path, "dependency", package, start, end, package_manager="python")
            except (tomllib.TOMLDecodeError, AttributeError, TypeError):
                self.issues.append(f"工程清单无法解析：{path}")
        elif name == "pom.xml":
            try:
                tree = ET.fromstring(text)
                for dep in tree.iter():
                    if dep.tag.rsplit("}", 1)[-1] == "dependency":
                        parts = {c.tag.rsplit("}", 1)[-1]: c.text for c in dep}
                        if parts.get("artifactId"):
                            start, end = declaration(r"<(?:\w+:)?artifactId>\s*" + re.escape(parts["artifactId"]) + r"\s*</(?:\w+:)?artifactId>")
                            self._fact(path, "dependency", f"{parts.get('groupId', '')}:{parts['artifactId']}", start, end, package_manager="maven")
            except ET.ParseError:
                self.issues.append(f"工程清单无法解析：{path}")
        elif path.endswith(".xml") and "<mapper" in text:
            try:
                root = ET.fromstring(text)
                namespace = root.attrib.get("namespace", "")
                for element in root:
                    if element.tag in {"select", "insert", "update", "delete"}:
                        sql = " ".join(element.itertext())
                        statement = namespace + "." + element.attrib.get("id", "")
                        owner = self._fact(path, "sql_statement", statement, 1, self.snapshot.files[path]["lines"], namespace=namespace)
                        self._sql_access(owner, sql, owner.source_ref_ids)
                        self.frameworks.add("MyBatis")
            except ET.ParseError:
                self.issues.append(f"Mapper XML 无法解析：{path}")
        elif name == "requirements.txt":
            for line_no, line in enumerate(source_lines(text), 1):
                match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[|[<>=!~\s]|$)", line)
                if match:
                    self._fact(path, "dependency", match.group(1), line_no, line_no, package_manager="pip")
        elif name in {"build.gradle", "build.gradle.kts"}:
            for match in re.finditer(r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation)\s*\(?\s*['\"]([^'\"]+)['\"]", text):
                line = text.count("\n", 0, match.start()) + 1
                self._fact(path, "dependency", ":".join(match.group(1).split(":")[:2]), line, line, package_manager="gradle")
        elif name == "go.mod":
            for line_no, line in enumerate(source_lines(text), 1):
                match = re.match(r"\s*(?:require\s+)?([\w.-]+/[\w./-]+)\s+v[\w.+-]+", line)
                if match:
                    self._fact(path, "dependency", match.group(1), line_no, line_no, package_manager="go")
        if path.endswith(".sql"):
            for match in re.finditer(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", text, re.I):
                line = text.count("\n", 0, match.start()) + 1
                f = self._fact(path, "table", match.group(1), line, line, declaration="DDL")
                self.tables[f.name].append(f)
        if name == ".env.example" or path.endswith((".properties", ".ini", ".cfg")):
            for number, line in enumerate(source_lines(text), 1):
                match = re.match(r"\s*([A-Za-z_][\w.-]*)\s*=", line)
                if match:
                    self._fact(path, "config", match.group(1), number, number, usage="declaration")
        if path.endswith((".yaml", ".yml")):
            parents: list[tuple[int, str]] = []
            for number, line in enumerate(source_lines(text), 1):
                match = re.match(r"(\s*)([A-Za-z_][\w.-]*)\s*:", line)
                if match:
                    indent = len(match.group(1))
                    while parents and parents[-1][0] >= indent:
                        parents.pop()
                    key = ".".join([p[1] for p in parents] + [match.group(2)])
                    self._fact(path, "config", key, number, number, usage="yaml_key")
                    parents.append((indent, match.group(2)))

    def _resolve(self) -> None:
        # 声明映射与静态导入定位，任何歧义都会保留候选。
        for path, owner, expression, ref in self.calls:
            candidates = self.symbols.get(expression.rsplit(".", 1)[-1], [])
            target = None
            if path.endswith(".py"):
                expanded = self._expand_name(path, expression)
                candidates = self.symbols.get(expanded.rsplit(".", 1)[-1], candidates)
                shadowed = set(owner.attributes.get("shadowed_names", []) + owner.attributes.get("closure_bindings", []) + self.global_bindings.get(path, []))
                if expression.split(".")[0] in shadowed or ref in owner.attributes.get("scope_shadowed_calls", []):
                    exact = []
                elif expanded != expression:
                    module, _, name = expanded.rpartition(".")
                    imported_path = self._find_python_path(module)
                    exact = [f for f in candidates if f.attributes["path"] == imported_path and f.name == name]
                    candidates = exact
                elif expression.startswith(("self.", "cls.")):
                    # Python 允许覆写/猴子补丁，不把实例分派当作确定调用。
                    exact = []
                elif "." not in expression:
                    exact = [f for f in candidates if f.attributes["path"] == path and f.name == expression
                             and f.attributes["symbol_kind"] in {"function", "class"}]
                else:
                    exact = []
                if len(exact) == 1 and not exact[0].attributes.get("redefined"):
                    target = exact[0]
            elif path.endswith(".java"):
                obj, _, method = expression.rpartition(".")
                class_name = owner.name.rpartition(".")[0]
                if not obj or obj == "this":
                    qualifier = class_name
                else:
                    qualifier = self.java_imports.get(path, {}).get(obj, obj)
                arity = owner.attributes.get("call_arities", {}).get(ref)
                exact = [f for f in candidates if f.name == qualifier + "." + (method or expression)
                         and f.attributes.get("parameter_count") == arity and not f.attributes.get("varargs")]
                if len(exact) == 1 and exact[0].attributes.get("dispatch_fixed"):
                    target = exact[0]
            self._relation(owner, "CALLS", target, expression, candidates if not target else [], [ref])
        for owner, name, kind, evidence, access_type in self.accesses:
            if access_type == "model":
                model_name = name.rsplit(".", 1)[-1]
                matches = [f for fs in self.tables.values() for f in fs if f.attributes.get("model", "").rsplit(".", 1)[-1] == model_name]
                module = name.rpartition(".")[0]
                target_path = self._find_python_path(module) if module else owner.attributes["path"]
                local = [f for f in matches if f.attributes["path"] == target_path]
                matches = local or matches
                # 跨模块仅凭同名模型时保留候选。
                resolved = len(local) == 1
            else:
                matches = self.tables.get(name, [])
                resolved = len(matches) == 1
            self._relation(owner, kind, matches[0] if resolved else None, name, matches if not resolved else [], evidence)
        for route in self.routes:
            router = route["router"]
            if router not in self.routers:
                self.issues.append(f"路由实例无法静态确定，未展开：{route['path']}:{route['line']}")
                continue
            def prefixes(key: str, visited: set[str]) -> list[str]:
                if key in visited or self.routers.get(key) is None:
                    return []
                own = self.routers[key] or ""
                if key in self.apps:
                    return [own]
                parents = [(p, pre) for p, c, pre in self.mounts if c == key and p in self.routers]
                if not parents:
                    self.issues.append(f"路由未找到应用挂载点：{key}")
                    return []
                return [_join_path(base, pre, own) for p, pre in parents if pre is not None
                        for base in prefixes(p, visited | {key})]
            paths = prefixes(router, set())
            if not isinstance(route["route"], str) or not isinstance(route["methods"], list) or not paths:
                self.issues.append(f"FastAPI 路由或前缀动态/未挂载：{route['path']}:{route['line']}")
                continue
            for prefix in paths:
                for method in route["methods"]:
                    if isinstance(method, str):
                        name = f"{method.upper()} {_join_path(prefix, route['route'])}"
                        f = self._fact(route["path"], "interface", name, route["line"], route["line"], interface_type="http")
                        self._relation(f, "HANDLED_BY", route["owner"], route["owner"].name)
        for fact in list(self.facts.values()):
            if fact.kind == "sql_statement":
                candidates = [f for fs in self.symbols.values() for f in fs if f.name == fact.name]
                self._relation(fact, "IMPLEMENTED_BY", candidates[0] if len(candidates) == 1 else None, fact.name,
                               candidates if len(candidates) != 1 else [])

    def extract(self) -> FactBundle:
        """提取完整事实集合；未变化的通用语言文件可复用声明。"""
        for path in self.snapshot.files:
            if PurePosixPath(path).name in {"pom.xml", "build.gradle", "build.gradle.kts", "package.json", "pyproject.toml", "go.mod"}:
                parent = str(PurePosixPath(path).parent)
                if parent != ".":
                    self.build_roots.add(parent)
        for path, meta in sorted(self.snapshot.files.items()):
            if not meta["available"]:
                continue
            old = self.previous.observations.get(path) if self.previous else None
            if old and self.previous.files.get(path, {}).get("blob_sha") == meta["blob_sha"] and old["module"] == self._module(path):
                self._restore(path, old)
                self.observations[path] = {**old, "refs": [self.refs[r["id"]].model_dump() for r in old["refs"]]}
                self.reused_files += 1
                continue
            issues_before, frameworks_before = len(self.issues), set(self.frameworks)
            calls_before, routes_before = len(self.calls), len(self.routes)
            mounts_before, accesses_before, relations_before = len(self.mounts), len(self.accesses), len(self.relations)
            text = (self.root / path).read_text(encoding="utf-8-sig")
            self._fact(path, "file", path, 1, min(20, meta["lines"]), language=detect_language(path) or "engineering")
            if path.endswith(".py"):
                self._python(path, text)
            elif detect_language(path):
                self._generic(path, text)
            elif PurePosixPath(path).suffix in {".rs", ".c", ".cpp", ".h", ".cs", ".kt", ".scala", ".rb", ".php", ".swift", ".dart"}:
                self.issues.append(f"语言未适配，仅保存文件结构：{path}")
            self._engineering(path, text)
            self.observations[path] = {
                "module": self._module(path),
                "facts": [f.model_dump() for f in self.facts.values() if f.attributes["path"] == path],
                "refs": [r.model_dump() for r in self.refs.values() if r.path == path],
                "relations": [r.model_dump() for r in self.relations[relations_before:]],
                "imports": self.imports.get(path, {}),
                "globals": self.global_bindings.get(path, []),
                "java_imports": self.java_imports.get(path, {}),
                "calls": [[p, f.id, n, r] for p, f, n, r in self.calls[calls_before:]],
                "routes": [{**r, "owner": r["owner"].id} for r in self.routes[routes_before:]],
                "routers": {k: v for k, v in self.routers.items() if k.startswith(path + ":")},
                "mounts": self.mounts[mounts_before:], "apps": [k for k in self.apps if k.startswith(path + ":")],
                "accesses": [[f.id, n, t, e, a] for f, n, t, e, a in self.accesses[accesses_before:]],
                "issues": self.issues[issues_before:], "frameworks": sorted(self.frameworks - frameworks_before),
            }
        self._resolve()
        # TESTED_BY 只关联测试中已经确认的调用，不猜测文件名对应的业务模块。
        tests = {(f.attributes["path"], f.name): f for f in self.facts.values() if f.kind == "test"}
        for relation in list(self.relations):
            source = self.facts.get(relation.from_id)
            test = tests.get((source.attributes["path"], source.name)) if source else None
            if test and relation.type == "CALLS" and relation.to_id:
                self._relation(self.facts[relation.to_id], "TESTED_BY", test, source.name, evidence=relation.source_ref_ids)
        return FactBundle(
            source_commit=self.snapshot.commit_sha, files=self.snapshot.files, modules=self.modules,
            facts=list(self.facts.values()), relations=self.relations, source_refs=list(self.refs.values()),
            issues=sorted(set(self.issues)), frameworks=sorted(self.frameworks),
            source_fingerprint=self.snapshot.fingerprint,
            observations=self.observations,
        )

    def _restore(self, path: str, data: dict) -> None:
        for raw in data["refs"]:
            ref = SourceRef.model_validate({**raw, "commit_sha": self.snapshot.commit_sha})
            self.refs[ref.id] = ref
        for raw in data["facts"]:
            fact = Fact.model_validate(raw)
            self.facts[fact.id] = fact
            if fact.kind == "symbol":
                self.symbols[fact.name.rsplit(".", 1)[-1]].append(fact)
            elif fact.kind == "table":
                self.tables[fact.name].append(fact)
        restored_relations = [Relation.model_validate(r) for r in data["relations"]]
        self.relations.extend(restored_relations)
        self._relations_by_id.update((r.id, r) for r in restored_relations)
        self.imports[path] = data["imports"]
        self.global_bindings[path] = data.get("globals", [])
        self.java_imports[path] = data.get("java_imports", {})
        self.calls.extend((p, self.facts[f], n, r) for p, f, n, r in data["calls"])
        self.routes.extend({**r, "owner": self.facts[r["owner"]]} for r in data["routes"])
        self.routers.update(data["routers"])
        self.mounts.extend(tuple(m) for m in data["mounts"])
        self.apps.update(data["apps"])
        self.accesses.extend((self.facts[f], n, t, e, a) for f, n, t, e, a in data["accesses"])
        self.issues.extend(data["issues"])
        self.frameworks.update(data["frameworks"])
