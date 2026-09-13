from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import stat
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import numpy as np
import polars as pl
import pyogrio
import pytest
import rasterio
import shapely
from pyproj import CRS, Transformer
from rasterio.enums import MergeAlg
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import LineString, MultiLineString, Polygon
from typer.testing import CliRunner

from househunter.cli import app
from househunter.config import RuntimePaths, canonical_json, sha256_bytes, sha256_file
from househunter.errors import HouseHunterError
from househunter.geography import STATE_BY_FIPS
from househunter.mountain import IN_SCOPE_STATES
from househunter.mountain_gis import (
    HAWAII_WKT2_2019,
    RegionSources,
    _download_arcgis_snapshot,
    _query_bounds,
    _raw_metrics_from_tile,
    _read_geometries,
    _source_download_reservation,
    _validate_public_dns,
    _validate_source_lock_contract,
    _verify_gis_dataset,
    build_region_raw_metrics,
    delete_managed_source_family,
    download_sources,
    extract_locked_archive,
    load_regions,
    qualify_national_inventory,
    read_tile_pad,
    read_tile_trails,
    source_provenance_item,
    storage_projection,
    verify_region_sources_locked,
    verify_source_lock,
)
from househunter.mountain_pack import (
    _complete_phase_sources,
    _file_metadata,
    _finalize_trail_fragment_store,
    _ingest_trail_fragment_batch,
    _initialize_trails_progress,
    _materialize_elevation_batch,
    _materialize_fragment_trails,
    _materialize_trails_batch,
    _query_trail_fragment_wkbs,
    _source_family_digest,
    _source_names_digest,
    _valid_phase,
    _write_json,
    _write_phase,
    block_sample_sha256,
    build_prepared_raw_metrics,
    prepare_regions,
    prune_owned_preparation_workspaces,
    raw_metric_sha256,
    remove_owned_comparison_directory,
    verify_prepared_pack,
)
from househunter.mountain_paths import ensure_owned_child


def _geojson(path: Path, geometry: dict, properties: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "geometry": geometry, "properties": properties}],
            }
        )
    )


def test_download_reservation_checks_hash_and_remaining_extraction(tmp_path: Path) -> None:
    destination = tmp_path / "downloads"
    destination.mkdir()
    target = destination / "source.bin"
    expected = b"expected"
    source = {
        "family": "elevation",
        "filename": target.name,
        "size": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
        "archive": {"root": "expanded", "total_uncompressed_size": 20},
    }
    lock = {"sources": [source]}

    target.write_bytes(b"corrupt!")
    assert _source_download_reservation(lock, destination) == len(expected) + 20

    target.write_bytes(expected)
    assert _source_download_reservation(lock, destination) == 20

    (destination / "expanded").mkdir()
    assert _source_download_reservation(lock, destination) == 0


def test_download_rejects_matching_symlink_without_reading_or_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"pinned"
    destination = tmp_path / "downloads"
    destination.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(content)
    target = destination / "source.bin"
    target.symlink_to(outside)
    source = {
        "family": "elevation",
        "filename": target.name,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "url": "https://example.test/source.bin",
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps({"schema_version": 2, "sources": [source]}))
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=content)

    monkeypatch.setattr(
        "househunter.mountain_gis._validate_source_lock_contract",
        lambda lock: {"example.test"},
    )
    monkeypatch.setattr("househunter.mountain_gis.verify_source_lock", lambda *args, **kwargs: {})
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="symlink"),
    ):
        download_sources(lock_path, destination, client=client)

    assert requests == 0
    assert target.is_symlink()
    assert outside.read_bytes() == content


def test_managed_download_stops_before_network_when_quota_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "downloads"
    source = {
        "family": "elevation",
        "filename": "source.bin",
        "size": 6,
        "sha256": hashlib.sha256(b"source").hexdigest(),
        "url": "https://example.test/source.bin",
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "storage_projection": {"source_staging_bytes": 6},
                "sources": [source],
            }
        )
    )
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"source")

    monkeypatch.setattr(
        "househunter.mountain_gis._validate_source_lock_contract",
        lambda lock: {"example.test"},
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.ensure_storage_budget",
        lambda *args, **kwargs: (_ for _ in ()).throw(HouseHunterError("quota exhausted")),
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="quota exhausted"),
    ):
        download_sources(
            lock_path,
            destination,
            client=client,
            managed_root=tmp_path / "mountain",
            families={"elevation"},
        )

    assert requests == 0
    assert not (destination / "source.bin").exists()


def test_source_lock_rejects_changed_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"pinned")
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "name": "fixture",
                        "url": "https://example.test/source.bin",
                        "acquired_at": "2026-09-12T00:00:00Z",
                        "crs": "EPSG:5070",
                        "schema": {"value": "bytes"},
                        "count": 1,
                        "path": str(source),
                        "size": source.stat().st_size,
                        "sha256": sha256_file(source),
                    }
                ],
            }
        )
    )
    assert verify_source_lock(lock)["schema_version"] == 1
    source.write_bytes(b"changed")
    try:
        verify_source_lock(lock)
    except HouseHunterError as exc:
        assert "checksum mismatch" in str(exc)
    else:
        raise AssertionError("changed source bytes were accepted")


def _v2_source_lock(sources: list[dict[str, object]]) -> dict[str, object]:
    for source in sources:
        source.setdefault("release", "fixture-v1")
        source.setdefault("license", "public domain")
        source.setdefault("crs", "EPSG:5070")
        source.setdefault("schema", _fixture_gis_schema(str(source.get("filename", ""))))
        source.setdefault("count", 1)
        if geometry_type := _geometry_type(str(source.get("family", ""))):
            source.setdefault("geometry_type", geometry_type)
    projection = storage_projection(
        sources,
        prepared_pack_bytes=1,
        preparation_workspace_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        compact_bundle_bytes=1,
        report_bytes=1,
        safety_reserve_bytes=7_000_000_000,
        maximum_atomic_write_bytes=1,
    )
    return {
        "schema_version": 2,
        "allowed_hosts": ["prd-tnm.s3.amazonaws.com"],
        "expected_states": {
            state: {"blocks": 1, "population": 1} for state in sorted(IN_SCOPE_STATES)
        },
        "block_geoid_sha256": "0" * 64,
        "region_crs": {
            "conus_dc": "EPSG:5070",
            "alaska": "EPSG:3338",
            "hawaii": HAWAII_WKT2_2019,
        },
        "elevation_precedence": ["elevation"],
        "tile_inventory": {
            "tile_count": 6,
            "block_count": 51,
            "tiles": [
                {
                    "region": region,
                    "tile_x": index,
                    "tile_y": 0,
                    "blocks": 46 if index == 0 else 1,
                    "population": 46 if index == 0 else 1,
                    "block_geoid_sha256": "0" * 64,
                    "block_sample_sha256": "0" * 64,
                }
                for index, region in enumerate(
                    ("alaska", "conus_dc", "conus_dc", "conus_dc", "conus_dc", "hawaii")
                )
            ],
        },
        "representative_tiles": [
            {
                "case": case,
                "region": region,
                "tile_x": index,
                "tile_y": 0,
                "raw_metric_sha256": "0" * 64,
            }
            for case, region, index in (
                ("coastal_nodata", "conus_dc", 1),
                ("colorado_rockies", "conus_dc", 2),
                ("flat_plains", "conus_dc", 3),
                ("overlap_seam", "conus_dc", 4),
                ("alaska", "alaska", 0),
                ("hawaii", "hawaii", 5),
            )
        ],
        "storage_projection": projection,
        "sources": sources,
    }


def _representative_tiles(
    *, region: str, tile_x: int, tile_y: int, digest: str
) -> list[dict[str, object]]:
    return [
        {
            "case": case,
            "region": region,
            "tile_x": tile_x,
            "tile_y": tile_y,
            "raw_metric_sha256": digest,
        }
        for case in sorted(
            {
                "alaska",
                "coastal_nodata",
                "colorado_rockies",
                "flat_plains",
                "hawaii",
                "overlap_seam",
            }
        )
    ]


def _fixture_gis_schema(filename: str) -> dict[str, object]:
    if filename.endswith((".tif", ".tiff")):
        return {
            "bands": 1,
            "dtypes": ["float32"],
            "nodata": None,
            "width": 1,
            "height": 1,
        }
    return {"fixture": "object"}


def _geometry_type(family: str) -> str | None:
    return {
        "blocks": "Polygon",
        "pad_us": "MultiPolygon",
        "trails": "LineString",
    }.get(family)


def _batched_v2_source_lock() -> dict[str, object]:
    sources = []
    for name, family, filename, size in (
        ("blocks", "blocks", "blocks.fgb", 11),
        ("dem-west", "elevation", "dem-west.tif", 17),
        ("dem-east", "elevation", "dem-east.tif", 19),
        ("pad_us", "pad_us", "pad.gpkg", 23),
        ("trails", "trails", "trails.fgb", 29),
    ):
        source = {
            "name": name,
            "family": family,
            "filename": filename,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-09-12T00:00:00Z",
            "size": size,
            "sha256": "0" * 64,
        }
        sources.append(source)
    payload = _v2_source_lock(sources)
    payload["elevation_precedence"] = ["dem-west", "dem-east"]
    tile_keys = [
        f"{tile['region']}:{tile['tile_x']}:{tile['tile_y']}"
        for tile in payload["tile_inventory"]["tiles"]
    ]
    payload["preparation_batches"] = [
        {
            "id": "elevation-001",
            "family": "elevation",
            "sources": ["dem-west"],
            "tile_keys": tile_keys[:3],
            "source_staging_bytes": 17,
            "maximum_part_bytes": 17,
        },
        {
            "id": "elevation-002",
            "family": "elevation",
            "sources": ["dem-east"],
            "tile_keys": tile_keys[3:],
            "source_staging_bytes": 19,
            "maximum_part_bytes": 19,
        },
    ]
    payload["storage_projection"] = storage_projection(
        sources,
        preparation_batches=payload["preparation_batches"],
        prepared_pack_bytes=1,
        preparation_workspace_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        compact_bundle_bytes=1,
        report_bytes=1,
        safety_reserve_bytes=7_000_000_000,
        maximum_atomic_write_bytes=1,
    )
    return payload


def _trails_batch_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    RegionSources,
    list[dict[str, object]],
    dict[str, object],
    list[dict[str, object]],
]:
    workspace = tmp_path / "workspace"
    tile_key = "f" * 24
    (workspace / "tiles" / tile_key).mkdir(parents=True)
    (workspace / "checkpoints").mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    paths = (source_root / "west.fgb", source_root / "east.fgb")
    for path in paths:
        path.write_bytes(path.stem.encode())
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=source_root / "blocks.fgb",
        elevation=(),
        pad_us=source_root / "pad.gpkg",
        trails=paths,
    )
    entries = [
        {
            "key": tile_key,
            "region": "conus_dc",
            "tile_x": 0,
            "tile_y": 0,
            "shape": [2, 2],
            "bounds": [0.0, 0.0, 2.0, 2.0],
            "transform": [1.0, 0.0, 0.0, 0.0, -1.0, 2.0],
        }
    ]
    source_lock = {
        "sources": [
            {
                "name": name,
                "family": "trails",
                "filename": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for name, path in zip(("west", "east"), paths, strict=True)
        ]
    }
    batches = [
        {
            "id": f"trails-{index:03d}",
            "family": "trails",
            "sources": [name],
            "tile_keys": ["conus_dc:0:0"],
        }
        for index, name in enumerate(("west", "east"), start=1)
    ]
    return workspace, region, entries, source_lock, batches


def _state_clipped_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    RegionSources,
    list[dict[str, object]],
    dict[str, object],
    list[dict[str, object]],
]:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    region = RegionSources(**{**region.__dict__, "target_crs": "EPSG:4269"})
    for batch in batches:
        batch.pop("tile_keys")
    return workspace, region, entries, source_lock, batches


def test_source_lock_v2_requires_reviewed_https_and_national_digests() -> None:
    source_specs = [
        ("blocks", "blocks.fgb"),
        ("elevation", "elevation.tif"),
        ("pad_us", "pad.gpkg"),
        ("trails", "trails.fgb"),
    ]
    sources = []
    for family, filename in source_specs:
        source = {
            "name": family,
            "family": family,
            "filename": filename,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-09-12T00:00:00Z",
            "crs": "EPSG:5070",
            "schema": _fixture_gis_schema(filename),
            "count": 1,
            "size": 1,
            "sha256": "0" * 64,
        }
        if geometry_type := _geometry_type(family):
            source["geometry_type"] = geometry_type
        sources.append(source)
    payload = _v2_source_lock(sources)
    assert set(STATE_BY_FIPS.values()) >= set(payload["expected_states"])
    assert _validate_source_lock_contract(payload) == {"prd-tnm.s3.amazonaws.com"}

    payload["sources"][0]["url"] += "?token=secret"
    with pytest.raises(HouseHunterError, match="HTTPS host policy"):
        _validate_source_lock_contract(payload)


def test_source_dns_rejects_private_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "househunter.mountain_gis.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )

    with pytest.raises(HouseHunterError, match="non-public"):
        _validate_public_dns("https://prd-tnm.s3.amazonaws.com/source.tif")


def test_source_provenance_is_allowlisted_and_secret_fields_are_rejected() -> None:
    source = {
        "name": "elevation",
        "family": "elevation",
        "filename": "elevation.tif",
        "url": "https://prd-tnm.s3.amazonaws.com/elevation.tif",
        "acquired_at": "2026-09-12T00:00:00Z",
        "release": "pinned",
        "license": "public domain",
        "crs": "EPSG:5070",
        "schema": _fixture_gis_schema("elevation.tif"),
        "count": 1,
        "size": 1,
        "sha256": "0" * 64,
        "local_note": "must not ship",
    }
    assert "local_note" not in source_provenance_item(source)
    assert "url" not in source_provenance_item(source)

    sources = [
        source,
        *[
            {
                "name": family,
                "family": family,
                "filename": filename,
                "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
                "acquired_at": "2026-09-12T00:00:00Z",
                "crs": "EPSG:5070",
                "schema": _fixture_gis_schema(filename),
                "geometry_type": _geometry_type(family),
                "count": 1,
                "size": 1,
                "sha256": "0" * 64,
            }
            for family, filename in (
                ("blocks", "blocks.fgb"),
                ("pad_us", "pad.gpkg"),
                ("trails", "trails.fgb"),
            )
        ],
    ]
    payload = _v2_source_lock(sources)
    payload["sources"][0]["metadata"] = {"api_token": "do-not-publish"}
    with pytest.raises(HouseHunterError, match="secret-bearing"):
        _validate_source_lock_contract(payload)


