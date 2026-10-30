"""Independent regressions for lexical ownership and complete evidence."""

import hashlib
from pathlib import Path

from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.schema import Fact, FactBundle, Relation
from src.core.project_brain.snapshot import RepoSnapshot, source_fingerprint, source_lines


def extract_fixture(tmp_path: Path, files: dict[str, str]) -> FactBundle:
    """Build a synthetic fixed snapshot without executing fixture code."""
    metadata = {}
    for path, text in files.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        content = text.encode()
        metadata[path] = {"blob_sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
                          "lines": len(source_lines(text)), "available": True, "mode": "100644"}
    snapshot = RepoSnapshot(str(tmp_path), "a" * 40, metadata, [], source_fingerprint(metadata))
    return FactExtractor(snapshot).extract()


def calls_from(bundle: FactBundle, name: str) -> tuple[Fact, list[Relation]]:
    """Return a symbol and all its call relationships."""
    owner = next(f for f in bundle.facts if f.kind == "symbol" and f.name == name)
    return owner, [r for r in bundle.relations if r.type == "CALLS" and r.from_id == owner.id]


def test_nested_assignments_do_not_shadow_outer_import(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "from helper import operation as imported\ndef outer():\n    def nested():\n        imported = 1\n    return imported()\n", "helper.py": "def operation():\n    pass\n"})
    outer, calls = calls_from(bundle, "outer")
    assert "imported" not in outer.attributes["assigned_names"]
    assert calls[0].resolution == "resolved"


def test_nested_function_is_function_and_local_definition_shadows_global(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "def helper():\n    pass\ndef outer():\n    def helper():\n        pass\n    return helper()\n"})
    nested, _ = calls_from(bundle, "outer.helper")
    assert nested.attributes["symbol_kind"] == "function"
    outer, calls = calls_from(bundle, "outer")
    assert "helper" in outer.attributes["shadowed_names"]
    assert calls[0].resolution != "resolved"


def test_method_nested_function_is_not_method(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "class Service:\n    def run(self):\n        def inner():\n            return unknown()\n        return inner()\n"})
    method, _ = calls_from(bundle, "Service.run")
    nested, _ = calls_from(bundle, "Service.run.inner")
    assert method.attributes["symbol_kind"] == "method"
    assert nested.attributes["symbol_kind"] == "function"


def test_closure_binding_prevents_false_import_resolution(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "from helper import operation\ndef outer(operation):\n    def inner():\n        return operation()\n    return inner\n", "helper.py": "def operation():\n    pass\n"})
    _, calls = calls_from(bundle, "outer.inner")
    assert calls[0].resolution != "resolved"


def test_nested_calls_keep_their_lexical_owner(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "def outer():\n    direct()\n    def inner():\n        nested()\n    class Local:\n        def run(self):\n            method_only()\n    return inner()\n"})
    _, outer_calls = calls_from(bundle, "outer")
    _, inner_calls = calls_from(bundle, "outer.inner")
    assert {r.target_name for r in outer_calls} == {"direct", "inner"}
    assert {r.target_name for r in inner_calls} == {"nested"}


def test_comprehension_target_does_not_shadow_outer_name(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "from helper import operation\ndef run(values):\n    items = [operation for operation in values]\n    return operation()\n", "helper.py": "def operation():\n    pass\n"})
    owner, calls = calls_from(bundle, "run")
    assert "operation" not in owner.attributes["shadowed_names"]
    assert calls[0].resolution == "resolved"


def test_comprehension_call_does_not_resolve_target_as_import(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "from helper import operation\ndef run(values):\n    return [operation() for operation in values]\n", "helper.py": "def operation():\n    pass\n"})
    _, calls = calls_from(bundle, "run")
    assert calls[0].resolution != "resolved"


def test_duplicate_call_relationship_preserves_every_site(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "def helper():\n    pass\ndef run():\n    helper()\n    helper()\n"})
    _, calls = calls_from(bundle, "run")
    refs = {r.id: r for r in bundle.source_refs}
    assert len(calls) == 1
    assert {refs[rid].line_start for rid in calls[0].source_ref_ids} == {4, 5}


def test_same_named_tests_in_different_files_keep_correct_test_fact(tmp_path):
    bundle = extract_fixture(tmp_path, {"a.py": "def work():\n    pass\ndef test_work():\n    work()\n", "b.py": "def other():\n    pass\ndef test_work():\n    other()\n"})
    facts = {f.id: f for f in bundle.facts}
    tested = [r for r in bundle.relations if r.type == "TESTED_BY"]
    assert len(tested) == 2
    assert all(facts[r.from_id].attributes["path"] == facts[r.to_id].attributes["path"] for r in tested)


