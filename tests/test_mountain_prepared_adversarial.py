from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
import shapely
from rasterio.transform import from_origin
from typer.testing import CliRunner

from househunter.cli import app
from househunter.errors import HouseHunterError
from househunter.mountain import (
    RAW_PRECISION,
    access_metrics,
    aggregate_scores,
    score_blocks,
    window_cells,
)
from househunter.mountain_gis import (
    RegionSources,
    _raw_metrics_from_tile,
    iter_region_tiles,
)
from househunter.mountain_pack import (
    allocated_size,
    build_prepared_raw_metrics,
    ensure_storage_budget,
    prepare_regions,
    prune_owned_prepared_packs,
    remove_owned_staging_directory,
    remove_owned_work_directory,
    verify_prepared_pack,
)


def test_allocated_size_tolerates_worker_file_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vanishing = tmp_path / "tile.part"
    vanishing.write_bytes(b"in progress")
    original_lstat = Path.lstat

    def racing_lstat(path: Path, *args: object, **kwargs: object) -> Any:
        if path == vanishing:
            path.unlink(missing_ok=True)
            raise FileNotFoundError(path)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", racing_lstat)

    assert allocated_size(tmp_path) >= 0


def _tile(
    block_geoid: str,
    tile_x: int,
    *,
    elevation_offset: float = 0,
) -> tuple[pl.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    size = 30
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
    elevation = np.tile(
        np.arange(size, dtype=np.float32) * 5_000 + elevation_offset,
        (size, 1),
    )
    pad = np.ones((size, size), dtype=np.uint8)
    trails = np.zeros((size, size), dtype=np.float32)
    trails[15, 15] = 1
    return samples, elevation, pad, trails


def _pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tiles: list[tuple[pl.DataFrame, np.ndarray, np.ndarray, np.ndarray]],
    *,
    name: str = "fixture",
) -> tuple[Path, Path]:
    root = tmp_path / name
    root.mkdir()
    source_lock = root / "source-lock.json"
    region_config = root / "regions.json"
    source_lock.write_text('{"schema_version": 1, "sources": []}\n')
    region_config.write_text('{"schema_version": 1, "regions": []}\n')
    monkeypatch.setattr(
        "househunter.mountain_pack.iter_region_tiles",
        lambda *args, **kwargs: iter(tiles),
    )
    region = RegionSources(
        name="fixture",
        target_crs="EPSG:5070",
        blocks=root / "blocks",
        elevation=(root / "elevation",),
        pad_us=root / "pad",
        trails=root / "trails",
    )
    return prepare_regions(
        (region,),
        root / "prepared",
        state_by_fips={"08": "CO"},
        source_lock_path=source_lock,
        region_config_path=region_config,
        cell_size_m=10_000,
        tile_size_m=100_000,
    )


def test_prepared_pack_external_lock_rejects_same_count_population_geoid_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, original_lock = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001001", -1)],
        name="original",
    )
    substituted, _ = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001999", -1)],
        name="substituted",
    )

    assert original.name != substituted.name
    with pytest.raises(HouseHunterError, match="identity"):
        verify_prepared_pack(substituted, original_lock)


def test_prepared_pack_rejects_toolchain_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, lock = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001001", -1)],
    )
    monkeypatch.setattr(
        "househunter.mountain_pack._toolchain",
        lambda: {"python": "different-toolchain"},
    )

    with pytest.raises(HouseHunterError, match="incompatible"):
        verify_prepared_pack(pack, lock)


def test_prepared_pack_rejects_unmanifested_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, lock = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001001", -1)],
    )
    (pack / "unreviewed-source.vrt").write_bytes(b"")

    with pytest.raises(HouseHunterError, match="unexpected|inventory|envelope"):
        verify_prepared_pack(pack, lock)


def test_prepared_pack_rejects_symlinked_tile_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, lock = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001001", -1)],
    )
    external_tiles = tmp_path / "external-tiles"
    (pack / "tiles").rename(external_tiles)
    (pack / "tiles").symlink_to(external_tiles, target_is_directory=True)

    with pytest.raises(HouseHunterError, match="symlink"):
        verify_prepared_pack(pack, lock)


