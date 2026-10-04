"""Check a directory of impact outputs against the data contract.

    python -m impact.validate data/impact/mock
    python -m impact.validate data/impact
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

NUM = (int, float)
OPT_NUM = (int, float, type(None))
OPT_STR = (str, type(None))


def _req(errs, where, obj, spec):
    for key, types in spec.items():
        if key not in obj:
            errs.append(f"{where}: missing {key}")
        elif not isinstance(obj[key], types) or isinstance(obj[key], bool) and bool not in (types if isinstance(types, tuple) else (types,)):
            errs.append(f"{where}: {key}={obj[key]!r} has type {type(obj[key]).__name__}")
        elif isinstance(obj[key], float) and not math.isfinite(obj[key]):
            errs.append(f"{where}: {key} is not finite")


def _coords_ok(geom) -> bool:
    def walk(c):
        if isinstance(c[0], (int, float)):
            return -11 < c[0] < -5 and 51 < c[1] < 56  # Ireland, EPSG:4326 lon/lat
        return all(walk(x) for x in c)
    return walk(geom["coordinates"])


def check(d: Path) -> list[str]:
    errs: list[str] = []
    load = lambda n: json.loads((d / n).read_text())  # noqa: E731

    def features(name, geom_types, spec):
        try:
            fc = load(name)
        except FileNotFoundError:
            errs.append(f"{name}: missing")
            return []
        for i, f in enumerate(fc["features"]):
            if f["geometry"]["type"] not in geom_types:
                errs.append(f"{name}[{i}]: geometry {f['geometry']['type']} not in {geom_types}")
            elif not _coords_ok(f["geometry"]):
                errs.append(f"{name}[{i}]: coordinates are not EPSG:4326 lon/lat in Ireland")
            _req(errs, f"{name}[{i}]", f["properties"], spec)
        return fc["features"]

    devs = features("developments.geojson", {"Polygon"}, {
        "app_id": str, "num_units": int, "dev_type": str, "status": str, "received_date": OPT_STR,
        "description": OPT_STR, "footprint_source": str, "centroid": list, "water": dict,
        "transport": dict, "schools": dict, "scores": dict})
    for i, f in enumerate(devs):
        p, w = f["properties"], f"developments.geojson[{i}]"
        if p.get("status") not in ("granted", "pending"):
            errs.append(f"{w}: status {p.get('status')!r}")
        if p.get("footprint_source") not in ("polygon", "buffered_point"):
            errs.append(f"{w}: footprint_source {p.get('footprint_source')!r}")
        if isinstance(p.get("water"), dict):
            _req(errs, w + ".water", p["water"], {"connected": (bool, type(None)), "distance_to_network_m": OPT_NUM,
                                                 "route_length_m": OPT_NUM, "est_cost_eur": OPT_NUM})
        if isinstance(p.get("transport"), dict):
            t = p["transport"]
            _req(errs, w + ".transport", t, {"class": str, "nearest_stop_m": OPT_NUM, "nearest_frequent_stop_m": OPT_NUM,
                                             "nearest_rail_name": OPT_STR, "nearest_rail_m": OPT_NUM, "residents": NUM,
                                             "peak_pt_trips": NUM, "proposed_route_id": OPT_STR})
            if t.get("class") not in ("served", "weak", "unserved"):
                errs.append(f"{w}.transport.class {t.get('class')!r}")
        if isinstance(p.get("schools"), dict):
            _req(errs, w + ".schools", p["schools"], {"primary_pupils": NUM, "secondary_pupils": NUM,
                                                      "nearby_primary": list, "nearby_secondary": list,
                                                      "pressure_flag": bool})
        if isinstance(p.get("scores"), dict):
            _req(errs, w + ".scores", p["scores"], {k: NUM for k in ("water", "transport", "schools", "overall")})
            if any(not 0 <= p["scores"].get(k, 0) <= 100 for k in ("water", "transport", "schools", "overall")):
                errs.append(f"{w}.scores out of 0–100: {p['scores']}")

    features("stops.geojson", {"Point"}, {"stop_id": str, "name": str, "peak_buses_per_hr": NUM, "routes": list})
    rails = features("rail_lines.geojson", {"LineString", "MultiLineString"}, {"id": str, "mode": str, "name": str})
    for i, f in enumerate(rails):
        if f["properties"].get("mode") not in ("dart", "luas", "rail"):
            errs.append(f"rail_lines.geojson[{i}]: mode {f['properties'].get('mode')!r}")
    routes = features("transport_routes.geojson", {"LineString"}, {
        "route_id": str, "length_km": NUM, "round_trip_min": NUM, "headway_min": NUM, "buses_required": int,
        "serves_app_ids": list, "peak_demand": NUM})
    schools = features("schools.geojson", {"Point"}, {"school_id": str, "name": str, "level": str,
                                                      "enrolment": (int, type(None)), "projected_new_pupils": NUM,
                                                      "pressure_ratio": OPT_NUM})
    for i, f in enumerate(schools):
        if f["properties"].get("level") not in ("primary", "post-primary"):
            errs.append(f"schools.geojson[{i}]: level {f['properties'].get('level')!r}")
    features("network.geojson", {"LineString", "MultiLineString"}, {"edge_id": str, "confidence": str,
                                                                    "evidence": list, "length_m": NUM})
    exts = features("extensions.geojson", {"LineString", "MultiLineString"}, {"edge_id": str, "order": int,
                                                                              "length_m": NUM, "serves_app_ids": list})

    # cross-references
    app_ids = {f["properties"]["app_id"] for f in devs}
    route_ids = {f["properties"]["route_id"] for f in routes}
    school_ids = {f["properties"]["school_id"] for f in schools}
    for f in routes + exts:
        missing = set(f["properties"].get("serves_app_ids", [])) - app_ids
        if missing:
            errs.append(f"serves_app_ids references unknown developments: {sorted(missing)[:3]}")
    for f in devs:
        t, s = f["properties"].get("transport") or {}, f["properties"].get("schools") or {}
        if t.get("proposed_route_id") and t["proposed_route_id"] not in route_ids:
            errs.append(f"{f['properties']['app_id']}: proposed_route_id {t['proposed_route_id']} not in transport_routes")
        nb = [x for k in ("nearby_primary", "nearby_secondary") if isinstance(s.get(k), list) for x in s[k]]
        if set(nb) - school_ids:
            errs.append(f"{f['properties']['app_id']}: nearby school not in schools.geojson")

    try:
        a = load("assumptions.json")
        for i, x in enumerate(a):
            _req(errs, f"assumptions.json[{i}]", x, {"key": str, "label": str, "unit": str, "source_note": str})
            if "value" not in x:
                errs.append(f"assumptions.json[{i}]: missing value")
    except FileNotFoundError:
        errs.append("assumptions.json: missing")
    try:
        s = load("summary.json")
        _req(errs, "summary.json", s, {k: NUM for k in ("planned_units", "residents", "new_peak_pt_trips",
                                                        "proposed_routes", "buses_required", "new_primary_pupils",
                                                        "new_secondary_pupils", "water_extension_km", "water_cost_eur")}
             | {"by_status": dict, "sources": dict, "generated_at": str})
        for k, v in s.get("sources", {}).items():
            if v.get("status") not in ("ok", "partial", "failed") or not isinstance(v.get("records"), int):
                errs.append(f"summary.sources.{k}: {v}")
        if set(s.get("by_status", {})) != {"granted", "pending"}:
            errs.append(f"summary.by_status keys {sorted(s.get('by_status', {}))}")
    except FileNotFoundError:
        errs.append("summary.json: missing")
    return errs


if __name__ == "__main__":
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "data/impact")
    errs = check(d)
    print(f"{d}: {'OK' if not errs else f'{len(errs)} problem(s)'}")
    for e in errs[:40]:
        print("  -", e)
    sys.exit(1 if errs else 0)
