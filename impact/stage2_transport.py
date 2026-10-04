"""Impact stage 2: public transport access, demand and proposed new bus routes.

GTFS: the NTA static feed (config.GTFS_URL, from data.gov.ie 'nta-gtfs'), cached under
data/cache/gtfs/. Frequencies are AM-peak (07:00–10:00) departures on one representative
weekday: the first Tuesday on or after both today and the feed start date.
"""
from __future__ import annotations

import math
import zipfile
from datetime import date, datetime, timedelta

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import requests
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import LineString, box
from sklearn.cluster import DBSCAN

from pipeline.common import log
from pipeline.stage3_network import pad_bbox

from . import config as C
from .common import (CACHE, DEVS, IMPACT_INTERIM, METRIC_CRS, OUT, PIPE_INTERIM, WGS84, fc, feature,
                     geom_out, read_json, source, write_json)

GTFS_DIR = CACHE / "gtfs"
GTFS_ZIP = GTFS_DIR / "GTFS_All.zip"
NEEDED = ["feed_info.txt", "routes.txt", "trips.txt", "calendar.txt", "calendar_dates.txt", "stops.txt",
          "stop_times.txt", "shapes.txt"]
STOP_PAD_M = 2000      # stops/stations this far outside the bbox still count as "nearest"
PEAK = ("07:00:00", "10:00:00")
_to_m = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)
_to_ll = Transformer.from_crs(METRIC_CRS, WGS84, always_xy=True)


def ensure_gtfs() -> None:
    GTFS_DIR.mkdir(parents=True, exist_ok=True)
    if not GTFS_ZIP.exists():
        log(f"  downloading {C.GTFS_URL}")
        tmp = GTFS_ZIP.with_suffix(".part")
        with requests.get(C.GTFS_URL, stream=True, timeout=300, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(GTFS_ZIP)
    with zipfile.ZipFile(GTFS_ZIP) as z:
        for name in NEEDED:
            if not (GTFS_DIR / name).exists():
                z.extract(name, GTFS_DIR)


def _csv(name: str, cols: list[str]) -> pd.DataFrame:
    return pd.read_csv(GTFS_DIR / name, usecols=cols, dtype=str, engine="pyarrow")


def service_date(feed_start: str) -> date:
    start = max(date.today(), datetime.strptime(feed_start, "%Y%m%d").date())
    return start + timedelta(days=(1 - start.weekday()) % 7)  # Tuesday


def active_services(day: date) -> set[str]:
    cal = _csv("calendar.txt", ["service_id", "tuesday", "start_date", "end_date"])
    ds = day.strftime("%Y%m%d")
    on = set(cal[(cal.tuesday == "1") & (cal.start_date <= ds) & (cal.end_date >= ds)].service_id)
    cd = _csv("calendar_dates.txt", ["service_id", "date", "exception_type"])
    cd = cd[cd.date == ds]
    on |= set(cd[cd.exception_type == "1"].service_id)
    on -= set(cd[cd.exception_type == "2"].service_id)
    return on


def mode_of(route_type: str, short: str) -> str | None:
    if route_type == "0":
        return "luas"
    if route_type == "2":
        return "dart" if (short or "").upper() == "DART" else "rail"
    return None


def load_gtfs(bbox):
    feed = _csv("feed_info.txt", ["feed_start_date", "feed_end_date"]).iloc[0]
    day = service_date(feed.feed_start_date)
    services = active_services(day)
    routes = _csv("routes.txt", ["route_id", "route_short_name", "route_long_name", "route_type"])
    trips = _csv("trips.txt", ["route_id", "service_id", "trip_id", "shape_id"])
    trips = trips[trips.service_id.isin(services)].merge(routes, on="route_id")

    stops = _csv("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"])
    stops["lat"], stops["lon"] = stops.stop_lat.astype(float), stops.stop_lon.astype(float)
    w, s, e, n = pad_bbox(bbox, STOP_PAD_M)
    stops = stops[(stops.lon >= w) & (stops.lon <= e) & (stops.lat >= s) & (stops.lat <= n)].copy()

    st = _csv("stop_times.txt", ["trip_id", "departure_time", "stop_id"])
    st = st[st.stop_id.isin(set(stops.stop_id)) & st.trip_id.isin(set(trips.trip_id))]
    st = st.merge(trips[["trip_id", "route_id", "route_short_name", "route_type"]], on="trip_id")
    st["peak"] = (st.departure_time >= PEAK[0]) & (st.departure_time < PEAK[1])
    return day, routes, trips, stops, st


