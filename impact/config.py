"""Every assumption the Infrastructure Impact Dashboard relies on.

Rule: any number the pipeline estimates (rather than reads from a source) must come from a
named entry here. Each entry carries a label, unit and a source note; anything without a
citation says "TODO: cite". assumptions.json is generated from ASSUMPTIONS verbatim.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

ASSUMPTIONS: list["A"] = []


@dataclass(frozen=True)
class A:
    key: str
    label: str
    value: object
    unit: str
    source_note: str

    def __post_init__(self):
        ASSUMPTIONS.append(self)


def assumptions_json() -> list[dict]:
    return [asdict(a) for a in ASSUMPTIONS]


TODO = "TODO: cite"

# --- demand ---------------------------------------------------------------------------
HOUSEHOLD_SIZE = A(
    "HOUSEHOLD_SIZE", "Average persons per household (Dublin)", 2.75, "persons/dwelling",
    "CSO Census 2022 SAPS table T5.2: persons in private households ÷ private households, summed "
    "over the 5,576 small areas intersecting County Dublin (1,570,981 / 571,188).").value

NHTS_DAILY_TRIP_RATE = A(
    "NHTS_DAILY_TRIP_RATE", "Trips per person per day, Dublin City and Suburbs", 2.68, "trips/person/day",
    "NTA National Household Travel Survey 2023 (Ipsos B&A, Aug 2024), Figure 16.").value
NHTS_AM_PEAK_SHARE = A(
    "NHTS_AM_PEAK_SHARE", "Share of daily trips made in the AM peak (07:00–09:59)", 0.22, "fraction",
    "NTA National Household Travel Survey 2023, trend section: 'AM Peak travel is unchanged at 22% in "
    "2023' (national figure; no Dublin-specific peak share is published).").value
PEAK_TRIP_RATE = A(
    "PEAK_TRIP_RATE", "AM-peak trips per resident", round(NHTS_DAILY_TRIP_RATE * NHTS_AM_PEAK_SHARE, 3),
    "trips/person (07:00–10:00)", "Derived: NHTS_DAILY_TRIP_RATE × NHTS_AM_PEAK_SHARE.").value
PT_MODE_SHARE = A(
    "PT_MODE_SHARE", "Public transport share of trips, Dublin City and Suburbs", 0.07, "fraction",
    "NTA National Household Travel Survey 2023, Figure 53: bus/coach 5% + train/DART/Luas 2% of all "
    "trips. All-day, all-purpose share; the AM-peak commuting share is likely higher.").value
AM_PEAK_HOURS = A(
    "AM_PEAK_HOURS", "Length of the AM peak window", 3, "hours",
    "Project brief (weekday 07:00–10:00); matches the NHTS 2023 AM-peak definition.").value

# --- schools --------------------------------------------------------------------------
PRIMARY_SHARE_OF_POPULATION = A(
    "PRIMARY_SHARE_OF_POPULATION", "Share of population presenting for primary school", 0.12, "fraction",
    "Department of Education and Science / DoEHLG, 'The Provision of Schools and the Planning System: "
    "A Code of Practice' (2008): 'an average of 12% of the population are expected to present for "
    "primary education'. Cross-check: 2025/26 mainstream primary enrolment 535,600 ÷ Census 2022 "
    "population 5,149,139 = 10.4%.").value
POSTPRIMARY_SHARE_OF_POPULATION = A(
    "POSTPRIMARY_SHARE_OF_POPULATION", "Share of population in post-primary school", 0.0834, "fraction",
    "Derived: Department of Education 'Data on Individual Schools' 2025/26 post-primary enrolment "
    "429,653 ÷ CSO Census 2022 State population 5,149,139. The 2008 Code of Practice gives no "
    "post-primary figure.").value
CHILDREN_PER_HOUSEHOLD_PRIMARY = A(
    "CHILDREN_PER_HOUSEHOLD_PRIMARY", "Primary pupils per new dwelling",
    round(PRIMARY_SHARE_OF_POPULATION * HOUSEHOLD_SIZE, 3), "pupils/dwelling",
    "Derived: PRIMARY_SHARE_OF_POPULATION × HOUSEHOLD_SIZE.").value
CHILDREN_PER_HOUSEHOLD_SECONDARY = A(
    "CHILDREN_PER_HOUSEHOLD_SECONDARY", "Post-primary pupils per new dwelling",
    round(POSTPRIMARY_SHARE_OF_POPULATION * HOUSEHOLD_SIZE, 3), "pupils/dwelling",
    "Derived: POSTPRIMARY_SHARE_OF_POPULATION × HOUSEHOLD_SIZE.").value
PRIMARY_RADIUS_M = A("PRIMARY_RADIUS_M", "Catchment radius for 'nearby' primary schools", 2000, "m",
                     "Project brief.").value
POSTPRIMARY_RADIUS_M = A("POSTPRIMARY_RADIUS_M", "Catchment radius for 'nearby' post-primary schools",
                         5000, "m", "Project brief.").value
SCHOOL_PRESSURE_THRESHOLD = A(
    "SCHOOL_PRESSURE_THRESHOLD", "Projected new pupils ÷ current enrolment that raises a pressure flag",
    0.10, "ratio", f"{TODO}. Proxy only: the Department publishes enrolment, not capacity.").value

# --- transport ------------------------------------------------------------------------
STOP_WALK_M = A("STOP_WALK_M", "Walking distance that counts as 'near' a stop", 400, "m",
                f"Project brief; {TODO} (common planning walk-distance standard).").value
FREQ_THRESHOLD = A("FREQ_THRESHOLD", "Departures per hour for a 'frequent' stop or station", 6,
                   "departures/hr", f"{TODO}. Design choice: a 10-minute AM-peak service.").value
BUS_SPEED_KMH = A("BUS_SPEED_KMH", "Average operating speed of a new bus route", 15, "km/h", TODO).value
LAYOVER_MIN = A("LAYOVER_MIN", "Layover/recovery time per round trip", 10, "min", TODO).value
BUS_CAPACITY = A("BUS_CAPACITY", "Passengers per bus", 90, "passengers", f"{TODO} (double-deck).").value
BUS_LOAD_FACTOR = A("BUS_LOAD_FACTOR", "Planned peak load as share of capacity", 0.8, "fraction", TODO).value
MIN_HEADWAY = A("MIN_HEADWAY", "Shortest headway a proposed route may run", 5, "min", "Project brief; design choice.").value
MAX_HEADWAY = A("MAX_HEADWAY", "Longest headway a proposed route may run", 20, "min", "Project brief; design choice.").value
DBSCAN_EPS_M = A("DBSCAN_EPS_M", "Clustering distance for weak/unserved developments", 600, "m",
                 "Project brief.").value
DBSCAN_MIN_DEVS = A("DBSCAN_MIN_DEVS", "Minimum developments in a cluster to propose a route", 2,
                    "developments", "Design choice.").value
GTFS_URL = A("GTFS_URL", "NTA GTFS static feed", "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip",
             "url", "data.gov.ie dataset 'NTA GTFS and GTFS Realtime' (nta-gtfs), resource 'GTFS'.").value

# --- water ----------------------------------------------------------------------------
WATER_MAIN_COST_EUR_PER_M = A("WATER_MAIN_COST_EUR_PER_M", "Installed cost of new water main",
                              500, "EUR/m", f"{TODO}. Placeholder unit rate; Uisce Éireann does not "
                              "publish one.").value

# --- planning / footprints ------------------------------------------------------------
STATUS_MAP = A("STATUS_MAP", "LLM status → dashboard status (others excluded)",
               {"granted": "granted", "pending": "pending", "appealed": "pending"}, "mapping",
               "Design choice: refused, withdrawn, invalid and unknown applications are excluded.").value
FOOTPRINT_SQUARE_M = A("FOOTPRINT_SQUARE_M", "Side of the square drawn when no site polygon exists",
                       40, "m", "Display only; carries no analytic weight.").value

# --- scores (0 = no extra need, 100 = greatest need) ------------------------------------
WATER_SCORE_FULL_ROUTE_M = A("WATER_SCORE_FULL_ROUTE_M", "New-main route length that scores 100",
                             1000, "m", "Design choice.").value
TRANSPORT_SCORE_FULL_M = A("TRANSPORT_SCORE_FULL_M", "Distance to a frequent stop/station that scores 100",
                           1600, "m", "Design choice (4 × STOP_WALK_M).").value
SCHOOL_SCORE_FULL_RATIO = A("SCHOOL_SCORE_FULL_RATIO", "Nearby-school pressure ratio that scores 100",
                            0.25, "ratio", "Design choice.").value
SCORE_WEIGHTS = A("SCORE_WEIGHTS", "Overall score weights", {"water": 0.4, "transport": 0.35, "schools": 0.25},
                  "weights", "Design choice.").value

# --- output size ----------------------------------------------------------------------
SIMPLIFY_TOLERANCE_M = A("SIMPLIFY_TOLERANCE_M", "Geometry simplification tolerance", 5, "m",
                         "Keeps the full dataset under ~15 MB (project brief).").value
COORD_DECIMALS = A("COORD_DECIMALS", "Coordinate decimals in outputs", 5, "decimal places",
                   "≈1 m precision.").value
