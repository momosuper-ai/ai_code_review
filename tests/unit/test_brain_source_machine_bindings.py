"""Source provenance keeps machine positions distinct from sensitive display."""

import json

import pytest

from src.core.project_brain.exceptions import SourceBindingError
from src.core.project_brain.schema import FactBundle
from tests.unit.test_brain_source_catalog import catalog_case  # noqa: F401
from tests.unit.test_project_brain import repository, snapshot  # noqa: F401


def _row(bundle, field):
    return next((locator, observation[field][0]) for locator, observation in bundle.observations.items()
                if observation.get(field))


def test_access_identity_equal_to_secret_is_preserved_while_name_is_masked(catalog_case):
    _, catalog, privacy = catalog_case
    source = catalog.bundle()
    locator, access = _row(source, "accesses")
    values = {access[0], *access[3], access[1], access[2], access[4]}
    privacy.ledger.remember([{"value": value, "type": "SECRET"} for value in values])
    safe = catalog.safe_bundle(source, privacy)
    projected = safe.observations[locator]["accesses"][0]
    assert projected[0] == access[0] and projected[3] == access[3]
    assert projected[2] == access[2] and projected[4] == access[4]
    assert projected[1] != access[1] and access[1] not in projected[1]
    catalog.authenticate(safe)
    restored = FactBundle.model_validate(json.loads(json.dumps(safe.model_dump(), sort_keys=True)), strict=True)
    catalog.authenticate_checkpoint(restored, catalog.checkpoint_mac(safe))


@pytest.mark.parametrize("attribute", ["language", "symbol_kind", "parameter_count", "is_static"])
def test_every_preserved_fact_machine_attribute_is_bound(catalog_case, attribute):
    _, catalog, _ = catalog_case
    source = catalog.bundle()
    if attribute in {"language", "symbol_kind"}:
        selected = next(fact for fact in source.facts if attribute in fact.attributes)
        previous = selected.attributes[attribute]
        candidates = {"language": ["python", "java"], "symbol_kind": ["function", "method"]}[attribute]
        selected.attributes[attribute] = next(value for value in candidates if value != previous)
    else:
        selected = next((fact for fact in source.facts if attribute in fact.attributes), source.facts[0])
        previous = selected.attributes.get(attribute, 0)
        selected.attributes[attribute] = not previous if attribute == "is_static" else previous + 1
    with pytest.raises(SourceBindingError):
        catalog.authenticate(source)
    with pytest.raises(SourceBindingError):
        catalog.checkpoint_mac(source)


@pytest.mark.parametrize("change", ["call_path", "call_owner", "call_ref", "route_path", "route_owner", "route_line",
                                    "access_owner", "access_ref", "access_type", "access_mode", "mirror_id", "duplicate_call"])
def test_observation_machine_positions_are_authenticated(catalog_case, change):
    _, catalog, privacy = catalog_case
    source = catalog.bundle()
    if change.startswith("call_") or change == "duplicate_call":
        locator, row = _row(source, "calls")
        if change == "duplicate_call":
            source.observations[locator]["calls"].append(list(row))
        else:
            row[{"call_path": 0, "call_owner": 1, "call_ref": 3}[change]] = "not-the-bound-machine-id"
    elif change.startswith("route_"):
        _, row = _row(source, "routes")
        field = {"route_path": "path", "route_owner": "owner", "route_line": "line"}[change]
        row[field] = row[field] + 1 if field == "line" else "not-the-bound-machine-id"
    elif change.startswith("access_"):
        _, row = _row(source, "accesses")
        if change == "access_ref":
            row[3] = ["not-the-bound-machine-id"]
        else:
            index = {"access_owner": 0, "access_type": 2, "access_mode": 4}[change]
            row[index] = {0: "not-the-bound-machine-id", 2: "WRITES_TABLE" if row[2] == "READS_TABLE" else "READS_TABLE",
                          4: "table" if row[4] == "model" else "model"}[index]
    else:
        _, row = _row(source, "facts")
        row["id"] = "not-the-bound-machine-id"
    with pytest.raises(SourceBindingError):
        catalog.safe_bundle(source, privacy)


@pytest.mark.parametrize("field", ["calls", "routes", "accesses", "facts"])
def test_malformed_observation_identity_has_a_closed_error(catalog_case, field):
    _, catalog, _ = catalog_case
    source = catalog.bundle()
    locator = next(iter(source.observations))
    source.observations[locator][field] = "untrusted-shape"
    with pytest.raises(SourceBindingError, match="^source_binding_invalid$"):
        catalog.authenticate(source)
