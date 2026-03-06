"""LinkedIn post search via Playwright."""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime

import structlog
from playwright.async_api import Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.exceptions import PostSearchError
from models.post import Post
from utils.anti_detection import simulate_human_scroll

logger = structlog.get_logger(__name__)

_BASE_SEARCH_URL = (
    "https://www.linkedin.com/search/results/content/?keywords={keywords}&sortBy=date"
)

_TIMEOUT = 60_000
_MAX_POSTS_PER_KEYWORD = 10


def _build_search_url(keyword: str) -> str:
    encoded = urllib.parse.quote(keyword)
    return _BASE_SEARCH_URL.format(keywords=encoded)


def _normalize_linkedin_url(url: str) -> str:
    clean = url.split("?")[0]

    if clean.startswith("/"):
        clean = f"https://www.linkedin.com{clean}"

    return clean.rstrip("/")


async def _extract_post_author_url(post_element: object) -> str | None:
    try:
        selectors = [
            "a.app-aware-link[href*='/in/']",
            ".update-components-actor__meta a[href*='/in/']",
            "a[data-control-name='actor']",
        ]

        for sel in selectors:
            el = await post_element.query_selector(sel)  # type: ignore[attr-defined]
            if el:
                href = await el.get_attribute("href")

                if href and "/in/" in href:
                    return _normalize_linkedin_url(href)

    except Exception:
        pass

    return None


async def _extract_post_url(post_element: object) -> str | None:
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
                    return _normalize_linkedin_url(href)

    except Exception:
        pass

    return None


async def _extract_post_snippet(post_element: object) -> str | None:
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
    url = _build_search_url(keyword)

    logger.info("searching_posts", keyword=keyword, url=url)

    try:
        await page.goto(url, timeout=_TIMEOUT, wait_until="domcontentloaded")

        await page.wait_for_timeout(3000)

        await simulate_human_scroll(page, scroll_count=3)

    except Exception as exc:
        raise PostSearchError(
            f"Failed to load search page for '{keyword}': {exc}"
        ) from exc

    logger.info(
        "search_page_loaded",
        keyword=keyword,
        final_url=page.url,
        title=await page.title(),
    )

    candidate_selectors = [
        "div.feed-shared-update-v2",
        "div.occludable-update",
        "div[data-urn*='urn:li:activity:']",
        "div[data-urn*='urn:li:ugcPost:']",
    ]

    found_selector = None

    for sel in candidate_selectors:
        try:
            count = await page.locator(sel).count()

            logger.info(
                "selector_probe",
                keyword=keyword,
                selector=sel,
                count=count,
            )

            if count > 0:
                found_selector = sel
                break

        except Exception as exc:
            logger.warning(
                "selector_probe_failed",
                keyword=keyword,
                selector=sel,
                error=str(exc),
            )

    if not found_selector:
        logger.warning(
            "no_post_container_found",
            keyword=keyword,
            final_url=page.url,
            title=await page.title(),
        )
        return []

    post_elements = await page.query_selector_all(found_selector)

    posts: list[Post] = []
    seen_urls: set[str] = set()

    now = datetime.now(UTC).isoformat()

    for element in post_elements[:_MAX_POSTS_PER_KEYWORD]:
        try:
            author_url = await _extract_post_author_url(element)
            post_url = await _extract_post_url(element)
            snippet = await _extract_post_snippet(element)

            logger.info(
                "post_candidate",
                keyword=keyword,
                author_url=author_url,
                post_url=post_url,
                snippet_present=bool(snippet),
            )

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
            logger.warning(
                "post_extraction_error",
                keyword=keyword,
                error=str(exc),
            )

    logger.info("search_posts_done", keyword=keyword, count=len(posts))

    return posts
