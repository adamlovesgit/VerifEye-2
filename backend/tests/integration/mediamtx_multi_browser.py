"""Assert two real WHEP browser readers retain one MediaMTX path source.

Run this while VerifEye and an enabled test camera are live. MediaMTX represents
the camera-side pull as the path's single ``source``; browser and pre-roll
consumers appear only in ``readers``.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.cameras.media import MediaMTXClient


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera-id", required=True, type=int)
    parser.add_argument("--token", required=True, help="An existing VerifEye session bearer")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--control-url", default="http://127.0.0.1:9997")
    args = parser.parse_args()
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit("Install playwright and its Chromium browser to run this assertion.") from exc

    control = MediaMTXClient(args.control_url)
    path = f"verifeye-camera-{args.camera_id}-preview"
    before = control.runtime_path(path)
    if not before or not before.get("source"):
        raise RuntimeError(f"Preview path {path} does not have an online source.")
    source_id = before["source"].get("id")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        pages = []
        try:
            for _ in range(2):
                context = await browser.new_context()
                page = await context.new_page()
                await page.add_init_script(
                    f"localStorage.setItem('verifeye_token', {json.dumps(args.token)})"
                )
                await page.goto(args.base_url)
                selector = f'[data-camera-id="{args.camera_id}"] video'
                await page.wait_for_selector(selector)
                await page.wait_for_function(
                    "selector => document.querySelector(selector)?.readyState >= 2", arg=selector
                )
                pages.append((context, page))

            during = control.runtime_path(path)
            if during.get("source", {}).get("id") != source_id:
                raise AssertionError("Browser fan-out replaced the single preview-side source.")
            readers = during.get("readers") or []
            whep_readers = [reader for reader in readers if "webrtc" in str(reader.get("type", "")).lower()]
            if len(whep_readers) < 2:
                raise AssertionError(f"Expected two WHEP readers; runtime reported {readers!r}")
            print(f"OK: source {source_id!r} remained singular with {len(whep_readers)} WHEP readers.")
        finally:
            for context, _page in pages:
                await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
