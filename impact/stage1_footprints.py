"""Impact stage 1: development footprints + water results.

Inputs (from the shared water pipeline):
  interim/planning.json    every planning application in the bbox (pipeline stage 1)
  interim/extracted.json   LLM-kept residential/mixed new builds (pipeline stage 2)
  developments/extensions.geojson   connection + Steiner extension routing (pipeline stage 4)
Adds: site polygons from NPAD FeatureServer/1, joined by application ID.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import geopandas as gpd
import requests
from shapely.geometry import Point, box, shape
from shapely.ops import unary_union

from pipeline.common import log
from pipeline.stage1_planning import URL as POINTS_URL
from pipeline.stage1_planning import app_id

from . import config as C
from .common import DEVS, METRIC_CRS, PIPE_INTERIM, WATER_DIR, WGS84, read_json, source, write_json

POLYGONS_URL = POINTS_URL.replace("/FeatureServer/0/query", "/FeatureServer/1/query")


def fetch_polygons(bbox, years: int = 3) -> dict[str, object]:
    """app_id -> unioned site polygon (EPSG:2157 shapely geometry)."""
    since = (datetime.now(timezone.utc) - timedelta(days=365 * years)).strftime("%Y-%m-%d 00:00:00")
    w, s, e, n = bbox
    params = {"where": f"ReceivedDate >= TIMESTAMP '{since}'", "geometry": f"{w},{s},{e},{n}",
              "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
              "outFields": "PlanningAuthority,ApplicationNumber", "returnGeometry": "true",
              "outSR": 2157, "orderByFields": "OBJECTID", "resultRecordCount": 1000, "f": "geojson"}
    parts: dict[str, list] = {}
    offset = 0
    while True:
        r = requests.get(POLYGONS_URL, params={**params, "resultOffset": offset}, timeout=120)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        feats = body.get("features", [])
        for f in feats:
            if not f.get("geometry"):
                continue
            p = f["properties"]
            g = shape(f["geometry"])
            if not g.is_valid:
                g = g.buffer(0)
            parts.setdefault(app_id(p.get("PlanningAuthority"), p.get("ApplicationNumber")), []).append(g)
        if not feats or not (body.get("exceededTransferLimit")
                             or body.get("properties", {}).get("exceededTransferLimit")):
            break
        offset += len(feats)
    return {k: unary_union(v) for k, v in parts.items()}


def water_by_app() -> dict[str, dict]:
    devs = read_json(WATER_DIR / "developments.geojson") or {"features": []}
    exts = read_json(WATER_DIR / "extensions.geojson") or {"features": []}
    route: dict[str, float] = {}
    for f in exts["features"]:
        for a in f["properties"]["serves_app_ids"]:
            route[a] = route.get(a, 0.0) + f["properties"]["length_m"]
    out = {}
    for f in devs["features"]:
        p = f["properties"]
        # New pipe = the routed extension edges serving it; where routing added none (its
        # nearest street node is already served) it is the direct lateral to the network.
        dist = p.get("distance_to_network_m")
        rl = 0.0 if p["connected"] else (route.get(p["app_id"]) or dist or 0.0)
        out[p["app_id"]] = {"connected": bool(p["connected"]),
                            "distance_to_network_m": p.get("distance_to_network_m"),
                            "route_length_m": round(rl, 1),
                            "est_cost_eur": round(rl * C.WATER_MAIN_COST_EUR_PER_M)}
    return out


def run(bbox) -> None:
    planning = read_json(PIPE_INTERIM / "planning.json", [])
    extracted = read_json(PIPE_INTERIM / "extracted.json", [])
    log(f"  {len(planning)} planning applications, {len(extracted)} LLM-kept new builds")

    devs = []
    excluded = 0
    for d in extracted:
        status = C.STATUS_MAP.get(d.get("status"))
        if not status:
            excluded += 1
            continue
        devs.append({"app_id": d["app_id"], "num_units": int(d["num_units"]), "dev_type": d["dev_type"],
                     "status": status, "received_date": d.get("received_date"),
                     "description": d.get("description"), "lng": d["lon"], "lat": d["lat"]})
    log(f"  {len(devs)} granted/pending developments ({excluded} excluded by STATUS_MAP)")

    try:
        polys = fetch_polygons(bbox)
        log(f"  NPAD site polygons: {len(polys)} applications")
        source("planning_polygons", "ok", len(polys))
    except Exception as exc:
        log(f"  NPAD site polygons FAILED: {exc}")
        source("planning_polygons", "failed", 0, str(exc))
        polys = {}

    water = water_by_app()
    pts = gpd.GeoSeries([Point(d["lng"], d["lat"]) for d in devs], crs=WGS84).to_crs(METRIC_CRS)
    h = C.FOOTPRINT_SQUARE_M / 2
    n_poly = 0
    for d, pt in zip(devs, pts):
        poly = polys.get(d["app_id"])
        if poly is not None and not poly.is_empty:
            d["footprint_source"] = "polygon"
            n_poly += 1
        else:
            poly = box(pt.x - h, pt.y - h, pt.x + h, pt.y + h)
            d["footprint_source"] = "buffered_point"
        d["footprint_wkt_2157"] = poly.wkt
        c = gpd.GeoSeries([poly.representative_point() if d["footprint_source"] == "polygon" else pt],
                          crs=METRIC_CRS).to_crs(WGS84).iloc[0]
        d["centroid"] = [round(c.x, 6), round(c.y, 6)]
        d["water"] = water.get(d["app_id"]) or {"connected": None, "distance_to_network_m": None,
                                               "route_length_m": None, "est_cost_eur": None}
    write_json(DEVS, devs)

    p_src = (read_json(PIPE_INTERIM / "sources.json", {}) or {}).get("planning") or {}
    source("planning_points", "failed" if p_src.get("error") else "ok", len(planning), p_src.get("error"))
    llm_cache = read_json(PIPE_INTERIM.parent / "cache" / "llm.json", {}) or {}
    have = sum(1 for a in planning if a["app_id"] in llm_cache)
    status = "ok" if have == len(planning) else ("failed" if have == 0 and planning else "partial")
    source("llm_extraction", status, have, None if status == "ok" else f"{len(planning) - have} applications not extracted")
    unmatched = sum(1 for d in devs if d["water"]["connected"] is None)
    source("water_network", "partial" if unmatched else "ok", len(devs) - unmatched,
           f"{unmatched} developments missing from the water routing outputs" if unmatched else None)
    log(f"  footprints: {n_poly} polygon, {len(devs) - n_poly} buffered_point; "
        f"water joined for {len(devs) - unmatched}/{len(devs)}")
