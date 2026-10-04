"""Infrastructure Impact Dashboard — pipeline CLI (outputs in data/impact/).

    python run_impact.py mock                       # stage 0: data/impact/mock/
    python run_impact.py water --area dublin        # reuse: planning, LLM, context, network, extension routing
    python run_impact.py footprints|transport|schools|scores
    python run_impact.py all --area dublin          # water + footprints + transport + schools + scores
"""
from __future__ import annotations

import argparse
import time

from pipeline.common import AREAS, log, resolve_bbox, stage

IMPACT_STAGES = ["footprints", "transport", "schools", "scores"]


def run_stage(name: str, bbox) -> None:
    if name == "mock":
        from impact import stage0_mock as m
        title = "IMPACT 0 mocks"
    elif name == "water":
        import run as water_cli
        for s in ["planning", "llm", "context", "network", "extend"]:
            water_cli.run_stage(s, bbox)
        return
    elif name == "footprints":
        from impact import stage1_footprints as m
        title = "IMPACT 1 footprints"
    elif name == "transport":
        from impact import stage2_transport as m
        title = "IMPACT 2 transport"
    elif name == "schools":
        from impact import stage3_schools as m
        title = "IMPACT 3 schools"
    else:
        from impact import stage4_scores as m
        title = "IMPACT 4 scores, summary, assumptions"
    with stage(title):
        m.run(bbox)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=["mock", "water", *IMPACT_STAGES, "all"])
    p.add_argument("--bbox", help="W,S,E,N in EPSG:4326 (write it as --bbox=...)")
    p.add_argument("--area", choices=sorted(AREAS), help="named bbox preset (e.g. dublin)")
    args = p.parse_args()
    bbox = resolve_bbox(args.bbox, args.area)
    log(f"bbox W,S,E,N = {','.join(f'{v:.5f}' for v in bbox)}")
    t0 = time.perf_counter()
    for name in (["water", *IMPACT_STAGES] if args.stage == "all" else [args.stage]):
        run_stage(name, bbox)
    log(f"total {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
