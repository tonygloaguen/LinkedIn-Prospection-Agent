"""Anti-detection utilities: user-agent rotation, viewport, mouse simulation."""

from __future__ import annotations

import random

import structlog
from playwright.async_api import Page

logger = structlog.get_logger(__name__)

# Realistic Chrome user-agents (updated for 2024)
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# Viewport size ranges
_VIEWPORT_WIDTH_RANGE = (1280, 1920)
_VIEWPORT_HEIGHT_RANGE = (800, 1080)


def get_random_user_agent() -> str:
    """Return a randomly selected realistic Chrome user agent string.

    Returns:
        A user-agent string from the rotation pool.
    """
    return random.choice(_USER_AGENTS)


def get_random_viewport() -> dict[str, int]:
    """Return a random viewport size within realistic desktop ranges.

    Returns:
        Dictionary with 'width' and 'height' keys.
    """
    return {
        "width": random.randint(*_VIEWPORT_WIDTH_RANGE),
        "height": random.randint(*_VIEWPORT_HEIGHT_RANGE),
    }


async def simulate_human_scroll(page: Page, scroll_count: int = 3) -> None:
    """Simulate human-like scrolling on a page before interaction.

    Args:
        page: Playwright Page instance.
        scroll_count: Number of scroll increments to perform.
    """
    for i in range(scroll_count):
        delta = random.randint(200, 600)
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(random.randint(300, 800))
    logger.debug("human_scroll_done", scroll_count=scroll_count)


async def simulate_mouse_movement(page: Page) -> None:
    """Simulate random mouse movement before a click.

    Args:
        page: Playwright Page instance.
    """
    viewport = page.viewport_size
    if not viewport:
        return

    # Move to a random location first
    start_x = random.randint(100, viewport["width"] - 100)
    start_y = random.randint(100, viewport["height"] - 100)
    await page.mouse.move(start_x, start_y)
    await page.wait_for_timeout(random.randint(100, 300))

    # Small jitter
    for _ in range(random.randint(2, 5)):
        jitter_x = start_x + random.randint(-20, 20)
        jitter_y = start_y + random.randint(-20, 20)
        await page.mouse.move(jitter_x, jitter_y)
        await page.wait_for_timeout(random.randint(50, 150))

    logger.debug("mouse_movement_done")


async def human_click(page: Page, selector: str, timeout: int = 60_000) -> None:
    """Perform a human-like click: scroll, mouse move, then click.

    Args:
        page: Playwright Page instance.
        selector: CSS or text selector for the target element.
        timeout: Milliseconds to wait for element visibility.
    """
    await page.wait_for_selector(selector, timeout=timeout)
    await simulate_human_scroll(page, scroll_count=1)
    await simulate_mouse_movement(page)
    await page.click(selector)
    logger.debug("human_click_done", selector=selector)
