from __future__ import annotations

import io
import os
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import polars as pl

from .config import RuntimePaths, canonical_json, load_config, sha256_bytes
from .contracts import SourceStatus
from .download import _validate_source_manifest, _write_source_manifest
from .errors import HouseHunterError, SourceContractError
from .geography import STATE_BY_FIPS

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]

BEA_CACHE_NAME = "bea_rpp_2024.parquet"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
TARGET_LINE_CODES = (1, 2, 3, 4, 5)
VALUE_COLUMNS = {
    1: "cost_of_living_index",
    2: "cost_of_living_goods_index",
    3: "cost_of_living_housing_rents_index",
    4: "cost_of_living_utilities_index",
    5: "cost_of_living_other_services_index",
}
OUTSIDE_SCOPE_STATE_FIPS = frozenset({"60", "66", "69", "72", "78"})
IN_SCOPE_STATE_FIPS = frozenset(STATE_BY_FIPS) - OUTSIDE_SCOPE_STATE_FIPS


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _manifest_source(source: dict[str, Any]) -> dict[str, Any]:
    return {**source, "schema_fingerprint": source["header_sha256"]}


def _header_with_line_ending(content: bytes) -> bytes:
    header, separator, _ = content.partition(b"\n")
    if not separator:
        raise SourceContractError("BEA RPP table has no header row")
    return header + separator


def _logical_rows(frame: pl.DataFrame) -> list[list[Any]]:
    columns = [
        "cost_of_living_geography_id",
        "cost_of_living_geography_name",
        *VALUE_COLUMNS.values(),
        "cost_of_living_release_year",
    ]
    return [[row[column] for column in columns] for row in frame.iter_rows(named=True)]


def logical_checksum(frame: pl.DataFrame) -> str:
    return sha256_bytes(canonical_json(_logical_rows(frame)))


