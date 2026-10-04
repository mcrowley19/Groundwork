"""Serve the dashboard and the pipeline outputs.

    .venv/bin/python server.py                 # http://127.0.0.1:8000, real outputs in data/
    MOCK=1 .venv/bin/python server.py          # mocks: data/impact/mock/ if present, else data/mock/
    .venv/bin/python server.py --data some/dir --port 8080

GET  /                 frontend/index.html
GET  /data/<file>      public outputs only (never data/cache or data/interim); default dir is data/impact/
                       when the impact pipeline has run, else data/
GET  /api/status       which files exist, per-source counts and errors, data directory
POST /api/ask          {question, context?} -> {answer, highlight_app_ids, highlight_route_ids, engine}
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline.common import DATA, INTERIM, MOCK, ROOT, env, read_json

FRONTEND = ROOT / "frontend" / "index.html"
PUBLIC_FILES = {"developments.geojson", "extensions.geojson", "network.geojson", "stops.geojson", "rail_lines.geojson",
                "transport_routes.geojson", "schools.geojson", "assumptions.json", "summary.json", "areas.geojson",
                "assets.geojson"}


IMPACT = Path(os.environ.get("IMPACT_OUT") or DATA / "impact")


def data_dir() -> Path:
    """DATA_DIR / --data wins; MOCK=1 picks the impact mocks (then data/mock); otherwise the impact
    outputs when they exist, else the water-pipeline outputs in data/."""
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"]).resolve()
    if (env("MOCK") or "").lower() in ("1", "true", "yes"):
        return ((IMPACT / "mock") if (IMPACT / "mock" / "developments.geojson").exists() else MOCK).resolve()
    return (IMPACT if (IMPACT / "developments.geojson").exists() else DATA).resolve()


app = FastAPI(title="Infrastructure Impact Monitor — Dublin region (prototype)")


@app.get("/")
def index():
    return FileResponse(FRONTEND, headers={"Cache-Control": "no-store"})


@app.get("/data/{name}")
def data_file(name: str):
    if name not in PUBLIC_FILES:
        raise HTTPException(404)
    path = data_dir() / name
    if not path.exists():
        raise HTTPException(404, f"{name} has not been generated")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    d = data_dir()
    files = {n: (d / n).exists() for n in sorted(PUBLIC_FILES)}
    sources = read_json(INTERIM / "sources.json", {}) if d == DATA.resolve() else {}
    summary = read_json(d / "summary.json", {}) or {}
    return {"data_dir": str(d), "mock": d.name == "mock", "files": files, "sources": sources,
            "generated_at": summary.get("generated_at"), "llm_configured": bool(env("OPENAI_API_KEY"))}


# --- analyst query -------------------------------------------------------------

class Ask(BaseModel):
    question: str
    context: dict | None = None   # the client's current scenario table (developments + routes)


def _local_answer(q: str, ctx: dict) -> tuple[str, list[str], list[str]]:
    """Deterministic answers from the client's scenario table when no LLM key is set."""
    ql = q.lower()
    devs: list[dict] = list(ctx.get("developments") or [])
    routes: list[dict] = list(ctx.get("routes") or [])
    scen = ctx.get("scenario") or {}
    scen_txt = f"{scen.get('mode', 'projected')} scenario, pending approval rate {scen.get('pending_rate', 100)}%"
    if not devs:
        return "No developments are loaded, so there is nothing to query.", [], []
    fmt = lambda d: f"{d['app_id']} ({d['num_units']:,} units)"  # noqa: E731
    m = re.search(r"top\s+(\d+)", ql)
    n = int(m.group(1)) if m else 5

    named = [d for d in devs if d["app_id"].lower() in ql]
    if named:
        d = named[0]
        return (f"{d['app_id']}: {d['num_units']:,} units, {d['status']}, overall score {d['overall']:.0f}/100 "
                f"({d['overall_status']}). Water {d['water_score'] if d['water_score'] is not None else '—'}, transport "
                f"{d['transport_score']:.0f} ({d['transport_class']}), schools {d['schools_score']:.0f} "
                f"({d['schools_flag']}). {scen_txt}."), [d["app_id"]], ([d["route_id"]] if d.get("route_id") else [])
    rnamed = [r for r in routes if r["route_id"].lower() in ql]
    if rnamed:
        r = rnamed[0]
        return (f"{r['route_id']}: {r['length_km']:.1f} km, {r['round_trip_min']} min round trip at {r['headway_min']} min "
                f"headway needs {r['buses_required']} buses; serves {', '.join(r['serves_app_ids'])} "
                f"({r['peak_demand']:,} peak trips)."), list(r["serves_app_ids"]), [r["route_id"]]

    system = "water" if re.search(r"water|pipe|main|connect", ql) else "transport" if re.search(r"transport|bus|rail|luas|stop|route", ql) \
        else "schools" if re.search(r"school|pupil|class", ql) else "overall"
    key = {"water": "water_score", "transport": "transport_score", "schools": "schools_score", "overall": "overall"}[system]
    active = [d for d in devs if d.get("weight", 1) > 0]
    if re.search(r"critical|worst|most|highest|top|priorit|pressure", ql):
        ranked = sorted([d for d in active if d.get(key) is not None], key=lambda d: -d[key])[:n]
        crit = [d for d in active if d["overall_status"] == "critical"]
        lead = f"{len(crit)} development(s) are CRITICAL overall under the {scen_txt}. "
        body = "; ".join(f"{fmt(d)} {system} {d[key]:.0f}" for d in ranked)
        return lead + f"Highest {system} pressure: {body}.", [d["app_id"] for d in ranked], \
            [d["route_id"] for d in ranked if d.get("route_id")]
    if re.search(r"bus|route", ql):
        total = sum(r["buses_required"] for r in routes)
        body = "; ".join(f"{r['route_id']} {r['buses_required']} buses ({r['length_km']:.1f} km)" for r in routes[:n])
        return (f"{len(routes)} proposed routes need {total} buses in total. {body}."), \
            [a for r in routes[:n] for a in r["serves_app_ids"]], [r["route_id"] for r in routes[:n]]
    if re.search(r"cost|capital|€|euro", ql):
        ranked = sorted(active, key=lambda d: -(d.get("water_cost") or 0))[:n]
        total = sum((d.get("water_cost") or 0) * d.get("weight", 1) for d in active)
        body = "; ".join(f"{d['app_id']} €{d['water_cost']:,.0f}" for d in ranked if d.get("water_cost"))
        return f"Estimated water extension cost is €{total:,.0f} ({scen_txt}). Largest: {body or 'none'}.", \
            [d["app_id"] for d in ranked if d.get("water_cost")], []
    if re.search(r"school|pupil", ql):
        ranked = sorted(active, key=lambda d: -d["schools_score"])[:n]
        pp = sum(d["primary_pupils"] * d.get("weight", 1) for d in active)
        sp = sum(d["secondary_pupils"] * d.get("weight", 1) for d in active)
        body = "; ".join(f"{d['app_id']} {d['schools_flag']}" for d in ranked)
        return f"≈{pp:,.0f} primary and {sp:,.0f} secondary pupils from new homes ({scen_txt}). Most pressure: {body}.", \
            [d["app_id"] for d in ranked], []
    if "pending" in ql or "granted" in ql:
        st = "pending" if "pending" in ql else "granted"
        sel = sorted([d for d in devs if d["status"] == st], key=lambda d: -d["num_units"])
        return f"{len(sel)} {st} developments, {sum(d['num_units'] for d in sel):,} units. Largest: " + \
            "; ".join(fmt(d) for d in sel[:n]) + ".", [d["app_id"] for d in sel[:n]], []
    units = sum(d["num_units"] * d.get("weight", 1) for d in devs)
    crit = sum(1 for d in active if d["overall_status"] == "critical")
    return (f"{len(devs)} developments loaded, {units:,.0f} effective units, {crit} critical ({scen_txt}). "
            "Try: 'top 5 critical', 'which sites need a bus route', 'water cost', 'school pressure', or an application id."), [], []


