from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    import numpy as np

from .config import RuntimePaths, canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .mountain_paths import (
    OWNERSHIP_MARKER,
    ensure_owned_child,
    ensure_safe_directory,
    lexical_path,
    remove_owned_child,
    require_owned_child,
)

PIPELINE_VERSION = "mountain_pipeline_v1"
SCORE_VERSION = "mountain_score_v1"
TERRITORIES = frozenset({"AS", "GU", "MP", "PR", "VI"})
IN_SCOPE_STATES = frozenset(STATE_BY_FIPS.values()) - TERRITORIES
SCORE_WEIGHTS = {
    "relief_20km_pct": 0.45,
    "rugged_pct": 0.20,
    "public_mountain_access_pct": 0.20,
    "trail_access_pct": 0.15,
}
RAW_COMPONENTS = {
    "relief_20km_m": "relief_20km_pct",
    "rugged_fraction_20km": "rugged_pct",
    "public_mountain_access_raw": "public_mountain_access_pct",
    "trail_access_raw": "trail_access_pct",
}
SCORE_COLUMNS = [*SCORE_WEIGHTS, "mountain_score"]
RAW_PRECISION = {
    "relief_5km_m": 0,
    "relief_10km_m": 0,
    "relief_20km_m": 0,
    "relief_40km_m": 0,
    "rugged_fraction_20km": 3,
    "public_mountain_access_raw": 1,
    "trail_access_raw": 1,
    "open_mountain_km2_5": 1,
    "open_mountain_km2_15": 1,
    "open_mountain_km2_30": 1,
    "restricted_mountain_km2_30": 1,
    "closed_mountain_km2_30": 1,
    "unknown_mountain_km2_30": 1,
    "nearest_mountain_trail_km": 1,
    "mountain_trail_km_10": 1,
    "mountain_trail_km_25": 1,
}
AGGREGATE_MEANS = [
    "relief_5km_m",
    "relief_10km_m",
    "relief_20km_m",
    "relief_40km_m",
    "rugged_fraction_20km",
    "public_mountain_access_raw",
    "trail_access_raw",
    "open_mountain_km2_5",
    "open_mountain_km2_15",
    "open_mountain_km2_30",
    "restricted_mountain_km2_30",
    "closed_mountain_km2_30",
    "unknown_mountain_km2_30",
    "nearest_mountain_trail_km",
    "mountain_trail_km_10",
    "mountain_trail_km_25",
    *SCORE_COLUMNS,
]
MOUNTAIN_RUNTIME_COLUMNS = [
    *AGGREGATE_MEANS,
    "mountain_score_version",
    "mountain_pipeline_version",
    "mountain_population_coverage",
    "mountain_coverage_status",
]
BUNDLED_COMPACT_RELEASE = Path(__file__).with_name("assets") / "mountain"
FULL_RELEASE_MAX_BYTES = 4_000_000_000
COMPACT_RELEASE_MAX_BYTES = 50 * 1024 * 1024


def _allocated_tree_bytes(path: Path) -> int:
    return sum(
        item.lstat().st_blocks * 512 for item in (path, *path.rglob("*")) if not item.is_symlink()
    )


def window_cells(radius_km: float, cell_size_m: float = 250) -> int:
    """Return the nearest odd cell width of a same-area square for a radius."""
    cells = math.sqrt(math.pi) * radius_km * 1_000 / cell_size_m
    return max(1, int(math.floor(cells / 2)) * 2 + 1)


def _window_sum(values: np.ndarray, size: int) -> np.ndarray:
    from scipy import ndimage

    return ndimage.uniform_filter(values.astype(float), size=size, mode="constant") * size**2


def terrain_metrics(
    elevation_m: np.ndarray,
    *,
    cell_size_m: float = 250,
    relief_radii_km: Iterable[int] = (5, 10, 20, 40),
) -> dict[str, np.ndarray]:
    """Compute the v1 terrain arrays on one equal-area grid including its halo."""
    import numpy as np
    from scipy import ndimage

    elevation = np.asarray(elevation_m, dtype=float)
    if elevation.ndim != 2 or cell_size_m <= 0:
        raise HouseHunterError("Mountain elevation must be a 2-D grid with a positive cell size")
    valid = np.isfinite(elevation)
    if not valid.any():
        raise HouseHunterError("Mountain elevation grid contains no valid cells")
    high = np.where(valid, elevation, -np.inf)
    low = np.where(valid, elevation, np.inf)
    relief: dict[str, np.ndarray] = {}
    for radius in relief_radii_km:
        size = window_cells(radius, cell_size_m)
        maximum = ndimage.maximum_filter(high, size=size, mode="constant", cval=-np.inf)
        minimum = ndimage.minimum_filter(low, size=size, mode="constant", cval=np.inf)
        values = maximum - minimum
        values[~np.isfinite(values)] = np.nan
        relief[f"relief_{radius}km_m"] = values
    if "relief_5km_m" not in relief:
        raise HouseHunterError("Mountain terrain processing requires the 5 km relief window")
    nearest_valid = ndimage.distance_transform_edt(
        ~valid, return_distances=False, return_indices=True
    )
    filled = elevation[tuple(nearest_valid)]
    row_gradient, column_gradient = np.gradient(filled, cell_size_m, cell_size_m)
    slope = np.degrees(np.arctan(np.hypot(row_gradient, column_gradient)))
    mountain = valid & ((slope >= 15) | (relief["relief_5km_m"] >= 300))
    rugged_size = window_cells(20, cell_size_m)
    valid_count = _window_sum(valid, rugged_size)
    mountain_count = _window_sum(mountain, rugged_size)
    rugged = np.divide(
        mountain_count,
        valid_count,
        out=np.full_like(mountain_count, np.nan),
        where=valid_count > 0,
    )
    return {
        **relief,
        "slope_degrees": np.where(valid, slope, np.nan),
        "mountain_mask": mountain,
        "rugged_fraction_20km": rugged,
    }


