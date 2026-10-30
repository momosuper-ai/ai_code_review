"""Authenticated recovery imports; old calls and bills always keep their owner.

Only complete successful checkpoints can be external dependencies. A fresh
historical build owns new I/O; an old unknown call is never adopted or settled
by copying a result. All private paths are derived from server UUIDs.
"""
import copy
import hashlib
import hmac
import json
import os
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from src.core.project_brain.exceptions import SourceBindingError
from src.core.project_brain.recipes import component_recipes, recipe_hash
from src.core.project_brain.trusted_packet import checkpoint_digest
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainRecovery, BrainUnit, utcnow

PROTOCOL = "brain-recovery/1"
MAX_PARENTS = 8
MAX_MANIFEST = 8 * 1024 * 1024
ACTIVE = {"pending", "scanning", "extracting", "compiling", "validating"}


class RecoveryRejected(SourceBindingError):
    failure_code = "recovery_dependency_invalid"


def _fail():
    raise RecoveryRejected()


def _payload(value):
    result = dict(value or {})
    for key in ("_reserved_tokens", "_unknown_tokens"):
        number = result.pop(key, 0)
        if type(number) is not int or number < 0:
            _fail()
    return result


def candidate_plan(session, parent: BrainBuild) -> dict:
    counts = {}
    for unit in session.scalars(select(BrainUnit).where(BrainUnit.build_id == parent.id, BrainUnit.status == "completed")):
        kind = unit.kind if unit.kind in {"scan", "extract", "privacy", "source_privacy", "finalize", "compile", "planning", "module_summary", "project_summary", "assemble"} else "other"
        counts[kind] = counts.get(kind, 0) + 1
    return {"parent_build_id": parent.id, "source_commit": parent.source_commit,
            "state": "candidate_only", "completed_units_by_stage": counts}


def _parent_ok(session, parent):
    if (parent is None or parent.status != "failed" or (parent.coverage or {}).get("public_schema_version") != "2.0"
            or (parent.coverage or {}).get("recovery_write_intents")
            or any(code in (parent.error_message or "") for code in ("privacy_seal_conflict", "known_privacy_regression"))):
        _fail()
    now = utcnow()
    units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == parent.id)).all()
    if (any(unit.status == "running" or unit.lease_until is not None and unit.lease_until > now for unit in units)
            or session.scalar(select(BrainModelCall.id).where(BrainModelCall.build_id == parent.id,
                BrainModelCall.state == "reserved").limit(1)) is not None):
        _fail()


def create_recovery(session, parent_id: str) -> BrainBuild:
    """Register one fresh fixed-SHA recovery, without invoking GitLab/models."""
    parent = session.get(BrainBuild, parent_id)
    if parent is None:
        _fail()
    project = session.scalar(select(BrainProject).where(BrainProject.id == parent.project_id).with_for_update()
        .execution_options(populate_existing=True))
    parent = session.scalar(select(BrainBuild).where(BrainBuild.id == parent_id).with_for_update()
        .execution_options(populate_existing=True))
    _parent_ok(session, parent)
    existing = session.scalars(select(BrainRecovery).where(BrainRecovery.parent_build_id == parent_id)).all()
    for record in existing:
        child = session.get(BrainBuild, record.child_build_id)
        if child and child.status in ACTIVE and child.recipe_hash == recipe_hash():
            return child
    previous = session.scalars(select(BrainBuild).where(BrainBuild.project_id == parent.project_id,
        BrainBuild.source_commit == parent.source_commit, BrainBuild.recipe_hash == recipe_hash(),
        BrainBuild.mode == "full")).all()
    child = BrainBuild(id=str(uuid4()), project_id=project.id, source_commit=parent.source_commit,
        recipe_hash=recipe_hash(), mode="full", generation=max((row.generation for row in previous), default=-1) + 1,
        request_seq=project.request_seq, brain_version=project.next_version, historical=True,
        coverage={"public_schema_version": "2.0", "component_recipes": component_recipes(), "recovery_protocol": PROTOCOL})
    project.next_version += 1
    session.add(child)
    session.flush()
    session.add(BrainRecovery(child_build_id=child.id, parent_build_id=parent.id,
        plan_version=PROTOCOL, compatibility_hash=checkpoint_digest(component_recipes()), plan={}))
    session.add(BrainUnit(build_id=child.id, key="scan", kind="scan"))
    return child


