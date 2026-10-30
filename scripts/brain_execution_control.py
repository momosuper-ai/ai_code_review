"""Operate one scoped Brain build from a trusted Worker environment.

No model, GitLab, power-policy or source I/O is performed by this command.
Resume can dispatch the same durable pending units after its transaction.
"""

import argparse
import json
from uuid import UUID

from sqlalchemy import func, select

from src.config import settings
from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.core.project_brain.execution import ExecutionConflict, pause_build, resume_build
from src.models import SyncSession
from src.models.brain import BrainBuild, BrainModelCall, BrainProject, BrainUnit


def _utc(value):
    return value.isoformat(timespec="microseconds") + "Z" if value is not None else None


def main() -> int:
    """Commit a scoped control request and print only operational metadata."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "pause", "resume"))
    parser.add_argument("build_id", type=lambda value: str(UUID(value)))
    parser.add_argument("--project-id", type=int, required=True, help="GitLab project ID, not the internal DB ID")
    args = parser.parse_args()
    dispatch = False
    try:
        with SyncSession() as session:
            build = session.scalar(select(BrainBuild).join(BrainProject).where(
                BrainBuild.id == args.build_id, BrainProject.gitlab_project_id == args.project_id))
            if build is None:
                raise ExecutionConflict("build_not_found")
            if args.action == "pause":
                build = pause_build(session, build.id)
            elif args.action == "resume":
                if not settings.PROJECT_BRAIN_ENABLED:
                    raise ExecutionConflict("brain_disabled")
                session.scalar(select(BrainProject).where(BrainProject.id == build.project_id).with_for_update())
                build = session.scalar(select(BrainBuild).where(BrainBuild.id == args.build_id)
                    .with_for_update().execution_options(populate_existing=True))
                was_paused = build.execution_state == "paused"
                build = resume_build(session, build.id)
                dispatch = was_paused and build.execution_state == "running"
            session.commit()
        delivery = "not_requested"
        if dispatch:
            from src.tasks.brain_task import dispatch_pending

            try:
                dispatch_pending(args.build_id)
                delivery = "dispatched"
            except Exception:
                delivery = "pending_beat"
        with SyncSession() as session:
            build = session.get(BrainBuild, args.build_id)
            units = session.execute(select(BrainUnit.kind, BrainUnit.status, func.count()).where(
                BrainUnit.build_id == build.id).group_by(BrainUnit.kind, BrainUnit.status)
                .order_by(BrainUnit.kind, BrainUnit.status)).all()
            calls = session.execute(select(BrainModelCall.state, func.count()).where(
                BrainModelCall.build_id == build.id).group_by(BrainModelCall.state)).all()
            result = {"build_id": build.id, "status": build.status, "execution_state": build.execution_state,
                "pause_requested_at": _utc(build.pause_requested_at), "paused_at": _utc(build.paused_at),
                "resumed_at": _utc(build.resumed_at), "delivery": delivery,
                "units": [{"stage": stage, "state": state, "count": count} for stage, state, count in units],
                "model_calls": {state: count for state, count in calls}}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ExecutionConflict as error:
        print(json.dumps({"error": error.reason}))
        return 2
    except ModelCallsIncomplete:
        print(json.dumps({"error": "model_calls_incomplete"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
