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

    # ── Warm-up: navigate to feed before visiting individual profiles ─────────
    # After a heavy search session LinkedIn is suspicious of immediate profile
    # visits.  A feed visit + random pause mimics natural browsing behaviour.
    import random as _random

    try:
        _warmup_delay = _random.uniform(60, 120)
        logger.info(
            "enrich_warmup_pause",
            seconds=round(_warmup_delay, 1),
            hint="Cooling down after search phase before profile visits",
        )
        await page.wait_for_timeout(int(_warmup_delay * 1000))  # type: ignore[attr-defined]
        await page.goto(  # type: ignore[attr-defined]
            "https://www.linkedin.com/feed/",
            timeout=30_000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(int(_random.uniform(5_000, 10_000)))  # type: ignore[attr-defined]
        logger.info("enrich_warmup_done")
    except Exception as _warmup_exc:
        logger.warning("enrich_warmup_failed", error=str(_warmup_exc))
    # ─────────────────────────────────────────────────────────────────────────

    enriched_profiles: list[Profile] = []
    errors = list(state["errors"])
    actions_count = state["actions_count"]

    # Reserve half the remaining budget for scoring + sending.
    remaining = state["max_actions"] - actions_count
    max_enrich = max(1, remaining // 2)
    enrich_count = 0

    # current_page may be replaced after a page crash
    current_page = page

    # ── Resilience counters ──────────────────────────────────────────────────
    # Track consecutive failures to detect a bot wall early and pause/abort
    # rather than hammering LinkedIn for 100+ profiles in a row.
    _consecutive_failures = 0
    _total_attempts = 0
    _total_successes = 0
    _circuit_breaker_logged = False  # log circuit-breaker warning only once
    # After this many consecutive failures we pause for a longer delay.
    _consecutive_fail_pause_threshold = int(
        __import__("os").environ.get("ENRICH_PAUSE_AFTER_FAILURES", "5")
    )
    # After this many consecutive failures we give up enriching (circuit breaker).
    _consecutive_fail_abort_threshold = int(
        __import__("os").environ.get("ENRICH_ABORT_AFTER_FAILURES", "10")
    )
    # ─────────────────────────────────────────────────────────────────────────

    for profile in state["candidate_profiles"]:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        # Skip if already enriched
        if profile.headline is not None:
            enriched_profiles.append(profile)
            continue

        # Stop enriching once the reserved budget is used
        if enrich_count >= max_enrich:
            logger.info(
                "enrich_budget_reached",
                enriched=enrich_count,
                remaining_profiles=len(state["candidate_profiles"]) - len(enriched_profiles),
                hint="Increase max_actions or reduce keywords to enrich more profiles",
            )
            enriched_profiles.append(profile)
            continue

        # ── Circuit breaker: stop if bot wall is detected ────────────────────
        if _consecutive_failures >= _consecutive_fail_abort_threshold:
            if not _circuit_breaker_logged:
                logger.warning(
                    "enrich_circuit_breaker_open",
                    consecutive_failures=_consecutive_failures,
                    total_attempts=_total_attempts,
                    total_successes=_total_successes,
                    hint="LinkedIn likely blocking all profile requests — skipping remaining profiles",
                )
                _circuit_breaker_logged = True
            enriched_profiles.append(profile)
            continue
        # ─────────────────────────────────────────────────────────────────────

        _total_attempts += 1

        try:
            scraped = await scrape_profile(current_page, profile.linkedin_url)  # type: ignore[arg-type]
            enriched_profiles.append(scraped)
            actions_count += 1
            enrich_count += 1
            _total_successes += 1
            _consecutive_failures = 0  # reset on success

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
                success_rate=f"{_total_successes}/{_total_attempts}",
            )

            await delay_between_profile_visits()

        except ProfileScrapingError as exc:
            error_str = str(exc)
            _consecutive_failures += 1
            errors.append(f"enrich:{profile.linkedin_url}: {exc}")

            # Extract the classified category from the error prefix
            error_category = (
                error_str.split(":")[0] if ":" in error_str else "profile_scraping_error"
            )

            if _is_page_crash(error_str):
                logger.error(
                    "profile_scraping_failed",
                    url=profile.linkedin_url,
                    error_category="page_crash",
                    action="recycling_page",
                )
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
                    error_category=error_category,
                    consecutive_failures=_consecutive_failures,
                    success_rate=f"{_total_successes}/{_total_attempts}",
                )

            enriched_profiles.append(profile)  # Keep partial data
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="error",
                    payload={"url": profile.linkedin_url, "error_category": error_category},
                    success=False,
                    error_message=error_str,
                ),
            )

            # ── Pause heuristic: slow down after a burst of consecutive failures ──
            if _consecutive_failures == _consecutive_fail_pause_threshold and error_category in (
                "profile_challenge_detected",
                "profile_timeout_dom_incomplete",
            ):
                import random

                pause_s = random.uniform(30, 60)
                logger.warning(
                    "enrich_consecutive_failures_pause",
                    consecutive_failures=_consecutive_failures,
                    pause_seconds=round(pause_s, 1),
                    hint="Likely bot-wall — pausing before continuing",
                )
                await current_page.wait_for_timeout(int(pause_s * 1000))  # type: ignore[attr-defined]
            # ─────────────────────────────────────────────────────────────────

        except Exception as exc:
            _consecutive_failures += 1
            errors.append(f"enrich:{profile.linkedin_url}: {exc}")
            enriched_profiles.append(profile)
            logger.error(
                "profile_enrich_unexpected",
                url=profile.linkedin_url,
                error=str(exc),
                consecutive_failures=_consecutive_failures,
            )

    # ── Final enrichment summary ─────────────────────────────────────────────
    logger.info(
        "enrich_node_summary",
        total_profiles=len(state["candidate_profiles"]),
        enriched=_total_successes,
        attempts=_total_attempts,
        success_rate=f"{_total_successes}/{_total_attempts}" if _total_attempts else "0/0",
        circuit_breaker_triggered=_consecutive_failures >= _consecutive_fail_abort_threshold,
    )
    # ─────────────────────────────────────────────────────────────────────────

    return {
        **state,
        "candidate_profiles": enriched_profiles,
        "actions_count": actions_count,
        "errors": errors,
    }
