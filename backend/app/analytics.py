"""Real-time traffic analytics per intersection.

* Counts every vehicle that crosses a counting line, by category and approach.
* Closes a fixed 5-minute window (aligned to the clock, e.g. 07:00-07:05), stores it, and
  fires listeners (used for SMS / WhatsApp alerts).
* Keeps a rolling 5-minute view for the live dashboard.
* Converts flow + queue occupancy into a congestion score and level.
"""
import threading
import time
from collections import Counter, deque

from . import config, db
from .network import INTERSECTIONS

CATEGORIES = ["private_car", "sedan_taxi", "minibus_taxi", "bus", "truck", "motorcycle"]
CATEGORY_LABELS = {
    "private_car": "Private car",
    "sedan_taxi": "4+1 cab",
    "minibus_taxi": "Minibus taxi",
    "bus": "Bus",
    "truck": "Truck",
    "motorcycle": "Motorcycle",
}
PUBLIC_TRANSPORT = {"sedan_taxi", "minibus_taxi", "bus"}

LEVELS = [(0.4, "LOW"), (0.7, "MODERATE"), (0.9, "HEAVY"), (float("inf"), "SEVERE")]
LEVEL_TEXT = {
    "LOW": "Free flowing",
    "MODERATE": "Moderate traffic",
    "HEAVY": "Heavy traffic",
    "SEVERE": "Severe congestion",
}


def level_for(score: float) -> str:
    for limit, name in LEVELS:
        if score < limit:
            return name
    return "SEVERE"


