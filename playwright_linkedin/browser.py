"""BrowserManager: Playwright browser context with stealth and session persistence."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Geolocation,
    Page,
    Playwright,
    SetCookieParam,
    async_playwright,
)

from utils.anti_detection import get_random_user_agent, get_random_viewport

logger = structlog.get_logger(__name__)

# Paris geolocation
_PARIS_GEO: Geolocation = {"latitude": 48.8566, "longitude": 2.3522, "accuracy": 50}


def _get_session_path() -> str:
    """Return the configured session cookie file path."""
    return os.environ.get("SESSION_PATH", "./data/session.json")


async def _save_cookies(context: BrowserContext) -> None:
    """Persist browser cookies to disk.

    Args:
        context: Active Playwright browser context.
    """
    session_path = Path(_get_session_path())
    try:
        session_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        logger.error(
            "cookies_save_permission_denied",
            path=str(session_path),
            fix="sudo chown -R 1000:1000 /opt/linkedin-agent/data",
        )
        raise
    cookies = await context.cookies()
    session_path.write_text(json.dumps(cookies, indent=2))
    logger.info("cookies_saved", path=str(session_path))


_VALID_SAME_SITE = {"Strict", "Lax", "None"}


def _sanitize_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize cookie sameSite values to Playwright-accepted values.

    Args:
        cookies: Raw cookie list loaded from session file.

    Returns:
        Cookie list with invalid sameSite values replaced by "None".
    """
    sanitized = []
    for cookie in cookies:
        c = dict(cookie)
        if c.get("sameSite") not in _VALID_SAME_SITE:
            c["sameSite"] = "None"
        sanitized.append(c)
    return sanitized


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
        cookies = _sanitize_cookies(cookies)
        cookies_typed: list[SetCookieParam] = [SetCookieParam(**c) for c in cookies]
        await context.add_cookies(cookies_typed)
        logger.info("cookies_loaded", path=str(session_path), count=len(cookies))
        return True
    except Exception as exc:
        logger.warning("cookie_load_failed", error=str(exc))
        return False


def _ensure_pkg_resources() -> None:
    """Polyfill pkg_resources if missing (setuptools >= 71 no longer places it in site-packages).

    playwright-stealth uses pkg_resources.resource_string() to read its own JS files.
    The files are present on disk inside the package directory — this polyfill bridges
    the gap without downgrading setuptools or changing the Docker image.
    """
    import importlib
    import sys
    import types

    if "pkg_resources" in sys.modules:
        return  # already importable — nothing to do

    try:
        import pkg_resources  # noqa: F401  (may succeed in some environments)

        return
    except ImportError:
        pass

    _mod = types.ModuleType("pkg_resources")

    def _resource_string(package: str, resource_name: str) -> bytes:
        pkg = importlib.import_module(package)
        pkg_dir = Path(pkg.__file__).parent  # type: ignore[arg-type]
        return (pkg_dir / resource_name).read_bytes()

    _mod.resource_string = _resource_string  # type: ignore[attr-defined]
    sys.modules["pkg_resources"] = _mod


def _apply_stealth_to_context(context: BrowserContext) -> None:
    """Register a page event handler so stealth patches are applied to every new page.

    This must be called once on the context. Every subsequent context.new_page()
    will automatically receive stealth patches via the 'page' event.

    Args:
        context: Active Playwright browser context.
    """
    try:
        _ensure_pkg_resources()
        from playwright_stealth import stealth_async  # type: ignore[import]

        def _on_page(page: Page) -> None:
            """Schedule stealth_async on the new page without blocking the event loop."""
            asyncio.ensure_future(stealth_async(page))

        context.on("page", _on_page)
        logger.info("playwright_stealth_enabled")
    except ImportError:
        logger.warning("playwright_stealth_not_installed")


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
            "--disable-software-rasterizer",  # reduce GPU memory pressure on RPi
            "--disable-extensions",
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

    # Register stealth on context so every new_page() gets it automatically
    _apply_stealth_to_context(context)

    # Inline webdriver masking — active even when playwright-stealth is absent.
    # Injected into every new page before any script runs.
    await context.add_init_script("""
        (() => {
            try {
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            } catch(e) {}
            try {
                Object.defineProperty(navigator, 'languages',
                    {get: () => ['fr-FR', 'fr', 'en-US', 'en']});
            } catch(e) {}
            try {
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            } catch(e) {}
            try {
                const orig = navigator.permissions.query.bind(navigator.permissions);
                navigator.permissions.query = (p) =>
                    p.name === 'notifications'
                        ? Promise.resolve({state: Notification.permission})
                        : orig(p);
            } catch(e) {}
        })();
    """)

    await _load_cookies(context)

    logger.info(
        "browser_context_created",
        user_agent=user_agent[:40],
        viewport=viewport,
    )

    return browser, context


async def new_page_with_stealth(context: BrowserContext) -> Page:
    """Create a new page; stealth is applied automatically via the context event handler.

    Use this helper anywhere a fresh page is needed after a crash or re-auth.

    Args:
        context: Active Playwright browser context (stealth must have been registered).

    Returns:
        New Playwright Page with stealth applied.
    """
    page = await context.new_page()
    # Give the stealth coroutine a moment to complete before the caller uses the page
    await asyncio.sleep(0.1)
    return page


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
            try:
                await _save_cookies(self._context)
            except Exception as exc:
                logger.warning("cookies_save_failed", error=str(exc))
            try:
                await self._context.close()
            except Exception as exc:
                logger.warning("context_close_failed", error=str(exc))
        if self._browser:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.warning("browser_close_failed", error=str(exc))
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_closed")