def _llm_answer(q: str, ctx: dict) -> tuple[str, list[str], list[str]]:
    from openai import OpenAI

    client = OpenAI(api_key=env("OPENAI_API_KEY"), max_retries=2)
    model = env("OPENAI_MODEL") or "gpt-4o-mini"
    schema = {"name": "analyst_answer", "strict": True,
              "schema": {"type": "object", "additionalProperties": False,
                         "properties": {"answer": {"type": "string"},
                                        "highlight_app_ids": {"type": "array", "items": {"type": "string"}},
                                        "highlight_route_ids": {"type": "array", "items": {"type": "string"}}},
                         "required": ["answer", "highlight_app_ids", "highlight_route_ids"]}}
    system = ("You are an analyst on an infrastructure impact desk for the Dublin region. Answer in at most three "
              "sentences from the JSON supplied only, with readable numbers. Water network is INFERRED from public data; "
              "transport and school figures are prototype estimates. Return the ids of the developments and proposed "
              "routes your answer is about.")
    resp = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps({"question": q, **ctx}, ensure_ascii=False)}],
        response_format={"type": "json_schema", "json_schema": schema})
    out = json.loads(resp.choices[0].message.content)
    apps = {d["app_id"] for d in ctx.get("developments") or []}
    routes = {r["route_id"] for r in ctx.get("routes") or []}
    return out["answer"], [i for i in out["highlight_app_ids"] if i in apps], [i for i in out["highlight_route_ids"] if i in routes]


def _server_context(top_n: int = 30) -> dict:
    """LLM context built from the served files, for callers that send none: summary.json,
    the top developments by overall score and every proposed route (the key stays server-side)."""
    d = data_dir()
    devs = (read_json(d / "developments.geojson", {}) or {}).get("features", [])
    devs = sorted((f["properties"] for f in devs), key=lambda p: -((p.get("scores") or {}).get("overall") or 0))
    trim = lambda p: {**p, "description": (p.get("description") or "")[:300]}  # noqa: E731
    routes = (read_json(d / "transport_routes.geojson", {}) or {}).get("features", [])
    return {"summary": read_json(d / "summary.json", {}), "developments": [trim(p) for p in devs[:top_n]],
            "routes": [f["properties"] for f in routes]}


@app.post("/api/ask")
def ask(body: Ask):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "question is empty")
    ctx = body.context or {}
    if env("OPENAI_API_KEY"):
        try:
            answer, apps, routes = _llm_answer(q, ctx if ctx.get("developments") else _server_context())
            return {"answer": answer, "highlight_app_ids": apps, "highlight_route_ids": routes,
                    "engine": env("OPENAI_MODEL") or "gpt-4o-mini"}
        except Exception as exc:  # fall back rather than fail the desk
            answer, apps, routes = _local_answer(q, ctx)
            return {"answer": answer, "highlight_app_ids": apps, "highlight_route_ids": routes, "engine": "local",
                    "warning": f"LLM call failed: {exc}"}
    answer, apps, routes = _local_answer(q, ctx)
    return {"answer": answer, "highlight_app_ids": apps, "highlight_route_ids": routes, "engine": "local"}


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--data", help="directory of pipeline outputs (default data/, or data/mock when MOCK=1)")
    args = p.parse_args()
    if args.data:
        os.environ["DATA_DIR"] = str(Path(args.data).resolve())
    print(f"serving {data_dir()} on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
