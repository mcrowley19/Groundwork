"""Paths, source-status tracking and geometry helpers for the impact dashboard.

Outputs go to data/impact/ (the water monitor keeps data/). IMPACT_OUT / IMPACT_INTERIM /
IMPACT_WATER_DIR override the locations so tests can run against fixtures without ever
writing into data/.
"""
from __future__ import annotations

import os
from pathlib import Path

import geopandas as gpd
from shapely.geometry import mapping

from pipeline.common import CACHE, DATA, INTERIM, METRIC_CRS, WGS84, read_json, write_json

from . import config as C

OUT = Path(os.environ.get("IMPACT_OUT") or DATA / "impact")
MOCK = OUT / "mock"
IMPACT_INTERIM = Path(os.environ.get("IMPACT_INTERIM") or INTERIM / "impact")
WATER_DIR = Path(os.environ.get("IMPACT_WATER_DIR") or DATA)   # pipeline stage-4 outputs
PIPE_INTERIM = Path(os.environ.get("IMPACT_PIPE_INTERIM") or INTERIM)  # planning.json, extracted.json, graph
SOURCES = IMPACT_INTERIM / "sources.json"
DEVS = IMPACT_INTERIM / "developments.json"   # carried between stages 1→4
for d in (OUT, IMPACT_INTERIM):
    d.mkdir(parents=True, exist_ok=True)

OUTPUT_FILES = ["developments.geojson", "stops.geojson", "rail_lines.geojson",
                "transport_routes.geojson", "schools.geojson", "network.geojson",
                "extensions.geojson", "assumptions.json", "summary.json"]

__all__ = ["CACHE", "OUT", "MOCK", "IMPACT_INTERIM", "WATER_DIR", "PIPE_INTERIM", "DEVS",
           "METRIC_CRS", "WGS84", "read_json", "write_json", "source", "fc", "feature", "geom_out"]


def source(name: str, status: str, records: int | None, note: str | None = None) -> None:
    """status ∈ ok | partial | failed — surfaced verbatim in summary.sources."""
    assert status in ("ok", "partial", "failed")
    s = read_json(SOURCES, {})
    s[name] = {"status": status, "records": int(records or 0), **({"note": note} if note else {})}
    write_json(SOURCES, s)


def fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def feature(geometry: dict, props: dict) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _round(c, nd):
    if isinstance(c[0], (int, float)):
        return [round(c[0], nd), round(c[1], nd)]
    return [_round(x, nd) for x in c]


def geom_out(geom_metric, simplify: bool = True) -> dict:
    """Metric (EPSG:2157) shapely geometry -> simplified, rounded WGS84 GeoJSON dict."""
    g = geom_metric.simplify(C.SIMPLIFY_TOLERANCE_M, preserve_topology=True) if simplify else geom_metric
    g = gpd.GeoSeries([g], crs=METRIC_CRS).to_crs(WGS84).iloc[0]
    m = mapping(g)
    return {"type": m["type"], "coordinates": _round(m["coordinates"], C.COORD_DECIMALS)}


def round_geojson(geom: dict) -> dict:
    return {"type": geom["type"], "coordinates": _round(geom["coordinates"], C.COORD_DECIMALS)}
