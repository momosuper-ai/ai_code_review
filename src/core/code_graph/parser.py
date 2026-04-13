"""基于 tree-sitter 的多语言代码解析器，输出 (节点, 边) 供代码图写入。

支持语言：Python / JavaScript / TypeScript / Java / Go
底层依赖：
- ``tree-sitter``
- ``tree-sitter-python``
- ``tree-sitter-javascript``
- ``tree-sitter-typescript``（含 typescript 与 tsx 两种语言）
- ``tree-sitter-java``
- ``tree-sitter-go``

所有原生依赖在 Linux Docker 镜像构建时安装预编译 wheel，运行时零开销。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from functools import lru_cache

from src.utils.logger import logger


# ── 数据结构定义 ────────────────────────────────────────────────────

@dataclass
class ParsedNode:
    """源文件中发现的一个代码实体（函数/类/方法）。"""
    kind: str             # function / class / method
    name: str
    line_start: int
    line_end: int
    signature: str = ""
    calls: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)  # 继承目标


@dataclass
class ParsedFile:
    path: str             # 相对 repo 根目录的路径
    language: str
    content_hash: str
    nodes: list[ParsedNode] = field(default_factory=list)


# ── 语言识别 ────────────────────────────────────────────────────────

_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
}

# 规范化别名：parser 内部使用的 language key
_CANONICAL_LANG = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "tsx": "typescript",        # tsx 文件统一归到 typescript
    "java": "java",
    "go": "go",
}


def detect_language(path: str) -> str | None:
    """根据文件扩展名识别语言，不支持的返回 None。"""
    ext = os.path.splitext(path)[1].lower()
    raw = _LANG_BY_EXT.get(ext)
    return _CANONICAL_LANG.get(raw) if raw else None


def _detect_raw_language(path: str) -> str | None:
    """内部使用：返回 tsx 与 typescript 区分的原始标签。"""
    ext = os.path.splitext(path)[1].lower()
    return _LANG_BY_EXT.get(ext)


def _hash_content(content: bytes) -> str:
    """计算内容 SHA-256 前 16 位，用于跳过未变更文件。"""
    return hashlib.sha256(content).hexdigest()[:16]


# ── tree-sitter parser 缓存 ─────────────────────────────────────────

@lru_cache(maxsize=None)
def _get_parser(raw_language: str):
    """按原始语言标签获取 tree-sitter Parser（带缓存）。

    - ``tsx`` 与 ``typescript`` 分别映射到 tree-sitter-typescript 的两个 language
    """
    from tree_sitter import Language, Parser

    if raw_language == "python":
        import tree_sitter_python as ts_mod
        lang = Language(ts_mod.language())
    elif raw_language == "javascript":
        import tree_sitter_javascript as ts_mod
        lang = Language(ts_mod.language())
    elif raw_language == "typescript":
        import tree_sitter_typescript as ts_mod
        lang = Language(ts_mod.language_typescript())
    elif raw_language == "tsx":
        import tree_sitter_typescript as ts_mod
        lang = Language(ts_mod.language_tsx())
    elif raw_language == "java":
        import tree_sitter_java as ts_mod
        lang = Language(ts_mod.language())
    elif raw_language == "go":
        import tree_sitter_go as ts_mod
        lang = Language(ts_mod.language())
    else:
        raise ValueError(f"不支持的语言: {raw_language}")
    return Parser(lang)


# ── 通用辅助 ────────────────────────────────────────────────────────

def _text(node, source: bytes) -> str:
    """提取 tree-sitter 节点对应的源码文本。"""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _line_range(node) -> tuple[int, int]:
    """tree-sitter 使用 0 基行号，这里转换为 1 基行号的 (start, end)。"""
    return node.start_point[0] + 1, node.end_point[0] + 1


def _iter_children(node):
    """迭代某个节点的所有直接子节点。"""
    for i in range(node.child_count):
        yield node.child(i)


def _walk_collect_calls(
    root,
    source: bytes,
    own_name: str,
    stop_types: set[str],
    call_types: set[str],
    extract_call_name,
) -> list[str]:
    """递归收集函数体中的调用名，遇到嵌套定义时停止下探。"""
    calls: set[str] = set()

    def _visit(n):
        # 跳过嵌套定义的子树——它们的调用归属于嵌套函数，不属于外层
        if n is not root and n.type in stop_types:
            return
        if n.type in call_types:
            name = extract_call_name(n)
            if name and name != own_name:
                calls.add(name)
        for child in _iter_children(n):
            _visit(child)

    _visit(root)
    return sorted(calls)


# ── Python 解析 ─────────────────────────────────────────────────────

_PY_DEF_TYPES = {"function_definition", "class_definition"}
_PY_CALL_TYPES = {"call"}


def _py_call_name(call_node) -> str | None:
    """从 Python ``call`` 节点中提取被调用的符号名。"""
    func = call_node.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        return func.text.decode("utf-8", errors="replace")
    if func.type == "attribute":
        attr = func.child_by_field_name("attribute")
        if attr is not None:
            return attr.text.decode("utf-8", errors="replace")
    return None


def _parse_python_ts(tree, source: bytes) -> list[ParsedNode]:
    """使用 tree-sitter-python 提取定义、调用、继承关系。"""
    nodes: list[ParsedNode] = []

    def _collect_bases(class_node) -> list[str]:
        """从 ``class_definition.superclasses`` (argument_list) 收集基类名。"""
        bases: list[str] = []
        args = class_node.child_by_field_name("superclasses")
        if args is None:
            return bases
        for child in _iter_children(args):
            if child.type == "identifier":
                bases.append(_text(child, source))
            elif child.type == "attribute":
                attr = child.child_by_field_name("attribute")
                if attr is not None:
                    bases.append(_text(attr, source))
        return bases

    def _visit(node, inside_class: str | None):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _text(name_node, source)
            ls, le = _line_range(node)
            nodes.append(ParsedNode(
                kind="class",
                name=name,
                line_start=ls,
                line_end=le,
                signature=f"class {name}",
                bases=_collect_bases(node),
            ))
            # 递归进入类体，标记 inside_class
            body = node.child_by_field_name("body")
            if body is not None:
                for child in _iter_children(body):
                    _visit(child, name)
            return

        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _text(name_node, source)
            ls, le = _line_range(node)
            params_node = node.child_by_field_name("parameters")
            params_text = _text(params_node, source) if params_node else "()"
            kind = "method" if inside_class else "function"
            signature = (
                f"{inside_class}.{name}{params_text}"
                if inside_class else f"def {name}{params_text}"
            )
            body = node.child_by_field_name("body")
            calls = (
                _walk_collect_calls(
                    body, source, name,
                    stop_types=_PY_DEF_TYPES,
                    call_types=_PY_CALL_TYPES,
                    extract_call_name=_py_call_name,
                )
                if body is not None else []
            )
            nodes.append(ParsedNode(
                kind=kind,
                name=name,
                line_start=ls,
                line_end=le,
                signature=signature,
                calls=calls,
            ))
            # 函数体内的嵌套定义仍需遍历，但 inside_class 重置为 None
            if body is not None:
                for child in _iter_children(body):
                    _visit(child, None)
            return

        # 其它节点：继续向下遍历
        for child in _iter_children(node):
            _visit(child, inside_class)

    _visit(tree.root_node, None)
    return nodes


# ── JavaScript / TypeScript 解析 ────────────────────────────────────

_JS_DEF_TYPES = {
    "function_declaration", "class_declaration", "method_definition",
    "function_expression", "arrow_function", "generator_function_declaration",
    "interface_declaration", "enum_declaration",
}
_JS_CALL_TYPES = {"call_expression"}


def _js_call_name(call_node) -> str | None:
    """从 JS/TS ``call_expression`` 节点中提取被调用的符号名。"""
    func = call_node.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        return func.text.decode("utf-8", errors="replace")
    if func.type == "member_expression":
        prop = func.child_by_field_name("property")
        if prop is not None:
            return prop.text.decode("utf-8", errors="replace")
    return None


def _js_class_bases(class_node, source: bytes) -> list[str]:
    """从 ``class_declaration`` 的 heritage clause 中提取父类名。"""
    bases: list[str] = []
    for child in _iter_children(class_node):
        if child.type == "class_heritage":
            for sub in _iter_children(child):
                if sub.type in ("identifier", "type_identifier"):
                    bases.append(_text(sub, source))
                elif sub.type == "member_expression":
                    prop = sub.child_by_field_name("property")
                    if prop is not None:
                        bases.append(_text(prop, source))
    return bases


def _parse_js_like_ts(tree, source: bytes) -> list[ParsedNode]:
    """统一处理 JavaScript / TypeScript / TSX 的解析。"""
    nodes: list[ParsedNode] = []

    def _make_function_node(
        func_node, name: str, kind: str, inside_class: str | None,
    ) -> ParsedNode:
        ls, le = _line_range(func_node)
        params_node = func_node.child_by_field_name("parameters")
        params_text = _text(params_node, source) if params_node else "()"
        signature = (
            f"{inside_class}.{name}{params_text}"
            if inside_class else f"function {name}{params_text}"
        )
        body = func_node.child_by_field_name("body")
        calls = (
            _walk_collect_calls(
                body, source, name,
                stop_types=_JS_DEF_TYPES,
                call_types=_JS_CALL_TYPES,
                extract_call_name=_js_call_name,
            )
            if body is not None else []
        )
        return ParsedNode(
            kind=kind,
            name=name,
            line_start=ls,
            line_end=le,
            signature=signature,
            calls=calls,
        )

    def _visit(node, inside_class: str | None):
        t = node.type

        if t in ("class_declaration", "interface_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                for child in _iter_children(node):
                    _visit(child, inside_class)
                return
            name = _text(name_node, source)
            ls, le = _line_range(node)
            nodes.append(ParsedNode(
                kind="class",
                name=name,
                line_start=ls,
                line_end=le,
                signature=f"class {name}",
                bases=_js_class_bases(node, source),
            ))
            body = node.child_by_field_name("body")
            if body is not None:
                for child in _iter_children(body):
                    _visit(child, name)
            return

        if t == "function_declaration" or t == "generator_function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node, source)
                nodes.append(_make_function_node(node, name, "function", None))
            # 函数体内的嵌套定义继续遍历
            body = node.child_by_field_name("body")
            if body is not None:
                for child in _iter_children(body):
                    _visit(child, None)
            return

        if t == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node, source)
                nodes.append(_make_function_node(node, name, "method", inside_class))
            body = node.child_by_field_name("body")
            if body is not None:
                for child in _iter_children(body):
                    _visit(child, None)
            return

        # 处理 const/let/var name = (arrow|function)
        if t == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if (name_node is not None and value_node is not None
                    and value_node.type in ("arrow_function", "function_expression")):
                name = _text(name_node, source)
                nodes.append(_make_function_node(value_node, name, "function", None))
                body = value_node.child_by_field_name("body")
                if body is not None:
                    for child in _iter_children(body):
                        _visit(child, None)
                return
            # 否则按普通节点继续
            for child in _iter_children(node):
                _visit(child, inside_class)
            return

        for child in _iter_children(node):
            _visit(child, inside_class)

    _visit(tree.root_node, None)
    return nodes


# ── Java 解析 ───────────────────────────────────────────────────────

_JAVA_DEF_TYPES = {
    "class_declaration", "interface_declaration", "enum_declaration",
    "method_declaration", "constructor_declaration",
}
_JAVA_CALL_TYPES = {"method_invocation", "object_creation_expression"}


def _java_call_name(call_node) -> str | None:
    """从 Java ``method_invocation`` / ``object_creation_expression`` 提取调用名。"""
    if call_node.type == "method_invocation":
        name = call_node.child_by_field_name("name")
        if name is not None:
            return name.text.decode("utf-8", errors="replace")
    elif call_node.type == "object_creation_expression":
        type_node = call_node.child_by_field_name("type")
        if type_node is not None:
            return type_node.text.decode("utf-8", errors="replace")
    return None


def _java_class_bases(class_node, source: bytes) -> list[str]:
    """从 ``class_declaration`` 的 superclass / super_interfaces 提取父类和接口。"""
    bases: list[str] = []
    for child in _iter_children(class_node):
        if child.type == "superclass":
            for sub in _iter_children(child):
                if sub.type == "type_identifier":
                    bases.append(_text(sub, source))
        elif child.type in ("super_interfaces", "extends_interfaces"):
            for sub in _iter_children(child):
                if sub.type == "type_list":
                    for t in _iter_children(sub):
                        if t.type == "type_identifier":
                            bases.append(_text(t, source))
    return bases


def _parse_java_ts(tree, source: bytes) -> list[ParsedNode]:
    """使用 tree-sitter-java 提取类、方法和调用关系。"""
    nodes: list[ParsedNode] = []

    def _visit(node, inside_class: str | None):
        t = node.type

        if t in ("class_declaration", "interface_declaration", "enum_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                for child in _iter_children(node):
                    _visit(child, inside_class)
                return
            name = _text(name_node, source)
            ls, le = _line_range(node)
            nodes.append(ParsedNode(
                kind="class",
                name=name,
                line_start=ls,
                line_end=le,
                signature=f"class {name}",
                bases=_java_class_bases(node, source),
            ))
            body = node.child_by_field_name("body")
            if body is not None:
                for child in _iter_children(body):
                    _visit(child, name)
            return

        if t in ("method_declaration", "constructor_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node, source)
                ls, le = _line_range(node)
                params_node = node.child_by_field_name("parameters")
                params_text = _text(params_node, source) if params_node else "()"
                signature = (
                    f"{inside_class}.{name}{params_text}"
                    if inside_class else f"{name}{params_text}"
                )
                body = node.child_by_field_name("body")
                calls = (
                    _walk_collect_calls(
                        body, source, name,
                        stop_types=_JAVA_DEF_TYPES,
                        call_types=_JAVA_CALL_TYPES,
                        extract_call_name=_java_call_name,
                    )
                    if body is not None else []
                )
                nodes.append(ParsedNode(
                    kind="method",
                    name=name,
                    line_start=ls,
                    line_end=le,
                    signature=signature,
                    calls=calls,
                ))
            return

        for child in _iter_children(node):
            _visit(child, inside_class)

    _visit(tree.root_node, None)
    return nodes


# ── Go 解析 ────────────────────────────────────────────────────────

_GO_DEF_TYPES = {"function_declaration", "method_declaration", "type_declaration"}
_GO_CALL_TYPES = {"call_expression"}


def _go_call_name(call_node) -> str | None:
    """从 Go ``call_expression`` 节点中提取被调用的符号名。"""
    func = call_node.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        return func.text.decode("utf-8", errors="replace")
    if func.type == "selector_expression":
        field = func.child_by_field_name("field")
        if field is not None:
            return field.text.decode("utf-8", errors="replace")
    return None


def _go_receiver_type(method_node, source: bytes) -> str | None:
    """从 ``method_declaration`` 的 receiver 中提取接收器类型名。"""
    receiver = method_node.child_by_field_name("receiver")
    if receiver is None:
        return None
    # receiver → parameter_list → parameter_declaration → type
    for child in _iter_children(receiver):
        if child.type == "parameter_declaration":
            type_node = child.child_by_field_name("type")
            if type_node is None:
                continue
            if type_node.type == "pointer_type":
                for sub in _iter_children(type_node):
                    if sub.type == "type_identifier":
                        return _text(sub, source)
            elif type_node.type == "type_identifier":
                return _text(type_node, source)
    return None


def _go_type_bases(type_spec_node, source: bytes) -> list[str]:
    """从 ``type_spec`` 中提取嵌入字段（struct embedding）或嵌入接口。"""
    bases: list[str] = []
    type_node = type_spec_node.child_by_field_name("type")
    if type_node is None:
        return bases

    if type_node.type == "struct_type":
        # struct_type → field_declaration_list → field_declaration
        for child in _iter_children(type_node):
            if child.type == "field_declaration_list":
                for field_decl in _iter_children(child):
                    if field_decl.type != "field_declaration":
                        continue
                    # 嵌入字段：只有 type，没有 name
                    name_node = field_decl.child_by_field_name("name")
                    type_child = field_decl.child_by_field_name("type")
                    if name_node is None and type_child is not None:
                        if type_child.type == "type_identifier":
                            bases.append(_text(type_child, source))
                        elif type_child.type == "pointer_type":
                            for sub in _iter_children(type_child):
                                if sub.type == "type_identifier":
                                    bases.append(_text(sub, source))

    elif type_node.type == "interface_type":
        # interface_type 内嵌入的接口以 type_identifier 出现
        for child in _iter_children(type_node):
            # Go 1.18+ 可能是 type_elem 包含 type_identifier
            if child.type == "type_identifier":
                bases.append(_text(child, source))
            elif child.type == "type_elem":
                for sub in _iter_children(child):
                    if sub.type == "type_identifier":
                        bases.append(_text(sub, source))

    return bases


def _parse_go_ts(tree, source: bytes) -> list[ParsedNode]:
    """使用 tree-sitter-go 提取函数、方法、类型定义和调用关系。"""
    nodes: list[ParsedNode] = []

    def _visit(node):
        t = node.type

        if t == "type_declaration":
            for child in _iter_children(node):
                if child.type != "type_spec":
                    continue
                name_node = child.child_by_field_name("name")
                type_child = child.child_by_field_name("type")
                if name_node is None or type_child is None:
                    continue
                if type_child.type not in ("struct_type", "interface_type"):
                    continue
                name = _text(name_node, source)
                ls, le = _line_range(node)
                nodes.append(ParsedNode(
                    kind="class",
                    name=name,
                    line_start=ls,
                    line_end=le,
                    signature=f"type {name}",
                    bases=_go_type_bases(child, source),
                ))
            return

        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _text(name_node, source)
            ls, le = _line_range(node)
            params_node = node.child_by_field_name("parameters")
            params_text = _text(params_node, source) if params_node else "()"
            body = node.child_by_field_name("body")
            calls = (
                _walk_collect_calls(
                    body, source, name,
                    stop_types=_GO_DEF_TYPES,
                    call_types=_GO_CALL_TYPES,
                    extract_call_name=_go_call_name,
                )
                if body is not None else []
            )
            nodes.append(ParsedNode(
                kind="function",
                name=name,
                line_start=ls,
                line_end=le,
                signature=f"func {name}{params_text}",
                calls=calls,
            ))
            return

        if t == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _text(name_node, source)
            receiver_type = _go_receiver_type(node, source)
            ls, le = _line_range(node)
            params_node = node.child_by_field_name("parameters")
            params_text = _text(params_node, source) if params_node else "()"
            signature = (
                f"{receiver_type}.{name}{params_text}"
                if receiver_type else f"func {name}{params_text}"
            )
            body = node.child_by_field_name("body")
            calls = (
                _walk_collect_calls(
                    body, source, name,
                    stop_types=_GO_DEF_TYPES,
                    call_types=_GO_CALL_TYPES,
                    extract_call_name=_go_call_name,
                )
                if body is not None else []
            )
            nodes.append(ParsedNode(
                kind="method",
                name=name,
                line_start=ls,
                line_end=le,
                signature=signature,
                calls=calls,
            ))
            return

        for child in _iter_children(node):
            _visit(child)

    _visit(tree.root_node)
    return nodes


# ── 顶层分发 ────────────────────────────────────────────────────────

def parse_file(
    full_path: str,
    rel_path: str,
    enabled_languages: set[str] | None = None,
) -> ParsedFile | None:
    """解析一个源文件并返回 ``ParsedFile``；不支持的语言返回 ``None``。

    函数永不抛异常：解析错误会记录日志后跳过该文件。
    ``enabled_languages`` 过滤使用规范化后的语言名（tsx 归入 typescript）。
    """
    raw_language = _detect_raw_language(rel_path)
    if raw_language is None:
        return None
    canonical = _CANONICAL_LANG[raw_language]
    if enabled_languages is not None and canonical not in enabled_languages:
        return None

    try:
        with open(full_path, "rb") as f:
            source_bytes = f.read()
    except OSError as exc:
        logger.debug("读取文件失败 %s: %s", rel_path, exc)
        return None

    content_hash = _hash_content(source_bytes)

    try:
        parser = _get_parser(raw_language)
        tree = parser.parse(source_bytes)
        if canonical == "python":
            nodes = _parse_python_ts(tree, source_bytes)
        elif canonical in ("javascript", "typescript"):
            nodes = _parse_js_like_ts(tree, source_bytes)
        elif canonical == "java":
            nodes = _parse_java_ts(tree, source_bytes)
        elif canonical == "go":
            nodes = _parse_go_ts(tree, source_bytes)
        else:
            return None
    except ImportError as exc:
        logger.error("tree-sitter 语言绑定未安装: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover — 防御性捕获
        logger.warning("解析文件 %s 失败: %s", rel_path, exc)
        return None

    return ParsedFile(
        path=rel_path.replace("\\", "/"),
        language=canonical,
        content_hash=content_hash,
        nodes=nodes,
    )


# ── 测试用便捷函数（保持与旧版 API 兼容） ──────────────────────────

def _parse_python(content: str) -> list[ParsedNode]:
    """仅供测试使用：直接传入源码字符串解析 Python。"""
    parser = _get_parser("python")
    source = content.encode("utf-8")
    tree = parser.parse(source)
    return _parse_python_ts(tree, source)


def _parse_js_like(content: str, language: str) -> list[ParsedNode]:
    """仅供测试使用：直接传入源码字符串解析 JS/TS。"""
    raw = "javascript" if language == "javascript" else "typescript"
    parser = _get_parser(raw)
    source = content.encode("utf-8")
    tree = parser.parse(source)
    return _parse_js_like_ts(tree, source)


def _parse_java(content: str) -> list[ParsedNode]:
    """仅供测试使用：直接传入源码字符串解析 Java。"""
    parser = _get_parser("java")
    source = content.encode("utf-8")
    tree = parser.parse(source)
    return _parse_java_ts(tree, source)


def _parse_go(content: str) -> list[ParsedNode]:
    """仅供测试使用：直接传入源码字符串解析 Go。"""
    parser = _get_parser("go")
    source = content.encode("utf-8")
    tree = parser.parse(source)
    return _parse_go_ts(tree, source)
