from __future__ import annotations

import json
from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.responses import JSONResponse
from typer.testing import CliRunner

import househunter.mountain_migration as mountain_migration
from househunter.build import BUILD_SCHEMA_VERSION
from househunter.cli import app
from househunter.config import (
    RuntimePaths,
    atomic_write_json,
    canonical_json,
    sha256_bytes,
    sha256_file,
)
from househunter.errors import HouseHunterError
from househunter.geography import STATE_BY_FIPS
from househunter.mountain import (
    AGGREGATE_MEANS,
    BUNDLED_COMPACT_RELEASE,
    IN_SCOPE_STATES,
    access_metrics,
    aggregate_scores,
    current_compact_release,
    load_compact_release,
    magnitude_values,
    national_block_geoid_sha256,
    promote_release,
    prune_owned_compact_fallbacks,
    prune_owned_releases,
    score_blocks,
    stage_compact_fallback,
    stage_validated_release,
    terrain_metrics,
    validate_national_expectations,
    validate_release,
    window_cells,
    write_and_promote_compact_fallback,
    write_and_promote_release,
    write_compact_bundle,
    write_release,
)
from househunter.mountain_legacy_v1 import validate_legacy_v1_release
from househunter.mountain_paths import OWNERSHIP_MARKER

_SIX_PLACES = Decimal("0.000001")
_FOUR_PLACES = Decimal("0.0001")


def _magnitude_oracle(values: list[float | None]) -> list[float | None]:
    """Independent decimal oracle for six-place bases and four-place magnitudes."""
    canonical = [
        None
        if value is None
        else Decimal(str(value)).quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN)
        for value in values
    ]
    peers = [value for value in canonical if value is not None]
    with localcontext() as context:
        context.prec = 40
        result: list[float | None] = []
        for value in canonical:
            if value is None:
                result.append(None)
                continue
            tail = sum(peer >= value for peer in peers)
            magnitude = (
                (Decimal(len(peers)) / Decimal(tail))
                .log10()
                .quantize(
                    _FOUR_PLACES,
                    rounding=ROUND_HALF_EVEN,
                )
            )
            result.append(0.0 if magnitude == 0 else float(magnitude))
    return result


def _component_blocks(rows: list[dict[str, object]]) -> pl.DataFrame:
    """Build already-scored blocks so aggregation tests isolate v2 behavior."""
    template = score_blocks(_raw_blocks()).head(1)
    blocks: list[pl.DataFrame] = []
    for index, row in enumerate(rows, start=1):
        tract = str(row.get("tract", f"01001{index:06d}"))
        components = row.get("components", (0.0, 0.0, 0.0, 0.0))
        if components is None:
            relief = rugged = public = trail = None
            block_score = None
        else:
            relief, rugged, public, trail = components
            block_score = round(
                0.45 * float(relief)
                + 0.20 * float(rugged)
                + 0.20 * float(public)
                + 0.15 * float(trail),
                2,
            )
        blocks.append(
            template.with_columns(
                pl.lit(f"{tract}{int(row.get('block', 1)):04d}").alias("block_geoid"),
                pl.lit(tract).alias("tract_geoid"),
                pl.lit(str(row.get("county", tract[:5]))).alias("county_fips"),
                pl.lit(str(row.get("state", "AL"))).alias("state"),
                pl.lit(int(row.get("pop", 1)), dtype=pl.Int64).alias("pop20"),
                pl.lit(relief, dtype=pl.Float64).alias("relief_20km_pct"),
                pl.lit(rugged, dtype=pl.Float64).alias("rugged_pct"),
                pl.lit(public, dtype=pl.Float64).alias("public_mountain_access_pct"),
                pl.lit(trail, dtype=pl.Float64).alias("trail_access_pct"),
                pl.lit(block_score, dtype=pl.Float64).alias("mountain_score"),
            )
        )
    return pl.concat(blocks)


def _base_oracle(rows: list[dict[str, object]]) -> dict[str, Decimal | None]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["tract"]), []).append(row)
    result: dict[str, Decimal | None] = {}
    for tract, blocks in grouped.items():
        total_population = sum(int(block["pop"]) for block in blocks)
        covered = [block for block in blocks if block.get("components") is not None]
        covered_population = sum(int(block["pop"]) for block in covered)
        if (
            total_population <= 0
            or covered_population <= 0
            or Decimal(covered_population) / Decimal(total_population) < Decimal("0.9")
        ):
            result[tract] = None
            continue
        numerator = 0
        for block in covered:
            component_hundredths = [int(Decimal(str(value)) * 100) for value in block["components"]]
            units = sum(
                weight * component
                for weight, component in zip((45, 20, 20, 15), component_hundredths, strict=True)
            )
            numerator += units * int(block["pop"])
        result[tract] = (
            Decimal(numerator) / Decimal(covered_population) / Decimal(10_000)
        ).quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN)
    return result


def _rewrite_manifest_identity(
    release: Path, mutate: Callable[[dict[str, object]], None]
) -> dict[str, object]:
    path = release / "manifest.json"
    manifest = json.loads(path.read_text())
    mutate(manifest)
    identity = {key: item for key, item in manifest.items() if key != "release_id"}
    manifest["release_id"] = sha256_bytes(canonical_json(identity))[:16]
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def test_window_cells_uses_nearest_odd_same_area_square() -> None:
    assert window_cells(5, 250) == 35
    assert window_cells(10, 250) == 71
    assert window_cells(20, 250) == 141
    assert window_cells(40, 250) == 283


def test_terrain_metrics_handles_thresholds_and_nodata() -> None:
    elevation = np.tile(np.arange(9, dtype=float) * 100, (9, 1))
    elevation[0, 0] = np.nan

    metrics = terrain_metrics(elevation, cell_size_m=1_000, relief_radii_km=(5,))

    assert not np.isnan(metrics["relief_5km_m"][0, 0])
    assert metrics["mountain_mask"][4, 4]
    assert 0 < metrics["rugged_fraction_20km"][4, 4] <= 1


def test_access_metrics_uses_weighted_rings_and_handles_no_trails() -> None:
    mountain = np.ones((5, 5), dtype=bool)
    access = np.ones((5, 5), dtype=np.uint8)
    trails = np.zeros((5, 5), dtype=float)

    metrics = access_metrics(mountain, access, trails, cell_size_m=10_000)

    center = (2, 2)
    assert metrics["public_mountain_access_raw"][center] > 0
    assert metrics["trail_access_raw"][center] == 0
    assert np.isnan(metrics["nearest_mountain_trail_km"][center])


def test_trail_25km_is_cumulative_while_weight_uses_outer_ring() -> None:
    mountain = np.ones((9, 9), dtype=bool)
    trails = np.zeros((9, 9))
    trails[4, 4] = 1.0

    metrics = access_metrics(mountain, np.zeros((9, 9)), trails, cell_size_m=10_000)

    assert metrics["mountain_trail_km_10"][4, 4] == 1.0
    assert metrics["mountain_trail_km_25"][4, 4] == 1.0
    assert metrics["trail_access_raw"][4, 4] == 1.0


def _raw_blocks() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "block_geoid": ["010010001001001", "010010001001002", "020010001001001"],
            "tract_geoid": ["01001000100", "01001000100", "02001000100"],
            "county_fips": ["01001", "01001", "02001"],
            "state": ["AL", "AL", "AK"],
            "pop20": [10, 30, 60],
            "relief_5km_m": [100.0, 200.0, 300.0],
            "relief_10km_m": [150.0, 250.0, 350.0],
            "relief_20km_m": [200.0, 400.0, 600.0],
            "relief_40km_m": [250.0, 450.0, 650.0],
            "rugged_fraction_20km": [0.0, 0.5, 1.0],
            "public_mountain_access_raw": [0.0, 2.0, 4.0],
            "trail_access_raw": [0.0, 1.0, 3.0],
            "open_mountain_km2_5": [0.0, 1.0, 2.0],
            "open_mountain_km2_15": [0.0, 2.0, 4.0],
            "open_mountain_km2_30": [0.0, 3.0, 6.0],
            "restricted_mountain_km2_30": [0.0, 0.2, 0.4],
            "closed_mountain_km2_30": [0.0, 0.1, 0.2],
            "unknown_mountain_km2_30": [0.0, 0.3, 0.6],
            "nearest_mountain_trail_km": [None, 3.0, 1.0],
            "mountain_trail_km_10": [0.0, 1.0, 2.0],
            "mountain_trail_km_25": [0.0, 2.0, 5.0],
        }
    )


def _national_blocks(raw: pl.DataFrame | None = None) -> pl.DataFrame:
    raw = (_raw_blocks() if raw is None else raw).sort("block_geoid")
    present = set(raw["state"])
    additions = []
    for fips, state in STATE_BY_FIPS.items():
        if state not in IN_SCOPE_STATES or state in present:
            continue
        block_geoid = f"{fips}0010001001001"
        additions.append(
            raw.head(1).with_columns(
                pl.lit(block_geoid).alias("block_geoid"),
                pl.lit(block_geoid[:11]).alias("tract_geoid"),
                pl.lit(block_geoid[:5]).alias("county_fips"),
                pl.lit(state).alias("state"),
                pl.lit(1, dtype=pl.Int64).alias("pop20"),
            )
        )
    return pl.concat([raw, *additions])


def _national_expectations(blocks: pl.DataFrame) -> dict[str, object]:
    return {
        row["state"]: {"blocks": row["blocks"], "population": row["population"]}
        for row in blocks.group_by("state")
        .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
        .iter_rows(named=True)
    }


def _source_provenance() -> dict[str, object]:
    return {
        "source_lock_schema_version": 2,
        "source_lock_sha256": "1" * 64,
        "items": [
            {
                "name": "fixture",
                "acquired_at": "2026-01-01T00:00:00Z",
                "crs": "EPSG:5070",
                "schema": ["fixture"],
                "count": 1,
                "filename": "fixture.parquet",
                "size": 1,
                "sha256": "0" * 64,
            }
        ],
    }


def _write_test_release(
    blocks: pl.DataFrame,
    destination: Path,
    *,
    data_release: str = "fixture",
) -> Path:
    national = _national_blocks(blocks)
    return write_release(
        national,
        destination,
        data_release=data_release,
        sources=_source_provenance(),
        national_expectations=_national_expectations(national),
    )


