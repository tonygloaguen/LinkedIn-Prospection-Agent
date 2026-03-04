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


async def is_logged_in(page: Page) -> bool:
    """Check whether the current browser session is authenticated.

    Args:
        page: Active Playwright Page.

    Returns:
        True if the LinkedIn feed is accessible without redirect.
    """
    await page.goto(_FEED_URL, timeout=_TIMEOUT, wait_until="domcontentloaded")
    return "feed" in page.url


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
        await page.goto(_LOGIN_URL, timeout=_TIMEOUT, wait_until="domcontentloaded")

        # Fill credentials
        await page.fill("#username", email, timeout=_TIMEOUT)
        await page.fill("#password", password, timeout=_TIMEOUT)
        await page.click("button[type='submit']", timeout=_TIMEOUT)

        # Wait for redirect to feed or checkpoint
        await page.wait_for_url("**/feed/**", timeout=_TIMEOUT)

        if "feed" not in page.url:
            raise LinkedInAuthError(
                f"Login redirect did not reach feed, ended at: {page.url}"
            )

        logger.info("linkedin_login_success")
        return page

    except LinkedInAuthError:
        raise
    except Exception as exc:
        raise LinkedInAuthError(f"Login flow failed: {exc}") from exc