def parse_archive(content: bytes, source: dict[str, Any]) -> pl.DataFrame:
    if len(content) != int(source["expected_size"]):
        raise SourceContractError(
            "BEA RPP archive size changed: "
            f"expected {source['expected_size']}, got {len(content)}"
        )
    actual_archive_sha = sha256_bytes(content)
    if actual_archive_sha != source["archive_sha256"]:
        raise SourceContractError(
            "BEA RPP archive checksum mismatch: "
            f"expected {source['archive_sha256']}, got {actual_archive_sha}"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = [
                member
                for member in archive.infolist()
                if member.filename == source["table_filename"]
            ]
            if len(members) != 1:
                raise SourceContractError("BEA RPP archive must contain exactly one pinned table")
            member = members[0]
            if member.is_dir() or member.file_size != int(source["table_size"]):
                raise SourceContractError("BEA RPP table size changed")
            table = archive.read(member)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SourceContractError(f"Cannot read BEA RPP archive: {exc}") from exc
    if len(table) != int(source["table_size"]):
        raise SourceContractError("BEA RPP table size changed")
    actual_header_sha = sha256_bytes(_header_with_line_ending(table))
    if actual_header_sha != source["header_sha256"]:
        raise SourceContractError(
            "BEA RPP table header changed: "
            f"expected {source['header_sha256']}, got {actual_header_sha}"
        )
    try:
        raw = pl.read_csv(
            io.BytesIO(table),
            schema_overrides={"GeoFIPS": pl.String},
            null_values=["(NA)"],
        )
    except pl.exceptions.PolarsError as exc:
        raise SourceContractError(f"Cannot parse BEA RPP table: {exc}") from exc
    missing = set(source["required_columns"]) - set(raw.columns)
    if missing:
        raise SourceContractError(
            f"BEA RPP table is missing columns: {', '.join(sorted(missing))}"
        )
    year = str(source["release_year"])
    selected = (
        raw.with_columns(
            pl.col("GeoFIPS").str.strip_chars(' "').alias("geography_id"),
            pl.col("GeoName").str.strip_chars().str.strip_suffix(" *").alias("geography_name"),
        )
        .filter(pl.col("LineCode").is_in(TARGET_LINE_CODES))
        .select("geography_id", "geography_name", "TableName", "Unit", "LineCode", year)
    )
    if selected.filter((pl.col("TableName") != "MARPP") | (pl.col("Unit") != "Index")).height:
        raise SourceContractError("BEA RPP target rows changed table or unit")
    invalid_geography = pl.col("geography_id").is_null() | ~pl.col(
        "geography_id"
    ).str.contains(r"^\d{5}$")
    if selected.filter(invalid_geography).height:
        raise SourceContractError("BEA RPP contains invalid geography IDs")
    if selected.filter(
        pl.col("geography_name").is_null() | (pl.col("geography_name") == "")
    ).height:
        raise SourceContractError("BEA RPP contains blank geography names")
    invalid_values = pl.col(year).is_null() | ~pl.col(year).is_finite() | (pl.col(year) <= 0)
    if selected.filter(invalid_values).height:
        raise SourceContractError("BEA RPP contains invalid target values")
    if selected.select("geography_id", "LineCode").n_unique() != selected.height:
        raise SourceContractError("BEA RPP contains duplicate geography and line-code rows")

    nonmetro = str(source["nonmetropolitan_geofips"])
    expected_ids = {nonmetro}
    msa_ids = set(
        selected.filter(~pl.col("geography_id").is_in(["00000", nonmetro]))[
            "geography_id"
        ].to_list()
    )
    if len(msa_ids) != int(source["expected_msa_count"]):
        raise SourceContractError(
            "BEA RPP metropolitan geography count changed: "
            f"expected {source['expected_msa_count']}, got {len(msa_ids)}"
        )
    expected_ids.update(msa_ids)
    selected = selected.filter(pl.col("geography_id").is_in(expected_ids))
    counts = selected.group_by("geography_id").agg(
        pl.col("LineCode").n_unique().alias("line_count")
    )
    if counts.height != len(expected_ids) or counts.filter(pl.col("line_count") != 5).height:
        raise SourceContractError("BEA RPP geographies do not each contain all target line codes")

    names = selected.select("geography_id", "geography_name").unique()
    if names.height != len(expected_ids):
        raise SourceContractError("BEA RPP geography names are inconsistent")
    wide = selected.pivot(on="LineCode", index="geography_id", values=year)
    frame = (
        names.join(wide, on="geography_id", how="inner", validate="1:1")
        .rename(
            {
                "geography_id": "cost_of_living_geography_id",
                "geography_name": "cost_of_living_geography_name",
                **{str(code): column for code, column in VALUE_COLUMNS.items()},
            }
        )
        .with_columns(
            pl.lit(int(source["release_year"]), dtype=pl.Int16).alias(
                "cost_of_living_release_year"
            )
        )
        .select(
            "cost_of_living_geography_id",
            "cost_of_living_geography_name",
            *VALUE_COLUMNS.values(),
            "cost_of_living_release_year",
        )
        .sort("cost_of_living_geography_id")
    )
    if frame.height != int(source["expected_msa_count"]) + 1:
        raise SourceContractError("BEA RPP normalized row count changed")
    return frame


def validate_cached(path: Path, source: dict[str, Any]) -> tuple[pl.DataFrame, str]:
    if path.is_symlink() or not path.is_file():
        raise SourceContractError("BEA RPP cache must be a regular file")
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"Cannot read cached BEA RPP data: {exc}") from exc
    expected_columns = [
        "cost_of_living_geography_id",
        "cost_of_living_geography_name",
        *VALUE_COLUMNS.values(),
        "cost_of_living_release_year",
    ]
    if frame.columns != expected_columns:
        raise SourceContractError("Cached BEA RPP schema does not match the pinned schema")
    expected_rows = int(source["expected_msa_count"]) + 1
    if (
        frame.height != expected_rows
        or frame["cost_of_living_geography_id"].n_unique() != frame.height
    ):
        raise SourceContractError("Cached BEA RPP geography count or uniqueness is invalid")
    if frame["cost_of_living_geography_id"].to_list() != sorted(
        frame["cost_of_living_geography_id"].to_list()
    ):
        raise SourceContractError("Cached BEA RPP geographies are not sorted")
    expected_ids = {str(source["nonmetropolitan_geofips"])}
    ids = set(frame["cost_of_living_geography_id"].to_list())
    invalid_ids = frame.filter(
        pl.col("cost_of_living_geography_id").is_null()
        | ~pl.col("cost_of_living_geography_id").str.contains(r"^\d{5}$")
    )
    invalid_names = frame.filter(
        pl.col("cost_of_living_geography_name").is_null()
        | (pl.col("cost_of_living_geography_name").str.strip_chars() == "")
    )
    if (
        not expected_ids.issubset(ids)
        or "00000" in ids
        or invalid_ids.height
        or invalid_names.height
    ):
        raise SourceContractError("Cached BEA RPP geography scope is invalid")
    invalid_values = pl.any_horizontal(
        [
            pl.col(column).is_null()
            | ~pl.col(column).is_finite()
            | (pl.col(column) <= 0)
            for column in VALUE_COLUMNS.values()
        ]
    )
    if frame.filter(invalid_values).height:
        raise SourceContractError("Cached BEA RPP values are invalid")
    if frame.filter(pl.col("cost_of_living_release_year") != int(source["release_year"])).height:
        raise SourceContractError("Cached BEA RPP release year is invalid")
    digest = logical_checksum(frame)
    expected_digest = source.get("normalized_logical_sha256")
    if not isinstance(expected_digest, str) or not _is_sha256(expected_digest):
        raise SourceContractError("BEA RPP normalized checksum lock is invalid")
    if digest != expected_digest:
        raise SourceContractError(
            "BEA RPP normalized checksum mismatch: "
            f"expected {expected_digest}, got {digest}"
        )
    return frame, digest