def _reviewed_source_lock(blocks: pl.DataFrame) -> dict[str, object]:
    return {
        "schema_version": 2,
        "expected_states": _national_expectations(blocks),
        "block_geoid_sha256": national_block_geoid_sha256(blocks),
        "sources": _source_provenance()["items"],
    }


def _write_legacy_v1_release(
    root: Path,
    raw_blocks: pl.DataFrame | None = None,
) -> tuple[Path, dict[str, object], Path, pl.DataFrame]:
    """Create a strict, nationally complete v1 fixture for migration-only tests."""
    raw_blocks = _national_blocks() if raw_blocks is None else raw_blocks
    blocks = score_blocks(raw_blocks)
    aggregates: dict[str, pl.DataFrame] = {}
    for name, geography in (("tracts", "tract_geoid"), ("counties", "county_fips")):
        v2_detail = aggregate_scores(blocks, geography)
        score_means = (
            blocks.filter(pl.col("mountain_score").is_not_null())
            .group_by(geography)
            .agg(
                ((pl.col("mountain_score") * pl.col("pop20")).sum() / pl.col("pop20").sum())
                .round(2)
                .alias("mountain_score")
            )
            .rename({geography: "place_id"})
        )
        aggregates[name] = (
            v2_detail.drop("mountain_magnitude", "mountain_magnitude_version")
            .join(score_means, on="place_id", how="left")
            .with_columns(pl.lit("mountain_score_v1").alias("mountain_score_version"))
            .select(
                "place_id",
                "state",
                "population_2020",
                "mountain_covered_population",
                "mountain_population_coverage",
                "mountain_coverage_status",
                *AGGREGATE_MEANS,
                "mountain_score",
                "mountain_score_version",
                "mountain_pipeline_version",
            )
        )

    root.mkdir(parents=True, exist_ok=True)
    source_lock = _reviewed_source_lock(raw_blocks)
    source_lock_path = root / "source-lock.json"
    source_lock_path.write_text(json.dumps(source_lock, indent=2, sort_keys=True) + "\n")
    sources = _source_provenance()
    sources["source_lock_sha256"] = sha256_file(source_lock_path)
    staging = root / "legacy-staging"
    staging.mkdir()
    frames = {"blocks": blocks, **aggregates}
    for name, frame in frames.items():
        frame.write_parquet(staging / f"{name}.parquet", compression="zstd", statistics=True)
    (staging / OWNERSHIP_MARKER).write_text("release-v1\n")
    manifest: dict[str, object] = {
        "schema_version": 1,
        "pipeline_version": "mountain_pipeline_v1",
        "score_version": "mountain_score_v1",
        "data_release": "fixture",
        "national_complete": True,
        "national_expectations": _national_expectations(raw_blocks),
        "block_geoid_sha256": national_block_geoid_sha256(blocks),
        "sources": sources,
        "files": {
            name: {
                "filename": f"{name}.parquet",
                "rows": frame.height,
                "sha256": sha256_file(staging / f"{name}.parquet"),
            }
            for name, frame in frames.items()
        },
    }
    manifest["release_id"] = sha256_bytes(canonical_json(manifest))[:16]
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    release = root / str(manifest["release_id"])
    staging.rename(release)
    return release, source_lock, source_lock_path, raw_blocks


def _install_active_legacy_release(
    paths: RuntimePaths, fixture_root: Path
) -> tuple[Path, dict[str, object], Path, pl.DataFrame, dict[Path, bytes]]:
    release, source_lock, source_lock_path, raw_blocks = _write_legacy_v1_release(fixture_root)
    paths.ensure()
    releases = paths.data / "mountain" / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    installed = releases / release.name
    release.rename(installed)
    full_pointer = paths.data / "mountain" / "current.json"
    compact_pointer = paths.data / "mountain" / "compact" / "current.json"
    compact_pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer_payload = {"schema_version": 1, "release_id": installed.name}
    full_pointer.write_text(json.dumps(pointer_payload, sort_keys=True) + "\n")
    compact_pointer.write_text(json.dumps(pointer_payload, sort_keys=True) + "\n")
    paths.current.write_text(
        json.dumps(
            {
                "schema_version": 8,
                "build_id": "national-0000000000000000",
                "scope": "national",
                "path": str(paths.builds / "national-0000000000000000"),
            },
            sort_keys=True,
        )
        + "\n"
    )
    before = {
        full_pointer: full_pointer.read_bytes(),
        compact_pointer: compact_pointer.read_bytes(),
        paths.current: paths.current.read_bytes(),
    }
    return installed, source_lock, source_lock_path, raw_blocks, before


def _stub_migration_snapshot_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    paths: RuntimePaths,
    source_lock: dict[str, object],
) -> None:
    monkeypatch.setattr(
        mountain_migration,
        "load_source_lock_contract",
        lambda path, *, require_v2=False: source_lock,
    )
    monkeypatch.setattr(
        "househunter.mountain_pack.ensure_storage_budget",
        lambda root, *, reserve_bytes=0: None,
    )
    monkeypatch.setattr(mountain_migration, "snapshot_artifacts_are_valid", lambda path: True)

    def build_stub(
        build_paths: RuntimePaths,
        *,
        mountain_release: tuple[Path, dict[str, object], pl.DataFrame, pl.DataFrame],
        publish: bool,
        **kwargs: object,
    ) -> Path:
        del kwargs
        assert build_paths == paths
        assert publish is False
        manifest = mountain_release[1]
        build_id = f"national-{manifest['release_id']}"
        target = paths.builds / build_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "build.json").write_text(
            json.dumps(
                {
                    "schema_version": BUILD_SCHEMA_VERSION,
                    "build_id": build_id,
                    "scope": {"kind": "national", "state": None},
                    "mountain_release_id": manifest["release_id"],
                    "mountain_magnitude_version": manifest["magnitude_version"],
                },
                sort_keys=True,
            )
            + "\n"
        )
        return target

    def publish_stub(publish_paths: RuntimePaths, target: Path) -> None:
        assert publish_paths == paths
        atomic_write_json(
            paths.current,
            {
                "schema_version": BUILD_SCHEMA_VERSION,
                "build_id": target.name,
                "scope": "national",
                "path": str(target),
            },
        )

    monkeypatch.setattr(mountain_migration, "build_snapshot", build_stub)
    monkeypatch.setattr(mountain_migration, "publish_snapshot", publish_stub)


def _assert_migration_pointers(paths: RuntimePaths, release_id: str, snapshot_id: str) -> None:
    full = json.loads((paths.data / "mountain" / "current.json").read_text())
    compact = json.loads((paths.data / "mountain" / "compact" / "current.json").read_text())
    snapshot = json.loads(paths.current.read_text())
    assert full == {"schema_version": 1, "release_id": release_id}
    assert compact == {"schema_version": 1, "release_id": release_id}
    assert snapshot["schema_version"] == BUILD_SCHEMA_VERSION
    assert snapshot["build_id"] == snapshot_id


def test_magnitude_values_uses_inclusive_equal_or_higher_tail() -> None:
    values = [10.0, 20.0, 20.0, 30.0, None]

    assert magnitude_values(values) == [0.0, 0.1249, 0.1249, 0.6021, None]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([10.0], [0.0]),
        ([10.0, 10.0, 10.0], [0.0, 0.0, 0.0]),
        ([10.0, 20.0, 20.0], [0.0, 0.1761, 0.1761]),
        ([None, None], [None, None]),
    ],
)
def test_magnitude_values_handles_singletons_ties_and_empty_cohorts(
    values: list[float | None], expected: list[float | None]
) -> None:
    assert magnitude_values(values) == expected


def test_magnitude_values_is_uncapped_at_one_hundred_thousand_peers() -> None:
    values = [float(value) for value in range(100_000)]

    magnitudes = magnitude_values(values)

    assert magnitudes[0] == 0.0
    assert magnitudes[-1] == 5.0
    assert max(value for value in magnitudes if value is not None) == 5.0


def test_magnitude_values_is_row_order_independent() -> None:
    original = [("a", 10.0), ("b", 20.0), ("c", 20.0), ("d", 30.0), ("e", None)]
    shuffled = [original[index] for index in (3, 1, 4, 0, 2)]

    expected = dict(
        zip(
            (key for key, _ in original),
            magnitude_values([value for _, value in original]),
            strict=True,
        )
    )
    actual = dict(
        zip(
            (key for key, _ in shuffled),
            magnitude_values([value for _, value in shuffled]),
            strict=True,
        )
    )

    assert actual == expected


def test_magnitude_values_canonicalizes_bases_to_six_half_even_decimals() -> None:
    below_precision = [1.0000001, 1.0000004, 2.0]
    distinct_at_precision = [1.000001, 1.000002, 2.0]

    assert magnitude_values(below_precision) == [0.0, 0.0, 0.4771]
    assert magnitude_values(distinct_at_precision) == [0.0, 0.1761, 0.4771]


def test_magnitude_values_normalizes_negative_zero() -> None:
    result = magnitude_values([-0.0])

    assert result == [0.0]
    assert np.signbit(result[0]) is np.False_


@pytest.mark.parametrize("value", [-0.000001, float("nan"), float("inf"), -float("inf")])
def test_magnitude_values_rejects_invalid_bases(value: float) -> None:
    with pytest.raises(HouseHunterError, match="finite|negative|nonnegative"):
        magnitude_values([0.0, value])


def test_magnitude_values_matches_independent_decimal_oracle() -> None:
    values = [0.0, 0.0000005, 0.0000015, 9.8765435, 9.8765445, None]

    assert magnitude_values(values) == _magnitude_oracle(values)


def test_weighted_lower_rank_percentiles_and_fixed_composite() -> None:
    scored = score_blocks(_raw_blocks())

    assert scored["relief_20km_pct"].to_list() == [0.0, 10.0, 40.0]
    assert scored["rugged_pct"].to_list() == [0.0, 10.0, 40.0]
    assert scored["public_mountain_access_pct"].to_list() == [0.0, 10.0, 40.0]
    assert scored["trail_access_pct"].to_list() == [0.0, 10.0, 40.0]
    assert scored["mountain_score"].to_list() == [0.0, 10.0, 40.0]


def test_zero_population_does_not_change_calibration() -> None:
    raw = _raw_blocks().vstack(
        _raw_blocks()
        .head(1)
        .with_columns(
            pl.lit("010010001001999").alias("block_geoid"),
            pl.lit(0, dtype=pl.Int64).alias("pop20"),
            pl.lit(9_999.0).alias("relief_20km_m"),
        )
    )

    scored = score_blocks(raw)

    assert (
        scored.filter(pl.col("block_geoid") == "020010001001001")["relief_20km_pct"].item() == 40.0
    )
    assert (
        scored.filter(pl.col("block_geoid") == "010010001001999")["relief_20km_pct"].item() == 100.0
    )


