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
    def __init__(self, cameras: dict, handlers: dict | None = None):
        """handlers: {"video": fn(iid, path), "remove": fn(iid), "lines": fn(iid, lines)} - what to do
        when someone uploads a video, removes one or redraws counting lines in the ONLINE app."""
        super().__init__(daemon=True, name="uplink")
        self.cameras = cameras
        self.events: queue.Queue = queue.Queue(maxsize=MAX_BACKLOG)
        self.status = {"state": "starting", "last_ok": None, "error": None, "backlog": 0, "jobs_done": 0}
        self.client = httpx.Client(base_url=config.CLOUD_URL.rstrip("/"), timeout=20,
                                   headers={"X-Station-Key": config.STATION_KEY})
        analytics.on_event(self._queue)
        if handlers:
            threading.Thread(target=self._job_loop, args=(handlers,), daemon=True, name="station-jobs").start()

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
            cams = list(self.cameras.items())          # copy: the job thread may add/remove cameras
            payload = {"events": pending, "full": True,
                       "cameras": {iid: {**w.status, "lines": w.lines} for iid, w in cams}}
            try:
                self.client.post("/api/ingest", json=payload).raise_for_status()
                pending = []
                if time.time() - last_frame >= config.FRAME_UPLOAD_SECONDS:
                    last_frame = time.time()
                    for iid, w in cams:
                        if w.jpeg:
                            self.client.post(f"/api/ingest/frame/{iid}", content=self._small_jpeg(w.jpeg),
                                             headers={"Content-Type": "image/jpeg"}).raise_for_status()
                self.status.update(state="connected", last_ok=time.time(), error=None)
            except Exception as exc:     # offline / server down: keep the measurements and retry
                self.status.update(state="offline", error=str(exc)[:200])
                pending = pending[-MAX_BACKLOG:]
            self.status["backlog"] = len(pending)
            time.sleep(2)

    # ---- jobs from the online app: video uploads, removals, counting lines
    def _job_loop(self, handlers: dict):
        seen: set[str] = set()
        while True:
            try:
                for job in self.client.get("/api/station/jobs").raise_for_status().json():
                    if job["id"] not in seen:
                        seen.add(job["id"])
                        self._do_job(job, handlers)
            except Exception as exc:
                print(f"[station] could not check for jobs: {str(exc)[:120]}")
            time.sleep(5)

    def _do_job(self, job: dict, handlers: dict):
        iid, action = job["iid"], job["action"]
        result = {"ok": True, "error": None, "info": {}}
        try:
            if action == "video":
                folder = config.ROOT / "videos"
                folder.mkdir(exist_ok=True)
                dest = folder / f"{iid}_{time.strftime('%Y%m%d-%H%M%S')}{job.get('ext', '.mp4')}"
                print(f"[station] downloading video for {iid} ({job.get('megabytes')} MB)")
                with self.client.stream("GET", f"/api/station/jobs/{job['id']}/video", timeout=None) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as out:
                        for chunk in r.iter_bytes(1 << 20):
                            out.write(chunk)
                result["info"] = handlers["video"](iid, dest)
                print(f"[station] now analysing the uploaded video at {iid}")
            elif action == "remove":
                handlers["remove"](iid)
            elif action == "lines":
                handlers["lines"](iid, job["lines"])
            else:
                raise ValueError(f"unknown job {action}")
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:300], "info": {}}
            print(f"[station] job {action} at {iid} failed: {exc}")
        try:
            self.client.post(f"/api/station/jobs/{job['id']}/done", json=result).raise_for_status()
            self.status["jobs_done"] += 1
        except Exception as exc:
            print(f"[station] could not report job result: {str(exc)[:120]}")
