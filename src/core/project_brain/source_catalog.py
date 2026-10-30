"""Worker-only canonical source authentication and stable opaque locators.

Real Git paths and reverse lookup tables live only in this in-memory object.
Its project identity key is stored outside disposable build work directories.
Public readers never receive this object or have access to the identity key.
"""

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path, PureWindowsPath
import secrets

from filelock import FileLock

from src.core.project_brain.exceptions import PrivacyCheckpointMissing, SourceBindingError
from src.core.project_brain.schema import FactBundle
from src.core.project_brain.snapshot import RepoSnapshot, safe_relative

IDENTITY_VERSION = "opaque-v1"
IDENTITY_KEY_VERSION = 1


def _same_namespace(path):
    """Compare equivalent Windows API spellings without weakening containment.

    Concurrent creation can make Path.resolve() return an extended drive/UNC
    spelling for a child and the ordinary spelling for its existing parent.
    Other device namespaces and different drives/shares remain distinct.
    """
    if isinstance(path, PureWindowsPath):
        value = str(path)
        if value.startswith("\\\\?\\UNC\\"):
            return PureWindowsPath("\\\\" + value[8:])
        if (value.startswith("\\\\?\\") and len(value) >= 7
                and value[4].isalpha() and value[5:7] == ":\\"):
            return PureWindowsPath(value[4:])
    return path


def _within_resolved(path, root) -> bool:
    return _same_namespace(path).is_relative_to(_same_namespace(root))


