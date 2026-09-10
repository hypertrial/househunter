from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from .config import RuntimePaths, canonical_json, load_config, sha256_bytes, sha256_file
from .contracts import METHODOLOGY_NOTICE
from .download import validate_cached_fema
from .errors import HouseHunterError
from .reference import reference_assets, validate_reference_assets

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]


def logical_checksum(frame: pl.DataFrame, columns: list[str], sort_by: list[str]) -> str:
    rows = frame.select(columns).sort(sort_by).iter_rows()
    return sha256_bytes(canonical_json([list(row) for row in rows]))


def _cancelled(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise InterruptedError("Build cancelled")


def compute_scores(
    places: pl.DataFrame,
    weights: pl.DataFrame,
    acs: pl.DataFrame,
    fema: pl.DataFrame,
    *,
    fema_vintage: str = "December 2025",
    census_vintage: str = "2020 Census",
    acs_vintage: str = "2024 ACS 5-year",
) -> tuple[pl.DataFrame, pl.DataFrame]:
    contributions = (
        weights.join(fema.select("tract_id", "alr_npctl"), on="tract_id", how="left")
        .with_columns(
            (pl.col("housing_weight") * pl.col("alr_npctl")).alias("weighted_contribution")
        )
        .sort(["place_id", "weighted_contribution", "tract_id"], descending=[False, True, False])
    )
    aggregates = contributions.group_by("place_id").agg(
        pl.col("housing_units").sum().alias("total_weighted_housing"),
        pl.col("housing_units")
        .filter(pl.col("alr_npctl").is_not_null())
        .sum()
        .alias("covered_housing"),
        pl.col("weighted_contribution").sum().alias("risk_score_candidate"),
        pl.col("tract_id").is_null().any().alias("has_unmatched_geography"),
        pl.col("alr_npctl").is_null().any().alias("has_missing_fema"),
    )
    result = (
        places.join(acs, on="place_id", how="left")
        .join(aggregates, on="place_id", how="left")
        .with_columns(
            pl.col("total_weighted_housing").fill_null(0),
            pl.col("covered_housing").fill_null(0),
            pl.col("has_unmatched_geography").fill_null(False),
            pl.col("has_missing_fema").fill_null(False),
        )
        .with_columns(
            pl.when(pl.col("housing_units_2020") <= 0)
            .then(pl.lit("zero_housing"))
            .when(pl.col("has_unmatched_geography"))
            .then(pl.lit("unmatched_geography"))
            .when(pl.col("has_missing_fema") | (pl.col("total_weighted_housing") <= 0))
            .then(pl.lit("missing_fema"))
            .otherwise(pl.lit("complete"))
            .alias("coverage_status"),
            pl.when(pl.col("total_weighted_housing") > 0)
            .then(pl.col("covered_housing") / pl.col("total_weighted_housing"))
            .otherwise(pl.lit(0.0))
            .alias("coverage_ratio"),
        )
        .with_columns(
            pl.when(pl.col("coverage_status") == "complete")
            .then(pl.col("risk_score_candidate"))
            .otherwise(pl.lit(None, dtype=pl.Float64))
            .alias("risk_score")
        )
        .with_columns(
            pl.lit(fema_vintage).alias("fema_vintage"),
            pl.lit(census_vintage).alias("census_vintage"),
            pl.lit(acs_vintage).alias("acs_vintage"),
        )
        .drop("risk_score_candidate", "has_unmatched_geography", "has_missing_fema")
        .sort("place_id")
    )
    return result, contributions


def _write_duckdb(
    path: Path, places: Path, contributions: Path, metadata: dict[str, object]
) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute("CREATE TABLE places AS SELECT * FROM read_parquet(?)", [str(places)])
        connection.execute(
            "CREATE TABLE tract_contributions AS SELECT * FROM read_parquet(?)",
            [str(contributions)],
        )
        connection.execute("CREATE INDEX places_id_idx ON places(place_id)")
        connection.execute("CREATE INDEX places_state_idx ON places(state)")
        connection.execute("CREATE INDEX contribution_place_idx ON tract_contributions(place_id)")
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
    except (OSError, json.JSONDecodeError, pl.exceptions.PolarsError):
        return False
    if not (target / "househunter.duckdb").is_file():
        return False
    if metadata.get("build_id") != build_id or metadata.get("input_checksums") != input_hashes:
        return False
    if metadata.get("place_count") != places.height:
        return False
    if (
        metadata.get("ranked_place_count")
        != places.filter(pl.col("coverage_status") == "complete").height
    ):
        return False
    checksums = metadata.get("logical_checksums", {})
    if checksums != {
        "places": logical_checksum(places, places.columns, ["place_id"]),
        "tract_contributions": logical_checksum(
            contributions, contributions.columns, ["place_id", "tract_id"]
        ),
    }:
        return False
    try:
        connection = duckdb.connect(str(target / "househunter.duckdb"), read_only=True)
        try:
            place_count = connection.execute("SELECT count(*) FROM places").fetchone()[0]
            contribution_count = connection.execute(
                "SELECT count(*) FROM tract_contributions"
            ).fetchone()[0]
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
    assets = reference_assets()
    if progress:
        progress(5, "Validating Census reference assets")
    places, weights, acs = validate_reference_assets(assets)
    if state:
        if state not in places["state"].unique().to_list():
            raise HouseHunterError(f"Unknown state abbreviation: {state}")
        places = places.filter(pl.col("state") == state)
        weights = weights.join(places.select("place_id"), on="place_id", how="semi")
        acs = acs.join(places.select("place_id"), on="place_id", how="semi")
    _cancelled(cancelled)
    fema_path = paths.cache / "fema_nri_tracts.parquet"
    if not fema_path.is_file():
        raise HouseHunterError("FEMA data is not cached; run `househunter download --source fema`")
    source = load_config()["fema"]
    fema, fema_sha = validate_cached_fema(fema_path, source)
    input_hashes = {
        "fema": fema_sha,
        "places_2020": sha256_file(assets.places),
        "place_tract_weights_2020": sha256_file(assets.weights),
        "acs_2024_context": sha256_file(assets.acs),
    }
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
        progress(25, "Computing housing-weighted scores")
    scored, contributions = compute_scores(
        places, weights, acs, fema, fema_vintage=source["version"]
    )
    _cancelled(cancelled)
    complete = scored.filter(pl.col("coverage_status") == "complete")
    if complete.filter(pl.col("coverage_ratio") != 1.0).height:
        raise HouseHunterError(
            "Internal validation failed: ranked Places do not have full coverage"
        )
    place_columns = scored.columns
    contribution_columns = contributions.columns
    checksums = {
        "places": logical_checksum(scored, place_columns, ["place_id"]),
        "tract_contributions": logical_checksum(
            contributions, contribution_columns, ["place_id", "tract_id"]
        ),
    }
    metadata: dict[str, object] = {
        "schema_version": 1,
        "build_id": build_id,
        "scope": {"kind": "state" if state else "national", "state": state},
        "created_at": datetime.now(UTC).isoformat(),
        "place_count": scored.height,
        "ranked_place_count": complete.height,
        "source_vintages": {
            "fema": source["version"],
            "fema_release": source["release"],
            "census": "2020",
            "acs": "2024 ACS 5-year",
        },
        "input_checksums": input_hashes,
        "logical_checksums": checksums,
        "methodology_notice": METHODOLOGY_NOTICE,
    }
    temporary = paths.builds / f".{build_id}.{os.getpid()}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        places_output = temporary / "places.parquet"
        contributions_output = temporary / "tract_contributions.parquet"
        scored.write_parquet(places_output, compression="zstd", statistics=True)
        contributions.write_parquet(contributions_output, compression="zstd", statistics=True)
        (temporary / "build.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        if progress:
            progress(75, "Creating read-only query snapshot")
        _write_duckdb(
            temporary / "househunter.duckdb", places_output, contributions_output, metadata
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
    pointer = {"schema_version": 1, "build_id": build_id, "scope": scope, "path": str(target)}
    temporary = paths.current.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(pointer, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, paths.current)