def test_source_lock_v2_recomputes_every_storage_peak() -> None:
    sources = [
        {
            "name": family,
            "family": family,
            "filename": filename,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-09-12T00:00:00Z",
            "crs": "EPSG:5070",
            "schema": _fixture_gis_schema(filename),
            "count": 1,
            "size": index + 1,
            "sha256": "0" * 64,
            **(
                {"geometry_type": geometry_type}
                if (geometry_type := _geometry_type(family))
                else {}
            ),
        }
        for index, (family, filename) in enumerate(
            (
                ("blocks", "blocks.fgb"),
                ("elevation", "elevation.tif"),
                ("pad_us", "pad.gpkg"),
                ("trails", "trails.fgb"),
            )
        )
    ]
    payload = _v2_source_lock(sources)
    _validate_source_lock_contract(payload)

    payload["storage_projection"]["maximum_part_bytes"] -= 1
    with pytest.raises(HouseHunterError, match="storage projection"):
        _validate_source_lock_contract(payload)

    payload = _v2_source_lock(sources)
    payload["representative_tiles"][0]["raw_metric_sha256"] = "not-a-digest"
    with pytest.raises(HouseHunterError, match="representative tile"):
        _validate_source_lock_contract(payload)


def test_source_lock_v2_rejects_colliding_archive_roots() -> None:
    sources = []
    for family, filename in (
        ("blocks", "blocks.zip"),
        ("elevation", "elevation.tif"),
        ("pad_us", "pad.gpkg"),
        ("trails", "trails.zip"),
    ):
        source = {
            "name": family,
            "family": family,
            "filename": filename,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-09-12T00:00:00Z",
            "release": "fixture-v1",
            "license": "public domain",
            "crs": "EPSG:5070",
            "schema": _fixture_gis_schema(filename),
            "count": 1,
            "size": 1,
            "sha256": "0" * 64,
        }
        if filename.endswith(".zip"):
            dataset = f"data/{family}.fgb"
            source["archive"] = {
                "format": "zip",
                "root": "same-root",
                "datasets": [dataset],
                "total_uncompressed_size": 1,
                "members": [{"path": dataset, "size": 1, "sha256": "0" * 64}],
                "dataset_contracts": [
                    {
                        "path": dataset,
                        "crs": "EPSG:5070",
                        "schema": {"value": "String"},
                        "count": 1,
                        "geometry_type": "Polygon" if family == "blocks" else "LineString",
                    }
                ],
            }
        elif family != "elevation":
            source["geometry_type"] = "Polygon"
        sources.append(source)
    payload = _v2_source_lock(sources)
    payload["elevation_precedence"] = ["elevation"]
    payload["storage_projection"] = storage_projection(
        sources,
        prepared_pack_bytes=1,
        preparation_workspace_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        compact_bundle_bytes=1,
        report_bytes=1,
        safety_reserve_bytes=7_000_000_000,
        maximum_atomic_write_bytes=1,
    )

    with pytest.raises(HouseHunterError, match="required source families"):
        _validate_source_lock_contract(payload)

    payload = _v2_source_lock(sources)
    first = payload["representative_tiles"][0]
    for item in payload["representative_tiles"]:
        item.update(region=first["region"], tile_x=first["tile_x"], tile_y=first["tile_y"])
    with pytest.raises(HouseHunterError, match="representative tile"):
        _validate_source_lock_contract(payload)


def test_storage_projection_accounts_for_parts_extraction_and_atomic_preparation() -> None:
    sources = [
        {"size": 11, "archive": {"total_uncompressed_size": 101}},
        {"size": 17, "archive": {"total_uncompressed_size": 103}},
    ]

    projection = storage_projection(
        sources,
        prepared_pack_bytes=19,
        preparation_workspace_bytes=23,
        maximum_atomic_write_bytes=29,
        comparison_shard_bytes=31,
        active_and_rollback_release_bytes=37,
        candidate_release_bytes=41,
        work_shard_bytes=43,
        compact_bundle_bytes=47,
        report_bytes=53,
        retained_baseline_bytes=61,
        safety_reserve_bytes=59,
    )

    assert projection["source_staging_bytes"] == 11 + 17 + 101 + 103
    assert projection["maximum_part_bytes"] == 17
    assert projection["acquisition_peak_bytes"] == 61 + 11 + 17 + 101 + 103 + 17 + 59
    assert projection["preparation_peak_bytes"] == 61 + 11 + 17 + 101 + 103 + 23 + 29 + 31 + 59
    assert projection["timed_build_peak_bytes"] == 19 + 37 + 41 + 43 + 47 + 53 + 59


def test_storage_projection_uses_single_family_staging_peak() -> None:
    sources = [
        {"family": "blocks", "size": 11, "archive": {"total_uncompressed_size": 101}},
        {"family": "blocks", "size": 17, "archive": {"total_uncompressed_size": 103}},
        {"family": "trails", "size": 29, "archive": {"total_uncompressed_size": 307}},
    ]

    projection = storage_projection(
        sources,
        prepared_pack_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        safety_reserve_bytes=7_000_000_000,
    )

    assert projection["compressed_download_bytes"] == 57
    assert projection["extracted_source_bytes"] == 511
    assert projection["source_staging_bytes"] == 29 + 307


def test_storage_projection_uses_largest_declared_batch_not_whole_family() -> None:
    sources = [
        {"name": "blocks", "family": "blocks", "size": 50},
        {
            "name": "dem-west",
            "family": "elevation",
            "size": 100,
            "archive": {"total_uncompressed_size": 900},
        },
        {
            "name": "dem-east",
            "family": "elevation",
            "size": 200,
            "archive": {"total_uncompressed_size": 800},
        },
    ]
    batches = [
        {"family": "elevation", "sources": ["dem-west"]},
        {"family": "elevation", "sources": ["dem-east"]},
    ]

    projection = storage_projection(
        sources,
        preparation_batches=batches,
        prepared_pack_bytes=1,
        preparation_workspace_bytes=2,
        active_and_rollback_release_bytes=3,
        candidate_release_bytes=4,
        work_shard_bytes=5,
        comparison_shard_bytes=6,
        safety_reserve_bytes=7,
        maximum_atomic_write_bytes=8,
    )

    assert projection["compressed_download_bytes"] == 350
    assert projection["extracted_source_bytes"] == 1_700
    assert projection["maximum_part_bytes"] == 200
    assert projection["source_staging_bytes"] == 1_000
    assert projection["acquisition_peak_bytes"] == 1_207
    assert projection["preparation_peak_bytes"] == 1_023


def test_storage_projection_counts_shared_batch_sources_until_their_last_use() -> None:
    sources = [
        {"name": "first", "family": "elevation", "size": 100},
        {"name": "shared", "family": "elevation", "size": 200},
        {"name": "middle", "family": "elevation", "size": 300},
        {"name": "last", "family": "elevation", "size": 400},
    ]
    batches = [
        {"family": "elevation", "sources": ["first", "shared"]},
        {"family": "elevation", "sources": ["middle"]},
        {"family": "elevation", "sources": ["shared", "last"]},
    ]

    projection = storage_projection(
        sources,
        preparation_batches=batches,
        prepared_pack_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        safety_reserve_bytes=1,
    )

    # Before the third checkpoint, the shared source is retained and counted once.
    assert projection["source_staging_bytes"] == 200 + 400


def test_storage_projection_accepts_reviewed_phase_overlap_without_combining_phase_maxima() -> None:
    sources = [
        {"name": "blocks", "family": "blocks", "size": 1_000},
        {"name": "elevation", "family": "elevation", "size": 100},
    ]
    projection = storage_projection(
        sources,
        prepared_pack_bytes=800,
        preparation_workspace_bytes=800,
        preparation_overlap_bytes=1_100,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=200,
        safety_reserve_bytes=7,
        maximum_atomic_write_bytes=3,
    )

    assert projection["source_staging_bytes"] == 1_000
    assert projection["preparation_overlap_bytes"] == 1_100
    assert projection["preparation_peak_bytes"] == 1_110
    with pytest.raises(HouseHunterError, match="understates concurrent storage"):
        storage_projection(
            sources,
            prepared_pack_bytes=800,
            preparation_workspace_bytes=800,
            preparation_overlap_bytes=999,
            active_and_rollback_release_bytes=1,
            candidate_release_bytes=1,
            work_shard_bytes=1,
            comparison_shard_bytes=200,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing_source", "incomplete or overlapping"),
        ("overlapping_tile", "incomplete or overlapping"),
        ("wrong_family", "invalid preparation batch"),
    ),
)
def test_source_lock_rejects_incomplete_overlapping_or_cross_family_batches(
    mutation: str, message: str
) -> None:
    payload = _batched_v2_source_lock()
    batches = payload["preparation_batches"]
    if mutation == "missing_source":
        batches[1]["sources"] = []
        batches[1]["source_staging_bytes"] = 0
        batches[1]["maximum_part_bytes"] = 0
    elif mutation == "overlapping_tile":
        batches[1]["tile_keys"].append(batches[0]["tile_keys"][0])
    else:
        batches[0]["sources"] = ["trails"]

    with pytest.raises(HouseHunterError, match=message):
        _validate_source_lock_contract(payload)


def test_source_lock_allows_reused_elevation_source_across_disjoint_tile_batches() -> None:
    payload = _batched_v2_source_lock()
    payload["preparation_batches"][1]["sources"] = ["dem-west", "dem-east"]
    payload["preparation_batches"][1]["source_staging_bytes"] = 36
    payload["preparation_batches"][1]["maximum_part_bytes"] = 19
    payload["storage_projection"] = storage_projection(
        payload["sources"],
        preparation_batches=payload["preparation_batches"],
        prepared_pack_bytes=1,
        preparation_workspace_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        compact_bundle_bytes=1,
        report_bytes=1,
        safety_reserve_bytes=7_000_000_000,
        maximum_atomic_write_bytes=1,
    )

    assert _validate_source_lock_contract(payload) == {"prd-tnm.s3.amazonaws.com"}


def test_source_lock_rejects_reused_trail_source_across_batches() -> None:
    payload = _batched_v2_source_lock()
    tile_keys = [
        f"{tile['region']}:{tile['tile_x']}:{tile['tile_y']}"
        for tile in payload["tile_inventory"]["tiles"]
    ]
    payload["preparation_batches"].extend(
        {
            "id": f"trails-{index:03d}",
            "family": "trails",
            "sources": ["trails"],
            "tile_keys": [tile_key],
            "source_staging_bytes": 29,
            "maximum_part_bytes": 29,
        }
        for index, tile_key in enumerate(tile_keys[:2], start=1)
    )
    payload["storage_projection"] = storage_projection(
        payload["sources"],
        preparation_batches=payload["preparation_batches"],
        prepared_pack_bytes=1,
        preparation_workspace_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        compact_bundle_bytes=1,
        report_bytes=1,
        safety_reserve_bytes=7_000_000_000,
        maximum_atomic_write_bytes=1,
    )

    with pytest.raises(HouseHunterError, match="trails.*incomplete or overlapping"):
        _validate_source_lock_contract(payload)


def test_source_lock_accepts_identity_assembled_trail_ingestion_batches() -> None:
    payload = _batched_v2_source_lock()
    payload["trail_fragment_mode"] = "state_clipped_globalid_v1"
    payload["trail_fragment_contract"] = {
        "feature_rows": 2,
        "fragment_rows": 3,
        "logical_features": 1,
        "logical_sha256": "1" * 64,
    }
    payload["preparation_batches"].append(
        {
            "id": "trails-001",
            "family": "trails",
            "sources": ["trails"],
            "source_staging_bytes": 29,
            "maximum_part_bytes": 29,
        }
    )
    payload["storage_projection"] = storage_projection(
        payload["sources"],
        preparation_batches=payload["preparation_batches"],
        prepared_pack_bytes=1,
        preparation_workspace_bytes=1,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
        compact_bundle_bytes=1,
        report_bytes=1,
        safety_reserve_bytes=7_000_000_000,
        maximum_atomic_write_bytes=1,
    )

    assert _validate_source_lock_contract(payload) == {"prd-tnm.s3.amazonaws.com"}

    payload["preparation_batches"][-1]["tile_keys"] = ["conus_dc:0:0"]
    with pytest.raises(HouseHunterError, match="invalid preparation batch"):
        _validate_source_lock_contract(payload)


@pytest.mark.parametrize(
    "contract",
    (
        {"feature_rows": 0, "fragment_rows": 1, "logical_features": 1, "logical_sha256": "1" * 64},
        {"feature_rows": 2, "fragment_rows": 1, "logical_features": 1, "logical_sha256": "1" * 64},
        {"feature_rows": 1, "fragment_rows": 1, "logical_features": 2, "logical_sha256": "1" * 64},
        {"feature_rows": 1, "fragment_rows": 1, "logical_features": 1, "logical_sha256": "A" * 64},
    ),
)
def test_source_lock_rejects_invalid_trail_fragment_contract_boundaries(
    contract: dict[str, object],
) -> None:
    payload = _batched_v2_source_lock()
    payload["trail_fragment_mode"] = "state_clipped_globalid_v1"
    payload["trail_fragment_contract"] = contract

    with pytest.raises(HouseHunterError, match="invalid trail fragment contract"):
        _validate_source_lock_contract(payload)


def test_source_lock_rejects_fragment_mode_without_trail_ingestion_batches() -> None:
    payload = _batched_v2_source_lock()
    payload["trail_fragment_mode"] = "state_clipped_globalid_v1"
    payload["trail_fragment_contract"] = {
        "feature_rows": 1,
        "fragment_rows": 1,
        "logical_features": 1,
        "logical_sha256": "1" * 64,
    }

    with pytest.raises(HouseHunterError, match="trail.*batch"):
        _validate_source_lock_contract(payload)


