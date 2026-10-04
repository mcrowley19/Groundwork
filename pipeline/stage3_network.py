"""Stage 3: inferred water network — street graph + snapped evidence (+ assets layer)."""
from __future__ import annotations

import csv
import io
import math
import re
import time
from collections import Counter, defaultdict

import geopandas as gpd
import osmnx as ox
import pandas as pd
import requests
from shapely.geometry import Point

from . import osm_source

from .common import (round_coords, CACHE, DATA, INTERIM, METRIC_CRS, WGS84, env, feature_collection, log,
                     read_json, record_source, write_json)

ox.settings.cache_folder = str(CACHE / "osmnx")
ox.settings.use_cache = True
ox.settings.log_console = False
# County-scale Overpass queries (≈450k buildings) must be split or the server refuses them.
ox.settings.max_query_area_size = 50_000_000  # m² per sub-query
ox.settings.requests_timeout = 300
# overpass-api.de's /status endpoint has been failing (406), which stalls osmnx's
# rate-limit check indefinitely; default to a mirror and skip the slot check.
ox.settings.overpass_url = env("OVERPASS_URL") or "https://overpass.kumi.systems/api"
ox.settings.overpass_rate_limit = False

GRAPH = INTERIM / "graph.graphml"
OUT = DATA / "network.geojson"
ASSETS = DATA / "assets.geojson"
CENSUS = INTERIM / "census.geojson"
GRAPH_PAD_M = 250        # pad the bbox so developments near its edge still reach streets
SNAP_M = 30
BUILDING_M = 25
PIPELINE_BUFFER_M = 15   # an edge "follows" a mapped pipeline if half its length is this close
PIPELINE_SHARE = 0.5
GULLY_MAX_SPREAD_M = 3000  # a street name spread wider than this is ambiguous — not credited
RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def pad_bbox(bbox, metres: float):
    w, s, e, n = bbox
    dlat = metres / 111_320
    dlon = metres / (111_320 * math.cos(math.radians((s + n) / 2)))
    return (w - dlon, s - dlat, e + dlon, n + dlat)


def edge_id(u, v, k) -> str:
    a, b = sorted((u, v))
    return f"{a}-{b}-{k}"


def _first(v):
    return v[0] if isinstance(v, list) else v


def street_label(n):
    """osmnx/pyrosm give a str, a list (merged ways) or NaN."""
    if isinstance(n, list):
        names = list(dict.fromkeys(x for x in n if isinstance(x, str) and x))
        return " / ".join(names) or None
    return n if isinstance(n, str) and n else None


