"""Camera station -> cloud server uplink.

The PC at (or near) the junction runs the cameras and AI. It sends what it measures - every counted
vehicle, the number of vehicles in view, camera status - plus a small video snapshot every few seconds
to the cloud server, which serves the phone apps. Only results travel, not the video stream, so a slow
connection is enough. If the connection drops, measurements are kept and sent when it comes back.

Turn it on in the station's .env:  CLOUD_URL=https://your-server   STATION_KEY=<same key as the server>
"""
import queue
import threading
import time

import httpx

from . import config
from .analytics import analytics

MAX_BACKLOG = 20000


class Uplink(threading.Thread):
    def __init__(self, cameras: dict):
        super().__init__(daemon=True, name="uplink")
        self.cameras = cameras
        self.events: queue.Queue = queue.Queue(maxsize=MAX_BACKLOG)
        self.status = {"state": "starting", "last_ok": None, "error": None, "backlog": 0}
        self.client = httpx.Client(base_url=config.CLOUD_URL.rstrip("/"), timeout=20,
                                   headers={"X-Station-Key": config.STATION_KEY})
        analytics.on_event(self._queue)

    def _queue(self, event: dict):
        try:
            self.events.put_nowait(event)
        except queue.Full:            # offline for a long time: drop the oldest measurement
            self.events.get_nowait()
            self.events.put_nowait(event)

    def _drain(self) -> list[dict]:
        out = []
        while not self.events.empty() and len(out) < 2000:
            out.append(self.events.get_nowait())
        return out

    def _small_jpeg(self, jpeg: bytes) -> bytes:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        if w > config.FRAME_UPLOAD_WIDTH:
            img = cv2.resize(img, (config.FRAME_UPLOAD_WIDTH, int(h * config.FRAME_UPLOAD_WIDTH / w)))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
        return buf.tobytes() if ok else jpeg

    def run(self):
        last_frame = 0.0
        pending: list[dict] = []
        while True:
            pending += self._drain()
            payload = {"events": pending, "cameras": {iid: w.status for iid, w in self.cameras.items()}}
            try:
                self.client.post("/api/ingest", json=payload).raise_for_status()
                pending = []
                if time.time() - last_frame >= config.FRAME_UPLOAD_SECONDS:
                    last_frame = time.time()
                    for iid, w in self.cameras.items():
                        if w.jpeg:
                            self.client.post(f"/api/ingest/frame/{iid}", content=self._small_jpeg(w.jpeg),
                                             headers={"Content-Type": "image/jpeg"}).raise_for_status()
                self.status.update(state="connected", last_ok=time.time(), error=None)
            except Exception as exc:     # offline / server down: keep the measurements and retry
                self.status.update(state="offline", error=str(exc)[:200])
                pending = pending[-MAX_BACKLOG:]
            self.status["backlog"] = len(pending)
            time.sleep(2)
