"""Content compatibility is separate from the complete runtime audit recipe.

Component hashes are necessary compatibility evidence, never permission to
import a checkpoint. Source/packet identity, privacy provenance and billing
are authenticated independently by the recovery boundary. Missing historical
component manifests cannot be reconstructed by assuming today's hashes.
"""
import ast
import copy
import hashlib
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from src.config import settings
from src.core.project_brain import GENERATOR_VERSION, SCHEMA_VERSION

COMPONENT_PROTOCOL = "project-brain-components/1"
COMPONENTS = ("scan", "extract", "privacy", "compile", "summary", "render")
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_PARSER_PACKAGES = ("tree-sitter", "tree-sitter-python", "tree-sitter-javascript",
                    "tree-sitter-typescript", "tree-sitter-java", "tree-sitter-go")
_PACKAGES = (*_PARSER_PACKAGES, "openai", "httpx", "httpx2", "pydantic")
_BRAIN = "core/project_brain/"
_STAGE_FILES = {
    "scan": (_BRAIN + "snapshot.py",),
    "extract": (_BRAIN + "extractor.py", _BRAIN + "schema.py", _BRAIN + "source_catalog.py",
                "core/code_graph/parser.py"),
    "privacy": tuple(_BRAIN + name for name in (
        "privacy.py", "privacy_provenance.py", "privacy_history.py", "privacy_identity.py",
        "privacy_projection.py", "privacy_seal.py", "public_schema.py", "public_projection.py",
        "compiler.py", "exceptions.py", "summary_budget.py")) + ("core/privacy_rules.py", "core/privacy_filter.py", "core/context_manager.py"),
    "compile": tuple(_BRAIN + name for name in (
        "compiler.py", "context.py", "source_bound.py", "synthesis_wire.py", "trusted_packet.py", "planning.py", "summary_budget.py"))
        + ("core/context_manager.py", "core/dimensions.py"),
    "summary": (_BRAIN + "summary.py", _BRAIN + "summary_budget.py"),
    "render": tuple(_BRAIN + name for name in (
        "artifacts.py", "public_schema.py", "public_projection.py", "schema.py", "privacy_seal.py")),
}
_WORKER_BUSINESS = ("_source_identity_create", "_source_catalog", "_read_source_bundle", "_contexts", "_source_privacy_targets",
                    "_bind_source_identity", "_finish_safe_bundle", "_history_for", "_plan_frontier", "_execute")


def _encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _read(root: Path, relative: str) -> bytes:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("组件源码目录无效")
    return path.read_bytes()


def _runtime(runtime_versions: dict | None = None) -> dict:
    if runtime_versions is None:
        result = {"python": list(sys.version_info[:3])}
        for package in _PACKAGES:
            try:
                result[package] = version(package)
            except PackageNotFoundError:
                result[package] = "unavailable"
        return result
    # Never mix an administrator's old-runtime attestation with versions from
    # the current host. A supplied runtime manifest must be complete.
    if type(runtime_versions) is not dict or set(runtime_versions) != {*_PACKAGES, "python"}:
        raise ValueError("组件运行时清单不完整")
    python = runtime_versions["python"]
    if (type(python) not in {list, tuple} or len(python) != 3
            or any(type(item) is not int or item < 0 for item in python)
            or any(type(runtime_versions[name]) is not str
                   or re.fullmatch(r"[A-Za-z0-9_.!+\-]{1,128}", runtime_versions[name]) is None
                   for name in _PACKAGES)):
        raise ValueError("组件运行时清单无效")
    return {**runtime_versions, "python": list(python)}


class _WithoutDiagnostics(ast.NodeTransformer):
    """Remove logging, not data handling, from the shared client contract."""

    def visit_Expr(self, node):
        if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "logger"):
            return None
        return self.generic_visit(node)

    def visit_If(self, node):
        node = self.generic_visit(node)
        if not node.body:
            if not node.orelse:
                return None
            node.test, node.body, node.orelse = ast.UnaryOp(op=ast.Not(), operand=node.test), node.orelse, []
        return node


