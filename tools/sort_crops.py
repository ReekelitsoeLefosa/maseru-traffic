"""Step 2: sort the vehicle pictures into one folder per class.

    python tools/sort_crops.py

Builds datasets/crops/sorted/<class>/ from the rules' first guesses (guesses.csv), with the checked
answers in corrections.csv (vehicle,class) taking priority. Afterwards you can simply open the folders in
File Explorer and drag any picture that is in the wrong folder into the right one; training reads the
folders, so your fixes are what the model learns.

Running it again only adds pictures that are not sorted yet; it never undoes a move you made by hand.
"""
import csv
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CROPS = ROOT / "datasets" / "crops"
CLASSES = ["private_car", "sedan_taxi", "minibus_taxi", "bus", "truck", "motorcycle", "not_vehicle"]


def main():
    fixes = {}
    if (CROPS / "corrections.csv").exists():
        with open(CROPS / "corrections.csv") as f:
            fixes = {r["vehicle"]: r["class"] for r in csv.DictReader(f)}
    bad = set(fixes.values()) - set(CLASSES) - {"skip"}      # skip = too blurry/hidden to label: left out
    if bad:
        raise SystemExit(f"unknown class in corrections.csv: {bad}")
    for c in CLASSES:
        (CROPS / "sorted" / c).mkdir(parents=True, exist_ok=True)
    already = {p.name for p in (CROPS / "sorted").glob("*/*.jpg")}
    added = 0
    with open(CROPS / "guesses.csv") as f:
        for r in csv.DictReader(f):
            src = CROPS / "unsorted" / r["file"]
            if r["file"] in already or not src.exists():
                continue
            label = fixes.get(r["vehicle"], r["rule_guess"])
            if label == "skip":
                continue
            shutil.copy2(src, CROPS / "sorted" / label / r["file"])
            added += 1
    print(f"added {added} pictures")
    for c in CLASSES:
        n = len(list((CROPS / "sorted" / c).glob("*.jpg")))
        print(f"  {c:13} {n}")


if __name__ == "__main__":
    main()
