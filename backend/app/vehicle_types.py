"""Maseru vehicle-type rules on top of the stock COCO detector.

Used live by the camera worker and offline by train_custom_model.py to auto-label training frames.

* 4+1 cab (sedan_taxi): a car with the yellow belt/stripe painted along its sides.
* Minibus taxi (Quantum/HiAce): a "car" that is much bigger and taller than a typical car in the same
  view, or a "bus"/"truck" that is only a little bigger than a car.
"""
from collections import deque

import cv2
import numpy as np

MASERU_CLASSES = ["private_car", "sedan_taxi", "minibus_taxi", "bus", "truck", "motorcycle"]
COCO_MAP = {2: "private_car", 3: "motorcycle", 5: "bus", 7: "truck"}

# Yellow in OpenCV HSV (H 0-180). Sodium street lights are orange (H < 15), so they're excluded.
YELLOW_LO = np.array([16, 90, 90], np.uint8)
YELLOW_HI = np.array([38, 255, 255], np.uint8)
MIN_BOX_WIDTH = 24  # px; below this the belt is too few pixels to judge


def is_maseru_model(names: dict) -> bool:
    """True for a model trained with our classes (not the stock COCO model, which also has bus/truck)."""
    return {"private_car", "sedan_taxi", "minibus_taxi"} <= set(names.values())


def yellow_belt_score(frame, box) -> float:
    """Share of the car's side band that is covered by a horizontal yellow stripe (0..1)."""
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = y2 - y1, x2 - x1
    if w < MIN_BOX_WIDTH or h < 10:
        return 0.0
    # doors/sides sit in the middle of the box; skip roof, windows and wheels
    band = frame[max(0, y1 + int(0.35 * h)): y1 + int(0.85 * h), max(0, x1): x2]
    if band.size == 0:
        return 0.0
    mask = cv2.inRange(cv2.cvtColor(band, cv2.COLOR_BGR2HSV), YELLOW_LO, YELLOW_HI)
    cols_with_yellow = (mask.max(axis=0) > 0).mean()     # a belt runs along the car -> many columns
    pixel_share = (mask > 0).mean()
    # a belt is a narrow stripe: if the whole object is yellow (a lit bin, a yellow truck) it isn't one
    whole = cv2.inRange(cv2.cvtColor(frame[max(0, y1):y2, max(0, x1):x2], cv2.COLOR_BGR2HSV), YELLOW_LO, YELLOW_HI)
    if (whole > 0).mean() > 0.30:
        return 0.0
    return float(min(cols_with_yellow, pixel_share * 8))


def has_yellow_belt(frame, box, threshold: float = 0.35) -> bool:
    return yellow_belt_score(frame, box) >= threshold


