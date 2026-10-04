"""Impact stage 4: scores, final developments.geojson, network/extension copies,
summary.json and assumptions.json — then a size check against the ~15 MB budget."""
from __future__ import annotations

from datetime import datetime, timezone

import geopandas as gpd
from shapely import wkt
from shapely.geometry import Point, shape

from pipeline import sizing
from pipeline.common import log

from . import config as C
from .common import (DEVS, METRIC_CRS, OUT, OUTPUT_FILES, SOURCES, WATER_DIR, WGS84, fc, feature,
                     geom_out, read_json, write_json)
from .scoring import overall_score, schools_score, transport_score, water_score

SIZE_BUDGET_MB = 15


def _evidence_short(ev: str) -> str:
    """Compact evidence codes for the size budget; every item is kept, only its wording shrinks.
    'hydrant:osm:node/1 (fire_hydrant:type=…)' -> 'hydrant:osm:node/1'
    'osm_buildings_within_25m:23'              -> 'osm_buildings:23'
    'dcc_gullies:Custom House Quay (92 …)'     -> 'dcc_gullies'"""
    ev = ev.split(" (", 1)[0]
    if ev.startswith("osm_buildings_within_"):
        return "osm_buildings:" + ev.rsplit(":", 1)[1]
    if ev.startswith("dcc_gullies:"):
        return "dcc_gullies"
    return ev


def copy_network(devs: list[dict]) -> int:
    net = read_json(WATER_DIR / "network.geojson") or {"features": []}
    if not net["features"]:
        write_json(OUT / "network.geojson", fc([]))
        return 0
    gdf = gpd.GeoDataFrame.from_features(net["features"], crs=WGS84).to_crs(METRIC_CRS)
    if devs:
        pts = gpd.GeoSeries([Point(*d["centroid"]) for d in devs], crs=WGS84).to_crs(METRIC_CRS)
        zone = pts.buffer(C.NETWORK_CONTEXT_M).union_all()
        before = len(gdf)
        gdf = gdf[gdf.intersects(zone)]
        log(f"  network: kept {len(gdf)}/{before} edges within {C.NETWORK_CONTEXT_M} m of a development")
    feats = [feature(geom_out(r.geometry), {
        "edge_id": r.edge_id, "confidence": r.confidence,
        "evidence": [_evidence_short(e) for e in r.evidence], "length_m": round(r.length_m)})
        for r in gdf.itertuples()]
    write_json(OUT / "network.geojson", fc(feats))
    return len(feats)


def copy_extensions(app_ids: set[str]) -> list[dict]:
    ext = read_json(WATER_DIR / "extensions.geojson") or {"features": []}
    kept = []
    for f in ext["features"]:
        serves = [a for a in f["properties"]["serves_app_ids"] if a in app_ids]
        if serves:  # an edge that only served refused/withdrawn applications is dropped
            kept.append((f, serves))
    levels = sorted({f["properties"]["order"] for f, _ in kept})
    remap = {o: i for i, o in enumerate(levels)}
    feats = []
    for f, serves in kept:
        g = gpd.GeoSeries([shape(f["geometry"])], crs=WGS84).to_crs(METRIC_CRS).iloc[0]
        p = f["properties"]
        feats.append(feature(geom_out(g), {"edge_id": p["edge_id"], "order": remap[p["order"]],
                                           "length_m": p["length_m"], "serves_app_ids": serves}))
    write_json(OUT / "extensions.geojson", fc(feats))
    return feats


