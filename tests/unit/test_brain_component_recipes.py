"""Compatibility invalidates content dependencies, not a repaired HTTP path."""
import ast
import re
import shutil
from pathlib import Path

import pytest

from src.config import settings
from src.core.project_brain import recipes


@pytest.fixture
def source_tree(tmp_path, monkeypatch):
    source = tmp_path / "src"
    shutil.copytree(recipes._SOURCE_ROOT, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    monkeypatch.setattr(recipes, "_SOURCE_ROOT", source)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_NAME", "synthetic-semantic")
    monkeypatch.setattr(settings, "VLLM_MODEL_NAME", "synthetic-privacy")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", "")
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", "")
    return source


def changed(before, after):
    return {name for name in recipes.COMPONENTS if before[name] != after[name]}


AFTER_PRIVACY = {"privacy", "compile", "summary", "render"}
AFTER_COMPILE = {"compile", "summary", "render"}


def test_six_deterministic_hashes_without_returning_private_configuration(source_tree, monkeypatch):
    monkeypatch.setattr(settings, "VLLM_API_KEY", "synthetic-private-key")
    monkeypatch.setattr(settings, "VLLM_API_URL", "https://synthetic-private-gateway.invalid/v1")
    first = recipes.component_recipes()
    assert first == recipes.component_recipes(source_root=source_tree, runtime_versions=recipes._runtime())
    assert set(first) == set(recipes.COMPONENTS)
    assert all(re.fullmatch("[a-f0-9]{64}", value) for value in first.values())
    assert "synthetic-private" not in str(first)
    assert re.fullmatch("[a-f0-9]{64}", recipes.recipe_hash())


@pytest.mark.parametrize("field,value,expected", [
    ("PROJECT_BRAIN_MODEL_HTTP_RETRIES", 0, set()),
    ("PROJECT_BRAIN_MAX_BUILD_TOKENS", 17, set()),
    ("PROJECT_BRAIN_MODEL_NAME", "different-semantic", AFTER_COMPILE),
    ("VLLM_MODEL_NAME", "different-privacy", AFTER_PRIVACY),
    ("PRIVACY_CUSTOM_PATTERNS", '[{"pattern":"synthetic","type":"secret"}]', AFTER_PRIVACY),
    ("PROJECT_BRAIN_PRIVACY_EXTRA_BODY", '{"reasoning_effort":"none"}', AFTER_PRIVACY),
    ("PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", '{"reasoning_effort":"none"}', AFTER_COMPILE),
    ("PROJECT_BRAIN_PACKET_MAX_TOKENS", 63791, AFTER_COMPILE),
    ("PROJECT_BRAIN_LLM_MAX_TOKENS", 8192, AFTER_COMPILE),
    ("PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", 13, AFTER_COMPILE),
    ("AGENT_FORMAT_RETRY_MAX", 7, AFTER_COMPILE),
    ("PROJECT_BRAIN_MAX_SOURCE_FILES", 19991, set(recipes.COMPONENTS)),
    ("LLM_CHARS_PER_TOKEN", 3.17, AFTER_PRIVACY),
])
def test_setting_dependency_changes_are_scoped(source_tree, monkeypatch, field, value, expected):
    before = recipes.component_recipes()
    monkeypatch.setattr(settings, field, value)
    assert changed(before, recipes.component_recipes()) == expected


def test_output_mode_changes_compile_without_changing_source_privacy(source_tree, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    before = recipes.component_recipes()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    assert changed(before, recipes.component_recipes()) == AFTER_COMPILE


def test_inherited_extensions_only_affect_roles_which_actually_inherit(source_tree, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", '{"reasoning_effort":"none"}')
    before = recipes.component_recipes()
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", '{"reasoning_effort":"low"}')
    assert changed(before, recipes.component_recipes()) == AFTER_COMPILE
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", '{"reasoning_effort":"none"}')
    before = recipes.component_recipes()
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", '{"reasoning_effort":"high"}')
    assert recipes.component_recipes() == before


@pytest.mark.parametrize("package,affected", [
    ("httpx", set()), ("httpx2", set()),
    ("openai", AFTER_PRIVACY),
    ("pydantic", {"extract", *AFTER_PRIVACY}),
    ("tree-sitter-python", {"extract", *AFTER_PRIVACY}),
])
def test_dependency_versions_distinguish_content_from_http_backends(source_tree, package, affected):
    runtime = recipes._runtime()
    before = recipes.component_recipes(runtime_versions=runtime)
    runtime[package] = "999.0.0"
    assert changed(before, recipes.component_recipes(runtime_versions=runtime)) == affected


@pytest.mark.parametrize("relative,affected", [
    ("core/project_brain/snapshot.py", set(recipes.COMPONENTS)),
    ("core/project_brain/schema.py", {"extract", *AFTER_PRIVACY}),
    ("core/code_graph/parser.py", {"extract", *AFTER_PRIVACY}),
    ("core/privacy_rules.py", AFTER_PRIVACY),
    ("core/project_brain/compiler.py", AFTER_PRIVACY),
    ("core/project_brain/context.py", AFTER_COMPILE),
    ("core/project_brain/source_bound.py", AFTER_COMPILE),
    ("core/project_brain/summary.py", {"summary", "render"}),
    ("core/project_brain/artifacts.py", {"render"}),
    ("core/project_brain/transport.py", set()),
    ("core/http_transport.py", set()),
])
def test_implementation_dependency_changes_are_scoped(source_tree, relative, affected):
    before, audit_before = recipes.component_recipes(), recipes.recipe_hash()
    path = source_tree / relative
    path.write_text(path.read_text(encoding="utf-8") + "\n# synthetic implementation revision\n", encoding="utf-8")
    assert changed(before, recipes.component_recipes()) == affected
    assert recipes.recipe_hash() != audit_before


def test_timing_and_transport_changes_do_not_invalidate_component_hashes(source_tree):
    before = recipes.component_recipes()
    transport = source_tree / "core/project_brain/transport.py"
    transport.write_text(transport.read_text(encoding="utf-8") + "\n# synthetic timing revision\n", encoding="utf-8")
    (source_tree / "core/project_brain/timing.py").write_text(
        "def synthetic_timing():\n    return 1\n", encoding="utf-8")
    assert recipes.component_recipes() == before


def test_diagnostic_backend_repair_does_not_invalidate_client_content(source_tree):
    before, audit_before = recipes.component_recipes(), recipes.recipe_hash()
    path = source_tree / "core/llm_client.py"
    text = path.read_text(encoding="utf-8")
    assert 'return "unknown"' in text
    text = text.replace('return "unknown"', 'return "synthetic-diagnostic-change"', 1)
    path.write_text(text, encoding="utf-8")
    assert recipes.component_recipes() == before
    assert recipes.recipe_hash() != audit_before


def test_client_request_changes_invalidate_content_even_when_backend_versions_do_not(source_tree):
    before = recipes.component_recipes()
    path = source_tree / "core/llm_client.py"
    text = path.read_text(encoding="utf-8")
    assert '"temperature": temperature' in text
    path.write_text(text.replace('"temperature": temperature', '"temperature": temperature / 2', 1), encoding="utf-8")
    assert changed(before, recipes.component_recipes()) == AFTER_PRIVACY


def test_log_wording_does_not_change_semantic_request_contract(source_tree):
    before = recipes.component_recipes()
    path = source_tree / "core/llm_client.py"
    text = path.read_text(encoding="utf-8")
    assert "LLM 调用:" in text
    path.write_text(text.replace("LLM 调用:", "synthetic diagnostic:", 1), encoding="utf-8")
    assert recipes.component_recipes() == before


def test_queue_recovery_wrapper_is_audited_but_business_flow_is_content_bound(source_tree):
    before, audit_before = recipes.component_recipes(), recipes.recipe_hash()
    path = source_tree / "tasks/brain_task.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\ndef synthetic_recovery_wrapper():\n    return None\n", encoding="utf-8")
    assert recipes.component_recipes() == before
    assert recipes.recipe_hash() != audit_before
    assert 'if build.recipe_hash != recipe_hash():' in text
    path.write_text(text.replace('if build.recipe_hash != recipe_hash():',
                                 'if build.recipe_hash != recipe_hash() or False:', 1), encoding="utf-8")
    assert changed(before, recipes.component_recipes()) == AFTER_PRIVACY


def test_guard_base_normalization_preserves_content_but_audits_wrapper(source_tree):
    path = source_tree / "tasks/brain_task.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    has_base = any(isinstance(node, ast.FunctionDef) and node.name == "_guard_base" for node in tree.body)
    if has_base:
        tree.body = [node for node in tree.body if not (isinstance(node, ast.FunctionDef) and node.name == "_guard")]
        next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_guard_base").name = "_guard"
    path.write_text(ast.unparse(tree), encoding="utf-8")
    before, audit = recipes.component_recipes(), recipes.recipe_hash()
    next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_guard").name = "_guard_base"
    tree.body.extend(ast.parse("def _guard(*args, **kwargs):\n    return _guard_base(*args, **kwargs)\n").body)
    path.write_text(ast.unparse(tree), encoding="utf-8")
    assert recipes.component_recipes() == before
    assert recipes.recipe_hash() != audit


@pytest.mark.parametrize("target", ["guard", "history", "identity", "planner"])
def test_unknown_guard_or_history_business_change_is_not_ignored(source_tree, target):
    before = recipes.component_recipes()
    path = source_tree / "tasks/brain_task.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    selected = (names.get("_guard_base", names.get("_guard")) if target == "guard" else
                names[{"history": "_history_for", "identity": "_source_identity_create", "planner": "_plan_frontier"}[target]])
    selected.body.insert(0, ast.Expr(value=ast.Constant(value="synthetic business contract revision")))
    ast.fix_missing_locations(tree)
    path.write_text(ast.unparse(tree), encoding="utf-8")
    assert changed(before, recipes.component_recipes()) == AFTER_PRIVACY


def test_client_constructor_model_selection_is_content_bound(source_tree):
    before = recipes.component_recipes()
    path = source_tree / "core/llm_client.py"
    source = path.read_text(encoding="utf-8")
    assert "self._model = model_name or settings.VLLM_MODEL_NAME" in source
    path.write_text(source.replace("self._model = model_name or settings.VLLM_MODEL_NAME",
                                   "self._model = settings.VLLM_MODEL_NAME or model_name", 1), encoding="utf-8")
    assert changed(before, recipes.component_recipes()) == AFTER_PRIVACY


def test_runtime_audit_includes_http_retry_backend_and_generator_but_not_budget(source_tree, monkeypatch):
    audit = recipes.recipe_hash()
    components = recipes.component_recipes()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MODEL_HTTP_RETRIES", 1)
    assert recipes.recipe_hash() != audit and recipes.component_recipes() == components
    audit = recipes.recipe_hash()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_MAX_BUILD_TOKENS", 31)
    assert recipes.recipe_hash() == audit
    monkeypatch.setattr(recipes, "GENERATOR_VERSION", "project-brain/synthetic")
    assert recipes.recipe_hash() != audit and recipes.component_recipes() == components


@pytest.mark.parametrize("package", ["httpx", "httpx2"])
def test_http_backend_version_changes_audit_only(source_tree, monkeypatch, package):
    audit, before = recipes.recipe_hash(), recipes.component_recipes()
    original = recipes.version
    monkeypatch.setattr(recipes, "version", lambda name: "999.0.0" if name == package else original(name))
    assert recipes.component_recipes() == before
    assert recipes.recipe_hash() != audit


@pytest.mark.parametrize("bad", [{}, {"openai": "3.26.0"}, {"python": [3, 12, True]}])
def test_incomplete_historical_runtime_is_not_filled_from_current_host(source_tree, bad):
    with pytest.raises(ValueError, match="运行时"):
        recipes.component_recipes(runtime_versions=bad)


def test_selected_source_schema_and_python_version_are_independently_bound(source_tree):
    runtime = recipes._runtime()
    before = recipes.component_recipes(runtime_versions=runtime)
    runtime["python"] = [3, 99, 0]
    assert changed(before, recipes.component_recipes(runtime_versions=runtime)) == set(recipes.COMPONENTS)
    path = source_tree / "core/project_brain/__init__.py"
    text = path.read_text(encoding="utf-8")
    assert re.search(r'^SCHEMA_VERSION = "[^"]+"$', text, re.M)
    path.write_text(re.sub(r'^SCHEMA_VERSION = "[^"]+"$', 'SCHEMA_VERSION = "synthetic-schema"', text, flags=re.M), encoding="utf-8")
    assert changed(before, recipes.component_recipes()) == set(recipes.COMPONENTS)


def test_missing_source_contract_is_not_silently_compatible(source_tree):
    path = source_tree / "core/llm_client.py"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("async def chat(", "async def renamed_chat(", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="契约缺失"):
        recipes.component_recipes()