def _client_content(root: Path) -> str:
    """Track client identity/request handling; exclude only pure diagnostics."""
    tree = ast.parse(_read(root, "core/llm_client.py").decode("utf-8"))
    selected = []
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    if not {"TokenUsage", "ChatResult", "LLMClient"} <= classes.keys():
        raise ValueError("模型内容契约缺失")
    selected.extend(copy.deepcopy(classes[name]) for name in ("TokenUsage", "ChatResult"))
    methods = {node.name: node for node in classes["LLMClient"].body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not {"__init__", "chat", "set_privacy_filter"} <= methods.keys():
        raise ValueError("模型内容契约缺失")
    # Constructor changes can change the selected model or the injected
    # privacy boundary. Keep it whole; future transport-only normalization
    # would need its own reviewed protocol rather than assuming equivalence.
    for name in ("__init__", "chat", "set_privacy_filter"):
        selected.append(_WithoutDiagnostics().visit(copy.deepcopy(methods[name])))
    return _digest([ast.dump(node, include_attributes=False) for node in selected])


def _worker_content(root: Path) -> str:
    """Whole business functions remain strict; recovery/queue wrappers do not."""
    tree = ast.parse(_read(root, "tasks/brain_task.py").decode("utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    guard = functions.get("_guard_base", functions.get("_guard"))
    if not set(_WORKER_BUSINESS) <= functions.keys() or guard is None:
        raise ValueError("知识阶段业务契约缺失")
    # Recovery decorates the existing implementation, whose content contract
    # must remain strict. The outer wrapper is covered by the full audit hash
    # and the authenticated recovery protocol, not by a fake old guard hash.
    guard = copy.deepcopy(guard)
    guard.name = "_guard"
    return _digest([ast.dump(functions[name], include_attributes=False) for name in _WORKER_BUSINESS]
                   + [ast.dump(guard, include_attributes=False)])


def _schema_version(root: Path) -> str:
    tree = ast.parse(_read(root, _BRAIN + "__init__.py").decode("utf-8"))
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION" for target in node.targets)
              and isinstance(node.value, ast.Constant) and type(node.value.value) is str]
    if len(values) != 1:
        raise ValueError("组件Schema契约缺失")
    return values[0]


def _extra_body(role: str) -> dict:
    raw = (settings.PROJECT_BRAIN_PRIVACY_EXTRA_BODY if role == "privacy"
           else settings.PROJECT_BRAIN_SEMANTIC_EXTRA_BODY)
    raw = raw if raw.strip() else settings.LLM_EXTRA_BODY
    try:
        body = json.loads(raw) if raw.strip() else {}
    except (TypeError, ValueError):
        raise ValueError("知识模型扩展参数必须为JSON对象") from None
    if type(body) is not dict:
        raise ValueError("知识模型扩展参数必须为JSON对象")
    return body


def component_recipes(source_root: Path | None = None, runtime_versions: dict | None = None) -> dict[str, str]:
    """Hash six content stages for the selected trusted source/runtime.

    source_root is a src/ directory (containing core/, models/ and tasks/).
    Optional paths/runtime manifests are solely for trusted offline historical
    attestation tooling, never client API input. A supplied runtime manifest
    contains all package keys plus python=[major,minor,micro]. This function
    neither selects historical builds nor persists/backfills any attestation.
    """
    if source_root is not None and not isinstance(source_root, Path):
        raise ValueError("组件源码目录无效")
    root = (_SOURCE_ROOT if source_root is None else source_root).resolve()
    if not root.is_dir() or not (root / "core").is_dir():
        raise ValueError("组件源码目录无效")
    runtime = _runtime(runtime_versions)
    source_hashes = {name: hashlib.sha256(_read(root, name)).hexdigest()
                     for names in _STAGE_FILES.values() for name in names}
    content_client, content_worker = _client_content(root), _worker_content(root)
    common = {"protocol": COMPONENT_PROTOCOL, "schema": _schema_version(root), "python": runtime["python"]}
    context = {name: getattr(settings, name) for name in
               ("LLM_MAX_CONTEXT_TOKENS", "LLM_CONTEXT_THRESHOLD", "LLM_CHARS_PER_TOKEN")}
    configs = {
        "scan": {"source_file_limit": settings.PROJECT_BRAIN_MAX_SOURCE_FILES},
        "extract": {},
        "privacy": {**context, "endpoint": settings.VLLM_API_URL, "model": settings.VLLM_MODEL_NAME,
            "extra_body": _extra_body("privacy"), "privacy": {name: getattr(settings, name) for name in (
                "PRIVACY_FILTER_ENABLED", "PRIVACY_FILTER_LEVEL", "PRIVACY_INTERNAL_DOMAINS",
                "PRIVACY_INTERNAL_CIDRS", "PRIVACY_CUSTOM_PATTERNS", "PRIVACY_TOOL_SCAN_MODE")},
            "limits": {name: getattr(settings, name) for name in (
                "PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS", "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET",
                "PROJECT_BRAIN_PRIVACY_LLM_MAX_TOKENS", "PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS",
                "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT", "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT",
                "PROJECT_BRAIN_PRIVACY_CALL_TIMEOUT")}},
        "compile": {**context, "endpoint": settings.VLLM_API_URL, "model": settings.brain_model_name,
            "extra_body": _extra_body("semantic"), "response_format": settings.response_format_mode_for(settings.brain_model_name),
            "mode": settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE,
            "slots": settings.PROJECT_BRAIN_MAX_EXPLANATION_SLOTS, "packet": settings.PROJECT_BRAIN_PACKET_MAX_TOKENS,
            "output": settings.PROJECT_BRAIN_LLM_MAX_TOKENS,
            "retry_output": settings.PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS, "format_retries": settings.AGENT_FORMAT_RETRY_MAX},
        "summary": {}, "render": {},
    }
    upstream = {"scan": (), "extract": ("scan",), "privacy": ("extract",),
                "compile": ("privacy",), "summary": ("compile",), "render": ("summary",)}
    results = {}
    for stage in COMPONENTS:
        data = {**common, "stage": stage, "settings": configs[stage],
                "implementation": {name: source_hashes[name] for name in _STAGE_FILES[stage]},
                "upstream": {name: results[name] for name in upstream[stage]}}
        if stage == "extract":
            data["runtime"] = {name: runtime[name] for name in (*_PARSER_PACKAGES, "pydantic")}
        if stage == "privacy":
            data["runtime"] = {name: runtime[name] for name in ("openai", "pydantic")}
            data.update(client_content=content_client, worker_business=content_worker)
        results[stage] = _digest(data)
    return results


def recipe_hash() -> str:
    """Complete runtime audit hash; budget increases do not rewrite identity."""
    runtime = _runtime()
    root = _SOURCE_ROOT
    # Complete source auditing includes newly introduced recovery modules.
    files = sorted(str(path.relative_to(root)).replace("\\", "/")
                   for path in (root / "core/project_brain").glob("*.py"))
    files += ["tasks/brain_task.py", "models/brain.py", "config.py", "core/privacy_rules.py",
              "core/privacy_filter.py", "core/llm_client.py", "core/http_transport.py",
              "core/context_manager.py", "core/dimensions.py", "core/code_graph/parser.py"]
    config = {name: getattr(settings, name) for name in (
        "VLLM_API_URL", "VLLM_MODEL_NAME", "LLM_EXTRA_BODY", "PROJECT_BRAIN_PRIVACY_EXTRA_BODY",
        "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE",
        "PROJECT_BRAIN_MAX_EXPLANATION_SLOTS", "PROJECT_BRAIN_MODEL_HTTP_RETRIES",
        "PROJECT_BRAIN_PACKET_MAX_TOKENS", "PROJECT_BRAIN_LLM_MAX_TOKENS", "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS",
        "PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS", "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET",
        "PROJECT_BRAIN_PRIVACY_LLM_MAX_TOKENS", "PROJECT_BRAIN_PRIVACY_MAX_CALLS_PER_UNIT",
        "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT", "PROJECT_BRAIN_PRIVACY_CALL_TIMEOUT",
        "PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS", "PRIVACY_FILTER_ENABLED", "PRIVACY_FILTER_LEVEL",
        "PRIVACY_INTERNAL_DOMAINS", "PRIVACY_INTERNAL_CIDRS", "PRIVACY_CUSTOM_PATTERNS", "PRIVACY_TOOL_SCAN_MODE")}
    config.update(model=settings.brain_model_name, response_format=settings.response_format_mode_for(settings.brain_model_name))
    return _digest({"generator": GENERATOR_VERSION, "schema": SCHEMA_VERSION, "settings": config,
        "runtime": runtime, "components": component_recipes(),
        "implementation": {name: hashlib.sha256(_read(root, name)).hexdigest() for name in files}})
