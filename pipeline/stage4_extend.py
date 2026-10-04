"""Stage 4: connect developments; route new pipe for unconnected ones via a Steiner tree.

Also enriches all three layers with load and sizing detail:
- developments: planning metadata, census household size, estimated residents, demand
- extensions: dwellings served downstream, peak flow, Code-of-Practice main size
- network: new dwellings/residents whose connection lands on each existing edge
"""
from __future__ import annotations

from collections import defaultdict, deque

import geopandas as gpd
import networkx as nx
import osmnx as ox
from networkx.algorithms.approximation import steiner_tree
from shapely.geometry import LineString, Point

from . import sizing
from .common import round_coords, DATA, INTERIM, METRIC_CRS, WGS84, feature_collection, log, read_json, write_json
from .stage3_network import CENSUS, GRAPH, RANK, _first, edge_id, street_label

IN_DEVS = INTERIM / "extracted.json"
IN_NET = DATA / "network.geojson"
OUT_NET = DATA / "network.geojson"
OUT_DEVS = DATA / "developments.geojson"
OUT_EXT = DATA / "extensions.geojson"
CONNECT_M = 50
SOURCE = "__served_network__"


def _edge_geom(G, u, v, k) -> LineString:
    d = G.edges[u, v, k]
    if "geometry" in d:
        return d["geometry"]
    return LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])


def _street(d: dict):
    return street_label(d.get("name"))


