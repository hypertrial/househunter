from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from .config import RuntimePaths, canonical_json, load_config, sha256_bytes
from .contracts import COUNTY_METHODOLOGY_NOTICE, METHODOLOGY_NOTICE
from .download import validate_cached_fema, validate_cached_fema_counties
from .errors import HouseHunterError
from .geography import (
    KNOWN_STATES,
    STATE_BY_FIPS,
    UNKNOWN_COUNTY_FIPS,
    UNKNOWN_COUNTY_NAME,
    UNKNOWN_STATE,
)

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]

BUILD_SCHEMA_VERSION = 4


def logical_checksum(frame: pl.DataFrame, columns: list[str], sort_by: list[str]) -> str:
    rows = frame.select(columns).sort(sort_by).iter_rows()
    return sha256_bytes(canonical_json([list(row) for row in rows]))


def _cancelled(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise InterruptedError("Build cancelled")


def _county_display_expr() -> pl.Expr:
    name = pl.col("county").str.strip_chars().fill_null("")
    kind = pl.col("county_type").str.strip_chars().fill_null("")
    generic = (kind == "") | (kind.str.to_lowercase() == "county")
    return (
        pl.when(name == "")
        .then(pl.lit(UNKNOWN_COUNTY_NAME))
        .when(generic)
        .then(name)
        .otherwise(pl.concat_str([name, pl.lit(" "), kind]))
    )


def compute_scores(
    fema: pl.DataFrame,
    counties: pl.DataFrame | None = None,
    *,
    fema_vintage: str = "December 2025",
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    if counties is None:
        counties = pl.DataFrame(
            {
                "county_fips": [],
                "county": [],
                "county_type": [],
                "state": [],
                "alr_npctl": [],
                "nri_version": [],
            },
            schema={
                "county_fips": pl.String,
                "county": pl.String,
                "county_type": pl.String,
                "state": pl.String,
                "alr_npctl": pl.Float64,
                "nri_version": pl.String,
            },
        )
    county_complete = pl.col("alr_npctl").is_not_null()
    stripped_state = pl.col("state").str.strip_chars()
    county_state = (
        pl.when(stripped_state != "")
        .then(stripped_state.str.to_uppercase())
        .otherwise(
            pl.col("county_fips")
            .str.slice(0, 2)
            .replace_strict(STATE_BY_FIPS, default=UNKNOWN_STATE)
        )
    )
    county_scored = counties.select(
        pl.col("county_fips").alias("place_id"),
        _county_display_expr().alias("name"),
        county_state.alias("state"),
        pl.lit("county").alias("place_type"),
        pl.lit(0, dtype=pl.Int64).alias("population_2020"),
        pl.lit(0, dtype=pl.Int64).alias("housing_units_2020"),
        pl.when(county_complete)
        .then(pl.col("alr_npctl"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("risk_score"),
        pl.when(county_complete).then(pl.lit("complete")).otherwise(pl.lit("missing_fema")).alias(
            "coverage_status"
        ),
        pl.when(county_complete).then(pl.lit(1.0)).otherwise(pl.lit(0.0)).alias("coverage_ratio"),
        pl.lit(0, dtype=pl.Int64).alias("total_weighted_housing"),
        pl.lit(fema_vintage).alias("fema_vintage"),
        pl.lit("n/a").alias("census_vintage"),
        pl.col("county_fips"),
        _county_display_expr().alias("county_name"),
    ).sort("place_id")
    lookup = county_scored.select(
        pl.col("place_id").alias("matched_county_fips"),
        pl.col("name").alias("matched_county_name"),
    )
    complete = pl.col("alr_npctl").is_not_null()
    tract_state = (
        pl.col("tract_id").str.slice(0, 2).replace_strict(STATE_BY_FIPS, default=UNKNOWN_STATE)
    )
    scored = (
        fema.with_columns(pl.col("tract_id").str.slice(0, 5).alias("prefix"))
        .join(lookup, left_on="prefix", right_on="matched_county_fips", how="left")
        .select(
            pl.col("tract_id").alias("place_id"),
            pl.col("tract_id").alias("name"),
            tract_state.alias("state"),
            pl.lit("tract").alias("place_type"),
            pl.lit(0, dtype=pl.Int64).alias("population_2020"),
            pl.lit(0, dtype=pl.Int64).alias("housing_units_2020"),
            pl.when(complete)
            .then(pl.col("alr_npctl"))
            .otherwise(pl.lit(None, dtype=pl.Float64))
            .alias("risk_score"),
            pl.when(complete).then(pl.lit("complete")).otherwise(pl.lit("missing_fema")).alias(
                "coverage_status"
            ),
            pl.when(complete).then(pl.lit(1.0)).otherwise(pl.lit(0.0)).alias("coverage_ratio"),
            pl.lit(0, dtype=pl.Int64).alias("total_weighted_housing"),
            pl.lit(fema_vintage).alias("fema_vintage"),
            pl.lit("n/a").alias("census_vintage"),
            pl.when(pl.col("matched_county_name").is_null())
            .then(pl.lit(UNKNOWN_COUNTY_FIPS))
            .otherwise(pl.col("prefix"))
            .alias("county_fips"),
            pl.when(pl.col("matched_county_name").is_null())
            .then(pl.lit(UNKNOWN_COUNTY_NAME))
            .otherwise(pl.col("matched_county_name"))
            .alias("county_name"),
        )
        .sort("place_id")
    )
    contributions = fema.select(
        pl.col("tract_id").alias("place_id"),
        pl.col("tract_id"),
        pl.lit(0, dtype=pl.Int64).alias("housing_units"),
        pl.lit(1.0).alias("housing_weight"),
        pl.col("alr_npctl"),
        pl.when(complete).then(pl.col("alr_npctl")).otherwise(pl.lit(None, dtype=pl.Float64)).alias(
            "weighted_contribution"
        ),
    ).sort(["place_id", "tract_id"])
    return scored, contributions, county_scored


def _write_duckdb(
    path: Path,
    places: Path,
    contributions: Path,
    counties: Path,
    metadata: dict[str, object],
) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute("CREATE TABLE places AS SELECT * FROM read_parquet(?)", [str(places)])
        connection.execute(
            "CREATE TABLE tract_contributions AS SELECT * FROM read_parquet(?)",
            [str(contributions)],
        )
        connection.execute(
            "CREATE TABLE counties AS SELECT * FROM read_parquet(?)", [str(counties)]
        )
        connection.execute("CREATE INDEX places_id_idx ON places(place_id)")
        connection.execute("CREATE INDEX places_state_idx ON places(state)")
        connection.execute("CREATE INDEX places_county_idx ON places(county_fips)")
        connection.execute("CREATE INDEX contribution_place_idx ON tract_contributions(place_id)")
        connection.execute("CREATE INDEX counties_id_idx ON counties(place_id)")
        connection.execute("CREATE INDEX counties_state_idx ON counties(state)")
        connection.execute(
            "CREATE TABLE build_metadata AS SELECT ? AS metadata_json",
            [json.dumps(metadata, sort_keys=True)],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def _existing_build_is_valid(target: Path, build_id: str, input_hashes: dict[str, str]) -> bool:
    try:
        metadata = json.loads((target / "build.json").read_text())
        places = pl.read_parquet(target / "places.parquet")
        contributions = pl.read_parquet(target / "tract_contributions.parquet")
        counties = pl.read_parquet(target / "counties.parquet")
    except (OSError, json.JSONDecodeError, pl.exceptions.PolarsError):
        return False
    if not (target / "househunter.duckdb").is_file():
        return False
    if (
        metadata.get("schema_version") != BUILD_SCHEMA_VERSION
        or metadata.get("build_id") != build_id
        or metadata.get("input_checksums") != input_hashes
    ):
        return False
    if metadata.get("place_count") != places.height:
        return False
    if metadata.get("county_count") != counties.height:
        return False
    if (
        metadata.get("ranked_place_count")
        != places.filter(pl.col("coverage_status") == "complete").height
    ):
        return False
    if (
        metadata.get("ranked_county_count")
        != counties.filter(pl.col("coverage_status") == "complete").height
    ):
        return False
    checksums = metadata.get("logical_checksums", {})
    if checksums != {
        "places": logical_checksum(places, places.columns, ["place_id"]),
        "tract_contributions": logical_checksum(
            contributions, contributions.columns, ["place_id", "tract_id"]
        ),
        "counties": logical_checksum(counties, counties.columns, ["place_id"]),
    }:
        return False
    try:
        connection = duckdb.connect(str(target / "househunter.duckdb"), read_only=True)
        try:
            place_count = connection.execute("SELECT count(*) FROM places").fetchone()[0]
            contribution_count = connection.execute(
                "SELECT count(*) FROM tract_contributions"
            ).fetchone()[0]
            county_count = connection.execute("SELECT count(*) FROM counties").fetchone()[0]
            stored_metadata = json.loads(
                connection.execute("SELECT metadata_json FROM build_metadata").fetchone()[0]
            )
        finally:
            connection.close()
    except (duckdb.Error, json.JSONDecodeError, IndexError, TypeError):
        return False
    return (
        place_count == places.height
        and contribution_count == contributions.height
        and county_count == counties.height
        and stored_metadata == metadata
    )


def build_snapshot(
    paths: RuntimePaths,
    *,
    state: str | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> Path:
    paths.ensure()
    state = state.upper() if state else None
    if state and state not in KNOWN_STATES:
        raise HouseHunterError(f"Unknown state abbreviation: {state}")
    _cancelled(cancelled)
    fema_path = paths.cache / "fema_nri_tracts.parquet"
    county_path = paths.cache / "fema_nri_counties.parquet"
    if not fema_path.is_file():
        raise HouseHunterError("FEMA data is not cached; run `househunter download --source fema`")
    if not county_path.is_file():
        raise HouseHunterError(
            "FEMA county data is not cached; run `househunter download --source fema_counties`"
        )
    source = load_config()["fema"]
    county_source = load_config()["fema_counties"]
    fema, fema_sha = validate_cached_fema(fema_path, source)
    counties, county_sha = validate_cached_fema_counties(county_path, county_source)
    input_hashes = {"fema": fema_sha, "fema_counties": county_sha}
    scope = state or "national"
    build_key = sha256_bytes(canonical_json({"scope": scope, "inputs": input_hashes}))[:16]
    build_id = f"{scope.lower()}-{build_key}"
    target = paths.builds / build_id
    if target.is_dir():
        if not _existing_build_is_valid(target, build_id, input_hashes):
            raise HouseHunterError(
                f"Existing immutable build failed validation: {target}; move it aside and rebuild"
            )
        _publish_current(paths, target, build_id, scope)
        if progress:
            progress(100, "Using verified existing build")
        return target
    if progress:
        progress(25, "Ranking FEMA geographies")
    scored, contributions, county_scored = compute_scores(
        fema, counties, fema_vintage=source["version"]
    )
    if state:
        scored = scored.filter(pl.col("state") == state)
        contributions = contributions.join(scored.select("place_id"), on="place_id", how="semi")
        county_scored = county_scored.filter(pl.col("state") == state)
        if scored.height == 0:
            raise HouseHunterError(f"Unknown state abbreviation: {state}")
    _cancelled(cancelled)
    complete = scored.filter(pl.col("coverage_status") == "complete")
    if complete.filter(pl.col("coverage_ratio") != 1.0).height:
        raise HouseHunterError(
            "Internal validation failed: ranked tracts do not have full coverage"
        )
    county_complete = county_scored.filter(pl.col("coverage_status") == "complete")
    if county_complete.filter(pl.col("coverage_ratio") != 1.0).height:
        raise HouseHunterError(
            "Internal validation failed: ranked counties do not have full coverage"
        )
    place_columns = scored.columns
    contribution_columns = contributions.columns
    county_columns = county_scored.columns
    checksums = {
        "places": logical_checksum(scored, place_columns, ["place_id"]),
        "tract_contributions": logical_checksum(
            contributions, contribution_columns, ["place_id", "tract_id"]
        ),
        "counties": logical_checksum(county_scored, county_columns, ["place_id"]),
    }
    metadata: dict[str, object] = {
        "schema_version": BUILD_SCHEMA_VERSION,
        "build_id": build_id,
        "scope": {"kind": "state" if state else "national", "state": state},
        "created_at": datetime.now(UTC).isoformat(),
        "place_count": scored.height,
        "ranked_place_count": complete.height,
        "county_count": county_scored.height,
        "ranked_county_count": county_complete.height,
        "source_vintages": {
            "fema": source["version"],
            "fema_release": source["release"],
            "fema_counties": county_source["version"],
            "fema_counties_release": county_source["release"],
        },
        "input_checksums": input_hashes,
        "logical_checksums": checksums,
        "methodology_notice": METHODOLOGY_NOTICE,
        "county_methodology_notice": COUNTY_METHODOLOGY_NOTICE,
    }
    temporary = paths.builds / f".{build_id}.{os.getpid()}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        places_output = temporary / "places.parquet"
        contributions_output = temporary / "tract_contributions.parquet"
        counties_output = temporary / "counties.parquet"
        scored.write_parquet(places_output, compression="zstd", statistics=True)
        contributions.write_parquet(contributions_output, compression="zstd", statistics=True)
        county_scored.write_parquet(counties_output, compression="zstd", statistics=True)
        (temporary / "build.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        if progress:
            progress(75, "Creating read-only query snapshot")
        _write_duckdb(
            temporary / "househunter.duckdb",
            places_output,
            contributions_output,
            counties_output,
            metadata,
        )
        _cancelled(cancelled)
        os.replace(temporary, target)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    _publish_current(paths, target, build_id, scope)
    if progress:
        progress(100, "Build published")
    return target


def _publish_current(paths: RuntimePaths, target: Path, build_id: str, scope: str) -> None:
    pointer = {
        "schema_version": BUILD_SCHEMA_VERSION,
        "build_id": build_id,
        "scope": scope,
        "path": str(target),
    }
    temporary = paths.current.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(pointer, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, paths.current)