def test_scoring_normalizes_only_window_filter_float_noise() -> None:
    raw = _raw_blocks().with_columns(
        pl.Series("rugged_fraction_20km", [-3.8e-12, 0.5, 1.0 + 1e-10]),
        pl.lit(-1.1e-11).alias("public_mountain_access_raw"),
        pl.lit(-1.5e-12).alias("mountain_trail_km_25"),
    )
    empty = raw.head(1).with_columns(
        pl.lit("020010001001999").alias("block_geoid"),
        pl.lit("02001000100").alias("tract_geoid"),
        pl.lit("02001").alias("county_fips"),
        pl.lit("AK").alias("state"),
        pl.lit(0, dtype=pl.Int64).alias("pop20"),
        pl.lit(None, dtype=pl.Float64).alias("relief_20km_m"),
        pl.lit(-6.940493).alias("rugged_fraction_20km"),
    )

    scored = score_blocks(raw.vstack(empty)).sort("block_geoid")

    assert scored["rugged_fraction_20km"].to_list() == [0.0, 0.5, 1.0, None]
    assert scored["public_mountain_access_raw"].to_list() == [0.0] * 4
    assert scored["mountain_trail_km_25"].to_list() == [0.0] * 4

    with pytest.raises(HouseHunterError, match="invalid rugged_fraction_20km"):
        score_blocks(raw.with_columns(pl.lit(-0.01).alias("rugged_fraction_20km")))
    with pytest.raises(HouseHunterError, match="invalid public_mountain_access_raw"):
        score_blocks(raw.with_columns(pl.lit(-0.01).alias("public_mountain_access_raw")))


def test_population_coverage_gate_is_national_and_per_state() -> None:
    raw = _raw_blocks().with_columns(
        pl.when(pl.col("state") == "AK")
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("trail_access_raw"))
        .alias("trail_access_raw")
    )

    with pytest.raises(HouseHunterError, match="AK scored-population coverage"):
        score_blocks(raw, minimum_coverage=0.39)


def test_partial_release_can_label_territory_blocks_outside_scope() -> None:
    territory = (
        _raw_blocks()
        .head(1)
        .with_columns(
            pl.lit("720010001001001").alias("block_geoid"),
            pl.lit("72001000100").alias("tract_geoid"),
            pl.lit("72001").alias("county_fips"),
            pl.lit("PR").alias("state"),
        )
    )
    scored = score_blocks(_raw_blocks().vstack(territory))
    row = scored.filter(pl.col("state") == "PR").row(0, named=True)
    aggregate = aggregate_scores(scored, "tract_geoid").filter(pl.col("state") == "PR")

    assert row["mountain_score"] is None
    assert aggregate["mountain_coverage_status"].item() == "outside_scope"
    assert aggregate["mountain_magnitude"].item() is None
    assert "mountain_score" not in aggregate.columns


def test_aggregate_scores_uses_population_and_coverage_statuses() -> None:
    scored = score_blocks(_raw_blocks())
    partial = scored.with_columns(
        pl.when(pl.col("block_geoid") == "010010001001001")
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("trail_access_pct"))
        .alias("trail_access_pct")
    )

    tracts = aggregate_scores(partial, "tract_geoid")
    alabama = tracts.filter(pl.col("place_id") == "01001000100").row(0, named=True)
    alaska = tracts.filter(pl.col("place_id") == "02001000100").row(0, named=True)

    assert alabama["mountain_population_coverage"] == 0.75
    assert alabama["mountain_coverage_status"] == "insufficient_coverage"
    assert alabama["mountain_magnitude"] is None
    assert alaska["mountain_coverage_status"] == "complete"
    assert alaska["mountain_magnitude"] == 0.0


@pytest.mark.parametrize("geography", ["tract_geoid", "county_fips"])
def test_aggregate_scores_returns_null_for_field_without_population(geography: str) -> None:
    scored = score_blocks(_raw_blocks()).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("nearest_mountain_trail_km")
    )
    aggregates = aggregate_scores(scored, geography)
    assert aggregates["nearest_mountain_trail_km"].null_count() == aggregates.height
    response = JSONResponse(aggregates.to_dicts())
    assert json.loads(response.body)[0]["nearest_mountain_trail_km"] is None


def test_aggregate_scores_uses_precise_component_base_before_old_score_rounding() -> None:
    rows = [
        {
            "tract": "01001000001",
            "county": "01001",
            "pop": 1,
            "components": (20.01, 20.0, 20.0, 20.0),
        },
        {
            "tract": "01001000002",
            "county": "01001",
            "pop": 1,
            "components": (20.0, 20.0, 20.02, 20.0),
        },
        {
            "tract": "01001000003",
            "county": "01001",
            "pop": 1,
            "components": (30.0, 30.0, 30.0, 30.0),
        },
    ]

    aggregates = aggregate_scores(_component_blocks(rows), "tract_geoid")
    observed = dict(aggregates.select("place_id", "mountain_magnitude").iter_rows())

    assert observed == {
        "01001000001": 0.1761,
        "01001000002": 0.0,
        "01001000003": 0.4771,
    }
    assert "mountain_score" not in aggregates.columns
    assert "mountain_score_version" not in aggregates.columns
    assert aggregates["mountain_magnitude_version"].unique().to_list() == ["mountain_magnitude_v2"]


def test_aggregate_scores_matches_independent_integer_decimal_oracle() -> None:
    rows = [
        {
            "tract": "01001000001",
            "pop": 7,
            "block": 1,
            "components": (11.11, 22.22, 33.33, 44.44),
        },
        {
            "tract": "01001000001",
            "pop": 13,
            "block": 2,
            "components": (55.55, 66.66, 77.77, 88.88),
        },
        {
            "tract": "01001000002",
            "pop": 19,
            "components": (22.22, 22.22, 22.22, 22.22),
        },
        {
            "tract": "01001000003",
            "pop": 23,
            "components": (99.99, 88.88, 77.77, 66.66),
        },
    ]
    bases = _base_oracle(rows)
    ordered_ids = sorted(bases)
    expected = _magnitude_oracle(
        [None if bases[place_id] is None else float(bases[place_id]) for place_id in ordered_ids]
    )

    aggregates = aggregate_scores(_component_blocks(rows), "tract_geoid")

    assert aggregates["place_id"].to_list() == ordered_ids
    assert aggregates["mountain_magnitude"].to_list() == expected


def test_aggregate_scores_calibrates_tracts_and_counties_separately() -> None:
    rows = [
        {
            "tract": f"0100100000{index}",
            "county": "01001" if index < 3 else "01003",
            "pop": 1,
            "components": (score,) * 4,
        }
        for index, score in enumerate((10.0, 20.0, 30.0, 40.0), start=1)
    ]
    blocks = _component_blocks(rows)

    tracts = aggregate_scores(blocks, "tract_geoid")
    counties = aggregate_scores(blocks, "county_fips")

    assert tracts["mountain_magnitude"].to_list() == [0.0, 0.1249, 0.301, 0.6021]
    assert counties["mountain_magnitude"].to_list() == [0.0, 0.301]


def test_aggregate_scores_counts_geographies_not_population_as_peers() -> None:
    rows = [
        {
            "tract": "01001000001",
            "pop": 1_000_000_000,
            "components": (10.0,) * 4,
        },
        {"tract": "01001000002", "pop": 1, "components": (20.0,) * 4},
        {"tract": "01001000003", "pop": 1, "components": (30.0,) * 4},
    ]

    aggregates = aggregate_scores(_component_blocks(rows), "tract_geoid")

    assert aggregates["mountain_magnitude"].to_list() == [0.0, 0.1761, 0.4771]


def test_aggregate_scores_includes_partial_but_excludes_ineligible_geographies() -> None:
    rows = [
        {"tract": "01001000001", "pop": 100, "components": (10.0,) * 4},
        {
            "tract": "01001000002",
            "pop": 95,
            "block": 1,
            "components": (20.0,) * 4,
        },
        {"tract": "01001000002", "pop": 5, "block": 2, "components": None},
        {
            "tract": "01001000003",
            "pop": 89,
            "block": 1,
            "components": (30.0,) * 4,
        },
        {"tract": "01001000003", "pop": 11, "block": 2, "components": None},
        {"tract": "01001000004", "pop": 0, "components": (40.0,) * 4},
        {
            "tract": "72001000001",
            "county": "72001",
            "state": "PR",
            "pop": 100,
            "components": (50.0,) * 4,
        },
    ]

    aggregates = aggregate_scores(_component_blocks(rows), "tract_geoid")
    observed = {row["place_id"]: row for row in aggregates.iter_rows(named=True)}

    assert observed["01001000001"]["mountain_magnitude"] == 0.0
    assert observed["01001000002"]["mountain_magnitude"] == 0.301
    assert observed["01001000002"]["mountain_coverage_status"] == "partial"
    assert observed["01001000003"]["mountain_magnitude"] is None
    assert observed["01001000003"]["mountain_coverage_status"] == "insufficient_coverage"
    assert observed["01001000004"]["mountain_magnitude"] is None
    assert observed["01001000004"]["mountain_coverage_status"] == "zero_population"
    assert observed["72001000001"]["mountain_magnitude"] is None
    assert observed["72001000001"]["mountain_coverage_status"] == "outside_scope"


def test_aggregate_scores_ties_differences_beyond_six_decimal_base_precision() -> None:
    rows = [
        {
            "tract": "01001000001",
            "pop": 99_999,
            "block": 1,
            "components": (20.0,) * 4,
        },
        {
            "tract": "01001000001",
            "pop": 1,
            "block": 2,
            "components": (20.01,) * 4,
        },
        {
            "tract": "01001000002",
            "pop": 99_998,
            "block": 1,
            "components": (20.0,) * 4,
        },
        {
            "tract": "01001000002",
            "pop": 2,
            "block": 2,
            "components": (20.01,) * 4,
        },
        {"tract": "01001000003", "pop": 1, "components": (30.0,) * 4},
    ]

    aggregates = aggregate_scores(_component_blocks(rows), "tract_geoid")

    assert aggregates["mountain_magnitude"].to_list() == [0.0, 0.0, 0.4771]


