from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from househunter.config import sha256_file
from househunter.errors import HouseHunterError
from househunter.mountain_gis import (
    RegionSources,
    _read_geometries,
    build_region_raw_metrics,
    download_sources,
    load_regions,
    verify_source_lock,
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


@pytest.mark.parametrize("operation", ["verify", "download"])
def test_source_lock_rejects_non_object_entries(tmp_path: Path, operation: str) -> None:
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"schema_version": 1, "sources": ["not-an-object"]}))

    with pytest.raises(HouseHunterError, match="invalid entry"):
        if operation == "verify":
            verify_source_lock(lock)
        else:
            download_sources(lock, tmp_path / "downloads")


def test_download_sources_reuses_verified_cache_without_network(tmp_path: Path) -> None:
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
    destination = tmp_path / "downloads"
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
    )
    download_sources(lock, destination, client=client)
    assert (destination / "source.bin").read_bytes() == content

    unavailable = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(AssertionError("unexpected download"))
        )
    )
    download_sources(lock, destination, client=unavailable)
    assert (destination / "source.bin").read_bytes() == content


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
