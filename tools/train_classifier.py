"""Step 3: train the vehicle-type classifier on the sorted pictures.

    python tools/train_classifier.py [--epochs 40]

The camera still finds vehicles with YOLO; this small second model then looks at each vehicle's picture
and says which Maseru type it is (or that it isn't a vehicle at all, e.g. a bin). Saves
models/vehicle_types_cls.pt, which the app uses automatically when the file exists.

All pictures of one vehicle go into the same half (training or test), so the test score shows how well
the model does on vehicles it has never seen.
"""
import argparse
import random
import shutil
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SORTED = ROOT / "datasets" / "crops" / "sorted"
SPLIT = ROOT / "datasets" / "crops_split"
OUT = ROOT / "models" / "vehicle_types_cls.pt"


def split(val_share=0.2, seed=7):
    shutil.rmtree(SPLIT, ignore_errors=True)
    rng = random.Random(seed)
    for cls_dir in sorted(p for p in SORTED.iterdir() if p.is_dir()):
        by_vehicle = defaultdict(list)
        for p in cls_dir.glob("*.jpg"):
            by_vehicle[p.stem.rsplit("_", 1)[0]].append(p)
        if not by_vehicle:
            continue
        vehicles = sorted(by_vehicle)
        rng.shuffle(vehicles)
        n_val = max(1, round(len(vehicles) * val_share)) if len(vehicles) > 1 else 0
        for k, v in enumerate(vehicles):
            part = "val" if k < n_val else "train"
            (SPLIT / part / cls_dir.name).mkdir(parents=True, exist_ok=True)
            for p in by_vehicle[v]:
                shutil.copy2(p, SPLIT / part / cls_dir.name / p.name)
        print(f"  {cls_dir.name:13} {len(vehicles) - n_val:4} vehicles to learn from, {n_val:3} to test on")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--imgsz", type=int, default=128)
    args = ap.parse_args()
    split()
    from ultralytics import YOLO
    model = YOLO("yolov8n-cls.pt")          # ImageNet-pretrained; we only teach it our classes
    model.train(data=str(SPLIT), epochs=args.epochs, imgsz=args.imgsz, batch=32, device="cpu",
                project=str(ROOT / "runs" / "classify"), name="vehicle_types", exist_ok=True,
                fliplr=0.5, hsv_h=0.0, plots=False, verbose=False)   # hsv_h=0: colour matters (yellow cabs)
    best = ROOT / "runs" / "classify" / "vehicle_types" / "weights" / "best.pt"
    OUT.parent.mkdir(exist_ok=True)
    shutil.copy2(best, OUT)
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
