#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

import polars as pl

from househunter.config import sha256_file
from househunter.geography import STATE_BY_FIPS
from househunter.mountain import validate_national_expectations, write_release
from househunter.mountain_gis import (
    build_region_raw_metrics,
    load_regions,
    verify_region_sources_locked,
    verify_source_lock,
)
from househunter.mountain_pack import build_prepared_raw_metrics, verify_prepared_pack


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare national serial, prepared-one, and prepared-four Mountain output"
    )
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--regions", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--prepared-pack", required=True, type=Path)
    parser.add_argument("--prepared-lock", required=True, type=Path)
    parser.add_argument("--data-release", required=True)
    parser.add_argument("--report", type=Path, default=Path("data/mountain/equivalence.json"))
    args = parser.parse_args()
    lock = verify_source_lock(args.source_lock.resolve(), root=args.source_root.resolve())
    regions = load_regions(args.regions.resolve(), args.source_root.resolve())
    verify_region_sources_locked(regions, lock, root=args.source_root.resolve())
    pack = verify_prepared_pack(
        args.prepared_pack.resolve(),
        args.prepared_lock.resolve(),
        reviewed_source_lock_path=args.source_lock.resolve(),
        require_source_lock_v2=True,
    )
    serial = pl.concat(
        [build_region_raw_metrics(region, state_by_fips=STATE_BY_FIPS) for region in regions]
    ).sort("block_geoid")
    expectations = lock["expected_states"]
    validate_national_expectations(
        serial,
        expectations,
        expected_block_geoid_sha256=lock["block_geoid_sha256"],
    )
    scratch = Path(tempfile.mkdtemp(prefix="househunter-mountain-equivalence-"))
    try:
        one, _ = build_prepared_raw_metrics(
            args.prepared_pack.resolve(),
            args.prepared_lock.resolve(),
            scratch / ("1" * 64),
            workers=1,
            resume=False,
            verified_manifest=pack,
        )
        four, _ = build_prepared_raw_metrics(
            args.prepared_pack.resolve(),
            args.prepared_lock.resolve(),
            scratch / ("2" * 64),
            workers=4,
            resume=False,
            verified_manifest=pack,
        )
        if not serial.equals(one) or not serial.equals(four):
            raise RuntimeError("National raw tables differ across serial/prepared worker modes")
        sources = {
            **pack["source_provenance"],
            "source_lock_schema_version": pack["source_lock_schema_version"],
            "source_lock_sha256": pack["source_lock_sha256"],
            "prepared_pack": {
                "schema_version": 1,
                "pack_id": pack["pack_id"],
                "prepared_lock_sha256": sha256_file(args.prepared_lock.resolve()),
                "source_lock_sha256": pack["source_lock_sha256"],
            },
        }
        manifests = []
        for name, frame in (("serial", serial), ("one", one), ("four", four)):
            release = write_release(
                frame,
                scratch / f"release-{name}",
                data_release=args.data_release,
                sources=sources,
                national_expectations=expectations,
            )
            manifests.append(json.loads((release / "manifest.json").read_text()))
        identities = {manifest["release_id"] for manifest in manifests}
        hashes = {
            json.dumps(manifest["files"], sort_keys=True, separators=(",", ":"))
            for manifest in manifests
        }
        if len(identities) != 1 or len(hashes) != 1:
            raise RuntimeError("National releases differ across serial/prepared worker modes")
        report = {
            "schema_version": 1,
            "block_count": serial.height,
            "pack_id": pack["pack_id"],
            "release_id": manifests[0]["release_id"],
            "exact_raw_equality": True,
            "exact_release_equality": True,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(args.report)
    finally:
        shutil.rmtree(scratch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
