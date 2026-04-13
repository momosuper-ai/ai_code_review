"""src.core.code_graph 单元测试。"""

import os
from unittest.mock import MagicMock

import pytest

from src.core.code_graph.parser import parse_file, _parse_python, _parse_js_like, _parse_java, _parse_go
from src.core.code_graph.store import CodeGraphStore
from src.core.code_graph.query import CodeGraph


# ─────────────────────────────────────────────────────────────────────
# 解析器测试
# ─────────────────────────────────────────────────────────────────────

class TestParserPython:
    def test_parse_basic_function_and_class(self):
        src = (
            "def foo():\n"
            "    return bar()\n"
            "\n"
            "def bar():\n"
            "    return 1\n"
            "\n"
            "class Base:\n"
            "    pass\n"
            "\n"
            "class Child(Base):\n"
            "    def method(self):\n"
            "        foo()\n"
        )
        nodes = _parse_python(src)
        names = {n.name for n in nodes}
        assert "foo" in names
        assert "bar" in names
        assert "Base" in names
        assert "Child" in names
        assert "method" in names

        # foo 调用 bar
        foo = next(n for n in nodes if n.name == "foo")
        assert "bar" in foo.calls

        # Child 继承自 Base
        child = next(n for n in nodes if n.name == "Child")
        assert "Base" in child.bases

        # method 调用 foo
        method = next(n for n in nodes if n.name == "method")
        assert "foo" in method.calls
        assert method.kind == "method"

    def test_parse_syntax_error_returns_empty(self):
        nodes = _parse_python("def foo(\n")
        assert nodes == []


