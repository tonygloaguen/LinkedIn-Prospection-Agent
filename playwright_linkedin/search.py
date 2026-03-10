"""LinkedIn post search via Playwright."""

from __future__ import annotations

import re
import urllib.parse
from datetime import UTC, datetime

import structlog
from playwright.async_api import ElementHandle, Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.exceptions import LinkedInSessionExpiredError, PostSearchError
from models.post import Post
from utils.anti_detection import simulate_human_scroll

logger = structlog.get_logger(__name__)

# LinkedIn encodes sortBy with quotes: sortBy=%22date%22
_BASE_SEARCH_URL = (
    "https://www.linkedin.com/search/results/content/?keywords={keywords}&sortBy=%22date%22"
)

_TIMEOUT = 60_000
_MAX_POSTS_PER_KEYWORD = 10

# ── Selector chains ────────────────────────────────────────────────────────────
# Tried in priority order; first one returning ≥1 elements wins.
# Log which selector worked to detect silent LinkedIn DOM changes.

# Post container selectors (outer card element)
_POST_CONTAINER_SELECTORS: list[str] = [
    # Priority 1: URN-based — tied to LinkedIn's internal IDs, most stable
    "[data-urn*='urn:li:activity']",
    "[data-chameleon-result-urn*='urn:li:activity']",
    # Priority 2: structural wrappers introduced ~2023
    ".fie-impression-container",
    ".occludable-update",
    # Priority 3: legacy class names (may still exist in some A/B variants)
    ".feed-shared-update-v2",
]

# Selector to wait for before attempting extraction (any of these = results loaded)
_RESULTS_READY_SELECTOR = (
    "[data-urn*='urn:li:activity'], "
    "[data-chameleon-result-urn*='urn:li:activity'], "
    ".fie-impression-container, "
    ".occludable-update, "
    ".feed-shared-update-v2"
)

# Post URL link selectors (inside a post card)
_POST_URL_SELECTORS: list[str] = [
    "a[href*='/feed/update/']",  # Modern format: /feed/update/urn:li:activity:…
    "a[href*='/posts/']",  # Alternative: /posts/name_activity-…
    "a[href*='/activity/']",  # Old format
    "a[data-control-name='feed_detail_shares']",
]

# Author profile URL selectors (inside a post card)
_AUTHOR_URL_SELECTORS: list[str] = [
    ".update-components-actor__meta-link",  # 2023+ primary
    "a.update-components-actor__name[href*='/in/']",
    ".update-components-actor__name a[href*='/in/']",
    "a.app-aware-link[href*='/in/']",  # broad fallback
    "a[data-control-name='actor']",
    ".update-components-actor__meta a[href*='/in/']",
]

# Post text content selectors (inside a post card)
_SNIPPET_SELECTORS: list[str] = [
    ".update-components-text span[dir='ltr']",  # 2024+ primary
    ".update-components-text",
    ".feed-shared-update-v2__description",
    ".feed-shared-text span",
    ".feed-shared-text",
]

# Text artefacts injected by LinkedIn's "see more" button
_ARTIFACT_RE = re.compile(
    r"\s*(…voir plus|voir plus|…see more|see more|\.\.\.|\u2026)\s*$",
    re.IGNORECASE,
)


def _build_search_url(keyword: str) -> str:
    """Build the LinkedIn content search URL for a keyword."""
    encoded = urllib.parse.quote(keyword)
    return _BASE_SEARCH_URL.format(keywords=encoded)


