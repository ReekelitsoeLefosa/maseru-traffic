"""Build the Maseru junction network from real OpenStreetMap roads.

    python tools/build_network.py            # downloads roads (cached in data/osm_maseru_roads.json)
    python tools/build_network.py --refresh  # re-download

1. Downloads Maseru's trunk/primary/secondary/tertiary roads (Overpass API).
2. Joins them into a road graph (roads that meet share exact coordinates in OSM).
3. Places each monitored junction on the real road intersection nearest its anchor point.
4. Links neighbouring junctions by following the actual roads, keeping the road shape, length and name.
Output: backend/app/maseru_network.json, used by network.py for routing and drawing the map.

Add a junction: append to JUNCTIONS with an anchor near the real intersection, then re-run.
"""
import argparse
import heapq
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "osm_maseru_roads.json"
OUT = ROOT / "backend" / "app" / "maseru_network.json"
BBOX = (-29.38, 27.42, -29.27, 27.57)  # south, west, north, east
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
SPEED_KMH = {"trunk": 50, "primary": 40, "secondary": 35, "tertiary": 30}

# id, name, anchor (lat, lon) near the real junction, capacity (veh / 5 min), jam_occ (vehicles in view),
# optional: a road the junction must be on
JUNCTIONS = [
    ("maseru_bridge",   "Maseru Bridge Border",          (-29.29805, 27.45471), 120, 20),
    ("ha_hoohlo",       "Ha Hoohlo",                     (-29.30350, 27.46670), 110, 18),
    ("kingsway_pioneer", "Pioneer Roundabout (Kingsway / Pioneer Rd)", (-29.31847, 27.47432), 180, 25),
    ("pioneer_mall",    "Pioneer Mall (Pioneer Rd)",     (-29.31670, 27.47747), 140, 20),
    ("kingsway_cbd",    "Kingsway CBD (Queen II Hospital)", (-29.31444, 27.48510), 170, 25, "Kingsway"),
    ("sefika_rank",     "Sefika Taxi Rank",              (-29.31329, 27.49244), 130, 30),
    ("main_circle",     "Main Circle (Cathedral)",       (-29.31699, 27.49494), 200, 30),
    ("stadium",         "Setsoto Stadium",               (-29.31106, 27.50137), 150, 22),
    ("lakeside",        "Lakeside (Main North 1)",       (-29.31283, 27.51018), 160, 22),
    ("airport",         "Mejametalana (Airport Rd)",     (-29.30272, 27.50379), 140, 20),
    ("moshoeshoe_rd",   "Moshoeshoe Rd",                 (-29.30523, 27.48381), 160, 22),
    ("maseru_mall",     "Maseru Mall",                   (-29.33597, 27.48180), 150, 22),
    ("lekhaloaneng",    "Lekhaloaneng (Main South)",     (-29.33407, 27.50879), 190, 28),
    ("ha_thetsane",     "Ha Thetsane",                   (-29.34917, 27.44972), 130, 20),
]
MAX_SNAP_M = 600
MAX_LINK_KM = 8
ZONE_M = 80   # road points this close to a junction belong to it (roundabouts, dual carriageways)
DETOUR_KEEP = 1.15  # drop a link if going via another junction is at most 15% longer (shared corridor)


def haversine_m(a, b) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def fetch_roads(refresh: bool) -> list[dict]:
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text(encoding="utf-8"))["elements"]
    s, w, n, e = BBOX
    query = (f'[out:json][timeout:120];way["highway"~"^(trunk|primary|secondary|tertiary)(_link)?$"]'
             f"({s},{w},{n},{e});out geom tags;")
    last = None
    for url in OVERPASS:
        try:
            r = httpx.post(url, data={"data": query}, timeout=180,
                           headers={"User-Agent": "maseru-smart-traffic/0.1"})
            r.raise_for_status()
            CACHE.parent.mkdir(exist_ok=True)
            CACHE.write_text(r.text, encoding="utf-8")
            print(f"Downloaded roads from {url}")
            return r.json()["elements"]
        except Exception as exc:  # try the next mirror
            last = exc
            print(f"  {url}: {exc}")
    sys.exit(f"Could not download roads: {last}")


def build_graph(ways):
    """Undirected graph keyed by rounded coordinate. (One-way carriageways are treated as two-way:
    at city scale the opposite carriageway runs alongside, so route shape and length barely change.)"""
    adj = defaultdict(dict)   # node -> {neighbour: (metres, highway class, road name)}
    for w in ways:
        tags = w.get("tags", {})
        cls = tags["highway"].replace("_link", "")
        name = tags.get("name") or tags.get("ref") or cls.title() + " road"
        pts = [(round(p["lat"], 7), round(p["lon"], 7)) for p in w["geometry"]]
        for a, b in zip(pts, pts[1:]):
            d = haversine_m(a, b)
            if a != b and (b not in adj[a] or adj[a][b][0] > d):
                adj[a][b] = (d, cls, name)
                adj[b][a] = (d, cls, name)
    return adj


def largest_component(adj):
    seen, best = set(), set()
    for start in adj:
        if start in seen:
            continue
        comp, stack = set(), [start]
        while stack:
            u = stack.pop()
            if u in comp:
                continue
            comp.add(u)
            stack.extend(v for v in adj[u] if v not in comp)
        seen |= comp
        if len(comp) > len(best):
            best = comp
    return best