class IntersectionStats:
    def __init__(self, iid: str):
        self.iid = iid
        self.meta = INTERSECTIONS[iid]
        self.source = "simulated"
        self.events: deque = deque()            # (ts, category, approach) for rolling window
        self.occupancy: deque = deque()         # (ts, vehicles in view)
        self.window_start = self._align(time.time())
        self.window_counts: Counter = Counter()
        self.window_approach: Counter = Counter()
        self.window_occ: list[float] = []
        self.total_today = 0
        self.last_window: dict | None = None

    @staticmethod
    def _align(ts: float) -> float:
        return ts - (ts % config.WINDOW_SECONDS)

    def _trim(self, now: float):
        while self.events and self.events[0][0] < now - config.WINDOW_SECONDS:
            self.events.popleft()
        while self.occupancy and self.occupancy[0][0] < now - 60:
            self.occupancy.popleft()

    def score(self, count: int, occ: float) -> float:
        flow_ratio = count / self.meta["capacity"]
        density_ratio = occ / self.meta["jam_occ"]
        return round(max(flow_ratio, density_ratio), 3)

    def snapshot(self) -> dict:
        now = time.time()
        self._trim(now)
        cats = Counter(c for _, c, _ in self.events)
        apps = Counter(a for _, _, a in self.events)
        app_cat: dict[str, Counter] = {}
        for _, c, a in self.events:
            app_cat.setdefault(a, Counter())[c] += 1
        occ = sum(o for _, o in self.occupancy) / len(self.occupancy) if self.occupancy else 0.0
        count = len(self.events)
        s = self.score(count, occ)
        lvl = level_for(s)
        return {
            "id": self.iid,
            "name": self.meta["name"],
            "lat": self.meta["lat"],
            "lon": self.meta["lon"],
            "source": self.source,
            "rolling_5min_total": count,
            "by_category": {c: cats.get(c, 0) for c in CATEGORIES},
            "by_approach": dict(apps),
            "by_approach_category": {a: dict(c) for a, c in app_cat.items()},
            "public_transport": sum(cats.get(c, 0) for c in PUBLIC_TRANSPORT),
            "vehicles_per_hour": count * (3600 // config.WINDOW_SECONDS),
            "occupancy": round(occ, 1),
            "capacity_5min": self.meta["capacity"],
            "score": s,
            "level": lvl,
            "level_text": LEVEL_TEXT[lvl],
            "current_window": {
                "start": self.window_start,
                "end": self.window_start + config.WINDOW_SECONDS,
                "total": sum(self.window_counts.values()),
                "by_category": {c: self.window_counts.get(c, 0) for c in CATEGORIES},
            },
            "last_window": self.last_window,
            "total_today": self.total_today,
        }


class TrafficAnalytics:
    def __init__(self):
        self.lock = threading.Lock()
        self.stats = {iid: IntersectionStats(iid) for iid in INTERSECTIONS}
        self.window_listeners = []   # callables(window_summary_dict)
        self.event_listeners = []    # callables(event_dict) for real (camera) measurements, e.g. the cloud uplink
        self._day = time.localtime().tm_yday

    def on_window_closed(self, fn):
        self.window_listeners.append(fn)

    def on_event(self, fn):
        self.event_listeners.append(fn)

    def _emit(self, event: dict):
        for fn in self.event_listeners:
            try:
                fn(event)
            except Exception as exc:
                print(f"[analytics] event listener error: {exc}")

    def set_source(self, iid: str, source: str):
        self.stats[iid].source = source

    def record_crossing(self, iid, category, approach="main", direction="in", simulated=False, ts=None):
        ts = ts or time.time()
        with self.lock:
            st = self.stats[iid]
            st.events.append((ts, category, approach))
            st.window_counts[category] += 1
            st.window_approach[approach] += 1
            st.total_today += 1
        db.add_crossing(ts, iid, approach, category, direction, simulated)
        if not simulated:
            self._emit({"type": "crossing", "iid": iid, "category": category, "approach": approach,
                        "direction": direction, "ts": ts})

    def record_occupancy(self, iid, vehicles_in_view: float, ts=None, simulated=False):
        ts = ts or time.time()
        with self.lock:
            st = self.stats[iid]
            st.occupancy.append((ts, vehicles_in_view))
            st.window_occ.append(vehicles_in_view)
        if not simulated:
            self._emit({"type": "occupancy", "iid": iid, "value": vehicles_in_view, "ts": ts})

    def tick(self):
        """Call ~1/s. Closes finished 5-minute windows and notifies listeners."""
        now = time.time()
        closed = []
        with self.lock:
            if time.localtime().tm_yday != self._day:
                self._day = time.localtime().tm_yday
                for st in self.stats.values():
                    st.total_today = 0
            for st in self.stats.values():
                if now >= st.window_start + config.WINDOW_SECONDS:
                    total = sum(st.window_counts.values())
                    occ = sum(st.window_occ) / len(st.window_occ) if st.window_occ else 0.0
                    s = st.score(total, occ)
                    w = {
                        "intersection_id": st.iid,
                        "name": st.meta["name"],
                        "window_start": st.window_start,
                        "window_end": st.window_start + config.WINDOW_SECONDS,
                        "total": total,
                        "by_category": {c: st.window_counts.get(c, 0) for c in CATEGORIES},
                        "by_approach": dict(st.window_approach),
                        "public_transport": sum(st.window_counts.get(c, 0) for c in PUBLIC_TRANSPORT),
                        "avg_occupancy": round(occ, 1),
                        "score": s,
                        "level": level_for(s),
                        "source": st.source,
                    }
                    st.last_window = w
                    st.window_start = st._align(now)
                    st.window_counts, st.window_approach, st.window_occ = Counter(), Counter(), []
                    closed.append(w)
        for w in closed:
            db.save_window(w)
            for fn in self.window_listeners:
                try:
                    fn(w)
                except Exception as exc:  # never let a notifier break analytics
                    print(f"[analytics] listener error: {exc}")

    def snapshot(self) -> dict[str, dict]:
        with self.lock:
            return {iid: st.snapshot() for iid, st in self.stats.items()}

    def scores(self) -> dict[str, float]:
        return {iid: s["score"] for iid, s in self.snapshot().items()}


analytics = TrafficAnalytics()
