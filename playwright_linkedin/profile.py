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


async def _extract_name_js(page: Page) -> str | None:
    """Extract full name via JavaScript — more resilient to DOM class changes.

    Strategy (in order):
      1. First <h1> on the page (LinkedIn profile always has exactly one).
      2. Any element with aria-label containing the person's name pattern.
      3. <title> tag (format: "Firstname Lastname | LinkedIn").

    Args:
        page: Playwright Page on a LinkedIn profile.

    Returns:
        Full name string or None.
    """
    try:
        name: str | None = await page.evaluate(
            """
            () => {
                // 1. First h1 — always the profile name on /in/ pages
                const h1 = document.querySelector('h1');
                if (h1 && h1.innerText && h1.innerText.trim().length > 1) {
                    return h1.innerText.trim();
                }
                // 2. data-generated-suggestion or aria-label on known wrappers
                const labeled = document.querySelector(
                    '[aria-label][class*="profile"], [data-member-id] h1'
                );
                if (labeled) {
                    const t = labeled.innerText || labeled.getAttribute('aria-label');
                    if (t && t.trim().length > 1) return t.trim();
                }
                // 3. <title> fallback: "Name | LinkedIn" or "Name - LinkedIn"
                const title = document.title || '';
                const titleMatch = title.match(/^([^|\\-]+)[|\\-]/);
                if (titleMatch) {
                    const candidate = titleMatch[1].trim();
                    if (candidate.length > 1 && !candidate.toLowerCase().includes('linkedin')) {
                        return candidate;
                    }
                }
                return null;
            }
            """
        )
        return name if name else None
    except Exception:
        return None


async def _extract_bio_js(page: Page) -> str | None:
    """Extract the About/bio section via JavaScript.

    Strategy:
      1. Section with id="about" — look for the visible text span inside.
      2. Any element with data-generated-suggestion (LinkedIn's about section marker).
      3. CSS selector chain as last resort.

    Args:
        page: Playwright Page on a LinkedIn profile.

    Returns:
        Bio text string or None.
    """
    try:
        bio: str | None = await page.evaluate(
            """
            () => {
                // 1. #about anchor → navigate to the parent section and find text
                const aboutAnchor = document.getElementById('about');
                if (aboutAnchor) {
                    // The about section is typically a sibling or nearby section
                    let el = aboutAnchor.closest('section');
                    if (!el) {
                        // Try next section sibling of the anchor's parent
                        let parent = aboutAnchor.parentElement;
                        while (parent && parent.tagName !== 'SECTION') {
                            parent = parent.parentElement;
                        }
                        el = parent;
                    }
                    if (el) {
                        // Prefer aria-hidden=true spans (hidden from SR but visible)
                        const hiddenSpan = el.querySelector("span[aria-hidden='true']");
                        const txt = hiddenSpan && hiddenSpan.innerText;
                        if (txt && txt.trim().length > 10) {
                            return hiddenSpan.innerText.trim();
                        }
                        // Fallback: any <p> or <span> with substantial text
                        const spans = el.querySelectorAll('span, p');
                        for (const s of spans) {
                            const t = s.innerText ? s.innerText.trim() : '';
                            if (t.length > 20) return t;
                        }
                    }
                }
                // 2. data-generated-suggestion attribute (stable LinkedIn marker)
                const suggested = document.querySelector('[data-generated-suggestion]');
                if (suggested && suggested.innerText) return suggested.innerText.trim();

                // 3. pv-shared-text-with-see-more (legacy but still deployed in some variants)
                const sharedText = document.querySelector(
                    '.pv-shared-text-with-see-more span[aria-hidden="true"]'
                );
                if (sharedText && sharedText.innerText) return sharedText.innerText.trim();

                return null;
            }
            """
        )
        return bio if bio and len(bio) > 5 else None
    except Exception:
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
def _is_login_page(url: str) -> bool:
    """Return True if the current URL is a LinkedIn login/auth page."""
    return any(marker in url for marker in ("/login", "/uas/login", "/checkpoint", "/authwall"))


async def _wait_for_profile_ready(page: Page, url: str) -> None:
    """Wait until the profile page is actually rendered (h1 visible) or raise.

    LinkedIn is a React SPA — domcontentloaded fires before React mounts.
    We wait for the h1 (profile name) as a reliable render signal.

    Args:
        page: Playwright Page after goto().
        url: The requested profile URL (used for error context).

    Raises:
        ProfileScrapingError: If session expired or page failed to render.
    """
    current_url = page.url
    if _is_login_page(current_url):
        raise ProfileScrapingError(f"session_expired: redirected to login instead of {url}")

    try:
        await page.wait_for_selector("h1", timeout=15_000)
    except Exception:
        # h1 not found — may be a bot challenge or empty page
        current_url = page.url
        if _is_login_page(current_url):
            raise ProfileScrapingError(f"session_expired: redirected to login instead of {url}")
        raise ProfileScrapingError(f"profile_not_rendered: h1 never appeared for {url} (bot wall?)")


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
        await _wait_for_profile_ready(page, linkedin_url)
        # Scroll enough to trigger lazy-loaded sections (About, Experience)
        await simulate_human_scroll(page, scroll_count=4)
        # Extra pause after scroll so React renders lazy sections
        await page.wait_for_timeout(1_500)
    except ProfileScrapingError:
        raise
    except Exception as exc:
        raise ProfileScrapingError(f"Failed to load profile page {linkedin_url}: {exc}") from exc

    now = datetime.now(UTC).isoformat()

    # Full name — JS extraction first (stable), CSS chain as fallback
    full_name = await _extract_name_js(page)
    if not full_name:
        full_name = await _safe_inner_text(
            page,
            "h1.text-heading-xlarge, h1[class*='inline t-24'], h1",
        )

    if not full_name:
        logger.warning(
            "profile_name_null",
            url=linkedin_url,
            hint="LinkedIn DOM may have changed — JS and CSS fallbacks both failed",
        )

    # Headline — CSS selectors + JS fallback
    headline = await _safe_inner_text(
        page,
        ".text-body-medium.break-words, div[class*='pv-text-details__left-panel'] div:nth-child(2)",
    )
    if not headline:
        try:
            headline = await page.evaluate(
                """
                () => {
                    // Headline is typically the 2nd significant text block under h1
                    const h1 = document.querySelector('h1');
                    if (!h1) return null;
                    let el = h1.nextElementSibling;
                    while (el) {
                        const t = el.innerText ? el.innerText.trim() : '';
                        if (t.length > 3) return t;
                        el = el.nextElementSibling;
                    }
                    return null;
                }
                """
            )
        except Exception:
            pass

    # Bio / About section — JS extraction first, CSS chain as fallback
    bio = await _extract_bio_js(page)
    if not bio:
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