def run(bbox) -> None:
    devs = read_json(IN_DEVS, [])
    net = read_json(IN_NET)
    if net is None or not GRAPH.exists():
        log("  network missing — run stage 3 first")
        return
    G = ox.load_graphml(GRAPH)
    conf = {f["properties"]["edge_id"]: f["properties"]["confidence"] for f in net["features"]}
    served_ids = {eid for eid, c in conf.items() if c != "none"}
    log(f"  {len(devs)} developments, {len(served_ids)}/{len(conf)} served edges")

    census = read_json(CENSUS)
    sa = (gpd.GeoDataFrame.from_features(census["features"], crs=WGS84).to_crs(METRIC_CRS)
          if census and census.get("features") else None)
    if sa is None:
        log("  no census small areas — household size falls back to the CoP 2.7 design figure")

    net_gdf = gpd.GeoDataFrame.from_features(net["features"], crs=WGS84).to_crs(METRIC_CRS)
    served_gdf = net_gdf[net_gdf.edge_id.isin(served_ids)]

    # --- per-development context -----------------------------------------------------
    dev_gdf = gpd.GeoDataFrame(
        devs, geometry=[Point(d["lon"], d["lat"]) for d in devs], crs=WGS84
    ).to_crs(METRIC_CRS) if devs else None
    ctx: dict[str, dict] = {d["app_id"]: {} for d in devs}
    if devs and sa is not None:
        j = gpd.sjoin_nearest(dev_gdf[["app_id", "geometry"]],
                              sa[["small_area_id", "electoral_division", "household_size", "geometry"]],
                              how="left").drop_duplicates("app_id")
        for r in j.itertuples():
            ctx[r.app_id] = {"small_area_id": r.small_area_id, "electoral_division": r.electoral_division,
                             "household_size": r.household_size}
    if devs:
        j = gpd.sjoin_nearest(dev_gdf[["app_id", "geometry"]],
                              net_gdf[["edge_id", "street_name", "geometry"]],
                              how="left").drop_duplicates("app_id")
        for r in j.itertuples():
            ctx[r.app_id]["nearest_street"] = r.street_name
    for d in devs:
        c = ctx[d["app_id"]]
        hs = c.get("household_size")
        if hs is not None and hs != hs:  # NaN: small area with no households
            hs = None
        c["household_size_source"] = "census_2022_small_area" if hs else "cop_design_occupancy"
        c["household_size"] = hs or sizing.OCCUPANCY
        c["est_residents"] = round(d["num_units"] * c["household_size"])

    # --- connected? distance to nearest served edge -------------------------------
    dist: dict[str, float] = {}
    attach: dict[str, str] = {}  # app_id -> served edge its load lands on
    if devs and len(served_gdf):
        j = gpd.sjoin_nearest(dev_gdf[["app_id", "geometry"]], served_gdf[["edge_id", "geometry"]],
                              how="left", distance_col="d").sort_values("d").drop_duplicates("app_id")
        dist = j.set_index("app_id")["d"].to_dict()
        near_edge = j.set_index("app_id")["edge_id"].to_dict()
    connected = {d["app_id"]: dist.get(d["app_id"]) is not None and dist[d["app_id"]] <= CONNECT_M
                 for d in devs}
    for aid, ok in connected.items():
        if ok:
            attach[aid] = near_edge[aid]
    unconnected = [d for d in devs if not connected[d["app_id"]]]
    log(f"  connected {len(devs) - len(unconnected)}, unconnected {len(unconnected)}")

    # --- Steiner tree from a virtual source over the served network ----------------
    H = nx.Graph()
    best_key: dict[tuple, tuple] = {}
    for u, v, k, d in G.edges(keys=True, data=True):
        eid = edge_id(u, v, k)
        w = d["length"]
        if H.has_edge(u, v) and H.edges[u, v]["length"] <= w:
            continue
        H.add_edge(u, v, length=w)
        best_key[tuple(sorted((u, v)))] = (u, v, k, eid)
    served_nodes = {n for (u, v, k, eid) in best_key.values() if eid in served_ids for n in (u, v)}
    for n in served_nodes:
        H.add_edge(SOURCE, n, length=0.0)

    def best_served_edge_at(node) -> str | None:
        cands = [(RANK[conf[eid]], eid) for (u, v, k, eid) in
                 (best_key[tuple(sorted((node, nb)))] for nb in G.neighbors(node))
                 if eid in served_ids]
        return max(cands)[1] if cands else None

    units_by_app = {d["app_id"]: int(d["num_units"]) for d in devs}
    res_by_app = {aid: c["est_residents"] for aid, c in ctx.items()}
    ext_feats: list[dict] = []
    if unconnected and served_nodes:
        nodes_proj = ox.graph_to_gdfs(G, edges=False)[["geometry"]].to_crs(METRIC_CRS)
        un_gdf = dev_gdf[dev_gdf.app_id.isin({d["app_id"] for d in unconnected})]
        near = gpd.sjoin_nearest(un_gdf[["app_id", "geometry"]], nodes_proj, how="left")
        dev_node = near.drop_duplicates("app_id").set_index("app_id")["osmid"].to_dict()

        reachable = nx.node_connected_component(H, SOURCE)
        term_apps: dict = {}
        for aid, node in dev_node.items():
            if node not in reachable:
                log(f"  {aid}: nearest street node is not reachable from the served network")
            elif node in served_nodes:
                log(f"  {aid}: >{CONNECT_M} m from served pipe but its nearest street node is "
                    "already served — no new pipe routed")
                attach[aid] = best_served_edge_at(node)
            else:
                term_apps.setdefault(node, []).append(aid)
        terminals = [SOURCE, *term_apps]
        if len(terminals) > 1:
            T = steiner_tree(H.subgraph(reachable), terminals, weight="length", method="mehlhorn")
            # BFS from the virtual source: served nodes are depth 1.
            depth, parent = {SOURCE: 0}, {SOURCE: None}
            q = deque([SOURCE])
            while q:
                a = q.popleft()
                for b in T.neighbors(a):
                    if b not in depth:
                        depth[b], parent[b] = depth[a] + 1, a
                        q.append(b)
            serves: dict[tuple, set] = {}
            for node, aids in term_apps.items():
                n = node
                while parent[n] is not None and parent[n] != SOURCE:
                    key = tuple(sorted((n, parent[n])))
                    serves.setdefault(key, set()).update(aids)
                    n = parent[n]
                for aid in aids:  # n is now the served node the new pipe hangs off
                    attach[aid] = best_served_edge_at(n)
            for (a, b), aids in serves.items():
                u, v, k, eid = best_key[(a, b)]
                if eid in served_ids:
                    continue  # existing pipe, not new
                near_node = a if depth[a] < depth[b] else b
                d = G.edges[u, v, k]
                units = sum(units_by_app[x] for x in aids)
                ext_feats.append({
                    "type": "Feature",
                    "geometry": round_coords(_edge_geom(G, u, v, k).__geo_interface__),
                    "properties": {
                        "edge_id": eid,
                        "order": depth[near_node] - 1,
                        "length_m": round(float(d["length"]), 1),
                        "serves_app_ids": sorted(aids),
                        "street_name": _street(d),
                        "highway": _first(d.get("highway")),
                        "units_served": units,
                        "est_residents_served": sum(res_by_app[x] for x in aids),
                        "avg_daily_demand_m3": round(sizing.avg_daily_demand_m3(units), 1),
                        **sizing.size_main(units),
                    },
                })
            # Re-index orders densely (0,1,2…) after dropping existing-pipe edges.
            levels = sorted({f["properties"]["order"] for f in ext_feats})
            remap = {o: i for i, o in enumerate(levels)}
            for f in ext_feats:
                f["properties"]["order"] = remap[f["properties"]["order"]]
            ext_feats.sort(key=lambda f: (f["properties"]["order"], f["properties"]["edge_id"]))
    elif unconnected:
        log("  no served edges at all — cannot route extensions")

    # --- network load: new residents landing on each existing edge -------------------
    # (Growth against existing population is reported per census small area in
    # areas.geojson; splitting an area's population across street segments is too
    # arbitrary to compare against at edge level.)
    new_units: dict[str, int] = defaultdict(int)
    new_res: dict[str, int] = defaultdict(int)
    new_apps: dict[str, list] = defaultdict(list)
    for aid, eid in attach.items():
        if eid:
            new_units[eid] += units_by_app[aid]
            new_res[eid] += res_by_app[aid]
            new_apps[eid].append(aid)
    for f in net["features"]:
        p = f["properties"]
        eid = p["edge_id"]
        p["new_units"] = new_units.get(eid, 0)
        p["new_residents_est"] = new_res.get(eid, 0)
        p["new_app_ids"] = sorted(new_apps.get(eid, []))
    write_json(OUT_NET, net)

    by_id = {d["app_id"]: d for d in devs}
    dev_feats = []
    for d in devs:
        aid = d["app_id"]
        c = ctx[aid]
        dev_feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": {
                "app_id": aid,
                "num_units": int(d["num_units"]),
                "dev_type": d["dev_type"],
                "status": d["status"],
                "description": d["description"],
                "connected": bool(connected[aid]),
                "distance_to_network_m": (round(float(dist[aid]), 1) if dist.get(aid) is not None else None),
                "address": d.get("address"),
                "authority": d.get("authority"),
                "application_number": d.get("application_number"),
                "received_date": d.get("received_date"),
                "decision_date": d.get("decision_date"),
                "decision": d.get("decision"),
                "planning_url": d.get("planning_url"),
                "nearest_street": c.get("nearest_street"),
                "small_area_id": c.get("small_area_id"),
                "electoral_division": c.get("electoral_division"),
                "household_size": c["household_size"],
                "household_size_source": c["household_size_source"],
                "est_residents": c["est_residents"],
                "avg_daily_demand_m3": round(sizing.avg_daily_demand_m3(by_id[aid]["num_units"]), 1),
                "peak_flow_lps": round(sizing.peak_flow_lps(by_id[aid]["num_units"]), 2),
                "required_main_mm": sizing.size_main(int(d["num_units"]))["nominal_bore_mm"],
                "connects_via_edge_id": attach.get(aid),
            },
        })
    write_json(OUT_DEVS, feature_collection(dev_feats))
    write_json(OUT_EXT, feature_collection(ext_feats))
    km = sum(f["properties"]["length_m"] for f in ext_feats) / 1000
    loaded = sum(1 for f in net["features"] if f["properties"]["new_units"])
    log(f"  developments.geojson: {len(dev_feats)}; extensions.geojson: {len(ext_feats)} edges, "
        f"{km:.2f} km new pipe; {loaded} existing edges take new load")
