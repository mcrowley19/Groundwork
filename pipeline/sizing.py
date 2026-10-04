"""Water-main sizing per Uisce Éireann's Code of Practice for Water Infrastructure,
IW-CDS-5020-03 Revision 2 (July 2020). Every constant below is quoted from that document;
the outputs are design-guidance estimates, not surveyed pipe sizes.
"""
from __future__ import annotations

import math

REFERENCE = "Uisce Éireann IW-CDS-5020-03 Rev 2 (2020)"
PCC_L_PER_PERSON_DAY = 150    # §3.7.2 per-capita consumption
OCCUPANCY = 2.7               # §3.7.2 average occupancy ratio, persons per dwelling
PEAK_WEEK_FACTOR = 1.25       # §3.7.2 average day/peak week = 1.25 × average daily demand
PEAK_FACTOR = 5.0             # §3.7.2 peak demand for pipe sizing = 5.0 × average day/peak week
VELOCITY_RANGE_MPS = (0.3, 1.5)  # §3.7 flow velocity in mains, "preferably in the middle"
MIN_PUBLIC_MAIN_MM = 100      # §3.5.26 hydrants not supplied from mains < 100 mm; table note **

# §3.7.1 "Table: Typical Main Size for Multiple Properties":
# (max dwellings, nominal bore mm (other materials), PE outside diameter)
TYPICAL_MAIN = [
    (5, 50, "up to 63"),
    (40, 80, "90"),
    (100, 100, "110/125"),
    (300, 150, "160/180"),
    (700, 200, "225"),
]
STANDARD_BORES_MM = [250, 300, 350, 400, 450, 500, 600, 700, 800, 900, 1000]
DESIGN_VELOCITY_MPS = 0.9     # middle of the 0.3–1.5 m/s range, used only beyond the table


def avg_daily_demand_m3(units: int) -> float:
    return units * OCCUPANCY * PCC_L_PER_PERSON_DAY / 1000


def peak_flow_lps(units: int) -> float:
    return units * OCCUPANCY * PCC_L_PER_PERSON_DAY * PEAK_WEEK_FACTOR * PEAK_FACTOR / 86_400


def velocity_mps(flow_lps: float, bore_mm: float) -> float:
    area = math.pi / 4 * (bore_mm / 1000) ** 2
    return flow_lps / 1000 / area


def size_main(units: int) -> dict:
    """Nominal bore for a main serving `units` dwellings."""
    q = peak_flow_lps(units)
    for max_units, bore, pe_od in TYPICAL_MAIN:
        if units <= max_units:
            adopted = max(bore, MIN_PUBLIC_MAIN_MM)
            basis = f"{REFERENCE} typical main size table ({units} dwellings → {bore} mm)"
            if adopted != bore:
                basis += f"; raised to {MIN_PUBLIC_MAIN_MM} mm public-main minimum (§3.5.26)"
            return {"nominal_bore_mm": adopted,
                    "pe_outside_diameter_mm": pe_od if adopted == bore else "110/125",
                    "peak_flow_lps": round(q, 2),
                    "peak_velocity_mps": round(velocity_mps(q, adopted), 2),
                    "sizing_basis": basis}
    d_min = math.sqrt(4 * (q / 1000) / (math.pi * DESIGN_VELOCITY_MPS)) * 1000
    bore = next((b for b in STANDARD_BORES_MM if b >= d_min), STANDARD_BORES_MM[-1])
    return {"nominal_bore_mm": bore, "pe_outside_diameter_mm": None,
            "peak_flow_lps": round(q, 2),
            "peak_velocity_mps": round(velocity_mps(q, bore), 2),
            "sizing_basis": f"> 700 dwellings, beyond the {REFERENCE} table: smallest standard "
                            f"bore keeping peak velocity ≤ {DESIGN_VELOCITY_MPS} m/s "
                            "(hydraulic model required in practice)"}


def assumptions() -> dict:
    return {
        "reference": REFERENCE,
        "per_capita_l_per_day": PCC_L_PER_PERSON_DAY,
        "design_occupancy": OCCUPANCY,
        "peak_week_factor": PEAK_WEEK_FACTOR,
        "peak_factor": PEAK_FACTOR,
        "velocity_range_mps": list(VELOCITY_RANGE_MPS),
        "min_public_main_mm": MIN_PUBLIC_MAIN_MM,
        "typical_main_table": [{"max_dwellings": u, "nominal_bore_mm": b, "pe_outside_diameter_mm": od}
                               for u, b, od in TYPICAL_MAIN],
        "note": "Sizes are the Code's guidance values for the dwellings served, not surveyed "
                "pipes; the Code requires a hydraulic assessment for real design.",
    }
