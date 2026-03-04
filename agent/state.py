"""LangGraph state definition for the LinkedIn Prospection Agent."""

from __future__ import annotations

from typing_extensions import TypedDict

from models.post import Post
from models.profile import Profile, ScoredProfile


class RunMetrics(TypedDict):
    """Aggregated run metrics.

    Attributes:
        posts_found: Number of posts collected.
        profiles_extracted: Number of unique profiles extracted.
        profiles_scored: Number of profiles scored by LLM.
        invitations_sent: Number of connection invitations sent.
        errors_count: Number of errors encountered.
        start_time: ISO timestamp of run start.
        end_time: ISO timestamp of run end (None while running).
    """

    posts_found: int
    profiles_extracted: int
    profiles_scored: int
    invitations_sent: int
    errors_count: int
    start_time: str
    end_time: str | None


class LinkedInProspectionState(TypedDict):
    """Full state for the LinkedIn Prospection LangGraph pipeline.

    Attributes:
        keywords: Search keywords to use for post discovery.
        max_invitations: Maximum number of invitations to send per run (default 15).
        max_actions: Maximum number of total actions per run (default 40).
        dry_run: When True, no real invitations are sent.
        collected_posts: Posts found by search_posts node.
        candidate_profiles: Profiles extracted from posts.
        scored_profiles: Profiles with LLM scoring applied.
        messages_generated: Mapping of profile_id to generated connection message.
        invitations_sent: Profile IDs for which invitations were sent.
        actions_count: Running count of actions performed.
        errors: List of error descriptions encountered during the run.
        run_metrics: Aggregated metrics for the current run.
    """

    # Input
    keywords: list[str]
    max_invitations: int
    max_actions: int
    dry_run: bool

    # Pipeline data
    collected_posts: list[Post]
    candidate_profiles: list[Profile]
    scored_profiles: list[ScoredProfile]
    messages_generated: dict[str, str]  # profile_id -> message
    invitations_sent: list[str]  # profile_ids

    # Control
    actions_count: int
    errors: list[str]
    run_metrics: RunMetrics
