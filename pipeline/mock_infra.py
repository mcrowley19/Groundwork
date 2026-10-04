"""Mock dataset for the Infrastructure Impact dashboard (served when MOCK=1).

Everything here is grounded in public data already on disk, but the per-development
infrastructure numbers are prototype estimates, not an assessment:

- developments: the largest residential applications in data/interim/planning.json
  (unit counts parsed from the description text, so treat them as approximate);
  footprints are rectangles sized from the unit count because NPAD has no site boundary
- stops, rail lines, stations and schools: OpenStreetMap from the local extract
  (data/cache/osm/dublin.osm.pbf); bus frequencies and school enrolments are synthetic
- water: distance to the inferred network in data/network.geojson, sized per IW-CDS-5020-03
- proposed bus routes: shortest street paths from car-dependent sites to the nearest station

    .venv/bin/python -m pipeline.mock_infra            # writes data/mock/*
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points

from . import sizing
from .common import CACHE, DATA, INTERIM, METRIC_CRS, MOCK, WGS84, feature_collection, log, read_json, round_coords, write_json

BBOX = (-6.50, 53.22, -6.05, 53.47)   # greater Dublin city: W, S, E, N
PBF = CACHE / "osm" / "dublin.osm.pbf"
MAX_DEVS = 45
MIN_UNITS = 80

# --- assumptions (every estimated number in the UI points at one of these) --------------
ASSUMPTIONS = [
    {"key": "occupancy", "label": "Residents per dwelling", "value": sizing.OCCUPANCY, "unit": "persons",
     "source_note": "Uisce Éireann IW-CDS-5020-03 §3.7.2 design occupancy"},
    {"key": "site_area_per_unit", "label": "Site area per unit (footprint rectangle)", "value": 90, "unit": "m²",
     "source_note": "Prototype assumption (~110 units/ha). NPAD has no site boundary, so footprints are generated"},
    {"key": "water_connect_m", "label": "Connected if within", "value": 50, "unit": "m",
     "source_note": "Pipeline stage 4 threshold to the nearest served edge of the inferred network"},
    {"key": "water_route_factor", "label": "Pipe route length ÷ straight-line distance", "value": 1.25, "unit": "×",
     "source_note": "Prototype assumption for street-following routes"},
    {"key": "water_cost_100", "label": "Installed cost, 100 mm main", "value": 450, "unit": "€/m", "source_note": "Prototype assumption, urban open-cut"},
    {"key": "water_cost_150", "label": "Installed cost, 150 mm main", "value": 550, "unit": "€/m", "source_note": "Prototype assumption, urban open-cut"},
    {"key": "water_cost_200", "label": "Installed cost, 200 mm main", "value": 700, "unit": "€/m", "source_note": "Prototype assumption, urban open-cut"},
    {"key": "water_cost_250", "label": "Installed cost, ≥ 250 mm main", "value": 900, "unit": "€/m", "source_note": "Prototype assumption, urban open-cut"},
    {"key": "peak_trip_rate", "label": "Peak-hour trips per resident", "value": 0.12, "unit": "trips/h",
     "source_note": "Prototype assumption in the range of TII/TRICS residential peak rates"},
    {"key": "pt_mode_share", "label": "Public transport mode share, new residents", "value": 0.35, "unit": "share",
     "source_note": "Prototype assumption; Census 2022 Dublin commuting mode share is ~25–30%"},
    {"key": "frequent_bus", "label": "'Frequent' stop threshold", "value": 6, "unit": "buses/h", "source_note": "BusConnects spine-level frequency, prototype threshold"},
    {"key": "walk_stop_m", "label": "Walk distance to a bus stop", "value": 400, "unit": "m", "source_note": "DMURS / common planning guidance"},
    {"key": "walk_frequent_m", "label": "Walk distance to a frequent stop", "value": 800, "unit": "m", "source_note": "BusConnects catchment convention"},
    {"key": "walk_rail_m", "label": "Walk distance to rail / Luas", "value": 1000, "unit": "m", "source_note": "NTA rail catchment convention"},
    {"key": "bus_speed", "label": "Average bus speed, proposed routes", "value": 20, "unit": "km/h", "source_note": "Prototype assumption for urban routes"},
    {"key": "bus_layover", "label": "Layover per round trip", "value": 10, "unit": "min", "source_note": "Prototype assumption"},
    {"key": "bus_headway", "label": "Headway, proposed routes", "value": 15, "unit": "min", "source_note": "Prototype assumption"},
    {"key": "primary_pupils_per_unit", "label": "Primary pupils per new dwelling", "value": 0.20, "unit": "pupils",
     "source_note": "Prototype assumption; Department of Education uses 12% of new housing population"},
    {"key": "secondary_pupils_per_unit", "label": "Secondary pupils per new dwelling", "value": 0.12, "unit": "pupils",
     "source_note": "Prototype assumption; Department of Education uses 8.5% of new housing population"},
    {"key": "school_primary_radius", "label": "Primary school catchment", "value": 1500, "unit": "m", "source_note": "Prototype assumption"},
    {"key": "school_secondary_radius", "label": "Secondary school catchment", "value": 3000, "unit": "m", "source_note": "Prototype assumption"},
    {"key": "school_headroom", "label": "Spare capacity over current enrolment", "value": 0.12, "unit": "share",
     "source_note": "Prototype assumption; enrolments here are synthetic, not DoE returns"},
]
A = {a["key"]: a["value"] for a in ASSUMPTIONS}


def h01(s: str) -> float:
    """Deterministic pseudo-random in [0, 1) from a string."""
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def cost_per_m(bore: int) -> int:
    return A["water_cost_100"] if bore <= 100 else A["water_cost_150"] if bore <= 150 else A["water_cost_200"] if bore <= 200 else A["water_cost_250"]


# --- developments from the planning register --------------------------------------------
UNIT_RE = re.compile(r"(?<![\d-])(\d{2,4})\s*(?:no\.?\s*)?(?:new\s+)?(?:residential\s+)?"
                     r"(?:apartments?|dwellings?|units|houses|homes|bedspaces)", re.I)


def pick_developments() -> list[dict]:
    apps = read_json(INTERIM / "planning.json", [])
    out, seen = [], set()
    for a in sorted(apps, key=lambda a: a.get("received_date") or "", reverse=True):
        d = a.get("description") or ""
        m = UNIT_RE.search(d)
        if not m or not a.get("lat"):
            continue
        units = int(m.group(1))
        if not (MIN_UNITS <= units <= 1200) or re.search(r"pre-?\s*" + m.group(1), d, re.I):
            continue
        if not (BBOX[0] <= a["lon"] <= BBOX[2] and BBOX[1] <= a["lat"] <= BBOX[3]):
            continue
        st = f"{a.get('decision') or ''} {a.get('application_status') or ''}".lower()
        if re.search(r"invalid|withdrawn|incomplete|refuse", st):
            continue
        key = (round(a["lat"], 3), round(a["lon"], 3))
        if key in seen:
            continue
        seen.add(key)
        status = "granted" if re.search(r"grant|conditional|approval|final grant", st) else "pending"
        out.append({**a, "num_units": units, "status": status,
                    "dev_type": "mixed" if re.search(r"mixed|retail|commercial|office", d, re.I) else "residential"})
    out.sort(key=lambda a: -a["num_units"])
    return out[:MAX_DEVS]


def footprint(lon: float, lat: float, units: int, seed: str):
    area = units * A["site_area_per_unit"]
    w = math.sqrt(area / 1.6)
    hgt = area / w
    ang = h01(seed) * math.pi
    pt = gpd.GeoSeries([Point(lon, lat)], crs=WGS84).to_crs(METRIC_CRS)[0]
    c, s = math.cos(ang), math.sin(ang)
    corners = [(-w / 2, -hgt / 2), (w / 2, -hgt / 2), (w / 2, hgt / 2), (-w / 2, hgt / 2)]
    poly = Polygon([(pt.x + x * c - y * s, pt.y + x * s + y * c) for x, y in corners])
    return gpd.GeoSeries([poly], crs=METRIC_CRS).to_crs(WGS84)[0]


# --- OpenStreetMap via the local extract --------------------------------------------------
def osm_layers():
    from pyrosm import OSM

    osm = OSM(str(PBF), bounding_box=list(BBOX))
    stops = osm.get_data_by_custom_criteria(custom_filter={"highway": ["bus_stop"]}, filter_type="keep",
                                            keep_ways=False, keep_relations=False)
    stations = osm.get_data_by_custom_criteria(custom_filter={"railway": ["station", "halt", "tram_stop"]},
                                               filter_type="keep", keep_relations=False)
    rails = osm.get_data_by_custom_criteria(custom_filter={"railway": ["rail", "light_rail", "tram"]},
                                            filter_type="keep", keep_nodes=False, keep_relations=False)
    schools = osm.get_data_by_custom_criteria(custom_filter={"amenity": ["school"]}, filter_type="keep",
                                              keep_relations=False)
    log(f"  OSM: {len(stops)} bus stops, {len(stations)} stations, {len(rails)} rail ways, {len(schools)} schools")
    return stops, stations, rails, schools


def build_stops(stops: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    g = stops[stops.geometry.notna()].copy()
    g = g[g.geometry.geom_type == "Point"]
    routes = g.get("route_ref", pd.Series([None] * len(g), index=g.index)).fillna("")
    g["stop_id"] = ["osm:" + str(i) for i in g["id"]]
    g["name"] = g.get("name", pd.Series([None] * len(g), index=g.index)).fillna("Bus stop")
    g["routes"] = [sorted({r.strip() for r in str(r).replace(",", ";").split(";") if r.strip()}) for r in routes]
    # synthetic frequencies: route_ref count when tagged, else a minority of stops are frequent
    g["peak_buses_per_hr"] = [max(len(r) * 3, 2) if r else (int(6 + h01(sid) * 8) if h01(sid + "f") < 0.12 else int(2 + h01(sid) * 3))
                              for r, sid in zip(g["routes"], g["stop_id"])]
    return g[["stop_id", "name", "peak_buses_per_hr", "routes", "geometry"]].reset_index(drop=True)


def build_rail(rails: gpd.GeoDataFrame) -> list[dict]:
    feats = []
    for r in rails.itertuples():
        if r.geometry is None or r.geometry.geom_type not in ("LineString", "MultiLineString"):
            continue
        rw = getattr(r, "railway", "rail")
        name = getattr(r, "name", None)
        mode = "luas" if rw in ("light_rail", "tram") or (name and "luas" in str(name).lower()) else "rail"
        if isinstance(name, float):
            name = None
        feats.append({"type": "Feature", "geometry": round_coords(r.geometry.__geo_interface__),
                      "properties": {"id": f"osm:way/{r.id}", "mode": mode, "name": name or ("Luas" if mode == "luas" else "Irish Rail")}})
    return feats


def build_schools(schools: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    g = schools[schools.geometry.notna()].copy()
    g["geometry"] = g.geometry.representative_point()
    g["name"] = g.get("name", pd.Series([None] * len(g), index=g.index))
    g = g[g["name"].notna()].copy()
    g["school_id"] = ["osm:" + str(i) for i in g["id"]]
    def level(n: str) -> str:
        n = n.lower()
        if re.search(r"secondary|community college|community school|comprehensive|college|post.?primary|cbs|loreto|mercy|gaelcholáiste|coláiste", n):
            return "secondary"
        return "primary"
    g["level"] = g["name"].map(level)
    g["enrolment"] = [int((150 + h01(s) * 450) if lv == "primary" else (400 + h01(s) * 600)) for s, lv in zip(g["school_id"], g["level"])]
    return g[["school_id", "name", "level", "enrolment", "geometry"]].reset_index(drop=True)


# --- scoring ------------------------------------------------------------------------------
def status_of(score: float) -> str:
    return "critical" if score >= 65 else "watch" if score >= 35 else "nominal"


def run() -> None:
    devs = pick_developments()
    log(f"  {len(devs)} developments picked from the planning register ({sum(d['num_units'] for d in devs)} units)")
    dev_pts = gpd.GeoDataFrame(devs, geometry=[Point(d["lon"], d["lat"]) for d in devs], crs=WGS84).to_crs(METRIC_CRS)

    # --- water: distance to the inferred network ------------------------------------------
    net = read_json(DATA / "network.geojson") or feature_collection([])
    net_gdf = gpd.GeoDataFrame.from_features(net["features"], crs=WGS84).to_crs(METRIC_CRS) if net["features"] else None
    water, ext_feats, mock_net = {}, [], []
    if net_gdf is not None and len(net_gdf):
        served = net_gdf[net_gdf.confidence != "none"]
        j = gpd.sjoin_nearest(dev_pts[["app_id", "geometry"]], served[["edge_id", "geometry"]], how="left", distance_col="d")
        j = j.sort_values("d").drop_duplicates("app_id").set_index("app_id")
        near_geom = served.set_index("edge_id").geometry
        for d in devs:
            dist = float(j.loc[d["app_id"], "d"])
            connected = dist <= A["water_connect_m"]
            sz = sizing.size_main(d["num_units"])
            length = 0.0 if connected else round(dist * A["water_route_factor"], 1)
            cost = 0 if connected else int(length * cost_per_m(sz["nominal_bore_mm"]))
            water[d["app_id"]] = {"connected": connected, "distance_to_network_m": round(dist, 1), "route_length_m": length,
                                  "est_cost_eur": cost, "required_main_mm": sz["nominal_bore_mm"],
                                  "avg_daily_demand_m3": round(sizing.avg_daily_demand_m3(d["num_units"]), 1)}
            if not connected:
                p0 = dev_pts.set_index("app_id").geometry[d["app_id"]]
                edge = near_geom[j.loc[d["app_id"], "edge_id"]]
                p1 = nearest_points(p0, edge)[1]
                mid = Point((p0.x + p1.x) / 2 + (p1.y - p0.y) * 0.15, (p0.y + p1.y) / 2 - (p1.x - p0.x) * 0.15)
                for k, (a, b) in enumerate([(p1, mid), (mid, p0)]):
                    seg = gpd.GeoSeries([LineString([a, b])], crs=METRIC_CRS).to_crs(WGS84)[0]
                    ext_feats.append({"type": "Feature", "geometry": round_coords(seg.__geo_interface__),
                                      "properties": {"edge_id": f"X-{d['app_id']}-{k}", "order": k, "length_m": round(length / 2, 1),
                                                     "serves_app_ids": [d["app_id"]], "units_served": d["num_units"],
                                                     "nominal_bore_mm": sz["nominal_bore_mm"],
                                                     "est_cost_eur": cost // 2, "street_name": None}})
        hull = dev_pts.buffer(900).union_all()
        sub = net_gdf[net_gdf.intersects(hull)].to_crs(WGS84)
        mock_net = [{"type": "Feature", "geometry": round_coords(g.__geo_interface__),
                     "properties": {"edge_id": r.edge_id, "confidence": r.confidence, "evidence": list(r.evidence or []),
                                    "length_m": r.length_m, "street_name": getattr(r, "street_name", None)}}
                    for r, g in zip(sub.itertuples(), sub.geometry)]
        log(f"  water: {sum(1 for w in water.values() if w['connected'])} connected, {len(mock_net)} network edges kept")
    else:
        log("  network.geojson missing — water fields left empty")

    # --- transport + schools from OSM -----------------------------------------------------
    stops_raw, stations_raw, rails_raw, schools_raw = osm_layers()
    stops = build_stops(stops_raw).to_crs(METRIC_CRS)
    stations = stations_raw[stations_raw.geometry.notna()].copy()
    stations["geometry"] = stations.geometry.representative_point()
    stations["name"] = stations.get("name", pd.Series([None] * len(stations), index=stations.index)).fillna("Unnamed station")
    stations = stations.to_crs(METRIC_CRS)
    schools = build_schools(schools_raw).to_crs(METRIC_CRS)
    frequent = stops[stops.peak_buses_per_hr >= A["frequent_bus"]]

    def nearest(gdf, pt):
        if gdf is None or not len(gdf):
            return None, None
        d = gdf.geometry.distance(pt)
        i = d.idxmin()
        return gdf.loc[i], float(d[i])

    G = ox.load_graphml(INTERIM / "graph.graphml") if (INTERIM / "graph.graphml").exists() else None
    if G is not None:
        # keep the graph unprojected (ox.project_graph rebuilds an undirected graph as one-way edges);
        # nearest-node lookup is a KD-tree over the nodes in ITM
        from scipy.spatial import cKDTree
        node_ids = list(G.nodes)
        node_pts = gpd.GeoSeries([Point(G.nodes[n]["x"], G.nodes[n]["y"]) for n in node_ids], crs=WGS84).to_crs(METRIC_CRS)
        tree = cKDTree(np.column_stack([node_pts.x.values, node_pts.y.values]))
        nearest_node = lambda p: node_ids[tree.query([p.x, p.y])[1]]  # noqa: E731
        log(f"  street graph: {G.number_of_nodes()} nodes for route finding")

    transport, routes, route_of = {}, [], {}
    stop_hits, sch_hits = defaultdict(list), defaultdict(float)
    dev_geom = dev_pts.set_index("app_id").geometry
    for d in devs:
        pt = dev_geom[d["app_id"]]
        s, sd = nearest(stops, pt)
        f, fd = nearest(frequent, pt)
        st, std = nearest(stations, pt)
        residents = round(d["num_units"] * A["occupancy"])
        trips = round(residents * A["peak_trip_rate"] * A["pt_mode_share"])
        if std is not None and std <= A["walk_rail_m"]:
            cls = "rail"
        elif fd is not None and fd <= A["walk_frequent_m"]:
            cls = "frequent_bus"
        elif sd is not None and sd <= A["walk_stop_m"]:
            cls = "bus"
        else:
            cls = "car_dependent"
        transport[d["app_id"]] = {"class": cls, "nearest_stop_m": None if sd is None else round(sd), "nearest_stop_id": None if s is None else s.stop_id,
                                  "nearest_frequent_stop_m": None if fd is None else round(fd), "nearest_frequent_stop_id": None if f is None else f.stop_id,
                                  "nearest_rail_name": None if st is None else str(st["name"]), "nearest_rail_m": None if std is None else round(std),
                                  "residents": residents, "peak_pt_trips": trips, "proposed_route_id": None}
        if cls in ("bus", "car_dependent") and G is not None and st is not None:
            try:
                a = nearest_node(pt)
                b = nearest_node(st.geometry)
                path = nx.shortest_path(G, a, b, weight="length")
                coords = []
                for u, v in zip(path[:-1], path[1:]):
                    e = min(G.get_edge_data(u, v).values(), key=lambda x: x["length"])
                    geom = e.get("geometry") or LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])
                    coords.extend(list(geom.coords)[:-1])
                coords.append((G.nodes[path[-1]]["x"], G.nodes[path[-1]]["y"]))
                line = LineString(coords)  # WGS84
                km = gpd.GeoSeries([line], crs=WGS84).to_crs(METRIC_CRS)[0].length / 1000
                rt = 2 * km / A["bus_speed"] * 60 + A["bus_layover"]
                rid = f"PR-{len(routes) + 1:02d}"
                routes.append({"route_id": rid, "name": f"{rid} {d['app_id']} ↔ {st['name']}", "length_km": round(km, 2),
                               "round_trip_min": round(rt), "headway_min": A["bus_headway"],
                               "buses_required": math.ceil(rt / A["bus_headway"]), "serves_app_ids": [d["app_id"]],
                               "peak_demand": trips, "to_station": str(st["name"]),
                               "geometry": line})
                transport[d["app_id"]]["proposed_route_id"] = rid
                route_of[d["app_id"]] = rid
            except Exception as exc:  # no street path: fall back to a straight-line estimate
                log(f"  no street path for {d['app_id']} ({exc}); straight-line route used")
                line = LineString([pt, st.geometry])
                km = line.length / 1000 * 1.3
                rt = 2 * km / A["bus_speed"] * 60 + A["bus_layover"]
                rid = f"PR-{len(routes) + 1:02d}"
                routes.append({"route_id": rid, "name": f"{rid} {d['app_id']} ↔ {st['name']}", "length_km": round(km, 2),
                               "round_trip_min": round(rt), "headway_min": A["bus_headway"],
                               "buses_required": math.ceil(rt / A["bus_headway"]), "serves_app_ids": [d["app_id"]],
                               "peak_demand": trips, "to_station": str(st["name"]), "path_source": "straight-line × 1.3",
                               "geometry": gpd.GeoSeries([line], crs=METRIC_CRS).to_crs(WGS84)[0]})
                transport[d["app_id"]]["proposed_route_id"] = rid
                route_of[d["app_id"]] = rid
        if s is not None:
            stop_hits[s.stop_id].append(d["app_id"])

    # schools: share each development's pupils across the schools in its catchment
    school_new = defaultdict(float)
    schools_out, sch_pressure = {}, {}
    for d in devs:
        pt = dev_geom[d["app_id"]]
        pp = d["num_units"] * A["primary_pupils_per_unit"]
        sp = d["num_units"] * A["secondary_pupils_per_unit"]
        prim = schools[(schools.level == "primary") & (schools.geometry.distance(pt) <= A["school_primary_radius"])]
        sec = schools[(schools.level == "secondary") & (schools.geometry.distance(pt) <= A["school_secondary_radius"])]
        for sid in prim.school_id:
            school_new[sid] += pp / len(prim)
        for sid in sec.school_id:
            school_new[sid] += sp / len(sec)
        schools_out[d["app_id"]] = {"primary_pupils": round(pp), "secondary_pupils": round(sp),
                                    "nearby_primary": int(len(prim)), "nearby_secondary": int(len(sec)),
                                    "nearby_school_ids": list(prim.school_id) + list(sec.school_id)}
    schools["projected_new_pupils"] = [round(school_new.get(s, 0)) for s in schools.school_id]
    schools["pressure_ratio"] = [round((e + n) / (e * (1 + A["school_headroom"])), 3) for e, n in zip(schools.enrolment, schools.projected_new_pupils)]
    ratio_of = dict(zip(schools.school_id, schools.pressure_ratio))
    for d in devs:
        so = schools_out[d["app_id"]]
        ratios = [ratio_of[s] for s in so["nearby_school_ids"]]
        worst = max(ratios) if ratios else None
        so["worst_pressure_ratio"] = worst
        so["pressure_flag"] = ("critical" if so["nearby_primary"] == 0 or (worst or 0) > 1.15
                               else "watch" if so["nearby_secondary"] == 0 or (worst or 0) > 1.0 else "nominal")

    # --- scores + output ------------------------------------------------------------------
    feats = []
    for d in devs:
        w, t, s = water.get(d["app_id"]), transport[d["app_id"]], schools_out[d["app_id"]]
        ws = None if w is None else (10.0 if w["connected"] else min(100.0, 20 + w["route_length_m"] / 10))
        ts = {"rail": 15, "frequent_bus": 35, "bus": 60, "car_dependent": 85}[t["class"]] + min(15, t["peak_pt_trips"] / 20)
        ss = {"nominal": 20, "watch": 55, "critical": 85}[s["pressure_flag"]] + (min(15, max(-15, ((s["worst_pressure_ratio"] or 1) - 1) * 100)))
        parts = [x for x in (ws, ts, ss) if x is not None]
        overall = 0.6 * max(parts) + 0.4 * sum(parts) / len(parts)
        poly = footprint(d["lon"], d["lat"], d["num_units"], d["app_id"])
        feats.append({"type": "Feature", "geometry": round_coords(poly.__geo_interface__), "properties": {
            "app_id": d["app_id"], "num_units": d["num_units"], "dev_type": d["dev_type"], "status": d["status"],
            "received_date": d.get("received_date"), "description": (d.get("description") or "").strip(),
            "address": d.get("address"), "authority": d.get("authority"), "planning_url": d.get("planning_url"),
            "footprint_source": "estimated — rectangle sized from the unit count; NPAD has no site boundary",
            "centroid": [round(d["lon"], 5), round(d["lat"], 5)],
            "water": w or {"connected": None, "distance_to_network_m": None, "route_length_m": None, "est_cost_eur": None},
            "transport": t, "schools": s,
            "scores": {"water": None if ws is None else round(ws, 1), "transport": round(ts, 1), "schools": round(ss, 1), "overall": round(overall, 1)},
        }})

    MOCK.mkdir(parents=True, exist_ok=True)
    write_json(MOCK / "developments.geojson", feature_collection(feats))
    write_json(MOCK / "extensions.geojson", feature_collection(ext_feats))
    write_json(MOCK / "network.geojson", feature_collection(mock_net))
    stops_w = stops.to_crs(WGS84)
    write_json(MOCK / "stops.geojson", feature_collection([
        {"type": "Feature", "geometry": round_coords(g.__geo_interface__),
         "properties": {"stop_id": r.stop_id, "name": str(r.name), "peak_buses_per_hr": int(r.peak_buses_per_hr), "routes": list(r.routes),
                        "serves_app_ids": stop_hits.get(r.stop_id, [])}}
        for r, g in zip(stops_w.itertuples(), stops_w.geometry)]))
    stations_w = stations.to_crs(WGS84)
    rail_feats = build_rail(rails_raw) + [
        {"type": "Feature", "geometry": round_coords(g.__geo_interface__),
         "properties": {"id": f"osm:station/{r.id}", "mode": "station", "name": str(r.name)}}
        for r, g in zip(stations_w.itertuples(), stations_w.geometry)]
    write_json(MOCK / "rail_lines.geojson", feature_collection(rail_feats))
    write_json(MOCK / "transport_routes.geojson", feature_collection([
        {"type": "Feature", "geometry": round_coords(r.pop("geometry").__geo_interface__), "properties": r} for r in routes]))
    schools_w = schools.to_crs(WGS84)
    write_json(MOCK / "schools.geojson", feature_collection([
        {"type": "Feature", "geometry": round_coords(g.__geo_interface__),
         "properties": {"school_id": r.school_id, "name": str(r.name), "level": r.level, "enrolment": int(r.enrolment),
                        "projected_new_pupils": int(r.projected_new_pupils), "pressure_ratio": float(r.pressure_ratio)}}
        for r, g in zip(schools_w.itertuples(), schools_w.geometry)]))
    write_json(MOCK / "assumptions.json", ASSUMPTIONS)

    units = sum(d["num_units"] for d in devs)
    by_status = {}
    for d in devs:
        b = by_status.setdefault(d["status"], {"count": 0, "units": 0})
        b["count"] += 1
        b["units"] += d["num_units"]
    write_json(MOCK / "summary.json", {
        "planned_units": units,
        "residents": sum(t["residents"] for t in transport.values()),
        "new_peak_pt_trips": sum(t["peak_pt_trips"] for t in transport.values()),
        "proposed_routes": len(routes),
        "buses_required": sum(r["buses_required"] for r in routes),
        "new_primary_pupils": sum(s["primary_pupils"] for s in schools_out.values()),
        "new_secondary_pupils": sum(s["secondary_pupils"] for s in schools_out.values()),
        "water_extension_km": round(sum(w["route_length_m"] for w in water.values()) / 1000, 3),
        "water_cost_eur": sum(w["est_cost_eur"] for w in water.values()),
        "by_status": by_status,
        "sources": {
            "planning": {"name": "NPAD planning register (unit counts parsed from text)", "count": len(devs), "error": None, "official": True},
            "network": {"name": "Inferred water network (OSM + DCC gullies)", "count": len(mock_net), "error": None, "official": False},
            "osm_stops": {"name": "OpenStreetMap bus stops (frequencies synthetic)", "count": int(len(stops)), "error": None, "official": False},
            "osm_rail": {"name": "OpenStreetMap rail / Luas", "count": len(rail_feats), "error": None, "official": False},
            "osm_schools": {"name": "OpenStreetMap schools (enrolments synthetic)", "count": int(len(schools)), "error": None, "official": False},
            "mapillary": {"name": "Mapillary detections", "count": 0, "error": "MAPILLARY_TOKEN not set", "official": False},
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "MOCK dataset: real sites and OSM features, synthetic frequencies/enrolments, prototype assumptions",
    })
    log(f"  mock: {len(feats)} developments, {len(routes)} routes, {len(stops)} stops, {len(schools)} schools, "
        f"{len(rail_feats)} rail features → {MOCK}")


if __name__ == "__main__":
    run()
