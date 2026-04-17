# The Virtual Tower of Düsseldorf Airport

An AI-powered computer vision system that monitors the live airfield of **Düsseldorf Airport (DUS)**. The objective is to detect aircraft, track their movement, and fuse visual data with real-time flight telemetry.

---

## Current State

The ingestion and viewer layer is fully operational:

- Playwright scrapes the [DUS webcam page](https://www.dus.com/de-de/erleben/webcams) to discover live HLS stream URLs for all three cameras
- HLS streams are played directly in the browser via HLS.js at full frame rate (25–30 fps)
- A FastAPI backend manages stream state, URL refresh on expiry, and frame capture for inference
- The viewer UI supports switching between all three cameras with a switching overlay and live status indicator
- A live resolution and fps counter is displayed in the header

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                        Browser                          │
│  HLS.js → plays stream directly from 1000eyes.de CDN   │
│  Polls /streams for URL refresh every 35 min            │
└────────────────────┬────────────────────────────────────┘
                     │ REST API
┌────────────────────▼────────────────────────────────────┐
│                   FastAPI  (api/main.py)                 │
│  GET  /              viewer UI                           │
│  GET  /streams       list cameras + HLS URLs            │
│  POST /streams/{i}   switch active camera               │
│  GET  /frame         latest JPEG frame (for inference)  │
│  GET  /status        health check                       │
└──────┬──────────────────────────┬───────────────────────┘
       │                          │
┌──────▼──────────┐   ┌───────────▼──────────────────────┐
│ ingestion/      │   │ ingestion/                        │
│ scraper.py      │   │ grabber.py                        │
│                 │   │                                   │
│ Playwright      │   │ ffmpeg subprocess writes JPEG     │
│ headless Chrome │   │ frames to a temp dir at 1 fps;    │
│ clicks each     │   │ Python polls and stores the       │
│ camera trigger, │   │ latest frame in state.last_frame  │
│ captures m3u8   │   │ (used later for YOLO inference)   │
│ URLs            │   │                                   │
└─────────────────┘   └───────────────────────────────────┘
```

### Cameras

| Index | Label                | Stream ID  |
|-------|----------------------|------------|
| 0     | Flugzeugabfertigung  | dus5abb    |
| 1     | Rollweg              | —          |
| 2     | Vorfeld              | —          |

Stream URLs contain session tokens that rotate. The backend re-scrapes automatically every 30 minutes, or immediately when a stream fails.

---

## Setup

### Requirements

- Python 3.11+
- ffmpeg on PATH (or installed via winget: `winget install Gyan.FFmpeg`)
- Chromium for Playwright

```bash
pip install -r requirements.txt
playwright install chromium
```

### Run

```bash
uvicorn api.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000).

---

## Roadmap

- [ ] YOLO model inference on grabbed frames (aircraft detection)
- [ ] Object tracking with persistent identities across frames
- [ ] ADS-B data enrichment via OpenSky Network API
- [ ] Correlate visual detections with flight telemetry
- [ ] Alert system for anomalous ground movements
