"""Maseru road network: monitored junctions (graph nodes) and the real roads linking them (edges).

Loaded from maseru_network.json, which tools/build_network.py generates from OpenStreetMap: junctions
sit on real road intersections and every link follows the actual road shape, with its real length.
To add a junction, add it to JUNCTIONS in tools/build_network.py and re-run that script.

capacity   = vehicles per 5-minute window the monitored approaches can discharge before queuing.
jam_occ    = number of vehicles simultaneously visible in the camera view that indicates a queue/jam.
"""
import json
from pathlib import Path

_data = json.loads(Path(__file__).with_name("maseru_network.json").read_text(encoding="utf-8"))

INTERSECTIONS: dict[str, dict] = {
    j["id"]: {k: j[k] for k in ("name", "lat", "lon", "capacity", "jam_occ", "roads")} for j in _data["junctions"]
}

# Every link is two-way; geometry is stored from "from" to "to" as [[lat, lon], ...].
ROADS: list[dict] = _data["edges"]


def build_edges() -> dict[str, list[dict]]:
    adj: dict[str, list[dict]] = {k: [] for k in INTERSECTIONS}
    for e in ROADS:
        base = {"road": e["road"], "km": e["km"], "free_min": e["free_min"]}
        adj[e["from"]].append({**base, "to": e["to"], "geometry": e["geometry"]})
        adj[e["to"]].append({**base, "to": e["from"], "geometry": e["geometry"][::-1]})
    return adj


ADJ = build_edges()