def test_aggregate_scores_separates_bases_at_six_decimal_precision() -> None:
    rows = [
        {
            "tract": "01001000001",
            "pop": 99_990,
            "block": 1,
            "components": (20.0,) * 4,
        },
        {
            "tract": "01001000001",
            "pop": 10,
            "block": 2,
            "components": (20.01,) * 4,
        },
        {
            "tract": "01001000002",
            "pop": 99_980,
            "block": 1,
            "components": (20.0,) * 4,
        },
        {
            "tract": "01001000002",
            "pop": 20,
            "block": 2,
            "components": (20.01,) * 4,
        },
        {"tract": "01001000003", "pop": 1, "components": (30.0,) * 4},
    ]

    aggregates = aggregate_scores(_component_blocks(rows), "tract_geoid")

    assert aggregates["mountain_magnitude"].to_list() == [0.0, 0.1761, 0.4771]


def test_aggregate_scores_rounds_halfway_base_micros_half_even() -> None:
    rows: list[dict[str, object]] = []
    for index, high_population in enumerate((5, 10, 15, 20), start=1):
        tract = f"0100100000{index}"
        rows.extend(
            [
                {
                    "tract": tract,
                    "pop": 100_000 - high_population,
                    "block": 1,
                    "components": (20.0,) * 4,
                },
                {
                    "tract": tract,
                    "pop": high_population,
                    "block": 2,
                    "components": (20.01,) * 4,
                },
            ]
        )
    rows.append({"tract": "01001000005", "pop": 1, "components": (30.0,) * 4})

    aggregates = aggregate_scores(_component_blocks(rows), "tract_geoid")

    assert aggregates["mountain_magnitude"].to_list() == [
        0.0,
        0.0969,
        0.2218,
        0.2218,
        0.699,
    ]


def test_aggregate_scores_guards_int64_weighted_accumulation_bounds() -> None:
    # Max base units (1,000,000) are scaled by 100 before six-place rounding.
    maximum_safe_population = (2**63 - 1) // 100_000_000
    safe = [
        {
            "tract": "01001000001",
            "pop": maximum_safe_population,
            "components": (100.0,) * 4,
        },
        {"tract": "01001000002", "pop": 1, "components": (0.0,) * 4},
    ]
    overflow = [
        {
            "tract": "01001000001",
            "pop": maximum_safe_population + 1,
            "components": (100.0,) * 4,
        },
        {"tract": "01001000002", "pop": 1, "components": (0.0,) * 4},
    ]

    assert aggregate_scores(_component_blocks(safe), "tract_geoid").height == 2
    with pytest.raises(HouseHunterError, match="Int64|overflow|bounds"):
        aggregate_scores(_component_blocks(overflow), "tract_geoid")


def test_aggregate_scores_rejects_population_sum_overflow() -> None:
    rows = [
        {
            "tract": "01001000001",
            "pop": 2**62,
            "block": 1,
            "components": (0.0,) * 4,
        },
        {
            "tract": "01001000001",
            "pop": 2**62,
            "block": 2,
            "components": (0.0,) * 4,
        },
    ]

    with pytest.raises(HouseHunterError, match="Int64|overflow|bounds"):
        aggregate_scores(_component_blocks(rows), "tract_geoid")


def test_aggregate_scores_rejects_missing_population() -> None:
    blocks = _component_blocks(
        [{"tract": "01001000001", "pop": 1, "components": (10.0,) * 4}]
    ).with_columns(pl.lit(None, dtype=pl.Int64).alias("pop20"))

    with pytest.raises(HouseHunterError, match="population"):
        aggregate_scores(blocks, "tract_geoid")


def test_aggregate_scores_rejects_nonfinite_weighted_detail_overflow() -> None:
    blocks = score_blocks(
        _raw_blocks().with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit(1e308))
            .otherwise(pl.col("relief_5km_m"))
            .alias("relief_5km_m")
        )
    )

    with pytest.raises(HouseHunterError, match="relief_5km_m.*valid range"):
        aggregate_scores(blocks, "tract_geoid")


def test_release_is_deterministic_validated_and_compact(tmp_path) -> None:
    first = _write_test_release(_raw_blocks(), tmp_path / "one")
    second = _write_test_release(_raw_blocks().reverse(), tmp_path / "two")

    one = validate_release(first)
    two = validate_release(second)
    assert one["release_id"] == two["release_id"]
    assert one["compact_bytes"] < 50 * 1024 * 1024
    assert one["files"]["tracts"]["sha256"] == two["files"]["tracts"]["sha256"]


def test_release_schema_two_binds_complete_magnitude_contract(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")

    manifest = validate_release(release)
    tracts = pl.read_parquet(release / manifest["files"]["tracts"]["filename"])
    counties = pl.read_parquet(release / manifest["files"]["counties"]["filename"])
    blocks = pl.read_parquet(release / manifest["files"]["blocks"]["filename"])

    assert manifest["schema_version"] == 2
    assert manifest["pipeline_version"] == "mountain_pipeline_v1"
    assert manifest["base_score_version"] == "mountain_score_v1"
    assert manifest["magnitude_version"] == "mountain_magnitude_v2"
    assert manifest["magnitude_contract"] == {
        "formula": "log10(N / count(peer_base >= geography_base))",
        "component_percentile_decimals": 2,
        "integer_weights": {
            "relief_20km_pct": 45,
            "rugged_pct": 20,
            "public_mountain_access_pct": 20,
            "trail_access_pct": 15,
        },
        "population_weighting": "exact_integer_sums",
        "base_decimals": 6,
        "base_rounding": "half_even",
        "magnitude_decimals": 4,
        "magnitude_rounding": "half_even",
        "tie_rule": "inclusive_equal_or_higher",
        "peer_scope": "national_50_states_dc_same_grain",
        "peer_geography_weight": "one",
        "eligible_statuses": ["complete", "partial"],
        "uncapped": True,
        "peer_counts": {"tract": 51, "county": 51},
    }
    for aggregate in (tracts, counties):
        assert "mountain_magnitude" in aggregate.columns
        assert "mountain_magnitude_version" in aggregate.columns
        assert "mountain_score" not in aggregate.columns
        assert "mountain_score_version" not in aggregate.columns
    assert {"mountain_score", "mountain_score_version"} <= set(blocks.columns)


def test_normal_release_validation_rejects_schema_one(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")

    _rewrite_manifest_identity(release, lambda manifest: manifest.update(schema_version=1))

    with pytest.raises(HouseHunterError, match="schema 2|version is incompatible"):
        validate_release(release)


def test_compact_runtime_rejects_schema_one_with_rescore_instruction(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")

    def make_legacy(manifest: dict[str, object]) -> None:
        manifest["schema_version"] = 1
        manifest["score_version"] = manifest.pop("base_score_version")
        manifest.pop("magnitude_version")
        manifest.pop("magnitude_contract")

    _rewrite_manifest_identity(release, make_legacy)

    with pytest.raises(HouseHunterError, match=r"rescore-v1"):
        load_compact_release(release)


def test_release_writer_and_validator_reject_partial_schema_two_release(tmp_path) -> None:
    with pytest.raises(HouseHunterError, match="nationally complete"):
        write_release(_raw_blocks(), tmp_path / "partial", data_release="fixture", sources={})

    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")

    def make_partial(manifest: dict[str, object]) -> None:
        manifest["national_complete"] = False
        manifest["national_expectations"] = None
        manifest["block_geoid_sha256"] = None

    _rewrite_manifest_identity(release, make_partial)

    with pytest.raises(HouseHunterError, match="incompatible"):
        validate_release(release)


def test_release_rejects_tampered_magnitude_after_hash_and_identity_rewrite(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")
    manifest = json.loads((release / "manifest.json").read_text())
    tracts_path = release / manifest["files"]["tracts"]["filename"]
    tracts = pl.read_parquet(tracts_path).with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then((pl.col("mountain_magnitude") + 0.1).round(4))
        .otherwise(pl.col("mountain_magnitude"))
        .alias("mountain_magnitude")
    )
    tracts.write_parquet(tracts_path, compression="zstd", statistics=True)

    def update_hash(payload: dict[str, object]) -> None:
        payload["files"]["tracts"]["sha256"] = sha256_file(tracts_path)

    _rewrite_manifest_identity(release, update_hash)

    with pytest.raises(HouseHunterError, match="reconstructed|magnitudes differ"):
        validate_release(release)


def test_release_rejects_null_geography_even_when_aggregates_and_identity_are_rewritten(
    tmp_path,
) -> None:
    zero_population = (
        _raw_blocks()
        .head(1)
        .with_columns(
            pl.lit("010010099001001").alias("block_geoid"),
            pl.lit("01001009900").alias("tract_geoid"),
            pl.lit(0, dtype=pl.Int64).alias("pop20"),
        )
    )
    release = _write_test_release(
        pl.concat([_raw_blocks(), zero_population]), tmp_path / "candidate"
    )
    manifest = json.loads((release / "manifest.json").read_text())
    blocks_path = release / manifest["files"]["blocks"]["filename"]
    tracts_path = release / manifest["files"]["tracts"]["filename"]
    blocks = pl.read_parquet(blocks_path).with_columns(
        pl.when(pl.col("block_geoid") == "010010099001001")
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("tract_geoid"))
        .alias("tract_geoid")
    )
    tracts = pl.read_parquet(tracts_path).with_columns(
        pl.when(pl.col("place_id") == "01001009900")
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("place_id"))
        .alias("place_id")
    )
    blocks.write_parquet(blocks_path, compression="zstd", statistics=True)
    tracts.write_parquet(tracts_path, compression="zstd", statistics=True)

    def update_hashes(payload: dict[str, object]) -> None:
        payload["files"]["blocks"]["sha256"] = sha256_file(blocks_path)
        payload["files"]["tracts"]["sha256"] = sha256_file(tracts_path)

    _rewrite_manifest_identity(release, update_hashes)

    with pytest.raises(HouseHunterError, match="null|required values|identifiers"):
        validate_release(release)


@pytest.mark.parametrize("value", [0.00001, float("nan"), float("inf")])
def test_release_rejects_noncanonical_or_nonfinite_magnitude(tmp_path, value: float) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")
    manifest = json.loads((release / "manifest.json").read_text())
    tracts_path = release / manifest["files"]["tracts"]["filename"]
    tracts = pl.read_parquet(tracts_path).with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit(value))
        .otherwise(pl.col("mountain_magnitude"))
        .alias("mountain_magnitude")
    )
    tracts.write_parquet(tracts_path, compression="zstd", statistics=True)

    def update_hash(payload: dict[str, object]) -> None:
        payload["files"]["tracts"]["sha256"] = sha256_file(tracts_path)

    _rewrite_manifest_identity(release, update_hash)

    with pytest.raises(HouseHunterError, match="finite|four decimals|canonical|reconstructed"):
        validate_release(release)