def _clean(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


# --- OSM water infrastructure (one Overpass query) --------------------------------

OSM_TAGS = {
    "emergency": "fire_hydrant",
    "man_made": ["manhole", "pipeline", "pumping_station", "water_works", "water_tower",
                 "reservoir_covered", "storage_tank", "wastewater_plant", "water_well"],
    "amenity": "drinking_water",
}
WATER_SUBSTANCES = {"water", "drinking_water", "potable_water"}
SEWER_SUBSTANCES = {"sewage", "wastewater", "drain", "rainwater", "combined", "sewer"}
NON_WATER_MANHOLES = {"telecom", "gas", "power", "electricity", "heating", "cable_tv"}
DETAIL_TAGS = ["fire_hydrant:type", "fire_hydrant:position", "fire_hydrant:diameter",
               "water_source", "pressure", "substance", "manhole", "location", "diameter",
               "material", "content", "capacity", "ref", "operator", "start_date"]


def classify_osm(t: dict) -> tuple[str | None, str | None]:
    """-> (asset kind, evidence level or None if the asset is not street evidence)."""
    g = lambda k: _clean(t.get(k))  # noqa: E731
    substance = (g("substance") or g("pipeline") or g("type") or "").lower()
    if g("emergency") == "fire_hydrant":
        return "hydrant", "high"
    mm = g("man_made")
    if mm == "pipeline":
        if substance in WATER_SUBSTANCES:
            return "water_pipeline", "high"
        if substance in SEWER_SUBSTANCES:
            return "sewer_pipeline", "medium"
        return None, None  # gas, oil, unknown — not water evidence
    if mm == "manhole":
        if (g("manhole") or "").lower() in NON_WATER_MANHOLES:
            return None, None
        kind = (g("manhole") or "unspecified").lower()
        return f"manhole_{kind}", "medium"
    if g("amenity") == "drinking_water":
        return "drinking_fountain", "medium"
    if mm == "pumping_station":
        if substance in WATER_SUBSTANCES or not substance:
            return "water_pumping_station", None
        if substance in SEWER_SUBSTANCES:
            return "sewage_pumping_station", None
        return None, None
    if mm == "storage_tank":
        return ("water_storage_tank", None) if (g("content") or "").lower() == "water" else (None, None)
    if mm in {"water_works", "water_tower", "reservoir_covered", "wastewater_plant", "water_well"}:
        return mm, None
    return None, None


def osm_water(bbox) -> gpd.GeoDataFrame | None:
    try:
        gdf = (osm_source.features(bbox, OSM_TAGS) if osm_source.use_local()
               else ox.features_from_bbox(bbox, OSM_TAGS))
    except ox._errors.InsufficientResponseError:
        gdf = gpd.GeoDataFrame(geometry=[], crs=WGS84)
    except Exception as exc:
        log(f"  OSM water infrastructure FAILED: {exc}")
        record_source("hydrants_osm", 0, str(exc))
        record_source("osm_water_assets", 0, str(exc))
        return None
    rows = []
    for (el, oid), r in gdf.iterrows():
        t = {k: v for k, v in r.items() if k != "geometry"}
        kind, level = classify_osm(t)
        if not kind:
            continue
        geom = r.geometry if kind.endswith("pipeline") else r.geometry.representative_point()
        rows.append({
            "asset_id": f"osm:{el}/{oid}", "kind": kind, "source": "OpenStreetMap",
            "level": level, "name": _clean(t.get("name")),
            "details": {k: str(_clean(t[k])) for k in DETAIL_TAGS if _clean(t.get(k)) is not None},
            "geometry": geom,
        })
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84) if rows else None
    counts = Counter(r["kind"] for r in rows)
    record_source("hydrants_osm", counts.get("hydrant", 0))
    record_source("osm_water_assets", len(rows))
    log(f"  OSM water infrastructure: {len(rows)} — " +
        ", ".join(f"{k}={v}" for k, v in counts.most_common()))
    return out


# --- Mapillary --------------------------------------------------------------------

MAPILLARY_TILE_DEG = 0.005
MAPILLARY_VALUES = "object--fire-hydrant,object--manhole"


def mapillary(bbox) -> gpd.GeoDataFrame | None:
    token = env("MAPILLARY_TOKEN")
    if not token:
        log("  Mapillary: MAPILLARY_TOKEN not set — skipped")
        record_source("mapillary", 0, "MAPILLARY_TOKEN not set")
        return None
    w, s, e, n = bbox
    seen: dict[str, tuple] = {}
    try:
        lat = s
        while lat < n:
            lon = w
            while lon < e:
                tile = (lon, lat, min(lon + MAPILLARY_TILE_DEG, e), min(lat + MAPILLARY_TILE_DEG, n))
                url = "https://graph.mapillary.com/map_features"
                params = {"access_token": token, "fields": "id,object_value,geometry,first_seen_at",
                          "bbox": ",".join(f"{v:.6f}" for v in tile),
                          "object_values": MAPILLARY_VALUES, "limit": 2000}
                while url:
                    r = requests.get(url, params=params, timeout=60)
                    r.raise_for_status()
                    body = r.json()
                    for f in body.get("data", []):
                        seen[f["id"]] = (f["object_value"], f["geometry"]["coordinates"],
                                         f.get("first_seen_at"))
                    url = body.get("paging", {}).get("next")
                    params = None
                lon += MAPILLARY_TILE_DEG
            lat += MAPILLARY_TILE_DEG
    except Exception as exc:
        log(f"  Mapillary FAILED: {exc}")
        record_source("mapillary", 0, str(exc))
        return None
    rows = []
    for fid, (value, (x, y), seen_at) in seen.items():
        if "hydrant" in value:
            kind, level = "hydrant", "high"
        elif "manhole" in value:
            kind, level = "manhole_unspecified", "medium"
        else:
            continue
        rows.append({"asset_id": f"mapillary:{fid}", "kind": kind, "source": "Mapillary",
                     "level": level, "name": None,
                     "details": {"object_value": value, **({"first_seen_at": str(seen_at)} if seen_at else {})},
                     "geometry": Point(x, y)})
    record_source("mapillary", len(rows))
    log(f"  Mapillary features: {len(rows)} "
        f"({sum(r['level'] == 'high' for r in rows)} hydrants, "
        f"{sum(r['level'] == 'medium' for r in rows)} manholes)")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84) if rows else None