def test_download_batch_selects_and_verifies_only_its_locked_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = {"dem-west.tif": b"west", "dem-east.tif": b"east", "trails.fgb": b"trail"}
    sources = [
        {
            "name": name,
            "family": family,
            "filename": filename,
            "url": f"https://example.test/{filename}",
            "size": len(contents[filename]),
            "sha256": hashlib.sha256(contents[filename]).hexdigest(),
        }
        for name, family, filename in (
            ("dem-west", "elevation", "dem-west.tif"),
            ("dem-east", "elevation", "dem-east.tif"),
            ("trails", "trails", "trails.fgb"),
        )
    ]
    lock = {
        "schema_version": 2,
        "sources": sources,
        "preparation_batches": [
            {"id": "elevation-001", "family": "elevation", "sources": ["dem-west"]}
        ],
    }
    lock_path = tmp_path / "source-lock.json"
    lock_path.write_text(json.dumps(lock))
    requested: list[str] = []
    verified: list[set[str] | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        filename = Path(request.url.path).name
        requested.append(filename)
        return httpx.Response(200, content=contents[filename])

    monkeypatch.setattr(
        "househunter.mountain_gis._validate_source_lock_contract",
        lambda payload: {"example.test"},
    )
    monkeypatch.setattr(
        "househunter.mountain_gis.verify_source_families",
        lambda path, *, root, families, source_names=None: verified.append(source_names),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        download_sources(
            lock_path,
            tmp_path / "downloads",
            client=client,
            batch_id="elevation-001",
        )

    assert requested == ["dem-west.tif"]
    assert verified == [{"dem-west"}]
    assert (tmp_path / "downloads" / "dem-west.tif").read_bytes() == b"west"
    assert not (tmp_path / "downloads" / "dem-east.tif").exists()
    assert not (tmp_path / "downloads" / "trails.fgb").exists()


def test_anonymous_arcgis_snapshot_is_locked_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("househunter.mountain_gis.ARCGIS_ASSEMBLY_PAGE_GROUP", 64)
    objectids = list(range(1, 66))
    polygons = {
        objectid: {
            "type": "Polygon",
            "coordinates": [
                (
                    [
                        [objectid, 0],
                        [objectid + 1, 1],
                        [objectid + 1, 0],
                        [objectid, 1],
                        [objectid, 0],
                    ]
                    if objectid == 1
                    else [[objectid, 0], [objectid + 1, 0], [objectid + 1, 1], [objectid, 0]]
                )
            ],
        }
        for objectid in objectids
    }
    codes = {objectid: "OA" if objectid % 2 else "XA" for objectid in objectids}
    access_counts = {
        code: sum(value == code for value in codes.values()) for code in ("OA", "RA", "UK", "XA")
    }
    pages = [
        [
            {
                "type": "Feature",
                "geometry": polygons[objectid],
                "properties": {"OBJECTID": objectid, "Pub_Access": codes[objectid]},
            }
        ]
        for objectid in objectids
    ]
    objectid_digest = hashlib.sha256(
        "".join(f"{objectid}\n" for objectid in objectids).encode()
    ).hexdigest()
    service_metadata = {
        "currentVersion": 11.3,
        "name": "PADUS4_1Combined",
        "type": "Feature Layer",
        "geometryType": "esriGeometryPolygon",
        "objectIdField": "OBJECTID",
        "maxRecordCount": 2000,
        "supportedQueryFormats": "JSON, geoJSON",
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID"},
            {"name": "Pub_Access", "type": "esriFieldTypeString"},
        ],
    }
    service_digest = hashlib.sha256(
        json.dumps(
            {
                **{key: service_metadata[key] for key in service_metadata if key != "fields"},
                "fields": {item["name"]: item for item in service_metadata["fields"]},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    page_artifact_size = []
    page_artifact_sha256 = []
    for index, page in enumerate(pages):
        path = tmp_path / f"expected-page-{index}.fgb"
        geometry = shapely.from_geojson(json.dumps(page[0]["geometry"]))
        pyogrio.raw.write(
            path,
            shapely.to_wkb([shapely.MultiPolygon([geometry])], output_dimension=2, byte_order=1),
            [
                np.array([page[0]["properties"]["OBJECTID"]], dtype=np.int64),
                np.array([page[0]["properties"]["Pub_Access"]], dtype=object),
            ],
            ["OBJECTID", "Pub_Access"],
            layer="page",
            driver="FlatGeobuf",
            geometry_type="MultiPolygon",
            crs="EPSG:4326",
        )
        page_artifact_size.append(path.stat().st_size)
        page_artifact_sha256.append(sha256_file(path))
        path.unlink()
    source = {
        "url": "https://edits.nationalmap.gov/example/MapServer/0",
        "filename": "pad.fgb",
        "layer": "PADUS4_1Combined",
        "count": len(objectids),
        "size": 0,
        "sha256": "0" * 64,
        "acquisition": {
            "type": "arcgis_query_snapshot_v1",
            "object_id_field": "OBJECTID",
            "fields": ["OBJECTID", "Pub_Access"],
            "where": "1=1",
            "order_by": "OBJECTID ASC",
            "page_size": 1,
            "output_crs": "EPSG:4326",
            "service_metadata_sha256": service_digest,
            "download_bytes": 1,
            "page_shard_bytes": 1,
            "maximum_response_bytes": 1_000_000,
            "objectid_sha256": objectid_digest,
            "page_sha256": [
                hashlib.sha256(
                    json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                for page in pages
            ],
            "page_artifact_size": page_artifact_size,
            "page_artifact_sha256": page_artifact_sha256,
            "access_counts": access_counts,
        },
    }
    source["acquisition"]["page_shard_bytes"] = sum(page_artifact_size)
    feature_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal feature_requests
        assert "authorization" not in request.headers
        parameters = parse_qs(request.content.decode())
        assert "token" not in parameters
        if not request.url.path.endswith("/query"):
            return httpx.Response(200, json=service_metadata)
        if parameters.get("returnIdsOnly") == ["true"]:
            return httpx.Response(
                200,
                json={"objectIdFieldName": "OBJECTID", "objectIds": list(reversed(objectids))},
            )
        feature_requests += 1
        objectid = int(parameters["objectIds"][0])
        return httpx.Response(
            200,
            json={"type": "FeatureCollection", "features": pages[objectid - 1]},
        )

    target = tmp_path / "pad.fgb"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HouseHunterError, match="artifact checksum differs"):
            _download_arcgis_snapshot(
                source,
                target,
                http=client,
                owns_client=False,
                allowed_hosts={"edits.nationalmap.gov"},
            )
        page_root = next(tmp_path.glob(".*.arcgis-pages"))
        expected = tmp_path / "expected.fgb"
        geometries = []
        field_arrays = []
        field_names = None
        for shard in sorted(page_root.glob("*.fgb")):
            metadata, _, geometry, arrays = pyogrio.raw.read(
                shard, columns=["OBJECTID", "Pub_Access"], force_2d=True
            )
            field_names = list(metadata["fields"])
            geometries.append(geometry)
            field_arrays.append(list(arrays))
        assert field_names is not None
        pyogrio.raw.write(
            expected,
            np.concatenate(geometries),
            [
                np.concatenate([arrays[index] for arrays in field_arrays])
                for index in range(len(field_names))
            ],
            field_names,
            layer="PADUS4_1Combined",
            driver="FlatGeobuf",
            geometry_type="MultiPolygon",
            crs="EPSG:4326",
        )
        source["size"] = expected.stat().st_size
        source["sha256"] = sha256_file(expected)
        _download_arcgis_snapshot(
            source,
            target,
            http=client,
            owns_client=False,
            allowed_hosts={"edits.nationalmap.gov"},
        )

    assert feature_requests == len(objectids)
    assert target.is_file()
    metadata, _, _, arrays = pyogrio.raw.read(
        target, columns=["OBJECTID", "Pub_Access"], read_geometry=False
    )
    fields = dict(zip(metadata["fields"], arrays, strict=True))
    assert sorted(fields["OBJECTID"].tolist()) == objectids
    assert sorted(fields["Pub_Access"].tolist()) == sorted(codes.values())

    oversized = {
        **source,
        "acquisition": {**source["acquisition"], "maximum_response_bytes": 1_000},
    }
    compressed = gzip.compress(b"x" * 2_000)

    def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "transfer-encoding": "chunked"},
            stream=httpx.ByteStream(compressed),
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(oversized_handler)) as client,
        pytest.raises(HouseHunterError, match="exceeds its locked bound"),
    ):
        _download_arcgis_snapshot(
            oversized,
            tmp_path / "oversized.fgb",
            http=client,
            owns_client=False,
            allowed_hosts={"edits.nationalmap.gov"},
        )


def test_completed_arcgis_download_removes_valid_stranded_page_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "sources"
    destination.mkdir()
    content = b"complete snapshot"
    acquisition = {
        "type": "arcgis_query_snapshot_v1",
        "page_size": 1,
        "page_shard_bytes": 1,
        "maximum_response_bytes": 1,
    }
    source = {
        "name": "pad",
        "family": "pad_us",
        "filename": "pad.fgb",
        "url": "https://example.test/FeatureServer/0",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "acquisition": acquisition,
    }
    lock_path = tmp_path / "source-lock.json"
    lock_path.write_text(json.dumps({"schema_version": 2, "sources": [source]}))
    target = destination / "pad.fgb"
    target.write_bytes(content)
    page_root = destination / (f".{sha256_bytes(canonical_json(acquisition))[:32]}.arcgis-pages")
    page_root.mkdir()
    (page_root / ".househunter-mountain-owned").write_text("arcgis-pages-v1\n")
    (page_root / "000000.fgb").write_bytes(b"stale but owned")
    monkeypatch.setattr(
        "househunter.mountain_gis._validate_source_lock_contract",
        lambda payload: {"example.test"},
    )
    monkeypatch.setattr(
        "househunter.mountain_gis.verify_source_families", lambda *args, **kwargs: None
    )

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("network"))
    ) as client:
        download_sources(
            lock_path,
            destination,
            client=client,
            families={"pad_us"},
        )

    assert target.read_bytes() == content
    assert not page_root.exists()


def test_completed_arcgis_download_never_adopts_unowned_page_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "sources"
    destination.mkdir()
    content = b"complete snapshot"
    acquisition = {
        "type": "arcgis_query_snapshot_v1",
        "page_size": 1,
        "page_shard_bytes": 1,
        "maximum_response_bytes": 1,
    }
    source = {
        "name": "pad",
        "family": "pad_us",
        "filename": "pad.fgb",
        "url": "https://example.test/FeatureServer/0",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "acquisition": acquisition,
    }
    lock_path = tmp_path / "source-lock.json"
    lock_path.write_text(json.dumps({"schema_version": 2, "sources": [source]}))
    (destination / "pad.fgb").write_bytes(content)
    page_root = destination / (f".{sha256_bytes(canonical_json(acquisition))[:32]}.arcgis-pages")
    page_root.mkdir()
    valuable = page_root / "valuable.txt"
    valuable.write_text("preserve")
    monkeypatch.setattr(
        "househunter.mountain_gis._validate_source_lock_contract",
        lambda payload: {"example.test"},
    )
    monkeypatch.setattr(
        "househunter.mountain_gis.verify_source_families", lambda *args, **kwargs: None
    )

    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("network"))
        ) as client,
        pytest.raises(HouseHunterError, match="workspace is unsafe"),
    ):
        download_sources(
            lock_path,
            destination,
            client=client,
            families={"pad_us"},
        )

    assert valuable.read_text() == "preserve"


def test_corrupt_phase_artifact_invalidates_checkpoint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tile_key = "a" * 24
    tile = workspace / "tiles" / tile_key
    tile.mkdir(parents=True)
    (workspace / "checkpoints").mkdir()
    artifact = tile / "elevation.npy"
    artifact.write_bytes(b"valid")
    _write_phase(
        workspace,
        "elevation",
        {tile_key: _file_metadata(artifact)},
        dependency_sha256="b" * 64,
    )

    assert _valid_phase(workspace, "elevation", dependency_sha256="b" * 64) is not None

    artifact.write_bytes(b"corrupt")
    assert _valid_phase(workspace, "elevation", dependency_sha256="b" * 64) is None


def test_elevation_batch_resumes_after_source_deletion_but_rejects_corrupt_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    tile_key = "a" * 24
    (workspace / "tiles" / tile_key).mkdir(parents=True)
    (workspace / "checkpoints").mkdir()
    source_root = tmp_path / "staging" / ("b" * 16)
    source_root.mkdir(parents=True)
    source = source_root / "dem.tif"
    source.write_bytes(b"dem")
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=source_root / "blocks.fgb",
        elevation=(source,),
        pad_us=source_root / "pad.gpkg",
        trails=source_root / "trails.fgb",
    )
    source_lock = {
        "sources": [
            {
                "name": "dem",
                "family": "elevation",
                "filename": source.name,
                "sha256": hashlib.sha256(b"dem").hexdigest(),
            }
        ]
    }
    batch = {
        "id": "elevation-001",
        "family": "elevation",
        "sources": ["dem"],
        "tile_keys": ["conus_dc:0:0"],
    }
    entries = [
        {
            "key": tile_key,
            "region": "conus_dc",
            "tile_x": 0,
            "tile_y": 0,
            "shape": [2, 2],
            "bounds": [0.0, 0.0, 2.0, 2.0],
        }
    ]
    calls: list[str] = []
    monkeypatch.setattr(
        "househunter.mountain_pack.verify_source_families",
        lambda *args, **kwargs: calls.append("verified"),
    )
    monkeypatch.setattr("househunter.mountain_pack._elevation_index", lambda *args: object())
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_elevation",
        lambda *args, **kwargs: (
            calls.append("read") or np.ones((2, 2), dtype=np.float32),
            from_origin(0, 2, 1, 1),
        ),
    )

    def delete_sources(*args: object, **kwargs: object) -> None:
        calls.append("deleted")
        source.unlink(missing_ok=True)

    monkeypatch.setattr("househunter.mountain_pack.delete_managed_sources", delete_sources)
    arguments = {
        "batch": batch,
        "blocks_digest": "c" * 64,
        "cell_size_m": 1,
        "maximum_bytes": 1_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": source_root,
        "managed_staging_root": source_root.parent,
    }

    _materialize_elevation_batch((region,), entries, workspace, **arguments)
    assert calls == ["verified", "read", "deleted"]
    assert not source.exists()

    _materialize_elevation_batch((region,), entries, workspace, **arguments)
    assert calls == ["verified", "read", "deleted", "deleted"]

    (workspace / "tiles" / tile_key / "elevation.npy").write_bytes(b"corrupt")
    monkeypatch.setattr(
        "househunter.mountain_pack.verify_source_families",
        lambda *args, **kwargs: (_ for _ in ()).throw(HouseHunterError("raw source missing")),
    )
    with pytest.raises(HouseHunterError, match="raw source missing"):
        _materialize_elevation_batch((region,), entries, workspace, **arguments)


