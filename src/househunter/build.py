from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import duckdb
import polars as pl

from .chrr import build_processed
from .config import RuntimePaths, canonical_json, load_config, sha256_bytes, sha256_file
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
from .hazards import HAZARD_COLUMNS, with_hazard_columns

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]

BUILD_SCHEMA_VERSION = 8
MOUNTAIN_RUNTIME_GEOGRAPHY_VERSION = "mountain_runtime_geography_v1"
_CONNECTICUT_UNMATCHED_ZERO_POPULATION_TRACTS = frozenset(
    {"09001990000", "09007990100", "09009990000", "09011990100"}
)
_SNAPSHOT_FILES = (
    "build.json",
    "places.parquet",
    "tract_contributions.parquet",
    "counties.parquet",
    "chrr_county.parquet",
    "househunter.duckdb",
)


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


def _attach_mountain_scores(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    paths: RuntimePaths,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, str]]:
    from .mountain import AGGREGATE_MEANS, IN_SCOPE_STATES, current_compact_release

    def unavailable_status() -> pl.Expr:
        return (
            pl.when(pl.col("state").is_in(sorted(IN_SCOPE_STATES)))
            .then(pl.lit("unavailable"))
            .otherwise(pl.lit("outside_scope"))
            .alias("mountain_coverage_status")
        )

    current = current_compact_release(paths)
    if current is None:
        numeric = [pl.lit(None, dtype=pl.Float64).alias(column) for column in AGGREGATE_MEANS]
        missing = [
            *numeric,
            pl.lit(None, dtype=pl.String).alias("mountain_score_version"),
            pl.lit(None, dtype=pl.String).alias("mountain_pipeline_version"),
            pl.lit(0.0).alias("mountain_population_coverage"),
        ]
        return (
            places.with_columns(*missing, unavailable_status()),
            counties.with_columns(*missing, unavailable_status()),
            {
                "checksum": sha256_bytes(b"unavailable"),
                "data_release": "unavailable",
                "score_version": "unavailable",
            },
        )
    release, manifest, mountain_tracts, mountain_counties = current

    def reconcile_connecticut_tracts(
        frame: pl.DataFrame, mountain: pl.DataFrame
    ) -> pl.DataFrame:
        targets = frame.filter(pl.col("state") == "CT").select(
            pl.col("place_id").alias("_target_id"),
            pl.col("place_id").str.slice(-6).alias("_tract_code"),
        )
        if targets.is_empty():
            return mountain
        sources = mountain.filter(pl.col("place_id").str.starts_with("09")).with_columns(
            pl.col("place_id").str.slice(-6).alias("_tract_code")
        )
        mapped = sources.join(targets, on="_tract_code", how="inner")
        unmatched = sources.join(targets, on="_tract_code", how="anti")
        if (
            targets["_tract_code"].n_unique() != targets.height
            or mapped["_tract_code"].n_unique() != mapped.height
            or mapped.height != targets.height
        ):
            raise HouseHunterError("Mountain Connecticut tract reconciliation is ambiguous")
        if (
            set(unmatched["place_id"]) != _CONNECTICUT_UNMATCHED_ZERO_POPULATION_TRACTS
            or unmatched.filter(
                pl.col("mountain_coverage_status").is_null()
                | (pl.col("mountain_coverage_status") != "zero_population")
                | pl.col("mountain_population_coverage").is_null()
                | (pl.col("mountain_population_coverage") != 0)
                | pl.col("mountain_score").is_not_null()
            ).height
        ):
            raise HouseHunterError("Mountain Connecticut tract exceptions differ")
        return pl.concat(
            [
                mountain.filter(~pl.col("place_id").str.starts_with("09")),
                mapped.drop("place_id", "_tract_code")
                .rename({"_target_id": "place_id"})
                .select(mountain.columns),
            ]
        )

    def attach(
        frame: pl.DataFrame,
        mountain: pl.DataFrame,
        *,
        reconcile_ct: bool = False,
        allow_missing_states: frozenset[str] = frozenset(),
    ) -> pl.DataFrame:
        if reconcile_ct:
            mountain = reconcile_connecticut_tracts(frame, mountain)
        expected = frame.filter(
            pl.col("state").is_in(sorted(IN_SCOPE_STATES - allow_missing_states))
        ).select("place_id")
        missing_ids = expected.join(mountain.select("place_id"), on="place_id", how="anti")
        if missing_ids.height:
            raise HouseHunterError(
                f"Mountain compact release is missing {missing_ids.height} in-scope runtime rows"
            )
        return (
            frame.join(
                mountain.with_columns(pl.lit(True).alias("_mountain_present")),
                on="place_id",
                how="left",
            )
            .with_columns(
                pl.when(~pl.col("state").is_in(sorted(IN_SCOPE_STATES)))
                .then(pl.lit("outside_scope"))
                .when(pl.col("_mountain_present").is_null())
                .then(pl.lit("unavailable"))
                .otherwise(pl.col("mountain_coverage_status"))
                .alias("mountain_coverage_status"),
                pl.col("mountain_population_coverage").fill_null(0.0),
            )
            .drop("_mountain_present")
        )

    return (
        attach(places, mountain_tracts, reconcile_ct=True),
        attach(counties, mountain_counties, allow_missing_states=frozenset({"CT"})),
        {
            "checksum": sha256_file(release / "manifest.json"),
            "data_release": str(manifest["data_release"]),
            "score_version": str(manifest["score_version"]),
        },
    )