def _fetch_archive(client: httpx.Client, source: dict[str, Any]) -> bytes:
    last_error: Exception | None = None
    expected_size = int(source["expected_size"])
    if expected_size <= 0 or expected_size > MAX_ARCHIVE_BYTES:
        raise SourceContractError("Configured BEA RPP archive size is invalid")
    for attempt in range(4):
        try:
            with client.stream(
                "GET", source["archive_url"], headers={"Accept-Encoding": "identity"}
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise SourceContractError("BEA RPP response uses unsupported compression")
                length = response.headers.get("content-length")
                if length is not None and int(length) != expected_size:
                    raise SourceContractError("BEA RPP response size does not match the lock")
                if response.is_stream_consumed:
                    content = bytearray(response.content)
                    if len(content) > expected_size:
                        raise SourceContractError("BEA RPP response exceeds the locked size")
                else:
                    content = bytearray()
                    for chunk in response.iter_raw():
                        if len(content) + len(chunk) > expected_size:
                            raise SourceContractError("BEA RPP response exceeds the locked size")
                        content.extend(chunk)
            if len(content) != expected_size:
                raise SourceContractError("BEA RPP response size does not match the lock")
            return bytes(content)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * (2**attempt))
    raise SourceContractError(f"BEA RPP request failed after retries: {last_error}")


