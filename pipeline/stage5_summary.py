"""Stage 5: summary.json (+ hand-made mock files for the frontend on first run)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import geopandas as gpd
import osmnx as ox

from collections import Counter

from shapely.geometry import box

from . import osm_source, sizing
from .common import (round_coords, DATA, MOCK, METRIC_CRS, SOURCES, WGS84, feature_collection, log, read_json,
                     write_json)
from .stage3_network import CENSUS, pad_bbox
from .stage_context import CONTEXT

OUT = DATA / "summary.json"
AREAS = DATA / "areas.geojson"
PLACE_PAD_M = 2000  # suburb/neighbourhood nodes can sit well outside a small bbox
TOP_N = 5


def place_names(bbox) -> gpd.GeoDataFrame | None:
    try:
        tags = {"place": ["suburb", "neighbourhood", "quarter"]}
        pbbox = pad_bbox(bbox, PLACE_PAD_M)
        gdf = (osm_source.features(pbbox, tags) if osm_source.use_local()
               else ox.features_from_bbox(pbbox, tags))
    except Exception as exc:
        log(f"  OSM place names FAILED: {exc}")
        return None
    gdf = gdf[gdf["name"].notna()].copy()
    gdf["geometry"] = gdf.geometry.representative_point()
    log(f"  OSM places: {len(gdf)}")
    return gdf[["name", "geometry"]].reset_index(drop=True)


def write_areas(bbox, devs) -> dict:
    """Census small areas touching the bbox, with the new homes planned inside each."""
    census = read_json(CENSUS)
    if not census or not census.get("features"):
        write_json(AREAS, feature_collection([]))
        return {}
    sa = gpd.GeoDataFrame.from_features(census["features"], crs=WGS84)
    sa = sa[sa.intersects(box(*bbox))].copy()
    units = Counter()
    if devs:
        pts = gpd.GeoDataFrame(
            [{"units": f["properties"]["num_units"]} for f in devs],
            geometry=gpd.points_from_xy([f["geometry"]["coordinates"][0] for f in devs],
                                        [f["geometry"]["coordinates"][1] for f in devs]), crs=WGS84)
        j = gpd.sjoin(pts, sa[["small_area_id", "geometry"]], predicate="within")
        units.update(j.groupby("small_area_id")["units"].sum().to_dict())
    feats = []
    for r in sa.itertuples():
        p = {k: getattr(r, k) for k in ("small_area_id", "electoral_division", "lea", "local_authority",
                                        "population", "households", "dwellings", "vacant_dwellings",
                                        "household_size")}
        p = {k: (None if v != v else v) for k, v in p.items()}  # NaN -> None
        nu = int(units.get(r.small_area_id, 0))
        p["new_units"] = nu
        p["new_residents_est"] = round(nu * (p["household_size"] or sizing.OCCUPANCY))
        p["household_growth_pct"] = round(100 * nu / p["households"], 1) if p["households"] else None
        feats.append({"type": "Feature", "geometry": round_coords(r.geometry.__geo_interface__), "properties": p})
    write_json(AREAS, feature_collection(feats))
    pop = sum(f["properties"]["population"] or 0 for f in feats)
    hh = sum(f["properties"]["households"] or 0 for f in feats)
    log(f"  areas.geojson: {len(feats)} census small areas ({pop} people, {hh} households)")
    return {"small_areas": len(feats), "population_2022": pop, "households_2022": hh}


def run(bbox) -> None:
    write_mocks()
    devs = (read_json(DATA / "developments.geojson") or {"features": []})["features"]
    exts = (read_json(DATA / "extensions.geojson") or {"features": []})["features"]
    assets = (read_json(DATA / "assets.geojson") or {"features": []})["features"]

    area_of: dict[str, str] = {}
    if devs:
        places = place_names(bbox)
        if places is not None and len(places):
            pts = gpd.GeoDataFrame(
                [{"app_id": f["properties"]["app_id"]} for f in devs],
                geometry=gpd.points_from_xy([f["geometry"]["coordinates"][0] for f in devs],
                                            [f["geometry"]["coordinates"][1] for f in devs]),
                crs=WGS84).to_crs(METRIC_CRS)
            j = gpd.sjoin_nearest(pts, places.to_crs(METRIC_CRS), how="left")
            area_of = j.drop_duplicates("app_id").set_index("app_id")["name"].to_dict()

    units: dict[str, int] = defaultdict(int)
    residents: dict[str, int] = defaultdict(int)
    km: dict[str, float] = defaultdict(float)
    for f in devs:
        p = f["properties"]
        a = area_of.get(p["app_id"], "Unknown area")
        units[a] += p["num_units"]
        residents[a] += p.get("est_residents") or 0
    for f in exts:  # split each new edge evenly across the developments it serves
        p = f["properties"]
        share = p["length_m"] / 1000 / max(len(p["serves_app_ids"]), 1)
        for aid in p["serves_app_ids"]:
            km[area_of.get(aid, "Unknown area")] += share

    census = write_areas(bbox, devs)
    total_units = sum(f["properties"]["num_units"] for f in devs)
    src = read_json(SOURCES, {})
    count = lambda k: int((src.get(k) or {}).get("count") or 0)  # noqa: E731
    by_bore = Counter()
    for f in exts:
        by_bore[f["properties"]["nominal_bore_mm"]] += f["properties"]["length_m"]
    summary = {
        "total_units": total_units,
        "unconnected_units": sum(f["properties"]["num_units"] for f in devs
                                 if not f["properties"]["connected"]),
        "new_pipe_km": round(sum(f["properties"]["length_m"] for f in exts) / 1000, 3),
        "top_areas": [
            {"name": name, "units": u, "new_pipe_km": round(km.get(name, 0.0), 3),
             "est_residents": residents[name]}
            for name, u in sorted(units.items(), key=lambda kv: -kv[1])[:TOP_N]
        ],
        "sources": {"hydrants_osm": count("hydrants_osm"), "mapillary": count("mapillary"),
                    "planning": count("planning"),
                    "osm_water_assets": count("osm_water_assets"),
                    "dcc_gully_streets": count("dcc_gullies"),
                    "census_small_areas": count("census_small_areas")},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # --- additive detail beyond the original contract ---
        "est_new_residents": sum(f["properties"].get("est_residents") or 0 for f in devs),
        "new_avg_daily_demand_m3": round(sizing.avg_daily_demand_m3(total_units), 1),
        "new_peak_flow_lps": round(sizing.peak_flow_lps(total_units), 1),
        "new_pipe_km_by_bore_mm": {str(b): round(m / 1000, 3) for b, m in sorted(by_bore.items())},
        "largest_new_main_mm": max(by_bore) if by_bore else None,
        "existing": {**census,
                     "household_growth_pct": (round(100 * total_units / census["households_2022"], 1)
                                              if census.get("households_2022") else None)},
        "assets_by_kind": dict(Counter(f["properties"]["kind"] for f in assets).most_common()),
        "context": {k: v for k, v in (read_json(CONTEXT) or {}).items() if k != "errors"},
        "assumptions": sizing.assumptions(),
    }
    write_json(OUT, summary)
    log(f"  summary: {summary['total_units']} units, {summary['unconnected_units']} unconnected, "
        f"{summary['new_pipe_km']} km new pipe, {len(summary['top_areas'])} areas")
    failed = {k: v["error"] for k, v in src.items() if v.get("error")}
    if failed:
        log(f"  sources with errors/skips: {failed}")


# --- hand-made mock files around Custom House Quay -------------------------------

def write_mocks() -> None:
    if MOCK.exists():
        return
    MOCK.mkdir(parents=True)
    line = lambda *c: {"type": "LineString", "coordinates": [list(x) for x in c]}  # noqa: E731
    fc = lambda fs: {"type": "FeatureCollection", "features": fs}  # noqa: E731
    feat = lambda g, p: {"type": "Feature", "geometry": g, "properties": p}  # noqa: E731
    write_json(MOCK / "network.geojson", fc([
        feat(line((-6.2555, 53.3478), (-6.2525, 53.3480)),
             {"edge_id": "mock-1", "confidence": "high", "evidence": ["osm_hydrant:node/1"], "length_m": 200.0}),
        feat(line((-6.2525, 53.3480), (-6.2495, 53.3481)),
             {"edge_id": "mock-2", "confidence": "medium", "evidence": ["mapillary_manhole:1"], "length_m": 200.0}),
        feat(line((-6.2525, 53.3480), (-6.2524, 53.3492)),
             {"edge_id": "mock-3", "confidence": "low", "evidence": ["osm_buildings_within_25m:4"], "length_m": 133.0}),
        feat(line((-6.2495, 53.3481), (-6.2465, 53.3482)),
             {"edge_id": "mock-4", "confidence": "none", "evidence": [], "length_m": 200.0}),
        feat(line((-6.2465, 53.3482), (-6.2450, 53.3483)),
             {"edge_id": "mock-5", "confidence": "none", "evidence": [], "length_m": 100.0}),
    ]))
    write_json(MOCK / "developments.geojson", fc([
        feat({"type": "Point", "coordinates": [-6.2520, 53.3486]},
             {"app_id": "MOCK-A", "num_units": 42, "dev_type": "residential", "status": "granted",
              "description": "MOCK: 42 apartments", "connected": True, "distance_to_network_m": 18.0}),
        feat({"type": "Point", "coordinates": [-6.2480, 53.3488]},
             {"app_id": "MOCK-B", "num_units": 120, "dev_type": "mixed", "status": "pending",
              "description": "MOCK: 120 units over retail", "connected": False, "distance_to_network_m": 75.0}),
        feat({"type": "Point", "coordinates": [-6.2452, 53.3489]},
             {"app_id": "MOCK-C", "num_units": 300, "dev_type": "residential", "status": "granted",
              "description": "MOCK: 300 build-to-rent units", "connected": False, "distance_to_network_m": 260.0}),
    ]))
    write_json(MOCK / "extensions.geojson", fc([
        feat(line((-6.2495, 53.3481), (-6.2465, 53.3482)),
             {"edge_id": "mock-4", "order": 0, "length_m": 200.0, "serves_app_ids": ["MOCK-B", "MOCK-C"]}),
        feat(line((-6.2465, 53.3482), (-6.2450, 53.3483)),
             {"edge_id": "mock-5", "order": 1, "length_m": 100.0, "serves_app_ids": ["MOCK-C"]}),
    ]))
    write_json(MOCK / "summary.json", {
        "total_units": 462, "unconnected_units": 420, "new_pipe_km": 0.3,
        "top_areas": [{"name": "MOCK North Dock", "units": 462, "new_pipe_km": 0.3}],
        "sources": {"hydrants_osm": 1, "mapillary": 1, "planning": 3},
        "generated_at": "2026-01-01T00:00:00+00:00",
    })
    log(f"  wrote mock files to {MOCK}")