def test_repeated_environment_key_preserves_all_usage_evidence(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "import os\nfirst = os.getenv('SETTING')\nsecond = os.getenv('SETTING')\n"})
    fact = next(f for f in bundle.facts if f.kind == "config")
    refs = {r.id: r for r in bundle.source_refs}
    assert {refs[rid].line_start for rid in fact.source_ref_ids} == {2, 3}


def test_redefined_symbol_is_never_resolved_to_one_declaration(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "def helper():\n    pass\ndef helper():\n    pass\ndef run():\n    helper()\n"})
    declaration, _ = calls_from(bundle, "helper")
    _, calls = calls_from(bundle, "run")
    assert declaration.attributes["redefined"] is True
    assert len(declaration.source_ref_ids) == 2
    assert calls[0].resolution != "resolved"


def test_scope_and_all_sites_survive_incremental_restore(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "def target():\n    pass\ndef outer(target):\n    def inner():\n        target()\n        target()\n    return inner\n"})
    metadata = bundle.files
    snapshot = RepoSnapshot(str(tmp_path), "b" * 40, metadata, [], source_fingerprint(metadata))
    extractor = FactExtractor(snapshot, bundle)
    restored = extractor.extract()
    assert extractor.reused_files == 1
    _, calls = calls_from(restored, "outer.inner")
    assert calls[0].resolution != "resolved"
    assert len(calls[0].source_ref_ids) == 2
    assert all(ref.commit_sha == "b" * 40 for ref in restored.source_refs)


def test_main_guard_evidence_does_not_start_on_preceding_blank_lines(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "value = 1\n\n\nif __name__ == '__main__':\n    start()\n"})
    fact = next(f for f in bundle.facts if f.kind == "interface" and f.name == "Python 启动入口")
    refs = {ref.id: ref for ref in bundle.source_refs}
    assert refs[fact.source_ref_ids[0]].line_start == 4


def test_main_guard_inside_docstring_is_not_startup_fact(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": '"""\nif __name__ == "__main__":\n    example()\n"""\n'})
    assert not [f for f in bundle.facts if f.name == "Python 启动入口"]


def test_main_guard_explicit_uvicorn_import_creates_asgi_entrypoint(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "if __name__ == '__main__':\n    import uvicorn as server\n    server.run('app.main:app', host='not-a-public-value')\n"})
    fact = next(f for f in bundle.facts if f.kind == "entrypoint" and f.name == "uvicorn.run")
    assert fact.attributes["entry_type"] == "asgi"
    assert fact.attributes["application"] == "app.main:app"
    assert "not-a-public-value" not in str(fact.attributes)


def test_main_guard_rebound_or_nested_uvicorn_is_not_asgi_entrypoint(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "if __name__ == '__main__':\n    import uvicorn\n    uvicorn = factory\n    uvicorn.run()\n    def deferred():\n        import uvicorn\n        uvicorn.run()\n"})
    assert not [f for f in bundle.facts if f.kind == "entrypoint" and f.name == "uvicorn.run"]


def test_unicode_line_separator_in_value_does_not_shift_configuration_evidence(tmp_path):
    bundle = extract_fixture(tmp_path, {".env.example": "NOTE=first\u2028second\nMODE=safe\nNEXT=plain\n"})
    fact = next(f for f in bundle.facts if f.kind == "config" and f.name == "MODE")
    refs = {ref.id: ref for ref in bundle.source_refs}
    assert refs[fact.source_ref_ids[0]].line_start == 2


def test_imported_uvicorn_run_supports_keyword_static_application(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "from uvicorn import run as serve\nif __name__ == '__main__':\n    serve(app='app.main:app')\n"})
    fact = next(f for f in bundle.facts if f.kind == "entrypoint" and f.name == "uvicorn.run")
    refs = {ref.id: ref for ref in bundle.source_refs}
    assert fact.attributes["application_kind"] == "import_string"
    assert refs[fact.source_ref_ids[0]].line_start == 3


def test_uvicorn_dynamic_application_is_unknown(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "import uvicorn\nif __name__ == '__main__':\n    uvicorn.run(factory())\n"})
    assert not [f for f in bundle.facts if f.kind == "entrypoint" and f.name == "uvicorn.run"]
    assert any("ASGI 启动参数动态" in issue for issue in bundle.issues)


def test_function_local_main_guard_is_not_module_startup(tmp_path):
    bundle = extract_fixture(tmp_path, {"main.py": "def deferred():\n    if __name__ == '__main__':\n        start()\n"})
    assert not [f for f in bundle.facts if f.name == "Python 启动入口"]