def test_shared_elevation_source_is_retained_until_every_dependent_batch_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "checkpoints").mkdir(parents=True)
    source_root = tmp_path / "staging" / ("b" * 16)
    source_root.mkdir(parents=True)
    names = ("first", "shared", "middle", "last")
    paths = {name: source_root / f"{name}.tif" for name in names}
    for name, path in paths.items():
        path.write_bytes(name.encode())
    entries = [
        {
            "key": key * 24,
            "region": "conus_dc",
            "tile_x": index,
            "tile_y": 0,
            "shape": [2, 2],
            "bounds": [float(index), 0.0, float(index + 2), 2.0],
        }
        for index, key in enumerate(("a", "c", "e"))
    ]
    for entry in entries:
        (workspace / "tiles" / str(entry["key"])).mkdir(parents=True)
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=source_root / "blocks.fgb",
        elevation=tuple(paths.values()),
        pad_us=source_root / "pad.gpkg",
        trails=source_root / "trails.fgb",
    )
    batches = [
        {
            "id": "elevation-001",
            "family": "elevation",
            "sources": ["first", "shared"],
            "tile_keys": ["conus_dc:0:0"],
        },
        {
            "id": "elevation-002",
            "family": "elevation",
            "sources": ["middle"],
            "tile_keys": ["conus_dc:1:0"],
        },
        {
            "id": "elevation-003",
            "family": "elevation",
            "sources": ["shared", "last"],
            "tile_keys": ["conus_dc:2:0"],
        },
    ]
    source_lock = {
        "sources": [
            {
                "name": name,
                "family": "elevation",
                "filename": paths[name].name,
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
            }
            for name in names
        ],
        "preparation_batches": batches,
    }
    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr("househunter.mountain_pack._elevation_index", lambda *a: object())
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_elevation",
        lambda *a, **k: (np.ones((2, 2), dtype=np.float32), from_origin(0, 2, 1, 1)),
    )
    deleted: list[set[str]] = []

    def delete(*args: object, source_names: set[str], **kwargs: object) -> None:
        deleted.append(source_names)
        for name in source_names:
            paths[name].unlink(missing_ok=True)

    monkeypatch.setattr("househunter.mountain_pack.delete_managed_sources", delete)
    arguments = {
        "blocks_digest": "c" * 64,
        "cell_size_m": 1,
        "maximum_bytes": 1_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": source_root,
        "managed_staging_root": source_root.parent,
    }

    _materialize_elevation_batch((region,), entries, workspace, batch=batches[0], **arguments)
    assert deleted == [{"first"}]
    assert not paths["first"].exists()
    assert paths["shared"].exists()
    assert paths["middle"].exists()
    assert paths["last"].exists()

    _materialize_elevation_batch((region,), entries, workspace, batch=batches[1], **arguments)
    assert deleted == [{"first"}, {"first", "middle"}]
    assert paths["shared"].exists()
    assert not paths["middle"].exists()
    assert paths["last"].exists()

    _materialize_elevation_batch((region,), entries, workspace, batch=batches[2], **arguments)
    assert deleted == [
        {"first"},
        {"first", "middle"},
        {"first", "shared", "middle", "last"},
    ]
    assert all(not path.exists() for path in paths.values())


def test_elevation_batch_rejects_out_of_order_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "checkpoints").mkdir(parents=True)
    tile_key = "a" * 24
    (workspace / "tiles" / tile_key).mkdir(parents=True)
    source = tmp_path / "dem.tif"
    source.write_bytes(b"dem")
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks.fgb",
        elevation=(source,),
        pad_us=tmp_path / "pad.gpkg",
        trails=tmp_path / "trails.fgb",
    )
    batches = [
        {
            "id": f"elevation-{index:03d}",
            "family": "elevation",
            "sources": ["dem"],
            "tile_keys": ["conus_dc:0:0"],
        }
        for index in (1, 2)
    ]
    source_lock = {
        "sources": [
            {
                "name": "dem",
                "family": "elevation",
                "filename": source.name,
                "sha256": hashlib.sha256(b"dem").hexdigest(),
            }
        ],
        "preparation_batches": batches,
    }
    monkeypatch.setattr(
        "househunter.mountain_pack.verify_source_families",
        lambda *a, **k: pytest.fail("out-of-order batch read its sources"),
    )

    with pytest.raises(HouseHunterError, match="canonical order"):
        _materialize_elevation_batch(
            (region,),
            [
                {
                    "key": tile_key,
                    "region": "conus_dc",
                    "tile_x": 0,
                    "tile_y": 0,
                    "shape": [2, 2],
                    "bounds": [0.0, 0.0, 2.0, 2.0],
                }
            ],
            workspace,
            batch=batches[1],
            blocks_digest="c" * 64,
            cell_size_m=1,
            maximum_bytes=1_000_000,
            source_lock_path=tmp_path / "source-lock.json",
            source_lock=source_lock,
            source_root=tmp_path,
            managed_staging_root=None,
        )


def test_elevation_resume_rejects_valid_later_checkpoint_after_corrupt_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "checkpoints").mkdir(parents=True)
    entries = [
        {
            "key": key * 24,
            "region": "conus_dc",
            "tile_x": index,
            "tile_y": 0,
            "shape": [2, 2],
            "bounds": [float(index), 0.0, float(index + 2), 2.0],
        }
        for index, key in enumerate(("a", "b"))
    ]
    for entry in entries:
        (workspace / "tiles" / str(entry["key"])).mkdir(parents=True)
    sources = []
    for name in ("first", "second"):
        path = tmp_path / f"{name}.tif"
        path.write_bytes(name.encode())
        sources.append(
            {
                "name": name,
                "family": "elevation",
                "filename": path.name,
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
            }
        )
    batches = [
        {
            "id": f"elevation-{index + 1:03d}",
            "family": "elevation",
            "sources": [name],
            "tile_keys": [f"conus_dc:{index}:0"],
        }
        for index, name in enumerate(("first", "second"))
    ]
    source_lock = {"sources": sources, "preparation_batches": batches}
    dependency = "c" * 64
    later_file = workspace / "tiles" / str(entries[1]["key"]) / "elevation.npy"
    with later_file.open("wb") as handle:
        np.save(handle, np.ones((2, 2), dtype=np.float32), allow_pickle=False)
    _write_phase(
        workspace,
        "elevation--elevation-002",
        {str(entries[1]["key"]): _file_metadata(later_file)},
        dependency_sha256=dependency,
        verified_source_sha256=_source_names_digest(source_lock, {"second"}),
    )
    deleted: list[set[str]] = []
    monkeypatch.setattr(
        "househunter.mountain_pack.delete_managed_sources",
        lambda *args, source_names, **kwargs: deleted.append(source_names),
    )
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks.fgb",
        elevation=tuple(tmp_path / f"{name}.tif" for name in ("first", "second")),
        pad_us=tmp_path / "pad.gpkg",
        trails=tmp_path / "trails.fgb",
    )

    with pytest.raises(HouseHunterError, match="canonical order"):
        _materialize_elevation_batch(
            (region,),
            entries,
            workspace,
            batch=batches[1],
            blocks_digest=dependency,
            cell_size_m=1,
            maximum_bytes=1_000_000,
            source_lock_path=tmp_path / "source-lock.json",
            source_lock=source_lock,
            source_root=tmp_path,
            managed_staging_root=tmp_path,
        )

    assert deleted == []


def test_elevation_batch_writes_nodata_for_tile_with_no_source_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    tile_key = "d" * 24
    (workspace / "tiles" / tile_key).mkdir(parents=True)
    (workspace / "checkpoints").mkdir()
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks.fgb",
        elevation=(tmp_path / "unrelated-dem.tif",),
        pad_us=tmp_path / "pad.gpkg",
        trails=tmp_path / "trails.fgb",
    )
    monkeypatch.setattr(
        "househunter.mountain_pack._elevation_index",
        lambda *args: pytest.fail("empty-dependency batch indexed an unrelated DEM"),
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_elevation",
        lambda *args, **kwargs: pytest.fail("empty-dependency batch read an unrelated DEM"),
    )

    checkpoint = _materialize_elevation_batch(
        (region,),
        [
            {
                "key": tile_key,
                "region": "conus_dc",
                "tile_x": 5,
                "tile_y": -2,
                "shape": [2, 3],
                "bounds": [0.0, 0.0, 3.0, 2.0],
            }
        ],
        workspace,
        batch={
            "id": "elevation-002",
            "family": "elevation",
            "sources": [],
            "tile_keys": ["conus_dc:5:-2"],
        },
        blocks_digest="e" * 64,
        cell_size_m=1,
        maximum_bytes=1_000_000,
        source_lock_path=tmp_path / "source-lock.json",
        source_lock={"sources": []},
        source_root=None,
        managed_staging_root=None,
    )

    array = np.load(workspace / "tiles" / tile_key / "elevation.npy")
    assert checkpoint["files"].keys() == {tile_key}
    assert array.shape == (2, 3)
    assert np.isnan(array).all()


def test_read_tile_trails_preserves_source_order_and_sums_each_contribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = (tmp_path / "first.fgb", tmp_path / "second.fgb")
    seen: list[Path] = []

    def read(path: Path, **kwargs: object) -> tuple[list[LineString], dict[str, object]]:
        seen.append(path)
        return [LineString([(0, 0), (1, 1)])], {}

    monkeypatch.setattr("househunter.mountain_gis._read_geometries", read)
    monkeypatch.setattr(
        "househunter.mountain_gis.rasterize",
        lambda *args, **kwargs: np.full(kwargs["out_shape"], len(seen), dtype=np.float32),
    )
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks.fgb",
        elevation=(),
        pad_us=tmp_path / "pad.gpkg",
        trails=paths,
    )

    result = read_tile_trails(
        region,
        (0.0, 0.0, 2.0, 2.0),
        shape=(2, 2),
        transform=from_origin(0, 2, 1, 1),
    )

    assert seen == list(paths)
    np.testing.assert_array_equal(result, np.full((2, 2), 3, dtype=np.float32))


def test_pad_replace_precedence_uses_numeric_locked_objectid_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    polygon = Polygon([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])
    monkeypatch.setattr(
        "househunter.mountain_gis._read_geometries",
        lambda *args, **kwargs: (
            np.array([polygon, polygon], dtype=object),
            {
                "Pub_Access": np.array(["XA", "OA"], dtype=object),
                "OBJECTID": np.array([20, 10], dtype=np.int64),
            },
        ),
    )
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks.fgb",
        elevation=(),
        pad_us=tmp_path / "pad.fgb",
        trails=tmp_path / "trails.fgb",
        pad_order_field="OBJECTID",
    )

    result = read_tile_pad(
        region,
        (0.0, 0.0, 2.0, 2.0),
        shape=(2, 2),
        transform=from_origin(0, 2, 1, 1),
    )

    np.testing.assert_array_equal(result, np.full((2, 2), 3, dtype=np.uint8))


def test_pad_rasterization_preserves_locked_self_intersecting_polygon(tmp_path: Path) -> None:
    pad = tmp_path / "pad.geojson"
    _geojson(
        pad,
        {
            "type": "Polygon",
            "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]],
        },
        {"Pub_Access": "OA", "OBJECTID": 1},
    )
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:4326",
        blocks=tmp_path / "blocks.fgb",
        elevation=(),
        pad_us=pad,
        trails=tmp_path / "trails.fgb",
        pad_order_field="OBJECTID",
    )

    result = read_tile_pad(
        region,
        (0.0, 0.0, 2.0, 2.0),
        shape=(2, 2),
        transform=from_origin(0, 2, 1, 1),
    )

    assert result.max() == 1