def mostly_yellow(frame, box) -> bool:
    """Some 4+1 cabs are painted yellow all over rather than having a belt."""
    x1, y1, x2, y2 = (int(v) for v in box)
    if x2 - x1 < MIN_BOX_WIDTH or y2 - y1 < 10:
        return False
    body = frame[max(0, y1 + (y2 - y1) // 4): y2, max(0, x1): x2]   # skip the windscreen/roof
    if body.size == 0:
        return False
    return (cv2.inRange(cv2.cvtColor(body, cv2.COLOR_BGR2HSV), YELLOW_LO, YELLOW_HI) > 0).mean() > 0.30


# A minibus (Quantum/HiAce) stands ~1.3x taller and is a bit longer than an ordinary car at the same spot.
MINIBUS_MIN_REL_H = 1.28
MINIBUS_MIN_REL_W = 1.05
MIN_SIZE_SAMPLES = 150      # car boxes needed before the camera's perspective is known
MIN_JUDGE_PX = 40           # far-away vehicles are too few pixels (and too often half-hidden) to size up


class CropClassifier:
    """Model trained on Maseru vehicle pictures (tools/train_classifier.py): looks at each vehicle the detector
    found and names its type, or says "not_vehicle" (bins, umbrellas, stalls the detector mistakes for cars)."""

    MIN_H = 28   # px; same limit as the training pictures

    def __init__(self, path: str):
        from ultralytics import YOLO
        self.model = YOLO(path, task="classify")
        self.names = self.model.names

    def predict(self, frame, boxes) -> list[tuple[str, float] | None]:
        H, W = frame.shape[:2]
        crops, idx = [], []
        for k, box in enumerate(boxes):
            x1, y1, x2, y2 = (int(v) for v in box)
            if y2 - y1 < self.MIN_H:
                continue        # too small to judge; the rules decide
            px, py = int(0.06 * (x2 - x1)), int(0.06 * (y2 - y1))   # same margin as the training pictures
            crop = frame[max(0, y1 - py): min(H, y2 + py), max(0, x1 - px): min(W, x2 + px)]
            if crop.size:
                crops.append(crop)
                idx.append(k)
        out: list = [None] * len(boxes)
        if crops:
            for k, r in zip(idx, self.model.predict(crops, imgsz=128, verbose=False)):
                p = r.probs
                out[k] = (self.names[int(p.top1)], float(p.top1conf))
        return out


class TypeClassifier:
    """Learns how big an ordinary car looks at each height in this camera view (perspective), then calls a
    vehicle a minibus when it is clearly taller and longer than a car standing at the same spot.

    The previous rule compared every vehicle with the average car in the whole picture, so anything close
    to the camera (bakkies, SUVs, even hatchbacks) looked "big" and was called a minibus.
    """

    def __init__(self, minibus_rule: bool = True):
        self.minibus_rule = minibus_rule
        self.samples: deque = deque(maxlen=2000)   # (ground y, height, width) as fractions of the frame
        self._fit = None
        self._new = 0

    def _learn(self, y, h, w):
        self.samples.append((y, h, w))
        self._new += 1
        if len(self.samples) >= MIN_SIZE_SAMPLES and (self._fit is None or self._new >= 100):
            self._new = 0
            s = np.array(self.samples)
            edges = np.quantile(s[:, 0], np.linspace(0, 1, 9))
            pts = [(s[m, 0].mean(), np.median(s[m, 1]), np.median(s[m, 2]))
                   for a, b in zip(edges[:-1], edges[1:]) if (m := (s[:, 0] >= a) & (s[:, 0] <= b)).sum() >= 10]
            if len(pts) >= 3:
                p = np.array(pts)   # medians per band, so the odd big vehicle doesn't skew the line
                self._fit = (np.polyfit(p[:, 0], p[:, 1], 1), np.polyfit(p[:, 0], p[:, 2], 1))

    def relative_size(self, box, frame_shape):
        """(height, width) compared with an ordinary car at the same ground position, or None if unknown."""
        if self._fit is None:
            return None
        H, W = frame_shape[:2]
        y = box[3] / H
        eh, ew = np.polyval(self._fit[0], y), np.polyval(self._fit[1], y)
        if eh <= 0.01 or ew <= 0.01:
            return None
        return (box[3] - box[1]) / H / eh, (box[2] - box[0]) / W / ew

    def classify(self, coco_cls: int, frame, box) -> str | None:
        cat = COCO_MAP.get(coco_cls)
        if cat is None:
            return None
        H, W = frame.shape[:2]
        cut_off = box[0] <= 3 or box[2] >= W - 3 or box[3] >= H - 3   # partly outside the picture
        if cat == "private_car":
            if has_yellow_belt(frame, box) or mostly_yellow(frame, box):
                return "sedan_taxi"                    # cab check first: a cab is never a minibus
            if not cut_off:
                self._learn(box[3] / H, (box[3] - box[1]) / H, (box[2] - box[0]) / W)
        if not self.minibus_rule or cut_off or cat not in ("private_car", "bus", "truck"):
            return cat
        if box[3] - box[1] < MIN_JUDGE_PX:
            return cat
        rel = self.relative_size(box, frame.shape)
        if rel is None:
            return cat
        rh, rw = rel
        if cat == "private_car" and rh >= MINIBUS_MIN_REL_H and rw >= MINIBUS_MIN_REL_W:
            return "minibus_taxi"
        # the model often calls a minibus a "truck" or "bus"; a minibus is minibus-sized and van-shaped (box about
        # as tall as wide), while lorries are longer (flat bed) or, seen head-on, much taller than wide
        shape = (box[3] - box[1]) / max(box[2] - box[0], 1)
        if cat in ("bus", "truck") and 1.25 <= rh < 1.75 and MINIBUS_MIN_REL_W <= rw < 1.45 and 0.84 <= shape <= 1.08:
            return "minibus_taxi"
        return cat
