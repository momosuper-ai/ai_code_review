"""Worker-private irreversible fingerprints and bounded scan receipts.

Only keyed fingerprints, bounded candidate-location metadata and receipts persist.
Original values, source packets and reversible sanitization maps stay in memory.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from filelock import FileLock

from src.core.privacy_filter import PrivacyFilter
from src.core.project_brain.exceptions import PrivacyCheckpointMissing, PrivacySealConflict
from src.core.project_brain.privacy_seal import PrivacySealStore
from src.core.project_brain.privacy_provenance import (
    MAX_INDEX_BYTES, MAX_LOCATIONS, TYPES, canonical_json, decode_fingerprint_index,
    field_role, parse_fingerprint_document, validate_items, validate_records,
    validate_context, validate_policy_record, validate_scan_metadata,
)

_PLACEHOLDER = re.compile(r"(<<[A-Z]+_\d+>>)")


class PrivacyScanDeferred(Exception):
    """An intact checkpoint allows a fresh lease to continue bounded work."""


class PrivacyLedger:
    """Share irreversible discoveries and scan receipts within one build only."""

    def __init__(self, directory: Path, *, create: bool = True, seal_directory: Path | None = None,
                 require_index: bool = False) -> None:
        if type(create) is not bool or type(require_index) is not bool:
            raise ValueError("隐私账本创建模式无效")
        self._require_index = require_index
        self.root = directory / "privacy"
        self.index = self.root / "fingerprints.json"
        self._index_lock_instance = None
        self._seal_store: PrivacySealStore | None = None
        key = self.root / "key"
        if seal_directory is not None and (self.root.is_symlink() or key.is_symlink()):
            raise PrivacySealConflict()
        new_key = False
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._index_lock:
                if not key.exists():
                    if any(path.name != "index.lock" for path in self.root.iterdir()):
                        if seal_directory is not None:
                            raise PrivacySealConflict()
                        raise ValueError("隐私指纹密钥缺失，停止构建")
                    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(secrets.token_bytes(32))
                        stream.flush()
                        os.fsync(stream.fileno())
                    self._sync_directory(self.root)
                    new_key = True
                with key.open("rb") as stream:
                    self.key = stream.read(33)
                if require_index and len(self.key) == 32:
                    if new_key:
                        # New protocol durably represents even an empty set.
                        # A lost existing index must never become empty again.
                        self._commit_unlocked({}, {"calls": {}, "findings": {}})
                    elif not self.index.is_file() or self.index.is_symlink():
                        raise PrivacyCheckpointMissing()
                if seal_directory is not None and len(self.key) == 32:
                    # The first open marker shares the key-creation lock. A
                    # sibling can never observe a valid key but no anchor.
                    self._seal_store = PrivacySealStore(seal_directory, self.key)
                    self._seal_store.initialize(new_key=new_key)
            if len(self.key) != 32:
                if seal_directory is not None:
                    raise PrivacySealConflict()
                raise ValueError("隐私指纹密钥损坏，停止构建")
        else:
            # A persisted DB checkpoint cannot establish a fresh privacy history.
            # Do not mkdir or acquire a lock that can itself create a lock file.
            try:
                if not self.root.is_dir():
                    if seal_directory is not None:
                        raise PrivacySealConflict()
                    raise PrivacyCheckpointMissing()
                with key.open("rb") as stream:
                    self.key = stream.read(33)
            except OSError:
                if seal_directory is not None:
                    raise PrivacySealConflict() from None
                raise PrivacyCheckpointMissing() from None
            if len(self.key) != 32:
                if seal_directory is not None:
                    raise PrivacySealConflict()
                raise PrivacyCheckpointMissing()
        if seal_directory is not None and self._seal_store is None:
            self._seal_store = PrivacySealStore(seal_directory, self.key)
            # Reopening is read-only; only the key-creation transaction above
            # is permitted to establish a new persistent anchor.
            self._seal_store.initialize(new_key=False)
        self.entries: dict[str, tuple[int, str]] = {}
        self.origins: dict = {"calls": {}, "findings": {}}
        self.refresh()
        if self._seal_store is not None:
            sealed = self._seal_store.read()
            if sealed is not None and not hmac.compare_digest(sealed["revision"], self._revision()):
                raise PrivacySealConflict()

    @property
    def _index_lock(self):
        # A legacy create=False constructor remains a read-only operation and
        # must not even instantiate a potentially creating file lock.
        if self._index_lock_instance is None:
            self._index_lock_instance = FileLock(str(self.root / "index.lock"), timeout=10)
        return self._index_lock_instance

    def _assert_unsealed(self) -> None:
        """Use the in-memory build key even after disposable work is removed."""
        if self._seal_store is not None:
            if self._seal_store.read() is not None:
                raise PrivacySealConflict()
            self._check_seal_key()

    def assert_writable_discovery(self) -> None:
        """Check a strictly validated positive response before a lease gate.

        This check neither adds a finding nor authorizes an expired lease. A
        later write still reacquires the lock and independently checks the seal.
        """
        if self._seal_store is None:
            return
        # Closed history can be authenticated from the retained key even when
        # disposable work (and thus its index lock parent) has been removed.
        self._assert_unsealed()
        with self._index_lock:
            self._assert_unsealed()

    def _check_seal_key(self) -> None:
        """A live work directory cannot silently lose or replace its build key."""
        try:
            key = self.root / "key"
            if self.root.is_symlink() or key.is_symlink():
                raise PrivacySealConflict()
            with key.open("rb") as stream:
                current = stream.read(33)
            if len(current) != 32 or not hmac.compare_digest(current, self.key):
                raise PrivacySealConflict()
        except OSError:
            raise PrivacySealConflict() from None

    def _revision(self) -> str:
        envelope = self._envelope(self.entries, self.origins)
        envelope["index_hmac"] = self._index_binding(envelope)
        return self.digest("privacy:revision:v1:" + canonical_json(envelope))

    @staticmethod
    def _envelope(entries: dict, origins: dict) -> dict:
        policy = any(finding.get("policy_records") for finding in origins["findings"].values())
        findings = {digest: {**finding, **({"policy_records": finding.get("policy_records", {})} if policy else {})}
                    for digest, finding in origins["findings"].items()}
        return {"version": 3 if policy else 2, "entries": {key: list(value) for key, value in entries.items()},
                "origins": {"calls": origins["calls"], "findings": findings}}

    def revision(self) -> str:
        """Return a keyed digest of the complete current discovery transaction."""
        with self._index_lock:
            if self._seal_store is not None:
                self._seal_store.read()
                self._check_seal_key()
            self.refresh()
            return self._revision()

    def seal(self, expected_revision: str, content_digest: str) -> dict:
        """Fence public content against every discovery committed before ready."""
        if (self._seal_store is None or type(expected_revision) is not str or type(content_digest) is not str
                or re.fullmatch(r"[a-f0-9]{64}", expected_revision) is None
                or re.fullmatch(r"[a-f0-9]{64}", content_digest) is None):
            raise PrivacySealConflict()
        # A valid seal survives work cleanup and can be checked without opening
        # the removed index/key. It may only acknowledge the exact same content.
        prior = self._seal_store.read()
        if prior is not None and not self.root.exists():
            return self._seal_store.seal(expected_revision, content_digest)
        with self._index_lock:
            self._seal_store.read()
            self._check_seal_key()
            self.refresh()
            if not hmac.compare_digest(expected_revision, self._revision()):
                raise PrivacySealConflict()
            return self._seal_store.seal(expected_revision, content_digest)

    def retain_audit(self, destination: Path) -> None:
        """Retain only authenticated irreversible history in a private directory.

        ``destination/privacy`` is a standalone read-only audit ledger; a caller
        can reopen it with create=False and that privacy directory as its seal
        directory. No scan receipts, context, source or reversible map is copied.
        """
        if self._seal_store is None or not isinstance(destination, Path):
            raise PrivacySealConflict()
        if (destination.is_symlink() or destination.resolve().is_relative_to(self.root.parent.resolve())):
            raise PrivacySealConflict()
        with self._index_lock:
            self._check_seal_key()
            sealed = self._seal_store.read()
            if sealed is None:
                raise PrivacySealConflict()
            try:
                self.refresh()
            except (ValueError, OSError):
                raise PrivacySealConflict() from None
            if not hmac.compare_digest(sealed["revision"], self._revision()):
                raise PrivacySealConflict()
            envelope = self._envelope(self.entries, self.origins)
            envelope["index_hmac"] = self._index_binding(envelope)
            # Decode and authenticate the complete canonical copy, including
            # candidate origins and per-call bindings, before retaining a key.
            try:
                entries, origins = decode_fingerprint_index(envelope)
                self._verify_bindings(origins)
            except ValueError:
                raise PrivacySealConflict() from None
            if entries != self.entries:
                raise PrivacySealConflict()
            files = {"key": self.key, "fingerprints.json": canonical_json(envelope).encode(),
                     "state.json": canonical_json(self._seal_store._read_json(self._seal_store.state_path)).encode(),
                     "seal.json": canonical_json(sealed).encode()}
            try:
                destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with FileLock(str(destination) + ".lock", timeout=10):
                    if destination.exists():
                        self._check_audit(destination, files)
                        return
                    staging = destination.with_name(destination.name + ".tmp-" + secrets.token_hex(8))
                    staging.mkdir(mode=0o700)
                    private = staging / "privacy"
                    private.mkdir(mode=0o700)
                    try:
                        for name, content in files.items():
                            fd = os.open(private / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(content)
                                stream.flush()
                                os.fsync(stream.fileno())
                        self._sync_directory(private)
                        self._sync_directory(staging)
                        self._check_audit(staging, files)
                        # The independently retained seal is checked again
                        # before moving the all-or-nothing audit directory.
                        if self._seal_store.read() != sealed:
                            raise PrivacySealConflict()
                        staging.rename(destination)
                        self._sync_directory(destination.parent)
                    finally:
                        # Only exact files created by this invocation may be
                        # removed; do not traverse an arbitrary audit tree.
                        if staging.exists():
                            for name in files:
                                (private / name).unlink(missing_ok=True)
                            private.rmdir()
                            staging.rmdir()
            except OSError:
                raise PrivacySealConflict() from None

    @staticmethod
    def _check_audit(destination: Path, files: dict[str, bytes]) -> None:
        private = destination / "privacy"
        try:
            if (destination.is_symlink() or not destination.is_dir()
                    or {path.name for path in destination.iterdir()} != {"privacy"}
                    or private.is_symlink() or not private.is_dir()):
                raise PrivacySealConflict()
            names = {path.name for path in private.iterdir()}
            # A read-only audit reopen does not create locks. Calling revision
            # later may create this fixed empty mechanical lock, not a receipt.
            if not set(files) <= names or not names <= set(files) | {"index.lock"}:
                raise PrivacySealConflict()
            if "index.lock" in names:
                lock = private / "index.lock"
                if lock.is_symlink() or not lock.is_file() or lock.stat().st_size > 1:
                    raise PrivacySealConflict()
            for name, expected in files.items():
                path = private / name
                if path.is_symlink() or not path.is_file():
                    raise PrivacySealConflict()
                with path.open("rb") as stream:
                    content = stream.read(len(expected) + 1)
                if not hmac.compare_digest(content, expected):
                    raise PrivacySealConflict()
        except OSError:
            raise PrivacySealConflict() from None
    def digest(self, text: str) -> str:
        """Hash inputs without exposing dictionary-testable unkeyed secrets."""
        return hmac.new(self.key, text.encode(), hashlib.sha256).hexdigest()

    def refresh(self) -> None:
        """Reload all committed discoveries, including other worker processes."""
        if self._require_index and (not self.index.is_file() or self.index.is_symlink()):
            raise PrivacyCheckpointMissing()
        if self.index.exists():
            with self.index.open("rb") as stream:
                data = parse_fingerprint_document(stream.read(MAX_INDEX_BYTES + 1))
            entries, origins = decode_fingerprint_index(data)
            if "version" in data and not hmac.compare_digest(data["index_hmac"], self._index_binding(data)):
                raise ValueError("隐私指纹或来源检查点无效")
            self._verify_bindings(origins)
            self.entries, self.origins = entries, origins

    def _index_binding(self, envelope: dict) -> str:
        version = envelope.get("version", 2)
        return self.digest(f"provenance:index:v{version}:" + canonical_json({key: value for key, value in envelope.items() if key != "index_hmac"}))

    def _call_binding(self, call_id: str, call: dict) -> str:
        metadata = {key: value for key, value in call.items() if key != "binding_hmac"}
        return self.digest("provenance:call:v1:" + canonical_json([call_id, metadata]))

    def _origin_id(self, digest: str, record: dict) -> str:
        return self.digest("provenance:location:v1:" + canonical_json([digest, record]))

    def _policy_id(self, digest: str, record: dict) -> str:
        return self.digest("provenance:policy:v1:" + canonical_json([digest, record]))

    def _findings_binding(self, values: list[dict]) -> str:
        return self.digest("provenance:findings:v1:" + canonical_json(sorted(values, key=lambda item: item["digest"])))

    def _verify_bindings(self, origins: dict) -> None:
        """Reject structurally valid provenance changed without the build key."""
        observed: dict[str, dict[str, dict]] = {call_id: {} for call_id in origins["calls"]}
        for digest, finding in origins["findings"].items():
            for policy_id, record in finding.get("policy_records", {}).items():
                if not hmac.compare_digest(policy_id, self._policy_id(digest, record)):
                    raise ValueError("隐私指纹或来源检查点无效")
            for origin_id, record in finding["records"].items():
                if not hmac.compare_digest(origin_id, self._origin_id(digest, record)):
                    raise ValueError("隐私指纹或来源检查点无效")
                value = {"digest": digest, "length": record["length"], "type": record["type"]}
                previous = observed[record["call_id"]].setdefault(digest, value)
                if previous != value:
                    raise ValueError("隐私指纹或来源检查点无效")
        for call_id, call in origins["calls"].items():
            if (not hmac.compare_digest(call["binding_hmac"], self._call_binding(call_id, call))
                    or not hmac.compare_digest(call["findings_hmac"], self._findings_binding(list(observed[call_id].values())))):
                raise ValueError("隐私指纹或来源检查点无效")

    def _commit(self, entries: dict, origins: dict) -> None:
        if self._seal_store is not None:
            self._assert_unsealed()
            with self._index_lock:
                self._assert_unsealed()
                self._commit_unlocked(entries, origins)
            return
        self._commit_unlocked(entries, origins)

    def _commit_unlocked(self, entries: dict, origins: dict) -> None:
        envelope = self._envelope(entries, origins)
        envelope["index_hmac"] = self._index_binding(envelope)
        # Validate the whole staged transaction before touching disk or adopting
        # a discovery in this process. One replace commits entries AND origins.
        checked_entries, checked_origins = decode_fingerprint_index(envelope)
        self._verify_bindings(checked_origins)
        self._write(self.index, envelope)
        self.entries, self.origins = checked_entries, checked_origins

    def _write(self, path: Path, data: object) -> None:
        temporary = path.with_name(path.name + "." + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            self._sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        # Linux deployment: persist the rename before a later scan receipt can
        # become durable. Windows has no portable directory-fsync equivalent;
        # atomic replacement there covers process interruption, not power loss.
        if os.name == "posix":
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def remember(self, items: list[dict]) -> None:
        """Persist a legacy discovery with explicitly unknown provenance."""
        items = validate_items(items)
        if not items:
            return
        self._assert_unsealed()
        with self._index_lock:
            self._assert_unsealed()
            self.refresh()
            entries = dict(self.entries)
            origins = {"calls": dict(self.origins["calls"]), "findings": dict(self.origins["findings"])}
            for item in items:
                digest = self.digest("value:" + item["value"])
                entries.setdefault(digest, (len(item["value"]), item["type"]))
                previous = origins["findings"].get(digest, {"records": {}})
                origins["findings"][digest] = {**previous, "legacy_unknown": True, "records": dict(previous["records"])}
            self._commit(entries, origins)

    def remember_policy(self, items: list[dict], records: list[dict], *, kind: str,
                        scope: str, context: dict | None = None, source: dict | None = None) -> None:
        """Bind a local rule/history match without inventing an SDK call.

        Original values and complete records exist only for this invocation.
        The caller supplies records before sanitization, so every finding must
        have a real occurrence. Source snapshots are verified by the Worker.
        """
        items, records = validate_items(items), validate_records(records, 0)
        context = validate_context(context)
        if not items:
            return
        if (type(kind) is not str or kind not in {"inherited_hmac_match", "deterministic_rule"}
                or type(scope) is not str or re.fullmatch(r"[a-f0-9]{64}", scope) is None):
            raise ValueError("隐私指纹或来源检查点无效")
        if kind == "deterministic_rule":
            if source is not None:
                raise ValueError("隐私指纹或来源检查点无效")
            provenance = {"source_build_id": None, "source_commit": None, "source_snapshot": None, "source_finding": None,
                          "detector": "privacy_rules/1"}
        else:
            if type(source) is not dict or set(source) != {"source_build_id", "source_commit", "source_snapshot", "source_finding"}:
                raise ValueError("隐私指纹或来源检查点无效")
            provenance = {**source, "detector": "privacy_history/1"}
        additions = {}
        for item in items:
            value = item["value"]
            digest = self.digest("value:" + value)
            locations = {}
            for record in records:
                first = record["text"].find(value)
                if first < 0:
                    continue
                position, count = first, 0
                while position >= 0:
                    count += 1
                    position = record["text"].find(value, position + 1)
                metadata = {"kind": kind, "context": context, "scope": scope,
                    "record_hmac": self.digest("policy:record:v1:" + canonical_json(record)),
                    "field_hmac": self.digest("provenance:field:v1:" + record["field"]),
                    "field_role": field_role(record["field"]), "record_length": len(record["text"]),
                    "first_offset": first, "occurrence_count": count,
                    "length": len(value), "type": item["type"], **provenance}
                validate_policy_record(metadata, (len(value), item["type"]))
                locations[self._policy_id(digest, metadata)] = metadata
            if not locations:
                raise ValueError("隐私指纹或来源检查点无效")
            additions[digest] = (len(value), item["type"], locations)
        with self._index_lock:
            self._assert_unsealed()
            self.refresh()
            entries = dict(self.entries)
            origins = {"calls": dict(self.origins["calls"]), "findings": dict(self.origins["findings"])}
            for digest, (length, label, locations) in additions.items():
                if digest in entries and entries[digest][0] != length:
                    raise ValueError("隐私指纹或来源检查点无效")
                entries.setdefault(digest, (length, label))
                old = origins["findings"].get(digest, {"legacy_unknown": False, "records": {}})
                origins["findings"][digest] = {**old, "policy_records": {**old.get("policy_records", {}), **locations}}
            if entries != self.entries or origins != self.origins:
                self._commit(entries, origins)

    def remember_scan(self, items: list[dict], records: list[dict], *, call_id: str,
                      scope: str, packet: str, start: int = 0, context: dict | None = None,
                      after_response: bool = False) -> None:
        """Atomically bind findings to every actual transmitted candidate record.

        A matching location does not prove it caused classification. Character
        offsets/counts include overlapping occurrences. No raw value, field name
        or text is retained, even for malformed inputs or persistence failures.
        """
        context = validate_scan_metadata(call_id, scope, packet, start, context, after_response)
        items, records = validate_items(items), validate_records(records, start)
        discoveries, locations = {}, {}
        location_count = 0
        for item in items:
            value, kind = item["value"], item["type"]
            digest = self.digest("value:" + value)
            discoveries[digest] = {"digest": digest, "length": len(value), "type": kind}
            matches = {}
            for index, record in enumerate(records, start):
                text = record["text"]
                first = text.find(value)
                if first == -1:
                    continue
                count, position = 0, first
                while position != -1:
                    count += 1
                    position = text.find(value, position + 1)
                metadata = {"call_id": call_id,
                            "record_hmac": self.digest("provenance:record:v1:" + canonical_json(record)),
                            "field_hmac": self.digest("provenance:field:v1:" + record["field"]),
                            "field_role": field_role(record["field"]), "location_kind": "candidate_occurrence",
                            "record_index": index, "record_length": len(text), "first_offset": first,
                            "occurrence_count": count, "length": len(value), "type": kind}
                matches[self._origin_id(digest, metadata)] = metadata
                location_count += 1
                if location_count > MAX_LOCATIONS:
                    raise ValueError("隐私指纹或来源检查点无效")
            if not matches:
                raise ValueError("隐私指纹或来源检查点无效")
            locations[digest] = matches
        call = {"context": context, "after_response": after_response, "scope": scope, "packet": packet,
                "records_hmac": self.digest("provenance:records:v1:" + canonical_json(records)),
                "findings_hmac": self._findings_binding(list(discoveries.values())),
                "start": start, "record_count": len(records)}
        call["binding_hmac"] = self._call_binding(call_id, call)
        if self._seal_store is not None and self._seal_store.read() is not None:
            if not items:
                return  # No discovery or call journal is appended.
            raise PrivacySealConflict()
        with self._index_lock:
            self._assert_unsealed()
            self.refresh()
            previous = self.origins["calls"].get(call_id)
            if previous is not None and previous != call:
                raise ValueError("隐私指纹或来源检查点无效")
            if not items or previous is not None:
                # Empty discoveries need no duplicate call journal; DB metering
                # already records that call. Existing identities still conflict.
                return
            entries = dict(self.entries)
            origins = {"calls": {**self.origins["calls"], call_id: call}, "findings": dict(self.origins["findings"])}
            for digest, discovery in discoveries.items():
                if digest in entries and entries[digest][0] != discovery["length"]:
                    raise ValueError("隐私指纹或来源检查点无效")
                # Keep first global masking type, but preserve this call's real
                # observed label when another call classifies the value anew.
                entries.setdefault(digest, (discovery["length"], discovery["type"]))
                old = self.origins["findings"].get(digest, {"legacy_unknown": False, "records": {}})
                origins["findings"][digest] = {**old, "legacy_unknown": old["legacy_unknown"],
                                                "records": {**old["records"], **locations[digest]}}
            self._commit(entries, origins)

    def receipt(self, key: str) -> str | None:
        """Read only known completion/split markers, never original inputs."""
        path = self.root / (key + ".json")
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, str) or data not in {"completed", "split"}:
            raise ValueError("隐私扫描检查点损坏")
        return data

    def complete(self, key: str, state: str = "completed") -> None:
        """Atomically mark a whole scanned packet or a known truncated split."""
        if state not in {"completed", "split"} or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("隐私检查点标记无效")
        self._write(self.root / (key + ".json"), state)

    def mask(self, text: str) -> str:
        """Match fingerprints against ephemeral input, with stable placeholders."""
        if not self.entries or not text:
            return text
        lengths = sorted({n for n, _ in self.entries.values()}, reverse=True)
        result = []
        for segment in _PLACEHOLDER.split(text):
            if _PLACEHOLDER.fullmatch(segment):
                result.append(segment)
                continue
            found: dict[int, tuple[int, str, str]] = {}
            for length in lengths:
                for position in range(len(segment) - length + 1):
                    digest = self.digest("value:" + segment[position:position + length])
                    if digest in self.entries and position not in found:
                        found[position] = (length, self.entries[digest][1], digest)
            parts, position = [], 0
            while position < len(segment):
                if position in found:
                    length, kind, digest = found[position]
                    parts.append(f"<<{kind}_{int(digest[:16], 16)}>>")
                    position += length
                else:
                    parts.append(segment[position])
                    position += 1
            result.append("".join(parts))
        return "".join(result)


class LedgerPrivacyFilter(PrivacyFilter):
    """Apply ordinary rules plus worker-private cross-unit discoveries."""

    def __init__(self, ledger: PrivacyLedger) -> None:
        super().__init__(local_llm=None)
        self.ledger = ledger

    def sanitize_text(self, text: str) -> str:
        """Never reconstruct or return the original value of a fingerprint."""
        return self.ledger.mask(super().sanitize_text(text))


def scan_records(value: object, path: tuple[str, ...] = ()) -> list[dict]:
    """Keep entire scalar values and their field context, excluding empty data."""
    result = []
    if isinstance(value, str):
        if value and not _PLACEHOLDER.fullmatch(value):
            # Collection/packet wrappers are mechanical. Keep the semantic
            # parent and leaf so the same field retains context across stages.
            result.append({"field": ".".join(path[-2:]) or "value", "text": value})
    elif isinstance(value, list):
        for item in value:
            result.extend(scan_records(item, path))
    elif isinstance(value, dict):
        for key, item in sorted(value.items()):
            children = scan_records(item, (*path, key))
            if children:
                result.append({"field": "field_name", "text": key})
                result.extend(children)
    # Identical values in the same context only need a single classification.
    unique = {(r["field"], r["text"]): r for r in result}
    return list(unique.values())