def download_bea_rpp(
    paths: RuntimePaths,
    *,
    client: httpx.Client | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    force: bool = False,
) -> Path:
    paths.ensure()
    source = load_config().get("bea_rpp")
    if not isinstance(source, dict):
        raise SourceContractError("BEA RPP is missing from the pinned source configuration")
    output = paths.cache / BEA_CACHE_NAME
    manifest_source = _manifest_source(source)
    if cancelled and cancelled():
        raise InterruptedError("BEA RPP download cancelled")
    if output.is_file() and not force:
        try:
            frame, digest = validate_cached(output, source)
            _validate_source_manifest(
                paths.source_manifest,
                manifest_source,
                output,
                digest,
                frame.height,
                source_key="bea_rpp",
            )
        except SourceContractError:
            if progress:
                progress(0, "Cached BEA RPP data failed verification; refreshing")
        else:
            if progress:
                progress(100, "Using verified BEA RPP cache")
            return output

    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30, connect=15), follow_redirects=True)
    token = uuid4().hex
    temporary = paths.cache / f".{BEA_CACHE_NAME}.{token}.tmp"
    backup = paths.cache / f".{BEA_CACHE_NAME}.{token}.backup"
    try:
        if progress:
            progress(5, "Downloading pinned BEA RPP release")
        content = _fetch_archive(http, source)
        if cancelled and cancelled():
            raise InterruptedError("BEA RPP download cancelled")
        frame = parse_archive(content, source)
        digest = logical_checksum(frame)
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        validate_cached(temporary, source)
        if cancelled and cancelled():
            raise InterruptedError("BEA RPP download cancelled")
        previous_manifest = (
            paths.source_manifest.read_bytes() if paths.source_manifest.is_file() else None
        )
        if output.exists() or output.is_symlink():
            os.replace(output, backup)
        try:
            os.replace(temporary, output)
            _write_source_manifest(
                paths,
                manifest_source,
                output,
                digest,
                frame.height,
                source_key="bea_rpp",
            )
        except BaseException:
            output.unlink(missing_ok=True)
            if backup.exists() or backup.is_symlink():
                os.replace(backup, output)
            if previous_manifest is None:
                paths.source_manifest.unlink(missing_ok=True)
            elif (
                not paths.source_manifest.is_file()
                or paths.source_manifest.read_bytes() != previous_manifest
            ):
                restored_manifest = paths.source_manifest.with_name(
                    f".{paths.source_manifest.name}.{token}.restore"
                )
                restored_manifest.write_bytes(previous_manifest)
                os.replace(restored_manifest, paths.source_manifest)
            raise
        backup.unlink(missing_ok=True)
        if progress:
            try:
                progress(100, "BEA RPP download verified")
            except InterruptedError:
                if not cancelled or not cancelled():
                    raise
        return output
    finally:
        temporary.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        if owns_client:
            http.close()


def build_processed(paths: RuntimePaths) -> tuple[pl.DataFrame, str]:
    source = load_config().get("bea_rpp")
    if not isinstance(source, dict):
        raise SourceContractError("BEA RPP is missing from the pinned source configuration")
    cached = paths.cache / BEA_CACHE_NAME
    if not cached.is_file():
        raise SourceContractError(
            "BEA RPP data is not cached; run `househunter download --source bea_rpp`"
        )
    frame, digest = validate_cached(cached, source)
    _validate_source_manifest(
        paths.source_manifest,
        _manifest_source(source),
        cached,
        digest,
        frame.height,
        source_key="bea_rpp",
    )
    return frame, digest


