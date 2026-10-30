"""Worker-private immutable prior HMAC snapshots, never reversible value maps.

The Worker selects candidates by GitLab project in the database. This module
freezes authenticated bytes and rekeys only values actually present in current
complete records. Inheritance is a local policy match, not another model call.
"""

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

from filelock import FileLock

from src.core.project_brain.exceptions import PrivacyCheckpointMissing
from src.core.project_brain.privacy import PrivacyLedger
from src.core.project_brain.privacy_seal import PrivacySealStore
from src.core.project_brain.privacy_provenance import (
    MAX_INDEX_BYTES, canonical_json, decode_fingerprint_index, parse_fingerprint_document,
)

MAX_PARENTS = 256
MAX_HISTORY_BYTES = 128 * 1024 * 1024
_HEX = re.compile(r"[a-f0-9]{64}\Z")


def _fail():
    raise PrivacyCheckpointMissing()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mac(key: bytes, domain: str, value: dict) -> str:
    return hmac.new(key, (domain + canonical_json(value)).encode(), hashlib.sha256).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _read(path: Path, limit: int) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            _fail()
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            _fail()
        return data
    except OSError:
        _fail()


def _json(path: Path, limit: int = 256 * 1024) -> dict:
    try:
        result = json.loads(_read(path, limit), object_pairs_hook=_unique)
        if type(result) is not dict:
            _fail()
        return result
    except (ValueError, TypeError, RecursionError):
        _fail()


def _checked_ledger(key: bytes, raw: bytes, build_id: str) -> PrivacyLedger:
    if len(key) != 32:
        _fail()
    try:
        data = parse_fingerprint_document(raw)
        entries, origins = decode_fingerprint_index(data)
        # Verification is pure and does not open a second, potentially changed
        # source file. Only these exact validated bytes will be frozen.
        ledger = object.__new__(PrivacyLedger)
        ledger.key, ledger.entries, ledger.origins = key, entries, origins
        if "version" in data and not hmac.compare_digest(data["index_hmac"], ledger._index_binding(data)):
            _fail()
        ledger._verify_bindings(origins)
        contexts = [call["context"] for call in origins["calls"].values()]
        contexts.extend(record["context"] for finding in origins["findings"].values()
                        for record in finding.get("policy_records", {}).values())
        if any(context is not None and context["build_id"] != build_id for context in contexts):
            _fail()
        return ledger
    except (ValueError, TypeError, KeyError):
        _fail()


def _sync(directory: Path):
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class HistoryCandidate:
    project_id: int
    build_id: str
    source_commit: str
    directory: Path = field(repr=False)
    sealed_audit: bool = False
    require_index: bool = False


@dataclass(frozen=True)
class HistoryParent:
    snapshot_id: str
    metadata: dict
    ledger: PrivacyLedger = field(repr=False)


