#!/usr/bin/env python3
"""Playwright DOM diagnostic script for LinkedIn content search.

Run BEFORE patching search.py to identify working selectors on the live DOM.

Usage:
    LINKEDIN_EMAIL=you@example.com LINKEDIN_PASSWORD=secret python debug_dom.py

Output:
    - debug_output.html  : full page DOM after JS rendering
    - Terminal report    : data-* attributes, selector counts, best-match preview
"""

# NOTE: this script uses print() intentionally — it is a standalone CLI diagnostic
# tool, not part of the agent pipeline. Do not add structlog here.

import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SEARCH_URL = (
    "https://www.linkedin.com/search/results/content/"
    "?keywords=DevOps&sortBy=%22date%22"
)

# Ordered candidate selectors — from most specific to broadest fallback
CANDIDATE_SELECTORS: list[tuple[str, str]] = [
    # ── Priority 1: URN-based (LinkedIn internal IDs — most stable) ────────────
    ("[data-urn*='urn:li:activity']", "URN activity attr (modern)"),
    ("[data-chameleon-result-urn*='urn:li:activity']", "Chameleon URN attr"),
    # ── Priority 2: Structural / impression tracking ────────────────────────────
    (".fie-impression-container", "Impression container (2023+)"),
    (".occludable-update", "Occludable update (may still work)"),
    # ── Priority 3: Legacy class names ─────────────────────────────────────────
    (".feed-shared-update-v2", "Feed update v2 (legacy ~2022)"),
    (".search-result__occluded-item", "Search result item (legacy)"),
    # ── Priority 4: Result container wrappers ──────────────────────────────────
    ("li.reusable-search__result-container", "Reusable search result li"),
    (".reusable-search__result-container", "Reusable search result div"),
    (".search-results-container > div > ul > li", "Search results list items"),
    # ── Priority 5: Link-based detection ───────────────────────────────────────
    ("a[href*='/feed/update/']", "Feed update links (modern post URLs)"),
    ("a[href*='/posts/']", "Post links"),
    ("a[href*='/activity/']", "Activity links"),
    ("a[href*='/in/']", "Profile links (broad)"),
    # ── Priority 6: Text containers ─────────────────────────────────────────────
    (".update-components-text", "Post text component"),
    (".update-components-actor", "Post actor/author component"),
    (".update-components-actor__meta-link", "Actor meta link"),
    # ── Priority 7: Broadest fallbacks ─────────────────────────────────────────
    ("[data-urn]", "Any data-urn (very broad)"),
    ("article", "Article elements"),
]

_DIVIDER = "─" * 70


def _truncate(text: str, n: int = 500) -> str:
    return text[:n] + "…" if len(text) > n else text


async def _login(page: object, email: str, password: str) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    print("[1/5] Logging in to LinkedIn…")
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    await page.fill("#username", email)
    await page.fill("#password", password)
    await page.click("button[type='submit']")
    await page.wait_for_url("**/feed/**", timeout=60_000)
    print(f"      ✓ Authenticated — current URL: {page.url}")


async def _navigate_and_wait(page: object, url: str) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    print(f"\n[2/5] Navigating to search URL…\n      {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    print(f"      Page title : {await page.title()!r}")
    print(f"      Final URL  : {page.url}")


async def _wait_and_scroll(page: object) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    print("\n[3/5] Waiting 5 s + scrolling (3×) to trigger lazy-load…")
    await page.wait_for_timeout(5_000)
    for i in range(3):
        await page.mouse.wheel(0, 900)
        await page.wait_for_timeout(1_200)
        print(f"      Scroll {i + 1}/3 done")
    # Extra wait for React to finish rendering
    await page.wait_for_timeout(2_000)


async def _dump_html(page: object) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    html = await page.content()
    output = Path("debug_output.html")
    output.write_text(html, encoding="utf-8")
    print(f"\n[4/5] HTML dumped → {output.resolve()}  ({len(html):,} bytes)")


async def _analyse_dom(page: object) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    print(f"\n[5/5] DOM Analysis\n{_DIVIDER}")

    # ── Unique data-* attributes ────────────────────────────────────────────────
    data_attrs: dict[str, int] = await page.evaluate(
        """
        () => {
            const attrs = {};
            document.querySelectorAll('*').forEach(el => {
                for (const a of el.attributes) {
                    if (a.name.startsWith('data-')) {
                        attrs[a.name] = (attrs[a.name] || 0) + 1;
                    }
                }
            });
            return attrs;
        }
        """
    )

    print("\nTop 25 data-* attributes (by element count):")
    for attr, count in sorted(data_attrs.items(), key=lambda x: -x[1])[:25]:
        bar = "█" * min(count, 40)
        print(f"  {attr:<45s} ×{count:4d}  {bar}")

    # ── Selector probe ──────────────────────────────────────────────────────────
    print(f"\nSelector probe:\n{_DIVIDER}")
    results: list[tuple[str, str, int]] = []
    for sel, label in CANDIDATE_SELECTORS:
        elements = await page.query_selector_all(sel)
        count = len(elements)
        results.append((sel, label, count))
        icon = "✓" if count > 0 else "✗"
        print(f"  {icon} [{count:4d}]  {label}")
        print(f"          {sel}")

    # ── Best selector preview ───────────────────────────────────────────────────
    # Best = most matches among selectors returning between 1 and 50 elements
    promising = [(s, lbl, c) for s, lbl, c in results if 0 < c <= 50]
    if not promising:
        # Relax upper bound
        promising = [(s, lbl, c) for s, lbl, c in results if c > 0]

    if promising:
        best_sel, best_label, best_count = max(promising, key=lambda x: x[2])
        print(f"\n{_DIVIDER}")
        print(f"Best selector: {best_sel!r}  ({best_label}, {best_count} elements)")
        print("\nFirst 3 elements — outerHTML preview (500 chars):\n")

        elements = await page.query_selector_all(best_sel)
        for i, el in enumerate(elements[:3]):
            outer: str = await el.evaluate("el => el.outerHTML")
            print(f"  [{i + 1}] {_truncate(outer, 500)}\n")
    else:
        print(f"\n{_DIVIDER}")
        print("⚠  No selector returned any elements.")
        print("   LinkedIn may have served a wall/captcha or the account is blocked.")
        print("   Check debug_output.html for the actual page content.")

    print(f"\n{_DIVIDER}")
    print(f"Final URL : {page.url}")


async def main() -> None:
    email = os.environ.get("LINKEDIN_EMAIL", "")
    password = os.environ.get("LINKEDIN_PASSWORD", "")

    if not email or not password:
        print("ERROR: LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be set.")
        sys.exit(1)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="fr-FR",
            timezone_id="Europe/Paris",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8"},
        )

        page = await context.new_page()

        await _login(page, email, password)
        await _navigate_and_wait(page, SEARCH_URL)
        await _wait_and_scroll(page)
        await _dump_html(page)
        await _analyse_dom(page)

        await browser.close()

    print("\nDone. Open debug_output.html in a browser to inspect the full DOM.")
    print("Search for: data-urn, data-chameleon, class=\"update-components")


if __name__ == "__main__":
    asyncio.run(main())