class TestParserMultiLanguage:
    def test_javascript_basic(self):
        src = (
            "function greet(name) {\n"
            "    console.log(name);\n"
            "    return sanitize(name);\n"
            "}\n"
            "\n"
            "class Widget extends Base {\n"
            "}\n"
        )
        nodes = _parse_js_like(src, "javascript")
        names = {n.name for n in nodes}
        assert "greet" in names
        assert "Widget" in names
        greet = next(n for n in nodes if n.name == "greet")
        assert "sanitize" in greet.calls
        widget = next(n for n in nodes if n.name == "Widget")
        assert "Base" in widget.bases

    def test_typescript_arrow_function(self):
        src = (
            "export const handler = async (req) => {\n"
            "    return process(req);\n"
            "};\n"
        )
        nodes = _parse_js_like(src, "typescript")
        names = {n.name for n in nodes}
        assert "handler" in names

    def test_java_basic(self):
        src = (
            "public class Calculator extends Base {\n"
            "    public int add(int a, int b) {\n"
            "        return compute(a, b);\n"
            "    }\n"
            "}\n"
        )
        nodes = _parse_java(src)
        names = {n.name for n in nodes}
        assert "Calculator" in names
        assert "add" in names
        calc = next(n for n in nodes if n.name == "Calculator")
        assert "Base" in calc.bases

    def test_parse_file_by_extension(self, tmp_path):
        f = tmp_path / "sample.py"
        f.write_text("def x():\n    return 1\n")
        parsed = parse_file(str(f), "sample.py")
        assert parsed is not None
        assert parsed.language == "python"
        assert any(n.name == "x" for n in parsed.nodes)

    def test_go_basic(self):
        src = (
            "package main\n"
            "\n"
            "type Base struct {}\n"
            "\n"
            "type User struct {\n"
            "    Base\n"
            "    Name string\n"
            "}\n"
            "\n"
            "func greet(name string) string {\n"
            "    return helper()\n"
            "}\n"
            "\n"
            "func helper() string { return \"\" }\n"
        )
        nodes = _parse_go(src)
        names = {n.name for n in nodes}
        assert "Base" in names
        assert "User" in names
        assert "greet" in names
        assert "helper" in names

        greet = next(n for n in nodes if n.name == "greet")
        assert greet.kind == "function"
        assert "helper" in greet.calls

        user = next(n for n in nodes if n.name == "User")
        assert user.kind == "class"
        assert "Base" in user.bases

    def test_go_method_with_receiver(self):
        src = (
            "package main\n"
            "\n"
            "type Server struct {}\n"
            "\n"
            "func (s *Server) Start() { s.Listen() }\n"
            "\n"
            "func (s Server) Stop() {}\n"
        )
        nodes = _parse_go(src)
        start = next(n for n in nodes if n.name == "Start")
        assert start.kind == "method"
        assert "Server." in start.signature

        stop = next(n for n in nodes if n.name == "Stop")
        assert stop.kind == "method"
        assert "Server." in stop.signature

        assert "Listen" in start.calls

    def test_go_interface_embedding(self):
        src = (
            "package main\n"
            "\n"
            "type Reader interface {\n"
            "    Read()\n"
            "}\n"
            "\n"
            "type ReadWriter interface {\n"
            "    Reader\n"
            "    Write()\n"
            "}\n"
        )
        nodes = _parse_go(src)
        rw = next(n for n in nodes if n.name == "ReadWriter")
        assert rw.kind == "class"
        assert "Reader" in rw.bases

    def test_parse_file_go(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text("package main\nfunc hello() {}\n")
        parsed = parse_file(str(f), "main.go")
        assert parsed is not None
        assert parsed.language == "go"
        assert any(n.name == "hello" for n in parsed.nodes)

    def test_parse_file_unsupported_returns_none(self, tmp_path):
        f = tmp_path / "thing.xyz"
        f.write_text("nothing")
        assert parse_file(str(f), "thing.xyz") is None


# ─────────────────────────────────────────────────────────────────────
# Store 测试
# ─────────────────────────────────────────────────────────────────────

class TestStore:
    def test_incremental_upsert(self, tmp_path):
        store = CodeGraphStore(str(tmp_path))
        with store:
            # 首次解析
            f = tmp_path / "a.py"
            f.write_text("def old():\n    return 1\n")
            parsed = parse_file(str(f), "a.py")
            assert parsed is not None
            count1 = store.upsert_file(parsed)
            store.commit()
            assert count1 == 1
            assert not store.is_empty()

            # 文件变更：新符号替换旧符号
            f.write_text("def renamed():\n    return 2\n")
            parsed2 = parse_file(str(f), "a.py")
            store.upsert_file(parsed2)
            store.commit()

            rows = store.find_nodes_by_path("a.py")
            names = {r.name for r in rows}
            assert "renamed" in names
            assert "old" not in names

    def test_delete_file(self, tmp_path):
        store = CodeGraphStore(str(tmp_path))
        with store:
            f = tmp_path / "b.py"
            f.write_text("def foo():\n    return 1\n")
            parsed = parse_file(str(f), "b.py")
            store.upsert_file(parsed)
            store.commit()
            assert store.find_nodes_by_path("b.py")

            store.delete_file("b.py")
            store.commit()
            assert store.find_nodes_by_path("b.py") == []


# ─────────────────────────────────────────────────────────────────────
# 查询与影响面测试
# ─────────────────────────────────────────────────────────────────────

class TestBlastRadius:
    def test_blast_radius_chain(self, tmp_path):
        """构造 A -> B -> C 调用链，查询 B 的影响面，验证上下游都正确。"""
        f = tmp_path / "chain.py"
        f.write_text(
            "def C():\n"
            "    return 1\n"
            "\n"
            "def B():\n"
            "    return C()\n"
            "\n"
            "def A():\n"
            "    return B()\n"
        )
        store = CodeGraphStore(str(tmp_path))
        with store:
            parsed = parse_file(str(f), "chain.py")
            store.upsert_file(parsed)
            store.commit()

        graph = CodeGraph(str(tmp_path))
        try:
            assert graph.available()
            results = graph.get_blast_radius("chain.py", symbol="B", depth=2)
            assert len(results) == 1
            r = results[0]
            upstream_names = {c["name"] for c in r.upstream_callers}
            downstream_names = {c["name"] for c in r.downstream_callees}
            assert "A" in upstream_names
            assert "C" in downstream_names
        finally:
            graph.close()

    def test_find_definition(self, tmp_path):
        f = tmp_path / "defs.py"
        f.write_text("def target_fn():\n    return 1\n")
        store = CodeGraphStore(str(tmp_path))
        with store:
            parsed = parse_file(str(f), "defs.py")
            store.upsert_file(parsed)
            store.commit()

        graph = CodeGraph(str(tmp_path))
        try:
            results = graph.find_definition("target_fn")
            assert len(results) == 1
            assert results[0]["name"] == "target_fn"
            assert "defs.py:1" in results[0]["location"]
        finally:
            graph.close()

    def test_not_available_when_empty(self, tmp_path):
        graph = CodeGraph(str(tmp_path))
        try:
            assert not graph.available()
        finally:
            graph.close()
