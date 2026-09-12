from __future__ import annotations

import json
import math
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    import numpy as np

from .config import RuntimePaths, canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS

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


def validate_national_expectations(blocks: pl.DataFrame, expectations: dict[str, object]) -> None:
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
    _require_columns(blocks, ["state", "pop20"])
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


def write_release(
    raw_blocks: pl.DataFrame,
    destination: Path,
    *,
    data_release: str,
    sources: dict[str, object],
    national_expectations: dict[str, object] | None = None,
) -> Path:
    """Write one deterministic, checksummed compact release plus local block detail."""
    scored = score_blocks(raw_blocks)
    if national_expectations is not None:
        validate_national_expectations(scored, national_expectations)
    tracts = aggregate_scores(scored, "tract_geoid")
    counties = aggregate_scores(scored, "county_fips")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        outputs = {
            "blocks": (temporary / "blocks.parquet", scored),
            "tracts": (temporary / "tracts.parquet", tracts),
            "counties": (temporary / "counties.parquet", counties),
        }
        for path, frame in outputs.values():
            frame.write_parquet(path, compression="zstd", statistics=True)
        manifest = {
            "schema_version": 1,
            "pipeline_version": PIPELINE_VERSION,
            "score_version": SCORE_VERSION,
            "data_release": data_release,
            "national_complete": national_expectations is not None,
            "national_expectations": national_expectations,
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
    required_source_fields = {
        "name",
        "url",
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
    path: Path, *, maximum_compact_bytes: int = 50 * 1024 * 1024
) -> dict[str, object]:
    """Validate a Mountain release without trusting filenames or metadata."""
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
    if not isinstance(files, dict) or set(files) != {"blocks", "tracts", "counties"}:
        raise HouseHunterError("Mountain release manifest has an invalid file set")
    frames: dict[str, pl.DataFrame] = {}
    for name, metadata in files.items():
        if not isinstance(metadata, dict) or Path(
            str(metadata.get("filename"))
        ).name != metadata.get("filename"):
            raise HouseHunterError(f"Mountain release has an invalid {name} filename")
        file_path = path / metadata["filename"]
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
    expectations = manifest.get("national_expectations")
    if manifest["national_complete"]:
        expectations = _complete_release_expectations(manifest)
        validate_national_expectations(blocks, expectations)
    elif expectations is not None:
        raise HouseHunterError("Partial Mountain release cannot claim national expectations")
    for name, geography in (("tracts", "tract_geoid"), ("counties", "county_fips")):
        expected = aggregate_scores(blocks, geography)
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
    return {**manifest, "compact_bytes": compact_bytes}


def load_compact_release(
    path: Path,
) -> tuple[dict[str, object], pl.DataFrame, pl.DataFrame]:
    """Load only the promoted tract/county artifact needed by application builds."""
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
) -> tuple[Path, dict[str, object], pl.DataFrame, pl.DataFrame] | None:
    pointer = paths.data / "mountain" / "current.json"
    if not pointer.is_file():
        return None
    try:
        metadata = json.loads(pointer.read_text())
        release_id = str(metadata["release_id"])
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Current Mountain release pointer is invalid: {exc}") from exc
    releases = (paths.data / "mountain" / "releases").resolve()
    release = (releases / release_id).resolve()
    if metadata.get("schema_version") != 1 or not release.is_relative_to(releases):
        raise HouseHunterError("Current Mountain release pointer is invalid")
    manifest, tracts, counties = load_compact_release(release)
    if manifest["release_id"] != release_id:
        raise HouseHunterError("Current Mountain pointer and release disagree")
    return release, manifest, tracts, counties


def promote_release(paths: RuntimePaths, candidate: Path) -> Path:
    """Atomically promote a validated local release without replacing a good pointer early."""
    manifest = validate_release(candidate)
    if manifest["national_complete"] is not True:
        raise HouseHunterError("Partial Mountain releases cannot be promoted")
    root = paths.data / "mountain"
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release_id = str(manifest["release_id"])
    target = (releases / release_id).resolve()
    if not target.is_relative_to(releases.resolve()):
        raise HouseHunterError("Mountain release path escapes the release directory")
    if target.exists():
        validate_release(target)
    else:
        temporary = releases / f".{release_id}.{os.getpid()}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        try:
            shutil.copytree(candidate, temporary)
            validate_release(temporary)
            os.replace(temporary, target)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
    pointer = root / "current.json"
    temporary_pointer = root / ".current.json.tmp"
    temporary_pointer.write_text(
        json.dumps({"schema_version": 1, "release_id": release_id}, indent=2, sort_keys=True) + "\n"
    )
    os.replace(temporary_pointer, pointer)
    return target
