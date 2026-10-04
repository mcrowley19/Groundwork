"""Stage 1: planning applications from the National Planning Application Database."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import requests
from pyproj import Transformer

from .common import INTERIM, RUN_META, log, record_source, write_json

URL = (
    "https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/"
    "IrishPlanningApplications/FeatureServer/0/query"
)
FIELDS = [
    "OBJECTID", "PlanningAuthority", "ApplicationNumber", "DevelopmentDescription",
    "DevelopmentAddress", "ApplicationStatus", "ApplicationType", "Decision",
    "NumResidentialUnits", "ITMEasting", "ITMNorthing", "ReceivedDate", "DecisionDate",
    "LinkAppDetails",
]
PAGE = 1000
OUT = INTERIM / "planning.json"

_itm_to_wgs = Transformer.from_crs("EPSG:2157", "EPSG:4326", always_xy=True)


def app_id(authority: str | None, number: str | None) -> str:
    auth = re.sub(r"[^A-Za-z]", "", "".join(w[0] for w in (authority or "X").split()))
    return f"{auth.upper()}-{(number or '').strip()}"


def fetch(bbox, years: int = 3) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=365 * years)).strftime("%Y-%m-%d 00:00:00")
    w, s, e, n = bbox
    params = {
        "where": f"ReceivedDate >= TIMESTAMP '{since}'",
        "geometry": f"{w},{s},{e},{n}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(FIELDS),
        "returnGeometry": "true",
        "outSR": 2157,
        "orderByFields": "OBJECTID",
        "resultRecordCount": PAGE,
        "f": "json",
    }
    rows: list[dict] = []
    offset = 0
    while True:
        r = requests.get(URL, params={**params, "resultOffset": offset}, timeout=60)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        page = []
        for f in body.get("features", []):
            a = dict(f["attributes"])
            g = f.get("geometry") or {}
            a["_x"], a["_y"] = g.get("x"), g.get("y")
            page.append(a)
        rows.extend(page)
        log(f"  page offset={offset}: {len(page)} rows")
        if not body.get("exceededTransferLimit") or not page:
            break
        offset += len(page)
    return rows


def run(bbox) -> list[dict]:
    write_json(RUN_META, {"bbox": list(bbox)})
    try:
        raw = fetch(bbox)
    except Exception as exc:  # log and continue with nothing — never invent rows
        log(f"  planning source FAILED: {exc}")
        record_source("planning", 0, str(exc))
        write_json(OUT, [])
        return []
    log(f"  fetched {len(raw)} raw applications")

    apps: dict[str, dict] = {}
    skipped_geom = 0
    for a in raw:
        # Point geometry requested in EPSG:2157; the ITM attribute columns are
        # null for most Dublin City rows, so they are only a fallback.
        x, y = a.get("_x") or a.get("ITMEasting"), a.get("_y") or a.get("ITMNorthing")
        if not x or not y:
            skipped_geom += 1
            continue
        lon, lat = _itm_to_wgs.transform(x, y)
        aid = app_id(a.get("PlanningAuthority"), a.get("ApplicationNumber"))
        received, decided = a.get("ReceivedDate"), a.get("DecisionDate")
        apps[aid] = {
            "app_id": aid,
            "authority": a.get("PlanningAuthority"),
            "application_number": a.get("ApplicationNumber"),
            "description": (a.get("DevelopmentDescription") or "").strip(),
            "address": a.get("DevelopmentAddress"),
            "application_status": a.get("ApplicationStatus"),
            "application_type": a.get("ApplicationType"),
            "decision": a.get("Decision"),
            "source_num_units": a.get("NumResidentialUnits"),
            "received_date": (
                datetime.fromtimestamp(received / 1000, timezone.utc).date().isoformat()
                if received else None
            ),
            "decision_date": (
                datetime.fromtimestamp(decided / 1000, timezone.utc).date().isoformat()
                if decided else None
            ),
            "planning_url": a.get("LinkAppDetails"),
            "lon": lon,
            "lat": lat,
        }
    rows = list(apps.values())
    if skipped_geom:
        log(f"  dropped {skipped_geom} rows with no ITM coordinates")
    log(f"  {len(rows)} unique applications (EPSG:2157 -> 4326)")
    record_source("planning", len(rows))
    write_json(OUT, rows)
    return rows
