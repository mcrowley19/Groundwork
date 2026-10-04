"""Stage 2: structured extraction from planning descriptions with the OpenAI API."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import CACHE, INTERIM, env, log, read_json, write_json

CACHE_FILE = CACHE / "llm.json"
IN = INTERIM / "planning.json"
OUT = INTERIM / "extracted.json"
WORKERS = int(env("LLM_WORKERS") or 16)
# Account-level errors: every remaining call would fail the same way.
FATAL_CODES = {"insufficient_quota", "credit_balance_exhausted", "invalid_api_key", "account_deactivated"}

SCHEMA = {
    "name": "planning_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "num_units": {
                "type": ["integer", "null"],
                "description": "Number of NEW residential units (dwellings, apartments, "
                               "student/co-living bedspaces count as units) the application "
                               "creates. null if not stated or not residential.",
            },
            "dev_type": {"type": "string", "enum": ["residential", "commercial", "mixed", "other"]},
            "is_new_build": {
                "type": "boolean",
                "description": "True if it constructs new floorspace/buildings or creates new "
                               "dwellings (incl. change of use to residential). False for "
                               "signage, extensions to a single house, internal alterations, "
                               "retention, minor works.",
            },
            "status": {
                "type": "string",
                "enum": ["granted", "refused", "pending", "withdrawn", "appealed", "invalid", "unknown"],
            },
        },
        "required": ["num_units", "dev_type", "is_new_build", "status"],
    },
}

SYSTEM = (
    "You extract structured facts from Irish planning applications. Use only what the "
    "text states; never guess a unit count that is not written. Derive `status` from the "
    "authority's status/decision fields when given."
)


def _prompt(app: dict) -> str:
    return (
        f"Description: {app['description']}\n"
        f"Application type: {app.get('application_type')}\n"
        f"Authority status: {app.get('application_status')}\n"
        f"Authority decision: {app.get('decision')}"
    )


def _extract(client, model: str, app: dict) -> dict:
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": _prompt(app)}],
        response_format={"type": "json_schema", "json_schema": SCHEMA},
    )
    return json.loads(resp.choices[0].message.content)


def run(bbox) -> list[dict]:
    apps = read_json(IN, [])
    cache: dict = read_json(CACHE_FILE, {})
    todo = [a for a in apps if a["app_id"] not in cache and a["description"]]
    log(f"  {len(apps)} applications, {len(apps) - len(todo)} cached, {len(todo)} to extract")

    key = env("OPENAI_API_KEY")
    if todo and not key:
        log("  OPENAI_API_KEY missing — skipping LLM calls; uncached applications are dropped "
            "(no extraction is invented)")
    elif todo:
        from openai import OpenAI

        client = OpenAI(api_key=key, max_retries=4)
        model = env("OPENAI_MODEL") or "gpt-4o-mini"
        lock = threading.Lock()
        failed = 0
        with ThreadPoolExecutor(WORKERS) as pool:
            futs = {pool.submit(_extract, client, model, a): a for a in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                a = futs[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    failed += 1
                    if getattr(exc, "code", None) in FATAL_CODES:
                        log(f"  OpenAI refused the account ({exc.code}) — stopping; "
                            "everything extracted so far is cached")
                        for f in futs:
                            f.cancel()
                        break
                    log(f"  extraction failed for {a['app_id']}: {exc}")
                    continue
                with lock:
                    cache[a["app_id"]] = {**result, "model": model}
                    if i % 25 == 0:
                        write_json(CACHE_FILE, cache)
        write_json(CACHE_FILE, cache)
        done = sum(1 for a in todo if a["app_id"] in cache)
        log(f"  extracted {done}, failed {failed}, not attempted {len(todo) - done - failed} (model {model})")

    kept = []
    for a in apps:
        x = cache.get(a["app_id"])
        if not x:
            continue
        if x["dev_type"] in ("residential", "mixed") and x["is_new_build"] and (x["num_units"] or 0) > 0:
            kept.append({**a, "num_units": int(x["num_units"]), "dev_type": x["dev_type"],
                         "status": x["status"]})
    log(f"  kept {len(kept)} residential/mixed new builds with units "
        f"({sum(k['num_units'] for k in kept)} units)")
    write_json(OUT, kept)
    return kept