# --- OSM buildings ----------------------------------------------------------------

def osm_buildings(bbox) -> gpd.GeoDataFrame | None:
    try:
        gdf = (osm_source.buildings(bbox) if osm_source.use_local()
               else ox.features_from_bbox(bbox, {"building": True}))
    except ox._errors.InsufficientResponseError:
        return None
    except Exception as exc:
        log(f"  OSM buildings FAILED: {exc}")
        record_source("buildings_osm", 0, str(exc))
        return None
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon", "Point"])]
    record_source("buildings_osm", len(gdf))
    log(f"  OSM buildings: {len(gdf)}")
    return gdf[["geometry"]].reset_index(drop=True)


# --- DCC gully cleaning programme (street-name matched) ---------------------------

GULLY_URLS = [
    "https://data.smartdublin.ie/dataset/fe593b1b-88ef-4ada-9908-90d91088db0b/resource/"
    f"{rid}/download/{name}.csv"
    for rid, name in [
        ("c28e29c1-62de-4628-89d0-2d8e81b645e4", "dccgullycleaningdaily2004-11central-areap20110923-1517"),
        ("4b3d7386-55e4-4ca3-8f96-51e0bd7b8ba6", "dccgullycleaningdaily2004-11southeastarea-p20110923-1519"),
        ("01e26bfb-e2ae-4773-bc6b-722d420749c5", "dccgullycleaningdaily2004-11northcentralareap20110923-1518"),
        ("6d77a56b-0b11-4acd-be0c-83c6a6c8a602", "dccgullycleaningdaily2004-11northwestarea-p20110923-1518"),
        ("9ed3d9f3-9acd-4be2-9164-d5663186e04c", "dccgullycleaningdaily2004-11southcentralarea-p20110923-1519"),
    ]
]
ABBREV = {"RD": "ROAD", "AVE": "AVENUE", "AV": "AVENUE", "SQ": "SQUARE", "PL": "PLACE",
          "TCE": "TERRACE", "TER": "TERRACE", "UPR": "UPPER", "LR": "LOWER", "LWR": "LOWER",
          "NTH": "NORTH", "STH": "SOUTH", "PDE": "PARADE", "CRES": "CRESCENT", "GDNS": "GARDENS",
          "PK": "PARK", "LN": "LANE", "QY": "QUAY", "DR": "DRIVE", "CT": "COURT", "GRV": "GROVE",
          "BR": "BRIDGE", "STR": "STREET", "HSE": "HOUSE", "MT": "MOUNT"}


def norm_street(name: str) -> str:
    toks = re.sub(r"[^A-Z0-9 ]", "", name.upper().replace("'", "").replace(".", " ")).split()
    out = []
    for i, t in enumerate(toks):
        if t in ("ST", "SAINT"):
            out.append("SAINT" if i == 0 else "STREET")
        else:
            out.append(ABBREV.get(t, t))
    return " ".join(out)


QUALIFIERS = {"UPPER", "LOWER", "MIDDLE", "NORTH", "SOUTH", "EAST", "WEST", "LITTLE", "GREAT"}
STREET_TYPES = {"STREET", "ROAD", "AVENUE", "QUAY", "LANE", "PLACE", "SQUARE", "TERRACE",
                "PARADE", "CRESCENT", "GARDENS", "PARK", "DRIVE", "COURT", "GROVE", "WALK", "ROW"}


def street_keys(normed: str) -> list[str]:
    """Match keys from most to least specific: exact, word-order-free, minus qualifiers
    (Upper/Lower/North…), minus the street type (DCC sometimes truncates it)."""
    toks = normed.split()
    keys = [normed, " ".join(sorted(toks))]
    core = [t for t in toks if t not in QUALIFIERS]
    keys.append(" ".join(sorted(core)))
    truncated = len(core) > 1 and core[-1] in STREET_TYPES
    keys.append(" ".join(sorted(core[:-1])) if truncated else None)
    return keys  # always 4 entries, aligned with gully_lookup's tiers