def snap(anchor, adj, nodes, road=None):
    """Nearest real intersection (3+ road directions) to the anchor, else nearest road point."""
    if road:
        nodes = [n for n in nodes if any(road.lower() in adj[n][v][2].lower() for v in adj[n])]
    junctions = [n for n in nodes if len(adj[n]) >= 3]
    best = min(junctions, key=lambda n: haversine_m(anchor, n))
    if haversine_m(anchor, best) > MAX_SNAP_M:
        best = min(nodes, key=lambda n: haversine_m(anchor, n))
    return best, haversine_m(anchor, best)


def zones(placed_nodes, nodes):
    """Assign every road point within ZONE_M of a junction to that junction."""
    zone = {}
    for n in nodes:
        near = min(placed_nodes, key=lambda j: haversine_m(n, j))
        if haversine_m(n, near) <= ZONE_M:
            zone[n] = placed_nodes[near]
    return zone


def links_from(jid, adj, zone):
    """Follow roads out of junction jid until another junction's zone is reached (zones aren't crossed)."""
    starts = [n for n, z in zone.items() if z == jid]
    dist, prev = {n: 0.0 for n in starts}, {}
    pq, found = [(0.0, n) for n in starts], {}
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u] or d > MAX_LINK_KM * 1000:
            continue
        other = zone.get(u)
        if other and other != jid:
            if other not in found:
                found[other] = u
            continue
        for v, (m, cls, name) in adj[u].items():
            nd = d + m
            if nd < dist.get(v, float("inf")):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    out = {}
    for other, end in found.items():
        path, n = [end], end
        while n in prev:
            n = prev[n]
            path.append(n)
        out[other] = list(reversed(path))
    return out


def describe(path, adj):
    km, minutes, names = 0.0, 0.0, Counter()
    for a, b in zip(path, path[1:]):
        m, cls, name = adj[a][b]
        km += m / 1000
        minutes += m / 1000 / SPEED_KMH[cls] * 60
        names[name] += m
    return round(km, 3), round(minutes, 2), names.most_common(1)[0][0]


def simplify(path, tol_m=8.0):
    """Drop points that barely change the shape (keeps the JSON small)."""
    if len(path) < 3:
        return path
    keep = [path[0]]
    for i in range(1, len(path) - 1):
        a, b, c = keep[-1], path[i], path[i + 1]
        ab, bc, ac = haversine_m(a, b), haversine_m(b, c), haversine_m(a, c)
        if ab + bc - ac > tol_m / 10 or ab > 150:
            keep.append(b)
    keep.append(path[-1])
    return keep


def prune(edges):
    """Remove links that just duplicate a corridor already covered via another junction."""
    def shortest(a, b, skip):
        adj = defaultdict(list)
        for e in edges:
            if e is not skip and e.get("keep", True):
                adj[e["from"]].append((e["to"], e["km"]))
                adj[e["to"]].append((e["from"], e["km"]))
        dist, pq = {a: 0.0}, [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                return d
            if d > dist[u]:
                continue
            for v, km in adj[u]:
                if d + km < dist.get(v, float("inf")):
                    dist[v] = d + km
                    heapq.heappush(pq, (d + km, v))
        return float("inf")

    for e in sorted(edges, key=lambda e: -e["km"]):   # longest first
        if shortest(e["from"], e["to"], e) <= DETOUR_KEEP * e["km"]:
            e["keep"] = False
    return [{k: v for k, v in e.items() if k != "keep"} for e in edges if e.get("keep", True)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    adj = build_graph(fetch_roads(args.refresh))
    nodes = largest_component(adj)
    print(f"Road graph: {len(nodes)} points in the connected network")

    placed, by_node = {}, {}
    for jid, name, anchor, cap, jam, *road in JUNCTIONS:
        node, off = snap(anchor, adj, nodes, road[0] if road else None)
        if node in by_node:
            print(f"  ! {jid} snaps to the same point as {by_node[node]}; move its anchor")
            continue
        by_node[node] = jid
        roads = sorted({adj[node][v][2] for v in adj[node]})
        placed[jid] = {"id": jid, "name": name, "lat": node[0], "lon": node[1], "capacity": cap,
                       "jam_occ": jam, "roads": roads}
        print(f"  {jid:17s} {off:5.0f} m from anchor  on {', '.join(roads)}")

    zone = zones(by_node, nodes)
    edges, seen = [], set()
    for jid, j in placed.items():
        for other, path in links_from(jid, adj, zone).items():
            key = tuple(sorted((jid, other)))
            if key in seen:
                continue
            seen.add(key)
            # draw from junction centre to junction centre
            path = [(j["lat"], j["lon"])] + path + [(placed[other]["lat"], placed[other]["lon"])]
            km, minutes, road = describe(path[1:-1], adj) if len(path) > 3 else (0.05, 0.1, "link")
            edges.append({"from": jid, "to": other, "road": road, "km": km, "free_min": minutes,
                          "geometry": [[p[0], p[1]] for p in simplify(path)]})
    edges = prune(edges)
    lonely = [j for j in placed if not any(j in (e["from"], e["to"]) for e in edges)]
    print(f"{len(placed)} junctions, {len(edges)} road links" + (f"; not connected: {lonely}" if lonely else ""))
    OUT.write_text(json.dumps({"source": "OpenStreetMap contributors (ODbL)", "junctions": list(placed.values()),
                               "edges": edges}, indent=1), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
