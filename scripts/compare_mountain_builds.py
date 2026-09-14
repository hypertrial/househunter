#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

import polars as pl

from househunter.config import RuntimePaths, canonical_json, sha256_bytes, sha256_file
from househunter.geography import STATE_BY_FIPS
from househunter.locking import exclusive_lock
from househunter.mountain import (
    MAGNITUDE_VERSION,
    RELEASE_SCHEMA_VERSION,
    validate_national_expectations,
    write_release,
)
from househunter.mountain_gis import (
    build_region_raw_metrics,
    load_regions,
    load_source_lock_contract,
    verify_region_sources_locked,
    verify_source_lock,
)
from househunter.mountain_pack import (
    build_prepared_raw_metrics,
    ensure_storage_budget,
    raw_metric_sha256,
    remove_owned_comparison_directory,
    remove_owned_work_directory,
    verify_prepared_pack,
)
from househunter.mountain_paths import ensure_owned_child, ensure_safe_directory


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare national serial, shuffled, prepared-one, and prepared-four "
            "Mountain Magnitude output"
        )
    )
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--regions", required=True, type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--reference-shards", type=Path)
    parser.add_argument("--prepared-pack", required=True, type=Path)
    parser.add_argument("--prepared-lock", required=True, type=Path)
    parser.add_argument("--data-release", required=True)
    parser.add_argument("--report", type=Path, default=Path("data/mountain/equivalence.json"))
    args = parser.parse_args()
    if bool(args.source_root) == bool(args.reference_shards):
        parser.error("provide exactly one of --source-root or --reference-shards")
    paths = RuntimePaths.from_root()
    with exclusive_lock(paths.job_lock):
        return _run_locked(args, paths)


