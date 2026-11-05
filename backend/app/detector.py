"""Camera worker: YOLOv8 detection + ByteTrack tracking + counting-line crossings.

Works with a video file (testing), an RTSP/HTTP stream (IP camera at the intersection), or a
webcam index. Each tracked vehicle is counted once when its ground point (bottom-centre of the
box) crosses a counting line. Each line represents one approach (N/S/E/W) of the intersection.

Vehicle categories
------------------
With the stock COCO model (yolov8n.pt) we get car / bus / truck / motorcycle, and vehicle_types.py
adds Maseru's types: 4+1 cabs by their yellow side belt, minibus taxis by size. A model trained with
our class names (train_custom_model.py) is used directly instead.
"""
import threading
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from . import config
from .analytics import CATEGORIES, CATEGORY_LABELS, analytics
from .vehicle_types import COCO_MAP, TypeClassifier, is_maseru_model

TRACKER_CFG = str(Path(__file__).with_name("bytetrack_maseru.yaml"))
COLORS = {
    "private_car": (80, 200, 80), "sedan_taxi": (0, 215, 255), "minibus_taxi": (0, 140, 255),
    "bus": (255, 120, 0), "truck": (180, 80, 200), "motorcycle": (200, 200, 60),
}


def _side(p1, p2, pt) -> float:
    return (p2[0] - p1[0]) * (pt[1] - p1[1]) - (p2[1] - p1[1]) * (pt[0] - p1[0])


def _within_segment(p1, p2, pt, margin=0.08) -> bool:
    d = np.subtract(p2, p1)
    L2 = float(d @ d) or 1.0
    t = float(np.subtract(pt, p1) @ d) / L2
    return -margin <= t <= 1 + margin


