"""Impact stage 3: school places.

Schools: Department of Education 'Data on Individual Schools' 2025/26 (primary = mainstream
national schools; post-primary = all post-primary schools), with latitude/longitude and
enrolment. Capacity is not published, so pressure is a proxy against current enrolment.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import requests
from pyproj import Transformer
from scipy.spatial import cKDTree

from pipeline.common import log
from pipeline.stage3_network import pad_bbox

from . import config as C
from .common import CACHE, DEVS, METRIC_CRS, OUT, WGS84, fc, feature, read_json, source, write_json

ROLL = r"^\d{5}[A-Z]$"
PAD_M = 6000  # > the post-primary catchment, so edge-of-bbox developments see their schools
_to_m = Transformer.from_crs(WGS84, METRIC_CRS, always_xy=True)


def _download(url: str, name: str):
    path = CACHE / "schools" / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def _header_row(path, sheet, first_col: str) -> int:
    raw = pd.read_excel(path, sheet_name=sheet, header=None, nrows=6)
    return next(i for i in range(len(raw)) if str(raw.iat[i, 0]).startswith(first_col))


def load_primary() -> pd.DataFrame:
    path = _download(C.SCHOOLS_PRIMARY_URL, "primary_2025_26.xlsx")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sheets = pd.ExcelFile(path).sheet_names
        info_sh = next(s for s in sheets if s.strip().lower() == "mainstream")
        enr_sh = next(s for s in sheets if "standard" in s.lower())
        info = pd.read_excel(path, sheet_name=info_sh, header=_header_row(path, info_sh, "Academic Year"))
        enr = pd.read_excel(path, sheet_name=enr_sh, header=_header_row(path, enr_sh, "Academic Year"))
    info = info[info["Roll Number"].astype(str).str.match(ROLL)]
    enr = enr[enr["Roll Number"].astype(str).str.match(ROLL)]
    info = info[["Roll Number", "Official Name", "School Latitude", "School Longitude"]]
    df = info.merge(enr[["Roll Number", "Enrolment per Return"]], on="Roll Number", how="left")
    return pd.DataFrame({"school_id": df["Roll Number"], "name": df["Official Name"], "level": "primary",
                         "lat": pd.to_numeric(df["School Latitude"], errors="coerce"),
                         "lon": pd.to_numeric(df["School Longitude"], errors="coerce"),
                         "enrolment": pd.to_numeric(df["Enrolment per Return"], errors="coerce")})


def load_postprimary() -> pd.DataFrame:
    path = _download(C.SCHOOLS_POSTPRIMARY_URL, "postprimary_2025_26.xlsx")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sheets = pd.ExcelFile(path).sheet_names
        info_sh = next(s for s in sheets if "school list" in s.lower())
        enr_sh = next(s for s in sheets if "programme" in s.lower())
        info = pd.read_excel(path, sheet_name=info_sh, header=_header_row(path, info_sh, "Academic Year"))
        enr = pd.read_excel(path, sheet_name=enr_sh, header=_header_row(path, enr_sh, "Academic Year"))
    total = next(c for c in enr.columns if str(c).startswith("Total"))
    info = info[info["Roll Number"].astype(str).str.match(ROLL)]
    enr = enr[enr["Roll Number"].astype(str).str.match(ROLL)]
    info = info[["Roll Number", "Official School Name", "School Latitude", "School Longitude"]]
    df = info.merge(enr[["Roll Number", total]], on="Roll Number", how="left")
    return pd.DataFrame({"school_id": df["Roll Number"], "name": df["Official School Name"], "level": "post-primary",
                         "lat": pd.to_numeric(df["School Latitude"], errors="coerce"),
                         "lon": pd.to_numeric(df["School Longitude"], errors="coerce"),
                         "enrolment": pd.to_numeric(df[total], errors="coerce")})


def run(bbox) -> None:
    from .scoring import dev_pressure_ratio

    devs = read_json(DEVS, [])
    frames = []
    for name, loader in (("schools_primary", load_primary), ("schools_post_primary", load_postprimary)):
        try:
            df = loader()
            located = df.lat.notna() & df.lon.notna()
            source(name, "ok" if located.all() else "partial", int(located.sum()),
                   None if located.all() else f"{int((~located).sum())} schools without coordinates")
            frames.append(df[located])
            log(f"  {name}: {len(df)} schools nationally ({int(located.sum())} located)")
        except Exception as exc:
            log(f"  {name} FAILED: {exc}")
            source(name, "failed", 0, str(exc))
    if not frames:
        write_json(OUT / "schools.geojson", fc([]))
        for d in devs:
            d["schools"] = None
        write_json(DEVS, devs)
        return

    sc = pd.concat(frames, ignore_index=True)
    w, s, e, n = pad_bbox(bbox, PAD_M)
    sc = sc[(sc.lon >= w) & (sc.lon <= e) & (sc.lat >= s) & (sc.lat <= n)].reset_index(drop=True)
    sc["x"], sc["y"] = _to_m.transform(sc.lon.values, sc.lat.values)
    sc["projected"] = 0.0
    log(f"  {len(sc)} schools within {PAD_M / 1000:.0f} km of the bbox "
        f"({(sc.level == 'primary').sum()} primary, {(sc.level == 'post-primary').sum()} post-primary)")

    levels = {"primary": ("primary_pupils", "nearby_primary", C.PRIMARY_RADIUS_M, C.CHILDREN_PER_HOUSEHOLD_PRIMARY),
              "post-primary": ("secondary_pupils", "nearby_secondary", C.POSTPRIMARY_RADIUS_M,
                               C.CHILDREN_PER_HOUSEHOLD_SECONDARY)}
    trees = {lvl: (cKDTree(sc.loc[sc.level == lvl, ["x", "y"]].values), sc.index[sc.level == lvl].to_numpy())
             for lvl in levels if (sc.level == lvl).any()}
    xy = np.array([_to_m.transform(*d["centroid"]) for d in devs]) if devs else np.empty((0, 2))
    for k, d in enumerate(devs):
        out = {}
        enrol = {}
        for lvl, (pkey, nkey, radius, per_dwelling) in levels.items():
            pupils = d["num_units"] * per_dwelling
            idx = []
            if lvl in trees:
                tree, ids = trees[lvl]
                idx = [ids[i] for i in tree.query_ball_point(xy[k], radius)]
            for i in idx:  # share this development's pupils across its nearby schools
                sc.at[i, "projected"] += pupils / len(idx)
            out[pkey] = round(pupils, 1)
            out[nkey] = sorted(sc.school_id[idx].tolist())
            enrol[lvl] = sc.enrolment[idx].fillna(0).tolist()
        ratio = dev_pressure_ratio(out["primary_pupils"], enrol["primary"],
                                   out["secondary_pupils"], enrol["post-primary"])
        out["pressure_flag"] = bool(ratio is None or ratio >= C.SCHOOL_PRESSURE_THRESHOLD)
        out["worst_pressure_ratio"] = round(ratio, 4) if ratio is not None else None
        d["schools"] = out
        d["_school_ratio"] = ratio

    in_box = (sc.lon >= bbox[0]) & (sc.lon <= bbox[2]) & (sc.lat >= bbox[1]) & (sc.lat <= bbox[3])
    feats = [feature({"type": "Point", "coordinates": [round(r.lon, 5), round(r.lat, 5)]}, {
        "school_id": r.school_id, "name": r.name, "level": r.level,
        "enrolment": int(r.enrolment) if r.enrolment == r.enrolment else None,
        "projected_new_pupils": round(r.projected, 1),
        "pressure_ratio": round(r.projected / r.enrolment, 3) if r.enrolment and r.enrolment == r.enrolment else None})
        for r in sc[in_box | (sc.projected > 0)].itertuples()]
    write_json(OUT / "schools.geojson", fc(feats))
    write_json(DEVS, devs)
    flagged = sum(1 for d in devs if d["schools"]["pressure_flag"])
    log(f"  schools.geojson: {len(feats)} schools; {flagged}/{len(devs)} developments flagged "
        f"(threshold {C.SCHOOL_PRESSURE_THRESHOLD})")
