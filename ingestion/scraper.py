"""
Playwright-based scraper that clicks each camera on the DUS webcam page
to trigger stream loading and capture the resulting HLS m3u8 URLs.
"""

import asyncio
import logging
import re
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

WEBCAM_URL = "https://www.dus.com/de-de/erleben/webcams"
STREAM_HOST = "1000eyes.de"
CAMERA_NAMES = ["Flugzeugabfertigung", "Rollweg", "Vorfeld"]

CAMERA_SELECTORS = [
    '[class*="webcam"] [class*="thumb"]',
    '[class*="webcam"] [class*="preview"]',
    '[class*="webcam"] [class*="item"]',
    '[class*="webcam"] img',
    '[class*="camera"] [class*="thumb"]',
    '[class*="camera"] img',
    '[class*="stream"] [class*="thumb"]',
    "figure img",
    "video",
]

# Extracts the stream ID (e.g. "dus1ba9") from a chunklist URL so we can
# distinguish streams even when the session token rotates.
_STREAM_ID_RE = re.compile(r"/([^/]+)\.stream/")


def stream_id(url: str) -> str:
    """Extract the stable stream identifier from a chunklist URL.
    e.g. 'https://.../dus1ba9.stream/chunklist_w123.m3u8' → 'dus1ba9'
    """
    m = _STREAM_ID_RE.search(url)
    return m.group(1) if m else url


async def scrape_stream_urls(timeout_per_cam: float = 15.0) -> list[dict[str, str]]:
    """
    Open the DUS webcam page, dismiss the cookie dialog, click each camera
    trigger element, and capture the unique HLS m3u8 URL for each camera.

    Returns a list of {"label": str, "url": str} dicts.
    """
    streams: list[dict[str, str]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Collect ALL m3u8 requests globally; filter per-camera by stream ID
        pending: list[str] = []

        def on_request(request):
            url = request.url
            if STREAM_HOST in url and ".m3u8" in url:
                pending.append(url)

        page.on("request", on_request)

        try:
            await page.goto(WEBCAM_URL, timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            await _dismiss_cookie_dialog(page)

            triggers = await _find_camera_triggers(page)
            logger.info("Found %d camera trigger element(s)", len(triggers))

            seen_ids: set[str] = set()

            for i, trigger in enumerate(triggers):
                label = CAMERA_NAMES[i] if i < len(CAMERA_NAMES) else f"Camera {i + 1}"

                # Clear buffered requests, then click
                pending.clear()
                try:
                    await trigger.scroll_into_view_if_needed()
                    await trigger.click()
                except Exception as exc:
                    logger.warning("Click %d failed: %s", i + 1, exc)
                    continue

                # Wait for a NEW stream ID (one not seen from a previous camera)
                captured: str | None = None
                deadline = asyncio.get_event_loop().time() + timeout_per_cam
                while asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.3)
                    for url in pending:
                        sid = stream_id(url)
                        if sid not in seen_ids:
                            captured = url
                            seen_ids.add(sid)
                            break
                    if captured:
                        break

                if captured:
                    logger.info("Camera %d (%s): %s", i + 1, label, captured)
                    streams.append({"label": label, "url": captured})
                else:
                    logger.warning("Camera %d (%s): no new stream URL found", i + 1, label)

        except Exception as exc:
            logger.error("Scrape failed: %s", exc)
        finally:
            await browser.close()

    logger.info("Scrape complete — %d stream(s) found", len(streams))
    return streams


async def _dismiss_cookie_dialog(page) -> None:
    """Click the Cookiebot 'Allow all' button if the dialog is present."""
    selectors = [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyButtonAccept",
        "[id*='CybotCookiebot'][id*='AllowAll']",
        "[id*='CybotCookiebot'][id*='Accept']",
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                logger.info("Cookie dialog dismissed via '%s'", sel)
                await asyncio.sleep(1)
                return
        except Exception:
            continue
    logger.info("No cookie dialog found — continuing")


async def _find_camera_triggers(page) -> list:
    """Try each selector until we find a set of clickable camera elements."""
    for selector in CAMERA_SELECTORS:
        elements = await page.query_selector_all(selector)
        if elements:
            logger.info("Using selector '%s' — %d element(s)", selector, len(elements))
            return elements
    logger.warning("No camera trigger elements found with any selector")
    return []
