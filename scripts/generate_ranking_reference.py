#!/usr/bin/env python3
"""Maintainer-only generator for the compact ranking_v2 county bundle.

Ranking RPP geography is produced by assign_counties_v2: MSA MARPP, or official
state all-items RPP labeled `state`. This generator packages reviewed artifacts
and does not fetch Census, BEA, FBI, FCC, HRSA, EPA, or NOAA at runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

import polars as pl

from househunter.config import sha256_file
from househunter.errors import HouseHunterError
from househunter.ranking_pipeline import build_ranking_counties
from househunter.ranking_reference import (
    COUNTY_COLUMNS,
    CRIME_COVERAGE_FLOOR,
    HOUSING_SQFT_BOUNDS,
    HOUSING_SQFT_KNOTS,
    PILLAR_INTERNALS,
    default_source_lock_path,
    ecdf_calibration_audit,
    load_homeschool_policy,
    load_source_lock,
    pillar_utility,
    validate_ranking_assets,
    write_ranking_bundle,
    write_synthetic_fixture_bundle,
)
from househunter.secure_fetch import deny_restricted_data_class, download_locked_file


def _checkpoint_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".checkpoint.json")


def _artifact_is_current(path: Path, artifact: dict[str, object], lock_sha: str) -> bool:
    checkpoint_path = _checkpoint_path(path)
    if (
        path.is_symlink()
        or checkpoint_path.is_symlink()
        or not path.is_file()
        or not checkpoint_path.is_file()
    ):
        return False
    expected = {
        "schema": 1,
        "source_lock_sha256": lock_sha,
        "filename": artifact["filename"],
        "bytes": artifact["bytes"],
        "sha256": artifact["sha256"],
    }
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        checkpoint == expected
        and path.stat().st_size == artifact["bytes"]
        and sha256_file(path) == artifact["sha256"]
    )


def _write_artifact_checkpoint(path: Path, artifact: dict[str, object], lock_sha: str) -> None:
    checkpoint = {
        "schema": 1,
        "source_lock_sha256": lock_sha,
        "filename": artifact["filename"],
        "bytes": artifact["bytes"],
        "sha256": artifact["sha256"],
    }
    destination = _checkpoint_path(path)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    temporary.write_bytes(json.dumps(checkpoint, indent=2, sort_keys=True).encode() + b"\n")
    os.replace(temporary, destination)


def _parse_manual_stages(values: list[str]) -> dict[str, Path]:
    stages: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not raw_path or name in stages:
            raise HouseHunterError("--stage must be a unique SOURCE=PATH pair")
        stages[name] = Path(raw_path)
    return stages


def _acquire_artifacts(
    *,
    lock: dict[str, object],
    lock_path: Path,
    data_root: Path,
    manual_stages: dict[str, Path],
) -> None:
    allowed_hosts = {str(host).lower() for host in lock["allowed_hosts"]}
    lock_sha = sha256_file(lock_path)
    used_stages: set[str] = set()
    for source in lock["sources"]:
        name = str(source["name"])
        artifacts = source.get("artifacts") or []
        for artifact in artifacts:
            filename = str(artifact["filename"])
            destination = data_root / "raw" / name / filename
            stage_key = f"{name}/{filename}"
            staged = manual_stages.get(stage_key)
            selected_stage_key = stage_key
            if staged is None and len(artifacts) == 1:
                staged = manual_stages.get(name)
                selected_stage_key = name
            if staged is not None:
                if staged.is_symlink() or not staged.is_file():
                    raise HouseHunterError(f"Manual ranking source {name} must be a regular file")
                if (
                    staged.stat().st_size != artifact["bytes"]
                    or sha256_file(staged) != artifact["sha256"]
                ):
                    raise HouseHunterError(f"Manual ranking source {name} differs from its lock")
            if _artifact_is_current(destination, artifact, lock_sha):
                if staged is not None:
                    used_stages.add(selected_stage_key)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            url = artifact.get("url")
            if staged is not None:
                temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
                try:
                    shutil.copyfile(staged, temporary)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
                used_stages.add(selected_stage_key)
            elif url is not None:
                download_locked_file(
                    str(url),
                    destination,
                    expected_size=int(artifact["bytes"]),
                    expected_sha256=str(artifact["sha256"]),
                    allowed_hosts=allowed_hosts,
                    max_bytes=int(artifact["bytes"]),
                    label=f"Ranking source {name}",
                )
            else:
                raise HouseHunterError(f"Ranking source {name} requires --stage {stage_key}=PATH")
            _write_artifact_checkpoint(destination, artifact, lock_sha)
    unused = sorted(set(manual_stages) - used_stages)
    if unused:
        raise HouseHunterError("Unused manual ranking stages: " + ", ".join(unused))


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
    return frame.select(COUNTY_COLUMNS).sort("county_fips")


def _source_citations(lock: dict[str, object]) -> dict[str, str]:
    sources = {source["name"]: source for source in lock["sources"]}

    def cite(*names: str) -> str:
        return "; ".join(
            f"{sources[name]['agency']} {sources[name]['vintage']} — {sources[name]['terms_url']}"
            for name in names
        )

    return {
        "population": cite("census_pep_county_2025"),
        "hazard": cite("fema_residential_hazard"),
        "crime": cite("fbi_ucr_agency_2023_2025"),
        "water": cite("epa_sdwis_2026_q2", "epa_cws_service_areas_v2_1"),
        "healthcare": cite("hrsa_ahrf_2024_2025", "chrr_nppes_mental_health_2025"),
        "community_context": cite("chrr_community_context_2025"),
        "rpp": cite("bea_marpp_2024", "bea_sarpp_2024", "omb_county_cbsa_2023"),
        "property_tax": cite("acs_property_tax_2024"),
        "employment": cite("bls_qcew_2024", "bls_qcew_2025", "acs_commute_2024"),
        "broadband": cite("fcc_fixed_summary_2025_12"),
        "mountain": cite("househunter_mountain_v2"),
        "climate": cite("noaa_normals_1991_2020", "census_tiger_county_2025"),
        "homeschool": cite("homeschool_policy_v1"),
    }


def _national_calibration(counties: pl.DataFrame) -> dict[str, object]:
    return {
        "housing_sqft_bounds": HOUSING_SQFT_BOUNDS,
        "housing_sqft_knots": HOUSING_SQFT_KNOTS,
        "crime_coverage_floor": CRIME_COVERAGE_FLOOR,
        "crime_component_weights": {"violent": 0.60, "property": 0.40},
        "water_allocation_coverage_floor": 0.90,
        "provider_component_weights": {
            "primary_care": 1 / 3,
            "mental_health": 1 / 3,
            "dental": 1 / 3,
        },
        "employment_component_weights": {
            "growth": 0.50,
            "weekly_wage": 0.25,
            "commute_under_30": 0.25,
        },
        "community_context_formula": "(10 - published_group) / 9",
        "broadband_denominator": "broadband-serviceable locations",
        "climate_aggregation": "equal-weight complete stations strictly within current county",
        "pillar_internals": PILLAR_INTERNALS,
        "ecdf_components": ecdf_calibration_audit(counties),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path)
    parser.add_argument("--counties", type=Path, help="Reviewed compact county parquet")
    parser.add_argument("--calibration", type=Path, help="Reviewed calibration JSON")
    parser.add_argument("--citations", type=Path, help="Reviewed citations JSON")
    parser.add_argument(
        "--build-national",
        action="store_true",
        help="Run the locked maintainer ETL before publishing the national bundle",
    )
    parser.add_argument("--snapshot", type=Path, help="Validated national snapshot input")
    parser.add_argument("--fixture", action="store_true", help="Write the synthetic test fixture")
    parser.add_argument("--acquire", action="store_true", help="Acquire all locked artifacts")
    parser.add_argument("--data-root", type=Path, default=Path("data/ranking-v2"))
    parser.add_argument(
        "--stage",
        action="append",
        default=[],
        metavar="SOURCE=PATH",
        help="Provide a locked SOURCE=PATH or SOURCE/FILENAME=PATH artifact",
    )
    args = parser.parse_args()
    if args.fixture:
        write_synthetic_fixture_bundle(args.output)
        validate_ranking_assets(args.output)
        return 0
    lock_path = args.source_lock or default_source_lock_path()
    lock = load_source_lock(lock_path)
    for source in lock["sources"]:
        deny_restricted_data_class(source.get("data_class"), label=source["name"])
        if source.get("runtime_fetch") is not False:
            raise HouseHunterError(f"{source['name']} must remain maintainer-only")
    if args.acquire:
        _acquire_artifacts(
            lock=lock,
            lock_path=lock_path,
            data_root=args.data_root,
            manual_stages=_parse_manual_stages(args.stage),
        )
        if args.counties is None and not args.build_national:
            return 0
    if args.build_national:
        if args.snapshot is None:
            raise HouseHunterError("--build-national requires --snapshot")
        counties = build_ranking_counties(
            data_root=args.data_root,
            snapshot=args.snapshot,
            source_lock_path=lock_path,
        )
        calibration = _national_calibration(counties)
        citations = _source_citations(lock)
    elif args.counties is None or args.calibration is None or args.citations is None:
        raise HouseHunterError(
            "National generation requires --build-national or reviewed "
            "--counties, --calibration, and --citations"
        )
    else:
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
        source_lock_path=lock_path,
        scope="national",
        vintages={source["name"]: source["vintage"] for source in lock["sources"]},
    )
    validate_ranking_assets(args.output, source_lock_path=lock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