def test_prepared_pack_pruning_retains_only_selected_owned_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, first_lock = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001001", -1)],
        name="first",
    )
    second, _ = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001002", 0)],
        name="second",
    )
    destination = second.parent
    moved_first = destination / first.name
    first.rename(moved_first)
    moved_lock = destination / f"{first.name}.lock.json"
    first_lock.rename(moved_lock)

    removed = prune_owned_prepared_packs(destination, keep=second)

    assert removed == [first.name]
    assert second.is_dir()
    assert not moved_first.exists()
    assert not moved_lock.exists()


def test_prepared_build_rejects_worker_bounds_and_fresh_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for workers in (0, 5):
        with pytest.raises(HouseHunterError, match="one and four"):
            build_prepared_raw_metrics(
                tmp_path / "missing-pack",
                tmp_path / "missing-lock",
                tmp_path / f"work-{workers}",
                workers=workers,
            )

    pack, lock = _pack(
        tmp_path,
        monkeypatch,
        [_tile("080130001001001", -1)],
    )
    work = tmp_path / ("1" * 64)
    build_prepared_raw_metrics(pack, lock, work, workers=1, resume=False)

    with pytest.raises(HouseHunterError, match="fresh.*new empty"):
        build_prepared_raw_metrics(pack, lock, work, workers=1, resume=False)

    run_path = work / "run.json"
    run = json.loads(run_path.read_text())
    run["pack_id"] = "0" * 64
    run_path.write_text(json.dumps(run))
    with pytest.raises(HouseHunterError, match="incompatible inputs"):
        build_prepared_raw_metrics(pack, lock, work, workers=1, resume=True)


def test_resume_recomputes_both_swapped_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, lock = _pack(
        tmp_path,
        monkeypatch,
        [
            _tile("080130001001001", -1),
            _tile("080130001001002", 0, elevation_offset=10),
        ],
    )
    work = tmp_path / ("1" * 64)
    expected, _ = build_prepared_raw_metrics(pack, lock, work, workers=1, resume=False)
    first, second = sorted(work.glob("*.parquet"))
    first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
    first.write_bytes(second_bytes)
    second.write_bytes(first_bytes)

    resumed, report = build_prepared_raw_metrics(pack, lock, work, workers=1, resume=True)

    assert resumed.equals(expected)
    assert report["resumed_tiles"] == 0
    assert report["computed_tiles"] == 2


def test_failed_shard_keeps_only_verified_work_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, lock = _pack(
        tmp_path,
        monkeypatch,
        [
            _tile("080130001001001", -1),
            _tile("080130001001002", 0, elevation_offset=10),
        ],
    )
    work = tmp_path / ("1" * 64)

    def reservation(tile: dict[str, Any]) -> int:
        return 2_000_000 if tile["tile_x"] == -1 else 0

    monkeypatch.setattr("househunter.mountain_pack._reservation_bytes", reservation)
    with pytest.raises(HouseHunterError, match="exceeded its reservation"):
        build_prepared_raw_metrics(pack, lock, work, workers=1, resume=False)

    assert len(list(work.glob("*.parquet"))) == 1
    assert not list(work.glob("*.part"))

    monkeypatch.setattr("househunter.mountain_pack._reservation_bytes", lambda tile: 2_000_000)
    resumed, report = build_prepared_raw_metrics(pack, lock, work, workers=1, resume=True)
    assert resumed.height == 2
    assert report["resumed_tiles"] == 1
    assert report["computed_tiles"] == 1


def test_signed_core_boundaries_use_floor_and_dem_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xs = [-100_000.0, -0.001, 0.0, 99_999.999, 100_000.0]
    geoids = [f"08013000100{index:04d}" for index in range(1, len(xs) + 1)]
    blocks = pl.DataFrame(
        {
            "block_geoid": geoids,
            "tract_geoid": [value[:11] for value in geoids],
            "county_fips": [value[:5] for value in geoids],
            "state_fips": ["08"] * len(xs),
            "pop20": [1] * len(xs),
            "x": xs,
            "y": [0.0] * len(xs),
        }
    )
    monkeypatch.setattr("househunter.mountain_gis._blocks", lambda source: blocks)

    class ReverseIndex:
        def query(self, geometry: object) -> np.ndarray:
            return np.array([1, 0])

    first = tmp_path / "first.tif"
    second = tmp_path / "second.tif"
    monkeypatch.setattr(
        "househunter.mountain_gis._elevation_index",
        lambda paths, crs: ((first, second), ReverseIndex()),
    )
    observed_precedence: list[tuple[Path, ...]] = []

    def elevation(
        paths: tuple[Path, ...],
        *,
        bounds: tuple[float, float, float, float],
        target_crs: str,
        cell_size_m: float,
    ) -> tuple[np.ndarray, Any]:
        observed_precedence.append(paths)
        return (
            np.zeros((30, 30), dtype=np.float32),
            from_origin(bounds[0], bounds[3], cell_size_m, cell_size_m),
        )

    monkeypatch.setattr("househunter.mountain_gis._read_elevation", elevation)
    monkeypatch.setattr(
        "househunter.mountain_gis._read_geometries",
        lambda *args, **kwargs: (np.array([], dtype=object), {}),
    )
    region = RegionSources(
        name="fixture",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks",
        elevation=(first, second),
        pad_us=tmp_path / "pad",
        trails=tmp_path / "trails",
    )

    samples = pl.concat(
        [
            item[0]
            for item in iter_region_tiles(
                region,
                state_by_fips={"08": "CO"},
                cell_size_m=10_000,
                tile_size_m=100_000,
            )
        ]
    )

    assignments = dict(samples.select("block_geoid", "tile_x").iter_rows())
    assert [assignments[value] for value in geoids] == [-1, -1, 0, 0, 1]
    assert observed_precedence and all(paths == (first, second) for paths in observed_precedence)