def _clean_snippet(text: str, max_len: int = 500) -> str:
    """Remove DOM artefacts and truncate snippet text."""
    text = text.strip()
    text = _ARTIFACT_RE.sub("", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def _is_login_redirect(url: str) -> bool:
    """Return True if the URL indicates a redirect to the LinkedIn login page."""
    return "/uas/login" in url or (
        "/login" in url and "linkedin.com" in url and "/search/" not in url
    )


async def _extract_post_author_url(post_element: ElementHandle) -> str | None:
    """Extract the author profile URL from a post card element.

    Args:
        post_element: Playwright element handle for the post card.

    Returns:
        Normalised LinkedIn /in/ profile URL, or None if not found.
    """
    try:
        for sel in _AUTHOR_URL_SELECTORS:
            el = await post_element.query_selector(sel)
            if el:
                href = await el.get_attribute("href")
                if href and "/in/" in href:
                    # Strip query params and trailing slash
                    return str(href.split("?")[0].rstrip("/"))
    except Exception:
        pass
    return None


async def _extract_post_url(post_element: ElementHandle) -> str | None:
    """Extract the canonical URL of a post card.

    Args:
        post_element: Playwright element handle for the post card.

    Returns:
        Post URL without query parameters, or None if not found.
    """
    try:
        for sel in _POST_URL_SELECTORS:
            el = await post_element.query_selector(sel)
            if el:
                href = await el.get_attribute("href")
                if href:
                    return str(href.split("?")[0].rstrip("/"))
    except Exception:
        pass
    return None


async def _extract_post_snippet(post_element: ElementHandle) -> str | None:
    """Extract and clean text content from a post card.

    Args:
        post_element: Playwright element handle for the post card.

    Returns:
        Cleaned text snippet (≤500 chars), or None if not found.
    """
    try:
        for sel in _SNIPPET_SELECTORS:
            el = await post_element.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and text.strip():
                    return _clean_snippet(text)
    except Exception:
        pass
    return None


async def _find_post_elements(page: Page, keyword: str) -> tuple[list[ElementHandle], str]:
    """Try each container selector and return the first non-empty result set.

    Args:
        page: Playwright Page with search results loaded.
        keyword: Keyword (for logging only).

    Returns:
        Tuple of (element list, selector that worked).
        Returns ([], "") if no selector matched.
    """
    for sel in _POST_CONTAINER_SELECTORS:
        elements = await page.query_selector_all(sel)
        if elements:
            logger.info(
                "post_container_found",
                keyword=keyword,
                selector=sel,
                count=len(elements),
            )
            return elements, sel
    return [], ""


@retry(
    retry=retry_if_exception_type(PostSearchError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def search_posts_for_keyword(page: Page, keyword: str) -> list[Post]:
    """Search LinkedIn for posts matching a keyword and extract post data.

    Strategy:
        1. Navigate with domcontentloaded (fast — avoids SPA networkidle trap).
        2. Detect redirect to /uas/login → raise LinkedInSessionExpiredError immediately.
        3. Wait for any known post container selector to appear (explicit signal).
        4. Scroll 5× to trigger lazy-loading of additional cards.
        5. Try each container selector; log which one succeeded.
        6. Extract URL, author URL, snippet from each card.

    Args:
        page: Authenticated Playwright Page.
        keyword: Search keyword.

    Returns:
        List of Post objects extracted from the search results.

    Raises:
        LinkedInSessionExpiredError: If the session has expired (login redirect detected).
        PostSearchError: If the search page fails to load or navigate for any other reason.
    """
    url = _build_search_url(keyword)
    logger.info("searching_posts", keyword=keyword, url=url)

    # ── 1. Navigate ────────────────────────────────────────────────────────────
    try:
        await page.goto(url, timeout=_TIMEOUT, wait_until="domcontentloaded")
        final_url = page.url
        title = await page.title()
        logger.info(
            "search_page_loaded",
            keyword=keyword,
            final_url=final_url,
            title=title,
        )
    except Exception as exc:
        raise PostSearchError(f"Failed to load search page for '{keyword}': {exc}") from exc

    # ── 2. Detect session expiry (redirect to login) ────────────────────────────
    final_url = page.url
    if _is_login_redirect(final_url):
        logger.warning(
            "session_expired_detected",
            keyword=keyword,
            redirect_url=final_url,
        )
        raise LinkedInSessionExpiredError(
            f"Session expired — redirected to login for keyword '{keyword}'"
        )

    # ── 3. Wait for results to render ──────────────────────────────────────────
    try:
        await page.wait_for_selector(
            _RESULTS_READY_SELECTOR,
            timeout=15_000,
            state="attached",
        )
        logger.debug("results_selector_appeared", keyword=keyword)
    except Exception:
        # LinkedIn may be slow or the selector chain changed — proceed anyway
        logger.warning("results_wait_timeout", keyword=keyword)
        await page.wait_for_timeout(3_000)

    # ── 4. Scroll to trigger lazy-loading ──────────────────────────────────────
    await simulate_human_scroll(page, scroll_count=5)
    await page.wait_for_timeout(2_000)

    # ── 5. Find post elements ──────────────────────────────────────────────────
    posts: list[Post] = []
    seen_urls: set[str] = set()
    now = datetime.now(UTC).isoformat()

    post_elements, active_selector = await _find_post_elements(page, keyword)

    if not post_elements:
        logger.warning(
            "no_post_container_found",
            keyword=keyword,
            tried_selectors=_POST_CONTAINER_SELECTORS,
            final_url=page.url,
        )
        return posts

    # ── 6. Extract data from each card ────────────────────────────────────────
    now = datetime.now(UTC).isoformat()

    for element in post_elements[:_MAX_POSTS_PER_KEYWORD]:
        try:
            author_url = await _extract_post_author_url(element)
            post_url = await _extract_post_url(element)
            snippet = await _extract_post_snippet(element)

            if not author_url or not post_url:
                logger.debug(
                    "post_skipped_missing_fields",
                    keyword=keyword,
                    has_author=author_url is not None,
                    has_url=post_url is not None,
                )
                continue

            if post_url in seen_urls:
                continue

            seen_urls.add(post_url)
            posts.append(
                Post(
                    post_url=post_url,
                    author_linkedin_url=author_url,
                    content_snippet=snippet,
                    keywords_matched=[keyword],
                    found_at=now,
                )
            )

        except Exception as exc:
            logger.warning(
                "post_extraction_error",
                keyword=keyword,
                selector=active_selector,
                error=str(exc),
            )

    logger.info(
        "search_posts_done",
        keyword=keyword,
        cards_found=len(post_elements),
        posts_extracted=len(posts),
        active_selector=active_selector,
    )
    return posts