class PrivacyHistory:
    """A frozen same-project baseline authenticated with the project key."""

    def __init__(self, base: Path, project_id: int, build_id: str, source_commit: str,
                 project_key: bytes, *, create: bool = False, candidates=(), required=()):
        try:
            if (type(project_id) is not int or project_id <= 0 or type(project_key) is not bytes
                    or len(project_key) != 32 or str(UUID(build_id)) != build_id
                    or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", source_commit) is None):
                _fail()
            self.key = project_key
            self.scope = {"version": 1, "project_id": project_id, "build_id": build_id,
                          "source_commit": source_commit}
            self.root = base / "source-history" / str(project_id)
            if any(p.is_symlink() for p in [self.root, *self.root.parents] if p != base.parent):
                _fail()
            self.path = self.root / "builds" / (build_id + ".json")
            if create:
                self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
                with FileLock(str(self.root / "history.lock"), timeout=10):
                    if not self.path.exists():
                        self._freeze(tuple(candidates), tuple(required))
            self._load()
        except (OSError, TypeError, ValueError, KeyError):
            _fail()

    def _manifest(self, build_id: str) -> dict:
        if type(build_id) is not str or str(UUID(build_id)) != build_id:
            _fail()
        manifest = _json(self.root / "builds" / (build_id + ".json"))
        if (set(manifest) != {"version", "project_id", "build_id", "source_commit", "parents", "binding_hmac"}
                or type(manifest["version"]) is not int or manifest["version"] != 1
                or type(manifest["project_id"]) is not int or manifest["project_id"] != self.scope["project_id"]
                or manifest["build_id"] != build_id
                or type(manifest["source_commit"]) is not str
                or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", manifest["source_commit"]) is None
                or type(manifest["parents"]) is not list or len(manifest["parents"]) > MAX_PARENTS
                or len(set(manifest["parents"])) != len(manifest["parents"])
                or any(type(value) is not str or _HEX.fullmatch(value) is None for value in manifest["parents"])
                or type(manifest["binding_hmac"]) is not str
                or not hmac.compare_digest(manifest["binding_hmac"], _mac(self.key, "privacy-history:build:v1:",
                    {k: v for k, v in manifest.items() if k != "binding_hmac"}))):
            _fail()
        return manifest

    def _snapshot(self, snapshot_id: str) -> HistoryParent:
        if type(snapshot_id) is not str or _HEX.fullmatch(snapshot_id) is None:
            _fail()
        directory = self.root / "snapshots" / snapshot_id
        if directory.is_symlink():
            _fail()
        meta = _json(directory / "manifest.json")
        fields = {"version", "project_id", "source_build_id", "source_commit", "key_sha256", "index_sha256",
                  "source_kind", "state_sha256", "seal_sha256"}
        if (set(meta) != fields or type(meta["version"]) is not int or meta["version"] != 1
                or type(meta["project_id"]) is not int or meta["project_id"] != self.scope["project_id"]
                or str(UUID(meta["source_build_id"])) != meta["source_build_id"]
                or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", meta["source_commit"]) is None
                or type(meta["source_kind"]) is not str or meta["source_kind"] not in {"sealed_audit", "open_work"}
                or _mac(self.key, "privacy-history:snapshot:v1:", meta) != snapshot_id):
            _fail()
        expected = {"key", "fingerprints.json", "manifest.json"}
        if meta["source_kind"] == "sealed_audit":
            expected |= {"state.json", "seal.json"}
        elif meta["state_sha256"] is not None or meta["seal_sha256"] is not None:
            _fail()
        if {p.name for p in directory.iterdir()} != expected:
            _fail()
        key, raw = _read(directory / "key", 32), _read(directory / "fingerprints.json", MAX_INDEX_BYTES)
        if _sha(key) != meta["key_sha256"] or _sha(raw) != meta["index_sha256"]:
            _fail()
        ledger = _checked_ledger(key, raw, meta["source_build_id"])
        if meta["source_kind"] == "sealed_audit":
            state, seal = _read(directory / "state.json", 4096), _read(directory / "seal.json", 4096)
            if _sha(state) != meta["state_sha256"] or _sha(seal) != meta["seal_sha256"]:
                _fail()
            self._audit_seal(key, state, seal, ledger)
        return HistoryParent(snapshot_id, meta, ledger)

    @staticmethod
    def _audit_seal(key: bytes, state: bytes, seal: bytes, ledger: PrivacyLedger):
        try:
            state_value = json.loads(state, object_pairs_hook=_unique)
            seal_value = json.loads(seal, object_pairs_hook=_unique)
            sealed = PrivacySealStore(Path("."), key).authenticate_documents(state_value, seal_value)
            if sealed is None or not hmac.compare_digest(sealed["revision"], ledger._revision()):
                _fail()
        except Exception:
            _fail()

    def _freeze(self, candidates, required):
        snapshots = set()
        for build_id, expected_digest in required:
            prior = self._manifest(build_id)
            if prior["binding_hmac"] != expected_digest:
                _fail()
            snapshots.update(prior["parents"])
            if len(snapshots) > MAX_PARENTS:
                _fail()
        total = sum((self.root / "snapshots" / sid / "fingerprints.json").stat().st_size for sid in snapshots)
        if total > MAX_HISTORY_BYTES:
            _fail()
        for candidate in candidates:
            if (not isinstance(candidate, HistoryCandidate) or candidate.project_id != self.scope["project_id"]
                    or type(candidate.project_id) is not int or str(UUID(candidate.build_id)) != candidate.build_id
                    or type(candidate.require_index) is not bool or type(candidate.sealed_audit) is not bool):
                _fail()
            source = candidate.directory / "privacy"
            if source.is_symlink() or not source.is_dir():
                _fail()
            key = _read(source / "key", 32)
            if (source / "fingerprints.json").is_symlink():
                _fail()
            if not (source / "fingerprints.json").exists():
                if candidate.require_index or candidate.sealed_audit:
                    _fail()
                # A never-written empty index is valid for an open legacy
                # history. Required registered snapshots above cannot vanish.
                continue
            raw = _read(source / "fingerprints.json", MAX_INDEX_BYTES)
            checked = _checked_ledger(key, raw, candidate.build_id)
            audit_files = {}
            if candidate.sealed_audit:
                audit_files = {name: _read(source / name, 4096) for name in ("state.json", "seal.json")}
                self._audit_seal(key, audit_files["state.json"], audit_files["seal.json"], checked)
            if not checked.entries:
                continue
            meta = {"version": 1, "project_id": self.scope["project_id"], "source_build_id": candidate.build_id,
                    "source_commit": candidate.source_commit, "key_sha256": _sha(key), "index_sha256": _sha(raw),
                    "source_kind": "sealed_audit" if candidate.sealed_audit else "open_work",
                    "state_sha256": _sha(audit_files["state.json"]) if audit_files else None,
                    "seal_sha256": _sha(audit_files["seal.json"]) if audit_files else None}
            snapshot_id = _mac(self.key, "privacy-history:snapshot:v1:", meta)
            if snapshot_id not in snapshots:
                if len(snapshots) >= MAX_PARENTS or total + len(raw) > MAX_HISTORY_BYTES:
                    _fail()
                total += len(raw)
            destination = self.root / "snapshots" / snapshot_id
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            if not destination.exists():
                staging = destination.with_name(destination.name + ".tmp-" + uuid4().hex)
                staging.mkdir(mode=0o700)
                try:
                    _write(staging / "key", key)
                    _write(staging / "fingerprints.json", raw)
                    _write(staging / "manifest.json", canonical_json(meta).encode())
                    for name, content in audit_files.items():
                        _write(staging / name, content)
                    _sync(staging)
                    staging.rename(destination)
                    _sync(destination.parent)
                finally:
                    if staging.exists():
                        for name in ("key", "fingerprints.json", "manifest.json", *audit_files):
                            (staging / name).unlink(missing_ok=True)
                        staging.rmdir()
            self._snapshot(snapshot_id)
            snapshots.add(snapshot_id)
        if len(snapshots) > MAX_PARENTS:
            _fail()
        total = sum((self.root / "snapshots" / sid / "fingerprints.json").stat().st_size for sid in snapshots)
        if total > MAX_HISTORY_BYTES:
            _fail()
        manifest = {**self.scope, "parents": sorted(snapshots)}
        manifest["binding_hmac"] = _mac(self.key, "privacy-history:build:v1:", manifest)
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp-" + uuid4().hex)
        _write(temporary, canonical_json(manifest).encode())
        temporary.replace(self.path)
        _sync(self.path.parent)

    def _load(self):
        manifest = self._manifest(self.scope["build_id"])
        if any(manifest[k] != v for k, v in self.scope.items()):
            _fail()
        self.digest = manifest["binding_hmac"]
        total = sum((self.root / "snapshots" / sid / "fingerprints.json").stat().st_size for sid in manifest["parents"])
        if total > MAX_HISTORY_BYTES:
            _fail()
        self.parents = tuple(self._snapshot(sid) for sid in manifest["parents"])

    def apply(self, records: list[dict], ledger: PrivacyLedger, *, scope: str, context=None):
        """Rekey actual current occurrences; completed scans cannot forget them."""
        seen = {(record["source_snapshot"], record["source_finding"])
                for finding in ledger.origins["findings"].values()
                for record in finding.get("policy_records", {}).values()
                if record["kind"] == "inherited_hmac_match"}
        for parent in self.parents:
            pending = {digest: value for digest, value in parent.ledger.entries.items()
                       if (parent.snapshot_id, digest) not in seen}
            lengths = sorted({length for length, _ in pending.values()}, reverse=True)
            found = {}
            for record in records:
                text = record["text"]
                for length in lengths:
                    for start in range(max(0, len(text) - length + 1)):
                        value = text[start:start + length]
                        digest = parent.ledger.digest("value:" + value)
                        if digest in pending:
                            found[digest] = {"value": value, "type": pending[digest][1]}
            for digest, item in found.items():
                ledger.remember_policy([item], records, kind="inherited_hmac_match", scope=scope, context=context,
                    source={"source_build_id": parent.metadata["source_build_id"],
                            "source_commit": parent.metadata["source_commit"],
                            "source_snapshot": parent.snapshot_id, "source_finding": digest})