def test_release_rejects_persisted_negative_zero(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")
    manifest = json.loads((release / "manifest.json").read_text())
    tracts_path = release / manifest["files"]["tracts"]["filename"]
    tracts = pl.read_parquet(tracts_path).with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.lit(-0.0))
        .otherwise(pl.col("mountain_magnitude"))
        .alias("mountain_magnitude")
    )
    tracts.write_parquet(tracts_path, compression="zstd", statistics=True)

    def update_hash(payload: dict[str, object]) -> None:
        payload["files"]["tracts"]["sha256"] = sha256_file(tracts_path)

    _rewrite_manifest_identity(release, update_hash)

    with pytest.raises(HouseHunterError, match="negative zero"):
        validate_release(release)


@pytest.mark.parametrize(
    ("contract_key", "replacement"),
    [
        ("base_decimals", 5),
        ("magnitude_decimals", 3),
        ("tie_rule", "strictly_higher"),
        ("peer_scope", "state_same_grain"),
        ("peer_geography_weight", "population"),
    ],
)
def test_release_rejects_rewritten_magnitude_contract(
    tmp_path, contract_key: str, replacement: object
) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")

    def mutate(manifest: dict[str, object]) -> None:
        manifest["magnitude_contract"][contract_key] = replacement

    _rewrite_manifest_identity(release, mutate)

    with pytest.raises(HouseHunterError, match="magnitude contract|version is incompatible"):
        validate_release(release)


def test_release_rejects_rewritten_peer_counts(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")

    def mutate(manifest: dict[str, object]) -> None:
        manifest["magnitude_contract"]["peer_counts"]["tract"] += 1

    _rewrite_manifest_identity(release, mutate)

    with pytest.raises(HouseHunterError, match="peer count|magnitude contract"):
        validate_release(release)


def test_legacy_reader_fully_validates_v1_and_returns_raw_columns_only(tmp_path) -> None:
    release, source_lock, source_lock_path, raw_blocks = _write_legacy_v1_release(
        tmp_path / "fixture"
    )

    manifest, raw = validate_legacy_v1_release(
        release,
        reviewed_source_lock=source_lock,
        reviewed_source_lock_sha256=sha256_file(source_lock_path),
    )

    assert manifest["schema_version"] == 1
    assert raw.equals(raw_blocks.select(raw.columns).sort("block_geoid"))
    assert "mountain_score" not in raw.columns
    assert "mountain_score_version" not in raw.columns
    assert "mountain_magnitude" not in raw.columns


def test_legacy_reader_rejects_recomputed_identity_after_block_score_tamper(tmp_path) -> None:
    release, source_lock, source_lock_path, _ = _write_legacy_v1_release(tmp_path / "fixture")
    manifest = json.loads((release / "manifest.json").read_text())
    blocks_path = release / manifest["files"]["blocks"]["filename"]
    blocks = pl.read_parquet(blocks_path).with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("mountain_score") + 1.0)
        .otherwise(pl.col("mountain_score"))
        .alias("mountain_score")
    )
    blocks.write_parquet(blocks_path, compression="zstd", statistics=True)

    def update_hash(payload: dict[str, object]) -> None:
        payload["files"]["blocks"]["sha256"] = sha256_file(blocks_path)

    _rewrite_manifest_identity(release, update_hash)

    with pytest.raises(HouseHunterError, match="scores do not match raw"):
        validate_legacy_v1_release(
            release,
            reviewed_source_lock=source_lock,
            reviewed_source_lock_sha256=sha256_file(source_lock_path),
        )


def test_legacy_reader_rejects_recomputed_identity_after_aggregate_tamper(tmp_path) -> None:
    release, source_lock, source_lock_path, _ = _write_legacy_v1_release(tmp_path / "fixture")
    manifest = json.loads((release / "manifest.json").read_text())
    tracts_path = release / manifest["files"]["tracts"]["filename"]
    tracts = pl.read_parquet(tracts_path).with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("mountain_score") + 1.0)
        .otherwise(pl.col("mountain_score"))
        .alias("mountain_score")
    )
    tracts.write_parquet(tracts_path, compression="zstd", statistics=True)

    def update_hash(payload: dict[str, object]) -> None:
        payload["files"]["tracts"]["sha256"] = sha256_file(tracts_path)

    _rewrite_manifest_identity(release, update_hash)

    with pytest.raises(HouseHunterError, match="tracts do not match block aggregation"):
        validate_legacy_v1_release(
            release,
            reviewed_source_lock=source_lock,
            reviewed_source_lock_sha256=sha256_file(source_lock_path),
        )


def test_legacy_reader_rejects_null_geography_with_rewritten_aggregate_and_identity(
    tmp_path,
) -> None:
    zero_population = (
        _raw_blocks()
        .head(1)
        .with_columns(
            pl.lit("010010099001001").alias("block_geoid"),
            pl.lit("01001009900").alias("tract_geoid"),
            pl.lit(0, dtype=pl.Int64).alias("pop20"),
        )
    )
    release, source_lock, source_lock_path, _ = _write_legacy_v1_release(
        tmp_path / "fixture", pl.concat([_national_blocks(), zero_population])
    )
    manifest = json.loads((release / "manifest.json").read_text())
    blocks_path = release / manifest["files"]["blocks"]["filename"]
    tracts_path = release / manifest["files"]["tracts"]["filename"]
    blocks = pl.read_parquet(blocks_path).with_columns(
        pl.when(pl.col("block_geoid") == "010010099001001")
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("tract_geoid"))
        .alias("tract_geoid")
    )
    tracts = pl.read_parquet(tracts_path).with_columns(
        pl.when(pl.col("place_id") == "01001009900")
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.col("place_id"))
        .alias("place_id")
    )
    blocks.write_parquet(blocks_path, compression="zstd", statistics=True)
    tracts.write_parquet(tracts_path, compression="zstd", statistics=True)

    def update_hashes(payload: dict[str, object]) -> None:
        payload["files"]["blocks"]["sha256"] = sha256_file(blocks_path)
        payload["files"]["tracts"]["sha256"] = sha256_file(tracts_path)

    _rewrite_manifest_identity(release, update_hashes)

    with pytest.raises(HouseHunterError, match="cannot be null|null required values"):
        validate_legacy_v1_release(
            release,
            reviewed_source_lock=source_lock,
            reviewed_source_lock_sha256=sha256_file(source_lock_path),
        )


def test_legacy_reader_rejects_source_lock_drift(tmp_path) -> None:
    release, source_lock, _, _ = _write_legacy_v1_release(tmp_path / "fixture")

    with pytest.raises(HouseHunterError, match="reviewed source lock"):
        validate_legacy_v1_release(
            release,
            reviewed_source_lock=source_lock,
            reviewed_source_lock_sha256="f" * 64,
        )


@pytest.mark.parametrize("marker", [None, "forged\n"])
def test_legacy_reader_rejects_missing_or_forged_ownership(tmp_path, marker: str | None) -> None:
    release, source_lock, source_lock_path, _ = _write_legacy_v1_release(tmp_path / "fixture")
    marker_path = release / OWNERSHIP_MARKER
    if marker is None:
        marker_path.unlink()
    else:
        marker_path.write_text(marker)

    with pytest.raises(HouseHunterError, match="owned non-symlinked"):
        validate_legacy_v1_release(
            release,
            reviewed_source_lock=source_lock,
            reviewed_source_lock_sha256=sha256_file(source_lock_path),
        )


def test_legacy_reader_rejects_symlinked_release(tmp_path) -> None:
    release, source_lock, source_lock_path, _ = _write_legacy_v1_release(tmp_path / "fixture")
    symlink = tmp_path / "legacy-link"
    symlink.symlink_to(release, target_is_directory=True)

    with pytest.raises(HouseHunterError, match="owned non-symlinked"):
        validate_legacy_v1_release(
            symlink,
            reviewed_source_lock=source_lock,
            reviewed_source_lock_sha256=sha256_file(source_lock_path),
        )


def test_rescore_v1_matches_fresh_v2_and_commits_one_recoverable_identity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    legacy, source_lock, source_lock_path, raw_blocks, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    legacy_files_before = {
        path.name: sha256_file(path) for path in legacy.iterdir() if path.is_file()
    }
    journal_writes: list[dict[str, object]] = []
    real_atomic_write = mountain_migration.atomic_write_json

    def recording_atomic_write(path: Path, payload: object) -> None:
        if path.name == "migration-v2.json":
            assert isinstance(payload, dict)
            journal_writes.append(dict(payload))
        real_atomic_write(path, payload)

    monkeypatch.setattr(mountain_migration, "atomic_write_json", recording_atomic_write)

    progress: list[tuple[int, str]] = []
    report = mountain_migration.rescore_v1_release(
        paths, source_lock_path, progress=lambda value, message: progress.append((value, message))
    )

    assert report["legacy_release_id"] == legacy.name
    assert report["release_id"] == report["compact_release_id"]
    _assert_migration_pointers(paths, str(report["release_id"]), str(report["snapshot_id"]))
    assert [payload["phase"] for payload in journal_writes] == [
        "staged",
        "full_published",
        "compact_published",
        "snapshot_published",
    ]
    assert all(
        set(payload) == {"phase", "release_id", "compact_release_id", "snapshot_id"}
        for payload in journal_writes
    )
    assert not (paths.data / "mountain" / "migration-v2.json").exists()
    assert (70, f"Staging schema-{BUILD_SCHEMA_VERSION} HouseHunter snapshot") in progress
    assert Path(str(report["lineage_report"])).is_file()
    assert legacy.is_dir()
    assert {
        path.name: sha256_file(path) for path in legacy.iterdir() if path.is_file()
    } == legacy_files_before

    migrated = paths.data / "mountain" / "releases" / str(report["release_id"])
    migrated_manifest = validate_release(migrated)
    fresh_sources = _source_provenance()
    fresh_sources["source_lock_sha256"] = sha256_file(source_lock_path)
    fresh = write_release(
        raw_blocks,
        tmp_path / "fresh-v2",
        data_release="fixture",
        sources=fresh_sources,
        national_expectations=_national_expectations(raw_blocks),
    )
    fresh_manifest = validate_release(fresh)
    assert migrated_manifest["release_id"] == fresh_manifest["release_id"]
    assert migrated_manifest["files"] == fresh_manifest["files"]
    assert "migration" not in migrated_manifest
    assert "lineage" not in migrated_manifest


