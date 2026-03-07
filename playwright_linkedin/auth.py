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

        # Handle "choose account" page (Bon retour parmi nous / Welcome back)
        # LinkedIn shows this when cookies are recognized but session needs re-auth
        page_title = (await page.title()).lower()
        is_choose_account = (
            "retour" in page_title
            or "welcome back" in page_title
            or "choose" in page_title
        )
        if not is_choose_account:
            # Also check by looking for the account picker element
            try:
                picker = page.locator(".sign-in-form__account-picker, [data-test-id='choose-account-btn'], .account-picker").first
                if await picker.is_visible(timeout=3000):
                    is_choose_account = True
            except Exception:
                pass

        if is_choose_account:
            logger.info("linkedin_choose_account_page_detected")
            # Click the first account card (our account)
            choose_selectors = [
                ".sign-in-form__account-picker li:first-child button",
                ".account-picker__account-btn",
                "[data-test-id='choose-account-btn']",
                "li.account-picker__account button",
                "button.sign-in-form__account-btn",
            ]
            clicked = False
            for sel in choose_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click(timeout=5000)
                        await page.wait_for_load_state("domcontentloaded", timeout=_TIMEOUT)
                        await page.wait_for_timeout(2000)
                        logger.info("linkedin_choose_account_clicked", selector=sel)
                        clicked = True
                        break
                except Exception:
                    pass

            if not clicked:
                # Fallback: take screenshot and raise
                screenshot_path = "/logs/screenshots/choose_account_debug.png"
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                except Exception:
                    pass
                raise LinkedInAuthError(
                    f"Could not click account on choose-account page — url: {page.url}"
                )

            # After clicking account, check if we reached the feed or need password
            current_url = page.url
            if "feed" in current_url:
                logger.info("linkedin_login_success")
                return page

        # Verify the login form is present (standard form or post-account-pick password)
        try:
            await page.wait_for_selector("#username, #password", timeout=15_000)
        except Exception:
            current_url = page.url
            title = await page.title()
            # Check if we somehow landed on the feed already
            if "feed" in current_url:
                logger.info("linkedin_login_success")
                return page
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

        # Fill credentials (only fields that are visible)
        try:
            username_field = page.locator("#username")
            if await username_field.is_visible(timeout=3000):
                await page.fill("#username", email, timeout=_TIMEOUT)
        except Exception:
            pass
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
