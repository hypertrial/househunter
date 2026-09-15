#!/usr/bin/env python3
"""Maintainer-only generator for the compact ranking_v2 county bundle.

Ranking RPP geography is produced by assign_counties_v2: MSA MARPP, or official
state all-items RPP labeled `state`. This generator packages reviewed artifacts
and does not fetch Census, BEA, FBI, FCC, HRSA, EPA, or NOAA at runtime.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from househunter.errors import HouseHunterError
from househunter.ranking_reference import (
    COUNTY_COLUMNS,
    PILLAR_INTERNALS,
    load_homeschool_policy,
    load_source_lock,
    pillar_utility,
    validate_ranking_assets,
    write_ranking_bundle,
    write_synthetic_fixture_bundle,
)
from househunter.secure_fetch import deny_restricted_data_class, download_locked_file


def _load_reviewed_counties(path: Path) -> pl.DataFrame:
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:
        raise HouseHunterError(f"Cannot read reviewed ranking counties: {exc}") from exc
    missing = [name for name in COUNTY_COLUMNS if name not in frame.columns]
    if missing:
        raise HouseHunterError(
            "Reviewed ranking counties are missing columns: " + ", ".join(missing)
        )
    for row in frame.iter_rows(named=True):
        if row.get("u_safety") is None:
            raise HouseHunterError("Reviewed ranking counties must include precomputed utilities")
        del row
    return frame.select(COUNTY_COLUMNS).sort("county_fips")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path)
    parser.add_argument("--counties", type=Path, help="Reviewed compact county parquet")
    parser.add_argument("--calibration", type=Path, help="Reviewed calibration JSON")
    parser.add_argument("--citations", type=Path, help="Reviewed citations JSON")
    parser.add_argument("--fixture", action="store_true", help="Write the synthetic test fixture")
    parser.add_argument(
        "--download-staging",
        type=Path,
        help="Optional maintainer staging directory for locked HTTPS downloads",
    )
    args = parser.parse_args()
    if args.fixture:
        write_synthetic_fixture_bundle(args.output)
        validate_ranking_assets(args.output)
        return 0
    lock = load_source_lock(args.source_lock)
    for source in lock["sources"]:
        deny_restricted_data_class(source.get("data_class"), label=source["name"])
        if source.get("runtime_fetch") is not False:
            raise HouseHunterError(f"{source['name']} must remain maintainer-only")
    if args.download_staging is not None:
        allowed = {str(host).lower() for host in lock["allowed_hosts"]}
        identified = []
        for source in lock["sources"]:
            url = source.get("url")
            size = source.get("expected_size")
            digest = source.get("sha256")
            if not url:
                continue
            if not size or not digest:
                raise HouseHunterError(
                    f"{source['name']} cannot be downloaded without a reviewed size and sha256"
                )
            identified.append(source)
        if not identified:
            raise HouseHunterError("No ranking source has a reviewed download identity")
        for source in identified:
            download_locked_file(
                str(source["url"]),
                args.download_staging / f"{source['name']}.bin",
                expected_size=int(source["expected_size"]),
                expected_sha256=str(source["sha256"]),
                allowed_hosts=allowed,
                label=source["name"],
            )
    if args.counties is None or args.calibration is None or args.citations is None:
        raise HouseHunterError(
            "National generation requires --counties, --calibration, and --citations"
        )
    counties = _load_reviewed_counties(args.counties)
    calibration = json.loads(args.calibration.read_text())
    citations = json.loads(args.citations.read_text())
    internals = calibration.get("pillar_internals")
    if internals != PILLAR_INTERNALS:
        raise HouseHunterError("Calibration pillar internals differ from the locked v2 formula")
    for row in counties.iter_rows(named=True):
        expected = {
            "u_safety": pillar_utility(
                {
                    "hazard": row["u_hazard"],
                    "crime": row["u_crime"],
                    "water": row["u_water"],
                },
                internals["safety"],
            ),
            "u_health": pillar_utility(
                {
                    "healthcare": row["u_healthcare"],
                    "community_context": row["u_community_context"],
                },
                internals["health"],
            ),
            "u_opportunity": pillar_utility(
                {
                    "employment": row["u_employment"],
                    "broadband": row["u_broadband"],
                },
                internals["opportunity"],
            ),
            "u_lifestyle": row["u_mountain"],
            "u_family": row["homeschool_utility"],
        }
        for column, value in expected.items():
            observed = row[column]
            if value is None or observed is None:
                continue
            if abs(float(value) - float(observed)) > 1e-9:
                raise HouseHunterError(f"{column} drifted for {row['county_fips']}")
    write_ranking_bundle(
        args.output,
        counties,
        calibration=calibration,
        homeschool=load_homeschool_policy(),
        citations=citations,
        source_lock_path=args.source_lock,
        scope="national",
        vintages=lock.get("vintages") if isinstance(lock.get("vintages"), dict) else {},
    )
    validate_ranking_assets(args.output, source_lock_path=args.source_lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
