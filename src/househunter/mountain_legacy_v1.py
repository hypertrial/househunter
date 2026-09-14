from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from .config import canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .mountain import (
    AGGREGATE_MEANS,
    BLOCK_SCORE_COLUMNS,
    FULL_RELEASE_MAX_BYTES,
    IN_SCOPE_STATES,
    PIPELINE_VERSION,
    RAW_PRECISION,
    SCORE_VERSION,
    SCORE_WEIGHTS,
    _complete_release_expectations,
    _reconstruct_block_scores,
    _require_columns,
    _validate_block_ranges,
    validate_national_expectations,
)
from .mountain_gis import source_provenance_item
from .mountain_paths import OWNERSHIP_MARKER

LEGACY_AGGREGATE_MEANS = [*AGGREGATE_MEANS, "mountain_score"]


def _reconstruct_legacy_aggregates(blocks: pl.DataFrame, geography: str) -> pl.DataFrame:
    """Frozen independent oracle for the aggregate schema written by v1."""
    base = (
        blocks.group_by(geography)
        .agg(
            pl.col("state").first(),
            pl.col("pop20").sum().alias("population_2020"),
            pl.when(pl.col("mountain_score").is_not_null())
            .then(pl.col("pop20"))
            .otherwise(0)
            .sum()
            .alias("mountain_covered_population"),
        )
        .with_columns(
            pl.when(pl.col("population_2020") > 0)
            .then(pl.col("mountain_covered_population") / pl.col("population_2020"))
            .otherwise(0.0)
            .round(6)
            .alias("mountain_population_coverage")
        )
        .with_columns(
            pl.when(~pl.col("state").is_in(IN_SCOPE_STATES))
            .then(pl.lit("outside_scope"))
            .when(pl.col("population_2020") == 0)
            .then(pl.lit("zero_population"))
            .when(pl.col("mountain_population_coverage") >= 1 - 1e-9)
            .then(pl.lit("complete"))
            .when(pl.col("mountain_population_coverage") >= 0.9)
            .then(pl.lit("partial"))
            .otherwise(pl.lit("insufficient_coverage"))
            .alias("mountain_coverage_status")
        )
    )
    eligible = (
        pl.col("state").is_in(IN_SCOPE_STATES)
        & (pl.col("mountain_population_coverage") >= 0.9)
        & (pl.col("mountain_covered_population") > 0)
    )
    for column in LEGACY_AGGREGATE_MEANS:
        means = (
            blocks.filter(pl.col(column).is_not_null())
            .group_by(geography)
            .agg(
                (pl.col(column) * pl.col("pop20")).sum().alias("_numerator"),
                pl.col("pop20").sum().alias("_denominator"),
            )
            .with_columns(
                pl.when(pl.col("_denominator") > 0)
                .then(pl.col("_numerator") / pl.col("_denominator"))
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .alias("_mean")
            )
            .select(geography, "_mean")
        )
        base = (
            base.join(means, on=geography, how="left")
            .with_columns(
                pl.when(eligible)
                .then(pl.col("_mean"))
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .round(2 if column in [*SCORE_WEIGHTS, "mountain_score"] else RAW_PRECISION[column])
                .alias(column)
            )
            .drop("_mean")
        )
    return (
        base.with_columns(
            pl.lit(SCORE_VERSION).alias("mountain_score_version"),
            pl.lit(PIPELINE_VERSION).alias("mountain_pipeline_version"),
        )
        .rename({geography: "place_id"})
        .sort("place_id")
    )


