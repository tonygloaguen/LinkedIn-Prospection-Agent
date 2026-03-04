"""LinkedIn profile scraping via Playwright."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import structlog
from playwright.async_api import Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.exceptions import ProfileScrapingError
from models.profile import Profile
from utils.anti_detection import simulate_human_scroll

logger = structlog.get_logger(__name__)

_TIMEOUT = 60_000


async def _safe_inner_text(page: Page, selector: str) -> str | None:
    """Safely extract inner text from a selector, returning None on failure.

    Args:
        page: Playwright Page.
        selector: CSS selector.

    Returns:
        Inner text string or None if element not found.
    """
    try:
        el = await page.query_selector(selector)
        if el:
            text = await el.inner_text()
            return text.strip() if text else None
    except Exception:
        pass
    return None


async def _extract_connections_count(page: Page) -> int | None:
    """Extract the connections count from the profile sidebar.

    Args:
        page: Playwright Page on a LinkedIn profile.

    Returns:
        Connections count integer or None.
    """
    try:
        selectors = [
            ".pv-top-card--list-bullet li:last-child",
            ".pv-top-card-v2-ctas__text",
            "span.t-bold:has-text('connexions')",
            "span.t-bold:has-text('connections')",
        ]
        for sel in selectors:
            text = await _safe_inner_text(page, sel)
            if text:
                # Extract numeric value from "500+ connexions" or "234 connexions"
                match = re.search(r"(\d[\d,+]+)", text)
                if match:
                    raw = match.group(1).replace(",", "").replace("+", "")
                    return int(raw)
    except Exception:
        pass
    return None


@retry(
    retry=retry_if_exception_type(ProfileScrapingError),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    reraise=True,
)
async def scrape_profile(page: Page, linkedin_url: str) -> Profile:
    """Scrape a LinkedIn profile page and return a Profile object.

    Handles private profiles gracefully (returns partial data).

    Args:
        page: Authenticated Playwright Page.
        linkedin_url: Full LinkedIn profile URL (https://www.linkedin.com/in/...).

    Returns:
        Profile object with scraped data.

    Raises:
        ProfileScrapingError: If the page fails to load or is inaccessible.
    """
    logger.info("scraping_profile", url=linkedin_url)

    try:
        await page.goto(linkedin_url, timeout=_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        await simulate_human_scroll(page, scroll_count=2)
    except Exception as exc:
        raise ProfileScrapingError(f"Failed to load profile page {linkedin_url}: {exc}") from exc

    now = datetime.now(UTC).isoformat()

    # Full name
    full_name = await _safe_inner_text(page, "h1.text-heading-xlarge, h1[class*='inline t-24']")

    # Headline
    headline = await _safe_inner_text(
        page,
        ".text-body-medium.break-words, div[class*='pv-text-details__left-panel'] div:nth-child(2)",
    )

    # Bio / About section
    bio: str | None = None
    bio_selectors = [
        "#about ~ div .display-flex span[aria-hidden='true']",
        ".pv-shared-text-with-see-more span[aria-hidden='true']",
        "section.pv-about-section p",
    ]
    for sel in bio_selectors:
        bio = await _safe_inner_text(page, sel)
        if bio:
            break

    # Location
    location = await _safe_inner_text(
        page, ".pv-top-card--list-bullet li:first-child, span.text-body-small.inline.t-black--light"
    )

    # Connections count
    connections_count = await _extract_connections_count(page)

    profile = Profile(
        linkedin_url=linkedin_url,
        full_name=full_name,
        headline=headline,
        bio=bio,
        location=location,
        connections_count=connections_count,
        scraped_at=now,
    )

    logger.info(
        "profile_scraped",
        url=linkedin_url,
        name=full_name,
        has_bio=bio is not None,
    )

    return profile


async def scrape_commenters(page: Page, post_url: str, max_commenters: int = 3) -> list[str]:
    """Extract top commenter profile URLs from a post page.

    Args:
        page: Authenticated Playwright Page.
        post_url: URL of the LinkedIn post.
        max_commenters: Maximum number of commenter URLs to return.

    Returns:
        List of commenter LinkedIn profile URLs.
    """
    logger.info("scraping_commenters", post_url=post_url)
    urls: list[str] = []

    try:
        await page.goto(post_url, timeout=_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        await simulate_human_scroll(page, scroll_count=2)

        # Find commenter profile links
        selectors = [
            ".comments-post-meta__profile-link",
            "a[data-control-name='comment_actor_name']",
            ".comments-comment-item__post-meta a[href*='/in/']",
        ]

        for sel in selectors:
            elements = await page.query_selector_all(sel)
            for el in elements[:max_commenters]:
                href = await el.get_attribute("href")
                if href and "/in/" in href:
                    url = href.split("?")[0].rstrip("/")
                    if url not in urls:
                        urls.append(url)
            if urls:
                break

    except Exception as exc:
        logger.warning("commenter_scraping_failed", post_url=post_url, error=str(exc))

    logger.info("commenters_found", count=len(urls), post_url=post_url)
    return urls[:max_commenters]
