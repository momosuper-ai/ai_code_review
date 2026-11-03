"""Read-only per-build Project Brain timing summary."""

import argparse
import json
import math
import os

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from src.core.project_brain.timing import SEGMENT_NAMES
from src.models.brain import BrainBuild, BrainUnit


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    value = ordered[lower] if lower == upper else ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 3)


def _statistics(values: list[float], invocation_count: int) -> dict:
    return {
        "count": len(values),
        "sum_seconds": round(sum(values), 3),
        "p50_seconds": _percentile(values, 0.50),
        "p95_seconds": _percentile(values, 0.95),
        "invocations": invocation_count,
    }


def report(build_id: str, database_url: str) -> dict:
    url = make_url(database_url)
    if url.drivername == "postgresql+asyncpg":
        url = url.set(drivername="postgresql+psycopg2")
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            build = session.scalar(select(BrainBuild).where(BrainBuild.id == build_id))
            if build is None:
                raise ValueError("build_not_found")
            units = session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id)).all()
            grouped: dict[str, dict] = {}
            for unit in units:
                kind = grouped.setdefault(unit.kind, {"segments": {}, "invocations": {}})
                diagnostic = unit.diagnostic if isinstance(unit.diagnostic, dict) else {}
                segment_data = diagnostic.get("segments", {})
                if not isinstance(segment_data, dict):
                    continue
                for name in SEGMENT_NAMES:
                    item = segment_data.get(name)
                    if not isinstance(item, dict):
                        continue
                    seconds = item.get("seconds")
                    count = item.get("count")
                    if (type(seconds) not in {int, float} or not math.isfinite(seconds)
                            or type(count) is not int or count < 0):
                        continue
                    kind["segments"].setdefault(name, []).append(float(seconds))
                    kind["invocations"][name] = kind["invocations"].get(name, 0) + count
            by_kind = {}
            total_execute = 0.0
            total_model = 0.0
            for kind_name, values in sorted(grouped.items()):
                segments = values["segments"]
                invocations = values["invocations"]
                by_kind[kind_name] = {
                    "unit_count": sum(1 for unit in units if unit.kind == kind_name),
                    "segments": {
                        name: _statistics(samples, invocations.get(name, 0))
                        for name, samples in sorted(segments.items())
                    },
                }
                wall = sum(segments.get("execute_total", []))
                model = sum(segments.get("model_call", []))
                total_execute += wall
                total_model += model
                by_kind[kind_name]["model_call_coverage"] = {
                    "model_call_seconds": round(model, 3),
                    "execute_total_seconds": round(wall, 3),
                    "ratio": round(model / wall, 4) if wall else None,
                }
            wall_seconds = None
            if build.completed_at is not None and build.created_at is not None:
                wall_seconds = max(0.0, (build.completed_at - build.created_at).total_seconds())
            return {
                "build_id": build_id,
                "unit_count": len(units),
                "by_kind": by_kind,
                "model_call_coverage": {
                    "model_call_seconds": round(total_model, 3),
                    "execute_total_seconds": round(total_execute, 3),
                    "build_wall_seconds": round(wall_seconds, 3) if wall_seconds is not None else None,
                    "ratio_of_execute_total": round(total_model / total_execute, 4) if total_execute else None,
                    "ratio_of_build_wall": round(total_model / wall_seconds, 4) if wall_seconds else None,
                },
            }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    print(json.dumps(report(args.build_id, database_url), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
