"""Audit fixed source and formal knowledge using an independent static oracle.

No repository code is imported or executed. No model, GitLab or network call is
made. Reports contain filtered names and source coordinates, never source text,
configuration values, model responses or reversible privacy mappings.
"""

import argparse
import ast
import hashlib
import hmac
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.privacy_filter import PrivacyFilter
from src.core.project_brain.privacy_provenance import MAX_INDEX_BYTES, parse_fingerprint_index
from src.core.project_brain.snapshot import source_lines

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_FRAMEWORKS = {"fastapi", "flask", "django", "sqlalchemy", "pydantic", "pydantic_settings", "uvicorn", "celery", "pymysql"}
_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}
_IDENTIFIER = re.compile(r"[A-Za-z_][\w.]*\Z")
_CATEGORIES = ("interface", "entrypoint", "data_table", "table_access", "data_model", "configuration", "test", "direct_call", "module")
_SERVICE_FILES = {"cards.json", "facts.json", "symbols.jsonl", "relations.jsonl", "source_refs.jsonl", "index.json"}


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _name(node.value)
        return f"{owner}.{node.attr}" if owner else ""
    return ""


def _literal(node: ast.AST | None) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_literal(value) for value in node.elts]
        return values if all(value is not None for value in values) else None
    return None


def _local_bindings(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    bindings = {arg.arg for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
    bindings.update(arg.arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None)

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, current: ast.Name) -> None:
            if isinstance(current.ctx, ast.Store):
                bindings.add(current.id)

        def visit_FunctionDef(self, current: ast.FunctionDef) -> None:
            bindings.add(current.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, current: ast.ClassDef) -> None:
            bindings.add(current.name)

        def visit_Lambda(self, current: ast.Lambda) -> None:
            return

        def visit_Import(self, current: ast.Import) -> None:
            bindings.update(alias.asname or alias.name.split(".")[0] for alias in current.names)

        visit_ImportFrom = visit_Import

        def visit_ExceptHandler(self, current: ast.ExceptHandler) -> None:
            if current.name:
                bindings.add(current.name)
            self.generic_visit(current)

        def visit_ListComp(self, current: ast.ListComp) -> None:
            for child in ast.walk(current):
                if isinstance(child, ast.NamedExpr):
                    self.visit(child.target)

        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

    visitor = Bindings()
    for statement in node.body:
        visitor.visit(statement)
    return bindings


def _module_writes(tree: ast.Module) -> set[str]:
    """Identify module rebinding without descending into definitions."""
    writes: set[str] = set()

    class Writes(ast.NodeVisitor):
        def visit_Name(self, current: ast.Name) -> None:
            if isinstance(current.ctx, ast.Store):
                writes.add(current.id)

        def visit_FunctionDef(self, current: ast.FunctionDef) -> None:
            return

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

        def visit_ListComp(self, current: ast.ListComp) -> None:
            for child in ast.walk(current):
                if isinstance(child, ast.NamedExpr):
                    self.visit(child.target)

        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

    Writes().visit(tree)
    return writes


class _SafeNames:
    """Apply existing rules and optional read-only HMAC discoveries."""

    def __init__(self, ledger_path: Path | None) -> None:
        self.rules = PrivacyFilter(local_llm=None)
        self.key = b""
        self.entries: dict[str, tuple[int, str]] = {}
        if ledger_path is not None:
            directory = ledger_path if ledger_path.name == "privacy" else ledger_path / "privacy"
            self.key = (directory / "key").read_bytes()
            index = directory / "fingerprints.json"
            if index.exists() and index.stat().st_size > MAX_INDEX_BYTES:
                raise ValueError("审计隐私指纹无效")
            raw = index.read_bytes() if index.exists() else b"{}"
            if len(self.key) != 32:
                raise ValueError("审计隐私指纹无效")
            try:
                self.entries, _ = parse_fingerprint_index(raw)
            except ValueError:
                raise ValueError("审计隐私指纹无效") from None

    def observe(self, text: str) -> None:
        # Populate rule matches in memory only, including assignment context.
        self.rules.sanitize_text(text)

    def text(self, text: str) -> str:
        result = self.rules.sanitize_text(text)
        # Rule discoveries from assignment context must also redact bare names
        # encountered later. The mapping stays in this read-only process.
        for value, placeholder in sorted(self.rules._mapping._forward.items(), key=lambda item: len(item[0]), reverse=True):
            result = result.replace(value, placeholder)
        for length in sorted({value[0] for value in self.entries.values()}, reverse=True):
            matches = []
            for index in range(len(result) - length + 1):
                digest = hmac.new(self.key, ("value:" + result[index:index + length]).encode(), hashlib.sha256).hexdigest()
                if digest in self.entries:
                    matches.append((index, index + length))
            for start, end in reversed(matches):
                result = result[:start] + "<redacted>" + result[end:]
        return result

    def tree(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            return {self.text(str(key)): self.tree(item) for key, item in value.items()}
        return value

    def sanitize_text(self, text: str) -> str:
        """Adapter for the shared URL safety contract; this does not call a model."""
        return self.text(text)


def _safe_child(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or ":" in relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("审计路径越界")
    result = root / relative
    if not result.resolve().is_relative_to(root.resolve()) or result.is_symlink():
        raise ValueError("审计路径越界")
    return result


def _snapshot_files(root: Path, privacy: _SafeNames) -> tuple[str, dict, dict[str, str]]:
    metadata = json.loads((root / ".brain-snapshot.json").read_text(encoding="utf-8"))
    commit = metadata["commit_sha"]
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise ValueError("审计需要完整源码 SHA")
    sources = {}
    for relative, info in sorted(metadata["files"].items()):
        if not info.get("available"):
            continue
        content = _safe_child(root, relative).read_bytes()
        algorithm = hashlib.sha256 if len(info["blob_sha"]) == 64 else hashlib.sha1
        blob = algorithm(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if blob != info["blob_sha"]:
            raise ValueError("快照 blob 校验失败")
        text = content.decode("utf-8-sig")
        privacy.observe(text)
        sources[relative] = text
    return commit, metadata["files"], sources


def _oracle(root: Path, privacy: _SafeNames, project_url: str | None, source_locator=None, oracle_key: bytes | None = None) -> dict:
    commit, files, sources = _snapshot_files(root, privacy)
    if source_locator is not None:
        from src.core.project_brain.public_projection import safe_public_url
        project_url = safe_public_url(project_url, privacy)
    items: list[dict] = []
    unknowns: Counter = Counter()
    frameworks: Counter = Counter()
    modules = {path: path[:-3].replace("/", ".").removesuffix(".__init__") for path in sources if path.endswith(".py")}
    trees = {}
    imports: dict[str, dict[str, str]] = {}
    module_writes: dict[str, set[str]] = {}
    definitions: dict[tuple[str, str], list[tuple[ast.AST, set[str]]]] = defaultdict(list)
    routers: dict[tuple[str, str], str | None] = {}
    applications: set[tuple[str, str]] = set()
    mounts: list[tuple[tuple[str, str], tuple[str, str], str | None]] = []
    declarations = []

    def add(category: str, name: str, path: str, node: ast.AST | int, attributes: dict | None = None) -> None:
        start = node if isinstance(node, int) else node.lineno
        end = start if isinstance(node, int) else node.end_lineno or start
        raw = json.dumps([category, name, path, start, end], ensure_ascii=False)
        source = {"commit_sha": commit, "path": path, "blob_sha": files[path]["blob_sha"], "line_start": start, "line_end": end}
        if project_url:
            source["url"] = f"{project_url.rstrip('/')}/-/blob/{commit}/{quote(path, safe='/')}#L{start}"
        identity = (hmac.new(oracle_key, b"audit-oracle-v2:" + raw.encode(), hashlib.sha256).hexdigest()[:24]
                    if oracle_key is not None else hashlib.sha256(raw.encode()).hexdigest()[:24])
        items.append({"id": "oracle:" + identity, "category": category,
                      "name": name, "source": source, "attributes": attributes or {}})

    def expand(path: str, expression: str) -> str:
        first, _, remainder = expression.partition(".")
        return imports[path].get(first, first) + ("." + remainder if remainder else "")

    def module_path(module: str) -> str | None:
        matches = [path for path, name in modules.items() if name == module]
        return matches[0] if len(matches) == 1 else None

    def router_key(path: str, expression: str) -> tuple[str, str] | None:
        expression = expand(path, expression)
        if "." in expression:
            module, _, name = expression.rpartition(".")
            target = module_path(module)
            return (target, name) if target else None
        return path, expression

    for path in modules:
        try:
            tree = ast.parse(sources[path])
        except SyntaxError:
            unknowns["python_parse_error"] += 1
            continue
        trees[path] = tree
        aliases: dict[str, str] = {}
        package = modules[path].split(".") if path.endswith("/__init__.py") else modules[path].split(".")[:-1]
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(statement, ast.ImportFrom):
                parent = package[:len(package) - statement.level + 1] if statement.level else []
                full = ".".join(part for part in [*parent, statement.module or ""] if part)
                for alias in statement.names:
                    if alias.name != "*":
                        aliases[alias.asname or alias.name] = full + "." + alias.name
            elif isinstance(statement, _DEFINITIONS):
                aliases.pop(statement.name, None)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                for target in statement.targets if isinstance(statement, ast.Assign) else [statement.target]:
                    if isinstance(target, ast.Name):
                        aliases.pop(target.id, None)
        imports[path] = aliases
        module_writes[path] = _module_writes(tree)
        for full in aliases.values():
            if full.split(".")[0] in _FRAMEWORKS:
                frameworks[full.split(".")[0]] += 1
        add("module", path, path, 1, {"fact_kind": "file"})

        def visit(node: ast.AST, scope: str = "", inherited: set[str] | None = None) -> None:
            closure = inherited or set()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _DEFINITIONS):
                    name = scope + "." + child.name if scope else child.name
                    own = _local_bindings(child) if not isinstance(child, ast.ClassDef) else set()
                    definitions[(path, name)].append((child, closure | own))
                    declarations.append((path, name, child, closure | own))
                    add("declaration", name, path, child, {"fact_kind": "symbol"})
                    if child.name.startswith("test_") or child.name.endswith("Test"):
                        add("test", name, path, child, {"fact_kind": "test"})
                    visit(child, name, closure if isinstance(child, ast.ClassDef) else closure | own)
                else:
                    visit(child, scope, closure)

        visit(tree)
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and isinstance(statement.value, ast.Call):
                call = statement.value
                constructor = expand(path, _name(call.func))
                if constructor in {"fastapi.FastAPI", "fastapi.APIRouter"}:
                    prefix_node = next((kw.value for kw in call.keywords if kw.arg == "prefix"), None)
                    prefix = _literal(prefix_node) if prefix_node else ""
                    prefix = prefix if isinstance(prefix, str) and (not prefix or prefix.startswith("/") and not prefix.endswith("/")) else None
                    for target in statement.targets if isinstance(statement, ast.Assign) else [statement.target]:
                        if isinstance(target, ast.Name):
                            key = path, target.id
                            routers[key] = prefix if key not in routers else None
                            if constructor == "fastapi.FastAPI":
                                applications.add(key)
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "include_router" and call.args:
                    parent = router_key(path, _name(call.func.value))
                    child = router_key(path, _name(call.args[0]))
                    prefix_node = next((kw.value for kw in call.keywords if kw.arg == "prefix"), None)
                    prefix = _literal(prefix_node) if prefix_node else ""
                    if parent and child:
                        mounts.append((parent, child, prefix if isinstance(prefix, str) else None))
        def startup(body: list[ast.stmt], bound_aliases: dict[str, str]) -> None:
            for statement in body:
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        bound_aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                    continue
                if isinstance(statement, ast.ImportFrom):
                    for alias in statement.names:
                        if alias.name != "*":
                            bound_aliases[alias.asname or alias.name] = f"{statement.module or ''}.{alias.name}" if not statement.level else ""
                    continue
                if (isinstance(statement, ast.If) and isinstance(statement.test, ast.Compare)
                        and _name(statement.test.left) == "__name__" and len(statement.test.ops) == 1
                        and isinstance(statement.test.ops[0], ast.Eq) and len(statement.test.comparators) == 1
                        and _literal(statement.test.comparators[0]) == "__main__"):
                    add("entrypoint", "Python 启动入口", path, statement.lineno, {"fact_kind": "interface", "entry_type": "main_guard"})
                    startup(statement.body, dict(bound_aliases))
                elif isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)) and isinstance(statement.value, ast.Call):
                    call = statement.value
                    first, _, suffix = _name(call.func).partition(".")
                    full = bound_aliases.get(first, "") + ("." + suffix if suffix else "")
                    if full == "uvicorn.run":
                        application = call.args[0] if call.args else next((keyword.value for keyword in call.keywords if keyword.arg == "app"), None)
                        literal = _literal(application)
                        reference = _name(application)
                        if isinstance(literal, str) and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", literal):
                            add("entrypoint", "uvicorn.run", path, call, {"fact_kind": "entrypoint", "entry_type": "asgi", "application": literal, "application_kind": "import_string"})
                        elif reference:
                            add("entrypoint", "uvicorn.run", path, call, {"fact_kind": "entrypoint", "entry_type": "asgi", "application": reference, "application_kind": "reference"})
                        else:
                            unknowns["dynamic_asgi_application"] += 1
                written = _module_writes(ast.Module(body=[statement], type_ignores=[]))
                if isinstance(statement, _DEFINITIONS):
                    written.add(statement.name)
                for child in ast.walk(statement):
                    if isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del)):
                        written.add(_name(child).partition(".")[0])
                    elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Del):
                        written.add(child.id)
                    elif isinstance(child, (ast.Import, ast.ImportFrom)):
                        written.update(alias.asname or alias.name.split(".")[0] for alias in child.names)
                for binding in written:
                    bound_aliases.pop(binding, None)

        startup(tree.body, {})
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                expression = expand(path, _name(node.func))
                if expression in {"os.getenv", "os.environ.get"} and node.args:
                    key = _literal(node.args[0])
                    if isinstance(key, str) and _IDENTIFIER.fullmatch(key):
                        add("configuration", key, path, node, {"fact_kind": "config", "usage": "environment"})

    def prefixes(key: tuple[str, str], visited: set[tuple[str, str]]) -> list[str]:
        own = routers.get(key)
        if key in visited or own is None:
            return []
        if key in applications:
            return [own]
        return [base.rstrip("/") + "/" + extra.strip("/") + "/" + own.strip("/")
                for parent, child, extra in mounts if child == key and extra is not None
                for base in prefixes(parent, visited | {key})]

    for path, name, node, bound in declarations:
        if isinstance(node, ast.ClassDef):
            bases = {expand(path, _name(base)) for base in node.bases if _name(base).partition(".")[0] not in bound | module_writes[path]}
            if bases & {"pydantic.BaseModel", "pydantic.main.BaseModel"}:
                add("data_model", name, path, node, {"fact_kind": "symbol", "model_type": "Pydantic"})
            if bases & {"pydantic.BaseSettings", "pydantic_settings.BaseSettings"}:
                for field in node.body:
                    if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                        add("configuration", field.target.id, path, field, {"fact_kind": "config", "usage": "settings"})
            for field in node.body:
                if isinstance(field, (ast.Assign, ast.AnnAssign)):
                    targets = field.targets if isinstance(field, ast.Assign) else [field.target]
                    if any(isinstance(target, ast.Name) and target.id == "__tablename__" for target in targets):
                        table = _literal(field.value)
                        if isinstance(table, str) and _IDENTIFIER.fullmatch(table):
                            add("data_table", table, path, field, {"fact_kind": "table", "model": name})
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute) or decorator.func.attr not in _METHODS:
                continue
            key = router_key(path, _name(decorator.func.value))
            if key not in routers or "." in name:
                continue
            route_node = decorator.args[0] if decorator.args else next((kw.value for kw in decorator.keywords if kw.arg == "path"), None)
            route = _literal(route_node)
            method_node = next((kw.value for kw in decorator.keywords if kw.arg == "methods"), None)
            methods = (_literal(method_node) if method_node else ["GET"]) if decorator.func.attr == "api_route" else [decorator.func.attr.upper()]
            if not isinstance(route, str) or route and not route.startswith("/") or not isinstance(methods, list) or not all(isinstance(method, str) for method in methods):
                unknowns["dynamic_route"] += 1
                continue
            bases = prefixes(key, set())
            if not bases:
                unknowns["unmounted_or_dynamic_router"] += 1
                continue
            for prefix in bases:
                full = "/" + "/".join(part for part in (prefix.strip("/"), route.strip("/")) if part)
                full = re.sub(r"/{2,}", "/", full)
                if route.endswith("/") and full != "/":
                    full += "/"
                for method in methods:
                    add("interface", method.upper() + " " + full, path, decorator, {"fact_kind": "interface", "handler": name})

        def walk_body(current: ast.AST, hidden: set[str] | None = None) -> None:
            scoped = hidden or set()
            if isinstance(current, (*_DEFINITIONS, ast.Lambda)):
                return
            if isinstance(current, _COMPREHENSIONS):
                inner = set(scoped)
                for generator in current.generators:
                    walk_body(generator.iter, inner)
                    inner.update(child.id for child in ast.walk(generator.target) if isinstance(child, ast.Name))
                    for condition in generator.ifs:
                        walk_body(condition, inner)
                for field in ("elt", "key", "value"):
                    value = getattr(current, field, None)
                    if value is not None:
                        walk_body(value, inner)
                return
            if isinstance(current, ast.Call):
                expression = _name(current.func)
                first = expression.partition(".")[0]
                if expression and first not in bound | scoped | module_writes[path]:
                    full = expand(path, expression)
                    target_module, _, target_name = full.rpartition(".")
                    target_path = module_path(target_module) if full != expression else path if "." not in expression else None
                    targets = definitions.get((target_path, target_name if full != expression else expression), [])
                    if target_path and (target_name if full != expression else expression) not in module_writes[target_path] and len(targets) == 1:
                        add("direct_call", name + " -> " + (target_name if full != expression else expression), path, current,
                            {"owner": name, "target": target_name if full != expression else expression, "target_path": target_path, "scope": "direct_static_declaration"})
                adapted_sql = first not in bound | scoped | module_writes[path] and expand(path, expression) == "sqlalchemy.text"
                sql_call = expression.endswith(".execute") or adapted_sql
                if current.args and sql_call and isinstance(current.args[0], ast.JoinedStr):
                    unknowns["dynamic_sql"] += 1
                if current.args and isinstance(current.args[0], ast.Constant) and isinstance(current.args[0].value, str) and sql_call:
                    sql = current.args[0].value
                    cleaned = re.sub(r"/\*.*?\*/|--[^\n]*|'(?:''|[^'])*'", " ", sql, flags=re.S)
                    if re.search(r"\b(?:CREATE|ALTER|DROP|TRUNCATE)\b", cleaned, re.I):
                        unknowns["unsupported_sql_ddl"] += 1
                    elif not adapted_sql:
                        unknowns["unsupported_sql_driver"] += 1
                    elif re.search(r"\bWITH\b|\$\{|\b(?:FROM|JOIN|INTO|UPDATE)\s*[\"`\[]", cleaned, re.I):
                        unknowns["dynamic_or_dialect_sql"] += 1
                    else:
                        writes = set(re.findall(r"\b(?:INSERT\s+INTO|DELETE\s+FROM)\s+([\w.]+)", cleaned, re.I))
                        writes.update(re.findall(r"\bUPDATE\s+([\w.]+)\s+SET\b", cleaned, re.I))
                        reads = set(re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", cleaned, re.I)) - writes
                        for operation, tables in (("WRITES_TABLE", writes), ("READS_TABLE", reads)):
                            for table in sorted(tables):
                                add("table_access", table, path, current, {"owner": name, "operation": operation})
            for child in ast.iter_child_nodes(current):
                walk_body(child, scoped)

        for statement in node.body:
            walk_body(statement)

    for path, text in sources.items():
        if PurePosixPath(path).name in {"Dockerfile", "Procfile", "Makefile"}:
            for number, line in enumerate(source_lines(text), 1):
                match = re.match(r"\s*(CMD|ENTRYPOINT)\b", line) if PurePosixPath(path).name == "Dockerfile" else re.match(r"([\w.-]+)\s*:", line)
                if match:
                    add("entrypoint", match.group(1), path, number, {"fact_kind": "entrypoint", "entry_type": "deployment"})
        if PurePosixPath(path).name == ".env.example" or path.endswith((".properties", ".ini", ".cfg")):
            for number, line in enumerate(source_lines(text), 1):
                match = re.match(r"\s*([A-Za-z_][\w.-]*)\s*=", line)
                if match:
                    add("configuration", match.group(1), path, number, {"fact_kind": "config", "usage": "declaration"})

    safe_items = []
    for item in sorted(items, key=lambda item: (item["category"], item["source"]["path"], item["source"]["line_start"], item["name"])):
        if source_locator is None:
            safe_item = privacy.tree(item)
        else:
            original_path = item["source"]["path"]
            display_path = privacy.text(original_path)
            source = {**item["source"], "path": source_locator(original_path), "display_path": display_path,
                      "path_redacted": display_path != original_path}
            source.pop("url", None)
            if project_url and display_path == original_path:
                url = f"{project_url.rstrip('/')}/-/blob/{commit}/{quote(original_path, safe='/')}#L{source['line_start']}"
                if privacy.text(project_url) == project_url and privacy.text(url) == url:
                    source["url"] = url
            attributes = {}
            for key, value in item["attributes"].items():
                if key in {"path", "target_path"}:
                    attributes[key] = source_locator(value)
                elif key in {"fact_kind", "entry_type", "usage", "model_type", "application_kind", "scope", "operation", "independent_only"}:
                    # These enums are emitted by the independent AST visitor,
                    # not read from arbitrary source dictionary keys.
                    attributes[key] = value
                else:
                    attributes[key] = privacy.tree(value)
            safe_item = {"id": item["id"], "category": item["category"], "name": privacy.text(item["name"]),
                         "source": source, "attributes": attributes}
        required_names = [safe_item["name"], safe_item["source"]["path"], *[safe_item["attributes"][key] for key in ("owner", "target", "handler", "model") if key in safe_item["attributes"]]]
        if any("<<" in name or "<redacted>" in name for name in required_names):
            unknowns["privacy_redacted_oracle_items"] += 1
            continue
        safe_items.append(safe_item)
    return {"source_commit": commit, "coverage": {"available_files": len(sources), "python_files": len(trees),
        "unsupported_non_python_files": len(sources) - len(modules), "static_only": True},
        "framework_import_bindings": dict(frameworks), "unknown_counts": dict(unknowns),
        "items": safe_items,
        "_files": ({source_locator(path): info for path, info in files.items()} if source_locator else files),
        "_path_traits": ({source_locator(path): {"python": path in trees,
            "config_file": PurePosixPath(path).name == ".env.example" or path.endswith((".properties", ".ini", ".cfg"))}
            for path in files} if source_locator else None)}


def _formal_artifacts(root: Path, commit: str) -> tuple[dict, list[dict], str, dict]:
    manifest = json.loads(_safe_child(root, "repository/docs/project-brain/manifest.json").read_text(encoding="utf-8"))
    checksums = manifest.get("service_files", {})
    checked: dict[str, bytes] = {}
    if not isinstance(checksums, dict):
        raise ValueError("服务侧产物校验清单不完整")
    public = manifest.get("schema_version") == "2.0"
    if public and not checksums:
        raise ValueError("服务侧产物校验清单不完整")
    if checksums:
        if set(checksums) != _SERVICE_FILES:
            raise ValueError("服务侧产物校验清单不完整")
        for name, digest in checksums.items():
            content = _safe_child(root, name).read_bytes()
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("服务侧产物哈希校验失败")
            checked[name] = content
    bundle = json.loads(checked["facts.json"] if checksums else _safe_child(root, "facts.json").read_bytes())
    cards = json.loads(checked["cards.json"] if checksums else _safe_child(root, "cards.json").read_bytes())
    if public:
        from src.core.project_brain.public_projection import verify_projection
        from src.core.project_brain.public_schema import PublicBundle, PublicKnowledgeCard, parse_manifest
        graph = PublicBundle.model_validate(bundle)
        typed_cards = [PublicKnowledgeCard.model_validate(card) for card in cards]
        typed_manifest = parse_manifest(manifest)
        verify_projection(graph, typed_cards, typed_manifest)
        bundle = _public_matching_bundle(graph)
    if manifest.get("source_commit") != commit or bundle.get("source_commit") != commit or any(card.get("source_commit", commit) != commit for card in cards):
        raise ValueError("源码与正式知识版本不一致")
    texts = []
    for path, digest in manifest["files"].items():
        if path != "PROJECT_BRAIN.md" and not path.startswith("docs/project-brain/"):
            raise ValueError("产物发布路径越界")
        content = _safe_child(root / "repository", path).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("产物哈希校验失败")
        if path.endswith(".md"):
            texts.append(content.decode("utf-8"))
    return bundle, cards, "\n".join(texts), {"repository_file_hashes_valid": True, "files_checked": len(manifest["files"]),
        "source_commit_matches": True, "server_json_covered_by_repository_hashes": False,
        "service_file_hashes_valid": bool(checksums), "service_files_checked": len(checked),
        "manifest_authentication": "not_independently_verified", "public_schema_version": "2.0" if public else "1.0"}


def _public_matching_bundle(bundle) -> dict:
    """Read the needed typed attributes without collapsing unknown members."""
    facts = []
    for fact in bundle.facts:
        attributes = {}
        if fact.attributes.kind != "object":
            raise ValueError("公开事实属性结构无效")
        for entry in fact.attributes.entries:
            if entry.key not in {"path", "usage", "interface_type"}:
                continue
            if entry.key in attributes or entry.value.kind != "string":
                raise ValueError("公开事实定位属性不唯一")
            attributes[entry.key] = entry.value.text
        facts.append({**fact.model_dump(exclude={"attributes"}), "attributes": attributes})
    return {"source_commit": bundle.source_commit, "schema_version": "2.0", "facts": facts,
            "relations": [relation.model_dump() for relation in bundle.relations],
            "source_refs": [ref.model_dump() for ref in bundle.source_refs]}


def _matching(oracle: dict, bundle: dict) -> tuple[dict, dict[str, dict]]:
    facts = bundle.get("facts", [])
    by_id = {fact["id"]: fact for fact in facts}
    relations = bundle.get("relations", [])
    outcomes = {}
    categories = {}
    refs = {ref["id"]: ref for ref in bundle.get("source_refs", [])}

    def owner_fact(path: str, name: str) -> list[dict]:
        return [fact for fact in facts if fact["kind"] == "symbol" and fact["name"] == name and fact.get("attributes", {}).get("path") == path]

    def same_site(fact: dict, item: dict) -> bool:
        # If refs exist, names alone cannot substitute for source evidence.
        if not refs:
            return True
        return any(rid in refs and refs[rid]["path"] == item["source"]["path"] and refs[rid]["line_start"] <= item["source"]["line_start"] <= refs[rid]["line_end"] for rid in fact.get("source_ref_ids", []))

    for item in oracle["items"]:
        path, name = item["source"]["path"], item["name"]
        attributes = item["attributes"]
        candidate = False
        if item["category"] == "direct_call":
            owners = {fact["id"] for fact in owner_fact(path, attributes["owner"])}
            targets = {fact["id"] for fact in owner_fact(attributes["target_path"], attributes["target"])}
            matched = any(relation["type"] == "CALLS" and relation["from_id"] in owners and relation.get("to_id") in targets and relation["resolution"] == "resolved" for relation in relations)
            candidate = any(relation["type"] == "CALLS" and relation["from_id"] in owners and relation["resolution"] == "candidate" and targets.intersection(relation.get("candidates", [])) for relation in relations)
        elif item["category"] == "table_access":
            owners = {fact["id"] for fact in owner_fact(path, attributes["owner"])}
            matched = any(relation["type"] == attributes["operation"] and relation["from_id"] in owners and relation.get("target_name") == name for relation in relations)
        elif attributes.get("independent_only"):
            matched = False
        else:
            kind = attributes.get("fact_kind")
            matches = [fact for fact in facts if fact["kind"] == kind and fact["name"] == name and fact.get("attributes", {}).get("path") == path and same_site(fact, item)]
            matched = bool(matches)
            if matched and item["category"] == "interface":
                handlers = {fact["id"] for fact in owner_fact(path, attributes["handler"])}
                matched = any(relation["type"] == "HANDLED_BY" and relation["from_id"] in {fact["id"] for fact in matches} and relation.get("to_id") in handlers and relation["resolution"] == "resolved" for relation in relations)
        outcomes[item["id"]] = {"matched": matched, "candidate_only": candidate and not matched}

    for category in sorted({item["category"] for item in oracle["items"]}):
        items = [item for item in oracle["items"] if item["category"] == category]
        hits = sum(outcomes[item["id"]]["matched"] for item in items)
        candidates = sum(outcomes[item["id"]]["candidate_only"] for item in items)
        categories[category] = {"oracle_denominator": len(items), "matched": hits, "candidate_only": candidates,
            "recall": hits / len(items) if items else None,
            "precision": None, "precision_scope": "需要逐一反查所有生成条目；本项只计算独立源码样本召回",
            "missing_examples": [{"oracle_id": item["id"], "name": item["name"], "source": item["source"]} for item in items if not outcomes[item["id"]]["matched"]][:8]}

    # Compute precision only where the independent oracle exhaustively observes
    # the supported statement form. Other categories keep an explicit null.
    simple_kinds = {"declaration": "symbol", "test": "test", "module": "file", "data_table": "table", "interface": "interface", "configuration": "config"}
    python_paths = ({path for path, traits in oracle["_path_traits"].items() if traits["python"]}
                    if oracle.get("_path_traits") is not None else {item["source"]["path"] for item in oracle["items"] if item["category"] == "module"})
    for category, kind in simple_kinds.items():
        expected = {(item["source"]["path"], item["name"]) for item in oracle["items"] if item["category"] == category}
        generated = []
        for fact in facts:
            attributes = fact.get("attributes", {})
            path = attributes.get("path", "")
            if fact["kind"] != kind:
                continue
            if "<<" in fact["name"] or "<redacted>" in fact["name"] or "<<" in path or "<redacted>" in path:
                continue
            if category in {"declaration", "test", "module", "data_table", "interface"} and path not in python_paths:
                continue
            if category == "interface" and attributes.get("interface_type") != "http":
                continue
            traits = oracle.get("_path_traits")
            config_scope = ((traits.get(path, {}).get("python") and attributes.get("usage") in {"environment", "settings"}
                             or traits.get(path, {}).get("config_file")) if traits is not None else
                            (path.endswith(".py") and attributes.get("usage") in {"environment", "settings"}
                             or PurePosixPath(path).name == ".env.example" or path.endswith((".properties", ".ini", ".cfg"))))
            if category == "configuration" and not config_scope:
                continue
            generated.append((path, fact["name"]))
        if category not in categories:
            categories[category] = {"oracle_denominator": 0, "matched": 0, "candidate_only": 0, "recall": None, "missing_examples": []}
        supported = category != "interface" or not any(key in oracle["unknown_counts"] for key in ("dynamic_route", "unmounted_or_dynamic_router"))
        metric = categories[category]
        metric["generated_denominator"] = len(generated)
        metric["precision"] = sum(key in expected for key in generated) / len(generated) if generated and supported else None
        metric["precision_scope"] = "穷举支持的 Python 声明/测试/文件、字面量表声明、静态 FastAPI 路由或明确配置形式；动态路由存在时不评分该类精确率。"
        metric["unexpected_examples"] = [{"path": path, "name": name} for path, name in generated if (path, name) not in expected][:8] if supported else []

    invalid_refs = []
    for ref in refs.values():
        info = oracle["_files"].get(ref.get("path"))
        if not info or ref.get("commit_sha") != oracle["source_commit"] or ref.get("blob_sha") != info["blob_sha"] or not isinstance(ref.get("line_start"), int) or not isinstance(ref.get("line_end"), int) or not 1 <= ref["line_start"] <= ref["line_end"] <= info["lines"]:
            invalid_refs.append({"id": ref.get("id"), "code": "invalid_source_ref"})
    invalid_entities = []
    for fact in facts:
        if any(rid not in refs for rid in fact.get("source_ref_ids", [])):
            invalid_entities.append({"id": fact["id"], "code": "missing_fact_evidence"})
    for relation in relations:
        if relation.get("from_id") not in by_id or relation.get("to_id") and relation["to_id"] not in by_id or any(candidate not in by_id for candidate in relation.get("candidates", [])) or any(rid not in refs for rid in relation.get("source_ref_ids", [])):
            invalid_entities.append({"id": relation["id"], "code": "invalid_relation_reference"})
        if relation.get("resolution") == "resolved" and not relation.get("to_id"):
            invalid_entities.append({"id": relation["id"], "code": "resolved_without_target"})
        if relation.get("resolution") != "resolved" and relation.get("to_id"):
            invalid_entities.append({"id": relation["id"], "code": "candidate_with_definite_target"})
    return {"categories": categories, "source_reference_count": len(refs), "reference_evidence_available": bool(refs), "invalid_source_references": invalid_refs,
            "invalid_entity_references": invalid_entities, "sample_independent_of_extractor": True}, outcomes


def _questions(oracle: dict, outcomes: dict[str, dict], document: str | None, maximum: int) -> list[dict]:
    groups = {category: [item for item in oracle["items"] if item["category"] == category] for category in _CATEGORIES}
    selected = []
    seen = set()
    # Round robin balances categories, paths and statements rather than selecting
    # the first forty functions from a large generated schema file.
    while len(selected) < maximum and any(groups.values()):
        for category in _CATEGORIES:
            if not groups[category] or len(selected) >= maximum:
                continue
            item = groups[category].pop(0)
            key = category, item["source"]["path"], item["name"]
            if key in seen:
                continue
            seen.add(key)
            selected.append(item)
    normalized = re.sub(r"[`*_]", "", document).casefold() if document is not None else None
    questions = []
    prompts = {"interface": "该处理函数注册的静态 HTTP 方法和完整路径是什么？",
        "entrypoint": "该文件通过什么明确入口启动或部署？", "data_table": "模型明确声明映射到哪个表？",
        "table_access": "该函数中的字面量 SQL 明确访问哪个表，操作是读还是写？",
        "data_model": "该类的直接基类声明表明它是哪种数据模型？", "configuration": "该声明或读取使用哪个配置名称？",
        "test": "该文件包含哪个明确命名的测试声明？", "direct_call": "该函数直接调用了哪个可静态确认的项目函数？",
        "module": "该源码文件属于哪个文件路径，能否在知识中定位它？"}
    for index, item in enumerate(selected, 1):
        attributes = item["attributes"]
        terms = [item["name"]]
        if item["category"] == "direct_call":
            terms = [attributes["owner"], attributes["target"]]
        elif item["category"] == "interface":
            terms.append(attributes["handler"])
        present = all(re.sub(r"[`*_]", "", term).casefold() in normalized for term in terms) if normalized is not None else None
        questions.append({"id": f"question-{index:02d}", "category": item["category"], "question": prompts[item["category"]],
            "source": item["source"], "expected_answer": {"name": item["name"], **attributes}, "oracle_id": item["id"],
            "automatic_validation_scope": "静态声明/直接调用及文档词项存在性；不判断模型业务解释或仅凭词项推断回答正确",
            "automatic_result": {"fact_matched": outcomes.get(item["id"], {}).get("matched"),
                "all_answer_terms_present": present, "answer_correctness": "requires_manual_review"}})
    return questions


def run_audit(snapshot_path: Path | str, artifact_path: Path | str | None = None,
              privacy_ledger: Path | str | None = None, max_questions: int = 40,
              project_url: str | None = None, source_identity_key: Path | str | bytes | None = None,
              privacy_history_base: Path | str | None = None) -> dict:
    """Compare independently observed source with optional formal artifacts."""
    if not 1 <= max_questions <= 50:
        raise ValueError("质量问题集数量必须为 1～50")
    privacy = _SafeNames(Path(privacy_ledger) if privacy_ledger else None)
    source_locator = None
    oracle_key = None
    public_schema = False
    history_integrity = None
    if artifact_path is not None:
        manifest = json.loads(_safe_child(Path(artifact_path), "repository/docs/project-brain/manifest.json").read_text(encoding="utf-8"))
        public_schema = manifest.get("schema_version") == "2.0"
        if public_schema:
            identity = manifest.get("projection", {})
            if identity.get("identity_version") != "opaque-v1" or str(identity.get("key_version")) != "1":
                raise ValueError("审计不支持该来源身份版本")
            if source_identity_key is None:
                raise ValueError("公开知识审计需要私有来源身份密钥")
            if privacy_ledger is None:
                raise ValueError("公开知识审计需要完整隐私账本")
            try:
                if isinstance(source_identity_key, bytes):
                    key = source_identity_key
                else:
                    with Path(source_identity_key).open("rb") as stream:
                        key = stream.read(33)
            except OSError:
                raise ValueError("审计来源身份密钥无效") from None
            if len(key) != 32:
                raise ValueError("审计来源身份密钥无效")
            history_integrity = _history_integrity(manifest, key, source_identity_key,
                                                    Path(privacy_ledger), privacy_history_base)
            source_locator = lambda path: _opaque_source_locator(path, key)
            oracle_key = key
            if project_url is None:
                project_url = manifest.get("public_metadata", {}).get("project_url")
    oracle = _oracle(Path(snapshot_path), privacy, project_url, source_locator, oracle_key)
    integrity = None
    matching = None
    outcomes = {}
    document = None
    cards = []
    bundle = {}
    if artifact_path is not None:
        bundle, cards, document, integrity = _formal_artifacts(Path(artifact_path), oracle["source_commit"])
        if public_schema:
            # A caller may audit against a newer private discovery index than
            # the sealed artifact. Never export old free text into examples.
            for fact in bundle["facts"]:
                fact["name"] = privacy.text(fact["name"])
            for relation in bundle["relations"]:
                relation["target_name"] = privacy.text(relation["target_name"])
        matching, outcomes = _matching(oracle, bundle)
        if public_schema:
            for collection in ("invalid_source_references", "invalid_entity_references"):
                for issue in matching[collection]:
                    value = issue.get("id")
                    if type(value) is str and not re.fullmatch(r"[a-z_]+:(?:[a-f0-9]{24}|[a-f0-9]{64})", value):
                        issue["id"] = privacy.text(value)
    questions = _questions(oracle, outcomes, document, max_questions)
    refs = {ref["id"] for ref in bundle.get("source_refs", [])}
    relations = {relation["id"]: relation for relation in bundle.get("relations", [])}
    overview_claims = [claim for card in cards for claim in card.get("claims", [])]
    detail_claims = [claim for card in cards for claim in card.get("detail_claims", [])]
    claims = overview_claims + detail_claims
    invalid_claims = sum(not claim.get("evidence_ids") or any(rid not in refs for rid in claim.get("evidence_ids", [])) or any(rid not in relations for rid in claim.get("relation_ids", [])) for claim in claims)
    uncertain_claims = sum(any(relations.get(rid, {}).get("resolution") != "resolved" for rid in claim.get("relation_ids", [])) for claim in claims)
    oracle.pop("_files")
    oracle.pop("_path_traits")
    report = {"audit_version": "2.0" if public_schema else "1.0", "oracle": oracle, "artifact_integrity": integrity, "fact_matching": matching,
        "questions": questions, "document_checks": {"question_denominator": len(questions),
            "questions_with_all_terms_present": sum(question["automatic_result"]["all_answer_terms_present"] is True for question in questions) if document is not None else None,
            "metric_meaning": "文档词项可定位率，不是知识回答正确率"},
        "semantic_support": {"claim_count": len(claims), "overview_claim_count": len(overview_claims),
            "detail_claim_count": len(detail_claims), "claims_with_invalid_references": invalid_claims,
            "synthesis_relationship_views": {
                view: sum((claim.get("source_scope") or {}).get("kind") == "summary_synthesis"
                          and (claim.get("source_scope") or {}).get("relationship_view") == value for claim in claims)
                for view, value in (("symbol_graph", "symbol_graph"), ("resolution_counts", "resolution_counts"), ("legacy_unspecified", None))},
            "claims_referencing_candidate_or_unknown_relations": uncertain_claims,
            "answer_accuracy": None, "business_semantics_status": "requires_manual_review",
            "explanation": "证据存在性不证明推断得到证据支持；候选关系可以被明确标为未知，需逐条人工核对措辞。"},
        "limitations": ["静态源码 oracle 不覆盖运行时动态注册、反射、动态 SQL 或所有框架。",
            "只对明确可核实的源码项报告分母；未知项单独计数。",
            "文档出现答案词项不证明可据此回答问题；业务职责和推断正确性没有自动评分。",
            "本审计不运行项目代码、不调用模型，也不验证增量或 GitLab 发布。"]}
    if integrity is not None and not integrity["service_file_hashes_valid"]:
        report["limitations"].append("旧清单缺少服务侧文件校验和；事实图、卡片与索引没有进行 SHA256 完整性验证。")
    if public_schema:
        report["privacy_history"] = history_integrity
        if history_integrity is None:
            report["limitations"].append("旧清单没有已绑定的历史隐私基线；未认证跨构建发现继承。")
    # Schema 2 oracle paths/target_paths were independently converted to opaque
    # identities before display filtering. Formal names are already public and
    # matched without copying their arbitrary attribute trees into this report.
    # Re-filtering every machine key/locator here would corrupt that contract.
    return report if public_schema else privacy.tree(report)


def _history_integrity(manifest: dict, key: bytes, identity_path, ledger_path: Path, history_base) -> dict | None:
    """Authenticate the frozen policy baseline without writing any private file."""
    from src.core.project_brain.privacy_history import PrivacyHistory, _checked_ledger

    expected = manifest.get("coverage", {}).get("privacy_history_digest")
    if expected is None and manifest.get("generator") != "project-brain/2.0.1":
        return None
    try:
        if type(expected) is not str or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
            raise ValueError()
        if history_base is None:
            if not isinstance(identity_path, (str, Path)):
                raise ValueError()
            identity = Path(identity_path)
            if (identity.name != "identity.key" or identity.parent.name != "1"
                    or identity.parent.parent.name != str(manifest["project_id"])
                    or identity.parent.parent.parent.name != "source-identities"):
                raise ValueError()
            history_base = identity.parents[3]
        history = PrivacyHistory(Path(history_base), manifest["project_id"], manifest["build_id"],
                                 manifest["source_commit"], key)
        if history.digest != expected:
            raise ValueError()
        directory = ledger_path if ledger_path.name == "privacy" else ledger_path / "privacy"
        if directory.is_symlink() or (directory / "key").is_symlink() or (directory / "fingerprints.json").is_symlink():
            raise ValueError()
        with (directory / "key").open("rb") as stream:
            ledger_key = stream.read(33)
        with (directory / "fingerprints.json").open("rb") as stream:
            raw = stream.read(MAX_INDEX_BYTES + 1)
        if len(raw) > MAX_INDEX_BYTES:
            raise ValueError()
        ledger = _checked_ledger(ledger_key, raw, manifest["build_id"])
        parents = {parent.snapshot_id: parent for parent in history.parents}
        inherited = 0
        rules = 0
        for digest, finding in ledger.origins["findings"].items():
            for record in finding.get("policy_records", {}).values():
                if record["kind"] == "deterministic_rule":
                    rules += 1
                    continue
                parent = parents.get(record["source_snapshot"])
                if (parent is None or parent.metadata["source_build_id"] != record["source_build_id"]
                        or parent.metadata["source_commit"] != record["source_commit"]
                        or parent.ledger.entries.get(record["source_finding"]) != (record["length"], record["type"])
                        or record["length"] != ledger.entries[digest][0]):
                    raise ValueError()
                inherited += 1
        return {"baseline_authenticated": True, "parent_count": len(parents),
                "inherited_occurrence_records": inherited, "rule_occurrence_records": rules}
    except Exception:
        # Private paths, arbitrary metadata and HMAC values stay out of reports.
        raise ValueError("审计历史隐私基线无效") from None


def _opaque_source_locator(path: str, key: bytes) -> str:
    """Recompute the documented identity contract independently of extraction."""
    if path == ".":
        return "sources"
    if not path or "\\" in path or ":" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("审计来源路径无效")
    parts = path.split("/")
    return "sources/" + "/".join("s" + hmac.new(key, json.dumps(["path", "/".join(parts[:index])],
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        for index in range(1, len(parts) + 1))


def main() -> None:
    """Write filtered audit metrics and reproducible questions as JSON."""
    parser = argparse.ArgumentParser(description="固定源码版本的独立 Project Brain 质量审计")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--privacy-ledger", type=Path)
    parser.add_argument("--source-identity-key", type=Path)
    parser.add_argument("--privacy-history-base", type=Path)
    parser.add_argument("--project-url")
    parser.add_argument("--max-questions", type=int, default=40)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run_audit(arguments.snapshot, arguments.artifact, arguments.privacy_ledger, arguments.max_questions, arguments.project_url,
                       arguments.source_identity_key, arguments.privacy_history_base)
    data = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(data, encoding="utf-8")
        print(json.dumps({"source_commit": report["oracle"]["source_commit"], "oracle_items": len(report["oracle"]["items"]),
            "questions": len(report["questions"]), "formal_artifacts_checked": report["artifact_integrity"] is not None}, ensure_ascii=False))
    else:
        print(data)


if __name__ == "__main__":
    main()