def stop_table(stops: pd.DataFrame, st: pd.DataFrame) -> pd.DataFrame:
    bus = st.route_type == "3"
    rail = st.route_type.isin(["0", "2"])
    g = pd.DataFrame({
        "bus_peak": st[bus & st.peak].groupby("stop_id").size(),
        "all_peak": st[st.peak].groupby("stop_id").size(),
        "bus_any": st[bus].groupby("stop_id").size(),
        "rail_any": st[rail].groupby("stop_id").size(),
    }).fillna(0)
    routes = st[bus].groupby("stop_id")["route_short_name"].agg(lambda s: sorted(set(s.dropna())))
    rail_mode = (st[rail].assign(mode=[mode_of(t, s) for t, s in zip(st[rail].route_type, st[rail].route_short_name)])
                 .groupby("stop_id")["mode"].agg(lambda s: s.mode().iat[0]))
    t = stops.set_index("stop_id").join(g, how="inner").join(routes.rename("routes")).join(rail_mode.rename("rail_mode"))
    t["peak_buses_per_hr"] = t.bus_peak / C.AM_PEAK_HOURS
    t["peak_per_hr"] = t.all_peak / C.AM_PEAK_HOURS
    t["x"], t["y"] = _to_m.transform(t.lon.values, t.lat.values)
    return t.reset_index()


def rail_lines(trips: pd.DataFrame, bbox) -> list[dict]:
    rt = trips[trips.route_type.isin(["0", "2"])]
    if not len(rt):
        return []
    # one shape per route: the one most trips use
    best = rt.groupby(["route_id", "shape_id"]).size().reset_index(name="n").sort_values("n").drop_duplicates("route_id", keep="last")
    want = set(best.shape_id)
    shp = pd.read_csv(GTFS_DIR / "shapes.txt", dtype={"shape_id": str}, engine="pyarrow",
                      usecols=["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"])
    shp = shp[shp.shape_id.isin(want)].sort_values(["shape_id", "shape_pt_sequence"])
    clip = box(*[*_to_m.transform(*pad_bbox(bbox, STOP_PAD_M)[:2]), *_to_m.transform(*pad_bbox(bbox, STOP_PAD_M)[2:])])
    meta = rt.drop_duplicates("route_id").set_index("route_id")
    feats, seen = [], set()
    for r in best.itertuples():
        pts = shp[shp.shape_id == r.shape_id]
        if len(pts) < 2:
            continue
        x, y = _to_m.transform(pts.shape_pt_lon.values, pts.shape_pt_lat.values)
        line = LineString(zip(x, y)).intersection(clip)
        if line.is_empty:
            continue
        m = meta.loc[r.route_id]
        mode = mode_of(m.route_type, m.route_short_name)
        name = m.route_long_name if mode == "rail" else f"{m.route_short_name} ({m.route_long_name})"
        key = (mode, frozenset((m.route_long_name or "").split(" - ")))  # A–B and B–A are one line
        if key in seen:
            continue
        seen.add(key)
        feats.append(feature(geom_out(line), {"id": r.route_id, "mode": mode, "name": name}))
    return feats


def nearest(tree: cKDTree, xy: np.ndarray):
    d, i = tree.query(xy)
    return d, i


