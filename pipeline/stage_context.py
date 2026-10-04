"""Context stage: Census 2022 small areas + Uisce Éireann asset/capacity registers.

Runs before the network stage (which uses the Dublin City Council boundary from the
census to place DCC gully records) and feeds per-area load figures to stages 4–5.
"""
from __future__ import annotations

import io
import re
from html import unescape

import requests

from .common import CACHE, INTERIM, feature_collection, log, record_source, write_json
from .stage3_network import pad_bbox

CENSUS_URL = ("https://services-eu1.arcgis.com/BuS9rtTsYEV5C0xh/arcgis/rest/services/"
              "CensusHub2022_T5_2_SA/FeatureServer/0/query")
CENSUS_FIELDS = {
    "SA_PUB2022": "small_area_id", "ED_ENGLISH": "electoral_division", "CSO_LEA": "lea",
    "LOCAL_AUTHORITY": "local_authority", "T1_1AGETT_Norm": "population",
    "T5_2_TH": "households", "T5_2_TP": "persons_in_households", "T6_8_T_Norm": "dwellings",
    "T6_8_OVD_Norm": "vacant_dwellings", "T6_8_UHH_Norm": "holiday_homes",
}
CENSUS = INTERIM / "census.geojson"
CONTEXT = INTERIM / "context.json"
PAD_M = 600  # graph padding + a margin so every street segment sits inside some small area

UE = "https://water.widen.net/content/{}/original/{}"
UE_XLSX = {
    "wwtp": UE.format("zpvs4n6yad", "WWTP-2023Q3"),
    "dma": UE.format("d7n8siyenc", "DMA-2023Q3-Copy"),
    "wps": UE.format("6d9ygznuly", "WPS-2023Q3"),
    "wwps": UE.format("vwj0xxnrzf", "WWPS-2023Q3"),
    "reservoirs": UE.format("fkxmbpnexh", "WNS-2023Q3"),
}
REGISTERS = {
    "water_supply": "https://www.water.ie/connections/developer-services/capacity-registers/"
                    "water-supply-capacity-register/dublin",
    "wastewater": "https://www.water.ie/connections/developer-services/capacity-registers/"
                  "wastewater-treatment-capacity-register/Dublin",
}


def census(bbox) -> list[dict]:
    w, s, e, n = pad_bbox(bbox, PAD_M)
    params = {"geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
              "inSR": 4326, "outSR": 4326, "spatialRel": "esriSpatialRelIntersects",
              "outFields": ",".join(CENSUS_FIELDS), "returnGeometry": "true",
              "orderByFields": "ObjectId", "resultRecordCount": 1000, "f": "geojson"}
    feats, offset = [], 0
    while True:
        r = requests.get(CENSUS_URL, params={**params, "resultOffset": offset}, timeout=120)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"])
        page = body.get("features", [])
        feats.extend(page)
        if not page or not (body.get("exceededTransferLimit")
                            or body.get("properties", {}).get("exceededTransferLimit")):
            break
        offset += len(page)
    out = []
    for f in feats:
        p = {CENSUS_FIELDS[k]: v for k, v in f["properties"].items() if k in CENSUS_FIELDS}
        hh, pp = p.get("households") or 0, p.get("persons_in_households") or 0
        p["household_size"] = round(pp / hh, 2) if hh else None
        out.append({"type": "Feature", "geometry": f["geometry"], "properties": p})
    return out


def _xlsx(name: str, url: str):
    import pandas as pd

    path = CACHE / f"uisce_{name}.xlsx"
    if not path.exists():
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return pd.read_excel(io.BytesIO(path.read_bytes()))


def _register_rows(url: str) -> list[list[str]]:
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    html = r.text
    table = html[html.find("<table"): html.find("</table>")]
    rows = []
    for tr in re.findall(r"<tr.*?</tr>", table, flags=re.S):
        cells = [unescape(re.sub(r"<[^>]+>|\s+", " ", c)).strip(" •\u2022")
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S)]
        if any(cells):
            rows.append(cells)
    return rows


def uisce() -> dict:
    ctx: dict = {"source": "Uisce Éireann open data (CC BY 4.0) and capacity registers",
                 "errors": {}}
    dublin_las = ("Dublin City", "Fingal", "South Dublin", "Dun Laoghaire", "Dún Laoghaire")

    def is_dublin(v) -> bool:
        return isinstance(v, str) and v.startswith(dublin_las)

    try:
        df = _xlsx("wwtp", UE_XLSX["wwtp"])
        ring = df[df["Wastewater Treatment Plant Name"].str.contains("RINGSEND", na=False)]
        if len(ring):
            ctx["ringsend_wwtp_capacity_pe"] = int(ring.iloc[0]["Capacity (PE)"])
        ctx["dublin_wwtps"] = int(df["LA"].map(is_dublin).sum())
    except Exception as exc:
        ctx["errors"]["wwtp"] = str(exc)
    for key, col in (("dma", "LOCALAUTHORITY"), ("wps", "LA"), ("wwps", "LA"), ("reservoirs", "LA")):
        try:
            df = _xlsx(key, UE_XLSX[key])
            ctx[f"dublin_{key}_count"] = int(df[col].map(is_dublin).sum())
        except Exception as exc:
            ctx["errors"][key] = str(exc)
    for key, url in REGISTERS.items():
        try:
            rows = _register_rows(url)
            header, body = rows[0], rows[1:]
            ctx[f"{key}_capacity_register"] = [dict(zip(header, r)) for r in body]
        except Exception as exc:
            ctx["errors"][key] = str(exc)
    return ctx


def run(bbox) -> None:
    try:
        sas = census(bbox)
        record_source("census_small_areas", len(sas))
        pop = sum(f["properties"].get("population") or 0 for f in sas)
        hh = sum(f["properties"].get("households") or 0 for f in sas)
        log(f"  Census 2022 small areas: {len(sas)} ({pop} people, {hh} households)")
    except Exception as exc:
        log(f"  Census small areas FAILED: {exc}")
        record_source("census_small_areas", 0, str(exc))
        sas = []
    write_json(CENSUS, feature_collection(sas))

    ctx = uisce()
    write_json(CONTEXT, ctx)
    got = [k for k in ctx if k not in ("source", "errors")]
    record_source("uisce_eireann", len(got), "; ".join(f"{k}: {v}" for k, v in ctx["errors"].items()) or None)
    log(f"  Uisce Éireann context: {', '.join(got)}")
    for k, v in ctx["errors"].items():
        log(f"  Uisce Éireann {k} FAILED: {v}")
