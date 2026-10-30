"""Zero-model legacy compatibility attestation, inside the actual old image.

The operator mounts the current recipes.py as a read-only service source file.
The installed old lifecycle must reproduce the DB's exact execution recipe.
No receipt, call, DB row, budget, or old checkpoint is modified. This is not a
recovery request and never starts a Worker or sends a model/GitLab request.
"""
import argparse
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
from uuid import UUID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--expected-recipe", required=True)
    parser.add_argument("--recipes-module", type=Path, required=True)
    parser.add_argument("--workers-drained", action="store_true", required=True,
                        help="Operator has stopped/drained old Workers; old code cannot send revocation callbacks")
    args = parser.parse_args()
    if (str(UUID(args.build_id)) != args.build_id or len(args.expected_recipe) != 64
            or any(c not in "0123456789abcdef" for c in args.expected_recipe)):
        raise ValueError("legacy_attestation_identity_invalid")
    from src.models import SyncSession
    from src.models.brain import BrainBuild, BrainProject
    from src.core.project_brain.lifecycle import recipe_hash
    from src.core.project_brain.artifacts import ArtifactStore
    from src.core.project_brain.source_catalog import identity_key
    with SyncSession() as session:
        build = session.get(BrainBuild, args.build_id)
        if (build is None or build.status != "failed" or build.recipe_hash != args.expected_recipe
                or recipe_hash() != args.expected_recipe or (build.coverage or {}).get("component_recipes") is not None):
            raise ValueError("legacy_attestation_actual_old_recipe_required")
        project = session.get(BrainProject, build.project_id)
        scope = {"build_id": build.id, "source_commit": build.source_commit, "recipe_hash": build.recipe_hash}
        project_id = project.gitlab_project_id
    if args.recipes_module.is_symlink() or not args.recipes_module.is_file():
        raise ValueError("legacy_attestation_module_invalid")
    spec = importlib.util.spec_from_file_location("_brain_recovery_component_attestor", args.recipes_module)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import src
    body = {**scope, "workers_drained": True, "components": module.component_recipes(source_root=Path(src.__file__).parent)}
    if (type(body["components"]) is not dict or set(body["components"]) != {"scan", "extract", "privacy", "compile", "summary", "render"}
            or any(type(item) is not str or len(item) != 64 or any(c not in "0123456789abcdef" for c in item)
                   for item in body["components"].values())):
        raise ValueError("legacy_attestation_components_invalid")
    store = ArtifactStore()
    key = identity_key(store.work_base, project_id, create=False)
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    binding = hmac.new(key, ("brain-recovery/1:" + canonical).encode(), hashlib.sha256).hexdigest()
    document = {"body": body, "binding_hmac": binding}
    path = store.work_base / "source-recovery-attestations" / (args.build_id + ".json")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("legacy_attestation_path_invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("legacy_attestation_conflict")
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    print(json.dumps({"build_id": args.build_id, "old_recipe_verified": True,
        "components_attested": len(body["components"]), "db_rows_changed": 0,
        "model_calls": 0, "starts_recovery": False}))


if __name__ == "__main__":
    main()
