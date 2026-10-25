"""Offline data pipeline driver.

    python -m data_build.build_all [--stage all|airports,routes,...] [--only id1,id2] [--limit N] [--resume]

Stages (in order): airports, routes, destinations, climate, hotels, districts, synthetic, insights.
`hotels`, `districts` and `synthetic` run per destination in one pipeline (fetch -> hotels ->
districts -> synthetic, committed together), so an interrupted run never leaves a destination
half built. Every download is cached under data_build/cache/; `--resume` skips destinations whose
stage is already marked done in the DB meta table. Safe to interrupt at any time (Ctrl-C).
"""
from __future__ import annotations

import argparse
import sys

from .common import log

STAGES = ("airports", "routes", "destinations", "climate", "hotels", "districts", "synthetic", "insights", "pois")


def parse_stages(s: str) -> list[str]:
    if s == "all":
        return list(STAGES)
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if part not in STAGES:
            raise SystemExit(f"unknown stage {part!r}; known: {', '.join(STAGES)}")
        out.append(part)
    return [s for s in STAGES if s in out]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m data_build.build_all")
    ap.add_argument("--stage", default="all")
    ap.add_argument("--only", default="", help="comma list of destination ids")
    ap.add_argument("--limit", type=int, default=None, help="process at most N destinations")
    ap.add_argument("--resume", action="store_true", help="skip destinations already done for a stage")
    ap.add_argument("--refresh", action="store_true", help="ignore cached downloads (airports/routes)")
    ap.add_argument("--no-network", action="store_true", help="hotels: use cached Overpass/Photon only")
    ap.add_argument("--wait-rate-limit", action="store_true",
                    help="climate: on an Open-Meteo quota error sleep until the next window and continue")
    args = ap.parse_args(argv)
    stages = parse_stages(args.stage)
    only = [x.strip() for x in args.only.split(",") if x.strip()] or None

    try:
        if "airports" in stages:
            from .airports import run_airports
            run_airports(refresh=args.refresh)
        if "routes" in stages:
            from .airports import run_routes
            run_routes(refresh=args.refresh)
        if "destinations" in stages:
            from .destinations import run_destinations
            run_destinations(only=only, limit=args.limit)
        if "climate" in stages:
            from .climate import run_climate
            run_climate(only=only, limit=args.limit, resume=args.resume, wait_rate_limit=args.wait_rate_limit)
        hd = [s for s in ("hotels", "districts", "synthetic") if s in stages]
        if hd:
            from .hotels import run_hotel_pipeline
            run_hotel_pipeline(hd, only=only, limit=args.limit, resume=args.resume,
                               network=not args.no_network)
        if "insights" in stages:
            from .insights import run_insights
            run_insights()
        if "pois" in stages:
            from .pois import run_pois
            run_pois(only=only, limit=args.limit, resume=args.resume, network=not args.no_network)
    except KeyboardInterrupt:
        log("interrupted; completed destinations are committed, rerun with --resume")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