def test_pad_overlap_replaces_in_source_order_and_duplicate_trails_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    block_geoid = "080130001001001"
    blocks = pl.DataFrame(
        {
            "block_geoid": [block_geoid],
            "tract_geoid": [block_geoid[:11]],
            "county_fips": [block_geoid[:5]],
            "state_fips": ["08"],
            "pop20": [1],
            "x": [50_000.0],
            "y": [50_000.0],
        }
    )
    monkeypatch.setattr("househunter.mountain_gis._blocks", lambda source: blocks)

    class OneElevation:
        def query(self, geometry: object) -> np.ndarray:
            return np.array([0])

    elevation_path = tmp_path / "elevation.tif"
    monkeypatch.setattr(
        "househunter.mountain_gis._elevation_index",
        lambda paths, crs: ((elevation_path,), OneElevation()),
    )

    def elevation(
        paths: tuple[Path, ...],
        *,
        bounds: tuple[float, float, float, float],
        target_crs: str,
        cell_size_m: float,
    ) -> tuple[np.ndarray, Any]:
        return (
            np.zeros((30, 30), dtype=np.float32),
            from_origin(bounds[0], bounds[3], cell_size_m, cell_size_m),
        )

    monkeypatch.setattr("househunter.mountain_gis._read_elevation", elevation)
    polygon = shapely.box(40_000, 40_000, 60_000, 60_000)
    trail = shapely.LineString([(40_000, 50_000), (60_000, 50_000)])

    def geometries(*args: Any, **kwargs: Any) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        if kwargs.get("columns"):
            return (
                np.array([polygon, polygon], dtype=object),
                {"Pub_Access": np.array(["Open", "Closed"], dtype=object)},
            )
        return np.array([trail, trail], dtype=object), {}

    monkeypatch.setattr("househunter.mountain_gis._read_geometries", geometries)
    region = RegionSources(
        name="fixture",
        target_crs="EPSG:5070",
        blocks=tmp_path / "blocks",
        elevation=(elevation_path,),
        pad_us=tmp_path / "pad",
        trails=tmp_path / "trails",
    )

    samples, _, pad, trails = next(
        iter_region_tiles(
            region,
            state_by_fips={"08": "CO"},
            cell_size_m=10_000,
            tile_size_m=100_000,
        )
    )

    row, column = samples.select("row", "column").row(0)
    assert pad[row, column] == 3
    assert trails.max() == 2


def test_prepared_sample_bounds_are_inclusive_only_at_last_cell() -> None:
    block_geoid = "080130001001001"

    def samples(row: int, column: int) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "block_geoid": [block_geoid],
                "tract_geoid": [block_geoid[:11]],
                "county_fips": [block_geoid[:5]],
                "state": ["CO"],
                "pop20": [1],
                "row": pl.Series([row], dtype=pl.Int32),
                "column": pl.Series([column], dtype=pl.Int32),
            }
        )

    elevation = np.arange(9, dtype=np.float32).reshape(3, 3)
    pad = np.zeros((3, 3), dtype=np.uint8)
    trails = np.zeros((3, 3), dtype=np.float32)
    assert (
        _raw_metrics_from_tile(samples(2, 2), elevation, pad, trails, cell_size_m=10_000).height
        == 1
    )
    for row, column in ((-1, 0), (0, -1), (3, 0), (0, 3)):
        with pytest.raises(HouseHunterError, match="outside its tile"):
            _raw_metrics_from_tile(samples(row, column), elevation, pad, trails, cell_size_m=10_000)


