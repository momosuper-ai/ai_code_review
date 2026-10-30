"""Read-only, authenticated privacy receipt reuse between recovery builds.

Only the trusted Worker may select the failed parent after checking its database
state and leases. This adapter never copies a call, origin, key or completion
marker into the child. It does not authorize a model request or revive a unit.
The caller persists ``manifest()`` in its authenticated private recovery plan;
every later lease must ``reopen`` that exact manifest and recheck the parent DB.
No source values or reversible maps occur in that persistent manifest.
"""

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import re
from uuid import UUID

from billiard.exceptions import SoftTimeLimitExceeded

from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger
from src.core.project_brain.privacy_history import (
    MAX_HISTORY_BYTES, MAX_PARENTS, PrivacyHistory, _checked_ledger,
)
from src.core.project_brain.privacy_provenance import (
    MAX_INDEX_BYTES, MAX_SCAN_RECORDS, canonical_json, parse_fingerprint_document, validate_records,
)
from src.core.project_brain.privacy_seal import PrivacySealStore

MAX_RECEIPTS = 250_000
MAX_REUSE_RECORDS = MAX_SCAN_RECORDS
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_RECEIPT = re.compile(r"([a-f0-9]{64})\.json\Z")
_MANIFEST_FIELDS = {
    "version", "project_id", "parent_build_id", "source_commit", "parent_recipe_hash",
    "privacy_component_digest", "parent_state", "active_leases", "parent_revision",
    "history_digest", "material_digest", "receipt_inventory_digest", "completed_receipt_count",
    "total_receipt_count", "binding_hmac",
}


class RecoveryPrivacyError(ValueError):
    """A fixed failure category; paths, inputs and provider text never escape."""

    def __init__(self, code="recovery_privacy_invalid"):
        self.failure_code = code
        super().__init__(code)


def _need(condition, code="recovery_privacy_invalid"):
    if not condition:
        raise RecoveryPrivacyError(code)


def _raise_wrapped_interrupt(error):
    """Preserve real task cancellation hidden by lower authentication wrappers.

    Inspect only exception identities/types and their standard chain links.
    No error message or arbitrary payload participates in this decision. The
    fixed visit limit also covers cycles or branching cause/context chains.
    """
    pending, seen = [error], set()
    for _ in range(16):
        if not pending:
            return
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, SoftTimeLimitExceeded):
            raise current
        for name in ("__context__", "__cause__"):
            linked = BaseException.__getattribute__(current, name)
            if isinstance(linked, BaseException) and id(linked) not in seen:
                pending.append(linked)


def _mac(key: bytes, domain: str, value) -> str:
    return hmac.new(key, (domain + canonical_json(value)).encode(), hashlib.sha256).hexdigest()


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _read(path: Path, limit: int) -> bytes:
    _need(isinstance(path, Path))
    _need(not any(item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                  for item in (path, *path.parents)))
    _need(path.is_file())
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    _need(len(value) <= limit)
    return value


def _json(body: bytes):
    return json.loads(body, object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(RecoveryPrivacyError()))