class CameraWorker(threading.Thread):
    def __init__(self, cam_cfg: dict):
        super().__init__(daemon=True, name=f"cam-{cam_cfg['intersection_id']}")
        self.cfg = cam_cfg
        self.iid = cam_cfg["intersection_id"]
        self.lines = cam_cfg.get("lines", [])
        self.running = True
        self.jpeg: bytes | None = None
        self.frame_size = (0, 0)
        self.status = {"state": "starting", "fps": 0.0, "error": None, "model": config.YOLO_MODEL.replace("\\", "/").rsplit("/", 1)[-1]}
        self.line_counts: Counter = Counter()
        self._prev_side: dict = {}
        self._counted: set = set()
        self._track_cats: dict[int, Counter] = {}
        self._last_seen: dict[int, float] = {}
        self._types = TypeClassifier(minibus_rule=config.MINIBUS_HEURISTIC)

    # ------------------------------------------------------------------ setup
    def _open(self):
        src = self.cfg["source"]
        if self._is_file() and (config.ROOT / src).exists():
            src = str(config.ROOT / src)   # video paths in cameras.json are relative to the project
        cap = cv2.VideoCapture(int(src) if str(src).isdigit() else src)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video source: {src}")
        return cap

    def _is_file(self) -> bool:
        s = str(self.cfg["source"])
        return not s.isdigit() and "://" not in s

    def set_lines(self, lines: list[dict]):
        self.lines = lines
        self._prev_side.clear()
        self._counted.clear()

    # --------------------------------------------------------- classification
    def _categorise(self, cls_id: int, names: dict, custom: bool, box, frame) -> str | None:
        if custom:
            name = names.get(cls_id)
            return name if name in CATEGORIES else None
        return self._types.classify(cls_id, frame, box)

    # ------------------------------------------------------------------- main
    def run(self):
        try:
            from ultralytics import YOLO
            model = YOLO(config.YOLO_MODEL)
        except Exception as exc:
            self.status.update(state="error", error=f"Model load failed: {exc}")
            print(f"[camera {self.iid}] {self.status['error']}")
            return

        names = model.names
        custom = is_maseru_model(names)
        classes = None if custom else list(COCO_MAP)
        analytics.set_source(self.iid, "camera")

        try:
            cap = self._open()
        except Exception as exc:
            self.status.update(state="error", error=str(exc))
            print(f"[camera {self.iid}] {exc}")
            return

        is_file = self._is_file()
        vid_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        start_wall, frame_idx = time.time(), 0
        fps_t, fps_n, last_occ = time.time(), 0, 0.0
        self.status["state"] = "running"

        while self.running:
            if is_file:
                # keep video time in step with the wall clock so "5 minutes" really means 5 minutes
                target = int((time.time() - start_wall) * vid_fps)
                if frame_idx > target:
                    time.sleep((frame_idx - target) / vid_fps)
                while frame_idx < target - config.FRAME_STRIDE:
                    if not cap.grab():
                        break
                    frame_idx += 1
            ok, frame = cap.read()
            frame_idx += 1
            if not ok:
                if is_file and self.cfg.get("loop", True):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    start_wall, frame_idx = time.time(), 0
                    self._prev_side.clear()
                    self._counted.clear()
                    continue
                self.status.update(state="reconnecting")
                time.sleep(2)
                try:
                    cap.release()
                    cap = self._open()
                    self.status["state"] = "running"
                except Exception as exc:
                    self.status["error"] = str(exc)
                continue
            if frame_idx % config.FRAME_STRIDE:
                continue

            raw = frame.copy()   # colour checks must not see boxes drawn on earlier vehicles
            h, w = frame.shape[:2]
            self.frame_size = (w, h)
            res = model.track(frame, persist=True, conf=config.DETECT_CONF, imgsz=config.DETECT_IMGSZ,
                              classes=classes, tracker=TRACKER_CFG, verbose=False)[0]
            now = time.time()
            visible = 0
            lines_px = [((l["points"][0] * w, l["points"][1] * h), (l["points"][2] * w, l["points"][3] * h), l)
                        for l in self.lines]

            if res.boxes is not None and res.boxes.id is not None:
                for box, cls_id, tid in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.cls.cpu().numpy().astype(int),
                                            res.boxes.id.cpu().numpy().astype(int)):
                    cat = self._categorise(int(cls_id), names, custom, box, raw)
                    if cat is None:
                        continue
                    visible += 1
                    votes = self._track_cats.setdefault(tid, Counter())
                    votes[cat] += 1
                    cat = votes.most_common(1)[0][0]      # stable label per track
                    self._last_seen[tid] = now
                    x1, y1, x2, y2 = box
                    pt = ((x1 + x2) / 2, y2)                # ground contact point

                    for i, (p1, p2, line) in enumerate(lines_px):
                        side = _side(p1, p2, pt)
                        key = (tid, i)
                        prev = self._prev_side.get(key)
                        self._prev_side[key] = side
                        if prev is None or key in self._counted or prev * side >= 0:
                            continue
                        if not _within_segment(p1, p2, pt):
                            continue
                        direction = "in" if prev < 0 else "out"
                        want = line.get("count_direction", "both")
                        if want != "both" and want != direction:
                            continue
                        self._counted.add(key)
                        self.line_counts[line.get("approach", f"L{i}")] += 1
                        analytics.record_crossing(self.iid, cat, line.get("approach", f"L{i}"), direction)

                    color = COLORS.get(cat, (255, 255, 255))
                    cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                    cv2.putText(frame, f"{CATEGORY_LABELS.get(cat, cat)} #{tid}", (int(x1), int(y1) - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

            # forget tracks that left the scene
            for tid in [t for t, ts in self._last_seen.items() if now - ts > 5]:
                self._last_seen.pop(tid, None)
                self._track_cats.pop(tid, None)
                for k in [k for k in self._prev_side if k[0] == tid]:
                    self._prev_side.pop(k, None)
                self._counted = {k for k in self._counted if k[0] != tid}

            if now - last_occ >= 1.0:
                analytics.record_occupancy(self.iid, visible)
                last_occ = now

            for p1, p2, line in lines_px:
                cv2.line(frame, tuple(map(int, p1)), tuple(map(int, p2)), (0, 0, 255), 3)
                label = f"{line.get('approach', '?')}: {self.line_counts[line.get('approach', '?')]}"
                cv2.putText(frame, label, (int(p1[0]), int(p1[1]) - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            label = f"In view: {visible}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
            x0, y0 = (w - tw) // 2, 40                     # top-centre, clear of camera timestamps
            cv2.rectangle(frame, (x0 - 8, y0 - th - 8), (x0 + tw + 8, y0 + 8), (0, 0, 0), -1)
            cv2.putText(frame, label, (x0, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                self.jpeg = buf.tobytes()
            fps_n += 1
            if now - fps_t >= 2:
                self.status["fps"] = round(fps_n / (now - fps_t), 1)
                fps_t, fps_n = now, 0

        cap.release()
        self.status["state"] = "stopped"

    def stop(self):
        self.running = False
