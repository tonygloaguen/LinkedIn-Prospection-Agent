"""Rate limiting and delay management for the LinkedIn agent."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Optional

import structlog

from agent.exceptions import QuotaExceededException

logger = structlog.get_logger(__name__)

# Activity window: 08:00 – 20:00 local time
_ACTIVITY_HOUR_START = 8
_ACTIVITY_HOUR_END = 20


def _current_hour() -> int:
    """Return the current UTC hour (used as proxy for local Paris time)."""
    return datetime.now(timezone.utc).hour


def check_activity_window() -> None:
    """Raise QuotaExceededException if outside the allowed activity window (08h-20h).

    Raises:
        QuotaExceededException: If the current hour is outside the allowed range.
    """
    hour = _current_hour()
    if not (_ACTIVITY_HOUR_START <= hour < _ACTIVITY_HOUR_END):
        raise QuotaExceededException(
            f"Outside activity window (08h-20h UTC). Current hour: {hour}"
        )


async def check_quotas(
    db_path: str,
    max_invitations: int,
    max_actions: int,
    current_actions_count: int,
) -> None:
    """Verify that daily quotas have not been exceeded.

    Args:
        db_path: Path to the SQLite database.
        max_invitations: Maximum invitations allowed per day.
        max_actions: Maximum actions allowed per day.
        current_actions_count: Actions already performed in this run.

    Raises:
        QuotaExceededException: If any quota limit is reached.
    """
    import aiosqlite

    from storage.queries import count_today_actions, count_today_invitations

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        today_invitations = await count_today_invitations(db)
        today_actions = await count_today_actions(db)

    if today_invitations >= max_invitations:
        raise QuotaExceededException(
            f"Daily invitation quota reached: {today_invitations}/{max_invitations}"
        )

    total_actions = today_actions + current_actions_count
    if total_actions >= max_actions:
        raise QuotaExceededException(
            f"Daily action quota reached: {total_actions}/{max_actions}"
        )

    logger.debug(
        "quotas_ok",
        invitations=today_invitations,
        max_invitations=max_invitations,
        actions=total_actions,
        max_actions=max_actions,
    )


async def delay_between_actions(min_s: float = 20.0, max_s: float = 120.0) -> None:
    """Sleep for a random duration between actions.

    Args:
        min_s: Minimum delay in seconds (default 20).
        max_s: Maximum delay in seconds (default 120).
    """
    delay = random.uniform(min_s, max_s)
    logger.debug("throttle_delay", seconds=round(delay, 1))
    await asyncio.sleep(delay)


async def delay_after_invitation() -> None:
    """Sleep for a random duration after sending an invitation (45-180s)."""
    await delay_between_actions(45.0, 180.0)


async def delay_after_search() -> None:
    """Sleep for a random duration after performing a search (10-35s)."""
    await delay_between_actions(10.0, 35.0)


async def delay_between_profile_visits() -> None:
    """Sleep for a random duration between profile visits (20-120s)."""
    await delay_between_actions(20.0, 120.0)
