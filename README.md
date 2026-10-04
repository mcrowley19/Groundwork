# Will the pipes cope? — backend

Python pipeline that writes static files for the frontend:

| File | What |
|---|---|
| `data/network.geojson` | street edges with inferred water-main confidence |
| `data/developments.geojson` | residential/mixed new-build planning applications |
| `data/extensions.geojson` | new pipe needed to reach unconnected developments |
| `data/summary.json` | headline numbers |
| `data/assets.geojson` | **new** — every water asset found (hydrants, pipelines, manholes, pumping stations, fountains…) |
| `data/areas.geojson` | **new** — Census 2022 small areas with existing households vs planned new homes |
| `data/mock/*` | hand-made versions of all four, written on first run if `data/mock/` is absent |

All coordinates are EPSG:4326.

## Setup

```bash
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env   # add OPENAI_API_KEY; MAPILLARY_TOKEN is optional
```

## Run

`--bbox W,S,E,N` (EPSG:4326). If you leave it out, the pipeline reuses the bbox that stage 1 last ran with. On a fresh checkout that's the Custom House Quay test area.

```bash
.venv/bin/python run.py planning --bbox=-6.2560,53.3460,-6.2440,53.3500   # 1 planning applications
.venv/bin/python run.py llm        # 2 OpenAI extraction (cached in data/cache/llm.json)
.venv/bin/python run.py context    # 2b Census 2022 small areas + Uisce Éireann registers
.venv/bin/python run.py network    # 3 street graph + hydrant/manhole/building evidence
.venv/bin/python run.py extend     # 4 connected? + Steiner-tree extensions
.venv/bin/python run.py summary    # 5 summary.json (+ mocks)

.venv/bin/python run.py all --bbox=-6.2560,53.3460,-6.2440,53.3500       # everything
.venv/bin/python run.py all --area dublin                                 # all of County Dublin
```

`--area` presets: `test` (Custom House Quay), `docklands`, `city` (city and inner suburbs) and `dublin` (County Dublin).

OpenStreetMap is read from a local Geofabrik extract, not Overpass. The first run downloads `ireland-and-northern-ireland-latest.osm.pbf` (~400 MB) to `data/cache/osm/` and cuts a County Dublin extract from it (~3 min, once). Delete both files to refresh OSM. A county run needs about 5 GB of RAM. To use live Overpass instead, set `OSM_SOURCE=overpass` (optionally with `OVERPASS_URL`); the public servers were failing (406s and timeouts) on 2026-10-04.

Stage 2 makes one OpenAI call per uncached application, about 25k for County Dublin over 3 years. `LLM_WORKERS` (default 16) sets the concurrency.

Use `--bbox=` with an `=` sign. Otherwise argparse reads the leading `-` of a western longitude as a flag.

## How it works

1. **Planning**: queries the NPAD FeatureServer with a server-side envelope filter on the bbox plus `ReceivedDate` in the last 3 years, paging 1000 rows at a time. Point geometry is requested in EPSG:2157 and reprojected to 4326. The `ITMEasting/ITMNorthing` columns are null for Dublin City rows, so they're only a fallback. `app_id` = authority initials + application number, e.g. `DCC-3031/24`.
2. **LLM**: 8 concurrent `chat.completions` calls with a strict JSON schema. `OPENAI_MODEL` defaults to `gpt-4o-mini`. Results are cached per `app_id`; failed calls aren't cached, so the next run retries them. Keeps `dev_type ∈ {residential, mixed}`, `is_new_build`, `num_units > 0`.
3. **Network**: osmnx `drive` graph for the bbox padded by 250 m, made undirected. Point evidence snaps to the nearest edge within 30 m. Mapped pipelines credit edges that run within 15 m of them for at least half their length. DCC gully records credit streets by name inside the DCC boundary. An edge's confidence is the best evidence it has, otherwise none (levels are listed below). Also writes `assets.geojson`.
4. **Extend**: a development is connected if it's within 50 m of a served (non-`none`) edge. For the rest, every served node is joined to one virtual source at zero cost. Then `steiner_tree(method="mehlhorn")` runs over the source plus each development's nearest node, weighted by length. Edges are ordered by BFS depth from the source (0 = touching the existing network). `serves_app_ids` lists the developments whose tree path uses that edge.
5. **Summary**: groups developments by the nearest OSM `place=suburb|neighbourhood|quarter`, searched within 2 km of the bbox. Each extension edge's length is split evenly across the developments it serves.

