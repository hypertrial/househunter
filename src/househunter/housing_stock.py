from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from .config import sha256_file
from .contracts import HOUSING_STOCK_COVERAGE_STATUSES
from .errors import HouseHunterError

BUNDLE_SCHEMA_VERSION = 1
BUNDLED_HOUSING_STOCK = Path(__file__).with_name("assets") / "housing_stock"
ACS_SENTINELS = frozenset(-digit * 111_111_111 for digit in range(2, 10))
TRACT_PREFIX = "1400000US"
COUNTY_PREFIX = "0500000US"
COUNTY_CBSA_PREFIX = "313M700US"
AGE_COLUMNS = [
    "housing_stock_total_units_estimate",
    "housing_built_2020_plus_estimate",
    "housing_built_2010_2019_estimate",
    "housing_built_2000_2009_estimate",
]
HOUSING_COLUMNS = [
    "housing_stock_total_units_estimate",
    "housing_stock_total_units_moe",
    "housing_built_2020_plus_estimate",
    "housing_built_2020_plus_moe",
    "housing_built_2010_2019_estimate",
    "housing_built_2010_2019_moe",
    "housing_built_2000_2009_estimate",
    "housing_built_2000_2009_moe",
    "housing_built_2020_plus_pct",
    "housing_built_2010_plus_pct",
    "housing_built_2000_plus_pct",
    "housing_median_year_built",
    "housing_median_year_built_moe",
    "housing_stock_release_year",
    "housing_stock_coverage_status",
]
ARTIFACT_FILES = {
    "tracts": "tracts.parquet",
    "counties": "counties.parquet",
    "county_msa": "county_msa.parquet",
}


@dataclass(frozen=True)
class HousingStockBundle:
    manifest: dict[str, Any]
    tracts: pl.DataFrame
    counties: pl.DataFrame
    county_msa: pl.DataFrame


def default_source_lock_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "config"
        / "housing-stock"
        / "source-lock-2024.json"
    )


def load_source_lock(path: Path | None = None) -> dict[str, Any]:
    source = path or default_source_lock_path()
    try:
        lock = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read housing-stock source lock: {source}: {exc}") from exc
    if lock.get("schema") != 1 or lock.get("release_year") != 2024:
        raise HouseHunterError(f"Unsupported housing-stock source lock: {source}")
    return lock


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_raw_table(path: Path, contract: dict[str, Any]) -> None:
    if not path.is_file():
        raise HouseHunterError(f"Missing pinned ACS source: {path}")
    if path.stat().st_size != contract["expected_size"]:
        raise HouseHunterError(f"Pinned ACS source has unexpected size: {path}")
    if sha256_file(path) != contract["sha256"]:
        raise HouseHunterError(f"Pinned ACS source checksum differs: {path}")
    with path.open("rb") as handle:
        header = handle.readline()
    if _sha256_bytes(header) != contract["header_sha256"]:
        raise HouseHunterError(f"Pinned ACS source header differs: {path}")
    columns = header.decode("utf-8").rstrip("\r\n").split("|")
    if len(columns) != len(set(columns)):
        raise HouseHunterError(f"Pinned ACS source has duplicate columns: {path}")
    missing = sorted(set(contract["required_columns"]) - set(columns))
    if missing:
        raise HouseHunterError(f"Pinned ACS source is missing columns {missing}: {path}")


def _read_raw_table(path: Path, contract: dict[str, Any]) -> pl.DataFrame:
    verify_raw_table(path, contract)
    columns = list(contract["required_columns"])
    schema = {name: pl.String if name == "GEO_ID" else pl.Int64 for name in columns}
    try:
        frame = pl.read_csv(
            path,
            separator="|",
            columns=columns,
            schema_overrides=schema,
            null_values=[""],
        )
    except Exception as exc:
        raise HouseHunterError(f"Cannot parse pinned ACS source {path}: {exc}") from exc
    if frame.height != contract["expected_row_count"]:
        raise HouseHunterError(f"Pinned ACS source row count differs: {path}")
    return frame