def validate_legacy_v1_release(
    path: Path,
    *,
    reviewed_source_lock: dict[str, object],
    reviewed_source_lock_sha256: str,
) -> tuple[dict[str, object], pl.DataFrame]:
    """Fully validate one owned national v1 release for the migration command only."""
    marker = path / OWNERSHIP_MARKER
    if (
        path.is_symlink()
        or not path.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
        or marker.read_text() != "release-v1\n"
        or (path / "manifest.json").is_symlink()
    ):
        raise HouseHunterError("Mountain v1 migration requires an owned non-symlinked release")
    try:
        manifest = json.loads((path / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain v1 manifest: {exc}") from exc
    if (
        manifest.get("schema_version") != 1
        or manifest.get("pipeline_version") != PIPELINE_VERSION
        or manifest.get("score_version") != SCORE_VERSION
        or manifest.get("national_complete") is not True
    ):
        raise HouseHunterError("Mountain rescore requires a complete v1 release")
    identity = {key: value for key, value in manifest.items() if key != "release_id"}
    if manifest.get("release_id") != sha256_bytes(canonical_json(identity))[:16]:
        raise HouseHunterError("Mountain v1 release identity does not match its manifest")
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or set(files) != {"blocks", "tracts", "counties"}
        or any(not isinstance(value, dict) for value in files.values())
    ):
        raise HouseHunterError("Mountain v1 manifest has an invalid file set")
    expected_entries = {
        "manifest.json",
        OWNERSHIP_MARKER,
        *(str(value.get("filename")) for value in files.values()),
    }
    children = list(path.iterdir())
    if {child.name for child in children} != expected_entries or any(
        child.is_symlink() or not child.is_file() for child in children
    ):
        raise HouseHunterError("Mountain v1 release contains unexpected or unsafe entries")

    frames: dict[str, pl.DataFrame] = {}
    for name, metadata in files.items():
        filename = metadata.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise HouseHunterError(f"Mountain v1 {name} filename is invalid")
        artifact = path / filename
        try:
            frame = pl.read_parquet(artifact)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise HouseHunterError(f"Cannot read Mountain v1 {name} artifact: {exc}") from exc
        if metadata.get("rows") != frame.height or metadata.get("sha256") != sha256_file(artifact):
            raise HouseHunterError(f"Mountain v1 {name} artifact does not match its manifest")
        frames[name] = frame

    expectations = _complete_release_expectations(manifest)
    sources = manifest["sources"]
    expected_items = [
        source_provenance_item(item)
        for item in reviewed_source_lock.get("sources", [])
        if isinstance(item, dict)
    ]
    if (
        sources.get("source_lock_sha256") != reviewed_source_lock_sha256
        or sources.get("items") != expected_items
        or manifest.get("national_expectations") != reviewed_source_lock.get("expected_states")
        or manifest.get("block_geoid_sha256") != reviewed_source_lock.get("block_geoid_sha256")
    ):
        raise HouseHunterError("Mountain v1 release differs from the reviewed source lock")

    blocks = frames["blocks"]
    raw_columns = [
        "block_geoid",
        "tract_geoid",
        "county_fips",
        "state",
        "pop20",
        *RAW_PRECISION,
    ]
    _require_columns(
        blocks,
        [*raw_columns, *BLOCK_SCORE_COLUMNS, "mountain_score_version", "mountain_pipeline_version"],
    )
    if blocks.select(
        pl.any_horizontal(
            pl.col(column).is_null()
            for column in (
                "block_geoid",
                "tract_geoid",
                "county_fips",
                "state",
                "pop20",
            )
        ).any()
    ).item():
        raise HouseHunterError(
            "Mountain v1 block identifiers, state, and population cannot be null"
        )
    invalid_ids = blocks.filter(
        ~pl.col("block_geoid").str.contains(r"^\d{15}$")
        | (pl.col("tract_geoid") != pl.col("block_geoid").str.slice(0, 11))
        | (pl.col("county_fips") != pl.col("block_geoid").str.slice(0, 5))
    )
    if blocks["block_geoid"].n_unique() != blocks.height or invalid_ids.height:
        raise HouseHunterError("Mountain v1 block identifiers are invalid or duplicated")
    if blocks.select(pl.col("pop20").is_null().any()).item() or blocks.filter(
        pl.col("pop20") < 0
    ).height:
        raise HouseHunterError("Mountain v1 block population is invalid")
    _validate_block_ranges(blocks, label="v1 block")
    rounded_raw = blocks.select(
        pl.col(column).round(precision).alias(column) for column, precision in RAW_PRECISION.items()
    )
    if not blocks.select(*RAW_PRECISION).equals(rounded_raw):
        raise HouseHunterError("Mountain v1 raw metrics are not canonically rounded")
    reconstructed = _reconstruct_block_scores(blocks.select(raw_columns))
    score_columns = [
        "block_geoid",
        *BLOCK_SCORE_COLUMNS,
        "mountain_score_version",
        "mountain_pipeline_version",
    ]
    if not blocks.select(score_columns).equals(reconstructed.select(score_columns)):
        raise HouseHunterError("Mountain v1 scores do not match raw block metrics")
    validate_national_expectations(
        blocks,
        expectations,
        expected_block_geoid_sha256=str(manifest["block_geoid_sha256"]),
    )
    for name, geography in (("tracts", "tract_geoid"), ("counties", "county_fips")):
        if frames[name].select(pl.col("place_id").is_null().any()).item():
            raise HouseHunterError(f"Mountain v1 {name} identifiers cannot be null")
        expected = _reconstruct_legacy_aggregates(blocks, geography)
        if frames[name].columns != expected.columns or not frames[name].equals(expected):
            raise HouseHunterError(f"Mountain v1 {name} do not match block aggregation")
    allocated = sum(
        child.lstat().st_blocks * 512 for child in path.rglob("*") if not child.is_symlink()
    )
    if allocated > FULL_RELEASE_MAX_BYTES:
        raise HouseHunterError("Mountain v1 release exceeds the full-release size ceiling")
    return manifest, blocks.select(raw_columns).sort("block_geoid")
