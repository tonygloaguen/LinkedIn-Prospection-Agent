"""BrowserManager: Playwright browser context with stealth and session persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from utils.anti_detection import get_random_user_agent, get_random_viewport

logger = structlog.get_logger(__name__)

# Paris geolocation
_PARIS_GEO = {"latitude": 48.8566, "longitude": 2.3522, "accuracy": 50}


def _get_session_path() -> str:
    """Return the configured session cookie file path."""
    return os.environ.get("SESSION_PATH", "./data/session.json")


async def _save_cookies(context: BrowserContext) -> None:
    """Persist browser cookies to disk.

    Args:
        context: Active Playwright browser context.
    """
    session_path = Path(_get_session_path())
    session_path.parent.mkdir(parents=True, exist_ok=True)
    cookies = await context.cookies()
    session_path.write_text(json.dumps(cookies, indent=2))
    logger.info("cookies_saved", path=str(session_path))


async def _load_cookies(context: BrowserContext) -> bool:
    """Load persisted cookies into the browser context.

    Args:
        context: Active Playwright browser context.

    Returns:
        True if cookies were loaded, False if no session file exists.
    """
    session_path = Path(_get_session_path())
    if not session_path.exists():
        logger.debug("no_session_file", path=str(session_path))
        return False

    try:
        cookies = json.loads(session_path.read_text())
        await context.add_cookies(cookies)
        logger.info("cookies_loaded", path=str(session_path), count=len(cookies))
        return True
    except Exception as exc:
        logger.warning("cookie_load_failed", error=str(exc))
        return False


async def get_browser_context(playwright: Playwright) -> tuple[Browser, BrowserContext]:
    """Create a Playwright browser and context with stealth settings.

    Configures headless Chromium with RPi-optimised launch args,
    random user-agent, random viewport, Paris geolocation, and
    loads persisted cookies if available.

    Args:
        playwright: Active Playwright instance.

    Returns:
        Tuple of (Browser, BrowserContext).
    """
    user_agent = get_random_user_agent()
    viewport = get_random_viewport()

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--single-process",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )

    context = await browser.new_context(
        user_agent=user_agent,
        viewport=viewport,
        geolocation=_PARIS_GEO,
        locale="fr-FR",
        timezone_id="Europe/Paris",
        permissions=["geolocation"],
        extra_http_headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )

    # Apply stealth patches
    try:
        from playwright_stealth import stealth_async  # type: ignore[import]

        page = await context.new_page()
        await stealth_async(page)
        await page.close()
    except ImportError:
        logger.warning("playwright_stealth_not_installed")

    await _load_cookies(context)

    logger.info(
        "browser_context_created",
        user_agent=user_agent[:40],
        viewport=viewport,
    )

    return browser, context


class BrowserManager:
    """Async context manager for a Playwright browser session.

    Usage:
        async with BrowserManager() as (browser, context):
            page = await context.new_page()
            ...
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> tuple[Browser, BrowserContext]:
        """Start Playwright and create browser context."""
        self._playwright = await async_playwright().start()
        self._browser, self._context = await get_browser_context(self._playwright)
        return self._browser, self._context

    async def __aexit__(self, *args: object) -> None:
        """Save cookies and close browser resources."""
        if self._context:
            await _save_cookies(self._context)
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_closed")
