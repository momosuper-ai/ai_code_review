"""Legacy proofs require the actual old runtime recipe, without DB backfill."""
import json
import sys
from uuid import uuid4

import pytest

from scripts.brain_recovery_attest import main
from src.config import settings
from src.core.project_brain.artifacts import ArtifactStore
from src.core.project_brain.recovery import _components, RecoveryRejected
from src.core.project_brain.source_catalog import identity_key
from src.models.brain import BrainBuild, BrainProject
from tests.unit.test_project_brain import database  # noqa: F401


@pytest.fixture
def legacy(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_BASE_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_WORK_DIR", str(tmp_path / "private"))
    monkeypatch.setattr("src.models.SyncSession", database)
    monkeypatch.setattr("src.core.project_brain.lifecycle.recipe_hash", lambda: "b" * 64)
    with database() as session:
        project = BrainProject(gitlab_project_id=480, project_name="fixture/project", branch="main")
        session.add(project)
        session.flush()
        build = BrainBuild(project_id=project.id, source_commit="a" * 40, recipe_hash="b" * 64,
            request_seq=1, brain_version=1, status="failed", coverage={"public_schema_version": "2.0"},
            total_prompt_tokens=10, total_completion_tokens=2, reserved_tokens=19)
        session.add(build)
        session.commit()
        build_id = build.id
    code = tmp_path / "trusted_service_recipes.py"
    code.write_text("def component_recipes(source_root=None):\n    return {k: 'c'*64 for k in ('scan','extract','privacy','compile','summary','render')}\n", encoding="utf-8")
    store = ArtifactStore()
    key = identity_key(store.work_base, 480, create=True)
    monkeypatch.setattr(sys, "argv", ["attestor", "--build-id", build_id, "--expected-recipe", "b" * 64,
        "--recipes-module", str(code), "--workers-drained"])
    return database, build_id, store, key, code


def test_attestation_is_idempotent_and_never_backfills_old_metadata_or_costs(legacy, capsys):
    database, build_id, store, key, _ = legacy
    main()
    document = store.work_base / "source-recovery-attestations" / (build_id + ".json")
    before = document.read_bytes()
    main()
    assert document.read_bytes() == before
    assert "key" not in json.loads(before)
    with database() as session:
        build = session.get(BrainBuild, build_id)
        assert _components(build, store, key) == {name: "c" * 64 for name in ("scan", "extract", "privacy", "compile", "summary", "render")}
        assert build.coverage == {"public_schema_version": "2.0"}
        assert (build.total_prompt_tokens, build.total_completion_tokens, build.reserved_tokens) == (10, 2, 19)
    assert '"model_calls": 0' in capsys.readouterr().out


@pytest.mark.parametrize("change", ["actual_recipe", "expected_recipe", "parent_ready", "already_has_components", "malformed_components"])
def test_missing_old_runtime_proof_fails_before_writing(legacy, monkeypatch, change):
    database, build_id, store, _, code = legacy
    if change == "actual_recipe":
        monkeypatch.setattr("src.core.project_brain.lifecycle.recipe_hash", lambda: "d" * 64)
    elif change == "expected_recipe":
        monkeypatch.setattr(sys, "argv", ["attestor", "--build-id", build_id, "--expected-recipe", "d" * 64, "--recipes-module", str(code), "--workers-drained"])
    elif change in {"parent_ready", "already_has_components"}:
        with database() as session:
            build = session.get(BrainBuild, build_id)
            if change == "parent_ready":
                build.status = "ready"
            else:
                build.coverage = {"public_schema_version": "2.0", "component_recipes": {}}
            session.commit()
    else:
        code.write_text("def component_recipes(source_root=None):\n    return {'scan': 'c'*64}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        main()
    assert not (store.work_base / "source-recovery-attestations" / (build_id + ".json")).exists()


def test_missing_or_tampered_attestation_is_not_replaced_with_current_hashes(legacy):
    database, build_id, store, key, _ = legacy
    with database() as session:
        with pytest.raises(RecoveryRejected):
            _components(session.get(BrainBuild, build_id), store, key)
    main()
    path = store.work_base / "source-recovery-attestations" / (build_id + ".json")
    doc = json.loads(path.read_text())
    doc["body"]["components"]["compile"] = "f" * 64
    path.write_text(json.dumps(doc))
    with database() as session:
        with pytest.raises(RecoveryRejected):
            _components(session.get(BrainBuild, build_id), store, key)


@pytest.mark.parametrize("corruption", [[], {"body": []}, {"body": {}, "binding_hmac": 7}])
def test_malformed_private_proof_has_closed_failure_category(legacy, corruption):
    database, build_id, store, key, _ = legacy
    main()
    path = store.work_base / "source-recovery-attestations" / (build_id + ".json")
    path.write_text(json.dumps(corruption))
    with database() as session:
        with pytest.raises(RecoveryRejected):
            _components(session.get(BrainBuild, build_id), store, key)
