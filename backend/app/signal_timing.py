"""Adaptive traffic-signal timing (Webster's method) from measured flows.

Instead of a fixed cycle (e.g. 90 s split 50/50), the cycle length and green splits are
recomputed from the vehicles actually counted on each approach in the last 5 minutes.
Flows are converted to passenger-car units (PCU) because a bus or truck occupies the
junction longer than a car.
"""

PCU = {"private_car": 1.0, "sedan_taxi": 1.0, "minibus_taxi": 1.5, "bus": 2.0, "truck": 2.5, "motorcycle": 0.4}
SATURATION_PER_LANE = 1800   # PCU / hour of green / lane
LANES_PER_APPROACH = 2
LOST_TIME_PER_PHASE = 4.0    # s (start-up + clearance)
MIN_GREEN, MIN_CYCLE, MAX_CYCLE = 7.0, 40.0, 120.0
FIXED_CYCLE = 90.0           # the "traditional" fixed-time plan we compare against


def _phases(approach_flows: dict[str, float]) -> dict[str, list[str]]:
    """Group approaches into phases: N+S share a green, E+W share a green, others get their own."""
    phases: dict[str, list[str]] = {}
    for a in approach_flows:
        key = a.upper()[:1]
        name = "North-South" if key in ("N", "S") else "East-West" if key in ("E", "W") else a
        phases.setdefault(name, []).append(a)
    return phases


def _uniform_delay(cycle, green, flow_pcu_h, sat):
    """Webster uniform delay per vehicle (s)."""
    lam = green / cycle
    x = min(flow_pcu_h / (lam * sat), 0.98) if lam > 0 else 0.98
    return cycle * (1 - lam) ** 2 / (2 * (1 - lam * x))


def compute(by_approach_category: dict[str, dict[str, int]], window_seconds: int = 300, queue_ratio: float = 0.0) -> dict:
    """by_approach_category: {"N": {"private_car": 12, "bus": 1, ...}, "S": {...}, ...}
    queue_ratio: vehicles queued in view / jam level. In a jam few cars cross the line, so measured
    flow understates demand; a standing queue (ratio >= 1) is treated as an oversaturated junction."""
    scale = 3600 / window_seconds
    flows = {a: sum(PCU.get(c, 1.0) * n for c, n in cats.items()) * scale for a, cats in by_approach_category.items()}
    phases = _phases(flows)
    if len(phases) < 2:
        return {"available": False, "reason": "Need at least two opposing approaches (e.g. N and E) to time a signal."}

    sat = SATURATION_PER_LANE * LANES_PER_APPROACH
    y = {p: max(flows[a] for a in apps) / sat for p, apps in phases.items()}   # critical flow ratio per phase
    Y = sum(y.values())
    L = LOST_TIME_PER_PHASE * len(phases)
    oversaturated = Y >= 0.95 or queue_ratio >= 1.0
    cycle = MAX_CYCLE if oversaturated else (1.5 * L + 5) / (1 - Y)
    cycle = max(MIN_CYCLE, min(MAX_CYCLE, cycle))

    effective = cycle - L
    # every phase gets the safety minimum; the remaining green is shared in proportion to demand
    spare = effective - MIN_GREEN * len(phases)
    greens = {p: round(MIN_GREEN + spare * (y[p] / Y if Y > 0 else 1 / len(phases)), 1) for p in phases}

    fixed_green = (FIXED_CYCLE - L) / len(phases)

    def avg_delay(c, g_of):
        tot_flow = sum(flows.values()) or 1
        return sum(_uniform_delay(c, g_of(p), max(flows[a] for a in apps), sat) * sum(flows[a] for a in apps)
                   for p, apps in phases.items()) / tot_flow

    d_adaptive = avg_delay(cycle, lambda p: greens[p])
    d_fixed = avg_delay(FIXED_CYCLE, lambda p: fixed_green)

    return {
        "available": True,
        "cycle_s": round(cycle, 1),
        "lost_time_s": L,
        "degree_of_saturation": round(Y, 2),
        "oversaturated": oversaturated,
        "queue_adjusted": queue_ratio >= 1.0,
        "phases": [
            {"phase": p, "approaches": apps, "flow_pcu_h": round(max(flows[a] for a in apps)), "green_s": greens[p]}
            for p, apps in phases.items()
        ],
        "fixed_plan": {"cycle_s": FIXED_CYCLE, "green_each_s": round(fixed_green, 1)},
        "avg_delay_adaptive_s": round(d_adaptive, 1),
        "avg_delay_fixed_s": round(d_fixed, 1),
        "delay_reduction_pct": round(100 * (d_fixed - d_adaptive) / d_fixed, 1) if d_fixed else 0.0,
    }
