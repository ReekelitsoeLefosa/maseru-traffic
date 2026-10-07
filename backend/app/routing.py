"""Congestion-aware route suggestions over the Maseru network.

Edge travel time = free-flow time x delay factor, where the delay factor grows with the congestion
score measured at the intersections the edge connects. We return the best route under current
conditions, the "usual" (free-flow shortest) route, and a second-best alternative.
"""
import difflib
import heapq

from .network import ADJ, INTERSECTIONS


def delay_factor(score: float) -> float:
    # score ~0 (empty) .. >=1 (at/over capacity). 1.0x when free, ~3.5x when jammed.
    s = max(0.0, min(score, 1.6))
    return 1.0 + 1.0 * s ** 2


def junction_delay(score: float) -> float:
    """Extra minutes queuing at a junction: ~0 when free, ~1.5 at HEAVY, ~3.5 at capacity, ~7 when jammed."""
    return 6.0 * max(0.0, min(score, 1.4) - 0.3) ** 1.5


def edge_minutes(u: str, e: dict, scores: dict[str, float]) -> float:
    return (e["free_min"] * delay_factor((scores.get(u, 0) + scores.get(e["to"], 0)) / 2)
            + junction_delay(scores.get(e["to"], 0)))


def _dijkstra(src, dst, weight, banned_edges=frozenset()):
    dist, prev = {src: 0.0}, {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > dist.get(u, float("inf")):
            continue
        for e in ADJ[u]:
            if (u, e["to"]) in banned_edges:
                continue
            nd = d + weight(u, e)
            if nd < dist.get(e["to"], float("inf")):
                dist[e["to"]], prev[e["to"]] = nd, (u, e)
                heapq.heappush(pq, (nd, e["to"]))
    if dst not in dist:
        return None
    path, node = [], dst
    while node != src:
        u, e = prev[node]
        path.append((u, e))
        node = u
    return list(reversed(path))


def _describe(path, scores):
    nodes = [path[0][0]] + [e["to"] for _, e in path]
    minutes = sum(edge_minutes(u, e, scores) for u, e in path)
    roads = []
    for _, e in path:
        if not roads or roads[-1] != e["road"]:
            roads.append(e["road"])
    return {
        "nodes": nodes,
        "names": [INTERSECTIONS[n]["name"] for n in nodes],
        # the real road shape: each link's geometry, joined end to end
        "coords": [pt for i, (_, e) in enumerate(path) for pt in (e["geometry"] if i == 0 else e["geometry"][1:])],
        "roads": roads,
        "km": round(sum(e["km"] for _, e in path), 2),
        "minutes": round(minutes, 1),
        "free_minutes": round(sum(e["free_min"] for _, e in path), 1),
        "worst_level_nodes": [n for n in nodes if scores.get(n, 0) >= 0.7],
    }


def suggest(src: str, dst: str, scores: dict[str, float]) -> dict:
    if src not in INTERSECTIONS or dst not in INTERSECTIONS:
        raise ValueError("Unknown origin or destination")
    if src == dst:
        raise ValueError("Origin and destination are the same")

    def live(u, e):
        return edge_minutes(u, e, scores)

    best = _dijkstra(src, dst, live)
    usual = _dijkstra(src, dst, lambda u, e: e["free_min"])
    if best is None:
        raise ValueError("No route found")

    # Second-best: block each edge of the best route in turn, keep the cheapest detour (Yen's k=2).
    alt, alt_cost = None, float("inf")
    for u, e in best:
        cand = _dijkstra(src, dst, live, frozenset({(u, e["to"]), (e["to"], u)}))
        if cand:
            cost = sum(live(a, b) for a, b in cand)
            if cost < alt_cost:
                alt, alt_cost = cand, cost

    best_d, usual_d = _describe(best, scores), _describe(usual, scores)
    return {
        "from": INTERSECTIONS[src]["name"],
        "to": INTERSECTIONS[dst]["name"],
        "recommended": best_d,
        "usual": usual_d,
        "alternative": _describe(alt, scores) if alt else None,
        "reroute_advised": best_d["nodes"] != usual_d["nodes"],
        "minutes_saved": round(usual_d["minutes"] - best_d["minutes"], 1),
    }


def find_place(text: str) -> str | None:
    """Fuzzy-match free text (e.g. from WhatsApp) to an intersection id."""
    text = text.strip().lower()
    if not text:
        return None
    if text in INTERSECTIONS:
        return text
    for k, v in INTERSECTIONS.items():
        if text in v["name"].lower() or text.replace(" ", "_") in k:
            return k
    names = {v["name"].lower(): k for k, v in INTERSECTIONS.items()}
    match = difflib.get_close_matches(text, list(names), n=1, cutoff=0.4)
    return names[match[0]] if match else None
