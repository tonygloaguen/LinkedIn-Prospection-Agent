"""extract_profiles node: deduplicate authors and extract top commenters."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from agent.state import LinkedInProspectionState
from models.action_log import ActionLog
from models.profile import Profile
from playwright_linkedin.profile import scrape_commenters
from utils.throttle import check_activity_window

logger = structlog.get_logger(__name__)

_MAX_COMMENTERS_PER_POST = 3


async def extract_profiles(
    state: LinkedInProspectionState,
    page: object,
    db: object,
) -> LinkedInProspectionState:
    """Extract unique author profiles from collected posts + top commenters.

    Deduplicates authors across all collected posts and optionally extracts
    top commenters from each post page (up to _MAX_COMMENTERS_PER_POST per post).

    Args:
        state: Current pipeline state with collected_posts.
        page: Authenticated Playwright Page.
        db: Active aiosqlite database connection.

    Returns:
        Updated state with candidate_profiles populated.
    """
    from storage.queries import log_action

    check_activity_window()

    existing_profiles = list(state["candidate_profiles"])
    seen_urls: set[str] = {p.linkedin_url for p in existing_profiles}
    errors = list(state["errors"])
    actions_count = state["actions_count"]

    quota_reached = False
    for post in state["collected_posts"]:
        # Add post author — free operation, no action budget consumed
        if post.author_linkedin_url not in seen_urls:
            profile = Profile(
                linkedin_url=post.author_linkedin_url,
                scraped_at=datetime.now(UTC).isoformat(),
            )
            existing_profiles.append(profile)
            seen_urls.add(post.author_linkedin_url)

        # Commenter scraping consumes action budget — skip when exhausted
        if actions_count >= state["max_actions"]:
            quota_reached = True
            continue

        # Extract commenters
        try:
            commenter_urls = await scrape_commenters(
                page,  # type: ignore[arg-type]
                post.post_url,
                max_commenters=_MAX_COMMENTERS_PER_POST,
            )
            actions_count += 1

            for url in commenter_urls:
                if url not in seen_urls:
                    profile = Profile(
                        linkedin_url=url,
                        scraped_at=datetime.now(UTC).isoformat(),
                    )
                    existing_profiles.append(profile)
                    seen_urls.add(url)

            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="scrape",
                    post_id=post.id,
                    payload={"commenters_found": len(commenter_urls)},
                    success=True,
                ),
            )

        except Exception as exc:
            errors.append(f"commenters:{post.post_url}: {exc}")
            logger.warning("commenter_extraction_failed", post_url=post.post_url, error=str(exc))

    metrics = dict(state["run_metrics"])
    metrics["profiles_extracted"] = len(existing_profiles)

    if quota_reached:
        logger.info(
            "commenter_quota_reached",
            actions_used=actions_count,
            max_actions=state["max_actions"],
            authors_collected=len(existing_profiles),
        )

    logger.info("profiles_extracted", total=len(existing_profiles))

    # Return state with all collected profiles — quota is handled via actions_count
    # so downstream nodes (score, invite) still run with what we have.
    return {
        **state,
        "candidate_profiles": existing_profiles,
        "actions_count": actions_count,
        "errors": errors,
        "run_metrics": metrics,  # type: ignore[typeddict-item]
    }
