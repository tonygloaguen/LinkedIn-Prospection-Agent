"""LinkedIn post and people search via Playwright."""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import structlog
from playwright.async_api import Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.exceptions import PlaywrightTimeoutError, PostSearchError
from models.post import Post
from utils.anti_detection import simulate_human_scroll

logger = structlog.get_logger(__name__)

_BASE_SEARCH_URL = (
    "https://www.linkedin.com/search/results/content/"
    "?keywords={keywords}&sortBy=date"
)
_TIMEOUT = 60_000
_MAX_POSTS_PER_KEYWORD = 10
_MAX_COMMENTERS_PER_POST = 3


def _build_search_url(keyword: str) -> str:
    """Build the LinkedIn content search URL for a keyword.

    Args:
        keyword: Search term to encode.

    Returns:
        Full search URL string.
    """
    encoded = urllib.parse.quote(keyword)
    return _BASE_SEARCH_URL.format(keywords=encoded)


async def _extract_post_author_url(page: Page, post_element: object) -> Optional[str]:
    """Extract the author profile URL from a post element.

    Args:
        page: Playwright Page.
        post_element: Playwright element handle for the post card.

    Returns:
        LinkedIn profile URL or None.
    """
    try:
        # Author link selectors (LinkedIn changes these often)
        selectors = [
            "a.app-aware-link[href*='/in/']",
            "a[data-control-name='actor'] ",
            ".update-components-actor__meta a[href*='/in/']",
        ]
        for sel in selectors:
            el = await post_element.query_selector(sel)  # type: ignore[attr-defined]
            if el:
                href = await el.get_attribute("href")
                if href and "/in/" in href:
                    # Normalize to base profile URL
                    parts = href.split("?")[0].rstrip("/")
                    return parts
    except Exception:
        pass
    return None


async def _extract_post_url(post_element: object) -> Optional[str]:
    """Extract the direct URL of a post.

    Args:
        post_element: Playwright element handle for the post card.

    Returns:
        Post URL or None.
    """
    try:
        selectors = [
            "a[href*='/posts/']",
            "a[href*='/activity/']",
            "a[data-control-name='feed_detail_shares']",
        ]
        for sel in selectors:
            el = await post_element.query_selector(sel)  # type: ignore[attr-defined]
            if el:
                href = await el.get_attribute("href")
                if href:
                    return href.split("?")[0]
    except Exception:
        pass
    return None


async def _extract_post_snippet(post_element: object) -> Optional[str]:
    """Extract text content snippet from a post.

    Args:
        post_element: Playwright element handle for the post card.

    Returns:
        First 300 characters of post content or None.
    """
    try:
        selectors = [
            ".feed-shared-update-v2__description",
            ".update-components-text",
            ".feed-shared-text",
        ]
        for sel in selectors:
            el = await post_element.query_selector(sel)  # type: ignore[attr-defined]
            if el:
                text = await el.inner_text()
                if text:
                    return text[:300].strip()
    except Exception:
        pass
    return None


@retry(
    retry=retry_if_exception_type(PostSearchError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def search_posts_for_keyword(page: Page, keyword: str) -> list[Post]:
    """Search LinkedIn for posts matching a keyword and extract post data.

    Args:
        page: Authenticated Playwright Page.
        keyword: Search keyword.

    Returns:
        List of Post objects extracted from the search results.

    Raises:
        PostSearchError: If the search page fails to load.
    """
    url = _build_search_url(keyword)
    logger.info("searching_posts", keyword=keyword, url=url)

    try:
        await page.goto(url, timeout=_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        await simulate_human_scroll(page, scroll_count=3)
    except Exception as exc:
        raise PostSearchError(f"Failed to load search page for '{keyword}': {exc}") from exc

    posts: list[Post] = []
    seen_urls: set[str] = set()

    try:
        # Try multiple container selectors
        post_selectors = [
            ".feed-shared-update-v2",
            ".occludable-update",
            "[data-urn]",
        ]
        post_elements = []
        for sel in post_selectors:
            post_elements = await page.query_selector_all(sel)
            if post_elements:
                break

        now = datetime.now(timezone.utc).isoformat()

        for element in post_elements[:_MAX_POSTS_PER_KEYWORD]:
            try:
                author_url = await _extract_post_author_url(page, element)
                post_url = await _extract_post_url(element)
                snippet = await _extract_post_snippet(element)

                if not author_url or not post_url:
                    continue
                if post_url in seen_urls:
                    continue

                seen_urls.add(post_url)
                post = Post(
                    post_url=post_url,
                    author_linkedin_url=author_url,
                    content_snippet=snippet,
                    keywords_matched=[keyword],
                    found_at=now,
                )
                posts.append(post)

            except Exception as exc:
                logger.warning("post_extraction_error", keyword=keyword, error=str(exc))
                continue

    except Exception as exc:
        logger.error("search_results_extraction_failed", keyword=keyword, error=str(exc))

    logger.info("search_posts_done", keyword=keyword, count=len(posts))
    return posts
