"""LinkedIn connection invitation flow via Playwright."""

from __future__ import annotations

import structlog
from playwright.async_api import Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.exceptions import ConnectionSendError, PlaywrightTimeoutError
from utils.anti_detection import human_click, simulate_human_scroll

logger = structlog.get_logger(__name__)

_TIMEOUT = 60_000
_MESSAGE_MAX_CHARS = 280


async def _screenshot_debug(page: Page, name: str) -> None:
    """Take a debug screenshot on error.

    Args:
        page: Playwright Page.
        name: Filename suffix for the screenshot.
    """
    import os
    from pathlib import Path

    path = Path("./logs/screenshots")
    path.mkdir(parents=True, exist_ok=True)
    filepath = path / f"{name}.png"
    try:
        await page.screenshot(path=str(filepath))
        logger.info("debug_screenshot_saved", path=str(filepath))
    except Exception as exc:
        logger.warning("screenshot_failed", error=str(exc))


async def _check_already_connected(page: Page) -> bool:
    """Check if the user is already connected to the profile.

    Args:
        page: Playwright Page on the profile.

    Returns:
        True if already connected or invitation pending.
    """
    # "Message" button or "Pending" state indicates existing connection
    indicators = [
        "button:has-text('Message')",
        "button:has-text('Message')",
        "button:has-text('En attente')",
        "button:has-text('Pending')",
        "span:has-text('Connexion')",
        "span:has-text('1er degré')",
        "span:has-text('1st')",
    ]
    for sel in indicators:
        try:
            el = await page.query_selector(sel)
            if el:
                logger.info("already_connected_or_pending", selector=sel)
                return True
        except Exception:
            pass
    return False


@retry(
    retry=retry_if_exception_type(ConnectionSendError),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=10, max=30),
    reraise=True,
)
async def send_connection_invitation(
    page: Page,
    linkedin_url: str,
    message: str,
    dry_run: bool = False,
) -> bool:
    """Send a LinkedIn connection invitation with a personalised note.

    Flow: navigate to profile → click "Se connecter" → "Ajouter une note"
    → type message → "Envoyer".

    Args:
        page: Authenticated Playwright Page.
        linkedin_url: Target profile URL.
        message: Connection message (max 280 characters).
        dry_run: If True, log the action but do not actually send.

    Returns:
        True if invitation was sent (or dry_run), False if already connected.

    Raises:
        ConnectionSendError: If the invitation flow fails.
    """
    if len(message) > _MESSAGE_MAX_CHARS:
        message = message[:_MESSAGE_MAX_CHARS]
        logger.warning("message_truncated", length=_MESSAGE_MAX_CHARS)

    logger.info("send_connection_start", url=linkedin_url, dry_run=dry_run)

    try:
        await page.goto(linkedin_url, timeout=_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        await simulate_human_scroll(page, scroll_count=1)
    except Exception as exc:
        raise ConnectionSendError(
            f"Failed to navigate to profile {linkedin_url}: {exc}"
        ) from exc

    # Check edge cases
    if await _check_already_connected(page):
        logger.info("invitation_skipped_already_connected", url=linkedin_url)
        return False

    if dry_run:
        logger.info(
            "dry_run_connection_skipped",
            url=linkedin_url,
            message_preview=message[:50],
        )
        return True

    try:
        # Find "Se connecter" / "Connect" button
        connect_selectors = [
            "button:has-text('Se connecter')",
            "button:has-text('Connect')",
            "[data-control-name='connect']",
            "button[aria-label*='Inviter']",
            "button[aria-label*='Connect']",
        ]

        connect_btn = None
        for sel in connect_selectors:
            try:
                connect_btn = await page.wait_for_selector(sel, timeout=5_000)
                if connect_btn:
                    break
            except Exception:
                continue

        if not connect_btn:
            # Try overflow menu ("More" button)
            more_selectors = [
                "button:has-text('Plus')",
                "button:has-text('More')",
                "button[aria-label*='Plus']",
            ]
            for sel in more_selectors:
                try:
                    more_btn = await page.wait_for_selector(sel, timeout=3_000)
                    if more_btn:
                        await more_btn.click()
                        await page.wait_for_timeout(500)
                        for csell in connect_selectors:
                            connect_btn = await page.query_selector(csell)
                            if connect_btn:
                                break
                        break
                except Exception:
                    continue

        if not connect_btn:
            await _screenshot_debug(page, f"no_connect_btn_{linkedin_url.split('/')[-1]}")
            raise ConnectionSendError(f"Connect button not found for {linkedin_url}")

        await simulate_human_scroll(page, scroll_count=1)
        await connect_btn.click()
        await page.wait_for_timeout(1_000)

        # Click "Ajouter une note" / "Add a note"
        note_selectors = [
            "button:has-text('Ajouter une note')",
            "button:has-text('Add a note')",
            "[data-control-name='add_note']",
        ]
        note_btn = None
        for sel in note_selectors:
            try:
                note_btn = await page.wait_for_selector(sel, timeout=5_000)
                if note_btn:
                    break
            except Exception:
                continue

        if not note_btn:
            await _screenshot_debug(page, f"no_note_btn_{linkedin_url.split('/')[-1]}")
            raise ConnectionSendError(f"Add note button not found for {linkedin_url}")

        await note_btn.click()
        await page.wait_for_timeout(500)

        # Type the message
        textarea_selectors = [
            "textarea[name='message']",
            "textarea#custom-message",
            "textarea",
        ]
        textarea = None
        for sel in textarea_selectors:
            try:
                textarea = await page.wait_for_selector(sel, timeout=5_000)
                if textarea:
                    break
            except Exception:
                continue

        if not textarea:
            await _screenshot_debug(page, f"no_textarea_{linkedin_url.split('/')[-1]}")
            raise ConnectionSendError(f"Message textarea not found for {linkedin_url}")

        await textarea.fill(message)
        await page.wait_for_timeout(500)

        # Click "Envoyer" / "Send"
        send_selectors = [
            "button:has-text('Envoyer')",
            "button:has-text('Send')",
            "[data-control-name='send']",
            "button[aria-label*='Envoyer']",
        ]
        send_btn = None
        for sel in send_selectors:
            try:
                send_btn = await page.wait_for_selector(sel, timeout=5_000)
                if send_btn:
                    break
            except Exception:
                continue

        if not send_btn:
            await _screenshot_debug(page, f"no_send_btn_{linkedin_url.split('/')[-1]}")
            raise ConnectionSendError(f"Send button not found for {linkedin_url}")

        await send_btn.click()
        await page.wait_for_timeout(1_000)

        logger.info("invitation_sent", url=linkedin_url)
        return True

    except ConnectionSendError:
        raise
    except Exception as exc:
        await _screenshot_debug(page, f"connection_error_{linkedin_url.split('/')[-1]}")
        raise ConnectionSendError(
            f"Connection invitation flow failed for {linkedin_url}: {exc}"
        ) from exc
