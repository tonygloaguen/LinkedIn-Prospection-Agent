"""enrich_profile node: visit each profile page and scrape full data."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from agent.exceptions import ProfileScrapingError, QuotaExceededException
from agent.state import LinkedInProspectionState
from models.action_log import ActionLog
from models.profile import Profile
from playwright_linkedin.profile import scrape_profile
from utils.throttle import check_activity_window, delay_between_profile_visits

logger = structlog.get_logger(__name__)


async def enrich_profile(
    state: LinkedInProspectionState,
    page: object,
    db: object,
) -> LinkedInProspectionState:
    """Visit each candidate profile page and scrape full data.

    Skips profiles that already have a headline (already enriched).
    Handles private profiles gracefully with partial data.
    Persists scraped profiles to the database.

    Args:
        state: Current pipeline state with candidate_profiles.
        page: Authenticated Playwright Page.
        db: Active aiosqlite database connection.

    Returns:
        Updated state with enriched candidate_profiles.
    """
    from storage.queries import log_action, upsert_profile

    check_activity_window()

    enriched_profiles: list[Profile] = []
    errors = list(state["errors"])
    actions_count = state["actions_count"]

    for profile in state["candidate_profiles"]:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        # Skip if already enriched
        if profile.headline is not None:
            enriched_profiles.append(profile)
            continue

        try:
            scraped = await scrape_profile(page, profile.linkedin_url)  # type: ignore[arg-type]
            enriched_profiles.append(scraped)
            actions_count += 1

            await upsert_profile(db, scraped)  # type: ignore[arg-type]

            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="scrape",
                    profile_id=scraped.id,
                    payload={"url": scraped.linkedin_url, "has_bio": scraped.bio is not None},
                    success=True,
                ),
            )

            logger.info(
                "profile_enriched",
                url=profile.linkedin_url,
                name=scraped.full_name,
            )

            await delay_between_profile_visits()

        except ProfileScrapingError as exc:
            # Skip this profile, continue pipeline
            errors.append(f"enrich:{profile.linkedin_url}: {exc}")
            enriched_profiles.append(profile)  # Keep partial data
            logger.warning(
                "profile_scraping_failed",
                url=profile.linkedin_url,
                error=str(exc),
            )
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="error",
                    payload={"url": profile.linkedin_url},
                    success=False,
                    error_message=str(exc),
                ),
            )
        except Exception as exc:
            errors.append(f"enrich:{profile.linkedin_url}: {exc}")
            enriched_profiles.append(profile)
            logger.error(
                "profile_enrich_unexpected",
                url=profile.linkedin_url,
                error=str(exc),
            )

    return {
        **state,
        "candidate_profiles": enriched_profiles,
        "actions_count": actions_count,
        "errors": errors,
    }