def test_state_clipped_trail_fragments_rasterize_as_one_logical_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    region = RegionSources(
        **{
            **region.__dict__,
            "target_crs": "EPSG:4269",
            "trail_where": "trailtype <> 'Water Trail'",
        }
    )
    for batch in batches:
        batch.pop("tile_keys")
    globalid = "00000000-0000-4000-8000-000000000001"
    permanentidentifier = "00000000-0000-4000-8000-000000000002"
    pieces = {
        "west.fgb": [LineString([(0.1, 0.5), (1.0, 0.5)])],
        "east.fgb": [LineString([(1.0, 0.5), (1.9, 0.5)])],
    }

    def read(
        path: Path, **kwargs: object
    ) -> tuple[dict[str, object], np.ndarray, np.ndarray, tuple[np.ndarray, ...]]:
        geometries = pieces[path.name]
        return (
            {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
            np.arange(len(geometries), dtype=np.int64),
            shapely.to_wkb(geometries),
            (
                np.array([globalid] * len(geometries), dtype=object),
                np.array([permanentidentifier] * len(geometries), dtype=object),
                np.array(["Terra Trail"] * len(geometries), dtype=object),
            ),
        )

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr("househunter.mountain_pack.pyogrio.raw.read", read)
    common = {
        "ordered_batches": batches,
        "dependency_sha256": "7" * 64,
        "maximum_bytes": 10_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }
    for batch in batches:
        _ingest_trail_fragment_batch((region,), workspace, batch=batch, **common)
    observed_files = _materialize_fragment_trails(
        (region,),
        entries,
        workspace,
        dependency_sha256="7" * 64,
        maximum_bytes=10_000_000,
        source_lock=source_lock,
    )
    observed = np.load(workspace / "tiles" / str(entries[0]["key"]) / "trails.npy")
    expected = rasterize(
        [(MultiLineString(pieces["west.fgb"] + pieces["east.fgb"]), 1.0)],
        out_shape=(2, 2),
        transform=rasterio.Affine(*entries[0]["transform"]),
        fill=0.0,
        all_touched=False,
        merge_alg=MergeAlg.add,
        dtype=np.float32,
    )

    np.testing.assert_array_equal(observed, expected)
    assert observed.max() == 1
    assert set(observed_files) == {entries[0]["key"]}
    assert not (workspace / "trail-fragments.sqlite3").exists()


def test_state_clipped_trails_keep_distinct_overlapping_globalids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    region = RegionSources(**{**region.__dict__, "target_crs": "EPSG:4269"})
    batch = batches[0]
    batch.pop("tile_keys")
    line = LineString([(0.1, 0.5), (1.9, 0.5)])

    def read(
        *args: object, **kwargs: object
    ) -> tuple[dict[str, object], np.ndarray, np.ndarray, tuple[np.ndarray, ...]]:
        return (
            {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
            np.array([10, 11]),
            shapely.to_wkb([line, line]),
            (
                np.array(
                    [
                        "00000000-0000-4000-8000-000000000011",
                        "00000000-0000-4000-8000-000000000012",
                    ],
                    dtype=object,
                ),
                np.array([None, None], dtype=object),
                np.array(["Terra Trail", "Terra Trail"], dtype=object),
            ),
        )

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr("househunter.mountain_pack.pyogrio.raw.read", read)
    _ingest_trail_fragment_batch(
        (region,),
        workspace,
        batch=batch,
        ordered_batches=[batch],
        dependency_sha256="8" * 64,
        maximum_bytes=10_000_000,
        source_lock_path=tmp_path / "source-lock.json",
        source_lock={"sources": source_lock["sources"][:1]},
        source_root=tmp_path / "sources",
        managed_staging_root=None,
    )
    _materialize_fragment_trails(
        (region,),
        entries,
        workspace,
        dependency_sha256="8" * 64,
        maximum_bytes=10_000_000,
        source_lock={"sources": source_lock["sources"][:1]},
    )

    observed = np.load(workspace / "tiles" / str(entries[0]["key"]) / "trails.npy")
    assert observed.max() == 2


def test_state_clipped_trail_ids_normalize_braces_and_case_across_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, _, source_lock, batches = _state_clipped_fixture(tmp_path)
    globalid = "a0b1c2d3-e4f5-4678-9abc-000000000001"
    permanent = "b0c1d2e3-f4a5-4789-8bcd-000000000002"

    def read(
        path: Path, **kwargs: object
    ) -> tuple[dict[str, object], np.ndarray, np.ndarray, tuple[np.ndarray, ...]]:
        upper = path.name == "west.fgb"
        return (
            {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
            np.array([1]),
            shapely.to_wkb([LineString([(0, 0), (1, 0)])]),
            (
                np.array([f"{{{globalid.upper()}}}" if upper else globalid], dtype=object),
                np.array([f"{{{permanent.upper()}}}" if upper else permanent], dtype=object),
                np.array(["Terra Trail"], dtype=object),
            ),
        )

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr("househunter.mountain_pack.pyogrio.raw.read", read)
    common = {
        "ordered_batches": batches,
        "dependency_sha256": "1" * 64,
        "maximum_bytes": 10_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }
    for batch in batches:
        _ingest_trail_fragment_batch((region,), workspace, batch=batch, **common)

    connection, _ = _finalize_trail_fragment_store(workspace, source_lock)
    try:
        assert connection.execute(
            "SELECT globalid, permanentidentifier FROM logical_feature"
        ).fetchall() == [(globalid, permanent)]
    finally:
        connection.close()


def test_state_clipped_duplicate_id_with_conflicting_attributes_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, _, source_lock, batches = _state_clipped_fixture(tmp_path)
    globalid = "00000000-0000-4000-8000-000000000021"

    def read(
        path: Path, **kwargs: object
    ) -> tuple[dict[str, object], np.ndarray, np.ndarray, tuple[np.ndarray, ...]]:
        return (
            {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
            np.array([1]),
            shapely.to_wkb([LineString([(0, 0), (1, 0)])]),
            (
                np.array([globalid], dtype=object),
                np.array([None], dtype=object),
                np.array(["Terra Trail" if path.name == "west.fgb" else "Bike Trail"]),
            ),
        )

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr("househunter.mountain_pack.pyogrio.raw.read", read)
    common = {
        "ordered_batches": batches,
        "dependency_sha256": "2" * 64,
        "maximum_bytes": 10_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }
    for batch in batches:
        _ingest_trail_fragment_batch((region,), workspace, batch=batch, **common)

    with pytest.raises(HouseHunterError, match="attributes conflict"):
        _finalize_trail_fragment_store(workspace, source_lock)


def test_state_clipped_logical_digest_is_independent_of_source_row_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [
        "00000000-0000-4000-8000-000000000031",
        "00000000-0000-4000-8000-000000000032",
    ]
    digests: list[str] = []
    for label, order in (("forward", [0, 1]), ("reverse", [1, 0])):
        root = tmp_path / label
        workspace, region, _, source_lock, batches = _state_clipped_fixture(root)
        batch = batches[0]
        one_source_lock = {"sources": source_lock["sources"][:1]}

        def read(
            *args: object, row_order: list[int] = order, **kwargs: object
        ) -> tuple[dict[str, object], np.ndarray, np.ndarray, tuple[np.ndarray, ...]]:
            return (
                {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
                np.array([20, 10])[row_order],
                shapely.to_wkb([LineString([(index, 0), (index + 0.5, 0)]) for index in row_order]),
                (
                    np.array(ids, dtype=object)[row_order],
                    np.array([None, None], dtype=object)[row_order],
                    np.array(["Terra Trail", "Terra Trail"], dtype=object)[row_order],
                ),
            )

        monkeypatch.setattr(
            "househunter.mountain_pack.verify_source_families", lambda *a, **k: None
        )
        monkeypatch.setattr("househunter.mountain_pack.pyogrio.raw.read", read)
        _ingest_trail_fragment_batch(
            (region,),
            workspace,
            batch=batch,
            ordered_batches=[batch],
            dependency_sha256="3" * 64,
            maximum_bytes=10_000_000,
            source_lock_path=root / "source-lock.json",
            source_lock=one_source_lock,
            source_root=root / "sources",
            managed_staging_root=None,
        )
        connection, digest = _finalize_trail_fragment_store(workspace, one_source_lock)
        connection.close()
        digests.append(digest)

    assert len(set(digests)) == 1


def test_state_clipped_finalization_rebuilds_tampered_logical_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, _, source_lock, batches = _state_clipped_fixture(tmp_path)
    batch = batches[0]
    one_source_lock = {"sources": source_lock["sources"][:1]}
    original = LineString([(0, 0), (1, 0)])

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr(
        "househunter.mountain_pack.pyogrio.raw.read",
        lambda *a, **k: (
            {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
            np.array([1]),
            shapely.to_wkb([original]),
            (
                np.array(["00000000-0000-4000-8000-000000000041"], dtype=object),
                np.array([None], dtype=object),
                np.array(["Terra Trail"], dtype=object),
            ),
        ),
    )
    _ingest_trail_fragment_batch(
        (region,),
        workspace,
        batch=batch,
        ordered_batches=[batch],
        dependency_sha256="4" * 64,
        maximum_bytes=10_000_000,
        source_lock_path=tmp_path / "source-lock.json",
        source_lock=one_source_lock,
        source_root=tmp_path / "sources",
        managed_staging_root=None,
    )
    connection, digest = _finalize_trail_fragment_store(workspace, one_source_lock)
    connection.execute(
        "UPDATE logical_feature SET wkb = ?",
        (sqlite3.Binary(bytes(shapely.to_wkb(LineString([(9, 9), (10, 9)])))),),
    )
    connection.commit()
    connection.close()

    rebuilt, rebuilt_digest = _finalize_trail_fragment_store(workspace, one_source_lock)
    try:
        geometry = shapely.from_wkb(
            rebuilt.execute("SELECT wkb FROM logical_feature").fetchone()[0]
        )
        assert geometry.equals(shapely.MultiLineString([original]))
        assert rebuilt_digest == digest
    finally:
        rebuilt.close()


def test_finalized_trails_resume_removes_stranded_fragment_store(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tile_key = "a" * 24
    tile = workspace / "tiles" / tile_key
    tile.mkdir(parents=True)
    (workspace / "checkpoints").mkdir()
    trails = tile / "trails.npy"
    with trails.open("wb") as handle:
        np.save(handle, np.zeros((2, 2), dtype=np.float32), allow_pickle=False)
    source_lock = {
        "sources": [
            {
                "name": "trails",
                "family": "trails",
                "sha256": "1" * 64,
            }
        ]
    }
    dependency = "2" * 64
    _write_phase(
        workspace,
        "trails",
        {tile_key: _file_metadata(trails)},
        dependency_sha256=dependency,
        verified_source_sha256=_source_family_digest(source_lock, "trails"),
    )
    store = workspace / "trail-fragments.sqlite3"
    store.write_bytes(b"stranded after final checkpoint")
    Path(f"{store}-journal").write_bytes(b"stranded sidecar")

    files = _materialize_fragment_trails(
        (),
        [],
        workspace,
        dependency_sha256=dependency,
        maximum_bytes=1_000_000,
        source_lock=source_lock,
    )

    assert files == {tile_key: _file_metadata(trails)}
    assert not store.exists()
    assert not Path(f"{store}-journal").exists()


def test_state_clipped_ingestion_resumes_after_database_commit_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, _, source_lock, batches = _state_clipped_fixture(tmp_path)
    batch = batches[0]
    one_source_lock = {"sources": source_lock["sources"][:1]}
    reads = 0

    def read(
        *args: object, **kwargs: object
    ) -> tuple[dict[str, object], np.ndarray, np.ndarray, tuple[np.ndarray, ...]]:
        nonlocal reads
        reads += 1
        return (
            {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
            np.array([1]),
            shapely.to_wkb([LineString([(0, 0), (1, 0)])]),
            (
                np.array(["00000000-0000-4000-8000-000000000041"], dtype=object),
                np.array([None], dtype=object),
                np.array(["Terra Trail"], dtype=object),
            ),
        )

    original_write_phase = _write_phase

    def crash(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated crash after database commit")

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr("househunter.mountain_pack.pyogrio.raw.read", read)
    monkeypatch.setattr("househunter.mountain_pack._write_phase", crash)
    arguments = {
        "batch": batch,
        "ordered_batches": [batch],
        "dependency_sha256": "4" * 64,
        "maximum_bytes": 10_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": one_source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }
    with pytest.raises(RuntimeError, match="after database commit"):
        _ingest_trail_fragment_batch((region,), workspace, **arguments)

    monkeypatch.setattr("househunter.mountain_pack._write_phase", original_write_phase)
    monkeypatch.setattr(
        "househunter.mountain_pack.pyogrio.raw.read",
        lambda *a, **k: pytest.fail("committed batch was reread"),
    )
    _ingest_trail_fragment_batch((region,), workspace, **arguments)

    with sqlite3.connect(workspace / "trail-fragments.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM ingested_batch").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM fragment").fetchone() == (1,)
    assert reads == 1


@pytest.mark.parametrize(
    ("damage", "message"),
    (("missing", "missing"), ("corrupt", "corrupt"), ("digest", "digest differs")),
)
def test_state_clipped_completed_checkpoint_rejects_invalid_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, message: str
) -> None:
    workspace, region, _, source_lock, batches = _state_clipped_fixture(tmp_path)
    batch = batches[0]
    one_source_lock = {"sources": source_lock["sources"][:1]}
    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr(
        "househunter.mountain_pack.pyogrio.raw.read",
        lambda *a, **k: (
            {"fields": np.array(["GLOBALID", "permanentidentifier", "trailtype"])},
            np.array([1]),
            shapely.to_wkb([LineString([(0, 0), (1, 0)])]),
            (
                np.array(["00000000-0000-4000-8000-000000000051"], dtype=object),
                np.array([None], dtype=object),
                np.array(["Terra Trail"], dtype=object),
            ),
        ),
    )
    arguments = {
        "batch": batch,
        "ordered_batches": [batch],
        "dependency_sha256": "5" * 64,
        "maximum_bytes": 10_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": one_source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }
    _ingest_trail_fragment_batch((region,), workspace, **arguments)
    store = workspace / "trail-fragments.sqlite3"
    if damage == "missing":
        store.unlink()
    elif damage == "corrupt":
        store.write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(store) as connection:
            connection.execute("UPDATE ingested_batch SET source_sha256 = ?", ("f" * 64,))

    with pytest.raises(HouseHunterError, match=message):
        _ingest_trail_fragment_batch((region,), workspace, **arguments)


def test_trails_batches_must_run_in_canonical_order(tmp_path: Path) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)

    with pytest.raises(HouseHunterError, match="canonical source order"):
        _materialize_trails_batch(
            (region,),
            entries,
            workspace,
            batch=batches[1],
            ordered_batches=batches,
            dependency_sha256="a" * 64,
            maximum_bytes=1_000_000,
            source_lock_path=tmp_path / "source-lock.json",
            source_lock=source_lock,
            source_root=tmp_path / "sources",
            managed_staging_root=None,
        )


def test_trails_transaction_recovers_when_target_committed_before_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    original_write = _write_json
    crashed = False

    def crash_before_progress(path: Path, payload: dict[str, object]) -> None:
        nonlocal crashed
        if path.name == "trails-progress.json" and payload.get("completed_batches"):
            crashed = True
            raise RuntimeError("simulated crash before progress advance")
        original_write(path, payload)

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails",
        lambda *a, **k: np.ones((2, 2), dtype=np.float32),
    )
    monkeypatch.setattr("househunter.mountain_pack._write_json", crash_before_progress)
    arguments = {
        "batch": batches[0],
        "ordered_batches": batches,
        "dependency_sha256": "b" * 64,
        "maximum_bytes": 1_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }

    with pytest.raises(RuntimeError, match="simulated crash"):
        _materialize_trails_batch((region,), entries, workspace, **arguments)
    assert crashed
    np.testing.assert_array_equal(
        np.load(workspace / "tiles" / entries[0]["key"] / "trails.npy"),
        np.ones((2, 2), dtype=np.float32),
    )

    monkeypatch.setattr("househunter.mountain_pack._write_json", original_write)
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails",
        lambda *a, **k: pytest.fail("recovery recomputed an already committed transaction"),
    )
    _materialize_trails_batch((region,), entries, workspace, **arguments)
    progress = json.loads((workspace / "checkpoints" / "trails-progress.json").read_text())
    assert progress["completed_batches"] == ["trails-001"]


def test_trails_transaction_recovers_partially_committed_multi_tile_batch_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    second_key = "e" * 24
    (workspace / "tiles" / second_key).mkdir()
    entries.append(
        {
            **entries[0],
            "key": second_key,
            "tile_x": 1,
        }
    )
    batches[0]["tile_keys"] = ["conus_dc:0:0", "conus_dc:1:0"]
    transaction = workspace / "checkpoints" / ".trails-trails-001.txn"
    original_replace = os.replace
    commits = 0

    def crash_during_second_commit(source: Path, destination: Path) -> None:
        nonlocal commits
        if source.parent == transaction and destination.name == "trails.npy":
            commits += 1
            if commits == 2:
                raise RuntimeError("simulated crash during second tile commit")
        original_replace(source, destination)

    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails",
        lambda *a, **k: np.ones((2, 2), dtype=np.float32),
    )
    monkeypatch.setattr("househunter.mountain_pack.os.replace", crash_during_second_commit)
    arguments = {
        "batch": batches[0],
        "ordered_batches": batches,
        "dependency_sha256": "9" * 64,
        "maximum_bytes": 1_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }

    with pytest.raises(RuntimeError, match="second tile commit"):
        _materialize_trails_batch((region,), entries, workspace, **arguments)
    observed = [
        np.load(workspace / "tiles" / str(entry["key"]) / "trails.npy") for entry in entries
    ]
    assert sum(bool(array.any()) for array in observed) == 1

    monkeypatch.setattr("househunter.mountain_pack.os.replace", original_replace)
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails",
        lambda *a, **k: pytest.fail("resume recomputed a pending transaction"),
    )
    _materialize_trails_batch((region,), entries, workspace, **arguments)

    for entry in entries:
        np.testing.assert_array_equal(
            np.load(workspace / "tiles" / str(entry["key"]) / "trails.npy"),
            np.ones((2, 2), dtype=np.float32),
        )
    progress = json.loads((workspace / "checkpoints" / "trails-progress.json").read_text())
    assert progress["completed_batches"] == ["trails-001"]


@pytest.mark.parametrize("corrupt", ("current", "shadow"))
def test_trails_transaction_rejects_corrupt_current_or_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: str
) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    dependency = "c" * 64
    _initialize_trails_progress(workspace, dependency_sha256=dependency, entries=entries)
    key = str(entries[0]["key"])
    target = workspace / "tiles" / key / "trails.npy"
    transaction = workspace / "checkpoints" / ".trails-trails-001.txn"
    transaction.mkdir()
    shadow = transaction / f"{key}.npy"
    with shadow.open("wb") as handle:
        np.save(handle, np.ones((2, 2), dtype=np.float32), allow_pickle=False)
    before = _file_metadata(target)
    after = {**_file_metadata(shadow), "filename": "trails.npy"}
    (transaction / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": "trails-001",
                "dependency_sha256": dependency,
                "verified_source_sha256": _source_names_digest(source_lock, {"west"}),
                "updates": {key: {"before": before, "after": after}},
            }
        )
    )
    (target if corrupt == "current" else shadow).write_bytes(b"corrupt")
    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)

    with pytest.raises(HouseHunterError, match="(?:progress|transaction) checksum differs"):
        _materialize_trails_batch(
            (region,),
            entries,
            workspace,
            batch=batches[0],
            ordered_batches=batches,
            dependency_sha256=dependency,
            maximum_bytes=1_000_000,
            source_lock_path=tmp_path / "source-lock.json",
            source_lock=source_lock,
            source_root=tmp_path / "sources",
            managed_staging_root=None,
        )


def test_completed_trails_batch_resumes_after_managed_source_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    west = tmp_path / "sources" / "west.fgb"
    calls: list[str] = []
    monkeypatch.setattr(
        "househunter.mountain_pack.verify_source_families",
        lambda *a, **k: calls.append("verified"),
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails",
        lambda *a, **k: calls.append("read") or np.ones((2, 2), dtype=np.float32),
    )

    def delete(*args: object, **kwargs: object) -> None:
        calls.append("deleted")
        west.unlink()

    monkeypatch.setattr("househunter.mountain_pack.delete_managed_sources", delete)
    arguments = {
        "batch": batches[0],
        "ordered_batches": batches,
        "dependency_sha256": "d" * 64,
        "maximum_bytes": 1_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": tmp_path,
    }

    _materialize_trails_batch((region,), entries, workspace, **arguments)
    assert calls == ["verified", "read", "deleted"]
    assert not west.exists()

    _materialize_trails_batch((region,), entries, workspace, **arguments)
    assert calls == ["verified", "read", "deleted"]


def test_overlapping_trails_batches_add_to_the_same_tile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, region, entries, source_lock, batches = _trails_batch_fixture(tmp_path)
    contributions = iter((1.0, 2.0))
    monkeypatch.setattr("househunter.mountain_pack.verify_source_families", lambda *a, **k: None)
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails",
        lambda *a, **k: np.full((2, 2), next(contributions), dtype=np.float32),
    )
    common = {
        "ordered_batches": batches,
        "dependency_sha256": "e" * 64,
        "maximum_bytes": 1_000_000,
        "source_lock_path": tmp_path / "source-lock.json",
        "source_lock": source_lock,
        "source_root": tmp_path / "sources",
        "managed_staging_root": None,
    }

    for batch in batches:
        _materialize_trails_batch((region,), entries, workspace, batch=batch, **common)

    np.testing.assert_array_equal(
        np.load(workspace / "tiles" / entries[0]["key"] / "trails.npy"),
        np.full((2, 2), 3, dtype=np.float32),
    )