def access_metrics(
    mountain_mask: np.ndarray,
    pad_access: np.ndarray,
    trail_length_km: np.ndarray,
    *,
    cell_size_m: float = 250,
) -> dict[str, np.ndarray]:
    """Compute v1 PAD-US rings and mountain-trail access on an aligned grid.

    PAD-US values are 1=Open, 2=Restricted, 3=Closed, and 4=Unknown. Trail
    cells contain non-water trail length in kilometres.
    """
    import numpy as np
    from scipy import ndimage

    mountain = np.asarray(mountain_mask, dtype=bool)
    access = np.asarray(pad_access)
    trails = np.asarray(trail_length_km, dtype=float)
    if mountain.ndim != 2 or access.shape != mountain.shape or trails.shape != mountain.shape:
        raise HouseHunterError("Mountain access grids must be aligned 2-D arrays")
    if cell_size_m <= 0 or np.any(np.isfinite(trails) & (trails < 0)):
        raise HouseHunterError("Mountain access grids contain an invalid cell size or trail length")
    cell_km2 = (cell_size_m / 1_000) ** 2

    def area(code: int, radius: int) -> np.ndarray:
        return (
            _window_sum(mountain & (access == code), window_cells(radius, cell_size_m)) * cell_km2
        )

    open_5 = area(1, 5)
    open_15_total = area(1, 15)
    open_30_total = area(1, 30)
    open_15 = np.maximum(0, open_15_total - open_5)
    open_30 = np.maximum(0, open_30_total - open_15_total)
    mountain_trails = np.where(mountain & np.isfinite(trails), trails, 0.0)
    trail_10 = _window_sum(mountain_trails, window_cells(10, cell_size_m))
    trail_25_total = _window_sum(mountain_trails, window_cells(25, cell_size_m))
    trail_10_25 = np.maximum(0, trail_25_total - trail_10)
    presence = mountain_trails > 0
    nearest = ndimage.distance_transform_edt(~presence) * cell_size_m / 1_000
    nearest[(nearest > 100) | ~np.isfinite(nearest)] = np.nan
    if not presence.any():
        nearest[:] = np.nan
    return {
        "open_mountain_km2_5": open_5,
        "open_mountain_km2_15": open_15,
        "open_mountain_km2_30": open_30,
        "restricted_mountain_km2_30": area(2, 30),
        "closed_mountain_km2_30": area(3, 30),
        "unknown_mountain_km2_30": area(4, 30),
        "public_mountain_access_raw": open_5 + 0.75 * open_15 + 0.40 * open_30,
        "nearest_mountain_trail_km": nearest,
        "mountain_trail_km_10": trail_10,
        "mountain_trail_km_25": trail_25_total,
        "trail_access_raw": trail_10 + 0.40 * trail_10_25,
    }