def gully_lookup(gullies: dict[str, dict]):
    """name -> [(dcc_street, record)] using the most specific key tier that hits."""
    tiers: list[dict[str, list]] = [defaultdict(list) for _ in range(4)]
    for k, rec in gullies.items():
        toks = k.split()
        tiers[0][k].append((k, rec))
        tiers[1][" ".join(sorted(toks))].append((k, rec))
        # Unqualified/truncated DCC names are indexed as-is for the looser tiers.
        if not QUALIFIERS & set(toks):
            tiers[2][" ".join(sorted(toks))].append((k, rec))
            tiers[3][" ".join(sorted(toks))].append((k, rec))

    def find(name: str) -> list:
        for tier, key in zip(tiers, street_keys(norm_street(name))):
            if key is not None and key in tier:
                return tier[key]
        return []
    return find


def gully_streets() -> dict[str, dict] | None:
    """Street -> {visits, inspections} from DCC's 2004–2011 gully cleaning returns."""
    cache = CACHE / "dcc_gullies.json"
    cached = read_json(cache)
    if cached is not None:
        return cached
    agg: dict[str, dict] = defaultdict(lambda: {"visits": 0, "inspections": 0})
    try:
        for url in GULLY_URLS:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            lines = r.content.decode("latin-1").splitlines()
            start = next(i for i, l in enumerate(lines) if l.startswith("Date,Area,Street"))
            for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
                raw = (row.get("Street") or "").strip()
                # Keep the leading UPPER-CASE street name; DCC appends lower-case section notes.
                words = []
                for w in raw.split():
                    if any(c.islower() for c in w):
                        break
                    words.append(w)
                if not words:
                    continue
                key = norm_street(" ".join(words))
                agg[key]["visits"] += 1
                try:
                    agg[key]["inspections"] += int(row.get("Insp.") or 0)
                except ValueError:
                    pass
    except Exception as exc:
        log(f"  DCC gullies FAILED: {exc}")
        record_source("dcc_gullies", 0, str(exc))
        return None
    write_json(cache, agg)
    return dict(agg)


# --- run --------------------------------------------------------------------------