def _fingerprint(session, build_id):
    """Metadata only; errors, provider bodies and source text are excluded."""
    units = [[u.id, u.kind, u.key, u.status, u.input_hash, checkpoint_digest(_payload(u.payload)),
              checkpoint_digest(u.result)] for u in session.scalars(select(BrainUnit).where(
                  BrainUnit.build_id == build_id).order_by(BrainUnit.id).execution_options(populate_existing=True))]
    calls = [[c.id, c.unit_id, c.lease_token, c.stage, c.role, c.purpose, c.requested_model, c.attempt,
              c.state, c.reserved_tokens, c.prompt_tokens, c.completion_tokens, c.cached_tokens,
              c.usage_estimated, c.transport_group, c.transport_attempt, c.http_status,
              str(c.closed_at), str(c.settled_at)] for c in session.scalars(select(BrainModelCall).where(
                  BrainModelCall.build_id == build_id).order_by(BrainModelCall.id).execution_options(populate_existing=True))]
    return {"units": checkpoint_digest(units), "calls": checkpoint_digest(calls)}


def _document_path(store, child_id):
    return store.work_base / "source-recoveries" / (child_id + ".json")


def _read(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail()
            value[key] = item
        return value
    try:
        if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
            _fail()
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            _fail()
        value = json.loads(raw, object_pairs_hook=unique)
        if type(value) is not dict:
            _fail()
        return value
    except (OSError, ValueError, TypeError):
        _fail()


def _binding(key, body):
    return hmac.new(key, (PROTOCOL + ":" + json.dumps(body, sort_keys=True,
        ensure_ascii=True, separators=(",", ":"))).encode(), hashlib.sha256).hexdigest()


def _write(path, body, key):
    document = {"body": body, "binding_hmac": _binding(key, body)}
    if any(item.is_symlink() or getattr(item, "is_junction", lambda: False)() for item in (path, *path.parents)):
        _fail()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    if len(raw) > MAX_MANIFEST:
        _fail()
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if _read(path) != document:
            _fail()
        return document
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return document


def _components(build, store, key):
    from src.core.project_brain.recipes import COMPONENTS
    value = (build.coverage or {}).get("component_recipes")
    if value is None:
        # Old metadata stays unchanged. An operator must run the offline
        # attestor inside the actual old image; never assume today's hashes.
        document = _read(store.work_base / "source-recovery-attestations" / (build.id + ".json"))
        body = document.get("body", {})
        if (type(body) is not dict or set(document) != {"body", "binding_hmac"}
                or type(document["binding_hmac"]) is not str
                or set(body) != {"build_id", "source_commit", "recipe_hash", "components", "workers_drained"}
                or body["build_id"] != build.id or body["source_commit"] != build.source_commit
                or body["recipe_hash"] != build.recipe_hash
                or body["workers_drained"] is not True
                or not hmac.compare_digest(document["binding_hmac"], _binding(key, body))):
            _fail()
        value = body["components"]
    if (type(value) is not dict or set(value) != set(COMPONENTS)
            or any(type(item) is not str or len(item) != 64 or any(c not in "0123456789abcdef" for c in item) for item in value.values())):
        _fail()
    return value


def _privacy(session, parent, project, store, key, components, manifest=None):
    from src.core.project_brain.recovery_privacy import FrozenPrivacyReceipts, ReceiptParentScope
    extraction = session.scalar(select(BrainUnit).where(BrainUnit.build_id == parent.id,
        BrainUnit.kind == "extract", BrainUnit.status == "completed"))
    if extraction is None or not isinstance((extraction.result or {}).get("privacy_history_digest"), str):
        _fail()
    scope = ReceiptParentScope(project.gitlab_project_id, parent.id, parent.source_commit,
        parent.recipe_hash, components["privacy"])
    kwargs = {"directory": store.directory(parent.id, "work"),
        "seal_directory": store.work_base / "source-seals" / parent.id,
        "history_base": store.work_base, "project_key": key}
    frozen = (FrozenPrivacyReceipts.freeze(scope, **kwargs,
                expected_history_digest=extraction.result["privacy_history_digest"])
              if manifest is None else FrozenPrivacyReceipts.reopen(manifest, **kwargs, expected_scope=scope))
    # Index MAC validity alone is insufficient: every model origin must still
    # be an actual successful privacy call of that exact old unit/stage.
    from src.tasks.brain_task import _assert_privacy_calls_settled
    _assert_privacy_calls_settled(session, parent.id, frozen.ledger)
    return frozen


def freeze_parent(session, child, project, store):
    from src.core.project_brain.source_catalog import identity_key
    record = session.get(BrainRecovery, child.id)
    if record is None:
        return
    if record.status == "frozen":
        load_recovery(session, child, project, store)
        return
    if record.status != "pending":
        _fail()
    if (record.compatibility_hash != checkpoint_digest(component_recipes())
            or (child.coverage or {}).get("component_recipes") != component_recipes()):
        _fail()
    key = identity_key(store.work_base, project.gitlab_project_id, create=False)
    parents, seen, parent_id = [], {child.id}, record.parent_build_id
    while parent_id:
        if parent_id in seen or len(parents) >= MAX_PARENTS:
            _fail()
        seen.add(parent_id)
        parent = session.get(BrainBuild, parent_id)
        _parent_ok(session, parent)
        if parent.project_id != child.project_id or parent.source_commit != child.source_commit:
            _fail()
        components = _components(parent, store, key)
        privacy = _privacy(session, parent, project, store, key, components)
        parents.append({"build_id": parent.id, "recipe_hash": parent.recipe_hash,
            "components": components, "fingerprint": _fingerprint(session, parent.id), "privacy": privacy.manifest()})
        predecessor = session.get(BrainRecovery, parent.id)
        if predecessor:
            load_recovery(session, parent, project, store)
        parent_id = predecessor.parent_build_id if predecessor else None
    body = {"protocol": PROTOCOL, "child_build_id": child.id, "project_id": project.gitlab_project_id,
        "source_commit": child.source_commit, "recipe_hash": child.recipe_hash,
        "components": component_recipes(), "parents": parents}
    document = _write(_document_path(store, child.id), body, key)
    record.plan, record.manifest_hash = body, checkpoint_digest(document)
    record.status, record.frozen_at = "frozen", utcnow()


def load_recovery(session, child, project, store):
    from src.core.project_brain.source_catalog import identity_key
    record = session.get(BrainRecovery, child.id)
    if record is None:
        return None
    key = identity_key(store.work_base, project.gitlab_project_id, create=False)
    document = _read(_document_path(store, child.id))
    body = document.get("body", {})
    if (type(body) is not dict or record.status not in {"frozen", "completed"} or set(document) != {"body", "binding_hmac"}
            or type(document["binding_hmac"]) is not str
            or set(body) != {"protocol", "child_build_id", "project_id", "source_commit", "recipe_hash", "components", "parents"}
            or body["protocol"] != PROTOCOL or body["child_build_id"] != child.id
            or body["project_id"] != project.gitlab_project_id or body["source_commit"] != child.source_commit
            or body["recipe_hash"] != child.recipe_hash or body["components"] != _components(child, store, key)
            or record.compatibility_hash != checkpoint_digest(body["components"])
            or not hmac.compare_digest(document["binding_hmac"], _binding(key, body))
            or checkpoint_digest(document) != record.manifest_hash or checkpoint_digest(body) != checkpoint_digest(record.plan)
            or not isinstance(body["parents"], list) or not 1 <= len(body["parents"]) <= MAX_PARENTS):
        _fail()
    frozen, seen = [], {child.id}
    expected = record.parent_build_id
    for item in body["parents"]:
        if (type(item) is not dict or set(item) != {"build_id", "recipe_hash", "components", "fingerprint", "privacy"}
                or item["build_id"] in seen or item["build_id"] != expected):
            _fail()
        seen.add(item["build_id"])
        parent = session.get(BrainBuild, item["build_id"])
        _parent_ok(session, parent)
        if (parent.project_id != child.project_id or parent.source_commit != child.source_commit
                or parent.recipe_hash != item["recipe_hash"] or _components(parent, store, key) != item["components"]
                or _fingerprint(session, parent.id) != item["fingerprint"]):
            _fail()
        frozen.append(_privacy(session, parent, project, store, key, item["components"], item["privacy"]))
        previous = session.get(BrainRecovery, parent.id)
        expected = previous.parent_build_id if previous else None
    if expected is not None:
        _fail()
    return body, frozen


class _LedgerView:
    """RAM-only inherited record receipts; never create child model origins."""
    def __init__(self, ledger, parents):
        self.ledger, self.parents, self.external, self.suppressed = ledger, parents, set(), set()

    def __getattr__(self, name):
        return getattr(self.ledger, name)

    def receipt(self, key):
        return "completed" if key in self.external else self.ledger.receipt(key)

    def refresh(self):
        for parent in self.parents:
            parent.assert_current()
        self.ledger.refresh()
        if getattr(self, "external_revision", None) != self.ledger._revision():
            self.external = set()

    def complete(self, key, state="completed"):
        if key not in self.suppressed:
            self.ledger.complete(key, state)


def bind_guard(guard, child, project, store):
    from src.core.project_brain.lifecycle import SyncSession
    with SyncSession() as session:
        loaded = load_recovery(session, child, project, store)
    if loaded is None:
        return guard
    body, parents = loaded
    original_clean, old_check = guard.clean, guard.discovery_check
    view = _LedgerView(guard.ledger, parents)
    guard.ledger = view
    guard.recovery_receipt_hits = 0

    def check():
        for parent in parents:
            parent.assert_current()
        if old_check:
            old_check()
    guard.discovery_check = check

    async def clean(text):
        from src.core.project_brain.compiler import encoded, sanitize_structure
        from src.core.project_brain.privacy import scan_records
        from src.core.privacy_filter import PrivacyFilter
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        scope = view.digest("input:" + text)
        with SyncSession() as session:
            verify_dependencies(session, session.get(BrainBuild, child.id))
        # Run the same current sticky/rule pass before considering any skip.
        guard.check_discoveries()
        guard._apply_local_policy(parsed, scope)
        external = set()
        external_revisions = set()
        records = scan_records(sanitize_structure(parsed, PrivacyFilter(local_llm=None)))
        for start in range(0, len(records), 128):
            pending = records[start:start + 128]
            for parent in parents:
                if not pending:
                    break
                if parent.scope.privacy_component_digest != body["components"]["privacy"]:
                    continue
                hits = parent.lookup_records(pending, current=view.ledger, history=guard.privacy_history,
                    privacy_component_digest=body["components"]["privacy"])
                unmatched = []
                for record, hit in zip(pending, hits, strict=True):
                    if hit is None:
                        unmatched.append(record)
                    else:
                        external.add(view.digest("record:v2:" + encoded(record)))
                        external_revisions.add(hit.current_revision)
                pending = unmatched
        # A sibling discovery can advance the current index between parent or
        # record batches. Never relabel earlier receipts as evidence for that
        # newer revision. The ordinary guard still authenticates all parents,
        # including those not searched after a complete hit, before any send.
        revision = view.ledger.revision()
        if external and (len(external_revisions) != 1 or revision not in external_revisions):
            external.clear()
        before = view.external, view.suppressed, getattr(view, "external_revision", None)
        view.external, view.suppressed = external, {scope} if external else set()
        view.external_revision = revision
        guard.recovery_receipt_hits += len(external)
        try:
            return await original_clean(text)
        finally:
            view.external, view.suppressed, view.external_revision = before
    guard.clean = clean
    return guard


def bind_unit_guard(guard, build_id):
    from src.core.project_brain.lifecycle import SyncSession
    from src.core.project_brain.artifacts import ArtifactStore
    with SyncSession() as session:
        build = session.get(BrainBuild, build_id)
        if build is None or (build.coverage or {}).get("recovery_protocol") != PROTOCOL:
            return guard
        project = session.get(BrainProject, build.project_id)
    return bind_guard(guard, build, project, ArtifactStore())


def track_parent_discoveries(guard, build_id):
    """A late accepted finding must revoke already exported descendants.

    Persist revocation and a bounded write intent before entering the public
    remember method. DB locks are released before taking the private file lock.
    A crash cannot leave a previously ready child available after a discovery;
    a new recovery also refuses an unresolved parent write intent.
    """
    ledger = guard.ledger
    for name in ("remember_scan", "remember_policy"):
        original = getattr(ledger, name)
        def remember(*args, _original=original, **kwargs):
            intent = str(uuid4())
            invalidate_descendants(build_id, intent=intent)
            result = _original(*args, **kwargs)
            _finish_discovery_intent(build_id, intent)
            return result
        setattr(ledger, name, remember)
    return guard


def invalidate_descendants(parent_id, *, intent=None):
    from src.core.project_brain.lifecycle import SyncSession
    from src.tasks.brain_task import _cancel_unfinished
    from src.models.brain import BrainPublication
    with SyncSession() as session:
        parent = session.get(BrainBuild, parent_id)
        if parent is None:
            return
        project = session.scalar(select(BrainProject).where(BrainProject.id == parent.project_id).with_for_update()
            .execution_options(populate_existing=True))
        parent = session.scalar(select(BrainBuild).where(BrainBuild.id == parent_id).with_for_update()
            .execution_options(populate_existing=True))
        if intent is not None:
            intents = list((parent.coverage or {}).get("recovery_write_intents", []))
            if len(intents) >= 64:
                _fail()
            parent.coverage = {**(parent.coverage or {}), "recovery_write_intents": intents + [intent]}
        frontier, seen = [parent_id], {parent_id}
        while frontier:
            descendants = session.scalars(select(BrainRecovery).where(BrainRecovery.parent_build_id.in_(frontier))).all()
            frontier = []
            for record in descendants:
                if record.child_build_id in seen:
                    continue
                seen.add(record.child_build_id)
                child = session.get(BrainBuild, record.child_build_id)
                if child is None or child.project_id != project.id:
                    _fail()
                record.status, record.error_code = "invalidated", "recovery_parent_changed"
                if child.status in ACTIVE | {"ready"}:
                    child.status, child.completed_at = "failed", utcnow()
                    child.error_message = "恢复依赖出现新隐私发现；failure_code=source_binding_invalid"
                    _cancel_unfinished(session, child.id)
                    if project.active_build_id == child.id:
                        project.active_build_id = None
                    for publication in session.scalars(select(BrainPublication).where(BrainPublication.build_id == child.id)):
                        publication.status, publication.error_code = "failed", "source_binding_invalid"
                        publication.lease_token, publication.lease_until = None, None
                frontier.append(child.id)
        session.commit()


def _finish_discovery_intent(build_id, intent):
    from src.core.project_brain.lifecycle import SyncSession
    with SyncSession() as session:
        observed = session.get(BrainBuild, build_id)
        session.scalar(select(BrainProject).where(BrainProject.id == observed.project_id).with_for_update())
        build = session.scalar(select(BrainBuild).where(BrainBuild.id == build_id).with_for_update()
            .execution_options(populate_existing=True))
        coverage = dict(build.coverage or {})
        intents = list(coverage.get("recovery_write_intents", []))
        if intent not in intents:
            _fail()
        intents.remove(intent)
        if intents:
            coverage["recovery_write_intents"] = intents
        else:
            coverage.pop("recovery_write_intents", None)
        build.coverage = coverage
        session.commit()


async def execute_recoverable(original, unit, build, project, token):
    from src.core.project_brain.recovery_privacy import RecoveryPrivacyError
    try:
        return await _execute_recovery(original, unit, build, project, token)
    except RecoveryPrivacyError:
        # A changed proof is terminal, never three more paid task attempts.
        raise RecoveryRejected() from None


async def _execute_recovery(original, unit, build, project, token):
    from src.core.project_brain.lifecycle import SyncSession
    from src.core.project_brain.artifacts import ArtifactStore
    if (build.coverage or {}).get("recovery_protocol") != PROTOCOL:
        return await original(unit, build, project, token)
    if build.recipe_hash != recipe_hash():
        _fail()
    store = ArtifactStore()
    with SyncSession() as session:
        recovery = session.get(BrainRecovery, build.id)
    if recovery is None:
        return await original(unit, build, project, token)
    if unit.kind == "scan":
        result = await original(unit, build, project, token)
        with SyncSession() as session:
            freeze_parent(session, build, project, store)
            session.commit()
        return result
    with SyncSession() as session:
        loaded = load_recovery(session, build, project, store)
    if unit.kind == "compile":
        inherited = await _compile_checkpoint(unit, build, project, token, store, loaded)
        if inherited is not None:
            return inherited
    return await original(unit, build, project, token)


async def _compile_checkpoint(unit, build, project, token, store, loaded):
    from src.core.project_brain.lifecycle import SyncSession
    from src.tasks import brain_task as worker
    from src.core.project_brain.schema import Compilation
    from src.core.project_brain.snapshot import read_snapshot
    from src.core.project_brain.compiler import packet_hash, project_compilation_texts
    body, _ = loaded
    candidates = []
    with SyncSession() as session:
        for item in body["parents"]:
            if item["components"]["compile"] != body["components"]["compile"]:
                continue
            old = session.scalar(select(BrainUnit).where(BrainUnit.build_id == item["build_id"],
                BrainUnit.key == unit.key, BrainUnit.kind == "compile", BrainUnit.status == "completed"))
            final = session.scalar(select(BrainUnit).where(BrainUnit.build_id == item["build_id"],
                BrainUnit.kind == "finalize", BrainUnit.status == "completed"))
            if old and final and checkpoint_digest(_payload(old.payload)) == checkpoint_digest(_payload(unit.payload)):
                if not _successful_unit_calls(session, old):
                    continue
                candidates.append((item, old, final))
    if not candidates:
        return None
    snapshot = read_snapshot(store.directory(build.id, "snapshots"))
    catalog = worker._source_catalog(build, project, snapshot)
    bundle = worker._read_source_bundle(store, build, catalog)
    context = worker._contexts(build, bundle, catalog.snapshot)[unit.payload["index"]]
    if packet_hash(bundle, build.recipe_hash, context) != unit.input_hash:
        _fail()
    with SyncSession() as session:
        final = session.scalar(select(BrainUnit).where(BrainUnit.build_id == build.id,
            BrainUnit.kind == "finalize", BrainUnit.status == "completed"))
        privacy = session.scalars(select(BrainUnit.status).where(BrainUnit.build_id == build.id, BrainUnit.kind == "privacy")).all()
        source = session.scalars(select(BrainUnit.status).where(BrainUnit.build_id == build.id, BrainUnit.kind == "source_privacy")).all()
        if final is None or not source or any(state != "completed" for state in source):
            _fail()
        from src.core.project_brain.trusted_packet import VerifiedPacket
        VerifiedPacket.from_checkpoint(context, bundle.model_dump(), source_commit=build.source_commit,
            recipe_hash=build.recipe_hash, finalize=final.result, privacy_states=privacy,
            finalize_state=final.status, planned_hash=unit.input_hash)
    for item, old, final in candidates:
        planned = (final.result or {}).get("packets", [])
        matches = [p for p in planned if p.get("index") == unit.payload["index"]]
        if (len(matches) != 1 or matches[0].get("hash") != old.input_hash
                or matches[0].get("packet_digest") != checkpoint_digest(context)):
            continue
        compilation = Compilation.model_validate(old.result, strict=True)
        evidence = {e["id"] for value in context["items"] for e in value["evidence"]}
        if any(not set(claim.evidence_ids) <= evidence for claim in compilation.claims):
            _fail()
        guard = worker._guard(unit, token, new_mode=True, project_id=project.gitlab_project_id)
        try:
            worker._bind_source_identity(bundle, guard, catalog)
            safe = Compilation.model_validate(project_compilation_texts(compilation.model_dump(), guard.filter), strict=True)
            # A newly discovered value changes inherited prose: re-generate the
            # affected unit instead of treating a changed explanation as equal.
            if checkpoint_digest(safe.model_dump()) != checkpoint_digest(compilation.model_dump()):
                continue
            guard.check_discoveries()
            return {**safe.model_dump(), "_recovery_import": {
                "parent_build_id": old.build_id, "parent_unit_id": old.id,
                "parent_input_hash": old.input_hash, "result_hash": checkpoint_digest(safe.model_dump()),
                "input_hash": unit.input_hash}}
        finally:
            await guard.close()
    return None


def recovery_coverage(session, child_id):
    child = session.get(BrainBuild, child_id)
    if child is None or (child.coverage or {}).get("recovery_protocol") != PROTOCOL:
        return {}
    record = session.get(BrainRecovery, child_id)
    if record is None:
        return {}
    lineage = usage_lineage(session, child)
    return {"recovery_parent_build_id": record.parent_build_id,
            "recovery_manifest_hash": record.manifest_hash,
            "recovery_imports_hash": checkpoint_digest(record.imports or {}),
            "recovery_reused_compile_units": len(record.imports or {}),
            "recovery_historical_budget_occupied_tokens": lineage["historical_budget_occupied_tokens"],
            "recovery_historical_unknown_reserved_tokens": lineage["historical_unknown_reserved_tokens"],
            "recovery_parent_count": len((record.plan or {}).get("parents", []))}


def _successful_unit_calls(session, unit, _seen=None):
    from src.core.project_brain.usage import _call_group_identity, _http_error_valid, HTTP_ERROR_UNIT_LIMIT, CallSpec, validate_call_id
    calls = session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == unit.build_id,
        BrainModelCall.unit_id == unit.id)).all()
    if not calls:
        seen = set() if _seen is None else set(_seen)
        if unit.id in seen or len(seen) >= MAX_PARENTS:
            return False
        seen.add(unit.id)
        recovery = session.get(BrainRecovery, unit.build_id)
        proof = (recovery.imports or {}).get(str(unit.id)) if recovery else None
        if (not recovery or recovery.status not in {"frozen", "completed"} or not recovery.manifest_hash
                or not proof or proof.get("result_hash") != checkpoint_digest(unit.result)
                or proof.get("input_hash") != unit.input_hash
                or proof.get("parent_build_id") not in {p["build_id"] for p in (recovery.plan or {}).get("parents", [])}):
            return False
        parent = session.get(BrainUnit, proof.get("parent_unit_id"))
        owner = session.get(BrainBuild, unit.build_id)
        ancestor = session.get(BrainBuild, parent.build_id) if parent else None
        return bool(parent and ancestor and owner and ancestor.status == "failed"
                    and ancestor.project_id == owner.project_id and ancestor.source_commit == owner.source_commit
                    and parent.build_id == proof["parent_build_id"] and parent.status == "completed"
                    and parent.kind == unit.kind == "compile" and parent.key == unit.key
                    and parent.input_hash == proof.get("parent_input_hash")
                    and checkpoint_digest(parent.result) == proof["result_hash"]
                    and checkpoint_digest(_payload(parent.payload)) == checkpoint_digest(_payload(unit.payload))
                    and _successful_unit_calls(session, parent, seen))
    groups, closed = defaultdict(list), 0
    for call in calls:
        try:
            validate_call_id(call.id)
            validate_call_id(call.lease_token)
            CallSpec(call.role, call.requested_model, call.purpose, call.attempt).validate()
            if call.transport_group is not None:
                validate_call_id(call.transport_group)
                if type(call.transport_attempt) is not int or not 1 <= call.transport_attempt <= 5:
                    return False
            elif call.transport_attempt is not None:
                return False
        except (TypeError, ValueError):
            return False
        if call.stage != unit.kind or call.state not in {"settled", "http_error"}:
            return False
        if call.state == "http_error":
            if not _http_error_valid(call) or call.transport_group is None:
                return False
            closed += 1
        elif (call.http_status is not None or call.closed_at is not None or call.settled_at is None
              or type(call.usage_estimated) is not bool
              or any(type(number) is not int or number < 0 for number in
                  (call.prompt_tokens, call.completion_tokens, call.cached_tokens, call.reserved_tokens))
              or call.cached_tokens > call.prompt_tokens):
            return False
        if call.transport_group:
            groups[call.transport_group].append(call)
    if closed > HTTP_ERROR_UNIT_LIMIT:
        return False
    for group in groups.values():
        group.sort(key=lambda call: call.transport_attempt)
        if (group[-1].state != "settled" or any(call.state != "http_error" for call in group[:-1])
                or any(call.transport_attempt != i or _call_group_identity(call) != _call_group_identity(group[0])
                       for i, call in enumerate(group, 1))):
            return False
    return True