def _raw_blocks(
    populations: list[int],
    *,
    component_values: list[float | None] | None = None,
) -> pl.DataFrame:
    count = len(populations)
    geoids = [f"01001000100{index:04d}" for index in range(1, count + 1)]
    values = component_values or [float(index) for index in range(1, count + 1)]
    columns: dict[str, object] = {
        "block_geoid": geoids,
        "tract_geoid": [value[:11] for value in geoids],
        "county_fips": [value[:5] for value in geoids],
        "state": ["AL"] * count,
        "pop20": populations,
    }
    for column in RAW_PRECISION:
        columns[column] = (
            values
            if column
            in {
                "relief_20km_m",
                "rugged_fraction_20km",
                "public_mountain_access_raw",
                "trail_access_raw",
            }
            else [0.0] * count
        )
    columns["rugged_fraction_20km"] = [
        None if value is None else min(float(value) / 100, 1.0) for value in values
    ]
    return pl.DataFrame(columns)


def test_national_ecdf_preserves_strict_below_tie_mass_across_tiles() -> None:
    raw = _raw_blocks([10, 20, 30, 40], component_values=[10.0, 20.0, 20.0, 30.0])

    scored = score_blocks(raw)

    assert scored["relief_20km_pct"].to_list() == [0.0, 10.0, 10.0, 60.0]
    assert scored["mountain_score"].to_list() == [0.0, 10.0, 10.0, 60.0]


def test_component_null_only_nulls_its_percentile_and_composite() -> None:
    raw = _raw_blocks([50, 50], component_values=[10.0, 20.0]).with_columns(
        pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("relief_20km_m"))
        .alias("relief_20km_m")
    )

    scored = score_blocks(raw, minimum_coverage=0)
    incomplete = scored.row(1, named=True)

    assert incomplete["relief_20km_pct"] is None
    assert incomplete["mountain_score"] is None
    assert incomplete["rugged_pct"] == 50.0
    assert incomplete["public_mountain_access_pct"] == 50.0
    assert incomplete["trail_access_pct"] == 50.0


@pytest.mark.parametrize(
    ("populations", "accepted"),
    [([995, 5], True), ([994, 6], False)],
)
def test_national_population_coverage_gate_at_99_5_percent(
    populations: list[int], accepted: bool
) -> None:
    raw = _raw_blocks(populations, component_values=[10.0, 20.0]).with_columns(
        pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("relief_20km_m"))
        .alias("relief_20km_m")
    )

    if accepted:
        score_blocks(raw)
    else:
        with pytest.raises(HouseHunterError, match="99.400%.*99.500%"):
            score_blocks(raw)


@pytest.mark.parametrize(
    ("populations", "status", "available"),
    [([90, 10], "partial", True), ([89, 11], "insufficient_coverage", False)],
)
def test_local_population_gate_at_90_percent(
    populations: list[int], status: str, available: bool
) -> None:
    scored = score_blocks(_raw_blocks(populations), minimum_coverage=0).with_columns(
        pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("mountain_score"))
        .alias("mountain_score")
    )

    aggregate = aggregate_scores(scored, "tract_geoid").row(0, named=True)

    assert aggregate["mountain_population_coverage"] == populations[0] / sum(populations)
    assert aggregate["mountain_coverage_status"] == status
    assert (aggregate["mountain_score"] is not None) is available


def test_zero_population_group_never_publishes_extreme_score() -> None:
    raw = _raw_blocks([1, 0], component_values=[1.0, 9_999.0]).with_columns(
        pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.lit("010010002001001"))
        .otherwise(pl.col("block_geoid"))
        .alias("block_geoid"),
        pl.when(pl.int_range(pl.len()) == 1)
        .then(pl.lit("01001000200"))
        .otherwise(pl.col("tract_geoid"))
        .alias("tract_geoid"),
    )
    scored = score_blocks(raw, minimum_coverage=0)

    aggregate = (
        aggregate_scores(scored, "tract_geoid")
        .filter(pl.col("place_id") == "01001000200")
        .row(0, named=True)
    )

    assert aggregate["mountain_population_coverage"] == 0
    assert aggregate["mountain_coverage_status"] == "zero_population"
    assert aggregate["mountain_score"] is None