@pytest.mark.parametrize(
    "failure_boundary",
    ["full_stage", "compact_stage", "snapshot_stage", "journal_stage"],
)
def test_rescore_v1_precommit_failures_preserve_every_pointer_byte(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure_boundary: str
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, before = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"injected {failure_boundary} failure")

    if failure_boundary == "full_stage":
        monkeypatch.setattr(mountain_migration, "write_and_stage_release", fail)
    elif failure_boundary == "compact_stage":
        monkeypatch.setattr(mountain_migration, "stage_compact_fallback", fail)
    elif failure_boundary == "snapshot_stage":
        monkeypatch.setattr(mountain_migration, "build_snapshot", fail)
    else:
        real_atomic_write = mountain_migration.atomic_write_json

        def fail_journal(path: Path, payload: object) -> None:
            if path.name == "migration-v2.json":
                raise RuntimeError("injected journal_stage failure")
            real_atomic_write(path, payload)

        monkeypatch.setattr(mountain_migration, "atomic_write_json", fail_journal)

    with pytest.raises(RuntimeError, match=failure_boundary):
        mountain_migration.rescore_v1_release(paths, source_lock_path)

    assert {pointer: pointer.read_bytes() for pointer in before} == before
    assert not (paths.data / "mountain" / "migration-v2.json").exists()


