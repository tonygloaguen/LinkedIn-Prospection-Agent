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

# Wider net for GDPR / cookie consent banners (LinkedIn EU variants, 2024-2025).
# Ordered from most-specific to most-generic to avoid false positives.
_CONSENT_SELECTORS = [
    # Tracking-control buttons (old and new naming)
    "button[data-tracking-control-name='ga-cookie.consent.accept.v3']",
    "button[data-tracking-control-name='cookie-policy-accept']",
    # action-type attribute used in some EU variants
    "button[action-type='ACCEPT']",
    # artdeco modal accept — the most common current variant
    "button.artdeco-button--primary[data-test-id='accept-btn']",
    # Cookie-policy dialog accept button
    "button[data-id='cookie-policy-dialog-accept']",
    # Generic "Accept all" / "Tout accepter" text match (last resort)
    "button:has-text('Accept all')",
    "button:has-text('Tout accepter')",
    "button:has-text('Accepter')",
]

# Fallback selectors for the login form fields in case LinkedIn A/B-tests new IDs.
# Tried in order; the first visible one wins.
_USERNAME_SELECTORS = [
    "#username",
    "input[name='session_key']",
    "input[autocomplete='username']",
    "input[type='email']",
]
_PASSWORD_SELECTORS = [
    "#password",
    "input[name='session_password']",
    "input[autocomplete='current-password']",
    "input[type='password']",
]


async def _dump_page_debug(page: Page, label: str) -> None:
    """Save a screenshot + first 8 KB of body HTML to /logs for post-mortem."""
    base = f"/logs/screenshots/{label}"
    try:
        await page.screenshot(path=f"{base}.png", full_page=True)
    except Exception:
        pass
    try:
        body_html: str = await page.evaluate("document.body?.innerHTML ?? ''")
        with open(f"{base}.html", "w", encoding="utf-8") as fh:
            fh.write(body_html[:8192])
    except Exception:
        pass


async def _find_visible(page: Page, selectors: list[str], timeout: int = 3000) -> str | None:
    """Return the first selector from *selectors* that is currently visible, or None."""
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible(timeout=timeout):
                return sel
        except Exception:
            pass
    return None


async def is_logged_in(page: Page) -> bool:
    """Check whether the current browser session is authenticated."""
    await page.goto(_TEST_SEARCH_URL, timeout=_TIMEOUT, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    url = page.url.lower()
    title = (await page.title()).lower()

    if "/uas/login" in url or "/login" in url:
        return False

    if "s'identifier" in title or "sign in" in title:
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
        # networkidle gives JS-heavy consent overlays time to fully render before
        # we look for buttons. Falls back gracefully on timeout.
        try:
            await page.goto(_LOGIN_URL, timeout=_TIMEOUT, wait_until="networkidle")
        except Exception:
            # networkidle can time-out on slow connections; domcontentloaded is enough.
            await page.goto(_LOGIN_URL, timeout=_TIMEOUT, wait_until="domcontentloaded")

        # Extra settle time — RPi 4 is slow and JS consent overlays render late.
        await page.wait_for_timeout(3000)

        # ------------------------------------------------------------------
        # Step 1: Dismiss GDPR / cookie consent banner (LinkedIn EU variants)
        # ------------------------------------------------------------------
        consent_sel = await _find_visible(page, _CONSENT_SELECTORS, timeout=4000)
        if consent_sel:
            try:
                await page.locator(consent_sel).first.click(timeout=5000)
                logger.info("linkedin_cookie_consent_dismissed", selector=consent_sel)
                # Wait for the overlay to animate away before proceeding.
                await page.wait_for_timeout(2500)
            except Exception as exc:
                logger.warning(
                    "linkedin_consent_click_failed", selector=consent_sel, error=str(exc)
                )
        else:
            logger.info("linkedin_no_consent_banner_detected")

        # ------------------------------------------------------------------
        # Step 2: Handle "choose account" page (Welcome back / Bon retour)
        # ------------------------------------------------------------------
        page_title = (await page.title()).lower()
        is_choose_account = (
            "retour" in page_title
            or "welcome back" in page_title
            or "choose" in page_title
        )
        if not is_choose_account:
            try:
                _picker_sel = (
                    ".sign-in-form__account-picker,"
                    " [data-test-id='choose-account-btn'],"
                    " .account-picker"
                )
                picker = page.locator(_picker_sel).first
                if await picker.is_visible(timeout=3000):
                    is_choose_account = True
            except Exception:
                pass

        if is_choose_account:
            logger.info("linkedin_choose_account_page_detected")
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
                await _dump_page_debug(page, "choose_account_debug")
                raise LinkedInAuthError(
                    f"Could not click account on choose-account page — url: {page.url}"
                )

            if "feed" in page.url:
                logger.info("linkedin_login_success")
                return page

        # ------------------------------------------------------------------
        # Step 3: Locate login form fields with fallback selectors
        # ------------------------------------------------------------------
        username_sel = await _find_visible(page, _USERNAME_SELECTORS, timeout=20_000)
        password_sel = await _find_visible(page, _PASSWORD_SELECTORS, timeout=5_000)

        if not username_sel and not password_sel:
            current_url = page.url
            title = await page.title()
            if "feed" in current_url:
                logger.info("linkedin_login_success")
                return page
            await _dump_page_debug(page, "login_debug")
            logger.warning(
                "login_form_not_found",
                url=current_url,
                title=title,
                screenshot="/logs/screenshots/login_debug.png",
                html_dump="/logs/screenshots/login_debug.html",
            )
            raise LinkedInAuthError(
                f"Login form not found — page: {title!r} at {current_url}"
            )

        logger.info(
            "login_form_found",
            username_selector=username_sel,
            password_selector=password_sel,
        )

        # ------------------------------------------------------------------
        # Step 4: Fill credentials and submit
        # ------------------------------------------------------------------
        if username_sel:
            await page.fill(username_sel, email, timeout=_TIMEOUT)

        pw_sel = password_sel or _PASSWORD_SELECTORS[0]
        await page.fill(pw_sel, password, timeout=_TIMEOUT)
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
