"""
FastAPI application — entry point for the Virtual Tower backend.

Endpoints
---------
GET /                  → serve the viewer UI
GET /frame             → latest camera frame as JPEG
GET /streams           → list all discovered streams with labels
POST /streams/{index}  → switch the active stream
GET /status            → JSON health / stream info
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.collector import DatasetCollector
from ingestion.grabber import FrameGrabber
from ingestion.scraper import scrape_stream_urls, stream_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

SCRAPE_INTERVAL = 30 * 60  # re-scrape every 30 minutes
UI_DIR = Path(__file__).resolve().parents[1] / "ui"


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class StreamState:
    def __init__(self):
        self.streams: list[dict[str, str]] = []  # [{label, url}, ...]
        self.active_index: int = 0
        self.last_frame: bytes | None = None
        self.healthy: bool = False
        self.last_scrape: float = 0.0
        self.rescrape_requested: bool = False  # grabber sets this on failure
        self.collecting: bool = False

    @property
    def stream_url(self) -> str | None:
        if self.streams and 0 <= self.active_index < len(self.streams):
            return self.streams[self.active_index]["url"]
        return None


state = StreamState()
grabber = FrameGrabber(state)
collector = DatasetCollector()


# ---------------------------------------------------------------------------
# Background scraper loop
# ---------------------------------------------------------------------------

async def _run_scrape() -> None:
    """Run one scrape pass and merge results into state."""
    logger.info("Running stream URL scrape...")
    streams = await scrape_stream_urls()

    if not streams:
        logger.warning("Scrape returned no streams")
        return

    prev_url = state.stream_url
    state.last_scrape = time.time()

    if not state.streams:
        # First run — accept order as-is
        state.streams = streams
    else:
        # Merge by stable stream ID so camera indices never shift
        new_by_id = {stream_id(s["url"]): s["url"] for s in streams}
        for s in state.streams:
            sid = stream_id(s["url"])
            if sid in new_by_id:
                s["url"] = new_by_id[sid]
        existing_ids = {stream_id(s["url"]) for s in state.streams}
        for s in streams:
            if stream_id(s["url"]) not in existing_ids:
                state.streams.append(s)

    state.active_index = min(state.active_index, len(state.streams) - 1)

    if state.collecting:
        collector.update(state.streams)

    # Only restart the grabber if the active URL changed AND the stream is
    # currently unhealthy — avoids interrupting a working feed just to
    # refresh the session token
    if state.stream_url != prev_url and not state.healthy:
        logger.info("Active URL changed and stream unhealthy — restarting grabber")
        grabber.restart()
    elif state.stream_url != prev_url:
        logger.info("Active URL refreshed (grabber healthy — will use new URL on next reconnect)")


async def scraper_loop():
    await _run_scrape()  # initial scrape

    while True:
        # Wait, but wake up early if the grabber signals a failure
        for _ in range(SCRAPE_INTERVAL * 2):   # check every 0.5 s
            await asyncio.sleep(0.5)
            if state.rescrape_requested:
                logger.info("Stream failure detected — re-scraping immediately")
                state.rescrape_requested = False
                break

        await _run_scrape()


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    grabber.start()
    asyncio.create_task(scraper_loop())
    yield
    grabber.stop()
    collector.stop_all()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="DUS Virtual Tower", lifespan=lifespan)
app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(UI_DIR / "index.html")


@app.get("/frame", summary="Latest camera frame (JPEG)")
async def get_frame():
    if state.last_frame is None:
        raise HTTPException(status_code=503, detail="No frame available yet — stream initialising")
    return Response(content=state.last_frame, media_type="image/jpeg")


@app.get("/streams", summary="List all discovered camera streams")
async def list_streams():
    return [
        {"index": i, "label": s["label"], "url": s["url"], "active": i == state.active_index}
        for i, s in enumerate(state.streams)
    ]


@app.post("/streams/{index}", summary="Switch to a different camera stream")
async def select_stream(index: int):
    if not state.streams:
        raise HTTPException(status_code=503, detail="No streams discovered yet")
    if index < 0 or index >= len(state.streams):
        raise HTTPException(status_code=404, detail=f"Stream index {index} not found")

    if index != state.active_index:
        state.active_index = index
        state.healthy = False
        grabber.restart()  # new frame arrives within ~2 s; old frame stays in last_frame until then
        logger.info("Switched to stream %d: %s", index, state.streams[index]["label"])

    return {"active": index, "label": state.streams[index]["label"]}


@app.post("/dataset/toggle", summary="Enable or disable dataset collection")
async def dataset_toggle():
    state.collecting = not state.collecting
    if state.collecting:
        collector.update(state.streams)
        logger.info("Dataset collection ENABLED")
    else:
        collector.stop_all()
        logger.info("Dataset collection DISABLED")
    return {"collecting": state.collecting}


@app.get("/dataset/stats", summary="Frame counts saved per stream")
async def dataset_stats():
    return {"collecting": state.collecting, "snapshots_taken": collector.snapshots_taken, "frames": collector.stats()}


@app.get("/status", summary="Stream health and metadata")
async def get_status():
    return {
        "healthy": state.healthy,
        "active_index": state.active_index,
        "stream_url": state.stream_url,
        "last_scrape": state.last_scrape,
        "total_streams": len(state.streams),
    }
