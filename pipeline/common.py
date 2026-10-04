"""Shared paths, bbox handling, logging and small IO helpers."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
INTERIM = DATA / "interim"
MOCK = DATA / "mock"

for d in (DATA, CACHE, INTERIM):
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:  # read-only filesystem when served from a host like Vercel
        pass

load_dotenv(ROOT / ".env")

# Named bboxes (W, S, E, N in EPSG:4326).
AREAS = {
    "test": (-6.2560, 53.3460, -6.2440, 53.3500),    # a few streets around Custom House Quay
    "docklands": (-6.2750, 53.3400, -6.2150, 53.3600),
    "city": (-6.3900, 53.2650, -6.1100, 53.4250),    # Dublin city and inner suburbs
    "dublin": (-6.5500, 53.1700, -6.0000, 53.6400),  # County Dublin
}
DEFAULT_BBOX = AREAS["test"]

# Metric CRS for all distance work: Irish Transverse Mercator.
METRIC_CRS = "EPSG:2157"
WGS84 = "EPSG:4326"

RUN_META = INTERIM / "run.json"
SOURCES = INTERIM / "sources.json"


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be W,S,E,N")
    w, s, e, n = parts
    if not (w < e and s < n):
        raise ValueError("bbox must satisfy W < E and S < N")
    return w, s, e, n


def resolve_bbox(arg: str | None, area: str | None = None) -> tuple[float, float, float, float]:
    """Explicit --bbox, then --area preset; otherwise reuse the bbox stage 1 ran with."""
    if arg:
        return parse_bbox(arg)
    if area:
        return AREAS[area]
    if RUN_META.exists():
        return tuple(read_json(RUN_META)["bbox"])
    return DEFAULT_BBOX


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@contextmanager
def stage(name: str):
    log(f"=== {name} ===")
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log(f"=== {name} done in {time.perf_counter() - t0:.1f}s ===")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.suffix == ".geojson" or path.parent == INTERIM:  # big files: no whitespace
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, indent=2, ensure_ascii=False)
    tmp.write_text(text)
    tmp.replace(path)


def record_source(key: str, count: int | None, error: str | None = None) -> None:
    """Row counts per external source, for summary.json `sources`."""
    src = read_json(SOURCES, {})
    src[key] = {"count": count, "error": error}
    write_json(SOURCES, src)


def feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def round_coords(geom: dict, nd: int = 5) -> dict:
    """~1 m precision; keeps county-scale GeoJSON small enough for the browser."""
    def r(c):
        return [round(c[0], nd), round(c[1], nd)] if isinstance(c[0], (int, float)) else [r(x) for x in c]
    return {**geom, "coordinates": r(list(geom["coordinates"]))}


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None