If a source fails, it's logged and recorded in `data/interim/sources.json`, and the run carries on with nothing from that source.

## Data sources

| Source | Used for |
|---|---|
| [NPAD planning applications](https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/IrishPlanningApplications/FeatureServer/0) | developments |
| OpenStreetMap (Overpass via osmnx) | streets, buildings, hydrants (with `fire_hydrant:type`, `water_source`, diameter), water/sewer pipelines, manholes, pumping stations, water towers, fountains, place names |
| Mapillary v4 map features (optional token) | hydrant and manhole detections |
| [DCC gully cleaning programme 2004–11](https://data.smartdublin.ie/dataset/drainage-gully-cleaning-programme) | per-street drainage evidence (matched by street name, Dublin City Council area only) |
| [CSO Census 2022 SAPS, Small Areas](https://services-eu1.arcgis.com/BuS9rtTsYEV5C0xh/arcgis/rest/services/CensusHub2022_T5_2_SA/FeatureServer/0) | existing population, households, dwellings, local household size |
| [Uisce Éireann open data](https://www.water.ie/open-data) (CC BY 4.0) + capacity registers | Ringsend WWTP capacity, Dublin asset counts, supply/wastewater capacity status |
| [Uisce Éireann Code of Practice IW-CDS-5020-03 Rev 2](https://www.water.ie/sites/default/files/docs/connections/faqs/Water-Code-of-Practice.pdf) | demand and main-sizing rules (`pipeline/sizing.py`) |

## Evidence levels

high: OSM/Mapillary hydrant, or an OSM water pipeline along the street. medium: OSM sewer pipeline, OSM manhole, drinking fountain, Mapillary manhole, or DCC gully records for the street. low: OSM building within 25 m.

## Fields beyond the original contract

These are additive, so the original contract fields are unchanged.

- **network**: `street_name`, `highway`, `evidence_counts`, `new_units`, `new_residents_est`, `new_app_ids` (developments whose connection lands on this edge).
- **developments**: `address`, `authority`, `application_number`, `received_date`, `decision_date`, `decision`, `planning_url` (null where NPAD has none, e.g. DCC), `nearest_street`, `small_area_id`, `electoral_division`, `household_size` (+ `household_size_source`), `est_residents`, `avg_daily_demand_m3`, `peak_flow_lps`, `required_main_mm`, `connects_via_edge_id`.
- **extensions**: `street_name`, `highway`, `units_served` (all dwellings downstream of this edge), `est_residents_served`, `avg_daily_demand_m3`, `peak_flow_lps`, `nominal_bore_mm`, `pe_outside_diameter_mm`, `peak_velocity_mps`, `sizing_basis`.
- **summary**: `sources` has extra counts; plus `est_new_residents`, `new_avg_daily_demand_m3`, `new_peak_flow_lps`, `new_pipe_km_by_bore_mm`, `largest_new_main_mm`, `existing` (2022 population/households in the bbox, household growth %), `assets_by_kind`, `context` (Uisce Éireann), `assumptions` (sizing constants with references). `top_areas[]` gains `est_residents`.

Pipe sizes and flows are **Code-of-Practice guidance estimates** for the dwellings served (150 L/person/day × 2.7 × 1.25 × 5 peak; typical-main-size table, 100 mm public-main floor; above 700 dwellings, the smallest standard bore at ≤ 0.9 m/s). They are not surveyed pipe sizes. Resident estimates use the census household size of the development's small area.

## Frontend: Infrastructure Impact Monitor

`frontend/index.html` is a single-file operations dashboard (MapLibre GL + deck.gl + D3 from pinned CDNs, no build step) served by `server.py`:

```bash
uv pip install --python .venv/bin/python -r requirements.txt   # adds fastapi, uvicorn, pyrosm
.venv/bin/python server.py                 # real outputs in data/  → http://127.0.0.1:8000
MOCK=1 .venv/bin/python server.py          # the mock dataset in data/mock/
.venv/bin/python server.py --data some/dir --port 8080
```

Routes: `GET /` (the page), `GET /data/<file>` (public outputs only), `GET /api/status` (which files exist, source errors), `POST /api/ask {question, context}` → `{answer, highlight_app_ids, highlight_route_ids, engine}`. The analyst query uses OpenAI when `OPENAI_API_KEY` is set and otherwise a local rules engine over the table the page sends with the question, so it works offline.

The page shows every development's **water**, **transport** and **schools** impact, with a scenario slider (pending approval rate, baseline/projected) recomputed client-side. Keyboard: `1–4` system, `/` query, `Esc` clear, `R` reset camera, arrows move through the table.

### Extra data files the frontend reads

Beyond the four pipeline outputs above, the page looks for these in the data directory and degrades to empty states (and a red source dot in the top bar) when they are absent:

| File | Geometry | Key properties |
|---|---|---|
| `developments.geojson` | Polygon (a Point is also accepted; a square footprint is generated) | plus `centroid`, `footprint_source`, `water{connected, distance_to_network_m, route_length_m, est_cost_eur, required_main_mm}`, `transport{class, nearest_stop_m, nearest_stop_id, nearest_frequent_stop_m, nearest_frequent_stop_id, nearest_rail_name, nearest_rail_m, residents, peak_pt_trips, proposed_route_id}`, `schools{primary_pupils, secondary_pupils, nearby_primary, nearby_secondary, nearby_school_ids, worst_pressure_ratio, pressure_flag}`, `scores{water, transport, schools, overall}` (0–100, higher = more pressure; watch ≥ 35, critical ≥ 65) |
| `stops.geojson` | Point | `stop_id`, `name`, `peak_buses_per_hr`, `routes[]` |
| `rail_lines.geojson` | LineString / Point | `id`, `mode` (`rail`, `luas`, `station`), `name` |
| `transport_routes.geojson` | LineString | `route_id`, `length_km`, `round_trip_min`, `headway_min`, `buses_required`, `serves_app_ids[]`, `peak_demand`, `to_station`, `path_source` |
| `schools.geojson` | Point | `school_id`, `name`, `level`, `enrolment`, `projected_new_pupils`, `pressure_ratio` |
| `assumptions.json` | n/a | `[{key, label, value, unit, source_note}]`, shown in the Assumptions drawer and behind every `≈` marker |
| `summary.json` | n/a | additionally `planned_units`, `residents`, `new_peak_pt_trips`, `proposed_routes`, `buses_required`, `new_primary_pupils`, `new_secondary_pupils`, `water_extension_km`, `water_cost_eur`, `by_status`, `sources{planning, network, osm_stops, osm_rail, osm_schools, mapillary}` |

The real files come from the impact pipeline (next section) and land in `data/impact/`, which `server.py` prefers when it exists. `pipeline/mock_infra.py` writes an older, partly synthetic mock set to `data/mock/`:

```bash
.venv/bin/python -m pipeline.mock_infra
```

It takes the largest residential applications from `data/interim/planning.json` (unit counts parsed from the description text, so approximate), measures water distance against the real `data/network.geojson`, pulls bus stops, stations, rail lines and schools from the local OSM extract in `data/cache/osm/dublin.osm.pbf`, routes proposed bus services along `data/interim/graph.graphml`, and applies the assumptions listed in `assumptions.json`. Bus frequencies and school enrolments are synthetic. Footprints are rectangles sized from the unit count. (The impact pipeline uses NPAD's real site boundaries from `FeatureServer/1` instead.) The real `network.geojson` can be tens of MB at county scale, so the page only fetches it when the layer is switched on.

## Infrastructure Impact pipeline (`impact/`, outputs in `data/impact/`)

This pipeline estimates the water, transport and schools infrastructure each granted or pending development will need. It reuses the water pipeline above (planning fetch, LLM extraction, inferred network, extension routing) and adds three new layers on top. Every estimated number comes from a named entry in `impact/config.py`, which is written out verbatim as `assumptions.json`. Values with no citation say `TODO: cite`.

```bash
.venv/bin/python run_impact.py mock                    # 0  hand-made mocks -> data/impact/mock/
.venv/bin/python run_impact.py water --area dublin     # reuse: planning, LLM, context, network, extend
.venv/bin/python run_impact.py footprints              # 1  NPAD site polygons + water results
.venv/bin/python run_impact.py transport               # 2  GTFS access, demand, DBSCAN routes
.venv/bin/python run_impact.py schools                 # 3  DoE schools, pupils, pressure
.venv/bin/python run_impact.py scores                  # 4  scores, summary.json, assumptions.json, size check
.venv/bin/python run_impact.py all --area dublin       # everything (water + 1–4)
.venv/bin/python -m impact.validate data/impact        # check the outputs against the data contract

.venv/bin/python server.py                             # serves data/impact/ (falls back to data/)
MOCK=1 .venv/bin/python server.py                      # serves data/impact/mock/ at the same paths
```

The impact stages reuse the last bbox from `data/interim/run.json`, so pass `--area`/`--bbox` only to `water` or `all`.

| Stage | Source | Notes |
|---|---|---|
| 1 Footprints | NPAD `FeatureServer/1` (site polygons, requested in EPSG:2157) | Joined to the LLM extraction by application ID. With no polygon, a `FOOTPRINT_SQUARE_M` square goes around the point (`footprint_source: "buffered_point"`). A multi-part site keeps its largest part (`footprint_parts` gives the count). Status: `STATUS_MAP` keeps granted, pending and appealed (as pending). `route_length_m` is the length of routed extension edges serving the site, or the direct lateral when routing added none. |
| 2 Transport | NTA GTFS `GTFS_All.zip` (URL from data.gov.ie `nta-gtfs`) | AM-peak (07:00–10:00) departures on one weekday: the first Tuesday on or after both today and the feed start. A stop or station is frequent at ≥ `FREQ_THRESHOLD`/hr. Classes: served (frequent within 400 m), weak (any stop within 400 m), unserved. Routes: DBSCAN (600 m) over weak/unserved sites; clusters under `MIN_ROUTE_UNITS` get none. Each route is routed on the drive graph from the cluster centroid through every site to the nearest frequent stop or station. |
| 3 Schools | DoE *Data on Individual Schools* 2025/26 (coordinates + enrolment) | Pupils = units × per-dwelling rates derived from the DoE 12% primary planning standard and the 8.34% post-primary share. A development's pressure ratio is its pupils ÷ the combined enrolment of nearby schools (2 km / 5 km), worst level. A school's ratio is the new pupils allocated to it ÷ its enrolment. This is a proxy, because capacity isn't published. |
| 4 Scores | — | 0–100, higher = more need. Water: route length vs `WATER_SCORE_FULL_ROUTE_M`. Transport: distance to a frequent stop vs `TRANSPORT_SCORE_FULL_M`. Schools: worst ratio vs `SCHOOL_SCORE_FULL_RATIO`. Overall: `SCORE_WEIGHTS`. To keep under ~15 MB, `network.geojson` holds edges within `NETWORK_CONTEXT_M` of a development (all edges when there are none), with compacted evidence codes. |

`summary.sources` reports `{status: ok|partial|failed, records}` for `planning_points`, `planning_polygons`, `llm_extraction`, `water_network`, `gtfs`, `schools_primary` and `schools_post_primary`. A failed source is logged and the run carries on with empty data for it.

Developments also carry the fields the dashboard uses beyond the contract: `address`, `authority`, `planning_url`, `transport.nearest_stop_id`/`nearest_frequent_stop_id`, `schools.worst_pressure_ratio`, `water.avg_daily_demand_m3`/`required_main_mm`.
