"""
Frame grabber: ffmpeg writes JPEG files to a temp folder at 0.5 fps;
the main loop polls that folder for new files every 0.5 s.

Avoids Windows pipe-flushing issues that cause the MJPEG pipe approach to
stall after the first frame.
"""

import glob
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

_FFMPEG = shutil.which("ffmpeg") or (
    r"C:\Users\const\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
)

RECONNECT_DELAY = 5


class FrameGrabber:
    def __init__(self, state):
        self.state = state
        self._generation = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def start(self):
        self._spawn()

    def stop(self):
        with self._lock:
            self._generation += 1
        logger.info("Frame grabber stopped")

    def restart(self):
        with self._lock:
            self._generation += 1
            gen = self._generation
        logger.info("Frame grabber restarting (gen %d)", gen)
        threading.Thread(target=self._loop, args=(gen,), daemon=True,
                         name=f"grabber-{gen}").start()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn(self):
        with self._lock:
            self._generation += 1
            gen = self._generation
        threading.Thread(target=self._loop, args=(gen,), daemon=True,
                         name=f"grabber-{gen}").start()

    def _is_current(self, gen: int) -> bool:
        return self._generation == gen

    def _loop(self, my_gen: int):
        while self._is_current(my_gen):
            url = self.state.stream_url
            if not url:
                time.sleep(2)
                continue

            logger.info("[gen %d] Connecting: %s", my_gen, url)
            self._stream(my_gen, url)

            if self._is_current(my_gen):
                logger.warning("[gen %d] Stream ended — retrying in %ds",
                               my_gen, RECONNECT_DELAY)
                self.state.healthy = False
                self.state.rescrape_requested = True
                time.sleep(RECONNECT_DELAY)

        logger.info("[gen %d] Thread exiting (current gen %d)",
                    my_gen, self._generation)

    def _stream(self, my_gen: int, url: str):
        tmpdir = tempfile.mkdtemp(prefix="vtower_")
        pattern = os.path.join(tmpdir, "frame%08d.jpg")

        cmd = [
            _FFMPEG,
            "-loglevel",         "error",
            "-live_start_index", "-1",   # start from the latest HLS segment
            "-i",                url,
            "-vf",               "fps=1",    # one frame per second for inference
            "-q:v",              "5",
            pattern,
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,   # capture for debug logging
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found at '%s'", _FFMPEG)
            shutil.rmtree(tmpdir, ignore_errors=True)
            time.sleep(30)
            return

        # Log any ffmpeg errors in the background
        def _log_stderr():
            for line in proc.stderr:
                logger.warning("[ffmpeg] %s", line.decode(errors="replace").strip())
        threading.Thread(target=_log_stderr, daemon=True).start()

        last_file: str | None = None
        try:
            while self._is_current(my_gen):
                if proc.poll() is not None:
                    logger.warning("[gen %d] ffmpeg exited (code %d)",
                                   my_gen, proc.returncode)
                    break

                files = sorted(glob.glob(os.path.join(tmpdir, "frame*.jpg")))
                if files:
                    latest = files[-1]
                    if latest != last_file:
                        try:
                            data = open(latest, "rb").read()
                        except OSError:
                            data = b""
                        if data:
                            if self._is_current(my_gen):
                                self.state.last_frame = data
                                self.state.healthy = True
                                logger.info("[gen %d] New frame: %s (%d B)",
                                            my_gen, os.path.basename(latest), len(data))
                            last_file = latest

                    # Remove all but the four newest files
                    for old in files[:-4]:
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
