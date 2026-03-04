"""follow_up_scheduler node: schedule follow-up actions for pending profiles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from agent.state import LinkedInProspectionState

logger = structlog.get_logger(__name__)

_FOLLOW_UP_DAYS = 7  # Follow up if no response after 7 days


async def follow_up_scheduler(
    state: LinkedInProspectionState,
    db: object,
) -> LinkedInProspectionState:
    """Identify profiles that need a follow-up action.

    Checks the database for profiles that were messaged more than
    _FOLLOW_UP_DAYS ago with no status update, and logs them for
    future follow-up runs.

    This node is informational only — it does not send messages.
    Use the generated follow_up candidates in the next run's keywords
    or filter logic.

    Args:
        state: Current pipeline state.
        db: Active aiosqlite database connection.

    Returns:
        State unchanged (follow-up candidates logged only).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_FOLLOW_UP_DAYS)).isoformat()

    try:
        candidates = await _get_follow_up_candidates(db, cutoff)  # type: ignore[arg-type]

        if candidates:
            logger.info(
                "follow_up_candidates_found",
                count=len(candidates),
                cutoff=cutoff,
                candidates=[c["full_name"] for c in candidates[:5]],
            )
        else:
            logger.info("no_follow_up_candidates", cutoff=cutoff)

    except Exception as exc:
        logger.error("follow_up_scheduler_error", error=str(exc))

    return state


async def _get_follow_up_candidates(
    db: object, cutoff_date: str
) -> list[dict[str, Optional[str]]]:
    """Query profiles messaged before cutoff_date with no response.

    Args:
        db: Active aiosqlite database connection.
        cutoff_date: ISO date string — profiles messaged before this are candidates.

    Returns:
        List of profile dicts with id, full_name, linkedin_url, last_action.
    """
    import aiosqlite

    conn: aiosqlite.Connection = db  # type: ignore[assignment]

    async with conn.execute(
        """
        SELECT id, full_name, linkedin_url, last_action
        FROM profiles
        WHERE status = 'messaged'
          AND last_action < ?
        ORDER BY last_action ASC
        LIMIT 20
        """,
        (cutoff_date,),
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
