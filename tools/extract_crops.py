"""Step 1 of training the vehicle-type classifier: cut every tracked vehicle out of some videos.

    python tools/extract_crops.py VIDEO [VIDEO ...]

For each vehicle the tracker follows, up to 3 pictures (taken at different moments, largest first) go to
datasets/crops/unsorted/, named <video>_<track>_<n>.jpg, and its first guess (from the current rules) is
written to datasets/crops/guesses.csv. Sort the pictures with tools/sort_crops.py, then train with
tools/train_classifier.py.
"""
import csv
import sys
from collections import Counter
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.vehicle_types import COCO_MAP, TypeClassifier  # noqa: E402

OUT = ROOT / "datasets" / "crops"
MIN_H = 28          # px; smaller vehicles are too blurry to label or learn from
STRIDE = 3          # look at every 3rd frame
PER_TRACK = 3


def extract(video: Path, model):
    tag = "".join(ch for ch in video.stem if ch.isalnum())[-12:]
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"cannot open {video}")
        return []
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    rules = TypeClassifier()
    tracks: dict[int, dict] = {}
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        if i % STRIDE:
            continue
        if i % 900 == 0:
            print(f"  {video.name}: {100 * i // max(n_frames, 1)}%", flush=True)
        H, W = frame.shape[:2]
        imgsz = 960 if W > 800 else 640
        r = model.track(frame, persist=True, conf=0.25, imgsz=imgsz, classes=list(COCO_MAP),
                        tracker=str(ROOT / "backend/app/bytetrack_maseru.yaml"), verbose=False)[0]
        if r.boxes.id is None:
            continue
        for b, c, t in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.cpu().numpy().astype(int),
                           r.boxes.id.cpu().numpy().astype(int)):
            guess = rules.classify(int(c), frame, b)
            x1, y1, x2, y2 = (int(v) for v in b)
            T = tracks.setdefault(int(t), {"votes": Counter(), "coco": Counter(), "shots": []})
            T["votes"][guess] += 1
            T["coco"][int(c)] += 1
            if y2 - y1 < MIN_H or x1 <= 2 or y1 <= 2 or x2 >= W - 2 or y2 >= H - 2:
                continue            # too small or cut off by the edge of the picture
            # keep the shots spread over time: replace the smallest one only if this frame is 20+ frames later
            area = (x2 - x1) * (y2 - y1)
            pad_x, pad_y = int(0.06 * (x2 - x1)), int(0.06 * (y2 - y1))
            crop = frame[max(0, y1 - pad_y): y2 + pad_y, max(0, x1 - pad_x): x2 + pad_x].copy()
            shots = T["shots"]
            if len(shots) < PER_TRACK and (not shots or i - shots[-1][1] >= 20):
                shots.append((area, i, crop))
            elif shots and i - max(s[1] for s in shots) >= 20:
                k = min(range(len(shots)), key=lambda j: shots[j][0])
                if area > shots[k][0]:
                    shots[k] = (area, i, crop)
    rows = []
    for t, T in tracks.items():
        if sum(T["votes"].values()) < 4 or not T["shots"]:
            continue                # seen only briefly: usually a false detection
        guess = T["votes"].most_common(1)[0][0]
        for n, (_, _, crop) in enumerate(sorted(T["shots"], key=lambda s: -s[0])):
            name = f"{tag}_{t}_{n}.jpg"
            cv2.imwrite(str(OUT / "unsorted" / name), crop)
            rows.append((name, f"{tag}_{t}", guess, COCO_MAP[T["coco"].most_common(1)[0][0]]))
    print(f"  {video.name}: {len({r[1] for r in rows})} vehicles, {len(rows)} pictures")
    return rows


def main():
    from ultralytics import YOLO
    (OUT / "unsorted").mkdir(parents=True, exist_ok=True)
    rows = []
    for v in sys.argv[1:]:
        rows += extract(Path(v), YOLO(str(ROOT / "yolov8n.pt")))   # fresh model = fresh tracker per video
    new = not (OUT / "guesses.csv").exists()
    with open(OUT / "guesses.csv", "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["file", "vehicle", "rule_guess", "detector_class"])
        w.writerows(rows)


if __name__ == "__main__":
    main()
