"""LinkedIn profile scraping via Playwright."""

from __future__ import annotations

import os
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

# ── Error classification ──────────────────────────────────────────────────────

_LOGIN_URL_MARKERS = ("/login", "/uas/login", "/checkpoint", "/authwall")
_CHALLENGE_URL_MARKERS = ("/challenge",)
_CHALLENGE_CONTENT_MARKERS = (
    "security check", "unusual activity", "verify you're a human",
    "captcha", "vérification de sécurité", "let's do a quick",
    "we noticed unusual", "identity verification",
)
_UNAVAILABLE_CONTENT_MARKERS = (
    "page not found", "this profile is not available",
    "this linkedin page isn't available", "profil introuvable",
    "n'est pas disponible", "no longer available",
)


def _classify_profile_error(
    final_url: str,
    title: str,
    html_snippet: str,
) -> str:
    """Map observable page signals to an actionable error category.

    Categories (used as the ProfileScrapingError prefix):
      - profile_redirected_to_login   → session expired
      - profile_challenge_detected    → bot wall / captcha / security check
      - profile_unavailable           → deleted / private / geo-blocked
      - profile_timeout_dom_incomplete → h1 never rendered (slow RPi, React lag)
    """
    combined = f"{final_url} {title} {html_snippet}".lower()

    if any(m in final_url for m in _LOGIN_URL_MARKERS):
        return "profile_redirected_to_login"

    if any(m in final_url for m in _CHALLENGE_URL_MARKERS):
        return "profile_challenge_detected"

    if any(m in combined for m in _CHALLENGE_CONTENT_MARKERS):
        return "profile_challenge_detected"

    if any(m in combined for m in _UNAVAILABLE_CONTENT_MARKERS):
        return "profile_unavailable"

    return "profile_timeout_dom_incomplete"


async def _save_debug_snapshot(page: Page, url: str, category: str) -> None:
    """Save screenshot + partial HTML to SCRAPING_DEBUG_DIR on failure.

    Only runs when env var SCRAPING_DEBUG=1 to avoid filling RPi disk.

    Args:
        page: Current Playwright page.
        url: Requested profile URL (used to build filename).
        category: Classified error category (used as filename prefix).
    """
    debug_dir = os.environ.get("SCRAPING_DEBUG_DIR", "/data/debug")
    os.makedirs(debug_dir, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]", "_", url.lower())[-40:]
    ts = datetime.now().strftime("%H%M%S")
    prefix = f"{debug_dir}/{ts}_{category}_{slug}"

    try:
        await page.screenshot(path=f"{prefix}.png", full_page=False)
    except Exception as e:
        logger.warning("debug_screenshot_failed", error=str(e))

    try:
        html = await page.content()
        with open(f"{prefix}.html", "w", encoding="utf-8") as f:
            f.write(html[:8000])  # first 8KB is enough for diagnosis
    except Exception:
        pass

    logger.info("debug_snapshot_saved", prefix=prefix, category=category)


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


def _is_login_page(url: str) -> bool:
    """Return True if the current URL is a LinkedIn login/auth page."""
    return any(marker in url for marker in _LOGIN_URL_MARKERS)


async def _wait_for_profile_ready(page: Page, url: str) -> None:
    """Wait until the profile page is rendered or raise a classified error.

    LinkedIn is a React SPA — domcontentloaded fires before React mounts.
    We wait for h1 as the render signal, then classify any failure precisely
    so callers can act on the category (retry, pause, abort session…).

    On failure, logs: final_url, page title, 300-char HTML preview.
    If SCRAPING_DEBUG=1 also saves screenshot + full HTML to SCRAPING_DEBUG_DIR.

    Args:
        page: Playwright Page after goto().
        url: The requested profile URL (for error context).

    Raises:
        ProfileScrapingError: Prefixed with the classified error category.
    """
    # Fast-path: immediate redirect to login before h1 wait
    if _is_login_page(page.url):
        raise ProfileScrapingError(f"profile_redirected_to_login: {url} → {page.url}")

    try:
        await page.wait_for_selector("h1", timeout=15_000)
        return  # success — h1 is present
    except Exception:
        pass

    # h1 timed out — gather diagnostic signals
    final_url = page.url
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        html_snippet = (await page.content())[:3000]
    except Exception:
        html_snippet = ""

    category = _classify_profile_error(final_url, title, html_snippet)

    logger.warning(
        "profile_load_failed",
        url=url,
        final_url=final_url,
        title=title,
        category=category,
        # First 300 chars of HTML — enough to see login form / challenge heading
        html_preview=html_snippet[:300].replace("\n", " "),
    )

    if os.environ.get("SCRAPING_DEBUG", "0") == "1":
        await _save_debug_snapshot(page, url, category)

    raise ProfileScrapingError(
        f"{category}: {url} (final_url={final_url!r}, title={title!r})"
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
    # Skip non-feed URLs — job listings and profile pages have no comment section
    _skip_patterns = ("/jobs/view/", "/jobs/search/", "/pulse/")
    for pattern in _skip_patterns:
        if pattern in post_url:
            logger.info("commenter_skip_non_feed", post_url=post_url, reason=pattern)
            return []

    # If post_url is a profile page (author fallback), skip it too
    try:
        from urllib.parse import urlparse

        _path = urlparse(post_url).path
        if _path.startswith("/in/") or _path.startswith("/company/"):
            logger.info("commenter_skip_profile_url", post_url=post_url)
            return []
    except Exception:
        pass

    logger.info("scraping_commenters", post_url=post_url)
    urls: list[str] = []

    try:
        await page.goto(post_url, timeout=_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(3_000)
        await simulate_human_scroll(page, scroll_count=3)
        await page.wait_for_timeout(2_000)

        # Try clicking "View more comments" if present
        for view_more_sel in [
            "button.comments-comments-list__load-more-comments-button",
            "button[aria-label*='comment']",
            "button:has-text('View')",
            "button:has-text('Voir')",
        ]:
            try:
                btn = page.locator(view_more_sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

        # 2025 LinkedIn comment selectors (ordered: most specific → most generic)
        selectors = [
            # 2025 primary: article-based comment items
            "article.comments-comment-item a[href*='/in/']",
            ".comments-comment-item .comments-post-meta a[href*='/in/']",
            ".comments-comment-item a.app-aware-link[href*='/in/']",
            # 2024 variants
            ".comments-comment-item__post-meta a[href*='/in/']",
            ".comments-post-meta__profile-link",
            # Broader container fallback
            ".comments-comments-list a[href*='/in/']",
            # Legacy
            "a[data-control-name='comment_actor_name']",
            # Very broad — any /in/ link inside a comment context
            "[data-test-id*='comment'] a[href*='/in/']",
        ]

        for sel in selectors:
            elements = await page.query_selector_all(sel)
            for el in elements[: max_commenters * 2]:
                href = await el.get_attribute("href")
                if href and "/in/" in href:
                    url = href.split("?")[0].rstrip("/")
                    if url not in urls:
                        urls.append(url)
                        if len(urls) >= max_commenters:
                            break
            if urls:
                break

    except Exception as exc:
        logger.warning("commenter_scraping_failed", post_url=post_url, error=str(exc))

    logger.info("commenters_found", count=len(urls), post_url=post_url)
    return urls[:max_commenters]