def _clean_numeric(frame: pl.DataFrame, columns: list[str], label: str) -> pl.DataFrame:
    invalid = pl.any_horizontal(
        [(pl.col(name) < 0) & ~pl.col(name).is_in(ACS_SENTINELS) for name in columns]
    )
    if frame.filter(invalid.fill_null(False)).height:
        raise HouseHunterError(f"{label} contains a negative value that is not an ACS sentinel")
    return frame.with_columns(
        [
            pl.when(pl.col(name).is_in(ACS_SENTINELS))
            .then(pl.lit(None, dtype=pl.Int64))
            .otherwise(pl.col(name))
            .alias(name)
            for name in columns
        ]
    )


def _geography_rows(
    frame: pl.DataFrame, prefix: str, digits: int, identifier: str, label: str
) -> pl.DataFrame:
    rows = frame.filter(pl.col("GEO_ID").str.starts_with(prefix))
    valid = (
        (pl.col("GEO_ID").str.len_chars() == len(prefix) + digits)
        & pl.col("GEO_ID").str.slice(len(prefix), digits).str.contains(r"^\d+$")
    )
    if rows.filter(~valid.fill_null(False)).height:
        raise HouseHunterError(f"{label} contains a malformed GEO_ID")
    rows = rows.with_columns(
        pl.col("GEO_ID").str.slice(len(prefix), digits).alias(identifier)
    ).drop("GEO_ID")
    if rows.height != rows[identifier].n_unique():
        raise HouseHunterError(f"{label} contains duplicate {identifier} values")
    return rows.sort(identifier)


def _pct(total: str, numerators: list[str], alias: str) -> pl.Expr:
    available = pl.all_horizontal([pl.col(name).is_not_null() for name in [total, *numerators]])
    numerator = pl.sum_horizontal([pl.col(name) for name in numerators])
    return (
        pl.when(available & (pl.col(total) > 0))
        .then((numerator * 100.0 / pl.col(total)).round(6))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias(alias)
    )


def _housing_frame(
    ages: pl.DataFrame,
    medians: pl.DataFrame,
    *,
    prefix: str,
    digits: int,
    identifier: str,
    release_year: int,
) -> pl.DataFrame:
    age = _geography_rows(ages, prefix, digits, identifier, f"B25034 {identifier}")
    median = _geography_rows(medians, prefix, digits, identifier, f"B25035 {identifier}")
    age_names = {
        "B25034_E001": "housing_stock_total_units_estimate",
        "B25034_M001": "housing_stock_total_units_moe",
        "B25034_E002": "housing_built_2020_plus_estimate",
        "B25034_M002": "housing_built_2020_plus_moe",
        "B25034_E003": "housing_built_2010_2019_estimate",
        "B25034_M003": "housing_built_2010_2019_moe",
        "B25034_E004": "housing_built_2000_2009_estimate",
        "B25034_M004": "housing_built_2000_2009_moe",
    }
    frame = age.rename(age_names).join(
        median.rename(
            {
                "B25035_E001": "housing_median_year_built",
                "B25035_M001": "housing_median_year_built_moe",
            }
        ),
        on=identifier,
        how="left",
        validate="1:1",
    )
    numeric = [name for name in frame.columns if name != identifier]
    frame = _clean_numeric(frame, numeric, identifier)
    total = "housing_stock_total_units_estimate"
    first_three = AGE_COLUMNS[1:]
    impossible = (
        pl.all_horizontal([pl.col(name).is_not_null() for name in AGE_COLUMNS])
        & (pl.sum_horizontal([pl.col(name) for name in first_three]) > pl.col(total))
    )
    if frame.filter(impossible.fill_null(False)).height:
        raise HouseHunterError(f"{identifier} housing-age components exceed total housing")
    invalid_year = pl.col("housing_median_year_built").is_not_null() & ~pl.col(
        "housing_median_year_built"
    ).is_between(1800, release_year)
    if frame.filter(invalid_year.fill_null(False)).height:
        raise HouseHunterError(f"{identifier} contains an invalid median year built")
    complete = pl.all_horizontal([pl.col(name).is_not_null() for name in AGE_COLUMNS]) & pl.col(
        "housing_median_year_built"
    ).is_not_null()
    frame = frame.with_columns(
        _pct(total, [AGE_COLUMNS[1]], "housing_built_2020_plus_pct"),
        _pct(total, AGE_COLUMNS[1:3], "housing_built_2010_plus_pct"),
        _pct(total, AGE_COLUMNS[1:4], "housing_built_2000_plus_pct"),
        pl.lit(release_year).cast(pl.Int16).alias("housing_stock_release_year"),
        pl.when(pl.col(total) == 0)
        .then(pl.lit("zero_housing"))
        .when(complete & (pl.col(total) > 0))
        .then(pl.lit("complete"))
        .otherwise(pl.lit("missing_acs"))
        .alias("housing_stock_coverage_status"),
    )
    return frame.select(identifier, *HOUSING_COLUMNS).sort(identifier)


