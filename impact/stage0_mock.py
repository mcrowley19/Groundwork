"""Stage 0: hand-made mock of every output file, in data/impact/mock/.

The base facts below (positions, unit counts, statuses, stop corridors, schools, rail lines,
route paths, water connection) are hand-placed around Dublin 1–4 and clearly labelled MOCK.
Every derived number is computed from them with the same config.py formulas the real
pipeline uses, so the mock is internally consistent (distances, classes, pupils, scores).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from pyproj import Transformer
from shapely.geometry import LineString, Point

from . import config as C
from .common import MOCK, fc, feature, round_geojson, write_json
from .scoring import dev_pressure_ratio, overall_score, schools_score, transport_score, water_score

_to_m = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)
_to_ll = Transformer.from_crs("EPSG:2157", "EPSG:4326", always_xy=True)


def _m(lng, lat):
    return _to_m.transform(lng, lat)


def _dist(a, b):
    (x1, y1), (x2, y2) = _m(*a), _m(*b)
    return math.hypot(x1 - x2, y1 - y2)


# (app_id, description, lng, lat, units, status, water_connected, ext_route_m)
DEVS = [
    ("MOCK-01", "MOCK: 420 apartments, Spencer Dock Block C", -6.2370, 53.3493, 420, "granted", True, 0),
    ("MOCK-02", "MOCK: 310 build-to-rent units, North Wall Quay", -6.2305, 53.3480, 310, "pending", True, 0),
    ("MOCK-03", "MOCK: 180 apartments, East Wall Road", -6.2265, 53.3570, 180, "granted", False, 340),
    ("MOCK-04", "MOCK: 96 apartments, Sheriff Street Upper", -6.2410, 53.3520, 96, "granted", True, 0),
    ("MOCK-05", "MOCK: 240 apartments, Grand Canal Dock", -6.2385, 53.3415, 240, "pending", True, 0),
    ("MOCK-06", "MOCK: 600 units, Ringsend / Poolbeg West", -6.2190, 53.3395, 600, "pending", False, 520),
    ("MOCK-07", "MOCK: 72 houses, Irishtown", -6.2230, 53.3345, 72, "granted", True, 0),
    ("MOCK-08", "MOCK: 150 apartments, Shelbourne Road", -6.2310, 53.3330, 150, "granted", True, 0),
    ("MOCK-09", "MOCK: 45 houses, Strand Road Sandymount", -6.2160, 53.3300, 45, "pending", True, 0),
    ("MOCK-10", "MOCK: 60 apartments, Mountjoy Square", -6.2545, 53.3575, 60, "granted", True, 0),
    ("MOCK-11", "MOCK: 130 student beds, Pearse Street", -6.2470, 53.3440, 130, "granted", True, 0),
    ("MOCK-12", "MOCK: 850 units, Poolbeg Peninsula", -6.2050, 53.3410, 850, "pending", False, 1180),
]

# Stop corridors: (polyline, n stops, AM-peak buses/hr, routes)
CORRIDORS = [
    ([(-6.2560, 53.3445), (-6.2400, 53.3430), (-6.2280, 53.3405), (-6.2200, 53.3390)], 9, 12, ["1", "47"]),
    ([(-6.2480, 53.3480), (-6.2290, 53.3475)], 6, 3, ["151"]),
    ([(-6.2500, 53.3520), (-6.2420, 53.3580), (-6.2330, 53.3620)], 8, 10, ["27", "53"]),
    ([(-6.2420, 53.3360), (-6.2300, 53.3310), (-6.2200, 53.3260)], 8, 8, ["4", "7", "39"]),
    ([(-6.2620, 53.3600), (-6.2560, 53.3520), (-6.2520, 53.3470)], 6, 15, ["16", "41"]),
    ([(-6.2240, 53.3330), (-6.2150, 53.3290)], 3, 2, ["18"]),
]

RAIL = [
    ("MOCK-DART", "dart", "MOCK DART (Connolly – Sydney Parade)", [
        ("Connolly", -6.2497, 53.3509), ("Tara Street", -6.2546, 53.3470), ("Pearse", -6.2484, 53.3433),
        ("Grand Canal Dock", -6.2377, 53.3397), ("Lansdowne Road", -6.2299, 53.3349),
        ("Sandymount", -6.2213, 53.3279), ("Sydney Parade", -6.2111, 53.3208)]),
    ("MOCK-LUAS-RED", "luas", "MOCK Luas Red Line (Docklands spur)", [
        ("The Point", -6.2290, 53.3485), ("Spencer Dock", -6.2370, 53.3488), ("Mayor Square", -6.2436, 53.3491),
        ("George's Dock", -6.2475, 53.3495), ("Busáras", -6.2520, 53.3500), ("Connolly Luas", -6.2500, 53.3510)]),
]

# (school_id, name, level, lng, lat, enrolment)
SCHOOLS = [
    ("MOCK-P01", "MOCK St Laurence O'Toole NS", "primary", -6.2410, 53.3510, 230),
    ("MOCK-P02", "MOCK Ringsend NS", "primary", -6.2250, 53.3410, 180),
    ("MOCK-P03", "MOCK Sandymount NS", "primary", -6.2190, 53.3310, 320),
    ("MOCK-P04", "MOCK East Wall NS", "primary", -6.2290, 53.3560, 150),
    ("MOCK-P05", "MOCK Gardiner Street NS", "primary", -6.2560, 53.3550, 260),
    ("MOCK-P06", "MOCK Ballsbridge NS", "primary", -6.2300, 53.3290, 210),
    ("MOCK-S01", "MOCK Larkin College", "post-primary", -6.2500, 53.3540, 520),
    ("MOCK-S02", "MOCK Marian College", "post-primary", -6.2290, 53.3330, 480),
    ("MOCK-S03", "MOCK Ringsend College", "post-primary", -6.2270, 53.3400, 300),
    ("MOCK-S04", "MOCK Belvedere College", "post-primary", -6.2610, 53.3555, 1000),
]

# Proposed routes: (route_id, path, serves)
ROUTES = [
    ("MOCK-R1", [(-6.2050, 53.3410), (-6.2190, 53.3395), (-6.2300, 53.3400), (-6.2377, 53.3397)],
     ["MOCK-06", "MOCK-12"]),
    ("MOCK-R2", [(-6.2265, 53.3570), (-6.2305, 53.3480), (-6.2400, 53.3500), (-6.2497, 53.3509)],
     ["MOCK-02", "MOCK-03"]),
    ("MOCK-R3", [(-6.2160, 53.3300), (-6.2230, 53.3345), (-6.2213, 53.3279)], ["MOCK-07", "MOCK-09"]),
]

NETWORK = [  # (edge_id, path, confidence, evidence)
    ("mock-e1", [(-6.2480, 53.3480), (-6.2370, 53.3478), (-6.2290, 53.3475)], "high", ["hydrant:mock/1"]),
    ("mock-e2", [(-6.2560, 53.3445), (-6.2400, 53.3430), (-6.2280, 53.3405)], "medium", ["dcc_gullies:Pearse Street"]),
    ("mock-e3", [(-6.2420, 53.3360), (-6.2300, 53.3310)], "low", ["osm_buildings_within_25m:12"]),
    ("mock-e4", [(-6.2500, 53.3520), (-6.2420, 53.3580)], "medium", ["manhole_sewer:mock/2"]),
    ("mock-e5", [(-6.2280, 53.3405), (-6.2230, 53.3398)], "low", ["osm_buildings_within_25m:4"]),
]
EXTENSIONS = [  # (edge_id, path, order, serves)
    ("mock-x1", [(-6.2230, 53.3398), (-6.2190, 53.3395)], 0, ["MOCK-06", "MOCK-12"]),
    ("mock-x2", [(-6.2190, 53.3395), (-6.2120, 53.3402)], 1, ["MOCK-12"]),
    ("mock-x3", [(-6.2120, 53.3402), (-6.2050, 53.3410)], 2, ["MOCK-12"]),
    ("mock-x4", [(-6.2420, 53.3580), (-6.2340, 53.3575), (-6.2265, 53.3570)], 0, ["MOCK-03"]),
]


def _line_len_m(path):
    return LineString([_m(*p) for p in path]).length


def _square(lng, lat, side_m):
    x, y = _m(lng, lat)
    h = side_m / 2
    ring = [_to_ll.transform(x + dx, y + dy) for dx, dy in ((-h, -h), (h, -h), (h, h), (-h, h), (-h, -h))]
    return {"type": "Polygon", "coordinates": [[list(p) for p in ring]]}


def run(bbox=None) -> None:
    MOCK.mkdir(parents=True, exist_ok=True)

    stops = []
    for ci, (path, n, bph, routes) in enumerate(CORRIDORS):
        line = LineString([_m(*p) for p in path])
        for i in range(n):
            x, y = line.interpolate(i / max(n - 1, 1), normalized=True).coords[0]
            lng, lat = _to_ll.transform(x, y)
            stops.append({"stop_id": f"MOCK-ST{ci + 1}{i + 1:02d}", "name": f"MOCK stop {ci + 1}.{i + 1}",
                          "peak_buses_per_hr": bph, "routes": routes, "_ll": (lng, lat)})
    stations = [(name, (lng, lat)) for _, _, _, sts in RAIL for name, lng, lat in sts]

    ext_len = {eid: _line_len_m(p) for eid, p, _, _ in EXTENSIONS}
    route_of = {a: rid for rid, _, serves in ROUTES for a in serves}

    devs = []
    for aid, desc, lng, lat, units, status, connected, _ in DEVS:
        near_stop = min(_dist((lng, lat), s["_ll"]) for s in stops)
        near_freq = min(_dist((lng, lat), s["_ll"]) for s in stops if s["peak_buses_per_hr"] >= C.FREQ_THRESHOLD)
        rname, rdist = min(((n, _dist((lng, lat), p)) for n, p in stations), key=lambda t: t[1])
        near_freq = min(near_freq, rdist)  # rail stations run ≥ FREQ_THRESHOLD in the peak
        cls = "served" if near_freq <= C.STOP_WALK_M else ("weak" if near_stop <= C.STOP_WALK_M else "unserved")
        residents = units * C.HOUSEHOLD_SIZE
        route_len = sum(ext_len[e] for e, _, _, serves in EXTENSIONS if aid in serves)
        devs.append(dict(aid=aid, desc=desc, lng=lng, lat=lat, units=units, status=status,
                         connected=connected, route_len=route_len, cls=cls, near_stop=near_stop,
                         near_freq=near_freq, rname=rname, rdist=rdist, residents=residents,
                         trips=residents * C.PEAK_TRIP_RATE * C.PT_MODE_SHARE,
                         prim=units * C.CHILDREN_PER_HOUSEHOLD_PRIMARY,
                         sec=units * C.CHILDREN_PER_HOUSEHOLD_SECONDARY))

    # schools: split each development's pupils evenly across its nearby schools
    projected = {s[0]: 0.0 for s in SCHOOLS}
    for d in devs:
        for level, key, radius in (("primary", "prim", C.PRIMARY_RADIUS_M), ("post-primary", "sec", C.POSTPRIMARY_RADIUS_M)):
            near = [s[0] for s in SCHOOLS if s[2] == level and _dist((d["lng"], d["lat"]), (s[3], s[4])) <= radius]
            d["near_" + key] = near
            for sid in near:
                projected[sid] += d[key] / len(near)
    ratio = {s[0]: projected[s[0]] / s[5] for s in SCHOOLS}

    dev_feats = []
    for d in devs:
        enrol = {s[0]: s[5] for s in SCHOOLS}
        max_ratio = dev_pressure_ratio(d["prim"], [enrol[s] for s in d["near_prim"]],
                                       d["sec"], [enrol[s] for s in d["near_sec"]])
        flag = max_ratio is None or max_ratio >= C.SCHOOL_PRESSURE_THRESHOLD
        dist_note = 0.0 if d["connected"] else 60.0 + d["route_len"] / 10
        scores = {"water": water_score(d["connected"], d["route_len"]),
                  "transport": transport_score(d["near_freq"]),
                  "schools": schools_score(max_ratio)}
        scores["overall"] = overall_score(scores)
        dev_feats.append(feature(_square(d["lng"], d["lat"], C.FOOTPRINT_SQUARE_M), {
            "app_id": d["aid"], "num_units": d["units"], "dev_type": "residential", "status": d["status"],
            "received_date": "2025-03-01", "description": d["desc"], "footprint_source": "buffered_point",
            "centroid": [d["lng"], d["lat"]],
            "water": {"connected": d["connected"], "distance_to_network_m": round(dist_note, 1),
                      "route_length_m": round(d["route_len"], 1),
                      "est_cost_eur": round(d["route_len"] * C.WATER_MAIN_COST_EUR_PER_M)},
            "transport": {"class": d["cls"], "nearest_stop_m": round(d["near_stop"]),
                          "nearest_frequent_stop_m": round(d["near_freq"]),
                          "nearest_rail_name": d["rname"], "nearest_rail_m": round(d["rdist"]),
                          "residents": round(d["residents"]), "peak_pt_trips": round(d["trips"], 1),
                          "proposed_route_id": route_of.get(d["aid"])},
            "schools": {"primary_pupils": round(d["prim"], 1), "secondary_pupils": round(d["sec"], 1),
                        "nearby_primary": d["near_prim"], "nearby_secondary": d["near_sec"],
                        "pressure_flag": bool(flag)},
            "scores": scores,
        }))

    route_feats = []
    for rid, path, serves in ROUTES:
        km = _line_len_m(path) / 1000
        rt = 2 * km / C.BUS_SPEED_KMH * 60 + C.LAYOVER_MIN
        demand = sum(d["trips"] for d in devs if d["aid"] in serves)
        per_hr = demand / C.AM_PEAK_HOURS
        need = max(1, math.ceil(per_hr / (C.BUS_CAPACITY * C.BUS_LOAD_FACTOR)))
        headway = min(max(60 / need, C.MIN_HEADWAY), C.MAX_HEADWAY)
        route_feats.append(feature(round_geojson({"type": "LineString", "coordinates": path}), {
            "route_id": rid, "length_km": round(km, 2), "round_trip_min": round(rt, 1),
            "headway_min": round(headway, 1), "buses_required": math.ceil(rt / headway),
            "serves_app_ids": serves, "peak_demand": round(demand, 1)}))

    write_json(MOCK / "developments.geojson", fc(dev_feats))
    write_json(MOCK / "stops.geojson", fc([feature(round_geojson({"type": "Point", "coordinates": list(s["_ll"])}),
                                                   {k: v for k, v in s.items() if k != "_ll"}) for s in stops]))
    write_json(MOCK / "rail_lines.geojson", fc([feature(round_geojson({"type": "LineString", "coordinates": [[x, y] for _, x, y in sts]}),
                                                        {"id": rid, "mode": mode, "name": name}) for rid, mode, name, sts in RAIL]))
    write_json(MOCK / "transport_routes.geojson", fc(route_feats))
    write_json(MOCK / "schools.geojson", fc([feature(round_geojson({"type": "Point", "coordinates": [lng, lat]}), {
        "school_id": sid, "name": name, "level": level, "enrolment": enr,
        "projected_new_pupils": round(projected[sid], 1), "pressure_ratio": round(ratio[sid], 3)})
        for sid, name, level, lng, lat, enr in SCHOOLS]))
    write_json(MOCK / "network.geojson", fc([feature(round_geojson({"type": "LineString", "coordinates": p}), {
        "edge_id": e, "confidence": c, "evidence": ev, "length_m": round(_line_len_m(p), 1)}) for e, p, c, ev in NETWORK]))
    write_json(MOCK / "extensions.geojson", fc([feature(round_geojson({"type": "LineString", "coordinates": p}), {
        "edge_id": e, "order": o, "length_m": round(ext_len[e], 1), "serves_app_ids": s}) for e, p, o, s in EXTENSIONS]))
    write_json(MOCK / "assumptions.json", C.assumptions_json())
    ext_km = sum(ext_len.values()) / 1000
    by = {s: {"developments": sum(d["status"] == s for d in devs), "units": sum(d["units"] for d in devs if d["status"] == s)}
          for s in ("granted", "pending")}
    write_json(MOCK / "summary.json", {
        "planned_units": sum(d["units"] for d in devs),
        "residents": round(sum(d["residents"] for d in devs)),
        "new_peak_pt_trips": round(sum(d["trips"] for d in devs), 1),
        "proposed_routes": len(route_feats),
        "buses_required": sum(f["properties"]["buses_required"] for f in route_feats),
        "new_primary_pupils": round(sum(d["prim"] for d in devs), 1),
        "new_secondary_pupils": round(sum(d["sec"] for d in devs), 1),
        "water_extension_km": round(ext_km, 3),
        "water_cost_eur": round(ext_km * 1000 * C.WATER_MAIN_COST_EUR_PER_M),
        "by_status": by,
        "sources": {"mock": {"status": "ok", "records": len(devs)}},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    print(f"  mock: {len(dev_feats)} developments, {len(stops)} stops, {len(RAIL)} rail lines, "
          f"{len(route_feats)} routes, {len(SCHOOLS)} schools -> {MOCK}")