def _content_fence(scope, directory: Path, seal_directory: Path, history_base: Path):
    """Read complete dependency bytes; timestamps never authorize a cache hit.

    The first/last captures bracket full authentication in ``freeze``. A later
    equal pair proves that those exact authenticated bytes are still present,
    without rebuilding their ledgers/HMAC graphs. Receipts and the history
    snapshots referenced by this build are included, including inventories.
    Unrelated history builds are deliberately outside this parent's scope.
    """
    files, inventories, semantics = [], [], []

    def names(path):
        _need(isinstance(path, Path) and path.is_dir())
        _need(not any(item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                      for item in (path, *path.parents)))
        return tuple(sorted(item.name for item in path.iterdir()))

    def capture(label, path, limit, *, optional=False):
        if optional and not path.exists() and not path.is_symlink():
            files.append((label, None))
            return None
        body = _read(path, limit)
        files.append((label, hashlib.sha256(body).digest()))
        return body

    private = directory / "privacy"
    private_names = names(private)
    receipt_names = tuple(name for name in private_names if _RECEIPT.fullmatch(name))
    _need(len(receipt_names) <= MAX_RECEIPTS, "recovery_privacy_receipt_limit")
    _need(set(private_names) <= {"key", "fingerprints.json", "index.lock", *receipt_names})
    # The optional lock is not part of the authenticated inventory, just as in
    # _receipts. It must remain a bounded ordinary file when it is present.
    if "index.lock" in private_names:
        _read(private / "index.lock", 1)
    inventories.append(("privacy", receipt_names))
    capture("privacy/key", private / "key", 32)
    capture("privacy/index", private / "fingerprints.json", MAX_INDEX_BYTES)
    capture("privacy/state", seal_directory / "state.json", 4096)
    capture("privacy/seal", seal_directory / "seal.json", 4096, optional=True)
    for name in receipt_names:
        capture("receipt/" + name, private / name, 32)

    history_root = history_base / "source-history" / str(scope.project_id)
    history_raw = capture("history/build", history_root / "builds" / (scope.build_id + ".json"), 256 * 1024)
    manifest = _json(history_raw)
    _need(type(manifest) is dict and set(manifest) == {
        "version", "project_id", "build_id", "source_commit", "parents", "binding_hmac"})
    parents = manifest["parents"]
    _need(type(parents) is list and len(parents) <= MAX_PARENTS
          and all(type(value) is str and _HEX.fullmatch(value) for value in parents)
          and len(set(parents)) == len(parents))
    semantics.append(("history/build", hashlib.sha256(canonical_json(manifest).encode()).digest()))
    history_bytes = 0
    for snapshot_id in parents:
        snapshot = history_root / "snapshots" / snapshot_id
        snapshot_names = names(snapshot)
        prefix = "history/" + snapshot_id + "/"
        metadata = _json(capture(prefix + "manifest", snapshot / "manifest.json", 256 * 1024))
        _need(type(metadata) is dict and set(metadata) == {
            "version", "project_id", "source_build_id", "source_commit", "key_sha256", "index_sha256",
            "source_kind", "state_sha256", "seal_sha256"})
        _need(metadata["source_kind"] in {"open_work", "sealed_audit"})
        semantics.append((prefix + "manifest", hashlib.sha256(canonical_json(metadata).encode()).digest()))
        expected = {"key", "fingerprints.json", "manifest.json"}
        if metadata["source_kind"] == "sealed_audit":
            expected |= {"state.json", "seal.json"}
        _need(set(snapshot_names) == expected)
        inventories.append((prefix, snapshot_names))
        capture(prefix + "key", snapshot / "key", 32)
        history_bytes += len(capture(prefix + "index", snapshot / "fingerprints.json", MAX_INDEX_BYTES))
        _need(history_bytes <= MAX_HISTORY_BYTES)
        if metadata["source_kind"] == "sealed_audit":
            capture(prefix + "state", snapshot / "state.json", 4096)
            capture(prefix + "seal", snapshot / "seal.json", 4096)
    return tuple(files), tuple(inventories), tuple(semantics)


def _fence_matches_authenticated(fence, materials: dict, inventory: dict, history: PrivacyHistory) -> bool:
    """Bind a capture to the exact material authenticated by this freeze.

    Merely bracketing full validation with equal captures is insufficient:
    A/B/A can otherwise pair a cached A with a ledger validated from B. Every
    captured identity/hash is linked here to the actual validated arguments or
    the authenticated history snapshot manifests. JSON whitespace may differ;
    its canonical meaning and every referenced material digest may not.
    """
    files, directories, semantics = fence
    file_map, directory_map, semantic_map = dict(files), dict(directories), dict(semantics)
    if len(file_map) != len(files) or len(directory_map) != len(directories) or len(semantic_map) != len(semantics):
        return False
    expected_files = {"privacy/" + name for name in materials} | {"history/build"}
    if any(file_map.get("privacy/" + name) != (hashlib.sha256(body).digest() if body is not None else None)
           for name, body in materials.items()):
        return False
    expected_receipts = tuple(digest + ".json" for digest in sorted(inventory))
    if directory_map.get("privacy") != expected_receipts:
        return False
    for digest, item in inventory.items():
        label = "receipt/" + digest + ".json"
        expected_files.add(label)
        if file_map.get(label) != bytes.fromhex(item["body_sha256"]):
            return False
    expected_history = {**history.scope, "parents": [parent.snapshot_id for parent in history.parents],
                        "binding_hmac": history.digest}
    if semantic_map.get("history/build") != hashlib.sha256(canonical_json(expected_history).encode()).digest():
        return False
    expected_directories, expected_semantics = {"privacy"}, {"history/build"}
    for parent in history.parents:
        prefix = "history/" + parent.snapshot_id + "/"
        expected_directories.add(prefix)
        expected_semantics.add(prefix + "manifest")
        expected_files.add(prefix + "manifest")
        if semantic_map.get(prefix + "manifest") != hashlib.sha256(canonical_json(parent.metadata).encode()).digest():
            return False
        expected_names = {"key", "fingerprints.json", "manifest.json"}
        names = ("key", "index")
        if parent.metadata["source_kind"] == "sealed_audit":
            expected_names |= {"state.json", "seal.json"}
            names += ("state", "seal")
        if directory_map.get(prefix) != tuple(sorted(expected_names)):
            return False
        for name in names:
            expected_files.add(prefix + name)
            if file_map.get(prefix + name) != bytes.fromhex(parent.metadata[name + "_sha256"]):
                return False
    return (set(file_map) == expected_files and set(directory_map) == expected_directories
            and set(semantic_map) == expected_semantics)