def test_distance_and_window_threshold_boundaries() -> None:
    assert {radius: window_cells(radius, 250) for radius in (5, 10, 15, 20, 25, 30, 40)} == {
        5: 35,
        10: 71,
        15: 107,
        20: 141,
        25: 177,
        30: 213,
        40: 283,
    }
    mountain = np.ones((1, 102), dtype=bool)
    access = np.zeros_like(mountain, dtype=np.uint8)
    trails = np.zeros_like(mountain, dtype=np.float32)
    trails[0, 0] = 1

    metrics = access_metrics(mountain, access, trails, cell_size_m=1_000)

    assert metrics["nearest_mountain_trail_km"][0, 100] == 100
    assert np.isnan(metrics["nearest_mountain_trail_km"][0, 101])


def test_cleanup_refuses_marked_but_unrecognized_work_directory(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    unrecognized = work_root / "user-owned"
    unrecognized.mkdir(parents=True)
    (unrecognized / ".househunter-mountain-owned").write_text("forged\n")
    valuable = unrecognized / "valuable.txt"
    valuable.write_text("keep")

    with pytest.raises(HouseHunterError, match="Refusing to clean"):
        remove_owned_work_directory(unrecognized, work_root)

    assert valuable.read_text() == "keep"


def test_storage_reservation_enforces_engineering_ceiling_and_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "mountain"
    monkeypatch.setattr("househunter.mountain_pack.allocated_size", lambda path: 45_000_000_000)
    monkeypatch.setattr(
        "househunter.mountain_pack.shutil.disk_usage",
        lambda path: type("Usage", (), {"free": 100_000_000_000})(),
    )
    ensure_storage_budget(managed)
    with pytest.raises(HouseHunterError, match="engineering ceiling"):
        ensure_storage_budget(managed, reserve_bytes=1)

    monkeypatch.setattr("househunter.mountain_pack.allocated_size", lambda path: 0)
    monkeypatch.setattr(
        "househunter.mountain_pack.shutil.disk_usage",
        lambda path: type("Usage", (), {"free": 10_000_000_000})(),
    )
    ensure_storage_budget(managed)
    with pytest.raises(HouseHunterError, match="free space"):
        ensure_storage_budget(managed, reserve_bytes=1)


def test_storage_budget_rejects_symlinked_managed_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    managed = tmp_path / "mountain"
    managed.symlink_to(actual, target_is_directory=True)

    with pytest.raises(HouseHunterError, match="symlink|managed root"):
        ensure_storage_budget(managed)


def test_staging_cleanup_rejects_unowned_hash_shaped_directory(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging = staging_root / ("a" * 16)
    staging.mkdir(parents=True)
    source = staging / "source.tif"
    source.write_text("preserve")

    with pytest.raises(HouseHunterError, match="Refusing to clean unowned"):
        remove_owned_staging_directory(staging, staging_root)

    assert source.read_text() == "preserve"


def test_prepared_build_cli_requires_independent_v2_source_lock() -> None:
    result = CliRunner().invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "fixture",
            "--prepared-pack",
            "pack",
            "--prepared-lock",
            "pack.lock.json",
        ],
    )

    assert result.exit_code == 1
    assert "require --source-lock" in result.output


def test_prepare_cli_rejects_legacy_source_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source_lock = tmp_path / "source-lock.json"
    source_lock.write_text('{"schema_version": 1, "sources": []}\n')
    regions = tmp_path / "regions.json"
    regions.write_text('{"schema_version": 1, "regions": []}\n')
    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))

    result = CliRunner().invoke(
        app,
        [
            "mountain",
            "prepare",
            "--source-lock",
            str(source_lock),
            "--source-root",
            str(source_root),
            "--regions",
            str(regions),
        ],
    )

    assert result.exit_code == 1
    assert "source-lock v2" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--prepared-pack", "pack"],
        ["--prepared-lock", "lock", "--raw-blocks", "raw.parquet"],
        ["--prepared-pack", "pack", "--prepared-lock", "lock", "--raw-blocks", "raw.parquet"],
    ],
)
def test_prepared_cli_requires_exactly_one_complete_input_mode(arguments: list[str]) -> None:
    result = CliRunner().invoke(
        app,
        ["mountain", "build", "--data-release", "fixture", *arguments],
    )

    assert result.exit_code != 0
    assert "exactly one" in result.output or "must be provided together" in result.output