def run(bbox) -> None:
    devs = read_json(DEVS, [])
    dev_feats = []
    for d in devs:
        w, t, s = dict(d.get("water") or {}), d.get("transport") or {}, d.get("schools") or {}
        if w:  # Uisce Éireann Code-of-Practice demand and main size (pipeline/sizing.py)
            w["avg_daily_demand_m3"] = round(sizing.avg_daily_demand_m3(d["num_units"]), 1)
            w["required_main_mm"] = sizing.size_main(d["num_units"])["nominal_bore_mm"]
        scores = {"water": water_score(bool(w.get("connected")), w.get("route_length_m")),
                  "transport": transport_score(t.get("nearest_frequent_stop_m")),
                  "schools": schools_score(d.get("_school_ratio")) if s else 100.0}
        scores["overall"] = overall_score(scores)
        poly = wkt.loads(d["footprint_wkt_2157"])
        parts = list(getattr(poly, "geoms", [poly]))
        poly = max(parts, key=lambda g: g.area)  # contract is Polygon: keep a multi-part site's largest part
        dev_feats.append(feature(geom_out(poly), {
            "app_id": d["app_id"], "num_units": d["num_units"], "dev_type": d["dev_type"],
            "status": d["status"], "received_date": d.get("received_date"),
            "description": d.get("description"), "footprint_source": d["footprint_source"],
            "centroid": d["centroid"], "water": w or None, "transport": t or None, "schools": s or None,
            "scores": scores, "footprint_parts": len(parts), "address": d.get("address"),
            "authority": d.get("authority"), "planning_url": d.get("planning_url")}))
    dev_feats.sort(key=lambda f: -f["properties"]["scores"]["overall"])
    write_json(OUT / "developments.geojson", fc(dev_feats))

    n_net = copy_network(devs)
    exts = copy_extensions({d["app_id"] for d in devs})
    routes = (read_json(OUT / "transport_routes.geojson") or {"features": []})["features"]
    write_json(OUT / "assumptions.json", C.assumptions_json())

    P = [f["properties"] for f in dev_feats]
    ext_km = sum(f["properties"]["length_m"] for f in exts) / 1000
    # Routed extension network costed once (a shared trunk is not paid for per development),
    # plus the direct laterals of unconnected developments that no extension edge serves.
    on_ext = {a for f in exts for a in f["properties"]["serves_app_ids"]}
    laterals_m = sum((p["water"] or {}).get("route_length_m") or 0 for p in P
                     if p["water"] and p["water"].get("connected") is False and p["app_id"] not in on_ext)
    water_cost = round((ext_km * 1000 + laterals_m) * C.WATER_MAIN_COST_EUR_PER_M)
    by = {st: {"developments": sum(p["status"] == st for p in P),
               "units": sum(p["num_units"] for p in P if p["status"] == st)} for st in ("granted", "pending")}
    summary = {
        "planned_units": sum(p["num_units"] for p in P),
        "residents": sum((p["transport"] or {}).get("residents") or 0 for p in P)
                     or round(sum(p["num_units"] for p in P) * C.HOUSEHOLD_SIZE),
        "new_peak_pt_trips": round(sum((p["transport"] or {}).get("peak_pt_trips") or 0 for p in P), 1),
        "proposed_routes": len(routes),
        "buses_required": sum(r["properties"]["buses_required"] for r in routes),
        "new_primary_pupils": round(sum((p["schools"] or {}).get("primary_pupils") or 0 for p in P), 1),
        "new_secondary_pupils": round(sum((p["schools"] or {}).get("secondary_pupils") or 0 for p in P), 1),
        "water_extension_km": round(ext_km, 3),
        "water_cost_eur": water_cost,
        "by_status": by,
        "sources": {k: {"status": v["status"], "records": v["records"]} for k, v in (read_json(SOURCES, {}) or {}).items()},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json(OUT / "summary.json", summary)
    log(f"  developments.geojson: {len(dev_feats)}; network.geojson: {n_net}; extensions.geojson: {len(exts)}")
    log(f"  summary: {summary['planned_units']} units, {summary['proposed_routes']} routes / "
        f"{summary['buses_required']} buses, {summary['water_extension_km']} km new main, "
        f"€{summary['water_cost_eur']:,}")
    sizes = {n: (OUT / n).stat().st_size / 1e6 for n in OUTPUT_FILES if (OUT / n).exists()}
    total = sum(sizes.values())
    log("  sizes: " + ", ".join(f"{n} {mb:.2f} MB" for n, mb in sorted(sizes.items(), key=lambda kv: -kv[1])))
    log(f"  total {total:.1f} MB (budget {SIZE_BUDGET_MB} MB){'  — OVER BUDGET' if total > SIZE_BUDGET_MB else ''}")
