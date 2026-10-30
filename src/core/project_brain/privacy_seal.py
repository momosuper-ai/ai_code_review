"""Worker-private, keyed privacy seals independent of disposable work data.

No source text, value, path lookup or key copy belongs in this store. The caller
serializes every transition with the privacy index lock. A durable closed marker
is written before its seal; a process crash in between is explicitly fail-closed.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from src.core.project_brain.exceptions import PrivacySealConflict
from src.core.project_brain.privacy_provenance import canonical_json

_HEX = re.compile(r"[a-f0-9]{64}\Z")
_MAX_BYTES = 4096


def _hex(value: object) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


class PrivacySealStore:
    """Authenticate an open/closed anchor and a single immutable content seal."""

    def __init__(self, directory: Path, key: bytes) -> None:
        if not isinstance(directory, Path) or type(key) is not bytes or len(key) != 32:
            raise PrivacySealConflict()
        self.root, self.key = directory, key
        self.state_path, self.path = directory / "state.json", directory / "seal.json"

    def _binding(self, prefix: str, value: dict) -> str:
        body = {name: item for name, item in value.items() if name != "binding_hmac"}
        return hmac.new(self.key, (prefix + canonical_json(body)).encode(), hashlib.sha256).hexdigest()

    def _state(self, sealed: dict | None) -> dict:
        state = {"version": 1, "status": "sealed" if sealed else "open",
                 "seal_binding_hmac": sealed["binding_hmac"] if sealed else None}
        state["binding_hmac"] = self._binding("privacy:seal:state:v1:", state)
        return state

    def _read_json(self, path: Path) -> dict:
        def unique(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise PrivacySealConflict()
                result[name] = value
            return result

        try:
            if self.root.is_symlink() or not self.root.is_dir() or path.is_symlink():
                raise PrivacySealConflict()
            with path.open("rb") as stream:
                data = stream.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise PrivacySealConflict()
            value = json.loads(data, object_pairs_hook=unique)
            if type(value) is not dict:
                raise PrivacySealConflict()
            return value
        except (OSError, ValueError, TypeError, RecursionError):
            raise PrivacySealConflict() from None

    def _checked_seal(self) -> dict:
        return self._check_seal_value(self._read_json(self.path))

    def _check_seal_value(self, seal: dict) -> dict:
        if type(seal) is not dict:
            raise PrivacySealConflict()
        if (set(seal) != {"version", "revision", "content_digest", "binding_hmac"}
                or type(seal["version"]) is not int or seal["version"] != 1
                or not all(_hex(seal[field]) for field in ("revision", "content_digest", "binding_hmac"))
                or not hmac.compare_digest(seal["binding_hmac"], self._binding("privacy:seal:content:v1:", seal))):
            raise PrivacySealConflict()
        return seal

    def read(self) -> dict | None:
        """Missing or malformed closed history is never interpreted as open."""
        state = self._read_json(self.state_path)
        seal = self._read_json(self.path) if self.path.exists() or self.path.is_symlink() else None
        return self.authenticate_documents(state, seal)

    def authenticate_documents(self, state: dict, seal: dict | None) -> dict | None:
        """Check exact in-memory snapshot bytes without reopening source files."""
        if (type(state) is not dict or set(state) != {"version", "status", "seal_binding_hmac", "binding_hmac"}
                or type(state["version"]) is not int or state["version"] != 1
                or type(state["status"]) is not str or state["status"] not in {"open", "sealed"}
                or not _hex(state["binding_hmac"])
                or not hmac.compare_digest(state["binding_hmac"], self._binding("privacy:seal:state:v1:", state))):
            raise PrivacySealConflict()
        if state["status"] == "open":
            if state["seal_binding_hmac"] is not None or seal is not None:
                raise PrivacySealConflict()
            return None
        if not _hex(state["seal_binding_hmac"]):
            raise PrivacySealConflict()
        seal = self._check_seal_value(seal)
        if not hmac.compare_digest(state["seal_binding_hmac"], seal["binding_hmac"]):
            raise PrivacySealConflict()
        return seal

    def initialize(self, *, new_key: bool) -> None:
        """Only the transaction creating a new build key may establish open."""
        if self.state_path.exists() or self.state_path.is_symlink():
            self.read()
            return
        if not new_key:
            raise PrivacySealConflict()
        try:
            if self.root.is_symlink() or (self.root.exists() and any(self.root.iterdir())):
                raise PrivacySealConflict()
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            self._sync_directory(self.root.parent)
            self._write(self.state_path, self._state(None))
        except OSError:
            raise PrivacySealConflict() from None

    def seal(self, revision: str, content_digest: str) -> dict:
        if not _hex(revision) or not _hex(content_digest):
            raise PrivacySealConflict()
        prior = self.read()
        if prior is not None:
            if (hmac.compare_digest(prior["revision"], revision)
                    and hmac.compare_digest(prior["content_digest"], content_digest)):
                return prior
            raise PrivacySealConflict()
        seal = {"version": 1, "revision": revision, "content_digest": content_digest}
        seal["binding_hmac"] = self._binding("privacy:seal:content:v1:", seal)
        # A crash between these two atomic replacements leaves a closed marker
        # without a valid seal. Recovery must quarantine it, never reopen it.
        self._write(self.state_path, self._state(seal))
        self._write(self.path, seal)
        return seal

    def _write(self, path: Path, data: dict) -> None:
        temporary = path.with_name(path.name + "." + secrets.token_hex(8))
        try:
            if self.root.is_symlink() or not self.root.is_dir() or path.is_symlink():
                raise PrivacySealConflict()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(canonical_json(data))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            self._sync_directory(self.root)
        except OSError:
            raise PrivacySealConflict() from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "posix":
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