def assign_counties(
    counties: pl.DataFrame,
    county_msa: pl.DataFrame,
    rpp: pl.DataFrame,
    source: dict[str, Any],
) -> pl.DataFrame:
    """Assign each county an MSA value or the single national nonmetro value."""
    if "county_fips" not in counties.columns:
        raise SourceContractError("Cost-of-living county input is missing county_fips")
    invalid_county = pl.col("county_fips").is_null() | ~pl.col(
        "county_fips"
    ).str.contains(r"^\d{5}$")
    if counties["county_fips"].n_unique() != counties.height or counties.filter(
        invalid_county
    ).height:
        raise SourceContractError("Cost-of-living county input has invalid or duplicate FIPS")
    if county_msa.columns != ["county_fips", "cbsa_id"] or county_msa[
        "county_fips"
    ].n_unique() != county_msa.height:
        raise SourceContractError("Cost-of-living county-to-CBSA input is invalid")
    if county_msa.filter(
        pl.col("county_fips").is_null()
        | pl.col("cbsa_id").is_null()
        | ~pl.col("county_fips").str.contains(r"^\d{5}$")
        | ~pl.col("cbsa_id").str.contains(r"^\d{5}$")
    ).height:
        raise SourceContractError("Cost-of-living county-to-CBSA IDs are invalid")
    validate_cached_frame = rpp.select(
        "cost_of_living_geography_id",
        "cost_of_living_geography_name",
        *VALUE_COLUMNS.values(),
        "cost_of_living_release_year",
    )
    expected_rows = int(source["expected_msa_count"]) + 1
    if (
        validate_cached_frame.height != expected_rows
        or validate_cached_frame["cost_of_living_geography_id"].n_unique()
        != expected_rows
    ):
        raise SourceContractError("Cost-of-living BEA input is invalid")
    nonmetro = str(source["nonmetropolitan_geofips"])
    bea_ids = set(validate_cached_frame["cost_of_living_geography_id"])
    if nonmetro not in bea_ids:
        raise SourceContractError("Cost-of-living BEA input lacks the nonmetro fallback")

    assignments = counties.select("county_fips").join(
        county_msa, on="county_fips", how="left", validate="1:1"
    )
    state_fips = pl.col("county_fips").str.slice(0, 2)
    in_scope = state_fips.is_in(IN_SCOPE_STATE_FIPS)
    outside_scope = state_fips.is_in(OUTSIDE_SCOPE_STATE_FIPS)
    msa_ids = sorted(bea_ids - {nonmetro})
    assignments = assignments.with_columns(
        pl.when(~in_scope)
        .then(None)
        .when(pl.col("cbsa_id").is_in(msa_ids))
        .then(pl.col("cbsa_id"))
        .otherwise(pl.lit(nonmetro))
        .alias("cost_of_living_geography_id"),
        pl.when(~in_scope)
        .then(None)
        .when(pl.col("cbsa_id").is_in(msa_ids))
        .then(pl.lit("metropolitan"))
        .otherwise(pl.lit("nonmetropolitan"))
        .alias("cost_of_living_geography_type"),
        pl.when(in_scope)
        .then(pl.lit("complete"))
        .when(outside_scope)
        .then(pl.lit("outside_scope"))
        .otherwise(pl.lit("unmatched_geography"))
        .alias("cost_of_living_coverage_status"),
    ).drop("cbsa_id")
    output = assignments.join(
        validate_cached_frame,
        on="cost_of_living_geography_id",
        how="left",
        validate="m:1",
    ).sort("county_fips")
    if output.height != counties.height:
        raise SourceContractError("Cost-of-living county assignment changed row count")
    complete = output.filter(pl.col("cost_of_living_coverage_status") == "complete")
    if complete.filter(pl.col("cost_of_living_index").is_null()).height:
        raise SourceContractError("Cost-of-living county assignment left in-scope values null")
    return output


