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

_PAGE_CRASHED_MARKERS = ("page crashed", "page.goto: page crashed", "target page, context")


def _is_page_crash(error_str: str) -> bool:
    """Return True if the error string indicates a Chromium page crash."""
    lower = error_str.lower()
    return any(marker in lower for marker in _PAGE_CRASHED_MARKERS)


async def _recycle_page(context: object) -> object:
    """Close all existing pages and open a fresh one from the context.

    Called after a page crash to ensure subsequent scraping uses a clean page.

    Args:
        context: Active Playwright BrowserContext.

    Returns:
        New Playwright Page.
    """
    from playwright_linkedin.browser import new_page_with_stealth

    try:
        # Close all stale pages to free memory before opening a new one
        pages = context.pages  # type: ignore[attr-defined]
        for p in pages:
            try:
                await p.close()
            except Exception:
                pass
    except Exception:
        pass

    new_page = await new_page_with_stealth(context)  # type: ignore[arg-type]
    logger.info("page_recycled_after_crash")
    return new_page


async def enrich_profile(
    state: LinkedInProspectionState,
    page: object,
    db: object,
    context: object | None = None,
) -> LinkedInProspectionState:
    """Visit each candidate profile page and scrape full data.

    Skips profiles that already have a headline (already enriched).
    Handles private profiles gracefully with partial data.
    Persists scraped profiles to the database.

    On Chromium page crashes (memory pressure on RPi), the page is recycled
    (a fresh page is opened from the context) before continuing to the next profile.
    Requires `context` to be passed for page recycling.

    Args:
        state: Current pipeline state with candidate_profiles.
        page: Authenticated Playwright Page.
        db: Active aiosqlite database connection.
        context: Playwright BrowserContext — required for page crash recovery.

    Returns:
        Updated state with enriched candidate_profiles.
    """
    from storage.queries import log_action, upsert_profile

    check_activity_window()

    enriched_profiles: list[Profile] = []
    errors = list(state["errors"])
    actions_count = state["actions_count"]

    # current_page may be replaced after a page crash
    current_page = page

    for profile in state["candidate_profiles"]:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        # Skip if already enriched
        if profile.headline is not None:
            enriched_profiles.append(profile)
            continue

        try:
            scraped = await scrape_profile(current_page, profile.linkedin_url)  # type: ignore[arg-type]
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
            error_str = str(exc)
            errors.append(f"enrich:{profile.linkedin_url}: {exc}")

            if _is_page_crash(error_str):
                logger.error(
                    "profile_scraping_failed",
                    url=profile.linkedin_url,
                    error=error_str,
                    action="recycling_page",
                )
                # Recycle the page to avoid using a corrupted context for the next profile
                if context is not None:
                    try:
                        current_page = await _recycle_page(context)
                    except Exception as recycle_exc:
                        logger.error("page_recycle_failed", error=str(recycle_exc))
                else:
                    logger.warning(
                        "page_crash_no_context",
                        url=profile.linkedin_url,
                        hint="Pass context to enrich_profile for automatic page recovery",
                    )
            else:
                logger.warning(
                    "profile_scraping_failed",
                    url=profile.linkedin_url,
                    error=error_str,
                )

            enriched_profiles.append(profile)  # Keep partial data
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="error",
                    payload={"url": profile.linkedin_url},
                    success=False,
                    error_message=error_str,
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