def run(bbox) -> None:
    gbbox = pad_bbox(bbox, GRAPH_PAD_M)
    t = time.perf_counter()
    if osm_source.use_local():
        G = osm_source.graph(gbbox)
    else:
        G = ox.graph_from_bbox(gbbox, network_type="drive", truncate_by_edge=True)
    G = ox.convert.to_undirected(G)
    log(f"  street graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges "
        f"({time.perf_counter() - t:.1f}s)")

    edges = ox.graph_to_gdfs(G, nodes=False).reset_index()
    edges["edge_id"] = [edge_id(u, v, k) for u, v, k in zip(edges.u, edges.v, edges.key)]
    names = edges["name"] if "name" in edges else pd.Series([None] * len(edges))
    edges["street_name"] = [street_label(n) for n in names]
    edges["highway"] = [_first(h) for h in edges["highway"]]
    edges = edges[["edge_id", "u", "v", "key", "length", "street_name", "highway",
                   "geometry"]].to_crs(METRIC_CRS)
    evidence: dict[str, list[str]] = {eid: [] for eid in edges.edge_id}
    best: dict[str, str] = {eid: "none" for eid in edges.edge_id}

    def credit(eid: str, level: str, ev: str) -> None:
        evidence[eid].append(ev)
        if RANK[level] > RANK[best[eid]]:
            best[eid] = level

    ebbox = gbbox  # graph padding (250 m) already exceeds the 30 m snap distance
    t = time.perf_counter()
    found = [g for g in (osm_water(ebbox), mapillary(ebbox)) if g is not None and len(g)]
    assets = pd.concat(found, ignore_index=True) if found else None
    if assets is not None:
        assets = gpd.GeoDataFrame(assets, geometry="geometry", crs=WGS84)
        ev = assets[assets.level.notna()].to_crs(METRIC_CRS)
        pts = ev[ev.geom_type == "Point"]
        lines = ev[ev.geom_type != "Point"]
        if len(pts):
            snapped = gpd.sjoin_nearest(pts, edges[["edge_id", "geometry"]], how="inner",
                                        max_distance=SNAP_M, distance_col="dist")
            snapped = snapped.sort_values("dist").drop_duplicates("asset_id")
            for row in snapped.itertuples():
                detail = ", ".join(f"{k}={v}" for k, v in row.details.items()
                                   if k in ("fire_hydrant:type", "water_source", "fire_hydrant:diameter",
                                            "manhole", "pressure"))
                credit(row.edge_id, row.level,
                       f"{row.kind}:{row.asset_id}" + (f" ({detail})" if detail else ""))
            log(f"  snapped {len(snapped)}/{len(pts)} point features within {SNAP_M} m")
        for row in lines.itertuples():
            buf = row.geometry.buffer(PIPELINE_BUFFER_M)
            cand = edges.iloc[edges.sindex.query(buf, predicate="intersects")]
            n = 0
            for e in cand.itertuples():
                if e.geometry.intersection(buf).length >= PIPELINE_SHARE * e.geometry.length:
                    credit(e.edge_id, row.level, f"{row.kind}:{row.asset_id}")
                    n += 1
            log(f"  {row.kind} {row.asset_id} follows {n} edges")

    gullies = gully_streets()
    if gullies:
        dcc = None
        census = read_json(CENSUS)
        if census and census.get("features"):
            sa = gpd.GeoDataFrame.from_features(census["features"], crs=WGS84)
            dcc_sa = sa[sa.local_authority.str.upper() == "DUBLIN CITY COUNCIL"]
            dcc = dcc_sa.to_crs(METRIC_CRS).union_all() if len(dcc_sa) else None
        matched_edges = 0
        streets = 0
        find = gully_lookup(gullies)
        for name, grp in edges[edges.street_name.notna()].groupby("street_name"):
            hits = {k: rec for n in name.split(" / ") for k, rec in find(n)}
            hits = list(hits.values())
            if not hits:
                continue
            if dcc is not None:
                grp = grp[grp.intersects(dcc)]
            else:
                continue  # no boundary -> can't tell DCC's street from a namesake elsewhere
            if not len(grp):
                continue
            x0, y0, x1, y1 = grp.total_bounds
            if math.hypot(x1 - x0, y1 - y0) > GULLY_MAX_SPREAD_M:
                continue
            visits = sum(h["visits"] for h in hits)
            insp = sum(h["inspections"] for h in hits)
            for eid in grp.edge_id:
                credit(eid, "medium", f"dcc_gullies:{name} ({visits} cleaning visits, "
                                      f"{insp} gully inspections 2004-11)")
            streets += 1
            matched_edges += len(grp)
        record_source("dcc_gullies", streets)
        log(f"  DCC gullies: {len(gullies)} streets in returns, matched {streets} streets "
            f"/ {matched_edges} edges" + ("" if dcc is not None else " (no DCC boundary — skipped)"))

    bld = osm_buildings(gbbox)
    if bld is not None and len(bld):
        near = gpd.sjoin(edges[["edge_id", "geometry"]], bld.to_crs(METRIC_CRS),
                         predicate="dwithin", distance=BUILDING_M)
        counts = near.groupby("edge_id").size()
        for eid, c in counts.items():
            credit(eid, "low", f"osm_buildings_within_{BUILDING_M}m:{int(c)}")
        log(f"  {len(counts)} edges have buildings within {BUILDING_M} m")
    log(f"  evidence snapping took {time.perf_counter() - t:.1f}s")

    ox.save_graphml(G, GRAPH)

    # assets layer (every water asset found, not just the ones that snapped)
    asset_feats = []
    if assets is not None:
        for row in assets.itertuples():
            asset_feats.append({
                "type": "Feature", "geometry": round_coords(row.geometry.__geo_interface__),
                "properties": {"asset_id": row.asset_id, "kind": row.kind, "source": row.source,
                               "name": row.name, "evidence_level": row.level,
                               "details": row.details},
            })
    write_json(ASSETS, feature_collection(asset_feats))
    log(f"  assets.geojson: {len(asset_feats)} features")

    edges["confidence"] = edges.edge_id.map(best)
    out = edges.to_crs(WGS84)
    feats = [{
        "type": "Feature",
        "geometry": round_coords(row.geometry.__geo_interface__),
        "properties": {
            "edge_id": row.edge_id,
            "confidence": row.confidence,
            "evidence": evidence[row.edge_id],
            "length_m": round(float(row.length), 1),
            "street_name": row.street_name,
            "highway": row.highway,
            "evidence_counts": dict(Counter(e.split(":", 1)[0] for e in evidence[row.edge_id])),
        },
    } for row in out.itertuples()]
    write_json(OUT, feature_collection(feats))
    counts = edges.confidence.value_counts().to_dict()
    log(f"  network.geojson: {len(feats)} edges — " +
        ", ".join(f"{k}={counts.get(k, 0)}" for k in ("high", "medium", "low", "none")))
