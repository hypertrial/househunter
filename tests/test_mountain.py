from __future__ import annotations

import json

import numpy as np
import polars as pl
import pytest
from fastapi.responses import JSONResponse
from typer.testing import CliRunner

from househunter.cli import app
from househunter.config import canonical_json, sha256_bytes, sha256_file
from househunter.errors import HouseHunterError
from househunter.geography import STATE_BY_FIPS
from househunter.mountain import (
    IN_SCOPE_STATES,
    access_metrics,
    aggregate_scores,
    current_compact_release,
    national_block_geoid_sha256,
    promote_release,
    prune_owned_compact_fallbacks,
    prune_owned_releases,
    score_blocks,
    terrain_metrics,
    validate_national_expectations,
    validate_release,
    window_cells,
    write_and_promote_compact_fallback,
    write_and_promote_release,
    write_compact_bundle,
    write_release,
)


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


def _national_blocks() -> pl.DataFrame:
    raw = _raw_blocks()
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


def _reviewed_source_lock(blocks: pl.DataFrame) -> dict[str, object]:
    return {
        "schema_version": 2,
        "expected_states": _national_expectations(blocks),
        "block_geoid_sha256": national_block_geoid_sha256(blocks),
        "sources": _source_provenance()["items"],
    }


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
    assert aggregate["mountain_score"].item() is None


def test_aggregate_scores_uses_population_and_coverage_statuses() -> None:
    scored = score_blocks(_raw_blocks())
    partial = scored.with_columns(
        pl.when(pl.col("block_geoid") == "010010001001001")
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("mountain_score"))
        .alias("mountain_score")
    )

    tracts = aggregate_scores(partial, "tract_geoid")
    alabama = tracts.filter(pl.col("place_id") == "01001000100").row(0, named=True)
    alaska = tracts.filter(pl.col("place_id") == "02001000100").row(0, named=True)

    assert alabama["mountain_population_coverage"] == 0.75
    assert alabama["mountain_coverage_status"] == "insufficient_coverage"
    assert alabama["mountain_score"] is None
    assert alaska["mountain_coverage_status"] == "complete"
    assert alaska["mountain_score"] == 40.0


@pytest.mark.parametrize("geography", ["tract_geoid", "county_fips"])
def test_aggregate_scores_returns_null_for_field_without_population(geography: str) -> None:
    scored = score_blocks(_raw_blocks()).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("nearest_mountain_trail_km")
    )
    aggregates = aggregate_scores(scored, geography)
    assert aggregates["nearest_mountain_trail_km"].null_count() == aggregates.height
    response = JSONResponse(aggregates.to_dicts())
    assert json.loads(response.body)[0]["nearest_mountain_trail_km"] is None


def test_release_is_deterministic_validated_and_compact(tmp_path) -> None:
    first = write_release(_raw_blocks(), tmp_path / "one", data_release="fixture", sources={})
    second = write_release(_raw_blocks(), tmp_path / "two", data_release="fixture", sources={})

    one = validate_release(first)
    two = validate_release(second)
    assert one["release_id"] == two["release_id"]
    assert one["compact_bytes"] < 50 * 1024 * 1024
    assert one["files"]["tracts"]["sha256"] == two["files"]["tracts"]["sha256"]


def test_release_rejects_forged_identity(tmp_path) -> None:
    release = write_release(
        _raw_blocks(), tmp_path / "candidate", data_release="fixture", sources={}
    )
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["release_id"] = "../../outside"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(HouseHunterError, match="identity"):
        validate_release(release)


def test_release_recomputes_block_percentiles_instead_of_trusting_scores(tmp_path) -> None:
    release = write_release(
        _raw_blocks(), tmp_path / "candidate", data_release="fixture", sources={}
    )
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
    release = write_release(
        _raw_blocks(), tmp_path / "candidate", data_release="fixture", sources={}
    )
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
    release = write_release(_raw_blocks(), tmp_path / column, data_release="fixture", sources={})
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


def test_generated_release_is_rejected_before_exceeding_storage_ceiling(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from househunter.config import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path)
    monkeypatch.setattr(
        "househunter.mountain_pack.allocated_size", lambda path: 42_000_000_000
    )
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
    _raw_blocks().write_parquet(raw)
    output = tmp_path / "candidate"
    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))
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
            "--allow-partial",
            "--no-promote",
        ],
    )
    assert built.exit_code == 0, built.output
    validated = runner.invoke(app, ["mountain", "validate", str(output)])
    assert validated.exit_code == 0, validated.output
    inspected = runner.invoke(app, ["mountain", "inspect", "01001000100", "--release", str(output)])
    assert inspected.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["mountain_score"] == 7.5


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
            "--allow-partial",
            "--no-promote",
        ],
    )

    assert result.exit_code == 1
    assert "must contain only" in result.output


def test_mountain_cli_never_promotes_partial_release(tmp_path, monkeypatch) -> None:
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

    assert result.exit_code == 1
    assert "cannot be promoted" in result.output