def test_external_source_checkpoint_is_verified_but_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "checkpoints").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    source = external / "blocks.fgb"
    source.write_bytes(b"user-owned")
    lock = {
        "sources": [
            {
                "name": "blocks",
                "family": "blocks",
                "filename": source.name,
                "sha256": "0" * 64,
            }
        ]
    }
    _write_phase(
        workspace,
        "blocks",
        {},
        verified_source_sha256=_source_family_digest(lock, "blocks"),
    )
    verified: list[set[str]] = []
    monkeypatch.setattr(
        "househunter.mountain_pack.verify_source_families",
        lambda path, *, root, families: verified.append(families),
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.delete_managed_source_family",
        lambda *args, **kwargs: pytest.fail("external source deletion attempted"),
    )

    _complete_phase_sources(
        workspace,
        "blocks",
        family="blocks",
        source_lock_path=tmp_path / "source-lock.json",
        source_lock=lock,
        source_root=external,
        managed_staging_root=None,
    )

    assert verified == []
    assert source.read_bytes() == b"user-owned"
    checkpoint = json.loads((workspace / "checkpoints" / "blocks.json").read_text())
    assert checkpoint["deletion_authorized"] is False
    assert "sources_deleted" not in checkpoint


def test_managed_source_family_deletion_is_exact_and_requires_ownership(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    managed = ensure_owned_child(
        staging / ("a" * 16),
        staging,
        name_pattern=r"[0-9a-f]{16}",
        marker_value="staging-v1\n",
    )
    blocks = managed / "blocks.fgb"
    elevation = managed / "elevation.tif"
    blocks.write_bytes(b"blocks")
    elevation.write_bytes(b"elevation")
    lock = {
        "sources": [
            {"family": "blocks", "filename": blocks.name},
            {"family": "elevation", "filename": elevation.name},
        ]
    }

    delete_managed_source_family(lock, family="blocks", root=managed, staging_root=staging)

    assert not blocks.exists()
    assert elevation.read_bytes() == b"elevation"

    unowned = staging / ("b" * 16)
    unowned.mkdir()
    valuable = unowned / "blocks.fgb"
    valuable.write_bytes(b"keep")
    with pytest.raises(HouseHunterError, match="unowned"):
        delete_managed_source_family(lock, family="blocks", root=unowned, staging_root=staging)
    assert valuable.read_bytes() == b"keep"


def test_source_lock_v2_requires_exact_hawaii_wkt_and_vector_geometry_type() -> None:
    sources = []
    for family, filename in (
        ("blocks", "blocks.fgb"),
        ("elevation", "elevation.tif"),
        ("pad_us", "pad.gpkg"),
        ("trails", "trails.fgb"),
    ):
        source = {
            "name": family,
            "family": family,
            "filename": filename,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-09-12T00:00:00Z",
            "crs": "EPSG:5070",
            "schema": _fixture_gis_schema(filename),
            "count": 1,
            "size": 1,
            "sha256": "0" * 64,
            **(
                {"geometry_type": geometry_type}
                if (geometry_type := _geometry_type(family))
                else {}
            ),
        }
        sources.append(source)
    payload = _v2_source_lock(sources)

    payload["region_crs"]["hawaii"] = CRS.from_epsg(5070).to_wkt()
    with pytest.raises(HouseHunterError, match="Hawaii CRS"):
        _validate_source_lock_contract(payload)

    payload = _v2_source_lock(sources)
    del payload["sources"][0]["geometry_type"]
    with pytest.raises(HouseHunterError, match="geometry type"):
        _validate_source_lock_contract(payload)

    payload = _v2_source_lock(sources)
    del payload["sources"][0]["license"]
    with pytest.raises(HouseHunterError, match="invalid metadata"):
        _validate_source_lock_contract(payload)


def test_source_lock_v2_rejects_checksum_valid_non_gis_content(tmp_path: Path) -> None:
    sources = []
    for family, filename in (
        ("blocks", "blocks.fgb"),
        ("elevation", "elevation.tif"),
        ("pad_us", "pad.gpkg"),
        ("trails", "trails.fgb"),
    ):
        path = tmp_path / filename
        path.write_bytes(b"not a GIS dataset")
        source = {
            "name": family,
            "family": family,
            "filename": filename,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-09-12T00:00:00Z",
            "crs": "EPSG:5070",
            "schema": _fixture_gis_schema(filename),
            "count": 1,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        if geometry_type := _geometry_type(family):
            source["geometry_type"] = geometry_type
        sources.append(source)
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(_v2_source_lock(sources)))

    with pytest.raises(HouseHunterError, match="inspect locked Mountain|GIS metadata"):
        verify_source_lock(lock, root=tmp_path)


def test_locked_raster_metadata_must_match_actual_dataset(tmp_path: Path) -> None:
    raster = tmp_path / "elevation.tif"
    with rasterio.open(
        raster,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:5070",
        transform=from_origin(0, 2, 1, 1),
        nodata=-9999,
    ) as output:
        output.write(np.zeros((2, 2), dtype=np.float32), 1)
    contract = {
        "count": 4,
        "crs": "EPSG:5070",
        "schema": {
            "bands": 1,
            "dtypes": ["float32"],
            "nodata": -9999.0,
            "width": 2,
            "height": 2,
        },
    }
    _verify_gis_dataset(raster, contract)
    contract["count"] = 3

    with pytest.raises(HouseHunterError, match="metadata differs"):
        _verify_gis_dataset(raster, contract)


def test_region_sources_cannot_swap_locked_pad_and_trail_roles(tmp_path: Path) -> None:
    sources = [
        {"name": "blocks", "family": "blocks", "filename": "blocks.fgb"},
        {"name": "elevation", "family": "elevation", "filename": "elevation.tif"},
        {"name": "pad", "family": "pad_us", "filename": "pad.gpkg"},
        {"name": "trails", "family": "trails", "filename": "trails.fgb"},
    ]
    lock = _v2_source_lock(sources)
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks.fgb",
        elevation=(tmp_path / "elevation.tif",),
        pad_us=tmp_path / "trails.fgb",
        trails=tmp_path / "pad.gpkg",
    )

    with pytest.raises(HouseHunterError, match="role|family"):
        verify_region_sources_locked((region,), lock, root=tmp_path)


@pytest.mark.parametrize(
    ("pad_access_field", "trail_where"),
    [("Changed", "trailtype <> 'Water Trail'"), ("Pub_Access", None)],
)
def test_region_sources_cannot_change_v1_access_semantics(
    tmp_path: Path, pad_access_field: str, trail_where: str | None
) -> None:
    sources = [
        {"name": "blocks", "family": "blocks", "filename": "blocks.fgb"},
        {"name": "elevation", "family": "elevation", "filename": "elevation.tif"},
        {"name": "pad", "family": "pad_us", "filename": "pad.gpkg"},
        {"name": "trails", "family": "trails", "filename": "trails.fgb"},
    ]
    lock = _v2_source_lock(sources)
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks.fgb",
        elevation=(tmp_path / "elevation.tif",),
        pad_us=tmp_path / "pad.gpkg",
        trails=tmp_path / "trails.fgb",
        pad_access_field=pad_access_field,
        trail_where=trail_where,
    )

    with pytest.raises(HouseHunterError, match="mountain_score_v1 source semantics"):
        verify_region_sources_locked((region,), lock, root=tmp_path)


@pytest.mark.parametrize("operation", ["verify", "download"])
def test_source_lock_rejects_non_object_entries(tmp_path: Path, operation: str) -> None:
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"schema_version": 1, "sources": ["not-an-object"]}))

    with pytest.raises(HouseHunterError, match="invalid entry"):
        if operation == "verify":
            verify_source_lock(lock)
        else:
            download_sources(lock, tmp_path / "downloads")


def test_download_sources_rejects_legacy_unreviewed_lock(tmp_path: Path) -> None:
    content = b"pinned"
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "name": "fixture",
                        "filename": "source.bin",
                        "url": "https://example.test/source.bin",
                        "acquired_at": "2026-09-12T00:00:00Z",
                        "crs": "EPSG:5070",
                        "schema": {"value": "bytes"},
                        "count": 1,
                        "size": len(content),
                        "sha256": __import__("hashlib").sha256(content).hexdigest(),
                    }
                ],
            }
        )
    )
    with pytest.raises(HouseHunterError, match="source-lock v2"):
        download_sources(lock, tmp_path / "downloads")


