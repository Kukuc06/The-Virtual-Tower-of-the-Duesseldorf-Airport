"""
Dataset collector: saves one JPEG per stream every CAPTURE_INTERVAL seconds
into dataset/<stream_label>/YYYYMMDD_HHMMSS_mmm.jpg for later labelling.

One ffmpeg process runs per stream so all cameras are captured in parallel.
"""

import glob
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_FFMPEG = shutil.which("ffmpeg")

DATASET_DIR = Path(__file__).resolve().parents[1] / "dataset"
CAPTURE_INTERVAL = 5  # seconds between saved frames


def _safe_label(label: str) -> str:
    return re.sub(r"[^\w\-]", "_", label)


class _StreamCapture:
    """Captures frames from one HLS stream and saves timestamped JPEGs."""

    def __init__(self, label: str, url: str, on_saved=None):
        self.label = label
        self.url = url
        self._on_saved = on_saved  # callback fired after each frame is written
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"collector-{self.label}"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        out_dir = DATASET_DIR / _safe_label(self.label)
        out_dir.mkdir(parents=True, exist_ok=True)

        fps = 1.0 / CAPTURE_INTERVAL
        tmpdir = tempfile.mkdtemp(prefix=f"vtower_ds_{_safe_label(self.label)}_")
        pattern = os.path.join(tmpdir, "frame%08d.jpg")

        cmd = [
            _FFMPEG,
            "-loglevel",         "error",
            "-live_start_index", "-1",
            "-i",                self.url,
            "-vf",               f"fps={fps}",
            "-q:v",              "3",
            pattern,
        ]

        try:
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        except FileNotFoundError:
            logger.error("ffmpeg not found on PATH. Install it: winget install Gyan.FFmpeg")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return

        def _drain_stderr():
            for line in proc.stderr:
                logger.debug("[ffmpeg/%s] %s", self.label,
                             line.decode(errors="replace").strip())

        threading.Thread(target=_drain_stderr, daemon=True).start()

        seen: set[str] = set()
        try:
            while not self._stop.is_set():
                if proc.poll() is not None:
                    logger.warning("ffmpeg exited for stream '%s' (code %d)",
                                   self.label, proc.returncode)
                    break

                for path in sorted(glob.glob(os.path.join(tmpdir, "frame*.jpg"))):
                    if path in seen:
                        continue
                    try:
                        data = open(path, "rb").read()
                    except OSError:
                        continue
                    if not data:
                        continue
                    seen.add(path)
                    ts = datetime.now().strftime("%d.%m.%Y_T%H-%M-%S")
                    dest = out_dir / f"{ts}.jpg"
                    dest.write_bytes(data)
                    logger.info("Dataset: saved %s", dest.name)
                    if self._on_saved:
                        self._on_saved(self.label)

                # Keep only the 2 newest tmp files to avoid filling the disk
                all_tmp = sorted(glob.glob(os.path.join(tmpdir, "frame*.jpg")))
                for old in all_tmp[:-2]:
                    try:
                        os.remove(old)
                    except OSError:
                        pass

                time.sleep(0.5)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(tmpdir, ignore_errors=True)


class DatasetCollector:
    """Manages one _StreamCapture per discovered stream."""

    def __init__(self):
        self._captures: dict[str, _StreamCapture] = {}
        self._lock = threading.Lock()
        self.last_saved: float = 0.0
        self.snapshots_taken: int = 0  # increments when every active stream saved one frame
        self._saved_since_snapshot: set[str] = set()

    def _on_frame_saved(self, label: str):
        self.last_saved = time.time()
        self._saved_since_snapshot.add(label)
        active = set(self._captures.keys())
        if active and active.issubset(self._saved_since_snapshot):
            self.snapshots_taken += 1
            self._saved_since_snapshot.clear()

    def update(self, streams: list[dict[str, str]]) -> None:
        """Start/restart captures to match the current stream list."""
        with self._lock:
            new_labels = {s["label"] for s in streams}

            for label in list(self._captures):
                if label not in new_labels:
                    self._captures.pop(label).stop()
                    logger.info("DatasetCollector: stopped capture for '%s'", label)

            for s in streams:
                label, url = s["label"], s["url"]
                existing = self._captures.get(label)
                if existing and existing.url == url:
                    continue  # already running with the same URL
                if existing:
                    existing.stop()
                cap = _StreamCapture(label, url, on_saved=self._on_frame_saved)
                self._captures[label] = cap
                cap.start()
                logger.info("DatasetCollector: started capture for '%s'", label)

    def stop_all(self) -> None:
        with self._lock:
            for cap in self._captures.values():
                cap.stop()
            self._captures.clear()

    def stats(self) -> dict[str, int]:
        """Return frame counts per stream label."""
        counts: dict[str, int] = {}
        for label in list(self._captures):
            d = DATASET_DIR / _safe_label(label)
            counts[label] = len(list(d.glob("*.jpg"))) if d.exists() else 0
        return counts
