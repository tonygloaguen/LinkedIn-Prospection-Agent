"""Manual login helper — run on your local machine (not RPi) with a visible browser.

Steps:
1. python debug_login.py
2. Log in manually in the browser window
3. Complete phone verification if prompted
4. Wait until you're on the LinkedIn feed
5. Press Enter in this terminal → cookies saved to data/session.json
6. scp data/session.json user@rpi:/opt/linkedin-agent/data/session.json
"""

import asyncio
import json
from pathlib import Path


async def main() -> None:
    from playwright.async_api import async_playwright

    session_path = Path("data/session.json")
    session_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            slow_mo=0,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        context = await browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        page = await context.new_page()

        print("Opening LinkedIn login page...")
        await page.goto("https://www.linkedin.com/login", wait_until="load")

        print("\n>>> Log in manually in the browser window.")
        print(">>> Complete Google phone verification if prompted.")
        print(">>> Wait until you see the LinkedIn feed (linkedin.com/feed).")
        print(">>> Then press Enter here to save cookies.\n")
        input("Press Enter when you are on the feed... ")

        current_url = page.url
        if "feed" not in current_url:
            print(f"WARNING: current URL is {current_url!r} — not the feed.")
            print("Saving cookies anyway, but session may not be valid.")

        cookies = await context.cookies()
        session_path.write_text(json.dumps(cookies, indent=2))
        print(f"\nSaved {len(cookies)} cookies to {session_path}")
        print("\nNext step — copy to RPi:")
        print(
            f"  scp {session_path} gloaguen@AI-Automation-Rpi:/opt/linkedin-agent/data/session.json"
        )

        await browser.close()


asyncio.run(main())
