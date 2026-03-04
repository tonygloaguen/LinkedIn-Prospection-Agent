"""Tests for Pydantic models."""

from __future__ import annotations

import pytest

from models.action_log import ActionLog
from models.post import Post
from models.profile import Profile, ScoredProfile


class TestProfile:
    """Tests for the Profile model."""

    def test_id_computed_from_url(self) -> None:
        """Profile.id is a deterministic SHA256 hash of linkedin_url."""
        p = Profile(linkedin_url="https://www.linkedin.com/in/alice")
        assert len(p.id) == 16
        assert p.id == Profile(linkedin_url="https://www.linkedin.com/in/alice").id

    def test_different_urls_different_ids(self) -> None:
        """Different URLs produce different IDs."""
        p1 = Profile(linkedin_url="https://www.linkedin.com/in/alice")
        p2 = Profile(linkedin_url="https://www.linkedin.com/in/bob")
        assert p1.id != p2.id

    def test_default_status_pending(self) -> None:
        """Default profile status is 'pending'."""
        p = Profile(linkedin_url="https://www.linkedin.com/in/alice")
        assert p.status == "pending"

    def test_score_bounds(self) -> None:
        """Score fields reject values outside [0, 1]."""
        with pytest.raises(Exception):
            Profile(linkedin_url="https://x.com", score_recruiter=1.5)

    def test_scored_profile_inherits(self) -> None:
        """ScoredProfile is a subclass of Profile."""
        p = Profile(linkedin_url="https://www.linkedin.com/in/alice")
        sp = ScoredProfile(**p.model_dump(), reasoning="test")
        assert sp.id == p.id
        assert sp.reasoning == "test"


class TestPost:
    """Tests for the Post model."""

    def test_id_from_url(self) -> None:
        """Post.id is derived from post_url."""
        post = Post(
            post_url="https://www.linkedin.com/posts/alice_123",
            author_linkedin_url="https://www.linkedin.com/in/alice",
        )
        assert len(post.id) == 16

    def test_author_profile_id(self) -> None:
        """Post.author_profile_id matches Profile.id for same URL."""
        from models.profile import Profile

        author_url = "https://www.linkedin.com/in/alice"
        post = Post(post_url="https://www.linkedin.com/posts/x", author_linkedin_url=author_url)
        profile = Profile(linkedin_url=author_url)
        assert post.author_profile_id == profile.id

    def test_keywords_matched_default_empty(self) -> None:
        """keywords_matched defaults to empty list."""
        post = Post(
            post_url="https://x.com",
            author_linkedin_url="https://www.linkedin.com/in/alice",
        )
        assert post.keywords_matched == []


class TestActionLog:
    """Tests for the ActionLog model."""

    def test_default_success_true(self) -> None:
        """ActionLog.success defaults to True."""
        log = ActionLog(timestamp="2024-01-01T00:00:00Z", action_type="search")
        assert log.success is True

    def test_error_action(self) -> None:
        """ActionLog can represent an error action."""
        log = ActionLog(
            timestamp="2024-01-01T00:00:00Z",
            action_type="error",
            success=False,
            error_message="Something failed",
        )
        assert log.success is False
        assert log.error_message == "Something failed"
