import asyncio
import time

import pytest
from sqlalchemy import select

from src.core.project_brain import lifecycle, timing
from src.core.project_brain.lifecycle import claim_unit
from src.core.project_brain.timing import SEGMENT_NAMES
from src.models.brain import BrainUnit
from src.tasks.brain_task import _complete
from tests.unit.test_project_brain import database  # noqa: F401


def test_completed_unit_persists_fixed_numeric_segments(database):
    with database() as session:
        build = lifecycle.create_build(session, 1, "main", "synthetic/timing", "a" * 40, historical=True)
        session.commit()
        unit_id = session.scalar(select(BrainUnit.id).where(BrainUnit.build_id == build.id))
    claimed = claim_unit(unit_id)
    assert claimed is not None
    timing.start()
    with timing.segment("bundle"):
        pass

    assert _complete(unit_id, claimed[3], {"fingerprint": "synthetic"}) is True
    with database() as session:
        segments = session.get(BrainUnit, unit_id).diagnostic["segments"]
    assert set(segments) == set(SEGMENT_NAMES)
    assert segments["bundle"]["count"] == 1
    assert all(set(value) == {"seconds", "count"}
               and type(value["seconds"]) is float and type(value["count"]) is int
               for value in segments.values())


@pytest.mark.asyncio
async def test_context_isolation_and_to_thread_accumulation():
    async def measure(duration):
        timing.start()

        def work():
            with timing.segment("bundle"):
                time.sleep(duration)

        await asyncio.to_thread(work)
        return timing.snapshot()

    first, second = await asyncio.gather(measure(0.01), measure(0.03))
    assert first["bundle"]["count"] == second["bundle"]["count"] == 1
    assert first["bundle"]["seconds"] < second["bundle"]["seconds"]
    for snapshot in (first, second):
        assert set(snapshot) == set(SEGMENT_NAMES)
        assert all(type(item["seconds"]) is float and type(item["count"]) is int
                   for item in snapshot.values())
