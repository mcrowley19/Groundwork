"""Stage 2: structured extraction from planning descriptions with an LLM.

Two backends: the OpenAI API (`LLM_BACKEND=openai`, needs `OPENAI_API_KEY`) or the Codex CLI
(`LLM_BACKEND=codex`, bills the ChatGPT account `codex login` is signed in with). Only
applications whose description could describe new homes are sent (see `is_candidate`).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .common import CACHE, INTERIM, env, log, read_json, write_json

CACHE_FILE = CACHE / "llm.json"
IN = INTERIM / "planning.json"
OUT = INTERIM / "extracted.json"
WORKERS = int(env("LLM_WORKERS") or 16)
CODEX_BATCH = int(env("CODEX_BATCH") or 25)
CODEX_WORKERS = int(env("CODEX_WORKERS") or 4)
CODEX_TIMEOUT_S = 900
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


# Pre-filter: an application can only yield num_units > 0 if its description mentions homes.
# Tuned for recall (the LLM makes the real call); on County Dublin it keeps ~3k of ~25k.
_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_NOUN = (r"(?:dwellings?|apartments?|houses?|units?|homes?|bed[- ]?spaces?|duplex(?:es)?|bungalows?|"
         r"mews|flats?|studios?|townhouses?|residences)")
_COUNT = re.compile(r"\b" + _NUM + r"(?![- ]?(?:storey|story|bed|person|metre|m\b|sq))\s*"
                    r"(?:no\.?|nos\.?|number)?\s*(?:\(\d+\)\s*)?(?:(?!existing\b)[\w-]+,?\s+){0,4}?"
                    + _NOUN + r"\b", re.I)
_FILL = r"(?:(?!existing\b|extension\b|rear\b|side\b)[\w-]+,?\s+){0,5}?"
_SINGLE = re.compile(r"\b(?:new|construct\w*(?:\s+of)?\s+(?:a|one|1)|erect\w*(?:\s+of)?\s+(?:a|one|1)|"
                     r"(?:a|one|1)\s+(?:new\s+)?(?:detached|semi-detached|infill))\s+" + _FILL
                     + r"(?:dwelling|house|bungalow|mews|home)s?\b", re.I)
_PLURAL = re.compile(r"\b(?:apartments|dwellings|houses|duplexes|bungalows|townhouses|homes|"
                     r"residential units|bed ?spaces|maisonettes)\b", re.I)
_SCHEME = re.compile(r"\bLRD\b|\bSHD\b|large[- ]scale residential|strategic housing|build[- ]to[- ]rent|"
                     r"student accommodation|co-?living|shared accommodation|residential development|"
                     r"housing development|social housing|Part 8", re.I)


def is_candidate(description: str | None) -> bool:
    d = description or ""
    return any(r.search(d) for r in (_SCHEME, _PLURAL, _COUNT, _SINGLE))


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


def _valid(x) -> bool:
    props = SCHEMA["schema"]["properties"]
    return (isinstance(x, dict)
            and (x.get("num_units") is None or (isinstance(x["num_units"], int) and x["num_units"] >= 0))
            and x.get("dev_type") in props["dev_type"]["enum"]
            and isinstance(x.get("is_new_build"), bool)
            and x.get("status") in props["status"]["enum"])


class CodexAuthError(RuntimeError):
    pass


def _codex_batch(model: str | None, batch: list[dict]) -> dict[str, dict]:
    """One `codex exec` call for a batch of applications; returns {app_id: extraction}."""
    item = {**SCHEMA["schema"], "required": ["app_id", *SCHEMA["schema"]["required"]],
            "properties": {"app_id": {"type": "string"}, **SCHEMA["schema"]["properties"]}}
    schema = {"type": "object", "additionalProperties": False, "required": ["results"],
              "properties": {"results": {"type": "array", "items": item}}}
    prompt = (SYSTEM + "\n\nDo not run commands or read files; answer from the text below. Return "
              "exactly one result per application, with its app_id copied verbatim.\n\n"
              + "\n\n".join(f"### app_id: {a['app_id']}\n{_prompt(a)}" for a in batch))
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "schema.json").write_text(json.dumps(schema))
        cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
               "--ignore-rules", "-s", "read-only", "-C", tmp, "-c", 'model_reasoning_effort="low"',
               "--output-schema", "schema.json", "-o", "out.json", "-"]
        if model:
            cmd[2:2] = ["-m", model]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp,
                              timeout=CODEX_TIMEOUT_S)
        out = Path(tmp) / "out.json"
        if proc.returncode != 0 or not out.exists():
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            msg = " | ".join(tail) or f"exit {proc.returncode}"
            if "sign in again" in msg or "401 Unauthorized" in msg or "Not logged in" in msg:
                raise CodexAuthError(msg)
            raise RuntimeError(msg)
        results = json.loads(out.read_text())["results"]
    wanted = {a["app_id"] for a in batch}
    return {r["app_id"]: {k: r[k] for k in SCHEMA["schema"]["required"]}
            for r in results if r.get("app_id") in wanted and _valid(r)}


def _run_codex(todo: list[dict], cache: dict) -> None:
    if not shutil.which("codex"):
        log("  LLM_BACKEND=codex but the codex CLI isn't on PATH — skipping LLM calls")
        return
    model = env("CODEX_MODEL")
    tag = f"codex:{model or 'default'}"
    batches = [todo[i:i + CODEX_BATCH] for i in range(0, len(todo), CODEX_BATCH)]
    log(f"  codex: {len(batches)} batches of up to {CODEX_BATCH}, {CODEX_WORKERS} at a time")
    failed = 0
    with ThreadPoolExecutor(CODEX_WORKERS) as pool:
        futs = {pool.submit(_codex_batch, model, b): b for b in batches}
        for i, fut in enumerate(as_completed(futs), 1):
            b = futs[fut]
            try:
                got = fut.result()
            except CodexAuthError as exc:
                log(f"  codex isn't signed in ({exc}) — run `codex login`; stopping, "
                    "everything extracted so far is cached")
                for f in futs:
                    f.cancel()
                break
            except Exception as exc:
                failed += len(b)
                log(f"  codex batch failed ({len(b)} apps, first {b[0]['app_id']}): {exc}")
                continue
            failed += len(b) - len(got)
            for app_id, x in got.items():
                cache[app_id] = {**x, "model": tag}
            write_json(CACHE_FILE, cache)
            log(f"  codex batch {i}/{len(batches)}: {len(got)}/{len(b)} extracted")
    done = sum(1 for a in todo if a["app_id"] in cache)
    log(f"  extracted {done}, failed {failed}, not attempted {len(todo) - done - failed} ({tag})")


def run(bbox) -> list[dict]:
    apps = read_json(IN, [])
    cache: dict = read_json(CACHE_FILE, {})
    candidates = [a for a in apps if is_candidate(a["description"])]
    todo = [a for a in candidates if a["app_id"] not in cache]
    log(f"  {len(apps)} applications, {len(candidates)} mention homes, "
        f"{len(candidates) - len(todo)} cached, {len(todo)} to extract")

    key = env("OPENAI_API_KEY")
    backend = env("LLM_BACKEND") or "openai"
    if todo and backend == "codex":
        _run_codex(todo, cache)
    elif todo and not key:
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
