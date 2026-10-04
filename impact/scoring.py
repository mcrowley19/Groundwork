"""Sub-scores (0 = no extra infrastructure need, 100 = greatest need) and the overall score.
Every scale comes from config.py."""
from __future__ import annotations

from . import config as C


def _clip(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 1)


def water_score(connected: bool, route_length_m: float | None) -> float:
    if connected:
        return 0.0
    if route_length_m is None or route_length_m <= 0:  # unconnected and no route could be found
        return 100.0
    return _clip(100 * route_length_m / C.WATER_SCORE_FULL_ROUTE_M)


def transport_score(nearest_frequent_stop_m: float | None) -> float:
    if nearest_frequent_stop_m is None:
        return 100.0
    return _clip(100 * nearest_frequent_stop_m / C.TRANSPORT_SCORE_FULL_M)


def schools_score(max_pressure_ratio: float | None) -> float:
    if max_pressure_ratio is None:  # no school of a level within its catchment
        return 100.0
    return _clip(100 * max_pressure_ratio / C.SCHOOL_SCORE_FULL_RATIO)


def dev_pressure_ratio(primary_pupils: float, primary_enrolments: list[float],
                       secondary_pupils: float, secondary_enrolments: list[float]) -> float | None:
    """A development's own pupils ÷ combined enrolment of its nearby schools, worst level.
    None when a level has no school within its catchment (treated as maximum pressure)."""
    ratios = []
    for pupils, enrol in ((primary_pupils, primary_enrolments), (secondary_pupils, secondary_enrolments)):
        total = sum(e for e in enrol if e)
        if not total:
            return None
        ratios.append(pupils / total)
    return max(ratios)


def overall_score(scores: dict) -> float:
    w = C.SCORE_WEIGHTS
    return _clip(sum(scores[k] * w[k] for k in w) / sum(w.values()))
