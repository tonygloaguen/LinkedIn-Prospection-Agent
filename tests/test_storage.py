"""Integration tests for storage layer (SQLite via aiosqlite)."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from models.action_log import ActionLog
from models.post import Post
from models.profile import Profile
from storage.database import init_db


@pytest.fixture
async def db_path(tmp_path: Path) -> str:
    """Create a temporary database and return its path."""
    path = str(tmp_path / "test.db")
    await init_db(path)
    return path


@pytest.fixture
async def db_conn(db_path: str):  # type: ignore[return]
    """Yield an open aiosqlite connection to the test database."""
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


class TestProfileQueries:
    """Tests for profile DB operations."""

    async def test_upsert_and_fetch_profile(self, db_conn: object) -> None:
        """Can upsert a profile and retrieve it by id."""
        from storage.queries import get_profile_by_id, upsert_profile

        profile = Profile(
            linkedin_url="https://www.linkedin.com/in/testuser",
            full_name="Test User",
            headline="DevSecOps Engineer",
            location="Paris, France",
        )
        await upsert_profile(db_conn, profile)  # type: ignore[arg-type]

        fetched = await get_profile_by_id(db_conn, profile.id)  # type: ignore[arg-type]
        assert fetched is not None
        assert fetched["full_name"] == "Test User"
        assert fetched["linkedin_url"] == "https://www.linkedin.com/in/testuser"

    async def test_upsert_profile_updates_on_conflict(self, db_conn: object) -> None:
        """Second upsert updates existing record."""
        from storage.queries import get_profile_by_id, upsert_profile

        profile = Profile(
            linkedin_url="https://www.linkedin.com/in/testuser2",
            full_name="Original Name",
        )
        await upsert_profile(db_conn, profile)  # type: ignore[arg-type]

        updated = Profile(
            linkedin_url="https://www.linkedin.com/in/testuser2",
            full_name="Updated Name",
            headline="CTO",
        )
        await upsert_profile(db_conn, updated)  # type: ignore[arg-type]

        fetched = await get_profile_by_id(db_conn, updated.id)  # type: ignore[arg-type]
        assert fetched["full_name"] == "Updated Name"
        assert fetched["headline"] == "CTO"

    async def test_update_profile_status(self, db_conn: object) -> None:
        """update_profile_status changes the status field."""
        from storage.queries import get_profile_by_id, update_profile_status, upsert_profile

        profile = Profile(linkedin_url="https://www.linkedin.com/in/status-test")
        await upsert_profile(db_conn, profile)  # type: ignore[arg-type]
        await update_profile_status(db_conn, profile.id, "messaged")  # type: ignore[arg-type]

        fetched = await get_profile_by_id(db_conn, profile.id)  # type: ignore[arg-type]
        assert fetched["status"] == "messaged"


class TestPostQueries:
    """Tests for post DB operations."""

    async def test_insert_post(self, db_conn: object) -> None:
        """Can insert a post without error."""
        from storage.queries import insert_post, upsert_profile

        author_url = "https://www.linkedin.com/in/someone"
        await upsert_profile(db_conn, Profile(linkedin_url=author_url))  # type: ignore[arg-type]
        post = Post(
            post_url="https://www.linkedin.com/posts/x_abc",
            author_linkedin_url=author_url,
            keywords_matched=["LangGraph", "agent"],
        )
        await insert_post(db_conn, post)  # type: ignore[arg-type]

    async def test_insert_duplicate_post_ignored(self, db_conn: object) -> None:
        """Duplicate post insert is silently ignored (OR IGNORE)."""
        from storage.queries import insert_post, upsert_profile

        author_url = "https://www.linkedin.com/in/someone-dup"
        await upsert_profile(db_conn, Profile(linkedin_url=author_url))  # type: ignore[arg-type]
        post = Post(
            post_url="https://www.linkedin.com/posts/x_dup",
            author_linkedin_url=author_url,
        )
        await insert_post(db_conn, post)  # type: ignore[arg-type]
        await insert_post(db_conn, post)  # Should not raise


class TestActionLogQueries:
    """Tests for action_logs DB operations."""

    async def test_log_action(self, db_conn: object) -> None:
        """Can log an action entry."""
        from storage.queries import log_action

        entry = ActionLog(
            timestamp="2024-01-01T10:00:00Z",
            action_type="search",
            payload={"keyword": "LangGraph"},
            success=True,
        )
        await log_action(db_conn, entry)  # type: ignore[arg-type]

    async def test_count_today_invitations(self, db_conn: object) -> None:
        """count_today_invitations returns correct count for today."""
        from datetime import datetime

        from storage.queries import count_today_invitations, log_action

        today = datetime.now(UTC).isoformat()
        for _ in range(3):
            await log_action(
                db_conn,  # type: ignore[arg-type]
                ActionLog(timestamp=today, action_type="connect", success=True),
            )

        count = await count_today_invitations(db_conn)  # type: ignore[arg-type]
        assert count >= 3


class TestRunHistory:
    """Tests for run_history DB operations."""

    async def test_save_run_history(self, db_conn: object) -> None:
        """Can save a run history entry."""
        from storage.queries import save_run_history

        await save_run_history(
            db_conn,  # type: ignore[arg-type]
            run_id="test-run-001",
            started_at="2024-01-01T08:00:00Z",
            ended_at="2024-01-01T09:00:00Z",
            metrics={"posts_found": 10, "invitations_sent": 3},
        )


class TestGetStats:
    """Tests for the get_stats aggregation query."""

    async def test_get_stats_empty_db(self, db_conn: object) -> None:
        """get_stats works on an empty database."""
        from storage.queries import get_stats

        stats = await get_stats(db_conn)  # type: ignore[arg-type]
        assert stats["profiles_total"] == 0
        assert stats["invitations_today"] == 0
        assert stats["invitations_total"] == 0
