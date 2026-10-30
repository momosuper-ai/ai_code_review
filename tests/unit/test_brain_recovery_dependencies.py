"""Real SQLite recovery gates reject changed or foreign checkpoint evidence.

Only body-free synthetic rows are used. No model, GitLab, source snapshot or
private ledger is constructed; the public/private MAC protocol has its own
tests. These checks exercise the production SQL predicates without replacing
their session or checkpoint digest implementations.
"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.llm_client import TokenUsage
from src.core.project_brain import recovery
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.core.project_brain.usage import BuildCallMeter
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit, utcnow
from tests.unit.test_project_brain import database  # noqa: F401


RESULT = {"title": "Synthetic checkpoint", "claims": [{"text": "Synthetic inference",
          "evidence_ids": ["evidence:" + "e" * 24]}], "unknowns": [], "document_path": None}


def add_call(session, build, unit, *, state="settled", group=None, number=None,
             lease=None, status=None):
    call = BrainModelCall(id=str(uuid4()), build_id=build.id, unit_id=unit.id,
        lease_token=lease or str(uuid4()), stage=unit.kind, role="semantic", purpose="initial",
        requested_model="synthetic-model", attempt=1, state=state,
        reserved_tokens=7 if state != "settled" else 11,
        prompt_tokens=10 if state == "settled" else 0,
        completion_tokens=1 if state == "settled" else 0, cached_tokens=0,
        usage_estimated=False, transport_group=group, transport_attempt=number,
        http_status=status, closed_at=utcnow() if state == "http_error" else None,
        settled_at=utcnow() if state == "settled" else None)
    session.add(call)
    session.flush()
    return call


def marker(parent, old, unit, result=None):
    return {"parent_build_id": parent.id, "parent_unit_id": old.id,
            "parent_input_hash": old.input_hash, "result_hash": checkpoint_digest(result or old.result),
            "input_hash": unit.input_hash}


@pytest.fixture
def dependency(database):
    with database() as session:
        project = BrainProject(gitlab_project_id=480, project_name="synthetic/project", branch="main")
        foreign_project = BrainProject(gitlab_project_id=481, project_name="synthetic/other", branch="main")
        session.add_all([project, foreign_project])
        session.flush()
        parent = BrainBuild(project_id=project.id, source_commit="a" * 40, recipe_hash="b" * 64,
            mode="full", request_seq=1, brain_version=1, status="failed",
            coverage={"public_schema_version": "2.0"}, total_prompt_tokens=10,
            total_completion_tokens=1, reserved_tokens=11)
        child = BrainBuild(project_id=project.id, source_commit="a" * 40, recipe_hash="c" * 64,
            mode="full", request_seq=2, brain_version=2, status="compiling",
            coverage={"public_schema_version": "2.0", "recovery_protocol": recovery.PROTOCOL})
        session.add_all([parent, child])
        session.flush()
        payload = {"index": 0, "module_id": "synthetic-module", "fact_ids": ["synthetic-fact"]}
        old = BrainUnit(build_id=parent.id, key="compile:0", kind="compile", status="completed",
            input_hash="d" * 64, payload=deepcopy(payload), result=deepcopy(RESULT))
        unit = BrainUnit(build_id=child.id, key="compile:0", kind="compile", status="running",
            input_hash="f" * 64, payload=deepcopy(payload), lease_token=str(uuid4()))
        session.add_all([old, unit])
        session.flush()
        call = add_call(session, parent, old)
        record = BrainRecovery(child_build_id=child.id, parent_build_id=parent.id,
            plan_version=recovery.PROTOCOL, status="frozen", compatibility_hash="1" * 64,
            manifest_hash="2" * 64, imports={}, plan={"parents": [{"build_id": parent.id,
                "fingerprint": recovery._fingerprint(session, parent.id)}]})
        session.add(record)
        session.commit()
        return SimpleNamespace(factory=database, parent=parent.id, child=child.id, old=old.id,
            unit=unit.id, call=call.id, project=project.id, foreign_project=foreign_project.id)


def rows(session, trial):
    return (session.get(BrainBuild, trial.parent), session.get(BrainBuild, trial.child),
            session.get(BrainUnit, trial.old), session.get(BrainUnit, trial.unit),
            session.get(BrainRecovery, trial.child))


def test_valid_dependency_and_import_keep_parent_accounting_and_results(dependency):
    t = dependency
    with t.factory() as session:
        parent, child, old, unit, record = rows(session, t)
        old_call = session.get(BrainModelCall, t.call)
        before = (parent.reserved_tokens, parent.total_prompt_tokens, parent.total_completion_tokens,
                  deepcopy(old.result), old_call.state, old_call.reserved_tokens)
        recovery.verify_dependencies(session, child)
        proof = marker(parent, old, unit)
        result = recovery.accept_import(session, child, unit, {**deepcopy(RESULT), "_recovery_import": proof})
        assert result == RESULT and "_recovery_import" not in result
        unit.result, unit.status = result, "completed"
        session.commit()
    with t.factory() as session:
        parent, child, old, unit, record = rows(session, t)
        recovery.verify_dependencies(session, child)
        old_call = session.get(BrainModelCall, t.call)
        assert before == (parent.reserved_tokens, parent.total_prompt_tokens, parent.total_completion_tokens,
                          old.result, old_call.state, old_call.reserved_tokens)
        assert record.imports[str(unit.id)] == proof
        assert child.reserved_tokens == child.total_prompt_tokens == child.total_completion_tokens == 0
        assert list(session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == child.id))) == []


@pytest.mark.parametrize("change", ["missing_record", "pending_record", "missing_manifest", "source_sha", "project",
                                    "parent_running", "parent_reserved", "parent_unit_payload", "parent_unit_result",
                                    "parent_unit_status", "parent_call_state", "parent_call_usage"])
def test_dependency_changes_are_rejected_without_rewriting_parent(dependency, change):
    t = dependency
    with t.factory() as session:
        parent, child, old, unit, record = rows(session, t)
        call = session.get(BrainModelCall, t.call)
        if change == "missing_record":
            session.delete(record)
        elif change == "pending_record":
            record.status = "pending"
        elif change == "missing_manifest":
            record.manifest_hash = None
        elif change == "source_sha":
            parent.source_commit = "9" * 40
        elif change == "project":
            parent.project_id = t.foreign_project
        elif change == "parent_running":
            parent.status = "compiling"
        elif change == "parent_reserved":
            call.state = "reserved"
        elif change == "parent_unit_payload":
            old.payload = {**old.payload, "index": 1}
        elif change == "parent_unit_result":
            old.result = {**old.result, "title": "Changed checkpoint"}
        elif change == "parent_unit_status":
            old.status = "failed"
        elif change == "parent_call_state":
            call.state = "unknown"
        elif change == "parent_call_usage":
            call.prompt_tokens += 1
        session.commit()
    with t.factory() as session:
        with pytest.raises(recovery.RecoveryRejected):
            recovery.verify_dependencies(session, session.get(BrainBuild, t.child))


def test_false_and_zero_in_parent_checkpoint_are_distinct_dependencies(dependency):
    t = dependency
    with t.factory() as session:
        parent, child, old, _, record = rows(session, t)
        old.payload = {**old.payload, "test_flag": False}
        session.flush()
        record.plan = {"parents": [{"build_id": parent.id, "fingerprint": recovery._fingerprint(session, parent.id)}]}
        session.commit()
        recovery.verify_dependencies(session, child)
        # SQLAlchemy's JSON dirty comparison also treats False == 0. Force
        # the actual DB mutation; otherwise refresh correctly sees unchanged
        # persisted False and the test only exercises a stale identity map.
        from sqlalchemy import update
        session.execute(update(BrainUnit).where(BrainUnit.id == old.id)
                        .values(payload={**old.payload, "test_flag": 0}))
        session.commit()
        stored = session.scalar(select(BrainUnit.payload).where(BrainUnit.id == old.id))
        assert type(stored["test_flag"]) is int
        with pytest.raises(recovery.RecoveryRejected):
            recovery.verify_dependencies(session, child)


def test_unknown_in_failed_sibling_is_retained_but_cannot_be_reused(dependency):
    t = dependency
    with t.factory() as session:
        parent, child, old, _, record = rows(session, t)
        failed = BrainUnit(build_id=parent.id, key="compile:1", kind="compile", status="failed",
            lease_token=str(uuid4()), payload={"_reserved_tokens": 0, "_unknown_tokens": 7})
        session.add(failed)
        session.flush()
        unknown = add_call(session, parent, failed, state="unknown", lease=failed.lease_token)
        parent.reserved_tokens += 7
        session.flush()
        record.plan = {"parents": [{"build_id": parent.id, "fingerprint": recovery._fingerprint(session, parent.id)}]}
        session.commit()
        assert recovery._successful_unit_calls(session, old)
        assert not recovery._successful_unit_calls(session, failed)
        recovery.verify_dependencies(session, child)
        assert session.get(BrainModelCall, unknown.id).state == "unknown"
        assert parent.reserved_tokens == 18 and failed.payload["_unknown_tokens"] == 7


def test_real_late_settlement_changes_frozen_fingerprint_and_does_not_become_child_usage(dependency):
    t = dependency
    with t.factory() as session:
        parent, child, _, _, record = rows(session, t)
        token = str(uuid4())
        failed = BrainUnit(build_id=parent.id, key="compile:1", kind="compile", status="failed",
            lease_token=token, payload={"_reserved_tokens": 0, "_unknown_tokens": 7})
        session.add(failed)
        session.flush()
        call = add_call(session, parent, failed, state="unknown", lease=token)
        parent.reserved_tokens += 7
        session.flush()
        record.plan = {"parents": [{"build_id": parent.id, "fingerprint": recovery._fingerprint(session, parent.id)}]}
        session.commit()
        recovery.verify_dependencies(session, child)
        unit_id, call_id = failed.id, call.id
    BuildCallMeter(t.parent, unit_id, token).settle(call_id, TokenUsage(2, 1), False, 7, None)
    with t.factory() as session:
        parent, child, _, _, _ = rows(session, t)
        assert session.get(BrainModelCall, call_id).state == "settled"
        assert (parent.total_prompt_tokens, parent.total_completion_tokens, parent.reserved_tokens) == (12, 2, 14)
        assert (child.total_prompt_tokens, child.total_completion_tokens, child.reserved_tokens) == (0, 0, 0)
        with pytest.raises(recovery.RecoveryRejected):
            recovery.verify_dependencies(session, child)


@pytest.mark.parametrize("change", ["unknown", "reserved", "wrong_stage", "open_http_group", "group_gap", "group_lease"])
def test_only_settled_or_exactly_recovered_call_groups_support_old_unit(dependency, change):
    t = dependency
    with t.factory() as session:
        parent, _, old, _, _ = rows(session, t)
        call = session.get(BrainModelCall, t.call)
        if change in {"unknown", "reserved"}:
            call.state = change
        elif change == "wrong_stage":
            call.stage = "privacy"
        else:
            session.delete(call)
            session.flush()
            group, token = str(uuid4()), str(uuid4())
            add_call(session, parent, old, state="http_error", group=group, number=1, lease=token, status=500)
            if change != "open_http_group":
                add_call(session, parent, old, group=group, number=3 if change == "group_gap" else 2,
                         lease=str(uuid4()) if change == "group_lease" else token)
        session.commit()
        assert not recovery._successful_unit_calls(session, old)


def test_completed_same_group_http_retry_is_valid_without_hiding_closed_bound(dependency):
    t = dependency
    with t.factory() as session:
        parent, _, old, _, _ = rows(session, t)
        session.delete(session.get(BrainModelCall, t.call))
        session.flush()
        group, token = str(uuid4()), str(uuid4())
        closed = add_call(session, parent, old, state="http_error", group=group, number=1, lease=token, status=500)
        add_call(session, parent, old, group=group, number=2, lease=token)
        session.commit()
        assert recovery._successful_unit_calls(session, old)
        assert closed.state == "http_error" and closed.reserved_tokens == 7


@pytest.mark.parametrize("change", ["parent_id", "parent_unit_id", "parent_input", "child_input", "result_hash",
                                    "extra_marker", "boolean_unit_id", "child_kind", "parent_kind", "parent_key",
                                    "foreign_project", "parent_active", "parent_not_completed", "parent_result_changed"])
def test_import_marker_cannot_borrow_or_reinterpret_a_checkpoint(dependency, change):
    t = dependency
    with t.factory() as session:
        parent, child, old, unit, record = rows(session, t)
        proof = marker(parent, old, unit)
        result = {**deepcopy(RESULT), "_recovery_import": proof}
        if change == "parent_id":
            proof["parent_build_id"] = str(uuid4())
        elif change == "parent_unit_id":
            proof["parent_unit_id"] = old.id + 1000
        elif change == "parent_input":
            proof["parent_input_hash"] = "9" * 64
        elif change == "child_input":
            proof["input_hash"] = "9" * 64
        elif change == "result_hash":
            proof["result_hash"] = "9" * 64
        elif change == "extra_marker":
            proof["unrecognized"] = "must not be ignored"
        elif change == "boolean_unit_id":
            proof["parent_unit_id"] = True
        elif change == "child_kind":
            unit.kind = "source_privacy"
        elif change == "parent_kind":
            old.kind = "source_privacy"
            session.get(BrainModelCall, t.call).stage = old.kind
        elif change == "parent_key":
            old.key = "compile:999"
        elif change == "foreign_project":
            parent.project_id = t.foreign_project
        elif change == "parent_active":
            parent.status = "compiling"
        elif change == "parent_not_completed":
            old.status = "failed"
        elif change == "parent_result_changed":
            old.result = {**old.result, "title": "Newer parent result"}
        session.flush()
        with pytest.raises(recovery.RecoveryRejected):
            recovery.accept_import(session, child, unit, result)
        assert record.imports == {}


def test_import_result_hash_does_not_equate_false_and_zero(dependency):
    t = dependency
    with t.factory() as session:
        parent, child, old, unit, _ = rows(session, t)
        old.result = {"synthetic_machine_flag": False}
        session.flush()
        proof = marker(parent, old, unit)
        with pytest.raises(recovery.RecoveryRejected):
            recovery.accept_import(session, child, unit,
                {"synthetic_machine_flag": 0, "_recovery_import": proof})


def test_import_proof_cannot_point_outside_frozen_parent_inventory(dependency):
    t = dependency
    with t.factory() as session:
        parent, child, old, unit, record = rows(session, t)
        proof = marker(parent, old, unit)
        unit.result = recovery.accept_import(session, child, unit,
            {**deepcopy(RESULT), "_recovery_import": proof})
        unit.status = "completed"
        session.flush()
        recovery.verify_dependencies(session, child)
        foreign = BrainBuild(project_id=t.foreign_project, source_commit=parent.source_commit,
            recipe_hash="8" * 64, mode="full", request_seq=1, brain_version=1, status="failed",
            coverage={"public_schema_version": "2.0"})
        session.add(foreign)
        session.flush()
        substitute = BrainUnit(build_id=foreign.id, key=old.key, kind=old.kind, status="completed",
            input_hash=old.input_hash, payload=deepcopy(old.payload), result=deepcopy(old.result))
        session.add(substitute)
        session.flush()
        add_call(session, foreign, substitute)
        record.imports = {str(unit.id): {**proof, "parent_build_id": foreign.id, "parent_unit_id": substitute.id}}
        session.flush()
        with pytest.raises(recovery.RecoveryRejected):
            recovery.verify_dependencies(session, child)


@pytest.mark.parametrize("change", ["missing", "pending", "missing_manifest", "wrong_hash", "wrong_input", "foreign_parent"])
def test_no_call_parent_unit_needs_a_frozen_authenticated_import(dependency, change):
    t = dependency
    with t.factory() as session:
        parent, _, old, _, _ = rows(session, t)
        session.delete(session.get(BrainModelCall, t.call))
        predecessor = BrainBuild(project_id=parent.project_id, source_commit=parent.source_commit,
            recipe_hash="8" * 64, mode="full", request_seq=0, brain_version=0, status="failed",
            coverage={"public_schema_version": "2.0"})
        session.add(predecessor)
        session.flush()
        proof = {"parent_build_id": predecessor.id, "parent_unit_id": 999,
            "parent_input_hash": "7" * 64, "result_hash": checkpoint_digest(old.result), "input_hash": old.input_hash}
        record = BrainRecovery(child_build_id=parent.id, parent_build_id=predecessor.id,
            plan_version=recovery.PROTOCOL, status="frozen", manifest_hash="6" * 64,
            compatibility_hash="5" * 64, imports={str(old.id): proof}, plan={"parents": [{"build_id": predecessor.id}]})
        if change != "missing":
            session.add(record)
        if change == "pending":
            record.status = "pending"
        elif change == "missing_manifest":
            record.manifest_hash = None
        elif change == "wrong_hash":
            record.imports = {str(old.id): {**proof, "result_hash": "4" * 64}}
        elif change == "wrong_input":
            record.imports = {str(old.id): {**proof, "input_hash": "4" * 64}}
        elif change == "foreign_parent":
            record.imports = {str(old.id): {**proof, "parent_build_id": str(uuid4())}}
        session.flush()
        assert not recovery._successful_unit_calls(session, old)


def test_no_call_unit_can_reuse_an_exact_frozen_ancestor_import(dependency):
    t = dependency
    with t.factory() as session:
        parent, _, old, _, _ = rows(session, t)
        session.delete(session.get(BrainModelCall, t.call))
        predecessor = BrainBuild(project_id=parent.project_id, source_commit=parent.source_commit,
            recipe_hash="8" * 64, mode="full", request_seq=0, brain_version=0, status="failed",
            coverage={"public_schema_version": "2.0"})
        session.add(predecessor)
        session.flush()
        ancestor = BrainUnit(build_id=predecessor.id, key=old.key, kind="compile", status="completed",
            input_hash="7" * 64, payload=deepcopy(old.payload), result=deepcopy(old.result))
        session.add(ancestor)
        session.flush()
        add_call(session, predecessor, ancestor)
        proof = marker(predecessor, ancestor, old)
        record = BrainRecovery(child_build_id=parent.id, parent_build_id=predecessor.id,
            plan_version=recovery.PROTOCOL, status="frozen", manifest_hash="6" * 64,
            compatibility_hash="5" * 64, imports={str(old.id): proof},
            plan={"parents": [{"build_id": predecessor.id,
                              "fingerprint": recovery._fingerprint(session, predecessor.id)}]})
        session.add(record)
        session.commit()
        assert recovery._successful_unit_calls(session, old)
        assert not list(session.scalars(select(BrainModelCall).where(BrainModelCall.unit_id == old.id)))
        assert session.get(BrainRecovery, parent.id).imports[str(old.id)] == proof


def test_unmodified_non_import_result_stays_on_normal_completion_path(dependency):
    with dependency.factory() as session:
        _, child, _, unit, record = rows(session, dependency)
        result = deepcopy(RESULT)
        assert recovery.accept_import(session, child, unit, result) is result
        assert record.imports == {}


@pytest.mark.parametrize("crash", [False, True])
def test_late_discovery_revokes_before_private_write_and_preserves_crash_intent(dependency, crash):
    from types import SimpleNamespace
    t = dependency
    with t.factory() as session:
        parent, child, _, _, _ = rows(session, t)
        before = (parent.reserved_tokens, parent.total_prompt_tokens, parent.total_completion_tokens)
        child.status = "ready"
        session.commit()

    def write():
        with t.factory() as session:
            parent, child, _, _, record = rows(session, t)
            assert child.status == "failed" and record.status == "invalidated"
            assert parent.coverage["recovery_write_intents"]
        if crash:
            raise RuntimeError("synthetic_private_write_interrupted")
        return "committed"

    guard = SimpleNamespace(ledger=SimpleNamespace(remember_scan=write, remember_policy=write))
    recovery.track_parent_discoveries(guard, t.parent)
    if crash:
        with pytest.raises(RuntimeError, match="synthetic_private_write_interrupted"):
            guard.ledger.remember_scan()
    else:
        assert guard.ledger.remember_scan() == "committed"
    with t.factory() as session:
        parent, child, _, _, _ = rows(session, t)
        assert bool(parent.coverage.get("recovery_write_intents")) is crash
        assert (parent.reserved_tokens, parent.total_prompt_tokens, parent.total_completion_tokens) == before
        if crash:
            with pytest.raises(recovery.RecoveryRejected):
                recovery._parent_ok(session, parent)
