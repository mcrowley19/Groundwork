"""Will the pipes cope? — pipeline CLI.

    python run.py all --bbox W,S,E,N
    python run.py all --area dublin
    python run.py planning|llm|context|network|extend|summary [--bbox W,S,E,N]
"""
from __future__ import annotations

import argparse
import time

from pipeline.common import AREAS, log, resolve_bbox, stage

STAGES = ["planning", "llm", "context", "network", "extend", "summary"]


def run_stage(name: str, bbox) -> None:
    if name == "planning":
        from pipeline import stage1_planning as m
        title = "STAGE 1 planning applications"
    elif name == "llm":
        from pipeline import stage2_llm as m
        title = "STAGE 2 LLM extraction"
    elif name == "context":
        from pipeline import stage_context as m
        title = "STAGE 2b census + Uisce Éireann context"
    elif name == "network":
        from pipeline import stage3_network as m
        title = "STAGE 3 inferred water network"
    elif name == "extend":
        from pipeline import stage4_extend as m
        title = "STAGE 4 extension routing"
    else:
        from pipeline import stage5_summary as m
        title = "STAGE 5 summary + mocks"
    with stage(title):
        m.run(bbox)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=STAGES + ["all"])
    p.add_argument("--bbox", help="W,S,E,N in EPSG:4326 (default: Custom House Quay test bbox, "
                                  "or the bbox stage 1 last ran with)")
    p.add_argument("--area", choices=sorted(AREAS), help="named bbox preset (e.g. dublin)")
    args = p.parse_args()
    bbox = resolve_bbox(args.bbox, args.area)
    log(f"bbox W,S,E,N = {','.join(f'{v:.5f}' for v in bbox)}")
    t0 = time.perf_counter()
    for name in STAGES if args.stage == "all" else [args.stage]:
        run_stage(name, bbox)
    log(f"total {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