def run(bbox) -> None:
    devs = read_json(DEVS, [])
    try:
        ensure_gtfs()
        day, routes, trips, stops, st = load_gtfs(bbox)
        table = stop_table(stops, st)
        log(f"  GTFS service day {day}: {len(trips)} trips, {len(table)} stops/stations with service in area")
    except Exception as exc:
        log(f"  GTFS FAILED: {exc}")
        source("gtfs", "failed", 0, str(exc))
        write_json(OUT / "stops.geojson", fc([]))
        write_json(OUT / "rail_lines.geojson", fc([]))
        write_json(OUT / "transport_routes.geojson", fc([]))
        for d in devs:
            d["transport"] = None
        write_json(DEVS, devs)
        return

    w, s, e, n = bbox
    in_box = (table.lon >= w) & (table.lon <= e) & (table.lat >= s) & (table.lat <= n)
    bus_stops = table[(table.bus_any > 0)]
    stop_feats = [feature({"type": "Point", "coordinates": [round(r.lon, 5), round(r.lat, 5)]},
                          {"stop_id": r.stop_id, "name": r.stop_name,
                           "peak_buses_per_hr": round(float(r.peak_buses_per_hr), 1),
                           "routes": r.routes if isinstance(r.routes, list) else []})
                  for r in bus_stops[in_box[bus_stops.index]].itertuples()]
    write_json(OUT / "stops.geojson", fc(stop_feats))
    rails = rail_lines(trips, bbox)
    write_json(OUT / "rail_lines.geojson", fc(rails))
    stations = table[table.rail_any > 0].reset_index(drop=True)
    frequent = table[table.peak_per_hr >= C.FREQ_THRESHOLD].reset_index(drop=True)
    log(f"  stops.geojson: {len(stop_feats)} bus stops; {len(stations)} rail/Luas stops; "
        f"{len(frequent)} frequent (≥{C.FREQ_THRESHOLD}/hr); rail_lines.geojson: {len(rails)}")
    source("gtfs", "ok", len(table), f"service day {day}")

    if not devs:
        write_json(OUT / "transport_routes.geojson", fc([]))
        write_json(DEVS, devs)
        return

    # --- per-development access ---------------------------------------------------
    xy = np.array([_to_m.transform(*d["centroid"]) for d in devs])
    d_any, i_any = nearest(cKDTree(table[["x", "y"]].values), xy)
    d_freq, i_freq = nearest(cKDTree(frequent[["x", "y"]].values), xy) if len(frequent) else (np.full(len(xy), np.inf), None)
    d_rail, i_rail = nearest(cKDTree(stations[["x", "y"]].values), xy) if len(stations) else (np.full(len(xy), np.inf), None)
    for k, d in enumerate(devs):
        near_stop = float(d_any[k])
        near_freq = float(d_freq[k])
        cls = ("served" if near_freq <= C.STOP_WALK_M else
               "weak" if near_stop <= C.STOP_WALK_M else "unserved")
        residents = d["num_units"] * C.HOUSEHOLD_SIZE
        d["transport"] = {
            "class": cls,
            "nearest_stop_m": round(near_stop) if math.isfinite(near_stop) else None,
            "nearest_frequent_stop_m": round(near_freq) if math.isfinite(near_freq) else None,
            "nearest_stop_id": table.stop_id.iat[i_any[k]],
            "nearest_frequent_stop_id": frequent.stop_id.iat[i_freq[k]] if i_freq is not None else None,
            "nearest_rail_name": stations.stop_name.iat[i_rail[k]] if i_rail is not None else None,
            "nearest_rail_m": round(float(d_rail[k])) if math.isfinite(d_rail[k]) else None,
            "residents": round(residents),
            "peak_pt_trips": round(residents * C.PEAK_TRIP_RATE * C.PT_MODE_SHARE, 1),
            "proposed_route_id": None,
        }
    counts = pd.Series([d["transport"]["class"] for d in devs]).value_counts().to_dict()
    log(f"  access: {counts}")

    # --- proposed routes ---------------------------------------------------------------
    cand = [k for k, d in enumerate(devs) if d["transport"]["class"] in ("weak", "unserved")]
    route_feats = []
    if len(cand) >= C.DBSCAN_MIN_DEVS and (len(frequent) or len(stations)):
        labels = DBSCAN(eps=C.DBSCAN_EPS_M, min_samples=C.DBSCAN_MIN_DEVS).fit_predict(xy[cand])
        clusters = {}
        for k, lab in zip(cand, labels):
            if lab >= 0:
                clusters.setdefault(lab, []).append(k)
        small = {lab: m for lab, m in clusters.items()
                 if sum(devs[k]["num_units"] for k in m) < C.MIN_ROUTE_UNITS}
        log(f"  DBSCAN: {len(cand)} weak/unserved developments -> {len(clusters)} clusters "
            f"({int((labels < 0).sum())} unclustered); {len(small)} below MIN_ROUTE_UNITS="
            f"{C.MIN_ROUTE_UNITS} get no route")
        clusters = {lab: m for lab, m in clusters.items() if lab not in small}
        G = ox.load_graphml(PIPE_INTERIM / "graph.graphml")
        H = nx.Graph()
        for u, v, dd in G.edges(data=True):
            if not H.has_edge(u, v) or H.edges[u, v]["length"] > dd["length"]:
                H.add_edge(u, v, length=dd["length"])
        node_ids = np.array(list(H.nodes))
        node_xy = np.array([_to_m.transform(G.nodes[nid]["x"], G.nodes[nid]["y"]) for nid in node_ids])
        node_tree = cKDTree(node_xy)
        targets = pd.concat([frequent[["x", "y", "stop_name"]], stations[["x", "y", "stop_name"]]]).drop_duplicates()
        target_tree = cKDTree(targets[["x", "y"]].values)

        for n_route, members in enumerate(sorted(clusters.values(), key=lambda m: -len(m)), 1):
            cxy = xy[members].mean(axis=0)
            start = node_ids[node_tree.query(cxy)[1]]
            end = node_ids[node_tree.query(targets[["x", "y"]].values[target_tree.query(cxy)[1]])[1]]
            stops_left = {node_ids[node_tree.query(xy[k])[1]] for k in members}
            order, cur = [], cxy
            while stops_left:  # greedy nearest-neighbour from the centroid
                nxt = min(stops_left, key=lambda nid: np.hypot(*(node_xy[np.where(node_ids == nid)[0][0]] - cur)))
                order.append(nxt)
                cur = node_xy[np.where(node_ids == nxt)[0][0]]
                stops_left.discard(nxt)
            path, ok = [start], True
            for a, b in zip([start, *order], [*order, end]):
                try:
                    seg = nx.shortest_path(H, a, b, weight="length")
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    ok = False
                    break
                path += seg[1:]
            if not ok or len(path) < 2:
                log(f"  cluster of {len(members)} developments: no drive-graph path — skipped")
                continue
            length_m = sum(H.edges[a, b]["length"] for a, b in zip(path, path[1:]) if a != b)
            line = LineString([node_xy[np.where(node_ids == nid)[0][0]] for nid in path])
            km = length_m / 1000
            rt = 2 * km / C.BUS_SPEED_KMH * 60 + C.LAYOVER_MIN
            demand = sum(devs[k]["transport"]["peak_pt_trips"] for k in members)
            need = max(1, math.ceil(demand / C.AM_PEAK_HOURS / (C.BUS_CAPACITY * C.BUS_LOAD_FACTOR)))
            headway = min(max(60 / need, C.MIN_HEADWAY), C.MAX_HEADWAY)
            rid = f"R{n_route:03d}"
            serves = sorted(devs[k]["app_id"] for k in members)
            for k in members:
                devs[k]["transport"]["proposed_route_id"] = rid
            route_feats.append(feature(geom_out(line), {
                "route_id": rid, "length_km": round(km, 2), "round_trip_min": round(rt, 1),
                "headway_min": round(headway, 1), "buses_required": math.ceil(rt / headway),
                "serves_app_ids": serves, "peak_demand": round(demand, 1)}))
    write_json(OUT / "transport_routes.geojson", fc(route_feats))
    write_json(DEVS, devs)
    log(f"  transport_routes.geojson: {len(route_feats)} routes, "
        f"{sum(f['properties']['buses_required'] for f in route_feats)} buses")
