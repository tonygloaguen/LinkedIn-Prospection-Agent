"""log_action node: terminal node that persists run metrics and logs summary."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import structlog

from agent.state import LinkedInProspectionState

logger = structlog.get_logger(__name__)


async def log_action(
    state: LinkedInProspectionState,
    db: object,
) -> LinkedInProspectionState:
    """Persist run metrics to run_history and log a structured summary.

    This is the terminal node of the pipeline. It finalises the run_metrics
    timestamps and saves the full run record to the database.

    Args:
        state: Final pipeline state with all run data.
        db: Active aiosqlite database connection.

    Returns:
        Updated state with finalised run_metrics.
    """
    from storage.queries import save_run_history

    end_time = datetime.now(UTC).isoformat()
    metrics = dict(state["run_metrics"])
    metrics["end_time"] = end_time
    metrics["errors_count"] = len(state["errors"])

    run_id = str(uuid.uuid4())

    try:
        await save_run_history(
            db,  # type: ignore[arg-type]
            run_id=run_id,
            started_at=str(metrics["start_time"]),
            ended_at=end_time,
            metrics=metrics,
        )
    except Exception as exc:
        logger.error("run_history_save_failed", error=str(exc))

    logger.info(
        "run_complete",
        run_id=run_id,
        posts_found=metrics.get("posts_found", 0),
        profiles_extracted=metrics.get("profiles_extracted", 0),
        profiles_scored=metrics.get("profiles_scored", 0),
        invitations_sent=metrics.get("invitations_sent", 0),
        errors_count=metrics["errors_count"],
        duration_s=_compute_duration(str(metrics["start_time"]), end_time),
    )

    if state["errors"]:
        logger.warning("run_errors", count=len(state["errors"]), errors=state["errors"][:5])

    # ── RTK post-run digest ────────────────────────────────────────────────────
    # Generates /logs/digest_<run_id_short>.txt with metrics + RTK-filtered logs.
    # Runs only when RTK_ENABLED is not "false" (default: enabled).
    # Never raises — failures are logged as warnings and ignored.
    try:
        from utils.diagnose import generate_run_digest

        generate_run_digest(
            run_id=run_id,
            log_file=os.environ.get("LOG_FILE"),
            metrics=metrics,
            errors=state["errors"],
        )
    except Exception as exc:
        logger.warning("run_digest_skipped", error=str(exc))

    return {
        **state,
        "run_metrics": metrics,  # type: ignore[typeddict-item]
    }


def _compute_duration(start_time: str, end_time: str) -> float:
    """Compute duration in seconds between two ISO timestamps.

    Args:
        start_time: ISO timestamp string.
        end_time: ISO timestamp string.

    Returns:
        Duration in seconds as a float.
    """
    try:
        start = datetime.fromisoformat(start_time)
        end = datetime.fromisoformat(end_time)
        return round((end - start).total_seconds(), 1)
    except Exception:
        return 0.0