def test_locked_archive_extraction_is_exact_and_rejects_traversal(tmp_path: Path) -> None:
    content = b"trail-data"
    archive_path = tmp_path / "trails.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("data/trails.fgb", content)
    source = {
        "archive": {
            "format": "zip",
            "root": "trails",
            "datasets": ["data/trails.fgb"],
            "total_uncompressed_size": len(content),
            "members": [
                {
                    "path": "data/trails.fgb",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }
    }
    destination = tmp_path / "sources"
    destination.mkdir()
    extracted = extract_locked_archive(source, archive_path, destination)
    assert (extracted / "data" / "trails.fgb").read_bytes() == content

    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as archive:
        archive.writestr("data/trails.fgb", content)
        archive.writestr("../escape", b"bad")
    hostile_destination = tmp_path / "hostile-sources"
    hostile_destination.mkdir()
    with pytest.raises(HouseHunterError, match="unsafe path"):
        extract_locked_archive(source, hostile, hostile_destination)
    assert not (tmp_path / "escape").exists()


def test_locked_archive_rejects_links_and_expansion_bombs(tmp_path: Path) -> None:
    link_path = tmp_path / "link.zip"
    link = zipfile.ZipInfo("data/trails.fgb")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(link_path, "w") as archive:
        archive.writestr(link, b"target")
    source = {
        "archive": {
            "format": "zip",
            "root": "links",
            "datasets": ["data/trails.fgb"],
            "total_uncompressed_size": 6,
            "members": [
                {
                    "path": "data/trails.fgb",
                    "size": 6,
                    "sha256": hashlib.sha256(b"target").hexdigest(),
                }
            ],
        }
    }
    destination = tmp_path / "link-sources"
    destination.mkdir()
    with pytest.raises(HouseHunterError, match="link, device, or encryption"):
        extract_locked_archive(source, link_path, destination)

    content = b"0" * 200_000
    bomb_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data/trails.fgb", content)
    source["archive"].update(
        {
            "root": "bomb",
            "total_uncompressed_size": len(content),
            "members": [
                {
                    "path": "data/trails.fgb",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }
    )
    with pytest.raises(HouseHunterError, match="expansion limits"):
        extract_locked_archive(source, bomb_path, destination)


def test_locked_archive_rejects_total_entry_count_before_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TooManyEntries:
        def __len__(self) -> int:
            return 200_001

        def __iter__(self) -> Any:
            raise AssertionError("oversized central directory was iterated")

    class FakeArchive:
        def __enter__(self) -> FakeArchive:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def infolist(self) -> TooManyEntries:
            return TooManyEntries()

    content = b"trail-data"
    source = {
        "archive": {
            "format": "zip",
            "root": "trails",
            "datasets": ["data/trails.fgb"],
            "total_uncompressed_size": len(content),
            "members": [
                {
                    "path": "data/trails.fgb",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }
    }
    destination = tmp_path / "sources"
    destination.mkdir()
    monkeypatch.setattr("househunter.mountain_gis.zipfile.ZipFile", lambda path: FakeArchive())

    with pytest.raises(HouseHunterError, match="entry|extraction limits"):
        extract_locked_archive(source, tmp_path / "trails.zip", destination)


def test_region_configuration_rejects_paths_outside_source_root(tmp_path: Path) -> None:
    config = tmp_path / "regions.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "regions": [
                    {
                        "name": "escape",
                        "target_crs": "EPSG:5070",
                        "blocks": "../blocks.geojson",
                        "elevation": ["elevation.tif"],
                        "pad_us": "pad.geojson",
                        "trails": "trails.geojson",
                    }
                ],
            }
        )
    )

    try:
        load_regions(config, tmp_path / "sources")
    except HouseHunterError as exc:
        assert "invalid source path" in str(exc)
    else:
        raise AssertionError("a source path outside the source root was accepted")


def test_owned_preparation_and_comparison_cleanup_is_strictly_contained(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared"
    stale = ensure_owned_child(
        prepared / f".{('a' * 32)}.work",
        prepared,
        name_pattern=r"\.[0-9a-f]{32}\.work",
        marker_value="prepared-pack-v1\n",
    )
    unrelated = prepared / "notes"
    unrelated.mkdir()

    assert prune_owned_preparation_workspaces(prepared) == [stale.name]
    assert not stale.exists()
    assert unrelated.is_dir()

    work = tmp_path / "work"
    comparison_id = "b" * 32
    comparison = ensure_owned_child(
        work / comparison_id,
        work,
        name_pattern=r"[0-9a-f]{32}",
        marker_value="comparison-v1\n",
    )
    external_root = tmp_path / "external"
    external = ensure_owned_child(
        external_root / comparison_id,
        external_root,
        name_pattern=r"[0-9a-f]{32}",
        marker_value="comparison-v1\n",
    )

    with pytest.raises(HouseHunterError, match="unowned|unsafe"):
        remove_owned_comparison_directory(external, work, expected_id=comparison_id)
    assert external.is_dir()

    remove_owned_comparison_directory(comparison, work, expected_id=comparison_id)
    assert not comparison.exists()


def test_national_fragment_contract_rejects_direct_region_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    source_lock = tmp_path / "source-lock.json"
    regions = tmp_path / "regions.json"
    source_root = tmp_path / "sources"
    source_lock.write_text("{}")
    regions.write_text("{}")
    source_root.mkdir()
    monkeypatch.setattr(
        "househunter.mountain_gis.verify_source_lock",
        lambda *args, **kwargs: {
            "schema_version": 2,
            "sources": [],
            "trail_fragment_mode": "state_clipped_globalid_v1",
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "fixture",
            "--source-lock",
            str(source_lock),
            "--regions",
            str(regions),
            "--source-root",
            str(source_root),
        ],
    )

    assert result.exit_code == 1
    assert "requires a prepared pack" in result.output


def test_vector_reader_rejects_invalid_and_wrong_geometry_types(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.geojson"
    _geojson(
        invalid,
        {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
        },
        {},
    )
    with pytest.raises(HouseHunterError, match="invalid geometry"):
        _read_geometries(
            invalid,
            target_bounds=(-1, -1, 2, 2),
            target_crs="EPSG:4326",
            expected_type_ids=frozenset({3, 6}),
        )

    polygon = tmp_path / "polygon.geojson"
    _geojson(
        polygon,
        {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        },
        {},
    )
    with pytest.raises(HouseHunterError, match="unexpected geometry type"):
        _read_geometries(
            polygon,
            target_bounds=(-1, -1, 2, 2),
            target_crs="EPSG:4326",
            expected_type_ids=frozenset({1, 5}),
        )


def test_vector_reader_splits_alaska_antimeridian_window(tmp_path: Path) -> None:
    source = tmp_path / "dateline.geojson"
    features = []
    for identifier, longitude in ((1, 179.5), (2, -179.5)):
        features.append(
            {
                "type": "Feature",
                "properties": {"id": identifier},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [longitude - 0.1, 51.4],
                            [longitude + 0.1, 51.4],
                            [longitude + 0.1, 51.6],
                            [longitude - 0.1, 51.6],
                            [longitude - 0.1, 51.4],
                        ]
                    ],
                },
            }
        )
    source.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    bounds = (-2_000_000.0, 400_000.0, -1_700_000.0, 700_000.0)

    windows = _query_bounds("EPSG:3338", "EPSG:4326", bounds)
    geometries, fields = _read_geometries(
        source,
        target_bounds=bounds,
        target_crs="EPSG:3338",
        columns=["id"],
        expected_type_ids=frozenset({3, 6}),
    )

    assert windows[0][0] > 0 and windows[0][2] == 180.0
    assert windows[1][0] == -180.0 and windows[1][2] < 0
    assert len(geometries) == 2
    assert set(fields["id"]) == {1, 2}


def test_fragment_trails_split_alaska_antimeridian_window() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE logical_feature (
            id INTEGER PRIMARY KEY,
            globalid TEXT NOT NULL,
            region TEXT NOT NULL,
            wkb BLOB NOT NULL
        );
        CREATE VIRTUAL TABLE logical_rtree USING rtree(id, minx, maxx, miny, maxy);
        """
    )
    for identifier, longitude in ((1, 179.5), (2, -179.5)):
        geometry = LineString([(longitude - 0.1, 51.5), (longitude + 0.1, 51.5)])
        connection.execute(
            "INSERT INTO logical_feature VALUES (?, ?, ?, ?)",
            (identifier, str(identifier), "alaska", bytes(shapely.to_wkb(geometry))),
        )
        connection.execute(
            "INSERT INTO logical_rtree VALUES (?, ?, ?, ?, ?)",
            (identifier, longitude - 0.1, longitude + 0.1, 51.5, 51.5),
        )

    records = _query_trail_fragment_wkbs(
        connection,
        region="alaska",
        target_crs="EPSG:3338",
        bounds=(-2_000_000.0, 400_000.0, -1_700_000.0, 700_000.0),
    )

    assert len(records) == 2
    centroids = shapely.centroid(shapely.from_wkb(records))
    assert {round(float(item.x), 1) for item in centroids} == {
        -179.5,
        179.5,
    }
    connection.close()


def test_region_builder_reads_raw_gis_and_samples_block_points(tmp_path: Path) -> None:
    longitude, latitude = -105.0, 40.0
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform(
        longitude, latitude
    )
    tile_size = 100_000
    cell_size = 10_000
    left = int(x // tile_size) * tile_size - 100_000
    top = (int(y // tile_size) + 1) * tile_size + 100_000
    elevation_path = tmp_path / "elevation.tif"
    elevation = np.tile(np.arange(30, dtype=np.float32) * 5_000, (30, 1))
    with rasterio.open(
        elevation_path,
        "w",
        driver="GTiff",
        width=30,
        height=30,
        count=1,
        dtype="float32",
        crs="EPSG:5070",
        transform=from_origin(left, top, cell_size, cell_size),
        nodata=-9999,
    ) as output:
        output.write(elevation, 1)

    blocks = tmp_path / "blocks.geojson"
    _geojson(
        blocks,
        {"type": "Point", "coordinates": [longitude, latitude]},
        {
            "GEOID20": "080130001001001",
            "POP20": 10,
            "INTPTLAT20": str(latitude),
            "INTPTLON20": str(longitude),
        },
    )
    pad = tmp_path / "pad.geojson"
    _geojson(
        pad,
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [-105.5, 39.5],
                    [-104.5, 39.5],
                    [-104.5, 40.5],
                    [-105.5, 40.5],
                    [-105.5, 39.5],
                ]
            ],
        },
        {"Pub_Access": "OA"},
    )
    trails = tmp_path / "trails.geojson"
    _geojson(
        trails,
        {"type": "LineString", "coordinates": [[-105.3, 40.0], [-104.7, 40.0]]},
        {},
    )
    frame = build_region_raw_metrics(
        RegionSources(
            name="fixture",
            target_crs="EPSG:5070",
            blocks=blocks,
            elevation=(elevation_path,),
            pad_us=pad,
            trails=trails,
        ),
        state_by_fips={"08": "CO"},
        cell_size_m=cell_size,
        tile_size_m=tile_size,
    )

    assert frame.height == 1
    assert frame["block_geoid"].item() == "080130001001001"
    assert frame["state"].item() == "CO"
    assert frame["relief_20km_m"].item() > 0
    assert frame["public_mountain_access_raw"].item() > 0
    assert frame["trail_access_raw"].item() > 0


def _prepared_tile(block_geoid: str, tile_x: int, elevation_offset: float) -> tuple[Any, ...]:
    size = 30
    elevation = np.tile(np.arange(size, dtype=np.float32) * 5_000 + elevation_offset, (size, 1))
    pad = np.ones((size, size), dtype=np.uint8)
    trails = np.zeros((size, size), dtype=np.float32)
    trails[15, 15] = 1
    samples = pl.DataFrame(
        {
            "block_geoid": [block_geoid],
            "tract_geoid": [block_geoid[:11]],
            "county_fips": [block_geoid[:5]],
            "state": ["CO"],
            "pop20": [10],
            "row": pl.Series([15], dtype=pl.Int32),
            "column": pl.Series([15], dtype=pl.Int32),
            "region": ["fixture"],
            "tile_x": [tile_x],
            "tile_y": [-1],
        }
    )
    return samples, elevation, pad, trails


def test_prepared_pack_matches_serial_workers_and_resumes_corrupt_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tiles = [
        _prepared_tile("080130001001001", -1, 0),
        _prepared_tile("080130001001002", 0, 10),
    ]
    monkeypatch.setattr(
        "househunter.mountain_pack.iter_region_tiles", lambda *args, **kwargs: iter(tiles)
    )
    source_lock = tmp_path / "source-lock.json"
    region_config = tmp_path / "regions.json"
    source_lock.write_text('{"schema_version": 1, "sources": []}\n')
    region_config.write_text('{"schema_version": 1, "regions": []}\n')
    region = RegionSources(
        name="fixture",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks",
        elevation=(tmp_path / "elevation",),
        pad_us=tmp_path / "pad",
        trails=tmp_path / "trails",
    )
    pack, lock = prepare_regions(
        (region,),
        tmp_path / "prepared",
        state_by_fips={"08": "CO"},
        source_lock_path=source_lock,
        region_config_path=region_config,
        cell_size_m=10_000,
        tile_size_m=100_000,
    )
    assert verify_prepared_pack(pack, lock)["block_count"] == 2
    monkeypatch.setattr(
        "househunter.mountain_pack.iter_region_tiles",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pack recomputed")),
    )
    reused_pack, reused_lock = prepare_regions(
        (region,),
        tmp_path / "prepared",
        state_by_fips={"08": "CO"},
        source_lock_path=source_lock,
        region_config_path=region_config,
        cell_size_m=10_000,
        tile_size_m=100_000,
    )
    assert (reused_pack, reused_lock) == (pack, lock)
    serial = pl.concat([_raw_metrics_from_tile(*tile, cell_size_m=10_000) for tile in tiles]).sort(
        "block_geoid"
    )

    one_work = tmp_path / ("1" * 64)
    parallel_work = tmp_path / ("2" * 64)
    one, one_report = build_prepared_raw_metrics(pack, lock, one_work, workers=1, resume=False)
    parallel, parallel_report = build_prepared_raw_metrics(
        pack, lock, parallel_work, workers=4, resume=False
    )

    assert one.equals(serial)
    assert parallel.equals(serial)
    assert one_report["computed_tiles"] == parallel_report["computed_tiles"] == 2

    shard = next(parallel_work.glob("*.parquet"))
    shard.write_bytes(b"corrupt")
    resumed, resumed_report = build_prepared_raw_metrics(
        pack, lock, parallel_work, workers=4, resume=True
    )
    assert resumed.equals(serial)
    assert resumed_report["computed_tiles"] == 1
    assert resumed_report["resumed_tiles"] == 1


def test_national_inventory_is_block_driven_and_storage_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = []
    for fips, state in STATE_BY_FIPS.items():
        if state in IN_SCOPE_STATES:
            rows.append(
                {
                    "block_geoid": f"{fips}0010001001001",
                    "tract_geoid": f"{fips}00100010",
                    "county_fips": f"{fips}001",
                    "state_fips": fips,
                    "pop20": 1,
                    "x": -1.0 if fips == "02" else 1.0,
                    "y": 1.0,
                }
            )
    monkeypatch.setattr("househunter.mountain_gis._blocks", lambda source: pl.DataFrame(rows))
    region = RegionSources(
        name="fixture",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks",
        elevation=(tmp_path / "elevation",),
        pad_us=tmp_path / "pad",
        trails=tmp_path / "trails",
    )

    report = qualify_national_inventory((region,), state_by_fips=STATE_BY_FIPS)

    assert report["grid"]["shape"] == [1_200, 1_200]
    assert report["tile_inventory"]["tile_count"] == 2
    assert report["tile_inventory"]["block_count"] == len(IN_SCOPE_STATES)
    assert report["storage_projection"]["managed_peak_bytes"] < 45_000_000_000


def test_preparation_resumes_only_checksum_valid_completed_tiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tiles = [
        _prepared_tile("080130001001001", -1, 0),
        _prepared_tile("080130001001002", 0, 10),
    ]
    source_lock = tmp_path / "source-lock.json"
    region_config = tmp_path / "regions.json"
    source_lock.write_text('{"schema_version": 1, "sources": []}\n')
    region_config.write_text('{"schema_version": 1, "regions": []}\n')
    region = RegionSources(
        name="fixture",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks",
        elevation=(tmp_path / "elevation",),
        pad_us=tmp_path / "pad",
        trails=tmp_path / "trails",
    )

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        yield tiles[0]
        raise HouseHunterError("interrupted")

    monkeypatch.setattr("househunter.mountain_pack.iter_region_tiles", interrupted)
    with pytest.raises(HouseHunterError, match="interrupted"):
        prepare_regions(
            (region,),
            tmp_path / "prepared",
            state_by_fips={"08": "CO"},
            source_lock_path=source_lock,
            region_config_path=region_config,
            cell_size_m=10_000,
            tile_size_m=100_000,
        )

    recomputed: list[tuple[int, int]] = []

    def resumed(*args: Any, **kwargs: Any) -> Any:
        skipped = kwargs.get("skip_tiles", set())
        for tile in tiles:
            coordinate = (int(tile[0]["tile_x"][0]), int(tile[0]["tile_y"][0]))
            if coordinate not in skipped:
                recomputed.append(coordinate)
                yield tile

    monkeypatch.setattr("househunter.mountain_pack.iter_region_tiles", resumed)
    pack, lock = prepare_regions(
        (region,),
        tmp_path / "prepared",
        state_by_fips={"08": "CO"},
        source_lock_path=source_lock,
        region_config_path=region_config,
        cell_size_m=10_000,
        tile_size_m=100_000,
    )

    assert recomputed == [(0, -1)]
    assert verify_prepared_pack(pack, lock)["block_count"] == 2


def test_v2_preparation_resumes_family_checkpoints_after_managed_raw_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    geoids = [
        f"{fips}0010001001001" for fips, state in STATE_BY_FIPS.items() if state in IN_SCOPE_STATES
    ]
    states = [STATE_BY_FIPS[value[:2]] for value in geoids]
    samples = pl.DataFrame(
        {
            "block_geoid": geoids,
            "tract_geoid": [value[:11] for value in geoids],
            "county_fips": [value[:5] for value in geoids],
            "state": states,
            "pop20": [1] * len(geoids),
            "row": pl.Series([15] * len(geoids), dtype=pl.Int32),
            "column": pl.Series([15] * len(geoids), dtype=pl.Int32),
            "region": ["conus_dc"] * len(geoids),
            "tile_x": [0] * len(geoids),
            "tile_y": [0] * len(geoids),
        }
    )
    elevation = np.tile(np.arange(30, dtype=np.float32) * 5_000, (30, 1))
    pad = np.ones((30, 30), dtype=np.uint8)
    trails = np.zeros((30, 30), dtype=np.float32)
    bounds = (-100_000.0, -100_000.0, 200_000.0, 200_000.0)
    source_items = []
    for family, filename in (
        ("blocks", "blocks.fgb"),
        ("elevation", "elevation.tif"),
        ("pad_us", "pad.gpkg"),
        ("trails", "trails.fgb"),
    ):
        source = {
            "name": family,
            "family": family,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-01-01T00:00:00Z",
            "crs": "EPSG:5070",
            "schema": _fixture_gis_schema(filename),
            "count": 1,
            "filename": filename,
            "size": 1,
            "sha256": "0" * 64,
            **(
                {"geometry_type": geometry_type}
                if (geometry_type := _geometry_type(family))
                else {}
            ),
        }
        source_items.append(source)
    payload = _v2_source_lock(source_items)
    payload["expected_states"] = {
        state: {"blocks": 1, "population": 1} for state in sorted(IN_SCOPE_STATES)
    }
    payload["block_geoid_sha256"] = hashlib.sha256(
        ("\n".join(sorted(geoids)) + "\n").encode()
    ).hexdigest()
    payload["tile_inventory"] = {
        "tile_count": 1,
        "block_count": len(geoids),
        "tiles": [
            {
                "region": "conus_dc",
                "tile_x": 0,
                "tile_y": 0,
                "blocks": len(geoids),
                "population": len(geoids),
                "block_geoid_sha256": payload["block_geoid_sha256"],
                "block_sample_sha256": block_sample_sha256(samples),
            }
        ],
    }
    payload["representative_tiles"] = _representative_tiles(
        region="conus_dc",
        tile_x=0,
        tile_y=0,
        digest=raw_metric_sha256(
            _raw_metrics_from_tile(samples, elevation, pad, trails, cell_size_m=10_000)
        ),
    )
    payload["storage_projection"] = storage_projection(
        source_items,
        prepared_pack_bytes=100_000_000,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
    )
    source_lock = tmp_path / "source-lock.json"
    source_lock.write_text(json.dumps(payload))
    regions_path = tmp_path / "regions.json"
    regions_path.write_text('{"schema_version":1,"regions":[]}\n')
    source_root = tmp_path / "staging" / ("a" * 16)
    source_root.mkdir(parents=True)
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=source_root / "blocks.fgb",
        elevation=(source_root / "elevation.tif",),
        pad_us=source_root / "pad.gpkg",
        trails=source_root / "trails.fgb",
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.iter_region_block_samples",
        lambda *args, **kwargs: iter([(samples, bounds)]),
    )
    monkeypatch.setattr("househunter.mountain_pack._elevation_index", lambda *args: ((), object()))
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_elevation",
        lambda *args, **kwargs: (elevation, from_origin(-100_000, 200_000, 10_000, 10_000)),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "househunter.mountain_pack.verify_source_families",
        lambda path, *, root, families: calls.append(next(iter(families))),
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.delete_managed_source_family",
        lambda lock, *, family, root, staging_root: calls.append(f"deleted:{family}"),
    )
    checkpoint = prepare_regions(
        (region,),
        tmp_path / "prepared",
        state_by_fips=STATE_BY_FIPS,
        source_lock_path=source_lock,
        region_config_path=regions_path,
        cell_size_m=10_000,
        source_root=source_root,
        managed_staging_root=source_root.parent,
        stop_after_family="elevation",
    )
    assert checkpoint == (None, None)
    assert calls == [
        "blocks",
        "deleted:blocks",
        "elevation",
        "deleted:elevation",
    ]

    monkeypatch.setattr(
        "househunter.mountain_pack.iter_region_block_samples",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("blocks recomputed")),
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_elevation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DEM recomputed")),
    )
    monkeypatch.setattr("househunter.mountain_pack.read_tile_pad", lambda *args, **kwargs: pad)
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails", lambda *args, **kwargs: trails
    )
    pack, lock = prepare_regions(
        (region,),
        tmp_path / "prepared",
        state_by_fips=STATE_BY_FIPS,
        source_lock_path=source_lock,
        region_config_path=regions_path,
        cell_size_m=10_000,
        source_root=source_root,
        managed_staging_root=source_root.parent,
    )

    assert verify_prepared_pack(pack, lock)["block_count"] == len(geoids)
    assert calls[-4:] == ["pad_us", "deleted:pad_us", "trails", "deleted:trails"]


def test_prepared_cli_build_promotes_and_rebuilds_queryable_snapshot(
    fixture_environment: tuple[RuntimePaths, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    geoids = []
    states = []
    for fips, state in STATE_BY_FIPS.items():
        if state in IN_SCOPE_STATES:
            geoids.append(f"{fips}0010001001001")
            states.append(state)
    geoids.extend(["010010002001001", "010010003001001"])
    states.extend(["AL", "AL"])
    size = 1_200
    samples = pl.DataFrame(
        {
            "block_geoid": geoids,
            "tract_geoid": [value[:11] for value in geoids],
            "county_fips": [value[:5] for value in geoids],
            "state": states,
            "pop20": [1] * len(geoids),
            "row": pl.Series([600] * len(geoids), dtype=pl.Int32),
            "column": pl.Series([600] * len(geoids), dtype=pl.Int32),
            "region": ["conus_dc"] * len(geoids),
            "tile_x": [0] * len(geoids),
            "tile_y": [0] * len(geoids),
        }
    )
    elevation = np.tile(np.arange(size, dtype=np.float32) * 5_000, (size, 1))
    pad = np.ones((size, size), dtype=np.uint8)
    trails = np.zeros((size, size), dtype=np.float32)
    trails[600, 600] = 1
    monkeypatch.setattr(
        "househunter.mountain_pack.iter_region_tiles",
        lambda *args, **kwargs: iter([(samples, elevation, pad, trails)]),
    )
    bounds = (-100_000.0, -100_000.0, 200_000.0, 200_000.0)
    monkeypatch.setattr(
        "househunter.mountain_pack.iter_region_block_samples",
        lambda *args, **kwargs: iter([(samples, bounds)]),
    )
    monkeypatch.setattr("househunter.mountain_pack._elevation_index", lambda *args: ((), object()))
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_elevation",
        lambda *args, **kwargs: (elevation, from_origin(-100_000, 200_000, 250, 250)),
    )
    monkeypatch.setattr("househunter.mountain_pack.read_tile_pad", lambda *args, **kwargs: pad)
    monkeypatch.setattr(
        "househunter.mountain_pack.read_tile_trails", lambda *args, **kwargs: trails
    )
    source_lock = tmp_path / "source-lock.json"
    expected_states = {
        row["state"]: {"blocks": row["blocks"], "population": row["population"]}
        for row in samples.group_by("state")
        .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
        .iter_rows(named=True)
    }
    source_items = []
    for family, filename in (
        ("blocks", "blocks.fgb"),
        ("elevation", "elevation.tif"),
        ("pad_us", "pad.gpkg"),
        ("trails", "trails.fgb"),
    ):
        source = {
            "name": family,
            "family": family,
            "url": f"https://prd-tnm.s3.amazonaws.com/{filename}",
            "acquired_at": "2026-01-01T00:00:00Z",
            "crs": "EPSG:5070",
            "schema": _fixture_gis_schema(filename),
            "count": 1,
            "filename": filename,
            "size": 1,
            "sha256": "0" * 64,
        }
        if geometry_type := _geometry_type(family):
            source["geometry_type"] = geometry_type
        source_items.append(source)
    tile_geoid_digest = hashlib.sha256(("\n".join(sorted(geoids)) + "\n").encode()).hexdigest()
    tile_sample_digest = block_sample_sha256(samples)
    source_payload = {
        **_v2_source_lock(source_items),
        "expected_states": expected_states,
        "block_geoid_sha256": hashlib.sha256(
            ("\n".join(sorted(geoids)) + "\n").encode()
        ).hexdigest(),
        "tile_inventory": {
            "tile_count": 1,
            "block_count": len(geoids),
            "tiles": [
                {
                    "region": "conus_dc",
                    "tile_x": 0,
                    "tile_y": 0,
                    "blocks": len(geoids),
                    "population": len(geoids),
                    "block_geoid_sha256": tile_geoid_digest,
                    "block_sample_sha256": tile_sample_digest,
                }
            ],
        },
        "sources": source_items,
    }
    source_payload["representative_tiles"] = _representative_tiles(
        region="conus_dc",
        tile_x=0,
        tile_y=0,
        digest=raw_metric_sha256(
            _raw_metrics_from_tile(samples, elevation, pad, trails, cell_size_m=250)
        ),
    )
    source_payload["storage_projection"] = storage_projection(
        source_items,
        prepared_pack_bytes=100_000_000,
        active_and_rollback_release_bytes=1,
        candidate_release_bytes=1,
        work_shard_bytes=1,
        comparison_shard_bytes=1,
    )
    source_lock.write_text(json.dumps(source_payload))
    region_config = tmp_path / "regions.json"
    region_config.write_text('{"schema_version": 1, "regions": []}\n')
    region = RegionSources(
        name="conus_dc",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks",
        elevation=(tmp_path / "elevation",),
        pad_us=tmp_path / "pad",
        trails=tmp_path / "trails",
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.verify_source_families",
        lambda *args, **kwargs: source_payload,
    )
    pack, lock = prepare_regions(
        (region,),
        tmp_path / "prepared",
        state_by_fips=STATE_BY_FIPS,
        source_lock_path=source_lock,
        region_config_path=region_config,
        cell_size_m=250,
        tile_size_m=100_000,
        source_root=tmp_path,
    )
    monkeypatch.setattr(
        "househunter.mountain_gis.load_source_lock_contract",
        lambda *args, **kwargs: source_payload,
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.load_source_lock_contract",
        lambda *args, **kwargs: source_payload,
    )

    built = CliRunner().invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "fixture-national",
            "--source-lock",
            str(source_lock),
            "--prepared-pack",
            str(pack),
            "--prepared-lock",
            str(lock),
            "--workers",
            "2",
            "--fresh",
        ],
    )

    assert built.exit_code == 0, built.output
    assert (paths.data / "mountain" / "current.json").is_file()
    assert paths.current.is_file()
    ranked = CliRunner().invoke(app, ["rank", "--metric", "mountain", "--limit", "1"])
    assert ranked.exit_code == 0, ranked.output
    assert len(ranked.output.splitlines()) == 2
    assert not (paths.data / "mountain" / "work" / pack.name).exists()

    prior_pointer = (paths.data / "mountain" / "current.json").read_bytes()
    monkeypatch.setattr(
        "househunter.cli.build_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(HouseHunterError("snapshot failed")),
    )
    failed = CliRunner().invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "fixture-failed-snapshot",
            "--source-lock",
            str(source_lock),
            "--prepared-pack",
            str(pack),
            "--prepared-lock",
            str(lock),
            "--workers",
            "2",
            "--fresh",
        ],
    )
    assert failed.exit_code == 1
    assert "snapshot failed" in failed.output
    assert (paths.data / "mountain" / "current.json").read_bytes() == prior_pointer
    assert (paths.data / "mountain" / "work" / pack.name).is_dir()