@dataclass(frozen=True)
class ReceiptParentScope:
    project_id: int
    build_id: str
    source_commit: str
    recipe_hash: str
    privacy_component_digest: str
    state: str = "failed"
    active_leases: int = 0

    def validate(self):
        _need(type(self.project_id) is int and self.project_id > 0 and _uuid(self.build_id)
              and type(self.source_commit) is str and _SHA.fullmatch(self.source_commit)
              and type(self.recipe_hash) is str and _HEX.fullmatch(self.recipe_hash)
              and type(self.privacy_component_digest) is str and _HEX.fullmatch(self.privacy_component_digest)
              and self.state == "failed" and type(self.active_leases) is int and self.active_leases == 0,
              "recovery_privacy_parent_scope_invalid")


@dataclass(frozen=True)
class ReceiptReuse:
    """Parent evidence, not a current successful scan or a model-call receipt."""

    parent_build_id: str
    snapshot_digest: str
    parent_receipt: str
    current_revision: str
    safe_record: dict = field(repr=False, compare=False)


@dataclass(frozen=True)
class FrozenPrivacyReceipts:
    scope: ReceiptParentScope
    _directory: Path = field(repr=False)
    _seal_directory: Path = field(repr=False)
    _history_base: Path = field(repr=False)
    _project_key: bytes = field(repr=False)
    _metadata: dict = field(repr=False)
    _ledger: PrivacyLedger = field(repr=False)
    _completed: frozenset = field(repr=False)
    # Process-local derived proof only. It never enters manifest(), persistence,
    # receipts, or the detached ledger accessor, and contains no source text.
    _content_fence: tuple | None = field(default=None, repr=False, compare=False)

    @classmethod
    def freeze(cls, scope: ReceiptParentScope, *, directory: Path, seal_directory: Path,
               history_base: Path, project_key: bytes, expected_history_digest: str):
        """Authenticate exact parent bytes without creating a lock or file.

        Failed work has an authenticated open seal state; a present closed seal
        is also authenticated and must match the ledger revision. Missing state,
        key, index, history, malformed receipts or legacy un-MACed indices fail.
        Old receipt bodies have no independent MAC. Their keyed names and exact
        inventory become bound by the new project-key recovery manifest here.
        """
        try:
            _need(type(scope) is ReceiptParentScope)
            scope.validate()
            _need(type(project_key) is bytes and len(project_key) == 32
                  and all(isinstance(path, Path) for path in (directory, seal_directory, history_base))
                  and type(expected_history_digest) is str and _HEX.fullmatch(expected_history_digest))
            try:
                before_fence = _content_fence(scope, directory, seal_directory, history_base)
            except Exception as error:
                _raise_wrapped_interrupt(error)
                # A dependency we cannot enumerate never enables fast reuse.
                # Full authentication below remains the authority and may fail.
                before_fence = None
            private = directory / "privacy"
            key = _read(private / "key", 32)
            _need(len(key) == 32)
            raw = _read(private / "fingerprints.json", MAX_INDEX_BYTES)
            document = parse_fingerprint_document(raw)
            _need(document.get("version") in {2, 3} and "index_hmac" in document)
            ledger = _checked_ledger(key, raw, scope.build_id)
            revision = ledger._revision()
            state = _read(seal_directory / "state.json", 4096)
            seal_path = seal_directory / "seal.json"
            seal = _read(seal_path, 4096) if seal_path.exists() or seal_path.is_symlink() else None
            sealed = PrivacySealStore(seal_directory, key).authenticate_documents(_json(state), _json(seal) if seal else None)
            _need(sealed is None or hmac.compare_digest(sealed["revision"], revision))
            history = PrivacyHistory(history_base, scope.project_id, scope.build_id, scope.source_commit, project_key)
            _need(hmac.compare_digest(history.digest, expected_history_digest), "recovery_privacy_history_changed")
            inventory, completed = cls._receipts(private)
            materials = {"key": key, "index": raw, "state": state, "seal": seal}
            # Detect a concurrent late response while capturing an inventory.
            _need(_read(private / "key", 32) == key and _read(private / "fingerprints.json", MAX_INDEX_BYTES) == raw
                  and _read(seal_directory / "state.json", 4096) == state
                  and (_read(seal_path, 4096) if seal_path.exists() or seal_path.is_symlink() else None) == seal
                  and cls._receipts(private)[0] == inventory, "recovery_privacy_parent_changed")
            history._load()
            _need(hmac.compare_digest(history.digest, expected_history_digest), "recovery_privacy_history_changed")
            metadata = {
                "version": 1, "project_id": scope.project_id, "parent_build_id": scope.build_id,
                "source_commit": scope.source_commit, "parent_recipe_hash": scope.recipe_hash,
                "privacy_component_digest": scope.privacy_component_digest, "parent_state": scope.state,
                "active_leases": scope.active_leases, "parent_revision": revision, "history_digest": history.digest,
                "material_digest": _mac(project_key, "recovery:privacy:materials:v1:", {
                    name: hashlib.sha256(body).hexdigest() if body is not None else None for name, body in materials.items()}),
                "receipt_inventory_digest": _mac(project_key, "recovery:privacy:receipts:v1:", inventory),
                "completed_receipt_count": len(completed), "total_receipt_count": len(inventory),
            }
            metadata["binding_hmac"] = _mac(project_key, "recovery:privacy:manifest:v1:", metadata)
            if before_fence is not None:
                _need(_fence_matches_authenticated(before_fence, materials, inventory, history),
                      "recovery_privacy_parent_changed")
                _need(_content_fence(scope, directory, seal_directory, history_base) == before_fence,
                      "recovery_privacy_parent_changed")
            return cls(scope, directory, seal_directory, history_base, project_key, metadata, ledger,
                       frozenset(completed), before_fence)
        except (SoftTimeLimitExceeded, RecoveryPrivacyError) as error:
            _raise_wrapped_interrupt(error)
            raise
        except Exception as error:
            _raise_wrapped_interrupt(error)
            raise RecoveryPrivacyError() from None

    @staticmethod
    def _receipts(private: Path):
        _need(private.is_dir() and not private.is_symlink())
        names = sorted(path.name for path in private.iterdir())
        receipt_names = [name for name in names if _RECEIPT.fullmatch(name)]
        _need(len(receipt_names) <= MAX_RECEIPTS, "recovery_privacy_receipt_limit")
        _need(set(names) <= {"key", "fingerprints.json", "index.lock", *receipt_names})
        if "index.lock" in names:
            _read(private / "index.lock", 1)
        inventory, completed = {}, set()
        for name in receipt_names:
            body = _read(private / name, 32)
            state = _json(body)
            _need(type(state) is str and state in {"completed", "split"})
            digest = _RECEIPT.fullmatch(name)[1]
            inventory[digest] = {"state": state, "body_sha256": hashlib.sha256(body).hexdigest()}
            if state == "completed":
                completed.add(digest)
        return inventory, completed

    def manifest(self) -> dict:
        """Machine metadata plus MAC, suitable for the private recovery plan."""
        return _json(canonical_json(self._metadata).encode())

    @property
    def ledger(self) -> PrivacyLedger:
        """Detached authenticated view for the Worker's DB-origin validation.

        Mutating this view cannot change the frozen lookup ledger or its files.
        It is private Worker data, not an API or serialization surface.
        """
        envelope = self._ledger._envelope(self._ledger.entries, self._ledger.origins)
        envelope["index_hmac"] = self._ledger._index_binding(envelope)
        return _checked_ledger(self._ledger.key, canonical_json(envelope).encode(), self.scope.build_id)

    @classmethod
    def reopen(cls, manifest: dict, *, directory: Path, seal_directory: Path, history_base: Path,
               project_key: bytes, expected_scope: ReceiptParentScope):
        try:
            _need(type(manifest) is dict and set(manifest) == _MANIFEST_FIELDS
                  and type(project_key) is bytes and len(project_key) == 32)
            expected_scope.validate()
            unsigned = {key: value for key, value in manifest.items() if key != "binding_hmac"}
            _need(type(manifest["binding_hmac"]) is str and hmac.compare_digest(
                manifest["binding_hmac"], _mac(project_key, "recovery:privacy:manifest:v1:", unsigned)))
            frozen = cls.freeze(expected_scope, directory=directory, seal_directory=seal_directory,
                history_base=history_base, project_key=project_key, expected_history_digest=manifest["history_digest"])
            _need(hmac.compare_digest(canonical_json(frozen._metadata), canonical_json(manifest)),
                  "recovery_privacy_parent_changed")
            return frozen
        except (SoftTimeLimitExceeded, RecoveryPrivacyError) as error:
            _raise_wrapped_interrupt(error)
            raise
        except Exception as error:
            _raise_wrapped_interrupt(error)
            raise RecoveryPrivacyError() from None

    def assert_current(self):
        """Recheck all bytes at every boundary; reuse only their verified parse."""
        try:
            if self._content_fence is not None:
                manifest = self.manifest()
                _need(set(manifest) == _MANIFEST_FIELDS)
                self.scope.validate()
                unsigned = {key: value for key, value in manifest.items() if key != "binding_hmac"}
                _need(type(self._project_key) is bytes and len(self._project_key) == 32
                      and type(manifest["binding_hmac"]) is str
                      and hmac.compare_digest(manifest["binding_hmac"],
                          _mac(self._project_key, "recovery:privacy:manifest:v1:", unsigned)))
                _need(all(manifest[key] == value for key, value in {
                    "project_id": self.scope.project_id, "parent_build_id": self.scope.build_id,
                    "source_commit": self.scope.source_commit, "parent_recipe_hash": self.scope.recipe_hash,
                    "privacy_component_digest": self.scope.privacy_component_digest,
                    "parent_state": self.scope.state, "active_leases": self.scope.active_leases}.items()))
                before = _content_fence(self.scope, self._directory, self._seal_directory, self._history_base)
                if before == self._content_fence:
                    _need(_content_fence(self.scope, self._directory, self._seal_directory,
                                        self._history_base) == before, "recovery_privacy_parent_changed")
                    return
        except (SoftTimeLimitExceeded, RecoveryPrivacyError) as error:
            _raise_wrapped_interrupt(error)
            raise
        except Exception as error:
            _raise_wrapped_interrupt(error)
            raise RecoveryPrivacyError() from None
        # No cached proof, or different bytes: keep the original complete
        # authentication and exact manifest comparison, never adopt new bytes.
        self.reopen(self.manifest(), directory=self._directory, seal_directory=self._seal_directory,
            history_base=self._history_base, project_key=self._project_key, expected_scope=self.scope)

    def lookup(self, record: dict, *, current: PrivacyLedger, history: PrivacyHistory,
               privacy_component_digest: str) -> ReceiptReuse | None:
        return self.lookup_records([record], current=current, history=history,
                                   privacy_component_digest=privacy_component_digest)[0]

    def lookup_records(self, records: list[dict], *, current: PrivacyLedger, history: PrivacyHistory,
                       privacy_component_digest: str) -> tuple[ReceiptReuse | None, ...]:
        """Match full rule-preprocessed text and field, then reapply all masks.

        The caller must run its ordinary current local policy/history step first.
        Equal values under different build keys remain reusable; a new discovery
        that protects a span not protected by the parent invalidates that record.
        No completion marker or successful-call provenance is written here.
        """
        try:
            _need(type(records) is list and 1 <= len(records) <= MAX_RECEIPTS)
            if len(records) > MAX_REUSE_RECORDS:
                # One parent authentication per bounded batch, never per row.
                # A later batch failure returns no partial receipt collection.
                results = tuple(hit for start in range(0, len(records), MAX_REUSE_RECORDS)
                    for hit in self.lookup_records(records[start:start + MAX_REUSE_RECORDS], current=current,
                        history=history, privacy_component_digest=privacy_component_digest))
                revision = self._current_revision(current, history.scope["build_id"])
                return (tuple(None for _ in records) if any(hit is not None and hit.current_revision != revision
                        for hit in results) else results)
            rows = validate_records(records, 0)
            _need(privacy_component_digest == self.scope.privacy_component_digest,
                  "recovery_privacy_component_mismatch")
            _need(type(current) is PrivacyLedger and type(history) is PrivacyHistory)
            self.assert_current()
            history_digest = history.digest
            history._load()
            _need(hmac.compare_digest(history.digest, history_digest)
                  and history.scope["project_id"] == self.scope.project_id
                  and history.scope["source_commit"] == self.scope.source_commit
                  and history.scope["build_id"] != self.scope.build_id,
                  "recovery_privacy_current_scope_invalid")
            if self._ledger.entries:
                _need(any(parent.metadata["source_build_id"] == self.scope.build_id
                          and hmac.compare_digest(parent.ledger._revision(), self._metadata["parent_revision"])
                          for parent in history.parents), "recovery_privacy_current_history_missing_parent")
            revision = self._current_revision(current, history.scope["build_id"])
            privacy = LedgerPrivacyFilter(current)
            result = []
            for record in rows:
                digest = self._ledger.digest("record:v2:" + canonical_json(record))
                safe = {key: privacy.sanitize_text(value) for key, value in record.items()}
                compatible = all(self._covered_by_parent(value, current) for value in record.values())
                result.append(ReceiptReuse(self.scope.build_id, self._metadata["binding_hmac"], digest,
                    revision, safe) if digest in self._completed and compatible else None)
            self.assert_current()
            history._load()
            _need(hmac.compare_digest(history.digest, history_digest), "recovery_privacy_history_changed")
            # An in-flight sibling discovery may make a previously exact hit
            # incompatible. Return no evidence; ordinary scanning remains due.
            if not hmac.compare_digest(self._current_revision(current, history.scope["build_id"]), revision):
                return tuple(None for _ in rows)
            return tuple(result)
        except (SoftTimeLimitExceeded, RecoveryPrivacyError) as error:
            _raise_wrapped_interrupt(error)
            raise
        except Exception as error:
            _raise_wrapped_interrupt(error)
            raise RecoveryPrivacyError() from None

    @staticmethod
    def _current_revision(ledger, build_id):
        # Follow the existing writer order: index lock, then key/seal reads.
        # A legitimate sibling discovery must not land between refresh() and
        # authentication of its bytes and appear to be a corrupt checkpoint.
        # Release before parent/history checks; the caller's final revision
        # comparison still discards reuse when the current index advances.
        with ledger._index_lock:
            ledger._check_seal_key()
            if ledger._seal_store is not None:
                _need(ledger._seal_store.read() is None, "recovery_privacy_current_sealed")
            _need(ledger.index.is_file() and not ledger.index.is_symlink())
            ledger.refresh()
            raw = _read(ledger.index, MAX_INDEX_BYTES)
            document = parse_fingerprint_document(raw)
            _need(document.get("version") in {2, 3} and "index_hmac" in document)
            checked = _checked_ledger(ledger.key, raw, build_id)
            _need(hmac.compare_digest(checked._revision(), ledger._revision()),
                  "recovery_privacy_current_changed")
            contexts = [call["context"] for call in ledger.origins["calls"].values()]
            contexts.extend(record["context"] for finding in ledger.origins["findings"].values()
                            for record in finding.get("policy_records", {}).values())
            _need(all(context is None or context["build_id"] == build_id for context in contexts),
                  "recovery_privacy_current_scope_invalid")
            return ledger._revision()

    @staticmethod
    def _spans(text, ledger):
        lengths = sorted({length for length, _ in ledger.entries.values()}, reverse=True)
        result = []
        for start in range(len(text)):
            for length in lengths:
                if start + length <= len(text) and ledger.digest("value:" + text[start:start + length]) in ledger.entries:
                    result.append((start, start + length))
                    break
        return result

    def _covered_by_parent(self, text, current):
        previous = self._spans(text, self._ledger)
        return all(any(a <= start and end <= b for a, b in previous)
                   for start, end in self._spans(text, current))
