"""LinkedIn authentication: login flow and session persistence."""

from __future__ import annotations

import os

import structlog
from playwright.async_api import BrowserContext, Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from agent.exceptions import LinkedInAuthError

logger = structlog.get_logger(__name__)

_LOGIN_URL = "https://www.linkedin.com/login"
_FEED_URL = "https://www.linkedin.com/feed/"
_TIMEOUT = 60_000
_TEST_SEARCH_URL = "https://www.linkedin.com/search/results/content/?keywords=DevOps&sortBy=date"


async def is_logged_in(page: Page) -> bool:
    """Check whether the current browser session is authenticated."""
    await page.goto(_TEST_SEARCH_URL, timeout=_TIMEOUT, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    url = page.url.lower()
    title = (await page.title()).lower()

    if "/uas/login" in url or "/login" in url:
        return False

    if "s’identifier" in title or "sign in" in title:
        return False

    return "/search/results/content" in url


@retry(
    retry=retry_if_exception_type(LinkedInAuthError),
    stop=stop_after_attempt(2),
    wait=wait_fixed(5),
    reraise=True,
)
async def login(context: BrowserContext) -> Page:
    """Perform the LinkedIn login flow.

    Reads credentials from LINKEDIN_EMAIL and LINKEDIN_PASSWORD env vars.
    Saves cookies after successful login for session reuse.

    Args:
        context: Active Playwright browser context.

    Returns:
        Authenticated Page instance.

    Raises:
        LinkedInAuthError: If login fails or credentials are missing.
    """
    email = os.environ.get("LINKEDIN_EMAIL", "")
    password = os.environ.get("LINKEDIN_PASSWORD", "")

    if not email or not password:
        raise LinkedInAuthError(
            "LINKEDIN_EMAIL and LINKEDIN_PASSWORD environment variables must be set"
        )

    page = await context.new_page()

    # Check if already logged in via persisted cookies
    try:
        if await is_logged_in(page):
            logger.info("linkedin_session_reused")
            return page
    except Exception:
        pass

    logger.info("linkedin_login_start")

    try:
        await page.goto(_LOGIN_URL, timeout=_TIMEOUT, wait_until="load")
        await page.wait_for_timeout(2000)

        # Dismiss GDPR / cookie consent banner if present (LinkedIn EU)
        consent_selectors = [
            "button[data-tracking-control-name='ga-cookie.consent.accept.v3']",
            "button[action-type='ACCEPT']",
            "button.artdeco-button--primary[data-test-id='accept-btn']",
        ]
        for selector in consent_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=3000):
                    await btn.click(timeout=5000)
                    logger.info("linkedin_cookie_consent_dismissed", selector=selector)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        # Verify the login form is present
        try:
            await page.wait_for_selector("#username", timeout=15_000)
        except Exception:
            current_url = page.url
            title = await page.title()
            screenshot_path = "/logs/screenshots/login_debug.png"
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.warning(
                    "login_form_not_found",
                    url=current_url,
                    title=title,
                    screenshot=screenshot_path,
                )
            except Exception:
                logger.warning("login_form_not_found", url=current_url, title=title)
            raise LinkedInAuthError(
                f"Login form (#username) not found — page: {title!r} at {current_url}"
            )

        # Fill credentials
        await page.fill("#username", email, timeout=_TIMEOUT)
        await page.fill("#password", password, timeout=_TIMEOUT)
        await page.click("button[type='submit']", timeout=_TIMEOUT)

        # Wait for any navigation after submit (feed, checkpoint, or error)
        await page.wait_for_load_state("domcontentloaded", timeout=_TIMEOUT)
        await page.wait_for_timeout(2000)

        current_url = page.url
        logger.info("linkedin_login_redirect", url=current_url)

        if "feed" in current_url:
            logger.info("linkedin_login_success")
            return page

        if "/checkpoint/" in current_url or "/challenge/" in current_url:
            raise LinkedInAuthError(
                f"LinkedIn security checkpoint — manual verification required: {current_url}"
            )

        if "/login" in current_url or "/uas/" in current_url:
            raise LinkedInAuthError(
                f"Login returned to login page — wrong credentials or bot detection: {current_url}"
            )

        # Unknown URL — try navigating to feed to confirm session
        await page.goto(_FEED_URL, timeout=_TIMEOUT, wait_until="domcontentloaded")
        if "feed" in page.url:
            logger.info("linkedin_login_success")
            return page

        raise LinkedInAuthError(f"Login redirect did not reach feed, ended at: {page.url}")

    except LinkedInAuthError:
        raise
    except Exception as exc:
        raise LinkedInAuthError(f"Login flow failed: {exc}") from exc
