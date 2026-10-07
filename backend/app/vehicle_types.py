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


class TypeClassifier:
    """Keeps a running median of car sizes in this camera view for the minibus rule."""

    def __init__(self, minibus_rule: bool = True):
        self.minibus_rule = minibus_rule
        self.car_areas: deque = deque(maxlen=300)

    def classify(self, coco_cls: int, frame, box) -> str | None:
        cat = COCO_MAP.get(coco_cls)
        if cat is None:
            return None
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        area = w * h
        if cat == "private_car":
            self.car_areas.append(area)
        if self.minibus_rule and len(self.car_areas) >= 20:
            med = float(np.median(self.car_areas))
            if cat == "private_car" and area > 1.8 * med and h / max(w, 1) > 0.7:
                return "minibus_taxi"
            if cat in ("bus", "truck") and area < 2.2 * med and h / max(w, 1) > 0.7:
                return "minibus_taxi"
        if cat == "private_car" and has_yellow_belt(frame, box):
            return "sedan_taxi"
        return cat
