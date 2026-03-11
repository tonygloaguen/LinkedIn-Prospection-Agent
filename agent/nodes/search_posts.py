"""search_posts node: search LinkedIn for posts matching keywords."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from agent.exceptions import LinkedInAuthError, PostSearchError, QuotaExceededException
from agent.state import LinkedInProspectionState
from models.action_log import ActionLog
from playwright_linkedin.search import search_posts_for_keyword
from utils.throttle import check_activity_window, delay_after_search

logger = structlog.get_logger(__name__)


async def search_posts(
    state: LinkedInProspectionState,
    page: object,
    db: object,
) -> LinkedInProspectionState:
    """Search LinkedIn for posts matching each keyword in state.keywords.

    For each keyword, navigates to the LinkedIn content search page,
    extracts post URLs, author URLs, and content snippets.
    Logs each search action to the database.

    Args:
        state: Current pipeline state.
        page: Authenticated Playwright Page.
        db: Active aiosqlite database connection.

    Returns:
        Updated state with collected_posts populated.
    """
    from storage.queries import log_action

    check_activity_window()

    all_posts = list(state["collected_posts"])
    seen_post_ids: set[str] = {p.id for p in all_posts}
    errors = list(state["errors"])
    actions_count = state["actions_count"]
    _reauthed = False  # allow at most one re-authentication per run

    for keyword in state["keywords"]:
        if actions_count >= state["max_actions"]:
            raise QuotaExceededException(f"Max actions ({state['max_actions']}) reached")

        try:
            # Inner try: intercept session expiry and attempt re-auth once
            try:
                posts = await search_posts_for_keyword(page, keyword)  # type: ignore[arg-type]
            except LinkedInAuthError as auth_exc:
                if _reauthed:
                    # Already re-authenticated once — give up
                    raise
                logger.warning(
                    "session_expired_retrying_login",
                    keyword=keyword,
                    error=str(auth_exc),
                )
                from playwright_linkedin.auth import login as _relogin  # noqa: PLC0415

                await _relogin(page.context)  # type: ignore[arg-type]
                _reauthed = True
                logger.info("session_refreshed_retrying", keyword=keyword)
                posts = await search_posts_for_keyword(page, keyword)  # type: ignore[arg-type]

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