def _run_locked(args: argparse.Namespace, paths: RuntimePaths) -> int:
    lock = (
        verify_source_lock(args.source_lock.resolve(), root=args.source_root.resolve())
        if args.source_root
        else load_source_lock_contract(args.source_lock.resolve(), require_v2=True)
    )
    if args.source_root and lock.get("trail_fragment_mode") == "state_clipped_globalid_v1":
        raise RuntimeError(
            "This national source contract requires source-derived reference shards so "
            "cross-state trail fragments are deduplicated"
        )
    pack = verify_prepared_pack(
        args.prepared_pack.resolve(),
        args.prepared_lock.resolve(),
        reviewed_source_lock_path=args.source_lock.resolve(),
        require_source_lock_v2=True,
    )
    if args.source_root:
        regions = load_regions(args.regions.resolve(), args.source_root.resolve())
        verify_region_sources_locked(regions, lock, root=args.source_root.resolve())
        serial = pl.concat(
            [build_region_raw_metrics(region, state_by_fips=STATE_BY_FIPS) for region in regions]
        ).sort("block_geoid")
    else:
        reference = args.reference_shards.resolve()
        prepared_lock = json.loads(args.prepared_lock.resolve().read_text())
        manifest = json.loads((reference / "manifest.json").read_text())
        if (
            manifest.get("schema_version") != 1
            or manifest.get("source_lock_sha256") != sha256_file(args.source_lock.resolve())
            or not isinstance(manifest.get("shards"), list)
            or prepared_lock.get("comparison_id") != reference.name
            or prepared_lock.get("comparison_manifest_sha256")
            != sha256_file(reference / "manifest.json")
        ):
            raise RuntimeError("Source-derived comparison manifest is incompatible")
        shards = []
        for item in manifest["shards"]:
            shard = reference / str(item["filename"])
            if shard.is_symlink() or sha256_file(shard) != item.get("parquet_sha256"):
                raise RuntimeError("Source-derived comparison shard is corrupt")
            frame = pl.read_parquet(shard)
            if raw_metric_sha256(frame) != item.get("raw_metric_sha256"):
                raise RuntimeError("Source-derived comparison shard content differs")
            shards.append(frame)
        serial = pl.concat(shards).sort("block_geoid")
        if serial.height != manifest.get("block_count") or sha256_bytes(
            canonical_json([item.get("raw_metric_sha256") for item in manifest["shards"]])
        ) != manifest.get("raw_metric_sha256"):
            raise RuntimeError("Source-derived comparison manifest does not match its shards")
    expectations = lock["expected_states"]
    validate_national_expectations(
        serial,
        expectations,
        expected_block_geoid_sha256=lock["block_geoid_sha256"],
    )
    managed_root = ensure_safe_directory(paths.data / "mountain")
    work_root = ensure_safe_directory(managed_root / "work")
    scratch = ensure_owned_child(
        work_root / uuid.uuid4().hex,
        work_root,
        name_pattern=r"[0-9a-f]{32}",
        marker_value="equivalence-v2\n",
    )
    ensure_storage_budget(managed_root, reserve_bytes=8_100_000_000)
    try:
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
        for index, (name, workers) in enumerate(
            (("serial", None), ("shuffled", 0), ("one", 1), ("four", 4)), 1
        ):
            work = scratch / (str(index) * 64)
            frame = (
                serial.sample(fraction=1.0, shuffle=True, seed=20260914) if workers == 0 else serial
            )
            if workers == 0 and not serial.equals(frame.sort("block_geoid")):
                raise RuntimeError("Shuffled national raw table changed canonical content")
            if workers not in {None, 0}:
                frame, _ = build_prepared_raw_metrics(
                    args.prepared_pack.resolve(),
                    args.prepared_lock.resolve(),
                    work,
                    workers=workers,
                    resume=False,
                    verified_manifest=pack,
                    managed_root=managed_root,
                )
                if not serial.equals(frame):
                    raise RuntimeError(
                        f"National raw table differs for prepared {name}-worker mode"
                    )
            release = write_release(
                frame,
                scratch / f"release-{name}",
                data_release=args.data_release,
                sources=sources,
                national_expectations=expectations,
            )
            manifests.append(json.loads((release / "manifest.json").read_text()))
            if workers not in {None, 0}:
                remove_owned_work_directory(work, scratch)
            if release.parent != scratch or release.is_symlink():
                raise RuntimeError("Equivalence release escaped managed scratch")
            shutil.rmtree(release)
        identities = {manifest["release_id"] for manifest in manifests}
        hashes = {
            json.dumps(manifest["files"], sort_keys=True, separators=(",", ":"))
            for manifest in manifests
        }
        if len(identities) != 1 or len(hashes) != 1:
            raise RuntimeError(
                "National releases differ across row ordering or prepared worker modes"
            )
        if any(
            manifest.get("schema_version") != RELEASE_SCHEMA_VERSION
            or manifest.get("magnitude_version") != MAGNITUDE_VERSION
            or manifest.get("magnitude_contract", {}).get("uncapped") is not True
            or manifest.get("magnitude_contract", {}).get("tie_rule") != "inclusive_equal_or_higher"
            for manifest in manifests
        ):
            raise RuntimeError("National releases do not carry the Mountain Magnitude v2 contract")
        report = {
            "schema_version": 2,
            "block_count": serial.height,
            "pack_id": pack["pack_id"],
            "release_id": manifests[0]["release_id"],
            "mountain_release_schema_version": RELEASE_SCHEMA_VERSION,
            "mountain_magnitude_version": MAGNITUDE_VERSION,
            "exact_raw_equality": True,
            "exact_release_equality": True,
            "row_order_independent": True,
            "worker_count_independent": True,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(args.report)
        if args.reference_shards:
            comparison_id = str(
                json.loads(args.prepared_lock.resolve().read_text())["comparison_id"]
            )
            reference = args.reference_shards.resolve()
            expected = work_root / comparison_id
            if reference == expected:
                remove_owned_comparison_directory(
                    reference,
                    work_root,
                    expected_id=comparison_id,
                )
    finally:
        remove_owned_work_directory(scratch, work_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
