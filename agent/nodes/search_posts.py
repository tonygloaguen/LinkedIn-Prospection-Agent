"""search_posts node: search LinkedIn for posts matching keywords."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from agent.exceptions import (
    LinkedInAuthError,
    LinkedInSessionExpiredError,
    PostSearchError,
    QuotaExceededException,
)
from agent.state import LinkedInProspectionState
from models.action_log import ActionLog
from playwright_linkedin.search import search_posts_for_keyword
from utils.throttle import check_activity_window, delay_after_search

logger = structlog.get_logger(__name__)

_REAUTH_MAX_ATTEMPTS = 3
_REAUTH_BACKOFF_SECONDS = 5


async def _reauth(context: object, attempt: int) -> object:
    """Attempt to re-authenticate and return a fresh page.

    Args:
        context: Active Playwright BrowserContext.
        attempt: Current attempt number (1-based), used for backoff.

    Returns:
        New authenticated Page.

    Raises:
        LinkedInAuthError: If login fails.
    """
    from playwright_linkedin.auth import login

    if attempt > 1:
        backoff = _REAUTH_BACKOFF_SECONDS * attempt
        logger.info("reauth_backoff", seconds=backoff, attempt=attempt)
        await asyncio.sleep(backoff)

    return await login(context)  # type: ignore[arg-type]


async def search_posts(
    state: LinkedInProspectionState,
    page: object,
    db: object,
    context: object | None = None,
) -> LinkedInProspectionState:
    """Search LinkedIn for posts matching each keyword in state.keywords.

    For each keyword, navigates to the LinkedIn content search page,
    extracts post URLs, author URLs, and content snippets.
    Logs each search action to the database.

    On session expiry (redirect to /uas/login), triggers mid-run re-authentication
    via `context` (up to _REAUTH_MAX_ATTEMPTS times). All remaining keywords are
    retried on the new page. If re-auth fails after all attempts, raises
    LinkedInAuthError to stop the pipeline cleanly rather than cascading errors.

    Args:
        state: Current pipeline state.
        page: Authenticated Playwright Page.
        db: Active aiosqlite database connection.
        context: Playwright BrowserContext — required for mid-run re-auth.

    Returns:
        Updated state with collected_posts populated.
    """
    from storage.queries import log_action

    check_activity_window()

    all_posts = list(state["collected_posts"])
    seen_post_ids: set[str] = {p.id for p in all_posts}
    errors = list(state["errors"])
    actions_count = state["actions_count"]

    # current_page may be replaced after a mid-run re-auth
    current_page = page

    for keyword in state["keywords"]:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        try:
            posts = await search_posts_for_keyword(current_page, keyword)  # type: ignore[arg-type]

            for post in posts:
                if post.id not in seen_post_ids:
                    all_posts.append(post)
                    seen_post_ids.add(post.id)

            actions_count += 1

            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="search",
                    payload={"keyword": keyword, "posts_found": len(posts)},
                    success=True,
                ),
            )

            logger.info(
                "keyword_search_done",
                keyword=keyword,
                new_posts=len(posts),
                total_posts=len(all_posts),
            )

            await delay_after_search()

        except LinkedInSessionExpiredError as exc:
            # Session expired mid-run — attempt re-auth then retry current keyword
            logger.warning(
                "session_expired_mid_run_reauth",
                keyword=keyword,
                error=str(exc),
            )
            errors.append(f"search:{keyword}:session_expired")

            if context is None:
                logger.error(
                    "reauth_impossible_no_context",
                    keyword=keyword,
                )
                raise LinkedInAuthError(
                    "Session expired mid-run and no BrowserContext available for re-auth"
                ) from exc

            reauth_page: object | None = None
            for attempt in range(1, _REAUTH_MAX_ATTEMPTS + 1):
                try:
                    reauth_page = await _reauth(context, attempt)
                    logger.info(
                        "session_expired_mid_run_reauth_success",
                        attempt=attempt,
                        keyword=keyword,
                    )
                    break
                except LinkedInAuthError as auth_exc:
                    logger.error(
                        "reauth_attempt_failed",
                        attempt=attempt,
                        max_attempts=_REAUTH_MAX_ATTEMPTS,
                        error=str(auth_exc),
                    )

            if reauth_page is None:
                raise LinkedInAuthError(
                    f"Session expired mid-run — re-auth failed after {_REAUTH_MAX_ATTEMPTS} attempts"
                ) from exc

            # Switch to the fresh authenticated page for all remaining keywords
            current_page = reauth_page

            # Retry the current keyword on the new page
            try:
                posts = await search_posts_for_keyword(current_page, keyword)  # type: ignore[arg-type]
                for post in posts:
                    if post.id not in seen_post_ids:
                        all_posts.append(post)
                        seen_post_ids.add(post.id)
                actions_count += 1
                await log_action(
                    db,  # type: ignore[arg-type]
                    ActionLog(
                        timestamp=datetime.now(UTC).isoformat(),
                        action_type="search",
                        payload={"keyword": keyword, "posts_found": len(posts)},
                        success=True,
                    ),
                )
                logger.info(
                    "keyword_search_done_after_reauth",
                    keyword=keyword,
                    new_posts=len(posts),
                )
                await delay_after_search()
            except Exception as retry_exc:
                errors.append(f"search:{keyword}:after_reauth: {retry_exc}")
                logger.error(
                    "search_failed_after_reauth",
                    keyword=keyword,
                    error=str(retry_exc),
                )

        except (QuotaExceededException, LinkedInAuthError):
            raise
        except PostSearchError as exc:
            errors.append(f"search:{keyword}: {exc}")
            logger.error("search_failed", keyword=keyword, error=str(exc))
            await log_action(
                db,  # type: ignore[arg-type]
                ActionLog(
                    timestamp=datetime.now(UTC).isoformat(),
                    action_type="error",
                    payload={"keyword": keyword},
                    success=False,
                    error_message=str(exc),
                ),
            )
        except Exception as exc:
            errors.append(f"search:{keyword}: {exc}")
            logger.error("search_unexpected_error", keyword=keyword, error=str(exc))

    metrics = dict(state["run_metrics"])
    metrics["posts_found"] = len(all_posts)

    return {
        **state,
        "collected_posts": all_posts,
        "actions_count": actions_count,
        "errors": errors,
        "run_metrics": metrics,  # type: ignore[typeddict-item]
    }