def _encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sync_parent(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def identity_key(work_base: Path, project_id: int, *, create: bool) -> bytes:
    """Load a stable project key; only initial extraction may create it."""
    if type(project_id) is not int or project_id <= 0 or type(create) is not bool:
        raise ValueError("source_identity_scope_invalid")
    base = work_base.resolve()
    directory = base / "source-identities" / str(project_id) / str(IDENTITY_KEY_VERSION)
    if not _within_resolved(directory.resolve(), base):
        raise PrivacyCheckpointMissing()
    if any(part.is_symlink() for part in [directory, *directory.parents] if part != base.parent):
        raise PrivacyCheckpointMissing()
    key_file = directory / "identity.key"
    anchor_file = directory / "identity.json"
    if create:
        # Serialize the initial directory test too: two first builds must not
        # mistake the other creator's empty, not-yet-written directory for loss.
        directory.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(directory.parent / "identity-create.lock"), timeout=10):
            fresh_directory = not directory.exists()
            directory.mkdir(mode=0o700, exist_ok=True)
            if key_file.is_symlink() or anchor_file.is_symlink():
                raise PrivacyCheckpointMissing()
            if not key_file.exists():
                if not fresh_directory or anchor_file.exists():
                    raise PrivacyCheckpointMissing()
                descriptor = os.open(key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    new_key = secrets.token_bytes(32)
                    stream.write(new_key)
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_parent(directory)
                anchor = {"version": IDENTITY_KEY_VERSION, "project_id": project_id,
                          "key_digest": hashlib.sha256(new_key).hexdigest()}
                descriptor = os.open(anchor_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(_encoded(anchor))
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_parent(directory)
    try:
        if key_file.is_symlink() or anchor_file.is_symlink():
            raise PrivacyCheckpointMissing()
        with key_file.open("rb") as stream:
            key = stream.read(33)
        with anchor_file.open("rb") as stream:
            data = stream.read(1025)
        if len(data) > 1024:
            raise PrivacyCheckpointMissing()
        def unique(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError()
                result[name] = value
            return result
        anchor = json.loads(data, object_pairs_hook=unique)
    except (OSError, ValueError, TypeError):
        raise PrivacyCheckpointMissing() from None
    if (len(key) != 32 or type(anchor) is not dict or _encoded(anchor) != _encoded({
            "version": IDENTITY_KEY_VERSION, "project_id": project_id,
            "key_digest": hashlib.sha256(key).hexdigest()})):
        raise PrivacyCheckpointMissing()
    return key


@dataclass(frozen=True)
class CatalogSnapshot:
    """A path-compatible snapshot adapter whose locators cannot name a file."""

    path: str
    commit_sha: str
    files: dict
    issues: list[str]
    fingerprint: str
    _paths: dict[str, str] = field(repr=False, compare=False)

    def resolve_source(self, locator: str) -> Path:
        if locator not in self.files or locator not in self._paths:
            raise ValueError("source_locator_invalid")
        relative = safe_relative(self._paths[locator])
        root = Path(self.path).resolve()
        result = root / relative
        if (result.is_symlink() or not _within_resolved(result.resolve(), root)
                or any(parent.is_symlink() for parent in result.parents if parent != root.parent)):
            raise ValueError("source_locator_invalid")
        return result


def _observation_machine_shape(observation: dict) -> dict:
    """Bind parser-owned coordinates while allowing only source text to change."""
    if type(observation) is not dict:
        raise SourceBindingError()
    result = {}
    if "module" in observation:
        result["module"] = observation["module"]
    for field in ("facts", "refs", "relations"):
        if field in observation:
            values = observation[field]
            if type(values) is not list or any(type(value) is not dict or "id" not in value for value in values):
                raise SourceBindingError()
            result[field] = [value["id"] for value in values]
    if "calls" in observation:
        values = observation["calls"]
        if type(values) is not list or any(not isinstance(value, (list, tuple)) or len(value) != 4 for value in values):
            raise SourceBindingError()
        result["calls"] = [[value[0], value[1], value[3]] for value in values]
    if "routes" in observation:
        values = observation["routes"]
        if type(values) is not list or any(type(value) is not dict for value in values):
            raise SourceBindingError()
        result["routes"] = [{key: value[key] for key in ("owner", "path", "line") if key in value} for value in values]
    if "accesses" in observation:
        values = observation["accesses"]
        if type(values) is not list or any(not isinstance(value, (list, tuple)) or len(value) != 5
                                         or not isinstance(value[3], (list, tuple)) for value in values):
            raise SourceBindingError()
        result["accesses"] = [{"fact_id": value[0], "source_ref_ids": list(value[3]),
            **({"type": value[2]} if type(value[2]) is str and value[2] in {"READS_TABLE", "WRITES_TABLE"} else {}),
            **({"mode": value[4]} if type(value[4]) is str and value[4] in {"model", "table"} else {})}
            for value in values]
    return result


def _machine_shape(bundle: FactBundle) -> dict:
    """Only service-owned, fully authenticated source identities belong here."""
    fact_fields = ("id", "kind", "module_id", "source_ref_ids", "extractor")
    relation_fields = ("id", "from_id", "type", "to_id", "resolution", "knowledge_type", "source_ref_ids", "candidates")
    ref_fields = ("id", "commit_sha", "path", "blob_sha", "line_start", "line_end", "symbol_id")
    from src.core.project_brain.compiler import machine_attribute
    return {
        "source_commit": bundle.source_commit, "source_fingerprint": bundle.source_fingerprint,
        "files": bundle.files, "modules": bundle.modules,
        "facts": [{**{k: getattr(f, k) for k in fact_fields},
                   "source_attributes": {k: value for k, value in f.attributes.items()
                                         if k in {"path", "call_arities"} or machine_attribute(f.kind, k, value)}}
                  for f in bundle.facts],
        "relations": [{k: getattr(r, k) for k in relation_fields} for r in bundle.relations],
        "source_refs": [{k: getattr(r, k) for k in ref_fields} for r in bundle.source_refs],
        "observations": {locator: _observation_machine_shape(observation)
                         for locator, observation in bundle.observations.items()},
    }


class SourceCatalog:
    """Authenticate canonical Git evidence while projecting only source text."""

    def __init__(self, snapshot: RepoSnapshot, raw: FactBundle, key: bytes, project_id: int, branch: str):
        if type(key) is not bytes or len(key) != 32 or type(project_id) is not int or project_id <= 0:
            raise ValueError("source_identity_scope_invalid")
        from src.core.project_brain.artifacts import validate
        validate(raw, [], snapshot)
        self._key, self._raw_snapshot = key, snapshot
        self.paths: dict[str, str] = {}
        raw_paths = set(raw.files) | set(raw.modules.values()) | set(raw.observations)
        raw_paths.update(ref.path for ref in raw.source_refs)
        self._locators = {}
        for path in sorted(raw_paths):
            locator = self._locator(path)
            if locator in self.paths and self.paths[locator] != path:
                raise ValueError("source_identity_collision")
            self.paths[locator] = path
            self._locators[path] = locator
        ids = {}
        for domain, values in (("fact", (f.id for f in raw.facts)), ("module", raw.modules),
                               ("relation", (r.id for r in raw.relations)), ("evidence", (r.id for r in raw.source_refs))):
            for value in values:
                if value in ids:
                    raise ValueError("source_identity_collision")
                ids[value] = self.opaque(domain, value)
        self._ids = ids

        value = raw.model_dump()
        value["files"] = {self._locators[p]: dict(meta) for p, meta in raw.files.items()}
        value["modules"] = {ids[k]: self._locators[p] for k, p in raw.modules.items()}
        # Translate only declared machine positions. A language, import name or
        # ordinary attribute can equal a real path/ID without denoting it.
        observations = {}
        for path, observation in value["observations"].items():
            observation["module"] = ids[observation["module"]]
            observation["calls"] = [[self._locators[p], ids[fid], name, ids[ref]]
                                    for p, fid, name, ref in observation.get("calls", [])]
            observation["routes"] = [{**route, "path": self._locators[route["path"]],
                                       "owner": ids[route["owner"]]}
                                      for route in observation.get("routes", [])]
            observation["accesses"] = [[ids[fid], name, kind, [ids[ref] for ref in refs], mode]
                                       for fid, name, kind, refs, mode in observation.get("accesses", [])]
            observations[self._locators[path]] = observation
        value["observations"] = observations
        for before, after in zip(raw.facts, value["facts"], strict=True):
            attributes = after["attributes"]
            if "path" in attributes:
                attributes["path"] = self._locators[attributes["path"]]
            if "call_arities" in attributes:
                attributes["call_arities"] = {ids[rid]: count for rid, count in attributes["call_arities"].items()}
            after.update(id=ids[before.id], module_id=ids[before.module_id],
                         source_ref_ids=[ids[rid] for rid in before.source_ref_ids],
                         attributes=attributes)
        for before, after in zip(raw.relations, value["relations"], strict=True):
            after.update(id=ids[before.id], from_id=ids[before.from_id],
                         to_id=ids[before.to_id] if before.to_id else None,
                         candidates=[ids[fid] for fid in before.candidates],
                         source_ref_ids=[ids[rid] for rid in before.source_ref_ids])
        value["display_paths"] = dict(self.paths)
        for before, after in zip(raw.source_refs, value["source_refs"], strict=True):
            after.update(id=ids[before.id], path=self._locators[before.path],
                         symbol_id=ids[before.symbol_id] if before.symbol_id else None,
                         display_path=before.path)
        self.identity = {"version": IDENTITY_VERSION, "key_version": IDENTITY_KEY_VERSION,
                         "project_id": project_id, "canonical_binding": self.mac("canonical", raw.model_dump()),
                         "branch_id": self.opaque("branch", branch)}
        value["source_identity"] = dict(self.identity)
        self._base = FactBundle.model_validate(value)
        mirrors = {"facts": {f.id: f.model_dump() for f in self._base.facts},
                   "refs": {r.id: r.model_dump() for r in self._base.source_refs},
                   "relations": {r.id: r.model_dump() for r in self._base.relations}}
        for observation in self._base.observations.values():
            for collection, records in mirrors.items():
                if collection in observation:
                    observation[collection] = [dict(records[ids[item["id"]]])
                                               for item in observation[collection]]
        self._shape = _machine_shape(self._base)
        self.snapshot = CatalogSnapshot(snapshot.path, snapshot.commit_sha, self._base.files,
                                        self._base.issues, snapshot.fingerprint, dict(self.paths))

    def mac(self, domain: str, value) -> str:
        return hmac.new(self._key, _encoded([domain, value]).encode(), hashlib.sha256).hexdigest()

    def opaque(self, domain: str, value: str) -> str:
        return domain + ":" + self.mac("id:" + domain, value)

    def _locator(self, path: str) -> str:
        if path == ".":
            return "sources"
        safe_relative(path)
        parts = path.split("/")
        return "sources/" + "/".join("s" + self.mac("path", "/".join(parts[:i])) for i in range(1, len(parts) + 1))

    def bundle(self) -> FactBundle:
        return self._base.model_copy(deep=True)

    def authenticate(self, bundle: FactBundle) -> None:
        if (_encoded(bundle.source_identity) != _encoded(self.identity)
                or _encoded(_machine_shape(bundle)) != _encoded(self._shape)
                or set(bundle.display_paths) != set(self.paths)):
            raise SourceBindingError()

    def checkpoint_mac(self, bundle: FactBundle) -> str:
        self.authenticate(bundle)
        return self.mac("safe-checkpoint", bundle.model_dump())

    def authenticate_checkpoint(self, bundle: FactBundle, signature: str) -> None:
        if type(signature) is not str or not hmac.compare_digest(signature, self.checkpoint_mac(bundle)):
            raise SourceBindingError()

    def safe_bundle(self, bundle: FactBundle, privacy) -> FactBundle:
        """Keep machine identities exact and redact every source-derived field."""
        self.authenticate(bundle)
        from src.core.project_brain.compiler import machine_attribute, source_text_tree
        clean = privacy.sanitize_text
        def text(value):
            if isinstance(value, str):
                return clean(value)
            if isinstance(value, list):
                return [text(v) for v in value]
            if isinstance(value, dict):
                keys = {k: clean(k) for k in value}
                counts = {}
                for k in keys.values():
                    counts[k] = counts.get(k, 0) + 1
                result = {}
                for k, v in value.items():
                    new_key = keys[k] if counts[keys[k]] == 1 else "attribute_" + self.mac("attribute-key", k)
                    if new_key in result:
                        raise ValueError("source_projection_key_collision")
                    result[new_key] = text(v)
                return result
            return source_text_tree(value, privacy)
        safe = bundle.model_copy(deep=True)
        safe.display_paths = {locator: clean(path) for locator, path in self.paths.items()}
        for ref in safe.source_refs:
            ref.display_path = safe.display_paths[ref.path]
        for fact in safe.facts:
            machine = {k: value for k, value in fact.attributes.items()
                       if k in {"path", "call_arities"} or machine_attribute(fact.kind, k, value)}
            if "path" in machine and (type(machine["path"]) is not str or machine["path"] not in self.paths):
                raise ValueError("source_attribute_binding_invalid")
            if "call_arities" in machine:
                arities = machine["call_arities"]
                ref_ids = {ref.id for ref in safe.source_refs}
                if (not isinstance(arities, dict) or any(type(rid) is not str or rid not in ref_ids
                        or type(count) is not int or count < 0 for rid, count in arities.items())):
                    raise ValueError("source_attribute_binding_invalid")
            other = {k: v for k, v in fact.attributes.items() if k not in machine}
            projected = text(other)
            if set(machine) & set(projected):
                raise ValueError("source_projection_key_collision")
            fact.name, fact.attributes = clean(fact.name), {**projected, **machine}
        for relation in safe.relations:
            relation.target_name = clean(relation.target_name)
        safe.issues, safe.frameworks = text(safe.issues), text(safe.frameworks)
        facts = {fact.id: fact.model_dump() for fact in safe.facts}
        refs = {ref.id: ref.model_dump() for ref in safe.source_refs}
        relations = {relation.id: relation.model_dump() for relation in safe.relations}
        observations = {}
        for locator, observation in safe.observations.items():
            machine_fields = {"module", "facts", "refs", "relations", "calls", "routes", "accesses"}
            projected = text({k: v for k, v in observation.items() if k not in machine_fields})
            if machine_fields & set(projected):
                raise ValueError("source_projection_key_collision")
            if "module" in observation:
                projected["module"] = observation["module"]
            for field, index in (("facts", facts), ("refs", refs), ("relations", relations)):
                if field in observation:
                    values = observation[field]
                    if (not isinstance(values, list) or any(not isinstance(entry, dict) or entry.get("id") not in index for entry in values)):
                        raise ValueError("source_observation_binding_invalid")
                    projected[field] = [dict(index[entry["id"]]) for entry in values]
            if "calls" in observation:
                calls = []
                for call in observation["calls"]:
                    if not isinstance(call, (list, tuple)) or len(call) != 4:
                        raise ValueError("source_observation_binding_invalid")
                    calls.append([call[0], call[1], text(call[2]), call[3]])
                projected["calls"] = calls
            if "routes" in observation:
                routes = []
                for route in observation["routes"]:
                    if not isinstance(route, dict):
                        raise ValueError("source_observation_binding_invalid")
                    machine = {k: route[k] for k in ("owner", "path", "line") if k in route}
                    value = text({k: v for k, v in route.items() if k not in machine})
                    if set(machine) & set(value):
                        raise ValueError("source_projection_key_collision")
                    routes.append({**value, **machine})
                projected["routes"] = routes
            if "accesses" in observation:
                accesses = []
                for access in observation["accesses"]:
                    if (not isinstance(access, (list, tuple)) or len(access) != 5
                            or not isinstance(access[3], (list, tuple))):
                        raise SourceBindingError()
                    kind = access[2] if type(access[2]) is str and access[2] in {"READS_TABLE", "WRITES_TABLE"} else text(access[2])
                    mode = access[4] if type(access[4]) is str and access[4] in {"model", "table"} else text(access[4])
                    accesses.append([access[0], text(access[1]), kind, list(access[3]), mode])
                projected["accesses"] = accesses
            observations[locator] = projected
        safe.observations = observations
        self.authenticate(safe)
        return safe

    def project_packet(self, packet: dict, privacy) -> dict:
        from src.core.project_brain.compiler import project_packet_texts
        # Packet ownership is checked by VerifiedPacket/check_summary_task;
        # this hook binds display locators to the catalog, never the model.
        return project_packet_texts(packet, privacy, self.paths)
