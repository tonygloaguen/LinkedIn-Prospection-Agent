"""All SQLite queries for the LinkedIn Prospection Agent."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite
import structlog

from models.action_log import ActionLog
from models.post import Post
from models.profile import Profile, ScoredProfile

logger = structlog.get_logger(__name__)


async def upsert_profile(db: aiosqlite.Connection, profile: Profile) -> None:
    """Insert or update a profile record in the database.

    Args:
        db: Active database connection.
        profile: Profile object to persist.
    """
    await db.execute(
        """
        INSERT INTO profiles (
            id, linkedin_url, full_name, headline, bio, location,
            connections_count, is_recruiter, is_technical,
            score_recruiter, score_technical, score_activity, score_total,
            profile_category, scraped_at, last_action, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            full_name = excluded.full_name,
            headline = excluded.headline,
            bio = excluded.bio,
            location = excluded.location,
            connections_count = excluded.connections_count,
            is_recruiter = excluded.is_recruiter,
            is_technical = excluded.is_technical,
            score_recruiter = excluded.score_recruiter,
            score_technical = excluded.score_technical,
            score_activity = excluded.score_activity,
            score_total = excluded.score_total,
            profile_category = excluded.profile_category,
            scraped_at = excluded.scraped_at,
            last_action = excluded.last_action
        """,
        (
            profile.id,
            profile.linkedin_url,
            profile.full_name,
            profile.headline,
            profile.bio,
            profile.location,
            profile.connections_count,
            int(profile.is_recruiter),
            int(profile.is_technical),
            profile.score_recruiter,
            profile.score_technical,
            profile.score_activity,
            profile.score_total,
            profile.profile_category,
            profile.scraped_at,
            profile.last_action,
            profile.status,
        ),
    )
    await db.commit()


async def upsert_scored_profile(db: aiosqlite.Connection, profile: ScoredProfile) -> None:
    """Update scoring fields for an existing profile.

    Args:
        db: Active database connection.
        profile: ScoredProfile with scoring data.
    """
    await db.execute(
        """
        UPDATE profiles SET
            score_recruiter = ?,
            score_technical = ?,
            score_activity = ?,
            score_total = ?,
            profile_category = ?,
            is_recruiter = ?,
            is_technical = ?
        WHERE id = ?
        """,
        (
            profile.score_recruiter,
            profile.score_technical,
            profile.score_activity,
            profile.score_total,
            profile.profile_category,
            int(profile.profile_category in ("recruiter",)),
            int(profile.profile_category in ("technical", "cto_ciso")),
            profile.id,
        ),
    )
    await db.commit()


async def update_profile_status(
    db: aiosqlite.Connection, profile_id: str, status: str, last_action: Optional[str] = None
) -> None:
    """Update the status and optionally the last_action of a profile.

    Args:
        db: Active database connection.
        profile_id: Profile identifier.
        status: New status value (pending|messaged|connected|ignored).
        last_action: Optional ISO timestamp of the last action.
    """
    ts = last_action or datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE profiles SET status = ?, last_action = ? WHERE id = ?",
        (status, ts, profile_id),
    )
    await db.commit()


async def get_profile_by_id(
    db: aiosqlite.Connection, profile_id: str
) -> Optional[dict[str, Any]]:
    """Fetch a profile record by its identifier.

    Args:
        db: Active database connection.
        profile_id: Profile identifier.

    Returns:
        Dictionary of profile fields or None if not found.
    """
    async with db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def insert_post(db: aiosqlite.Connection, post: Post) -> None:
    """Insert a post record, ignoring duplicates.

    Args:
        db: Active database connection.
        post: Post object to persist.
    """
    await db.execute(
        """
        INSERT OR IGNORE INTO posts (
            id, author_profile_id, content_snippet, post_url, keywords_matched, found_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            post.id,
            post.author_profile_id,
            post.content_snippet,
            post.post_url,
            json.dumps(post.keywords_matched),
            post.found_at,
        ),
    )
    await db.commit()


async def log_action(db: aiosqlite.Connection, log: ActionLog) -> None:
    """Insert an action log entry.

    Args:
        db: Active database connection.
        log: ActionLog object to persist.
    """
    await db.execute(
        """
        INSERT INTO action_logs (
            timestamp, action_type, profile_id, post_id, payload, success, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            log.timestamp,
            log.action_type,
            log.profile_id,
            log.post_id,
            json.dumps(log.payload) if log.payload else None,
            int(log.success),
            log.error_message,
        ),
    )
    await db.commit()


async def count_today_invitations(db: aiosqlite.Connection) -> int:
    """Count the number of connection invitations sent today.

    Args:
        db: Active database connection.

    Returns:
        Number of successful connect actions logged today.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    async with db.execute(
        """
        SELECT COUNT(*) FROM action_logs
        WHERE action_type = 'connect'
          AND success = 1
          AND timestamp LIKE ?
        """,
        (f"{today}%",),
    ) as cursor:
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def count_today_actions(db: aiosqlite.Connection) -> int:
    """Count total actions performed today.

    Args:
        db: Active database connection.

    Returns:
        Total count of action_logs entries for today.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    async with db.execute(
        "SELECT COUNT(*) FROM action_logs WHERE timestamp LIKE ?",
        (f"{today}%",),
    ) as cursor:
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def save_run_history(
    db: aiosqlite.Connection, run_id: str, started_at: str, ended_at: str, metrics: dict[str, Any]
) -> None:
    """Persist run history with metrics.

    Args:
        db: Active database connection.
        run_id: Unique run identifier.
        started_at: ISO timestamp of run start.
        ended_at: ISO timestamp of run end.
        metrics: RunMetrics dictionary to store as JSON.
    """
    await db.execute(
        """
        INSERT OR REPLACE INTO run_history (run_id, started_at, ended_at, metrics)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, started_at, ended_at, json.dumps(metrics)),
    )
    await db.commit()


async def get_stats(db: aiosqlite.Connection) -> dict[str, Any]:
    """Aggregate statistics across all runs.

    Args:
        db: Active database connection.

    Returns:
        Dictionary with profiles_total, by_category, by_status, top_profiles,
        invitations_today, invitations_total, actions_today.
    """
    stats: dict[str, Any] = {}

    async with db.execute("SELECT COUNT(*) FROM profiles") as c:
        row = await c.fetchone()
        stats["profiles_total"] = int(row[0]) if row else 0

    async with db.execute(
        "SELECT profile_category, COUNT(*) FROM profiles GROUP BY profile_category"
    ) as c:
        stats["by_category"] = {row[0]: row[1] async for row in c}

    async with db.execute(
        "SELECT status, COUNT(*) FROM profiles GROUP BY status"
    ) as c:
        stats["by_status"] = {row[0]: row[1] async for row in c}

    async with db.execute(
        "SELECT full_name, headline, score_total, profile_category FROM profiles "
        "ORDER BY score_total DESC LIMIT 10"
    ) as c:
        stats["top_profiles"] = [dict(row) async for row in c]

    stats["invitations_today"] = await count_today_invitations(db)
    stats["actions_today"] = await count_today_actions(db)

    async with db.execute(
        "SELECT COUNT(*) FROM action_logs WHERE action_type = 'connect' AND success = 1"
    ) as c:
        row = await c.fetchone()
        stats["invitations_total"] = int(row[0]) if row else 0

    return stats