@pytest.mark.parametrize(
    "failure_boundary",
    [
        "full_publish",
        "full_journal",
        "compact_publish",
        "compact_journal",
        "snapshot_publish",
        "snapshot_journal",
        "lineage_report",
    ],
)
def test_rescore_v1_interrupted_commit_forward_recovers_without_rereading_v1(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure_boundary: str
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    real_publish_full = mountain_migration.publish_release_pointer
    real_publish_compact = mountain_migration.publish_compact_pointer
    real_publish_snapshot = mountain_migration.publish_snapshot
    real_atomic_write = mountain_migration.atomic_write_json
    journal_write_count = 0

    def fail_full(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected full_publish failure")

    def fail_compact(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected compact_publish failure")

    def fail_snapshot(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected snapshot_publish failure")

    def maybe_fail_atomic(path: Path, payload: object) -> None:
        nonlocal journal_write_count
        if path.name == "migration-v2.json":
            journal_write_count += 1
            failure_at = {
                "full_journal": 2,
                "compact_journal": 3,
                "snapshot_journal": 4,
            }.get(failure_boundary)
            if journal_write_count == failure_at:
                raise RuntimeError(f"injected {failure_boundary} failure")
        if failure_boundary == "lineage_report" and path.parent.name == "migrations":
            raise RuntimeError("injected lineage_report failure")
        real_atomic_write(path, payload)

    if failure_boundary == "full_publish":
        monkeypatch.setattr(mountain_migration, "publish_release_pointer", fail_full)
    elif failure_boundary == "compact_publish":
        monkeypatch.setattr(mountain_migration, "publish_compact_pointer", fail_compact)
    elif failure_boundary == "snapshot_publish":
        monkeypatch.setattr(mountain_migration, "publish_snapshot", fail_snapshot)
    else:
        monkeypatch.setattr(mountain_migration, "atomic_write_json", maybe_fail_atomic)

    with pytest.raises(RuntimeError, match=failure_boundary):
        mountain_migration.rescore_v1_release(paths, source_lock_path)

    journal_path = paths.data / "mountain" / "migration-v2.json"
    assert journal_path.is_file()
    journal = json.loads(journal_path.read_text())
    assert set(journal) == {
        "phase",
        "release_id",
        "compact_release_id",
        "snapshot_id",
    }

    monkeypatch.setattr(mountain_migration, "publish_release_pointer", real_publish_full)
    monkeypatch.setattr(mountain_migration, "publish_compact_pointer", real_publish_compact)
    monkeypatch.setattr(mountain_migration, "publish_snapshot", real_publish_snapshot)
    monkeypatch.setattr(mountain_migration, "atomic_write_json", real_atomic_write)
    monkeypatch.setattr(
        mountain_migration,
        "validate_legacy_v1_release",
        lambda *args, **kwargs: pytest.fail("recovery reread the v1 release"),
    )

    report = mountain_migration.rescore_v1_release(paths, source_lock_path)

    _assert_migration_pointers(paths, str(report["release_id"]), str(report["snapshot_id"]))
    assert not journal_path.exists()


def test_rescore_recovery_republishes_snapshot_when_pointer_id_hides_wrong_target(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    real_publish_snapshot = mountain_migration.publish_snapshot

    def fail_snapshot(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("interrupt snapshot")

    monkeypatch.setattr(mountain_migration, "publish_snapshot", fail_snapshot)

    with pytest.raises(RuntimeError, match="interrupt snapshot"):
        mountain_migration.rescore_v1_release(paths, source_lock_path)

    journal = json.loads((paths.data / "mountain" / "migration-v2.json").read_text())
    atomic_write_json(
        paths.current,
        {
            "schema_version": BUILD_SCHEMA_VERSION,
            "build_id": journal["snapshot_id"],
            "scope": "wrong",
            "path": str(paths.builds / "wrong-target"),
        },
    )
    monkeypatch.setattr(mountain_migration, "publish_snapshot", real_publish_snapshot)

    mountain_migration.rescore_v1_release(paths, source_lock_path)

    assert json.loads(paths.current.read_text()) == {
        "schema_version": BUILD_SCHEMA_VERSION,
        "build_id": journal["snapshot_id"],
        "scope": "national",
        "path": str(paths.builds / journal["snapshot_id"]),
    }


def test_rescore_v1_rejects_compact_only_installation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    (paths.data / "mountain" / "current.json").unlink()

    with pytest.raises(HouseHunterError, match="pointer is missing"):
        mountain_migration.rescore_v1_release(paths, source_lock_path)


def test_rescore_v1_rejects_mismatched_full_and_compact_pointers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    compact_pointer = paths.data / "mountain" / "compact" / "current.json"
    compact_pointer.write_text(json.dumps({"schema_version": 1, "release_id": "0" * 16}) + "\n")

    with pytest.raises(HouseHunterError, match="full and compact pointers disagree"):
        mountain_migration.rescore_v1_release(paths, source_lock_path)


def test_rescore_v1_rejects_extra_or_forged_journal_fields(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    journal = paths.data / "mountain" / "migration-v2.json"
    journal.write_text(
        json.dumps(
            {
                "phase": "staged",
                "release_id": "0" * 16,
                "compact_release_id": "0" * 16,
                "snapshot_id": "national-0000000000000000",
                "path": str(tmp_path / "forged"),
            }
        )
        + "\n"
    )

    with pytest.raises(HouseHunterError, match="journal has an invalid contract"):
        mountain_migration.rescore_v1_release(paths, source_lock_path)


def test_rescore_v1_rejects_symlinked_source_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    symlink = tmp_path / "source-lock-link.json"
    symlink.symlink_to(source_lock_path)

    with pytest.raises(HouseHunterError, match="source lock must be a regular file"):
        mountain_migration.rescore_v1_release(paths, symlink)


def test_rescore_v1_requires_pointer_identity_to_match_validated_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    legacy, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    forged_id = "0" * 16
    forged_path = legacy.with_name(forged_id)
    legacy.rename(forged_path)
    forged_pointer = json.dumps({"schema_version": 1, "release_id": forged_id}) + "\n"
    (paths.data / "mountain" / "current.json").write_text(forged_pointer)
    (paths.data / "mountain" / "compact" / "current.json").write_text(forged_pointer)

    with pytest.raises(HouseHunterError, match="pointer.*identity|identity.*pointer"):
        mountain_migration.rescore_v1_release(paths, source_lock_path)


def test_release_rejects_forged_identity(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["release_id"] = "../../outside"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HouseHunterError, match="identity"):
        validate_release(release)


def test_release_recomputes_block_percentiles_instead_of_trusting_scores(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")
    blocks_path = release / "blocks.parquet"
    blocks = pl.read_parquet(blocks_path).with_columns(
        pl.when(pl.col("block_geoid") == "010010001001001")
        .then(50.0)
        .otherwise(pl.col("mountain_score"))
        .alias("mountain_score")
    )
    blocks.write_parquet(blocks_path, compression="zstd", statistics=True)
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["blocks"]["sha256"] = sha256_file(blocks_path)
    identity = {key: item for key, item in manifest.items() if key != "release_id"}
    manifest["release_id"] = sha256_bytes(canonical_json(identity))[:16]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    with pytest.raises(HouseHunterError, match="do not match rounded national raw metrics"):
        validate_release(release)


def test_release_rejects_noncanonical_raw_precision(tmp_path) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / "candidate")
    blocks_path = release / "blocks.parquet"
    blocks = pl.read_parquet(blocks_path).with_columns(
        (pl.col("relief_5km_m") + 0.4).alias("relief_5km_m")
    )
    blocks.write_parquet(blocks_path, compression="zstd", statistics=True)
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["blocks"]["sha256"] = sha256_file(blocks_path)
    identity = {key: item for key, item in manifest.items() if key != "release_id"}
    manifest["release_id"] = sha256_bytes(canonical_json(identity))[:16]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    with pytest.raises(HouseHunterError, match="canonically rounded"):
        validate_release(release)


def test_national_validation_rejects_state_geoid_and_fractional_population() -> None:
    blocks = _national_blocks()
    expectations = _national_expectations(blocks)
    wrong_state = blocks.with_columns(
        pl.when(pl.col("state") == "AL")
        .then(pl.lit("AK"))
        .otherwise(pl.col("state"))
        .alias("state")
    )
    with pytest.raises(HouseHunterError, match="state does not match"):
        validate_national_expectations(wrong_state, expectations)

    fractional_population = blocks.with_columns(pl.col("pop20").cast(pl.Float64))
    with pytest.raises(HouseHunterError, match="population/state types"):
        validate_national_expectations(fractional_population, expectations)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [("relief_5km_m", -1.0, "relief_5km_m"), ("trail_access_pct", 101.0, "trail_access_pct")],
)
def test_release_rejects_out_of_range_block_values(
    tmp_path, column: str, value: float, message: str
) -> None:
    release = _write_test_release(_raw_blocks(), tmp_path / column)
    blocks_path = release / "blocks.parquet"
    blocks = pl.read_parquet(blocks_path).with_columns(
        pl.when(pl.int_range(pl.len()) == 0).then(value).otherwise(pl.col(column)).alias(column)
    )
    blocks.write_parquet(blocks_path, compression="zstd", statistics=True)
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["blocks"]["sha256"] = sha256_file(blocks_path)
    identity = {key: item for key, item in manifest.items() if key != "release_id"}
    manifest["release_id"] = sha256_bytes(canonical_json(identity))[:16]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    with pytest.raises(HouseHunterError, match=message):
        validate_release(release)


def test_promotion_is_atomic_and_content_addressed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    blocks = _national_blocks()
    candidate = write_release(
        blocks,
        tmp_path / "candidate",
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    reservations: list[int] = []
    monkeypatch.setattr(
        "househunter.mountain_pack.ensure_storage_budget",
        lambda root, *, reserve_bytes=0: reservations.append(reserve_bytes),
    )
    promoted = promote_release(
        paths,
        candidate,
        reviewed_source_lock=_reviewed_source_lock(blocks),
        reviewed_source_lock_sha256="1" * 64,
        expected_raw_blocks=blocks,
    )
    pointer = json.loads((paths.data / "mountain" / "current.json").read_text())

    assert reservations and reservations[0] > 0
    assert promoted.name == pointer["release_id"]
    assert (
        promote_release(
            paths,
            candidate,
            reviewed_source_lock=_reviewed_source_lock(blocks),
            reviewed_source_lock_sha256="1" * 64,
            expected_raw_blocks=blocks,
        )
        == promoted
    )


def test_full_and_compact_staging_leave_all_pointers_byte_identical(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    blocks = _national_blocks()
    candidate = write_release(
        blocks,
        tmp_path / "candidate",
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    manifest = validate_release(candidate)
    full_pointer = paths.data / "mountain" / "current.json"
    compact_pointer = paths.data / "mountain" / "compact" / "current.json"
    snapshot_pointer = paths.current
    before = {
        full_pointer: b'{"old":"full"}\n',
        compact_pointer: b'{"old":"compact"}\n',
        snapshot_pointer: b'{"old":"snapshot"}\n',
    }
    for pointer, contents in before.items():
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_bytes(contents)

    staged = stage_validated_release(paths, candidate, manifest, move_candidate=False)
    compact = stage_compact_fallback(paths, staged, manifest)

    assert staged.name == manifest["release_id"]
    assert compact.name == manifest["release_id"]
    assert {pointer: pointer.read_bytes() for pointer in before} == before


def test_compact_staging_copy_failure_preserves_pointers_and_cleans_temp(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    blocks = _national_blocks()
    release = write_release(
        blocks,
        tmp_path / "candidate",
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    manifest = validate_release(release)
    full_pointer = paths.data / "mountain" / "current.json"
    compact_pointer = paths.data / "mountain" / "compact" / "current.json"
    full_pointer.parent.mkdir(parents=True, exist_ok=True)
    compact_pointer.parent.mkdir(parents=True, exist_ok=True)
    full_pointer.write_bytes(b"old-full\n")
    compact_pointer.write_bytes(b"old-compact\n")

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("injected compact copy failure")

    monkeypatch.setattr("househunter.mountain.shutil.copy2", fail_copy)

    with pytest.raises(OSError, match="injected compact copy failure"):
        stage_compact_fallback(paths, release, manifest)

    assert full_pointer.read_bytes() == b"old-full\n"
    assert compact_pointer.read_bytes() == b"old-compact\n"
    compact_root = compact_pointer.parent
    assert not any(
        path.name.startswith(".") and path.name.endswith(".tmp") for path in compact_root.iterdir()
    )


def test_external_promotion_rejects_mismatched_source_lock(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    blocks = _national_blocks()
    candidate = write_release(
        blocks,
        tmp_path / "candidate",
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )

    with pytest.raises(HouseHunterError, match="independently reviewed source lock"):
        promote_release(
            paths,
            candidate,
            reviewed_source_lock=_reviewed_source_lock(blocks),
            reviewed_source_lock_sha256="2" * 64,
            expected_raw_blocks=blocks,
        )

    assert not (paths.data / "mountain" / "current.json").exists()


def test_external_promotion_rejects_altered_source_provenance(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    blocks = _national_blocks()
    sources = _source_provenance()
    sources["items"][0]["license"] = "altered license"
    candidate = write_release(
        blocks,
        tmp_path / "candidate",
        data_release="fixture",
        sources=sources,
        national_expectations=_national_expectations(blocks),
    )

    with pytest.raises(HouseHunterError, match="independently reviewed source lock"):
        promote_release(
            paths,
            candidate,
            reviewed_source_lock=_reviewed_source_lock(blocks),
            reviewed_source_lock_sha256="1" * 64,
            expected_raw_blocks=blocks,
        )

    assert not (paths.data / "mountain" / "current.json").exists()


def test_external_promotion_rejects_values_not_recomputed_from_pack(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    blocks = _national_blocks()
    changed = blocks.with_columns(
        pl.when(pl.col("block_geoid") == blocks["block_geoid"][0])
        .then(pl.col("relief_20km_m") + 1.0)
        .otherwise(pl.col("relief_20km_m"))
        .alias("relief_20km_m")
    )
    candidate = write_release(
        changed,
        tmp_path / "candidate",
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )

    with pytest.raises(HouseHunterError, match="prepared-pack raw metrics"):
        promote_release(
            paths,
            candidate,
            reviewed_source_lock=_reviewed_source_lock(blocks),
            reviewed_source_lock_sha256="1" * 64,
            expected_raw_blocks=blocks,
        )

    assert not (paths.data / "mountain" / "current.json").exists()


def test_raw_block_promotion_runs_shared_snapshot_finalizer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocks = _national_blocks()
    raw = tmp_path / "raw.parquet"
    blocks.write_parquet(raw)
    source_lock = tmp_path / "source-lock.json"
    source_lock.write_text("{}\n")
    lock = {
        "schema_version": 2,
        "sources": [{**_source_provenance()["items"][0], "path": str(raw)}],
        "expected_states": _national_expectations(blocks),
        "block_geoid_sha256": national_block_geoid_sha256(blocks),
    }
    finalized: list[object] = []

    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("househunter.mountain_gis.verify_source_lock", lambda *args, **kwargs: lock)
    monkeypatch.setattr(
        "househunter.mountain_gis.locked_source_paths", lambda *args, **kwargs: {raw.resolve()}
    )

    def finalize(*args: object, **kwargs: object) -> tuple[object, object, list[object]]:
        finalized.append(args)
        return tmp_path / "snapshot", tmp_path / "compact", []

    monkeypatch.setattr("househunter.cli._rebuild_after_mountain_promotion", finalize)
    result = CliRunner().invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "fixture",
            "--raw-blocks",
            str(raw),
            "--source-lock",
            str(source_lock),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(finalized) == 1


def test_generated_promotion_retains_current_and_one_rollback(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    blocks = _national_blocks()
    releases = []
    for number in range(3):
        release, _ = write_and_promote_release(
            paths,
            blocks,
            data_release=f"fixture-{number}",
            sources=_source_provenance(),
            national_expectations=_national_expectations(blocks),
        )
        releases.append(release)

    repeated, _ = write_and_promote_release(
        paths,
        blocks,
        data_release="fixture-2",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )

    removed = prune_owned_releases(paths)
    pointer = json.loads((paths.data / "mountain" / "current.json").read_text())
    assert pointer["release_id"] == releases[2].name
    assert pointer["rollback_release_id"] == releases[1].name
    assert repeated == releases[2]
    assert removed == [releases[0].name]
    assert releases[1].is_dir() and releases[2].is_dir()


def test_promote_preserves_non_v1_current_release_errors(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    blocks = _national_blocks()
    write_and_promote_release(
        paths,
        blocks,
        data_release="current",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    current_id = json.loads((paths.data / "mountain" / "current.json").read_text())["release_id"]
    parquet = next((paths.data / "mountain" / "releases" / current_id).glob("*.parquet"))
    parquet.write_bytes(parquet.read_bytes() + b"\x00")

    with pytest.raises(HouseHunterError, match="Cannot read Mountain") as caught:
        write_and_promote_release(
            paths,
            blocks,
            data_release="next",
            sources=_source_provenance(),
            national_expectations=_national_expectations(blocks),
        )
    assert "Current Mountain release is v1" not in str(caught.value)


def test_promote_still_instructs_rescore_for_schema_one_current(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    blocks = _national_blocks()
    write_and_promote_release(
        paths,
        blocks,
        data_release="current",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    current_id = json.loads((paths.data / "mountain" / "current.json").read_text())["release_id"]
    manifest_path = paths.data / "mountain" / "releases" / current_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = 1
    manifest_path.write_text(json.dumps(manifest) + "\n")

    with pytest.raises(HouseHunterError, match=r"Current Mountain release is v1"):
        write_and_promote_release(
            paths,
            blocks,
            data_release="next",
            sources=_source_provenance(),
            national_expectations=_national_expectations(blocks),
        )


def test_generated_release_is_rejected_before_exceeding_storage_ceiling(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    monkeypatch.setattr("househunter.mountain_pack.allocated_size", lambda path: 42_000_000_000)
    monkeypatch.setattr(
        "househunter.mountain_pack.shutil.disk_usage",
        lambda path: type("Usage", (), {"free": 100_000_000_000})(),
    )

    with pytest.raises(HouseHunterError, match="engineering ceiling"):
        write_and_promote_release(
            paths,
            _national_blocks(),
            data_release="fixture",
            sources=_source_provenance(),
            national_expectations=_national_expectations(_national_blocks()),
        )

    assert not (paths.data / "mountain" / "releases").exists()


def test_runtime_uses_validated_bundled_fallback_without_pointer(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    blocks = _national_blocks()
    release = write_release(
        blocks,
        tmp_path / "release",
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    bundle = write_compact_bundle(release, tmp_path / "bundle")

    loaded = current_compact_release(paths, bundled_path=bundle)

    assert loaded is not None
    assert loaded[0] == bundle
    assert loaded[1]["release_id"] == validate_release(release)["release_id"]


def test_committed_bundle_meets_pinned_magnitude_regression_gates() -> None:
    manifest, _, _ = load_compact_release(BUNDLED_COMPACT_RELEASE)
    assert manifest["release_id"] == "95e0f7fba44f7309"
    assert (
        manifest["full_manifest_sha256"]
        == "e2b4a540d2da501f2980d197696dec766b041fab23e0e35b33357dec5f5c1e94"
    )
    files = manifest["files"]
    tracts = pl.read_parquet(BUNDLED_COMPACT_RELEASE / files["tracts"]["filename"])
    counties = pl.read_parquet(BUNDLED_COMPACT_RELEASE / files["counties"]["filename"])
    western_states = {"AK", "AZ", "CA", "CO", "HI", "ID", "MT", "NV", "NM", "OR", "UT", "WA", "WY"}

    scored_tracts = tracts.filter(pl.col("mountain_magnitude").is_not_null())
    scored_counties = counties.filter(pl.col("mountain_magnitude").is_not_null())
    assert scored_tracts.height == 83_848
    assert scored_counties.height == 3_143
    assert scored_tracts["mountain_magnitude"].max() == pytest.approx(4.9235, abs=0.0001)
    assert scored_counties["mountain_magnitude"].max() == pytest.approx(3.4973, abs=0.0001)

    for frame, expected_median, expected_p95 in (
        (scored_tracts, 0.88, 1.94),
        (scored_counties, 0.96, 2.14),
    ):
        values = frame.filter(pl.col("state").is_in(western_states))[
            "mountain_magnitude"
        ].to_numpy()
        median = float(np.median(values))
        p95 = float(np.quantile(values, 0.95))
        maximum = float(values.max())
        assert median == pytest.approx(expected_median, abs=0.02)
        assert p95 == pytest.approx(expected_p95, abs=0.02)
        assert p95 - median >= 0.75
        assert maximum - p95 >= 0.75

    anchor_expected = {
        "08097": 3.4973,
        "08117": 2.3213,
        "49051": 2.2421,
        "49035": 1.6109,
        "08013": 1.2349,
    }
    anchors = {
        place_id: magnitude
        for place_id, magnitude in counties.filter(pl.col("place_id").is_in(anchor_expected))
        .select("place_id", "mountain_magnitude")
        .iter_rows()
    }
    assert anchors == anchor_expected


def test_runtime_blocks_on_incomplete_or_unsafe_migration_journal(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    journal = paths.data / "mountain" / "migration-v2.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"phase":"staged"}\n')

    with pytest.raises(HouseHunterError, match=r"incomplete.*rescore-v1"):
        current_compact_release(paths)

    journal.unlink()
    journal.symlink_to(tmp_path / "outside")
    with pytest.raises(HouseHunterError, match="journal is unsafe"):
        current_compact_release(paths)


@pytest.mark.parametrize("pointer_kind", ["full", "compact"])
def test_runtime_rejects_broken_managed_pointer_symlinks(tmp_path, pointer_kind: str) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    if pointer_kind == "full":
        pointer = paths.data / "mountain" / "current.json"
    else:
        pointer = paths.data / "mountain" / "compact" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(tmp_path / "missing-pointer-target")

    with pytest.raises(HouseHunterError, match="pointer"):
        current_compact_release(paths)


def test_rescore_v1_rejects_symlinked_build_root_before_writing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    _, source_lock, source_lock_path, _, _ = _install_active_legacy_release(
        paths, tmp_path / "fixture"
    )
    _stub_migration_snapshot_pipeline(monkeypatch, paths, source_lock)
    paths.builds.rmdir()
    outside = tmp_path / "outside-builds"
    outside.mkdir()
    paths.builds.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HouseHunterError, match="build directory cannot be a symlink"):
        mountain_migration.rescore_v1_release(paths, source_lock_path)

    assert list(outside.iterdir()) == []


def test_runtime_rejects_mixed_managed_full_and_compact_pointers(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    blocks = _national_blocks()
    release, manifest = write_and_promote_release(
        paths,
        blocks,
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    write_and_promote_compact_fallback(paths, release, manifest)
    compact_pointer = paths.data / "mountain" / "compact" / "current.json"
    compact_pointer.write_text(json.dumps({"schema_version": 1, "release_id": "0" * 16}) + "\n")

    with pytest.raises(HouseHunterError, match=r"pointers disagree.*rescore-v1"):
        current_compact_release(paths)


def test_runtime_never_falls_through_from_managed_v1_to_v2_bundle(tmp_path) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    blocks = _national_blocks()
    legacy = write_release(
        blocks,
        tmp_path / "legacy",
        data_release="fixture",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )

    def make_legacy(manifest: dict[str, object]) -> None:
        manifest["schema_version"] = 1
        manifest["score_version"] = manifest.pop("base_score_version")
        manifest.pop("magnitude_version")
        manifest.pop("magnitude_contract")

    legacy_manifest = _rewrite_manifest_identity(legacy, make_legacy)
    releases = paths.data / "mountain" / "releases"
    releases.mkdir(parents=True)
    installed = releases / legacy_manifest["release_id"]
    legacy.rename(installed)
    (paths.data / "mountain" / "current.json").write_text(
        json.dumps({"schema_version": 1, "release_id": legacy_manifest["release_id"]}) + "\n"
    )
    bundled_release = write_release(
        blocks,
        tmp_path / "bundle-full",
        data_release="bundle",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    bundle = write_compact_bundle(bundled_release, tmp_path / "bundle")

    with pytest.raises(HouseHunterError, match=r"v1 scoring.*rescore-v1"):
        current_compact_release(paths, bundled_path=bundle)


def test_managed_compact_fallback_is_atomic_queryable_and_pruned(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    blocks = _national_blocks()
    first_release = write_release(
        blocks,
        tmp_path / "first-release",
        data_release="fixture-1",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    first_manifest = validate_release(first_release)
    reservations: list[int] = []
    monkeypatch.setattr(
        "househunter.mountain_pack.ensure_storage_budget",
        lambda root, *, reserve_bytes=0: reservations.append(reserve_bytes),
    )
    first = write_and_promote_compact_fallback(paths, first_release, first_manifest)

    loaded = current_compact_release(paths)
    assert loaded is not None
    assert loaded[0] == first
    assert loaded[1]["release_id"] == first_manifest["release_id"]
    assert reservations and reservations[0] > 0

    second_release = write_release(
        blocks,
        tmp_path / "second-release",
        data_release="fixture-2",
        sources=_source_provenance(),
        national_expectations=_national_expectations(blocks),
    )
    second_manifest = validate_release(second_release)
    second = write_and_promote_compact_fallback(paths, second_release, second_manifest)

    assert prune_owned_compact_fallbacks(paths) == [first.name]
    assert second.is_dir()
    assert not first.exists()


def test_complete_release_cannot_be_self_asserted(tmp_path) -> None:
    with pytest.raises(HouseHunterError, match="differ|cover"):
        write_release(
            _raw_blocks(),
            tmp_path / "candidate",
            data_release="fixture",
            sources=_source_provenance(),
            national_expectations=_national_expectations(_national_blocks()),
        )


def test_national_expectations_reject_same_count_and_population_geoid_substitution() -> None:
    blocks = _national_blocks()
    digest = national_block_geoid_sha256(blocks)
    substituted = blocks.with_columns(
        pl.when(pl.col("block_geoid") == blocks["block_geoid"][0])
        .then(pl.lit("010010001009999"))
        .otherwise(pl.col("block_geoid"))
        .alias("block_geoid")
    )

    with pytest.raises(HouseHunterError, match="GEOIDs differ"):
        validate_national_expectations(
            substituted,
            _national_expectations(blocks),
            expected_block_geoid_sha256=digest,
        )


def test_complete_release_rejects_blocks_outside_national_scope(tmp_path) -> None:
    blocks = _national_blocks()
    territory = blocks.head(1).with_columns(
        pl.lit("720010001001001").alias("block_geoid"),
        pl.lit("72001000100").alias("tract_geoid"),
        pl.lit("72001").alias("county_fips"),
        pl.lit("PR").alias("state"),
    )

    with pytest.raises(HouseHunterError, match="unexpected PR"):
        write_release(
            blocks.vstack(territory),
            tmp_path / "candidate",
            data_release="fixture",
            sources=_source_provenance(),
            national_expectations=_national_expectations(blocks),
        )


def test_mountain_cli_build_validate_and_inspect(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "raw.parquet"
    blocks = _national_blocks()
    blocks.write_parquet(raw)
    output = tmp_path / "candidate"
    source_lock = tmp_path / "source-lock.json"
    source_lock.write_text("{}\n")
    lock = {
        "schema_version": 2,
        "sources": [{**_source_provenance()["items"][0], "path": str(raw)}],
        "expected_states": _national_expectations(blocks),
        "block_geoid_sha256": national_block_geoid_sha256(blocks),
    }
    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("househunter.mountain_gis.verify_source_lock", lambda *args, **kwargs: lock)
    monkeypatch.setattr(
        "househunter.mountain_gis.locked_source_paths", lambda *args, **kwargs: {raw.resolve()}
    )
    runner = CliRunner()

    built = runner.invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "fixture",
            "--raw-blocks",
            str(raw),
            "--output",
            str(output),
            "--source-lock",
            str(source_lock),
            "--no-promote",
        ],
    )
    assert built.exit_code == 0, built.output
    validated = runner.invoke(app, ["mountain", "validate", str(output)])
    assert validated.exit_code == 0, validated.output
    inspected = runner.invoke(app, ["mountain", "inspect", "01001000100", "--release", str(output)])
    assert inspected.exit_code == 0, inspected.output
    payload = json.loads(inspected.output)
    assert payload["mountain_magnitude"] == 1.4065
    assert "mountain_score" not in payload
    assert "mountain_score_version" not in payload


def test_mountain_cli_rejects_unsafe_release_name(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "raw.parquet"
    _raw_blocks().write_parquet(raw)
    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))

    result = CliRunner().invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "../escape",
            "--raw-blocks",
            str(raw),
            "--no-promote",
        ],
    )

    assert result.exit_code == 1
    assert "must contain only" in result.output


def test_mountain_cli_rejects_removed_allow_partial_option(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "raw.parquet"
    _raw_blocks().write_parquet(raw)
    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))

    result = CliRunner().invoke(
        app,
        [
            "mountain",
            "build",
            "--data-release",
            "fixture",
            "--raw-blocks",
            str(raw),
            "--allow-partial",
        ],
    )

    assert result.exit_code == 2
    assert "No such option: --allow-partial" in result.output