def _weighted_lower_rank(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    import numpy as np

    calibration = np.isfinite(values) & (weights > 0)
    if not calibration.any():
        raise HouseHunterError("Mountain percentile calibration has no populated valid blocks")
    ordered = np.argsort(values[calibration], kind="stable")
    sorted_values = values[calibration][ordered]
    sorted_weights = weights[calibration][ordered]
    unique, first = np.unique(sorted_values, return_index=True)
    cumulative = np.concatenate(([0.0], np.cumsum(sorted_weights)))
    below = cumulative[first]
    total = sorted_weights.sum()
    result = np.full(values.shape, np.nan)
    valid = np.isfinite(values)
    indexes = np.searchsorted(unique, values[valid], side="left")
    percentiles = np.full(indexes.shape, 100.0)
    within = indexes < len(unique)
    percentiles[within] = below[indexes[within]] * 100 / total
    result[valid] = percentiles
    return np.round(result, 2)


def _require_columns(frame: pl.DataFrame, columns: Iterable[str]) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise HouseHunterError(f"Mountain data is missing columns: {', '.join(missing)}")


def _validate_block_ranges(frame: pl.DataFrame, *, label: str) -> None:
    for column in RAW_PRECISION:
        invalid = pl.col(column).is_not_null() & (
            ~pl.col(column).is_finite() | (pl.col(column) < 0)
        )
        if column == "rugged_fraction_20km":
            invalid |= pl.col(column) > 1
        if frame.filter(invalid).height:
            raise HouseHunterError(f"Mountain {label} {column} is outside its valid range")
    for column in SCORE_COLUMNS:
        if frame.filter(
            pl.col(column).is_not_null()
            & (~pl.col(column).is_finite() | ~pl.col(column).is_between(0, 100, closed="both"))
        ).height:
            raise HouseHunterError(f"Mountain {label} {column} is outside 0..100")


def score_blocks(frame: pl.DataFrame, *, minimum_coverage: float = 0.995) -> pl.DataFrame:
    """Calibrate populated blocks nationally and attach v1 component/total scores."""
    _require_columns(
        frame,
        ["block_geoid", "tract_geoid", "county_fips", "state", "pop20", *RAW_PRECISION],
    )
    if frame["block_geoid"].n_unique() != frame.height:
        raise HouseHunterError("Mountain block GEOIDs are not unique")
    if frame.filter(~pl.col("block_geoid").str.contains(r"^\d{15}$")).height:
        raise HouseHunterError("Mountain data contains invalid block GEOIDs")
    if frame.filter(pl.col("pop20") < 0).height:
        raise HouseHunterError("Mountain data contains negative block population")
    for column in RAW_PRECISION:
        invalid = pl.col(column).is_not_null() & (
            ~pl.col(column).is_finite() | (pl.col(column) < 0)
        )
        if column == "rugged_fraction_20km":
            invalid |= pl.col(column) > 1
        if frame.filter(invalid).height:
            raise HouseHunterError(f"Mountain data contains invalid {column}")
    frame = frame.with_columns(
        *(
            pl.col(column).round(precision).alias(column)
            for column, precision in RAW_PRECISION.items()
        )
    )
    in_scope = frame["state"].is_in(IN_SCOPE_STATES).to_numpy()
    weights = frame["pop20"].to_numpy().astype(float) * in_scope
    expressions: list[pl.Series] = []
    for raw, percentile in RAW_COMPONENTS.items():
        values = frame[raw].to_numpy().astype(float)
        expressions.append(
            pl.Series(percentile, _weighted_lower_rank(values, weights), nan_to_null=True)
        )
    scored = (
        frame.with_columns(expressions)
        .with_columns(
            sum(pl.col(column) * weight for column, weight in SCORE_WEIGHTS.items())
            .round(2)
            .alias("mountain_score"),
            pl.lit(SCORE_VERSION).alias("mountain_score_version"),
            pl.lit(PIPELINE_VERSION).alias("mountain_pipeline_version"),
        )
        .with_columns(
            *(
                pl.when(pl.col("state").is_in(IN_SCOPE_STATES))
                .then(pl.col(column))
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .alias(column)
                for column in SCORE_COLUMNS
            )
        )
    )
    populated = scored.filter((pl.col("pop20") > 0) & pl.col("state").is_in(IN_SCOPE_STATES))
    groups = [("national", populated), *populated.partition_by("state", as_dict=True).items()]
    for state, group in groups:
        label = state[0] if isinstance(state, tuple) else state
        total = group["pop20"].sum()
        covered = group.filter(pl.col("mountain_score").is_not_null())["pop20"].sum()
        coverage = 0.0 if not total else covered / total
        if coverage + 1e-12 < minimum_coverage:
            raise HouseHunterError(
                f"{label} scored-population coverage is {coverage:.3%}; "
                f"minimum is {minimum_coverage:.3%}"
            )
    return scored.sort("block_geoid")


def _reconstruct_block_scores(frame: pl.DataFrame) -> pl.DataFrame:
    """Independent Polars oracle used only to validate persisted national scores."""
    reconstructed = frame.with_columns(
        *(
            pl.col(column).round(precision).alias(column)
            for column, precision in RAW_PRECISION.items()
        )
    )
    for raw, percentile in RAW_COMPONENTS.items():
        calibration_weight = (
            pl.when(
                pl.col("state").is_in(IN_SCOPE_STATES)
                & (pl.col("pop20") > 0)
                & pl.col(raw).is_not_null()
            )
            .then(pl.col("pop20"))
            .otherwise(0)
            .alias("_weight")
        )
        ranks = (
            reconstructed.select(raw, calibration_weight)
            .filter(pl.col(raw).is_not_null())
            .group_by(raw)
            .agg(pl.col("_weight").sum())
            .sort(raw)
        )
        total = ranks["_weight"].sum()
        if not total:
            raise HouseHunterError("Mountain validation has no populated valid calibrators")
        ranks = ranks.with_columns(
            ((pl.col("_weight").cum_sum() - pl.col("_weight")) * 100 / total)
            .round(2)
            .alias(percentile)
        ).select(raw, percentile)
        reconstructed = reconstructed.join(ranks, on=raw, how="left")
    return (
        reconstructed.with_columns(
            sum(pl.col(column) * weight for column, weight in SCORE_WEIGHTS.items())
            .round(2)
            .alias("mountain_score"),
            pl.lit(SCORE_VERSION).alias("mountain_score_version"),
            pl.lit(PIPELINE_VERSION).alias("mountain_pipeline_version"),
        )
        .with_columns(
            *(
                pl.when(pl.col("state").is_in(IN_SCOPE_STATES))
                .then(pl.col(column))
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .alias(column)
                for column in SCORE_COLUMNS
            )
        )
        .sort("block_geoid")
    )


def aggregate_scores(blocks: pl.DataFrame, geography: str) -> pl.DataFrame:
    """Produce population-weighted tract or county values from identical block scores."""
    if geography not in {"tract_geoid", "county_fips"}:
        raise HouseHunterError("Mountain aggregation geography must be tract_geoid or county_fips")
    _require_columns(blocks, [geography, "state", "pop20", *AGGREGATE_MEANS])
    prepared = blocks.with_columns(
        pl.when(pl.col("mountain_score").is_not_null())
        .then(pl.col("pop20"))
        .otherwise(0)
        .alias("covered_pop")
    )
    weighted = []
    for column in AGGREGATE_MEANS:
        weighted.extend(
            [
                (pl.col(column) * pl.col("pop20")).sum().alias(f"_{column}_weighted"),
                pl.when(pl.col(column).is_not_null())
                .then(pl.col("pop20"))
                .otherwise(0)
                .sum()
                .alias(f"_{column}_population"),
            ]
        )
    grouped = prepared.group_by(geography).agg(
        pl.col("state").first(),
        pl.col("pop20").sum().alias("population_2020"),
        pl.col("covered_pop").sum().alias("mountain_covered_population"),
        *weighted,
    )
    coverage = (
        pl.when(pl.col("population_2020") > 0)
        .then(pl.col("mountain_covered_population") / pl.col("population_2020"))
        .otherwise(0.0)
    )
    grouped = grouped.with_columns(coverage.round(6).alias("mountain_population_coverage"))
    status = (
        pl.when(~pl.col("state").is_in(IN_SCOPE_STATES))
        .then(pl.lit("outside_scope"))
        .when(pl.col("population_2020") == 0)
        .then(pl.lit("zero_population"))
        .when(pl.col("mountain_population_coverage") >= 1 - 1e-9)
        .then(pl.lit("complete"))
        .when(pl.col("mountain_population_coverage") >= 0.9)
        .then(pl.lit("partial"))
        .otherwise(pl.lit("insufficient_coverage"))
    )
    grouped = grouped.with_columns(status.alias("mountain_coverage_status"))
    means = [
        pl.when(
            pl.col("state").is_in(IN_SCOPE_STATES)
            & (pl.col("mountain_population_coverage") >= 0.9)
            & (pl.col("mountain_covered_population") > 0)
            & (pl.col(f"_{column}_population") > 0)
        )
        .then(pl.col(f"_{column}_weighted") / pl.col(f"_{column}_population"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .round(2 if column in SCORE_COLUMNS else RAW_PRECISION.get(column, 3))
        .alias(column)
        for column in AGGREGATE_MEANS
    ]
    return (
        grouped.with_columns(
            *means,
            pl.lit(SCORE_VERSION).alias("mountain_score_version"),
            pl.lit(PIPELINE_VERSION).alias("mountain_pipeline_version"),
        )
        .drop(
            [
                name
                for column in AGGREGATE_MEANS
                for name in (f"_{column}_weighted", f"_{column}_population")
            ]
        )
        .rename({geography: "place_id"})
        .sort("place_id")
    )


def _reconstruct_aggregate_scores(blocks: pl.DataFrame, geography: str) -> pl.DataFrame:
    """Independent aggregation oracle for persisted tract and county validation."""
    if geography not in {"tract_geoid", "county_fips"}:
        raise HouseHunterError("Mountain aggregation geography must be tract_geoid or county_fips")
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
    for column in AGGREGATE_MEANS:
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
                pl.when(
                    pl.col("state").is_in(IN_SCOPE_STATES)
                    & (pl.col("mountain_population_coverage") >= 0.9)
                    & (pl.col("mountain_covered_population") > 0)
                )
                .then(pl.col("_mean"))
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .round(2 if column in SCORE_COLUMNS else RAW_PRECISION.get(column, 3))
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


def national_block_geoid_sha256(blocks: pl.DataFrame) -> str:
    _require_columns(blocks, ["block_geoid"])
    geoids = blocks.select("block_geoid").sort("block_geoid")["block_geoid"].to_list()
    return sha256_bytes(("\n".join(geoids) + "\n").encode())


def validate_national_expectations(
    blocks: pl.DataFrame,
    expectations: dict[str, object],
    *,
    expected_block_geoid_sha256: str | None = None,
) -> None:
    """Require exact pinned Census block counts and population totals by state."""
    if set(expectations) != IN_SCOPE_STATES:
        missing = sorted(IN_SCOPE_STATES - set(expectations))
        extra = sorted(set(expectations) - IN_SCOPE_STATES)
        details = [f"missing {', '.join(missing)}" if missing else ""]
        details.append(f"unexpected {', '.join(extra)}" if extra else "")
        raise HouseHunterError(
            "Mountain Census expectations must cover the 50 states and DC: "
            + "; ".join(detail for detail in details if detail)
        )
    _require_columns(blocks, ["block_geoid", "state", "pop20"])
    if (
        not blocks.schema["pop20"].is_integer()
        or blocks.select(pl.col("pop20").is_null().any()).item()
        or blocks.select(pl.col("state").is_null().any()).item()
    ):
        raise HouseHunterError("Mountain Census block population/state types are invalid")
    state_lookup = pl.DataFrame(
        {"_state_fips": list(STATE_BY_FIPS), "_expected_state": list(STATE_BY_FIPS.values())}
    )
    invalid_state_rows = (
        blocks.select("block_geoid", "state")
        .with_columns(pl.col("block_geoid").str.slice(0, 2).alias("_state_fips"))
        .join(state_lookup, on="_state_fips", how="left")
        .filter(
            pl.col("_expected_state").is_null() | (pl.col("state") != pl.col("_expected_state"))
        )
    )
    if invalid_state_rows.height:
        raise HouseHunterError("Mountain Census block state does not match its GEOID")
    actual = {
        row["state"]: row
        for row in blocks.group_by("state")
        .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
        .iter_rows(named=True)
    }
    if set(actual) != IN_SCOPE_STATES:
        missing = sorted(IN_SCOPE_STATES - set(actual))
        extra = sorted(set(actual) - IN_SCOPE_STATES)
        details = [f"missing {', '.join(missing)}" if missing else ""]
        details.append(f"unexpected {', '.join(extra)}" if extra else "")
        raise HouseHunterError(
            "Mountain Census blocks must cover exactly the 50 states and DC: "
            + "; ".join(detail for detail in details if detail)
        )
    for state, expected in expectations.items():
        if not isinstance(expected, dict):
            raise HouseHunterError(f"Mountain Census expectation is invalid for {state}")
        try:
            expected_blocks = int(expected["blocks"])
            expected_population = int(expected["population"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError(f"Mountain Census expectation is invalid for {state}") from exc
        observed = actual.get(state, {"blocks": 0, "population": 0})
        if observed["blocks"] != expected_blocks or observed["population"] != expected_population:
            raise HouseHunterError(
                f"Mountain Census totals differ for {state}: "
                f"expected {expected_blocks:,} blocks/{expected_population:,} people, "
                f"found {observed['blocks']:,}/{observed['population']:,}"
            )
    if (
        expected_block_geoid_sha256 is not None
        and national_block_geoid_sha256(blocks) != expected_block_geoid_sha256
    ):
        raise HouseHunterError("Mountain national block GEOIDs differ from the reviewed lock")


def write_release(
    raw_blocks: pl.DataFrame,
    destination: Path,
    *,
    data_release: str,
    sources: dict[str, object],
    national_expectations: dict[str, object] | None = None,
    validate: bool = True,
) -> Path:
    """Write one deterministic, checksummed compact release plus local block detail."""
    scored = score_blocks(raw_blocks)
    if national_expectations is not None:
        validate_national_expectations(scored, national_expectations)
    tracts = aggregate_scores(scored, "tract_geoid")
    counties = aggregate_scores(scored, "county_fips")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    temporary.mkdir(parents=True)
    try:
        outputs = {
            "blocks": (temporary / "blocks.parquet", scored),
            "tracts": (temporary / "tracts.parquet", tracts),
            "counties": (temporary / "counties.parquet", counties),
        }
        for path, frame in outputs.values():
            frame.write_parquet(path, compression="zstd", statistics=True)
        (temporary / OWNERSHIP_MARKER).write_text("release-v1\n")
        manifest = {
            "schema_version": 1,
            "pipeline_version": PIPELINE_VERSION,
            "score_version": SCORE_VERSION,
            "data_release": data_release,
            "national_complete": national_expectations is not None,
            "national_expectations": national_expectations,
            "block_geoid_sha256": national_block_geoid_sha256(scored)
            if national_expectations is not None
            else None,
            "sources": sources,
            "files": {
                name: {
                    "filename": path.name,
                    "rows": frame.height,
                    "sha256": sha256_file(path),
                }
                for name, (path, frame) in outputs.items()
            },
        }
        manifest["release_id"] = sha256_bytes(canonical_json(manifest))[:16]
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        if validate:
            validate_release(temporary)
        if destination.exists():
            raise HouseHunterError(f"Mountain release already exists: {destination}")
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination


def _complete_release_expectations(manifest: dict[str, object]) -> dict[str, object]:
    expectations = manifest.get("national_expectations")
    if not isinstance(expectations, dict) or set(expectations) != IN_SCOPE_STATES:
        raise HouseHunterError("Complete Mountain release lacks national expectations")
    sources = manifest.get("sources")
    items = sources.get("items") if isinstance(sources, dict) else None
    if (
        not isinstance(sources, dict)
        or sources.get("source_lock_schema_version") != 2
        or not isinstance(sources.get("source_lock_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(sources.get("source_lock_sha256"))) is None
    ):
        raise HouseHunterError("Complete Mountain release lacks reviewed source-lock provenance")
    required_source_fields = {
        "name",
        "acquired_at",
        "crs",
        "schema",
        "count",
        "filename",
        "size",
        "sha256",
    }
    if not isinstance(items, list) or not items:
        raise HouseHunterError("Complete Mountain release lacks source provenance")
    for item in items:
        if not isinstance(item, dict) or not required_source_fields <= set(item):
            raise HouseHunterError("Complete Mountain release has invalid source provenance")
        digest = item.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise HouseHunterError("Complete Mountain release has invalid source checksum")
    return expectations


def validate_release(
    path: Path,
    *,
    maximum_compact_bytes: int = 50 * 1024 * 1024,
    maximum_release_bytes: int = FULL_RELEASE_MAX_BYTES,
) -> dict[str, object]:
    """Validate a Mountain release without trusting filenames or metadata."""
    if path.is_symlink() or (path / "manifest.json").is_symlink():
        raise HouseHunterError("Mountain release cannot use symlinked roots or files")
    try:
        manifest = json.loads((path / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain release manifest: {exc}") from exc
    if (
        manifest.get("schema_version") != 1
        or manifest.get("pipeline_version") != PIPELINE_VERSION
        or manifest.get("score_version") != SCORE_VERSION
        or not isinstance(manifest.get("national_complete"), bool)
    ):
        raise HouseHunterError("Mountain release version is incompatible")
    identity = {key: value for key, value in manifest.items() if key != "release_id"}
    expected_release_id = sha256_bytes(canonical_json(identity))[:16]
    if manifest.get("release_id") != expected_release_id:
        raise HouseHunterError("Mountain release identity does not match its manifest")
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or set(files) != {"blocks", "tracts", "counties"}
        or any(not isinstance(metadata, dict) for metadata in files.values())
    ):
        raise HouseHunterError("Mountain release manifest has an invalid file set")
    expected_entries = {
        "manifest.json",
        *(str(metadata.get("filename")) for metadata in files.values()),
    }
    actual_entries = {child.name for child in path.iterdir()}
    if (
        not expected_entries <= actual_entries
        or actual_entries - expected_entries - {OWNERSHIP_MARKER}
        or any(child.is_symlink() or not child.is_file() for child in path.iterdir())
    ):
        raise HouseHunterError("Mountain release contains unexpected or unsafe entries")
    frames: dict[str, pl.DataFrame] = {}
    for name, metadata in files.items():
        if not isinstance(metadata, dict) or Path(
            str(metadata.get("filename"))
        ).name != metadata.get("filename"):
            raise HouseHunterError(f"Mountain release has an invalid {name} filename")
        file_path = path / metadata["filename"]
        if file_path.is_symlink():
            raise HouseHunterError(f"Mountain {name} artifact cannot be a symlink")
        try:
            frame = pl.read_parquet(file_path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise HouseHunterError(f"Cannot read Mountain {name} artifact: {exc}") from exc
        if sha256_file(file_path) != metadata.get("sha256") or frame.height != metadata.get("rows"):
            raise HouseHunterError(f"Mountain {name} artifact does not match its manifest")
        frames[name] = frame
    for name in ("tracts", "counties"):
        frame = frames[name]
        _require_columns(
            frame,
            [
                "place_id",
                "mountain_score",
                "mountain_coverage_status",
                "mountain_population_coverage",
            ],
        )
        if frame["place_id"].n_unique() != frame.height:
            raise HouseHunterError(f"Mountain {name} identifiers are not unique")
        if frame.filter(
            pl.col("mountain_score").is_not_null()
            & ~pl.col("mountain_score").is_between(0, 100, closed="both")
        ).height:
            raise HouseHunterError(f"Mountain {name} scores are outside 0..100")
    blocks = frames["blocks"]
    _require_columns(
        blocks,
        [
            "block_geoid",
            "tract_geoid",
            "county_fips",
            "state",
            "pop20",
            *RAW_COMPONENTS,
            *SCORE_COLUMNS,
        ],
    )
    invalid_ids = blocks.filter(
        ~pl.col("block_geoid").str.contains(r"^\d{15}$")
        | (pl.col("tract_geoid") != pl.col("block_geoid").str.slice(0, 11))
        | (pl.col("county_fips") != pl.col("block_geoid").str.slice(0, 5))
    )
    if blocks["block_geoid"].n_unique() != blocks.height or invalid_ids.height:
        raise HouseHunterError("Mountain block identifiers are invalid or duplicated")
    if blocks.filter(pl.col("pop20") < 0).height:
        raise HouseHunterError("Mountain block population is negative")
    _validate_block_ranges(blocks, label="block")
    rounded_raw = blocks.select(
        pl.col(column).round(precision).alias(column) for column, precision in RAW_PRECISION.items()
    )
    if not blocks.select(*RAW_PRECISION).equals(rounded_raw):
        raise HouseHunterError("Mountain block raw metrics are not canonically rounded")
    raw_columns = [
        "block_geoid",
        "tract_geoid",
        "county_fips",
        "state",
        "pop20",
        *RAW_PRECISION,
    ]
    recomputed_blocks = _reconstruct_block_scores(blocks.select(raw_columns))
    score_columns = [
        "block_geoid",
        *SCORE_COLUMNS,
        "mountain_score_version",
        "mountain_pipeline_version",
    ]
    if not blocks.select(score_columns).equals(recomputed_blocks.select(score_columns)):
        raise HouseHunterError("Mountain block scores do not match rounded national raw metrics")
    expectations = manifest.get("national_expectations")
    if manifest["national_complete"]:
        expectations = _complete_release_expectations(manifest)
        digest = manifest.get("block_geoid_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise HouseHunterError("Complete Mountain release lacks its block GEOID digest")
        validate_national_expectations(blocks, expectations, expected_block_geoid_sha256=digest)
    elif expectations is not None:
        raise HouseHunterError("Partial Mountain release cannot claim national expectations")
    for name, geography in (("tracts", "tract_geoid"), ("counties", "county_fips")):
        expected = _reconstruct_aggregate_scores(blocks, geography)
        if frames[name].columns != expected.columns or not frames[name].equals(expected):
            raise HouseHunterError(f"Mountain {name} do not match block aggregation")
    compact_bytes = sum(
        (path / files[name]["filename"]).stat().st_size for name in ("tracts", "counties")
    )
    if compact_bytes > maximum_compact_bytes:
        raise HouseHunterError(
            f"Mountain compact artifact is {compact_bytes:,} bytes; "
            f"maximum is {maximum_compact_bytes:,}"
        )
    release_bytes = sum(
        child.lstat().st_blocks * 512 for child in path.rglob("*") if not child.is_symlink()
    )
    if release_bytes > maximum_release_bytes:
        raise HouseHunterError(
            f"Mountain release is {release_bytes:,} allocated bytes; "
            f"maximum is {maximum_release_bytes:,}"
        )
    return {**manifest, "compact_bytes": compact_bytes, "allocated_bytes": release_bytes}


def load_compact_release(
    path: Path,
) -> tuple[dict[str, object], pl.DataFrame, pl.DataFrame]:
    """Load only the promoted tract/county artifact needed by application builds."""
    if path.is_symlink() or (path / "manifest.json").is_symlink():
        raise HouseHunterError("Mountain runtime release cannot use symlinked roots or files")
    try:
        manifest = json.loads((path / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain release manifest: {exc}") from exc
    if (
        manifest.get("schema_version") != 1
        or manifest.get("pipeline_version") != PIPELINE_VERSION
        or manifest.get("score_version") != SCORE_VERSION
        or manifest.get("national_complete") is not True
    ):
        raise HouseHunterError("Mountain runtime release is incomplete or incompatible")
    _complete_release_expectations(manifest)
    identity = {key: value for key, value in manifest.items() if key != "release_id"}
    if manifest.get("release_id") != sha256_bytes(canonical_json(identity))[:16]:
        raise HouseHunterError("Mountain release identity does not match its manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"blocks", "tracts", "counties"}:
        raise HouseHunterError("Mountain release manifest has an invalid file set")
    frames = []
    for name in ("tracts", "counties"):
        metadata = files[name]
        if not isinstance(metadata, dict) or Path(
            str(metadata.get("filename"))
        ).name != metadata.get("filename"):
            raise HouseHunterError(f"Mountain release has an invalid {name} filename")
        file_path = path / str(metadata["filename"])
        if file_path.is_symlink():
            raise HouseHunterError(f"Mountain {name} artifact cannot be a symlink")
        try:
            frame = pl.read_parquet(file_path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise HouseHunterError(f"Cannot read Mountain {name} artifact: {exc}") from exc
        if sha256_file(file_path) != metadata.get("sha256") or frame.height != metadata.get("rows"):
            raise HouseHunterError(f"Mountain {name} artifact does not match its manifest")
        _require_columns(frame, ["place_id", *MOUNTAIN_RUNTIME_COLUMNS])
        if frame["place_id"].n_unique() != frame.height:
            raise HouseHunterError(f"Mountain {name} identifiers are not unique")
        frames.append(frame.select("place_id", *MOUNTAIN_RUNTIME_COLUMNS))
    return manifest, frames[0], frames[1]


def current_compact_release(
    paths: RuntimePaths,
    *,
    bundled_path: Path | None = None,
) -> tuple[Path, dict[str, object], pl.DataFrame, pl.DataFrame] | None:
    mountain_root = paths.data / "mountain"
    releases_root = mountain_root / "releases"
    if mountain_root.is_symlink() or releases_root.is_symlink():
        raise HouseHunterError("Mountain runtime path cannot contain symlinked managed roots")
    pointer = mountain_root / "current.json"
    if not pointer.is_file():
        managed_fallback = False
        if bundled_path is not None:
            bundled = bundled_path
        else:
            compact_root = mountain_root / "compact"
            compact_pointer = compact_root / "current.json"
            bundled = BUNDLED_COMPACT_RELEASE
            if compact_pointer.is_file():
                if compact_root.is_symlink() or compact_pointer.is_symlink():
                    raise HouseHunterError("Mountain compact fallback path cannot be a symlink")
                try:
                    compact_metadata = json.loads(compact_pointer.read_text())
                    compact_id = str(compact_metadata["release_id"])
                except (OSError, KeyError, json.JSONDecodeError) as exc:
                    raise HouseHunterError(
                        f"Mountain compact fallback pointer is invalid: {exc}"
                    ) from exc
                unresolved = compact_root / compact_id
                if (
                    compact_metadata.get("schema_version") != 1
                    or re.fullmatch(r"[0-9a-f]{16}", compact_id) is None
                    or unresolved.is_symlink()
                ):
                    raise HouseHunterError("Mountain compact fallback pointer is invalid")
                bundled = unresolved
                managed_fallback = True
        if not (bundled / "manifest.json").is_file():
            if managed_fallback:
                raise HouseHunterError("Mountain compact fallback pointer is invalid")
            return None
        if managed_fallback and (
            not (bundled / OWNERSHIP_MARKER).is_file() or (bundled / OWNERSHIP_MARKER).is_symlink()
        ):
            raise HouseHunterError("Mountain compact fallback is unowned")
        manifest, tracts, counties = load_compact_release(bundled)
        if managed_fallback and manifest["release_id"] != bundled.name:
            raise HouseHunterError("Mountain compact fallback pointer and release disagree")
        return bundled, manifest, tracts, counties
    if pointer.is_symlink():
        raise HouseHunterError("Current Mountain release pointer cannot be a symlink")
    try:
        metadata = json.loads(pointer.read_text())
        release_id = str(metadata["release_id"])
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Current Mountain release pointer is invalid: {exc}") from exc
    releases = releases_root.resolve()
    unresolved_release = releases / release_id
    if unresolved_release.is_symlink():
        raise HouseHunterError("Current Mountain release pointer is invalid")
    release = unresolved_release.resolve()
    if metadata.get("schema_version") != 1 or not release.is_relative_to(releases):
        raise HouseHunterError("Current Mountain release pointer is invalid")
    manifest, tracts, counties = load_compact_release(release)
    if manifest["release_id"] != release_id:
        raise HouseHunterError("Current Mountain pointer and release disagree")
    return release, manifest, tracts, counties


def _current_release_ids(root: Path) -> tuple[str | None, str | None]:
    pointer = root / "current.json"
    if not pointer.is_file():
        return None, None
    if pointer.is_symlink():
        raise HouseHunterError("Current Mountain release pointer cannot be a symlink")
    try:
        payload = json.loads(pointer.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None

    def release_id(key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value) else None

    return release_id("release_id"), release_id("rollback_release_id")


def _publish_pointer(root: Path, release_id: str, rollback_release_id: str | None) -> None:
    payload: dict[str, object] = {"schema_version": 1, "release_id": release_id}
    if rollback_release_id and rollback_release_id != release_id:
        payload["rollback_release_id"] = rollback_release_id
    temporary_pointer = root / f".current.{uuid.uuid4().hex}.tmp"
    temporary_pointer.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary_pointer, root / "current.json")


def _promote_validated_release(
    paths: RuntimePaths,
    candidate: Path,
    manifest: dict[str, object],
    *,
    move_candidate: bool,
) -> Path:
    if manifest["national_complete"] is not True:
        raise HouseHunterError("Partial Mountain releases cannot be promoted")
    root = paths.data / "mountain"
    releases = root / "releases"
    releases = ensure_safe_directory(releases)
    release_id = str(manifest["release_id"])
    target = lexical_path(releases / release_id)
    previous, previous_rollback = _current_release_ids(root)
    if target.exists():
        require_owned_child(target, releases, name_pattern=r"[0-9a-f]{16}")
        validate_release(target)
        if move_candidate and candidate != target:
            remove_owned_child(candidate, releases, name_pattern=r"\.[0-9a-f]{32}\.tmp")
    elif move_candidate:
        require_owned_child(candidate, releases, name_pattern=r"\.[0-9a-f]{32}\.tmp")
        os.replace(candidate, target)
    else:
        from .mountain_pack import allocated_size, ensure_storage_budget

        ensure_storage_budget(root, reserve_bytes=allocated_size(candidate))
        temporary = releases / f".{release_id}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copytree(candidate, temporary)
            (temporary / OWNERSHIP_MARKER).write_text("release-v1\n")
            validate_release(temporary)
            os.replace(temporary, target)
        except BaseException:
            if (
                temporary.is_dir()
                and not temporary.is_symlink()
                and (temporary / OWNERSHIP_MARKER).is_file()
                and not (temporary / OWNERSHIP_MARKER).is_symlink()
            ):
                remove_owned_child(
                    temporary,
                    releases,
                    name_pattern=r"\.[0-9a-f]{16}\.[0-9a-f]{32}\.tmp",
                )
            raise
    rollback = previous_rollback if previous == release_id else previous
    _publish_pointer(root, release_id, rollback)
    return target


def promote_release(
    paths: RuntimePaths,
    candidate: Path,
    *,
    reviewed_source_lock: dict[str, object],
    reviewed_source_lock_sha256: str,
    expected_raw_blocks: pl.DataFrame,
) -> Path:
    """Defensively validate and copy an externally supplied candidate before promotion."""
    from .mountain_gis import source_provenance_item

    manifest = validate_release(candidate)
    sources = manifest.get("sources")
    expected_items = [
        source_provenance_item(item)
        for item in reviewed_source_lock.get("sources", [])
        if isinstance(item, dict)
    ]
    if (
        not isinstance(sources, dict)
        or sources.get("source_lock_schema_version") != 2
        or sources.get("source_lock_sha256") != reviewed_source_lock_sha256
        or sources.get("items") != expected_items
    ):
        raise HouseHunterError(
            "Mountain candidate does not match the independently reviewed source lock"
        )
    if manifest.get("block_geoid_sha256") != reviewed_source_lock.get(
        "block_geoid_sha256"
    ) or manifest.get("national_expectations") != reviewed_source_lock.get("expected_states"):
        raise HouseHunterError("Mountain candidate differs from the reviewed national inventory")
    expected = score_blocks(expected_raw_blocks).sort("block_geoid")
    blocks_file = candidate / manifest["files"]["blocks"]["filename"]
    actual = pl.read_parquet(blocks_file).sort("block_geoid")
    if actual.columns != expected.columns or not actual.equals(expected):
        raise HouseHunterError("Mountain candidate differs from prepared-pack raw metrics")
    return _promote_validated_release(paths, candidate, manifest, move_candidate=False)


def write_and_promote_release(
    paths: RuntimePaths,
    raw_blocks: pl.DataFrame,
    *,
    data_release: str,
    sources: dict[str, object],
    national_expectations: dict[str, object],
    timings: dict[str, float] | None = None,
) -> tuple[Path, dict[str, object]]:
    """Write, definitively validate once, and atomically promote a generated release."""
    from .mountain_pack import ensure_storage_budget

    ensure_storage_budget(
        paths.data / "mountain", reserve_bytes=FULL_RELEASE_MAX_BYTES
    )
    releases = paths.data / "mountain" / "releases"
    releases = ensure_safe_directory(releases)
    candidate = releases / f".{uuid.uuid4().hex}.tmp"
    try:
        candidate_started = time.monotonic()
        write_release(
            raw_blocks,
            candidate,
            data_release=data_release,
            sources=sources,
            national_expectations=national_expectations,
            validate=False,
        )
        if timings is not None:
            timings["scoring_aggregation_write_seconds"] = round(
                time.monotonic() - candidate_started, 3
            )
        validation_started = time.monotonic()
        manifest = validate_release(candidate)
        if timings is not None:
            timings["independent_validation_seconds"] = round(
                time.monotonic() - validation_started, 3
            )
        promotion_started = time.monotonic()
        target = _promote_validated_release(paths, candidate, manifest, move_candidate=True)
        if timings is not None:
            timings["promotion_seconds"] = round(time.monotonic() - promotion_started, 3)
        return target, manifest
    except BaseException:
        if (
            candidate.exists()
            and not candidate.is_symlink()
            and (candidate / OWNERSHIP_MARKER).is_file()
            and not (candidate / OWNERSHIP_MARKER).is_symlink()
        ):
            remove_owned_child(candidate, releases, name_pattern=r"\.[0-9a-f]{32}\.tmp")
        raise


def restore_release_pointer(paths: RuntimePaths, pointer: bytes | None) -> None:
    """Restore the exact prior pointer after a downstream snapshot failure."""
    root = paths.data / "mountain"
    current = root / "current.json"
    if pointer is None:
        current.unlink(missing_ok=True)
        return
    temporary = root / f".current.{uuid.uuid4().hex}.tmp"
    temporary.write_bytes(pointer)
    os.replace(temporary, current)


def prune_owned_releases(paths: RuntimePaths) -> list[str]:
    """Retain the active and rollback full releases; remove only marked older releases."""
    root = paths.data / "mountain"
    releases = root / "releases"
    if not releases.is_dir():
        return []
    releases = ensure_safe_directory(releases)
    try:
        pointer_path = root / "current.json"
        if pointer_path.is_symlink():
            raise HouseHunterError("Cannot prune through a symlinked Mountain pointer")
        pointer = json.loads(pointer_path.read_text())
    except HouseHunterError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(
            f"Cannot prune Mountain releases with an invalid pointer: {exc}"
        ) from exc
    protected = {pointer.get("release_id"), pointer.get("rollback_release_id")}
    removed: list[str] = []
    for child in releases.iterdir():
        if (
            child.name in protected
            or re.fullmatch(r"[0-9a-f]{16}", child.name) is None
            or child.is_symlink()
            or not child.is_dir()
            or not (child / OWNERSHIP_MARKER).is_file()
            or (child / OWNERSHIP_MARKER).is_symlink()
        ):
            continue
        remove_owned_child(child, releases, name_pattern=r"[0-9a-f]{16}")
        removed.append(child.name)
    return sorted(removed)


def write_and_promote_compact_fallback(
    paths: RuntimePaths,
    release: Path,
    manifest: dict[str, object],
) -> Path:
    """Atomically publish the compact managed fallback for a validated release."""
    runtime_manifest, _, _ = load_compact_release(release)
    release_id = str(manifest.get("release_id", ""))
    if runtime_manifest.get("release_id") != release_id:
        raise HouseHunterError("Mountain compact fallback source was not definitively validated")
    files = runtime_manifest["files"]
    compact_bytes = sum(
        (release / files[name]["filename"]).stat().st_size for name in ("tracts", "counties")
    )
    if compact_bytes > COMPACT_RELEASE_MAX_BYTES:
        raise HouseHunterError(
            f"Mountain compact artifact is {compact_bytes:,} bytes; "
            f"maximum is {COMPACT_RELEASE_MAX_BYTES:,}"
        )
    root = ensure_safe_directory(paths.data / "mountain" / "compact")
    target = lexical_path(root / release_id)
    if target.exists():
        require_owned_child(target, root, name_pattern=r"[0-9a-f]{16}")
        existing, _, _ = load_compact_release(target)
        if existing.get("release_id") != release_id:
            raise HouseHunterError("Mountain compact fallback identity collision")
    else:
        from .mountain_pack import ensure_storage_budget

        reserve_bytes = sum(
            (release / filename).stat().st_blocks * 512
            for filename in (
                "manifest.json",
                str(files["tracts"]["filename"]),
                str(files["counties"]["filename"]),
            )
        )
        ensure_storage_budget(paths.data / "mountain", reserve_bytes=reserve_bytes)
        temporary = ensure_owned_child(
            root / f".{release_id}.{uuid.uuid4().hex}.tmp",
            root,
            name_pattern=r"\.[0-9a-f]{16}\.[0-9a-f]{32}\.tmp",
            marker_value="compact-release-v1\n",
        )
        try:
            shutil.copy2(release / "manifest.json", temporary / "manifest.json")
            for name in ("tracts", "counties"):
                filename = str(files[name]["filename"])
                shutil.copy2(release / filename, temporary / filename)
            copied, _, _ = load_compact_release(temporary)
            if copied.get("release_id") != release_id:
                raise HouseHunterError("Mountain compact fallback copy differs from its release")
            if _allocated_tree_bytes(temporary) > COMPACT_RELEASE_MAX_BYTES:
                raise HouseHunterError("Mountain compact fallback exceeds its 50 MiB budget")
            os.replace(temporary, target)
        except BaseException:
            if temporary.exists():
                remove_owned_child(
                    temporary,
                    root,
                    name_pattern=r"\.[0-9a-f]{16}\.[0-9a-f]{32}\.tmp",
                )
            raise
    pointer = root / "current.json"
    temporary_pointer = root / f".current.{uuid.uuid4().hex}.tmp"
    temporary_pointer.write_text(
        json.dumps({"schema_version": 1, "release_id": release_id}, indent=2, sort_keys=True) + "\n"
    )
    os.replace(temporary_pointer, pointer)
    return target


def restore_compact_pointer(paths: RuntimePaths, pointer: bytes | None) -> None:
    """Restore the prior compact-fallback pointer during publication rollback."""
    root = ensure_safe_directory(paths.data / "mountain" / "compact")
    current = root / "current.json"
    if pointer is None:
        current.unlink(missing_ok=True)
        return
    temporary = root / f".current.{uuid.uuid4().hex}.tmp"
    temporary.write_bytes(pointer)
    os.replace(temporary, current)


def prune_owned_compact_fallbacks(paths: RuntimePaths) -> list[str]:
    """Retain only the active compact fallback and remove marked older copies."""
    root = paths.data / "mountain" / "compact"
    if not root.is_dir():
        return []
    root = ensure_safe_directory(root)
    pointer = root / "current.json"
    if pointer.is_symlink():
        raise HouseHunterError("Cannot prune through a symlinked compact fallback pointer")
    try:
        metadata = json.loads(pointer.read_text())
        protected = str(metadata["release_id"])
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise HouseHunterError(
            f"Cannot prune compact fallbacks with an invalid pointer: {exc}"
        ) from exc
    if metadata.get("schema_version") != 1 or re.fullmatch(r"[0-9a-f]{16}", protected) is None:
        raise HouseHunterError("Cannot prune compact fallbacks with an invalid pointer")
    removed: list[str] = []
    for child in root.iterdir():
        if child.name == protected or re.fullmatch(r"[0-9a-f]{16}", child.name) is None:
            continue
        if (
            child.is_symlink()
            or not child.is_dir()
            or not (child / OWNERSHIP_MARKER).is_file()
            or (child / OWNERSHIP_MARKER).is_symlink()
        ):
            continue
        remove_owned_child(child, root, name_pattern=r"[0-9a-f]{16}")
        removed.append(child.name)
    return sorted(removed)


def write_compact_bundle(release: Path, destination: Path) -> Path:
    """Write an atomic tract/county-only runtime fallback from a validated release."""
    manifest = validate_release(release)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    if destination.exists():
        raise HouseHunterError(f"Mountain compact bundle already exists: {destination}")
    temporary.mkdir(parents=True)
    try:
        shutil.copy2(release / "manifest.json", temporary / "manifest.json")
        files = manifest["files"]
        for name in ("tracts", "counties"):
            shutil.copy2(release / files[name]["filename"], temporary / files[name]["filename"])
        load_compact_release(temporary)
        if _allocated_tree_bytes(temporary) > COMPACT_RELEASE_MAX_BYTES:
            raise HouseHunterError("Mountain compact bundle exceeds its 50 MiB budget")
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return destination
