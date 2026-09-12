from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import polars as pl
import pytest
import rasterio
from pyproj import CRS, Transformer
from rasterio.transform import from_origin
from typer.testing import CliRunner

from househunter.cli import app
from househunter.config import RuntimePaths, sha256_file
from househunter.errors import HouseHunterError
from househunter.geography import STATE_BY_FIPS
from househunter.mountain import IN_SCOPE_STATES
from househunter.mountain_gis import (
    RegionSources,
    _raw_metrics_from_tile,
    _read_geometries,
    _source_download_reservation,
    _validate_source_lock_contract,
    _verify_gis_dataset,
    build_region_raw_metrics,
    download_sources,
    extract_locked_archive,
    load_regions,
    qualify_national_inventory,
    verify_region_sources_locked,
    verify_source_lock,
)
from househunter.mountain_pack import (
    build_prepared_raw_metrics,
    prepare_regions,
    verify_prepared_pack,
)


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
            "hawaii": CRS.from_user_input("ESRI:102007").to_wkt(),
        },
        "elevation_precedence": ["elevation"],
        "tile_inventory": {
            "tile_count": 1,
            "block_count": 51,
            "tiles": [
                {
                    "region": "conus_dc",
                    "tile_x": 0,
                    "tile_y": 0,
                    "blocks": 51,
                    "population": 51,
                }
            ],
        },
        "storage_projection": {
            "prepared_pack_bytes": 1,
            "active_and_rollback_release_bytes": 1,
            "candidate_release_bytes": 1,
            "work_shard_bytes": 1,
            "safety_reserve_bytes": 7_000_000_000,
            "managed_peak_bytes": 7_000_000_004,
        },
        "sources": sources,
    }


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


def test_source_lock_v2_requires_reviewed_https_and_national_digests() -> None:
    source_specs = [
        ("blocks", "blocks.fgb"),
        ("elevation", "elevation.tif"),
        ("pad_us", "pad.gpkg"),
        ("trails", "trails.fgb"),
    ]
    sources = []
    for family, filename in source_specs:
        sources.append(
            {
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
        )
    payload = _v2_source_lock(sources)
    assert set(STATE_BY_FIPS.values()) >= set(payload["expected_states"])
    assert _validate_source_lock_contract(payload) == {"prd-tnm.s3.amazonaws.com"}

    payload["sources"][0]["url"] += "?token=secret"
    with pytest.raises(HouseHunterError, match="HTTPS host policy"):
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
        sources.append(
            {
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
        )
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
        {"Pub_Access": "Open"},
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
    source_lock = tmp_path / "source-lock.json"
    expected_states = {state: {"blocks": 1, "population": 1} for state in sorted(IN_SCOPE_STATES)}
    source_items = [
        {
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
        for family, filename in (
            ("blocks", "blocks.fgb"),
            ("elevation", "elevation.tif"),
            ("pad_us", "pad.gpkg"),
            ("trails", "trails.fgb"),
        )
    ]
    source_lock.write_text(
        json.dumps(
            {
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
                        }
                    ],
                },
                "storage_projection": {
                    "prepared_pack_bytes": 100_000_000,
                    "active_and_rollback_release_bytes": 1,
                    "candidate_release_bytes": 1,
                    "work_shard_bytes": 1,
                    "safety_reserve_bytes": 7_000_000_000,
                    "managed_peak_bytes": 7_100_000_003,
                },
                "sources": source_items,
            }
        )
    )
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
    pack, lock = prepare_regions(
        (region,),
        tmp_path / "prepared",
        state_by_fips=STATE_BY_FIPS,
        source_lock_path=source_lock,
        region_config_path=region_config,
        cell_size_m=250,
        tile_size_m=100_000,
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