def accept_import(session, build, unit, result):
    """Called under the normal project/build/unit completion locks."""
    if "_recovery_import" not in result:
        return result
    marker = result["_recovery_import"]
    safe = {key: value for key, value in result.items() if key != "_recovery_import"}
    record = session.get(BrainRecovery, build.id)
    if (record is None or record.status != "frozen" or unit.kind != "compile"
            or type(marker) is not dict or set(marker) != {"parent_build_id", "parent_unit_id", "parent_input_hash", "result_hash", "input_hash"}
            or type(marker["parent_unit_id"]) is not int or marker["input_hash"] != unit.input_hash
            or marker["result_hash"] != checkpoint_digest(safe)):
        _fail()
    verify_dependencies(session, build)
    parent = session.get(BrainUnit, marker["parent_unit_id"])
    if (parent is None or parent.build_id != marker["parent_build_id"] or parent.status != "completed"
            or parent.kind != unit.kind or parent.key != unit.key
            or parent.input_hash != marker["parent_input_hash"] or checkpoint_digest(parent.result) != marker["result_hash"]
            or marker["parent_build_id"] not in {p["build_id"] for p in record.plan["parents"]}
            or not _successful_unit_calls(session, parent)):
        _fail()
    record.imports = {**(record.imports or {}), str(unit.id): dict(marker)}
    return safe


