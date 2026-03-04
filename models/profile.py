"""Pydantic models for LinkedIn profiles."""

from __future__ import annotations

import hashlib
from typing import Literal, Optional

from pydantic import BaseModel, Field, computed_field


ProfileCategory = Literal["recruiter", "technical", "cto_ciso", "other"]
ProfileStatus = Literal["pending", "messaged", "connected", "ignored"]


class Profile(BaseModel):
    """A LinkedIn profile with basic scraped data.

    Attributes:
        linkedin_url: Canonical LinkedIn profile URL.
        full_name: Display name of the person.
        headline: Professional headline (job title / tagline).
        bio: "About" section text, may be empty.
        location: Geographic location string.
        connections_count: Approximate number of connections (may be None for private).
        is_recruiter: Whether LLM identified this person as a recruiter.
        is_technical: Whether LLM identified this person as a technical profile.
        score_recruiter: Recruiter probability score [0.0, 1.0].
        score_technical: Technical profile probability score [0.0, 1.0].
        score_activity: Estimated LinkedIn activity score [0.0, 1.0].
        score_total: Weighted total score.
        profile_category: Assigned category.
        scraped_at: ISO timestamp of last scrape.
        last_action: ISO timestamp of last action taken.
        status: Current processing status.
    """

    linkedin_url: str
    full_name: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    connections_count: Optional[int] = None
    is_recruiter: bool = False
    is_technical: bool = False
    score_recruiter: float = Field(default=0.0, ge=0.0, le=1.0)
    score_technical: float = Field(default=0.0, ge=0.0, le=1.0)
    score_activity: float = Field(default=0.0, ge=0.0, le=1.0)
    score_total: float = Field(default=0.0, ge=0.0, le=1.0)
    profile_category: ProfileCategory = "other"
    scraped_at: Optional[str] = None
    last_action: Optional[str] = None
    status: ProfileStatus = "pending"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Compute a short SHA256 identifier from the LinkedIn URL."""
        return hashlib.sha256(self.linkedin_url.encode()).hexdigest()[:16]


class ScoredProfile(Profile):
    """A Profile enriched with LLM scoring data.

    Inherits all fields from Profile and overrides scoring fields
    with values returned by the LLM scoring node.
    """

    reasoning: Optional[str] = None
