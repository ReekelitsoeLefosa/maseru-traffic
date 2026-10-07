"""Train a YOLOv8 model on Maseru footage with our own vehicle classes:
    private_car, sedan_taxi (4+1 cab, yellow belt), minibus_taxi, bus, truck, motorcycle

Step 1 - auto-label frames from your videos (stock detector + Maseru rules in vehicle_types.py):
    python train_custom_model.py autolabel VIDEO [VIDEO ...] --every 0.5
Step 2 - REVIEW the labels. Auto-labels copy the rules' mistakes (e.g. a yellow-lit bin labelled as a
    4+1 cab), and the model learns whatever the labels say. Open datasets/maseru/preview/*.jpg, then fix
    labels in Label Studio / Roboflow / CVAT (import the YOLO-format folder datasets/maseru).
Step 3 - train (CPU works; checkpoints are saved every epoch, so you can stop early):
    python train_custom_model.py train --epochs 30
Step 4 - set YOLO_MODEL=runs/detect/maseru/weights/best.pt in .env and restart the app.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
from app.vehicle_types import COCO_MAP, MASERU_CLASSES, TypeClassifier  # noqa: E402

DATA = ROOT / "datasets" / "maseru"
COLORS = {"private_car": (80, 200, 80), "sedan_taxi": (0, 215, 255), "minibus_taxi": (0, 140, 255),
          "bus": (255, 120, 0), "truck": (180, 80, 200), "motorcycle": (200, 200, 60)}


def autolabel(videos: list[str], every: float, val_share: float, conf: float, imgsz: int):
    import cv2
    from ultralytics import YOLO

    model = YOLO(str(ROOT / "yolov8n.pt"))
    for sub in ("images/train", "images/val", "labels/train", "labels/val", "preview"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    totals: Counter = Counter()
    n_images = 0
    for video in videos:
        cap = cv2.VideoCapture(video)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
        types = TypeClassifier()          # size statistics are per camera view
        stem = Path(video).stem
        times = [i * every for i in range(int(duration / every))]
        sharp = {t: sharpness(cap, t) for t in times}
        # frames blurred by camera motion/haze: the detector misses most vehicles there, and a frame
        # with unlabelled vehicles teaches the model that vehicles are background - so skip them
        cutoff = 0.5 * sorted(sharp.values())[len(sharp) // 2]
        skipped = 0
        for t in times:
            if sharp[t] < cutoff:
                skipped += 1
                continue
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            res = model.predict(frame, imgsz=imgsz, conf=conf, classes=list(COCO_MAP), verbose=False)[0]
            lines, preview = [], frame.copy()
            for box, cls in zip(res.boxes.xyxy.tolist(), res.boxes.cls.int().tolist()):
                cat = types.classify(cls, frame, box)
                if cat is None:
                    continue
                x1, y1, x2, y2 = box
                lines.append(f"{MASERU_CLASSES.index(cat)} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                             f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
                totals[cat] += 1
                cv2.rectangle(preview, (int(x1), int(y1)), (int(x2), int(y2)), COLORS[cat], 2)
                cv2.putText(preview, cat, (int(x1), int(y1) - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.45, COLORS[cat], 1)
            # the last part of each video is held out for validation (neighbouring frames look alike)
            split = "val" if t >= duration * (1 - val_share) else "train"
            name = f"{stem}_{int(t * 1000):07d}"
            cv2.imwrite(str(DATA / "images" / split / f"{name}.jpg"), frame)
            (DATA / "labels" / split / f"{name}.txt").write_text("\n".join(lines), encoding="utf-8")
            if n_images % 10 == 0:
                cv2.imwrite(str(DATA / "preview" / f"{name}.jpg"), preview)
            n_images += 1
        print(f"{stem}: done ({skipped} blurred frames skipped)")
    write_yaml()
    print(f"{n_images} images labelled. Boxes per class: {dict(totals)}")
    print(f"Review: {DATA / 'preview'}")


def sharpness(cap, t: float) -> float:
    import cv2
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    if not ok:
        return 0.0
    gray = cv2.cvtColor(cv2.resize(frame, (640, int(640 * frame.shape[0] / frame.shape[1]))), cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def write_yaml() -> Path:
    yaml = DATA / "data.yaml"
    yaml.write_text(f"path: {DATA.as_posix()}\ntrain: images/train\nval: images/val\nnames:\n"
                    + "".join(f"  {i}: {c}\n" for i, c in enumerate(MASERU_CLASSES)), encoding="utf-8")
    return yaml


def train(epochs: int, imgsz: int, batch: int, base: str):
    from ultralytics import YOLO
    YOLO(str(ROOT / base)).train(data=str(write_yaml()), epochs=epochs, imgsz=imgsz, batch=batch,
                                 workers=0, device="cpu", project=str(ROOT / "runs" / "detect"),
                                 name="maseru", exist_ok=True, patience=10, plots=True)
    print(f"Best model: {ROOT / 'runs/detect/maseru/weights/best.pt'}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("autolabel")
    a.add_argument("videos", nargs="+")
    a.add_argument("--every", type=float, default=0.5, help="seconds between sampled frames")
    a.add_argument("--val-share", type=float, default=0.2)
    a.add_argument("--conf", type=float, default=0.15)
    a.add_argument("--imgsz", type=int, default=1280)
    t = sub.add_parser("train")
    t.add_argument("--epochs", type=int, default=30)
    t.add_argument("--imgsz", type=int, default=832)
    t.add_argument("--batch", type=int, default=8)
    t.add_argument("--base", default="yolov8n.pt")
    args = p.parse_args()
    if args.cmd == "autolabel":
        autolabel(args.videos, args.every, args.val_share, args.conf, args.imgsz)
    else:
        train(args.epochs, args.imgsz, args.batch, args.base)