def verify_dependencies(session, build):
    """DB fence also usable by API readers without access to private volumes."""
    if (build.coverage or {}).get("recovery_protocol") != PROTOCOL:
        return
    record = session.get(BrainRecovery, build.id)
    if record is None or record.status not in {"frozen", "completed"} or not record.manifest_hash:
        _fail()
    if build.status == "ready" and (
            (build.coverage or {}).get("recovery_manifest_hash") != record.manifest_hash
            or (build.coverage or {}).get("recovery_imports_hash") != checkpoint_digest(record.imports or {})):
        _fail()
    plan = record.plan or {}
    if not isinstance(plan.get("parents"), list) or not 1 <= len(plan["parents"]) <= MAX_PARENTS:
        _fail()
    for item in plan["parents"]:
        parent = session.get(BrainBuild, item["build_id"])
        _parent_ok(session, parent)
        if (parent.project_id != build.project_id or parent.source_commit != build.source_commit
                or _fingerprint(session, parent.id) != item["fingerprint"]):
            _fail()
    for key, marker in (record.imports or {}).items():
        child = session.get(BrainUnit, int(key))
        parent = session.get(BrainUnit, marker["parent_unit_id"])
        if (child is None or child.build_id != build.id or child.kind != "compile" or child.status != "completed"
                or child.input_hash != marker["input_hash"] or checkpoint_digest(child.result) != marker["result_hash"]
                or parent is None or parent.build_id != marker["parent_build_id"] or parent.status != "completed"
                or marker["parent_build_id"] not in {p["build_id"] for p in plan["parents"]}
                or parent.kind != child.kind or parent.key != child.key
                or parent.input_hash != marker["parent_input_hash"]
                or checkpoint_digest(parent.result) != marker["result_hash"]):
            _fail()


def usage_lineage(session, build):
    if (build.coverage or {}).get("recovery_protocol") != PROTOCOL:
        return None
    record = session.get(BrainRecovery, build.id)
    ids = [item["build_id"] for item in (record.plan or {}).get("parents", [])] if record else []
    if record and not ids:
        ids = [record.parent_build_id]
    parents = [session.get(BrainBuild, bid) for bid in dict.fromkeys(ids)]
    return {"reused_compile_units": len(record.imports or {}) if record else 0,
        "parent_build_ids": ids,
        "historical_settled_tokens": sum(p.total_prompt_tokens + p.total_completion_tokens for p in parents if p),
        "historical_budget_occupied_tokens": sum(p.reserved_tokens for p in parents if p),
        "historical_unknown_reserved_tokens": sum(call.reserved_tokens for call in session.scalars(
            select(BrainModelCall).where(BrainModelCall.build_id.in_(ids), BrainModelCall.state == "unknown"))),
        "historical_cost_is_new_usage": False, "parent_unknowns_preserved": True}