def assign_counties_v2(
    counties: pl.DataFrame,
    county_msa: pl.DataFrame,
    rpp: pl.DataFrame,
    source: dict[str, Any],
    state_rpp: pl.DataFrame,
) -> pl.DataFrame:
    """Assign MSA MARPP values or official state all-items RPP labeled `state`."""
    if "state_fips" not in state_rpp.columns or "cost_of_living_index" not in state_rpp.columns:
        raise SourceContractError("State RPP table is missing required columns")
    if state_rpp["state_fips"].n_unique() != state_rpp.height:
        raise SourceContractError("State RPP table contains duplicate states")
    if state_rpp.filter(
        pl.col("state_fips").is_null() | ~pl.col("state_fips").str.contains(r"^\d{2}$")
    ).height:
        raise SourceContractError("State RPP table contains invalid state FIPS")
    if "00999" in set(state_rpp["state_fips"].to_list()):
        raise SourceContractError("State RPP table must not use the 00999 nonmetro code")
    metro = assign_counties(counties, county_msa, rpp, source)
    state_values = state_rpp.rename(
        {
            column: f"state_{column}"
            for column in state_rpp.columns
            if column != "state_fips"
        }
    )
    assigned = metro.with_columns(pl.col("county_fips").str.slice(0, 2).alias("state_fips")).join(
        state_values, on="state_fips", how="left", validate="m:1"
    )
    nonmetro = pl.col("cost_of_living_geography_type") == "nonmetropolitan"
    output = assigned.with_columns(
        pl.when(nonmetro)
        .then(pl.col("state_cost_of_living_geography_id"))
        .otherwise(pl.col("cost_of_living_geography_id"))
        .alias("cost_of_living_geography_id"),
        pl.when(nonmetro)
        .then(pl.lit("state"))
        .otherwise(pl.col("cost_of_living_geography_type"))
        .alias("cost_of_living_geography_type"),
        pl.when(nonmetro)
        .then(pl.col("state_cost_of_living_geography_name"))
        .otherwise(pl.col("cost_of_living_geography_name"))
        .alias("cost_of_living_geography_name"),
        *[
            pl.when(nonmetro)
            .then(pl.col(f"state_{column}"))
            .otherwise(pl.col(column))
            .alias(column)
            for column in VALUE_COLUMNS.values()
        ],
    )
    if "state_cost_of_living_release_year" in output.columns:
        output = output.with_columns(
            pl.when(nonmetro)
            .then(pl.col("state_cost_of_living_release_year"))
            .otherwise(pl.col("cost_of_living_release_year"))
            .alias("cost_of_living_release_year")
        )
    drop = [column for column in output.columns if column.startswith("state_")]
    output = output.drop(drop).sort("county_fips")
    labeled_state = output.filter(pl.col("cost_of_living_geography_type") == "state")
    if labeled_state.filter(
        pl.col("cost_of_living_geography_id").eq("00999")
        | pl.col("cost_of_living_index").is_null()
    ).height:
        raise SourceContractError("State RPP assignment retained 00999 or left values null")
    return output


def inherit_county_costs(tracts: pl.DataFrame, county_costs: pl.DataFrame) -> pl.DataFrame:
    if "tract_id" not in tracts.columns or tracts["tract_id"].n_unique() != tracts.height:
        raise SourceContractError("Cost-of-living tract input is invalid")
    if tracts.filter(
        pl.col("tract_id").is_null()
        | ~pl.col("tract_id").str.contains(r"^\d{11}$")
    ).height:
        raise SourceContractError("Cost-of-living tract IDs are invalid")
    if county_costs["county_fips"].n_unique() != county_costs.height:
        raise SourceContractError("Cost-of-living county assignments are not unique")
    output = (
        tracts.select("tract_id")
        .with_columns(pl.col("tract_id").str.slice(0, 5).alias("county_fips"))
        .join(county_costs, on="county_fips", how="left", validate="m:1")
        .with_columns(
            pl.col("cost_of_living_coverage_status")
            .fill_null("unmatched_geography")
            .alias("cost_of_living_coverage_status")
        )
    )
    if output.height != tracts.height:
        raise SourceContractError("Cost-of-living tract inheritance changed row count")
    return output.sort("tract_id")


def source_status(paths: RuntimePaths) -> SourceStatus:
    config = load_config()
    source = config.get("bea_rpp")
    if not isinstance(source, dict):
        return SourceStatus(source="bea_rpp", version="unconfigured", cached=False)
    cached = paths.cache / BEA_CACHE_NAME
    if not cached.is_file():
        return SourceStatus(source="bea_rpp", version=source["version"], cached=False)
    try:
        frame, digest = validate_cached(cached, source)
        retrieved_at = _validate_source_manifest(
            paths.source_manifest,
            _manifest_source(source),
            cached,
            digest,
            frame.height,
            source_key="bea_rpp",
        )
        return SourceStatus(
            source="bea_rpp",
            version=source["version"],
            cached=True,
            sha256=digest,
            row_count=frame.height,
            retrieved_at=retrieved_at,
        )
    except (HouseHunterError, OSError, KeyError, TypeError, ValueError) as exc:
        return SourceStatus(
            source="bea_rpp", version=source["version"], cached=True, error=str(exc)
        )
