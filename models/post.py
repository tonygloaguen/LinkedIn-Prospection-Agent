"""Pydantic model for LinkedIn posts."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, computed_field


class Post(BaseModel):
    """A LinkedIn post discovered during keyword search.

    Attributes:
        post_url: URL to the LinkedIn post.
        author_linkedin_url: URL of the post author's profile.
        content_snippet: First ~300 characters of the post content.
        keywords_matched: List of search keywords that matched this post.
        found_at: ISO timestamp when the post was collected.
        author_profile_id: SHA256 short ID derived from author_linkedin_url.
    """

    post_url: str
    author_linkedin_url: str
    content_snippet: str | None = None
    keywords_matched: list[str] = Field(default_factory=list)
    found_at: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Compute a short SHA256 identifier from the post URL."""
        return hashlib.sha256(self.post_url.encode()).hexdigest()[:16]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def author_profile_id(self) -> str:
        """Compute the author profile id from the author LinkedIn URL."""
        return hashlib.sha256(self.author_linkedin_url.encode()).hexdigest()[:16]