def parse_raw_tables(
    b25034: pl.DataFrame, b25035: pl.DataFrame, lock: dict[str, Any]
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    release_year = int(lock["release_year"])
    tracts = _housing_frame(
        b25034,
        b25035,
        prefix=TRACT_PREFIX,
        digits=11,
        identifier="tract_id",
        release_year=release_year,
    )
    counties = _housing_frame(
        b25034,
        b25035,
        prefix=COUNTY_PREFIX,
        digits=5,
        identifier="county_fips",
        release_year=release_year,
    )
    crosswalk = _geography_rows(
        b25034.select("GEO_ID"),
        COUNTY_CBSA_PREFIX,
        10,
        "cbsa_county_id",
        "B25034 county-to-CBSA geography",
    ).select(
        pl.col("cbsa_county_id").str.slice(5, 5).alias("county_fips"),
        pl.col("cbsa_county_id").str.slice(0, 5).alias("cbsa_id"),
    )
    if crosswalk.height != crosswalk["county_fips"].n_unique():
        raise HouseHunterError("B25034 assigns one county to multiple CBSAs")
    expected = lock["geography_counts"]
    if tracts.height != expected["tracts"] or counties.height != expected["counties"]:
        raise HouseHunterError("Housing-stock geography counts differ from the source lock")
    geography = lock["county_cbsa_geography"]
    if (
        crosswalk.height != geography["expected_relationship_rows"]
        or crosswalk["cbsa_id"].n_unique() != geography["expected_cbsa_count"]
    ):
        raise HouseHunterError("County-to-CBSA counts differ from the source lock")
    return tracts, counties, crosswalk.sort("county_fips")


def load_raw_tables(
    b25034_path: Path, b25035_path: Path, lock: dict[str, Any]
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    b25034 = _read_raw_table(b25034_path, lock["sources"]["B25034"])
    b25035 = _read_raw_table(b25035_path, lock["sources"]["B25035"])
    return parse_raw_tables(b25034, b25035, lock)


def _artifact_manifest(path: Path, frame: pl.DataFrame) -> dict[str, Any]:
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "rows": frame.height,
        "columns": {name: str(dtype) for name, dtype in frame.schema.items()},
    }


def _validate_housing_frame(frame: pl.DataFrame, identifier: str, release_year: int) -> None:
    if frame.columns != [identifier, *HOUSING_COLUMNS]:
        raise HouseHunterError(f"Housing-stock {identifier} schema differs")
    identifiers = frame[identifier].to_list()
    if identifiers != sorted(set(identifiers)):
        raise HouseHunterError(f"Housing-stock {identifier} values are not sorted and unique")
    digits = 11 if identifier == "tract_id" else 5
    if frame.filter(~pl.col(identifier).str.contains(rf"^\d{{{digits}}}$")).height:
        raise HouseHunterError(f"Housing-stock {identifier} values are invalid")
    if not set(frame["housing_stock_coverage_status"].unique()) <= set(
        HOUSING_STOCK_COVERAGE_STATUSES
    ):
        raise HouseHunterError(f"Housing-stock {identifier} contains an unknown status")
    numeric = [name for name in HOUSING_COLUMNS if name.endswith(("_estimate", "_moe"))]
    negative = pl.any_horizontal([pl.col(name) < 0 for name in numeric]).fill_null(False)
    if frame.filter(negative).height:
        raise HouseHunterError(f"Housing-stock {identifier} contains a negative value")
    total = AGE_COLUMNS[0]
    impossible = (
        pl.all_horizontal([pl.col(name).is_not_null() for name in AGE_COLUMNS])
        & (pl.sum_horizontal([pl.col(name) for name in AGE_COLUMNS[1:]]) > pl.col(total))
    )
    if frame.filter(impossible.fill_null(False)).height:
        raise HouseHunterError(f"Housing-stock {identifier} components exceed total housing")
    invalid_year = pl.col("housing_median_year_built").is_not_null() & ~pl.col(
        "housing_median_year_built"
    ).is_between(1800, release_year)
    if frame.filter(invalid_year.fill_null(False)).height:
        raise HouseHunterError(f"Housing-stock {identifier} median year is invalid")
    complete = pl.all_horizontal([pl.col(name).is_not_null() for name in AGE_COLUMNS]) & pl.col(
        "housing_median_year_built"
    ).is_not_null()
    expected_status = (
        pl.when(pl.col(total) == 0)
        .then(pl.lit("zero_housing"))
        .when(complete & (pl.col(total) > 0))
        .then(pl.lit("complete"))
        .otherwise(pl.lit("missing_acs"))
    )
    if frame.filter(pl.col("housing_stock_coverage_status") != expected_status).height:
        raise HouseHunterError(f"Housing-stock {identifier} status differs from estimates")
    if frame.filter(pl.col("housing_stock_release_year") != release_year).height:
        raise HouseHunterError(f"Housing-stock {identifier} release year differs")
    for numerators, pct_name in (
        (AGE_COLUMNS[1:2], "housing_built_2020_plus_pct"),
        (AGE_COLUMNS[1:3], "housing_built_2010_plus_pct"),
        (AGE_COLUMNS[1:4], "housing_built_2000_plus_pct"),
    ):
        expected_pct = _pct(total, numerators, "expected_pct")
        compared = frame.with_columns(expected_pct)
        mismatch = (
            pl.col(pct_name).is_null() != pl.col("expected_pct").is_null()
        ) | (
            pl.col(pct_name).is_not_null()
            & ((pl.col(pct_name) - pl.col("expected_pct")).abs() > 1e-9)
        )
        if compared.filter(mismatch.fill_null(False)).height:
            raise HouseHunterError(f"Housing-stock {identifier} percentage differs")


def validate_housing_stock_assets(
    directory: Path = BUNDLED_HOUSING_STOCK, *, source_lock_path: Path | None = None
) -> HousingStockBundle:
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(
            f"Cannot read housing-stock manifest: {manifest_path}: {exc}"
        ) from exc
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise HouseHunterError("Unsupported housing-stock bundle schema")
    release_year_value = manifest.get("release_year")
    files = manifest.get("files")
    sources = manifest.get("sources")
    if not isinstance(release_year_value, int) or not isinstance(files, dict):
        raise HouseHunterError("Housing-stock manifest structure is invalid")
    if not isinstance(sources, dict) or set(sources) != {"B25034", "B25035"}:
        raise HouseHunterError("Housing-stock manifest source records are invalid")
    for source in sources.values():
        if not isinstance(source, dict) or not all(
            isinstance(source.get(name), expected)
            for name, expected in (
                ("url", str),
                ("expected_size", int),
                ("sha256", str),
                ("header_sha256", str),
            )
        ):
            raise HouseHunterError("Housing-stock manifest source record is invalid")
    lock: dict[str, Any] | None = None
    if source_lock_path is not None:
        lock = load_source_lock(source_lock_path)
        if manifest.get("source_lock_sha256") != sha256_file(source_lock_path):
            raise HouseHunterError("Housing-stock bundle does not match the source lock")
        if any(
            manifest.get(name) != lock.get(name)
            for name in ("release_year", "survey", "omb_delineation")
        ):
            raise HouseHunterError("Housing-stock manifest source metadata differs")
    frames: dict[str, pl.DataFrame] = {}
    for key, filename in ARTIFACT_FILES.items():
        record = files.get(key)
        if not isinstance(record, dict) or record.get("filename") != filename:
            raise HouseHunterError(f"Housing-stock manifest is missing {key}")
        path = directory / filename
        if (
            not path.is_file()
            or path.stat().st_size != record.get("bytes")
            or sha256_file(path) != record.get("sha256")
        ):
            raise HouseHunterError(f"Housing-stock artifact differs for {key}")
        try:
            frame = pl.read_parquet(path)
        except Exception as exc:
            raise HouseHunterError(f"Cannot read housing-stock artifact {key}: {exc}") from exc
        if frame.height != record.get("rows") or {
            name: str(dtype) for name, dtype in frame.schema.items()
        } != record.get("columns"):
            raise HouseHunterError(f"Housing-stock schema or row count differs for {key}")
        frames[key] = frame
    if lock is not None:
        expected = lock["geography_counts"]
        geography = lock["county_cbsa_geography"]
        if (
            frames["tracts"].height != expected["tracts"]
            or frames["counties"].height != expected["counties"]
            or frames["county_msa"].height != geography["expected_relationship_rows"]
            or frames["county_msa"]["cbsa_id"].n_unique()
            != geography["expected_cbsa_count"]
        ):
            raise HouseHunterError("Housing-stock bundle row count differs from source lock")
    release_year = release_year_value
    _validate_housing_frame(frames["tracts"], "tract_id", release_year)
    _validate_housing_frame(frames["counties"], "county_fips", release_year)
    county_msa = frames["county_msa"]
    if county_msa.columns != ["county_fips", "cbsa_id"]:
        raise HouseHunterError("Housing-stock county-to-CBSA schema differs")
    county_ids = county_msa["county_fips"].to_list()
    if county_ids != sorted(set(county_ids)):
        raise HouseHunterError("Housing-stock county-to-CBSA rows are not sorted and unique")
    invalid_crosswalk = county_msa.filter(
        ~pl.col("county_fips").str.contains(r"^\d{5}$")
        | ~pl.col("cbsa_id").str.contains(r"^\d{5}$")
    )
    if invalid_crosswalk.height:
        raise HouseHunterError("Housing-stock county-to-CBSA identifiers are invalid")
    unknown_counties = set(county_msa["county_fips"]) - set(frames["counties"]["county_fips"])
    if unknown_counties:
        raise HouseHunterError("Housing-stock county-to-CBSA references an unknown county")
    tract_counties = set(frames["tracts"]["tract_id"].str.slice(0, 5))
    if tract_counties - set(frames["counties"]["county_fips"]):
        raise HouseHunterError("Housing-stock tract references an unknown county")
    return HousingStockBundle(
        manifest=manifest,
        tracts=frames["tracts"],
        counties=frames["counties"],
        county_msa=county_msa,
    )


def write_housing_stock_bundle(
    output: Path,
    b25034_path: Path,
    b25035_path: Path,
    *,
    source_lock_path: Path | None = None,
) -> HousingStockBundle:
    lock_path = source_lock_path or default_source_lock_path()
    lock = load_source_lock(lock_path)
    tracts, counties, county_msa = load_raw_tables(b25034_path, b25035_path, lock)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid.uuid4().hex}.staging"
    backup = output.parent / f".{output.name}.{uuid.uuid4().hex}.backup"
    staging.mkdir()
    try:
        frames = {"tracts": tracts, "counties": counties, "county_msa": county_msa}
        for key, frame in frames.items():
            frame.write_parquet(
                staging / ARTIFACT_FILES[key], compression="zstd", statistics=True
            )
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "release_year": lock["release_year"],
            "survey": lock["survey"],
            "omb_delineation": lock["omb_delineation"],
            "source_lock_sha256": sha256_file(lock_path),
            "sources": {
                key: {
                    name: contract[name]
                    for name in ("url", "expected_size", "sha256", "header_sha256")
                }
                for key, contract in lock["sources"].items()
            },
            "files": {
                key: _artifact_manifest(staging / ARTIFACT_FILES[key], frame)
                for key, frame in frames.items()
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        bundle = validate_housing_stock_assets(staging, source_lock_path=lock_path)
        if output.exists():
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except BaseException:
            if backup.exists():
                os.replace(backup, output)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return bundle
    finally:
        if staging.exists():
            shutil.rmtree(staging)
