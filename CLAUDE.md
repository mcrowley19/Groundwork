# Will the pipes cope?

A map of Dublin's planned new homes set against an *estimated* water-main network, showing which developments look unserved and how much new pipe would reach them.

There are no real Uisce Éireann pipe records. The network is inferred from public evidence: hydrants, manholes and buildings near streets. The UI says this, and any copy you write should too.

## Shape of the repo

- `run.py`: CLI entry point. Runs the five pipeline stages in order, or one at a time.
- `pipeline/`: Python 3.12 backend. Each `stageN_*.py` exposes `run(bbox)`.
  - `common.py`: paths, `DEFAULT_BBOX`, CRS constants, `log`/`stage`, JSON helpers, `record_source`.
  - `stage1_planning.py`: NPAD FeatureServer → planning applications from the last 3 years inside the bbox.
  - `stage2_llm.py`: OpenAI structured extraction of `dev_type`, `is_new_build` and `num_units`. Results are cached in `data/cache/llm.json`.
  - `stage3_network.py`: osmnx street graph. Each edge gets a confidence of high/medium/low/none from OSM and Mapillary evidence.
  - `stage4_extend.py`: works out which developments are connected (within 50 m of a served edge). For the rest, a Steiner tree over a virtual source routes the extension pipes.
  - `stage5_summary.py`: writes `summary.json` with headline numbers and top areas by suburb, and writes the mocks if they're missing.
- `data/`: pipeline outputs, which are the contract with the frontend. `data/cache/` and `data/interim/` are gitignored.
- `server.py`: FastAPI server for the dashboard. Serves `frontend/index.html`, the public files in the data directory (`MOCK=1` switches to `data/mock/`), `/api/status` and `POST /api/ask` (`codex exec` when `LLM_BACKEND=codex`, else OpenAI when `OPENAI_API_KEY` is set, else a local rules engine; the LLM gets a compacted context, not all ~1.2k rows).
- `frontend/index.html`: the Infrastructure Impact Monitor, one file with inline CSS/JS, libraries from pinned CDNs, no build step. Water, transport and schools impact per development; scenario recompute is client-side.
- `pipeline/mock_infra.py`: writes the full mock dataset (developments with water/transport/schools, stops, rail, routes, schools, assumptions) to `data/mock/` from the planning register, the local OSM extract and the real inferred network. The pipeline itself does not produce transport or schools data yet.

`README.md` documents each stage's algorithm and thresholds in detail, plus the extra files the frontend reads.

## Data contract (all EPSG:4326)

| File | Geometry | Key properties |
|---|---|---|
| `network.geojson` | LineString | `edge_id`, `confidence` (high/medium/low/none), `evidence[]`, `length_m` |
| `developments.geojson` | Point | `app_id` (e.g. `DCC-3031/24`), `num_units`, `dev_type`, `status`, `description`, `connected`, `distance_to_network_m` |
| `extensions.geojson` | LineString | `edge_id`, `order` (BFS depth from the existing network, which drives the animation), `length_m`, `serves_app_ids[]` |
| `summary.json` | n/a | `total_units`, `unconnected_units`, `new_pipe_km`, `top_areas[]`, `sources{}`, `generated_at` |

If you change any of these fields, update `data/mock/*` in the same change. The frontend also accepts the richer mock shape (Polygon developments with `water`/`transport`/`schools`/`scores` objects, plus `stops`, `rail_lines`, `transport_routes`, `schools` and `assumptions.json`); that contract is tabulated in `README.md` under "Frontend".

## Commands

```bash
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env    # OPENAI_API_KEY required; OPENAI_MODEL (default gpt-4o-mini) and MAPILLARY_TOKEN optional

.venv/bin/python run.py all --bbox=-6.2560,53.3460,-6.2440,53.3500
.venv/bin/python run.py network            # one stage; reuses the last bbox from data/interim/run.json

.venv/bin/python server.py                 # dashboard on http://127.0.0.1:8000 over data/
MOCK=1 .venv/bin/python server.py          # dashboard over data/mock/
.venv/bin/python -m pipeline.mock_infra    # regenerate data/mock/ (needs data/cache/osm/dublin.osm.pbf)
```

## Conventions and gotchas

- Always write `--bbox=W,S,E,N` with the `=`. Without it, argparse reads a negative longitude as a flag.
- Do distance work in `METRIC_CRS` (EPSG:2157, Irish Transverse Mercator) and write outputs in WGS84.
- A failing external source must not stop the run. Log it, call `record_source(key, None, error)`, and carry on with no data from that source.
- Write files through `write_json` so they're replaced atomically.
- LLM results are cached per `app_id`, and failed calls aren't cached so the next run retries them. Delete `data/cache/llm.json` to force a full re-extraction.
- There are no tests yet. To check a change, run the stage and look at the output files.
