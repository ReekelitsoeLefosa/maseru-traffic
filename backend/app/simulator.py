"""Synthetic traffic for intersections that don't have a camera yet.

Lets the map, routing and alerts work city-wide while only one real/test camera is connected.
Everything produced here is marked simulated=1 in the database and "simulated" in the UI.
Demand follows typical Maseru weekday peaks; the vehicle mix is an assumption, not survey data.
"""
import math
import random
import threading
import time

from .analytics import analytics
from .network import INTERSECTIONS

MIX = {"private_car": 0.45, "sedan_taxi": 0.17, "minibus_taxi": 0.20, "bus": 0.03, "truck": 0.08, "motorcycle": 0.07}
APPROACHES = {"N": 0.3, "S": 0.3, "E": 0.2, "W": 0.2}


def demand_factor(t: float) -> float:
    """0..~1.1 share of capacity, peaking at the morning, lunch and evening rush hours."""
    lt = time.localtime(t)
    h = lt.tm_hour + lt.tm_min / 60
    peak = lambda c, w, a: a * math.exp(-((h - c) / w) ** 2)
    base = 0.12 + peak(7.6, 0.9, 0.85) + peak(13.0, 0.8, 0.4) + peak(17.2, 1.0, 0.9)
    if lt.tm_wday >= 5:
        base *= 0.6
    return base


def _poisson(lam: float) -> int:
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p < L:
            return k
        k += 1


class Simulator(threading.Thread):
    def __init__(self, intersection_ids: list[str]):
        super().__init__(daemon=True, name="simulator")
        self.ids = intersection_ids
        self.running = True
        self.local = {i: random.uniform(0.8, 1.25) for i in self.ids}   # slowly drifting local variation
        self.incidents: dict[str, float] = {}                          # iid -> expiry timestamp

    def add_incident(self, iid: str, minutes: float):
        self.incidents[iid] = time.time() + minutes * 60

    def run(self):
        while self.running:
            now = time.time()
            for iid in self.ids:
                meta = INTERSECTIONS[iid]
                self.local[iid] = min(1.4, max(0.6, self.local[iid] + random.gauss(0, 0.01)))
                incident = self.incidents.get(iid, 0) > now
                demand = demand_factor(now) * self.local[iid]
                if incident:   # crash / breakdown: queue builds up whatever the time of day
                    demand = max(demand * 1.6, 1.15)
                served = min(demand, 1.0) if not incident else 0.75      # jams reduce throughput
                rate = meta["capacity"] * served / 300.0                   # vehicles per second
                for _ in range(_poisson(rate)):
                    cat = random.choices(list(MIX), weights=list(MIX.values()))[0]
                    app = random.choices(list(APPROACHES), weights=list(APPROACHES.values()))[0]
                    analytics.record_crossing(iid, cat, app, "in", simulated=True, ts=now)
                occ = meta["jam_occ"] * min(1.4, demand ** 1.6) + random.gauss(0, 1)
                analytics.record_occupancy(iid, max(0.0, occ), ts=now, simulated=True)
            time.sleep(1.0)

    def stop_simulating(self, iid: str):
        """A real camera (local or a remote station) now covers this junction."""
        if iid in self.ids:
            self.ids = [i for i in self.ids if i != iid]
