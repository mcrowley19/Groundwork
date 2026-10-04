"""Local OpenStreetMap source: a Geofabrik extract read with pyrosm.

The public Overpass servers are unreliable (overpass-api.de answering 406, mirrors timing
out) and a county-scale pull — ~450k+ buildings — is fragile even when they are up. So by
default OSM comes from a downloaded extract instead:

    ireland-and-northern-ireland-latest.osm.pbf  (Geofabrik, ~400 MB, downloaded once)
      -> dublin.osm.pbf  (County Dublin + margin, cut once with pyosmium)
      -> pyrosm reads the requested bbox from whichever file covers it

Set OSM_SOURCE=overpass to use osmnx/Overpass instead.
"""
from __future__ import annotations

import json
import math
import time
import warnings

import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests

from .common import CACHE, WGS84, env, log

PBF_URL = "https://download.geofabrik.de/europe/ireland-and-northern-ireland-latest.osm.pbf"
OSM_DIR = CACHE / "osm"
IRELAND = OSM_DIR / "ireland.osm.pbf"
DUBLIN = OSM_DIR / "dublin.osm.pbf"
DUBLIN_BOX = (-6.65, 53.12, -5.95, 53.70)  # County Dublin + margin

_readers: dict = {}
NODE_ATTRS = {"x", "y", "osmid", "street_count"}
EDGE_ATTRS = {"osmid", "name", "highway", "length", "geometry"}


def use_local() -> bool:
    return (env("OSM_SOURCE") or "local").lower() != "overpass"


def _download() -> None:
    if IRELAND.exists():
        return
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    log(f"  downloading {PBF_URL} (one-off, ~400 MB)")
    t = time.perf_counter()
    tmp = IRELAND.with_suffix(".part")
    with requests.get(PBF_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(IRELAND)
    log(f"  downloaded {IRELAND.stat().st_size // 1_000_000} MB in {time.perf_counter() - t:.0f}s")


def _cut_dublin() -> None:
    if DUBLIN.exists():
        return
    import osmium

    log("  cutting County Dublin extract (one-off, ~3 min)")
    t = time.perf_counter()
    w, s, e, n = DUBLIN_BOX
    box = osmium.osm.Box(osmium.osm.Location(w, s), osmium.osm.Location(e, n))
    tmp = DUBLIN.with_name("dublin.part.osm.pbf")
    with osmium.BackReferenceWriter(str(tmp), ref_src=str(IRELAND), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(IRELAND)).with_locations():
            if obj.is_node():
                if box.contains(obj.location):
                    writer.add(obj)
            elif obj.is_way():
                if any(box.contains(nd.location) for nd in obj.nodes if nd.location.valid()):
                    writer.add(obj)
            elif obj.is_relation():
                writer.add(obj)  # keeps multipolygon buildings/areas resolvable
    tmp.replace(DUBLIN)
    log(f"  cut {DUBLIN.stat().st_size // 1_000_000} MB in {time.perf_counter() - t:.0f}s")


def _inside(bbox, outer) -> bool:
    return bbox[0] >= outer[0] and bbox[1] >= outer[1] and bbox[2] <= outer[2] and bbox[3] <= outer[3]


def reader(bbox):
    import pyrosm

    key = tuple(round(v, 6) for v in bbox)
    if key not in _readers:
        _download()
        if _inside(bbox, DUBLIN_BOX):
            _cut_dublin()
            path = DUBLIN
        else:
            path = IRELAND
        _readers.clear()  # one bbox at a time; these hold a lot of memory
        _readers[key] = pyrosm.OSM(str(path), bounding_box=list(bbox))
    return _readers[key]


def graph(bbox):
    """Simplified osmnx-compatible drive graph (MultiDiGraph)."""
    osm = reader(bbox)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        nodes, edges = osm.get_network(network_type="driving", nodes=True)
        G = osm.to_graph(nodes, edges, graph_type="networkx", osmnx_compatible=True)
    G.graph["crs"] = WGS84
    G = ox.simplify_graph(G)
    # pyrosm carries raw tag values (NaN for absent, "yes"/"no" strings) that osmnx's
    # GraphML loader rejects for its typed attrs. Keep only what the pipeline uses.
    def nan(v):
        return isinstance(v, float) and math.isnan(v)
    for _, d in G.nodes(data=True):
        for k in [k for k in d if k not in NODE_ATTRS or nan(d[k])]:
            del d[k]
    for *_, d in G.edges(keys=True, data=True):
        for k in [k for k in d if k not in EDGE_ATTRS]:
            del d[k]
        for k, v in list(d.items()):
            if isinstance(v, list):
                v = [x for x in v if not nan(x)]
                d[k] = v if v else None
            if d[k] is None or nan(d[k]):
                del d[k]
    return G


def _merge_tags(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """pyrosm keeps common tags as columns and the rest in a `tags` JSON column."""
    if "tags" not in gdf:
        return gdf
    extra = []
    for t in gdf["tags"]:
        if isinstance(t, str):
            try:
                t = json.loads(t)
            except ValueError:
                t = None
        extra.append(t if isinstance(t, dict) else {})
    extra = pd.DataFrame(extra, index=gdf.index)
    extra = extra[[c for c in extra.columns if c not in gdf.columns]]
    return gpd.GeoDataFrame(pd.concat([gdf.drop(columns=["tags"]), extra], axis=1),
                            geometry="geometry", crs=gdf.crs or WGS84)


def features(bbox, tags: dict) -> gpd.GeoDataFrame:
    """Like ox.features_from_bbox: index (element, id), one column per tag, geometry."""
    osm = reader(bbox)
    filt = {k: (True if v is True else ([v] if isinstance(v, str) else list(v))) for k, v in tags.items()}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gdf = osm.get_data_by_custom_criteria(custom_filter=filt, filter_type="keep",
                                              keep_nodes=True, keep_ways=True, keep_relations=True)
    if gdf is None or not len(gdf):
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    gdf = _merge_tags(gdf)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf["element"] = gdf["osm_type"].fillna("node")
    return gdf.set_index(["element", "id"])


def buildings(bbox) -> gpd.GeoDataFrame:
    osm = reader(bbox)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gdf = osm.get_buildings()
    if gdf is None:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    return gdf[["geometry"]].set_crs(WGS84, allow_override=True)