def compute_scores(
    fema: pl.DataFrame,
    counties: pl.DataFrame | None = None,
    chrr: pl.DataFrame | None = None,
    *,
    fema_vintage: str = "December 2025",
    chrr_release_year: int = 2025,
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
    if chrr is None:
        chrr = pl.DataFrame(
            {"county_fips": [], "community_conditions_group": []},
            schema={"county_fips": pl.String, "community_conditions_group": pl.Int8},
        )
    fema = with_hazard_columns(fema)
    counties = with_hazard_columns(counties)
    hazard_cols = [pl.col(column) for column in HAZARD_COLUMNS]
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
    county_scored = (
        counties.select(
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
            pl.when(county_complete)
            .then(pl.lit("complete"))
            .otherwise(pl.lit("missing_fema"))
            .alias("coverage_status"),
            pl.when(county_complete)
            .then(pl.lit(1.0))
            .otherwise(pl.lit(0.0))
            .alias("coverage_ratio"),
            pl.lit(0, dtype=pl.Int64).alias("total_weighted_housing"),
            pl.lit(fema_vintage).alias("fema_vintage"),
            pl.lit("n/a").alias("census_vintage"),
            pl.col("county_fips"),
            _county_display_expr().alias("county_name"),
            *hazard_cols,
        )
        .join(
            chrr.select("county_fips", "community_conditions_group"),
            on="county_fips",
            how="left",
        )
        .with_columns(
            pl.lit("county").alias("community_conditions_geography"),
            pl.lit(chrr_release_year, dtype=pl.Int16).alias("chrr_release_year"),
        )
        .sort("place_id")
    )
    lookup = county_scored.select(
        pl.col("place_id").alias("matched_county_fips"),
        pl.col("name").alias("matched_county_name"),
        "community_conditions_group",
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
            pl.when(complete)
            .then(pl.lit("complete"))
            .otherwise(pl.lit("missing_fema"))
            .alias("coverage_status"),
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
            pl.col("community_conditions_group"),
            pl.lit("county").alias("community_conditions_geography"),
            pl.lit(chrr_release_year, dtype=pl.Int16).alias("chrr_release_year"),
            *hazard_cols,
        )
        .sort("place_id")
    )
    contributions = fema.select(
        pl.col("tract_id").alias("place_id"),
        pl.col("tract_id"),
        pl.lit(0, dtype=pl.Int64).alias("housing_units"),
        pl.lit(1.0).alias("housing_weight"),
        pl.col("alr_npctl"),
        pl.when(complete)
        .then(pl.col("alr_npctl"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("weighted_contribution"),
    ).sort(["place_id", "tract_id"])
    return scored, contributions, county_scored


def _write_duckdb(
    path: Path,
    places: Path,
    contributions: Path,
    counties: Path,
    chrr_counties: Path,
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
        connection.execute(
            "CREATE TABLE chrr_county AS SELECT * FROM read_parquet(?)", [str(chrr_counties)]
        )
        connection.execute("CREATE INDEX places_id_idx ON places(place_id)")
        connection.execute("CREATE INDEX places_state_idx ON places(state)")
        connection.execute("CREATE INDEX places_county_idx ON places(county_fips)")
        connection.execute("CREATE INDEX contribution_place_idx ON tract_contributions(place_id)")
        connection.execute("CREATE INDEX counties_id_idx ON counties(place_id)")
        connection.execute("CREATE INDEX counties_state_idx ON counties(state)")
        connection.execute("CREATE INDEX chrr_county_id_idx ON chrr_county(county_fips)")
        connection.execute(
            "CREATE TABLE build_metadata AS SELECT ? AS metadata_json",
            [json.dumps(metadata, sort_keys=True)],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def _snapshot_signature(target: Path) -> tuple[tuple[int, int, int, int, int], ...]:
    return tuple(
        (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        for filename in _SNAPSHOT_FILES
        for metadata in [(target / filename).stat()]
    )


def _validate_snapshot_artifacts(target: Path) -> bool:
    try:
        metadata = json.loads((target / "build.json").read_text())
        places = pl.read_parquet(target / "places.parquet")
        contributions = pl.read_parquet(target / "tract_contributions.parquet")
        counties = pl.read_parquet(target / "counties.parquet")
        chrr_counties = pl.read_parquet(target / "chrr_county.parquet")
    except (OSError, json.JSONDecodeError, pl.exceptions.PolarsError):
        return False
    if metadata.get("schema_version") != BUILD_SCHEMA_VERSION:
        return False
    if metadata.get("place_count") != places.height:
        return False
    if metadata.get("county_count") != counties.height:
        return False
    if metadata.get("chrr_county_count") != chrr_counties.height:
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
        "chrr_county": logical_checksum(chrr_counties, chrr_counties.columns, ["county_fips"]),
    }:
        return False
    try:
        connection = duckdb.connect(str(target / "househunter.duckdb"), read_only=True)
        try:
            for table, filename in (
                ("places", "places.parquet"),
                ("tract_contributions", "tract_contributions.parquet"),
                ("counties", "counties.parquet"),
                ("chrr_county", "chrr_county.parquet"),
            ):
                parquet = str(target / filename)
                if connection.execute(f"DESCRIBE {table}").fetchall() != connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [parquet]
                ).fetchall():
                    return False
                differs = connection.execute(
                    f"SELECT EXISTS ("
                    f"(SELECT * FROM {table} EXCEPT ALL SELECT * FROM read_parquet(?)) "
                    f"UNION ALL "
                    f"(SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM {table})"
                    f")",
                    [parquet, parquet],
                ).fetchone()[0]
                if differs:
                    return False
            stored_metadata = json.loads(
                connection.execute("SELECT metadata_json FROM build_metadata").fetchone()[0]
            )
        finally:
            connection.close()
    except (duckdb.Error, json.JSONDecodeError, IndexError, TypeError):
        return False
    return stored_metadata == metadata


@lru_cache(maxsize=8)
def _cached_snapshot_artifacts_valid(
    target: str, signature: tuple[tuple[int, int, int, int, int], ...]
) -> bool:
    del signature
    return _validate_snapshot_artifacts(Path(target))


def snapshot_artifacts_are_valid(target: Path) -> bool:
    artifacts = [target / filename for filename in _SNAPSHOT_FILES]
    if any(not path.is_file() or path.is_symlink() for path in artifacts):
        return False
    try:
        return _cached_snapshot_artifacts_valid(str(target), _snapshot_signature(target))
    except OSError:
        return False


def _existing_build_is_valid(
    target: Path,
    build_id: str,
    input_hashes: dict[str, str],
    source_vintages: dict[str, str | int],
) -> bool:
    if not snapshot_artifacts_are_valid(target):
        return False
    try:
        metadata = json.loads((target / "build.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        metadata.get("build_id") == build_id
        and metadata.get("input_checksums") == input_hashes
        and metadata.get("source_vintages") == source_vintages
    )


def build_snapshot(
    paths: RuntimePaths,
    *,
    state: str | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> Path:
    from .mountain import current_compact_release

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
    config = load_config()
    source = config["fema"]
    county_source = config["fema_counties"]
    chrr_source = config["chrr"]
    fema, fema_sha = validate_cached_fema(fema_path, source)
    counties, county_sha = validate_cached_fema_counties(county_path, county_source)
    chrr_counties, chrr_sha = build_processed(paths)
    input_hashes = {"fema": fema_sha, "fema_counties": county_sha, "chrr": chrr_sha}
    source_vintages = {
        "fema": source["version"],
        "fema_release": source["release"],
        "fema_counties": county_source["version"],
        "fema_counties_release": county_source["release"],
        "chrr": chrr_source["version"],
        "chrr_release_year": chrr_source["release_year"],
        "mountain_runtime_geography": MOUNTAIN_RUNTIME_GEOGRAPHY_VERSION,
    }
    mountain_release = current_compact_release(paths)
    if mountain_release is None:
        mountain_identity = {
            "checksum": sha256_bytes(b"unavailable"),
            "data_release": "unavailable",
            "score_version": "unavailable",
        }
    else:
        mountain_path, mountain_manifest, _, _ = mountain_release
        mountain_identity = {
            "checksum": sha256_file(mountain_path / "manifest.json"),
            "data_release": str(mountain_manifest["data_release"]),
            "score_version": str(mountain_manifest["score_version"]),
        }
    input_hashes["mountain"] = mountain_identity["checksum"]
    source_vintages["mountain"] = mountain_identity["data_release"]
    source_vintages["mountain_score"] = mountain_identity["score_version"]
    scope = state or "national"
    build_key = sha256_bytes(
        canonical_json(
            {
                "schema_version": BUILD_SCHEMA_VERSION,
                "scope": scope,
                "inputs": input_hashes,
                "source_vintages": source_vintages,
            }
        )
    )[:16]
    build_id = f"{scope.lower()}-{build_key}"
    target = paths.builds / build_id
    if target.is_dir():
        if not _existing_build_is_valid(target, build_id, input_hashes, source_vintages):
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
        fema,
        counties,
        chrr_counties,
        fema_vintage=source["version"],
        chrr_release_year=chrr_source["release_year"],
    )
    scored, county_scored, attached_mountain = _attach_mountain_scores(scored, county_scored, paths)
    if attached_mountain != mountain_identity:
        raise HouseHunterError("Mountain release changed during snapshot build")
    if state:
        scored = scored.filter(pl.col("state") == state)
        contributions = contributions.join(scored.select("place_id"), on="place_id", how="semi")
        county_scored = county_scored.filter(pl.col("state") == state)
        chrr_counties = chrr_counties.filter(pl.col("state") == state)
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
        "chrr_county": logical_checksum(chrr_counties, chrr_counties.columns, ["county_fips"]),
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
        "chrr_county_count": chrr_counties.height,
        "chrr_grouped_count": chrr_counties.filter(
            pl.col("community_conditions_group").is_not_null()
        ).height,
        "source_vintages": source_vintages,
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
        chrr_output = temporary / "chrr_county.parquet"
        scored.write_parquet(places_output, compression="zstd", statistics=True)
        contributions.write_parquet(contributions_output, compression="zstd", statistics=True)
        county_scored.write_parquet(counties_output, compression="zstd", statistics=True)
        chrr_counties.write_parquet(chrr_output, compression="zstd", statistics=True)
        (temporary / "build.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        if progress:
            progress(75, "Creating read-only query snapshot")
        _write_duckdb(
            temporary / "househunter.duckdb",
            places_output,
            contributions_output,
            counties_output,
            chrr_output,
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
