from __future__ import annotations

import calendar
import csv
import io
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from .config import RuntimePaths, atomic_write_json, canonical_json, sha256_bytes, sha256_file
from .contracts import HOME_COSTS_COVERAGE_STATUSES
from .errors import HouseHunterError, SourceContractError
from .geography import STATE_BY_FIPS
from .housing_stock import validate_housing_stock_assets

Cancelled = Callable[[], bool]
Progress = Callable[[int, str], None]

RELEASE_SCHEMA = 1
POINTER_SCHEMA = 1
RELEASES_NAME = "releases"
CURRENT_NAME = "current.json"
NORMALIZED_NAME = "home_market.parquet"
MANIFEST_NAME = "manifest.json"
OWNERSHIP_MARKER = ".househunter-owned"
MAX_IMPORT_BYTES = 64 * 1024 * 1024
HOME_MARKET_OUTSIDE_SCOPE = frozenset({"AS", "GU", "MP", "PR", "VI"})

NORMALIZED_COLUMNS = [
    "county_fips",
    "source_county_name",
    "home_median_listing_price",
    "home_median_listing_price_per_square_foot",
    "home_median_square_feet",
    "home_active_listing_count",
    "home_total_listing_count",
    "home_market_month",
    "source_quality_flag",
    "home_sqft_for_1m_unrounded",
    "home_sqft_for_1m",
    "home_buying_power_percentile",
    "home_costs_coverage_status",
]


@dataclass(frozen=True)
class HomeMarketRelease:
    directory: Path
    frame: pl.DataFrame
    manifest: dict[str, Any]
    stale: bool


@lru_cache(maxsize=1)
def canonical_home_market_county_fips() -> frozenset[str]:
    """Return current 50-state/DC county equivalents from the pinned ACS asset."""
    counties = validate_housing_stock_assets().counties["county_fips"]
    return frozenset(
        county
        for county in counties
        if STATE_BY_FIPS.get(county[:2]) not in HOME_MARKET_OUTSIDE_SCOPE
        and county[:2] in STATE_BY_FIPS
    )


def default_release_lock_path() -> Path:
    checkout = Path(__file__).resolve().parents[2] / "config" / "home-market" / "release-lock.json"
    if checkout.is_file():
        return checkout
    packaged = Path(__file__).with_name("assets") / "home_market_release_lock.json"
    if packaged.is_file():
        return packaged
    raise HouseHunterError("Cannot find the home-market release lock")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def load_release_lock(path: Path | None = None) -> dict[str, Any]:
    source = path or default_release_lock_path()
    try:
        payload = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Cannot read home-market release lock: {exc}") from exc
    required_fields = payload.get("required_fields") if isinstance(payload, dict) else None
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or payload.get("automatic_download") is not False
        or payload.get("private_use_only") is not True
        or not isinstance(payload.get("source_name"), str)
        or not isinstance(payload.get("source_page"), str)
        or not isinstance(payload.get("terms_url"), str)
        or not isinstance(payload.get("usage_notice"), str)
        or not isinstance(required_fields, list)
        or len(required_fields) != len(set(required_fields))
        or any(not isinstance(field, str) or not field for field in required_fields)
        or not isinstance(releases, list)
        or not releases
    ):
        raise SourceContractError("Home-market release lock is malformed")
    seen_months: set[str] = set()
    seen_hashes: set[str] = set()
    for release in releases:
        if not isinstance(release, dict):
            raise SourceContractError("Home-market release lock contains a malformed release")
        month = release.get("month")
        digest = release.get("sha256")
        try:
            parsed_month = datetime.strptime(str(month), "%Y-%m").date()
        except ValueError as exc:
            raise SourceContractError("Home-market release lock contains an invalid month") from exc
        expected_month_code = parsed_month.year * 100 + parsed_month.month
        if (
            release.get("month_date_yyyymm") != expected_month_code
            or not isinstance(release.get("expected_filename"), str)
            or Path(release["expected_filename"]).name != release["expected_filename"]
            or not isinstance(release.get("byte_size"), int)
            or not 0 < release["byte_size"] <= MAX_IMPORT_BYTES
            or not _is_sha256(digest)
            or not _is_sha256(release.get("normalized_logical_sha256"))
            or not _is_sha256(release.get("header_sha256"))
            or not isinstance(release.get("row_count"), int)
            or release["row_count"] < 0
            or not isinstance(release.get("quality_flagged_row_count"), int)
            or not 0 <= release["quality_flagged_row_count"] <= release["row_count"]
            or not isinstance(release.get("null_price_per_square_foot_count"), int)
            or not 0 <= release["null_price_per_square_foot_count"] <= release["row_count"]
            or not isinstance(release.get("source_page"), str)
            or not isinstance(release.get("reviewed_on"), str)
        ):
            raise SourceContractError("Home-market release lock contains invalid metadata")
        if month in seen_months or digest in seen_hashes:
            raise SourceContractError("Home-market release lock contains duplicate releases")
        seen_months.add(month)
        seen_hashes.add(digest)
    return payload


def _release_contract(lock: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": lock["schema"],
        "source_name": lock["source_name"],
        "source_page": lock["source_page"],
        "terms_url": lock["terms_url"],
        "private_use_only": lock["private_use_only"],
        "usage_notice": lock["usage_notice"],
        "required_fields": lock["required_fields"],
        "release": release,
    }


def _release_contract_sha(lock: dict[str, Any], release: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(_release_contract(lock, release)))


def _header_with_line_ending(content: bytes) -> bytes:
    header, separator, _ = content.partition(b"\n")
    if not separator:
        raise SourceContractError("Home-market file has no header row")
    return header + separator


def _approved_release(
    source_file: Path, content: bytes, lock: dict[str, Any]
) -> dict[str, Any]:
    digest = sha256_bytes(content)
    matches = [
        release
        for release in lock["releases"]
        if release["expected_filename"] == source_file.name
        and release["byte_size"] == len(content)
        and release["sha256"] == digest
    ]
    if len(matches) != 1:
        raise SourceContractError(
            "Home-market file does not exactly match an approved filename, size, and checksum"
        )
    release = matches[0]
    header_sha = sha256_bytes(_header_with_line_ending(content))
    if header_sha != release["header_sha256"]:
        raise SourceContractError("Home-market header does not match the approved release")
    try:
        header_text = _header_with_line_ending(content).decode("utf-8-sig").rstrip("\r\n")
        header = next(csv.reader([header_text]))
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise SourceContractError(f"Cannot parse home-market header: {exc}") from exc
    if len(header) != len(set(header)):
        raise SourceContractError("Home-market header contains duplicate columns")
    missing = set(lock["required_fields"]) - set(header)
    if missing:
        raise SourceContractError(
            f"Home-market file is missing columns: {', '.join(sorted(missing))}"
        )
    return release


def _read_import_file(source_file: Path, approved_sizes: set[int]) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source_file, flags)
    except OSError as exc:
        raise SourceContractError(f"Cannot open home-market import file: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceContractError("Home-market import path must be a regular file")
        if metadata.st_size not in approved_sizes:
            raise SourceContractError(
                "Home-market file does not match an approved filename and size"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(MAX_IMPORT_BYTES + 1)
        if len(content) != metadata.st_size or len(content) > MAX_IMPORT_BYTES:
            raise SourceContractError("Home-market import file changed or exceeds its size limit")
        return content
    finally:
        os.close(descriptor)


def _finite_or_null(column: str) -> pl.Expr:
    return pl.col(column).is_null() | pl.col(column).is_finite()


def normalize(content: bytes, lock: dict[str, Any], release: dict[str, Any]) -> pl.DataFrame:
    required = lock["required_fields"]
    schema = {
        "month_date_yyyymm": pl.Int64,
        "county_fips": pl.String,
        "county_name": pl.String,
        "median_listing_price": pl.Float64,
        "active_listing_count": pl.Float64,
        "median_listing_price_per_square_foot": pl.Float64,
        "median_square_feet": pl.Float64,
        "total_listing_count": pl.Float64,
        "quality_flag": pl.Int64,
    }
    try:
        raw = pl.read_csv(
            io.BytesIO(content),
            columns=required,
            schema_overrides=schema,
            null_values=["", "NA", "N/A"],
        )
    except pl.exceptions.PolarsError as exc:
        raise SourceContractError(f"Cannot parse home-market file: {exc}") from exc
    if raw.height != release["row_count"]:
        raise SourceContractError(
            "Home-market row count changed: "
            f"expected {release['row_count']}, got {raw.height}"
        )
    if raw["county_fips"].n_unique() != raw.height:
        raise SourceContractError("Home-market county FIPS values are not unique")
    if raw.filter(
        pl.col("county_fips").is_null()
        | ~pl.col("county_fips").str.contains(r"^\d{5}$")
    ).height:
        raise SourceContractError("Home-market file contains invalid county FIPS values")
    if raw.filter(~pl.col("county_fips").is_in(canonical_home_market_county_fips())).height:
        raise SourceContractError(
            "Home-market file contains unknown or out-of-scope county FIPS values"
        )
    blank_county = pl.col("county_name").is_null() | (
        pl.col("county_name").str.strip_chars() == ""
    )
    if raw.filter(blank_county).height:
        raise SourceContractError("Home-market file contains blank county names")
    months = raw["month_date_yyyymm"].drop_nulls().unique().to_list()
    if months != [release["month_date_yyyymm"]] or raw["month_date_yyyymm"].null_count():
        raise SourceContractError(
            "Home-market file must contain exactly the approved release month"
        )
    if raw["quality_flag"].null_count() or raw.filter(~pl.col("quality_flag").is_in([0, 1])).height:
        raise SourceContractError("Home-market quality flags must be 0 or 1")
    flagged = raw.filter(pl.col("quality_flag") != 0).height
    if flagged != release["quality_flagged_row_count"]:
        raise SourceContractError(
            "Home-market quality-flag count changed: "
            f"expected {release['quality_flagged_row_count']}, got {flagged}"
        )
    null_ppsf = raw["median_listing_price_per_square_foot"].null_count()
    if null_ppsf != release["null_price_per_square_foot_count"]:
        raise SourceContractError(
            "Home-market null price-per-square-foot count changed: "
            f"expected {release['null_price_per_square_foot_count']}, got {null_ppsf}"
        )
    numeric_columns = [
        "median_listing_price",
        "active_listing_count",
        "median_listing_price_per_square_foot",
        "median_square_feet",
        "total_listing_count",
    ]
    invalid_finite = pl.any_horizontal([~_finite_or_null(column) for column in numeric_columns])
    if raw.filter(invalid_finite).height:
        raise SourceContractError("Home-market file contains nonfinite numeric values")
    if raw.filter(
        (pl.col("median_listing_price").is_not_null() & (pl.col("median_listing_price") < 0))
        | (pl.col("median_square_feet").is_not_null() & (pl.col("median_square_feet") < 0))
        | (pl.col("active_listing_count").is_not_null() & (pl.col("active_listing_count") < 0))
        | (pl.col("total_listing_count").is_not_null() & (pl.col("total_listing_count") < 0))
    ).height:
        raise SourceContractError("Home-market file contains negative market values")

    eligible = (pl.col("quality_flag") == 0) & (
        pl.col("median_listing_price_per_square_foot").is_not_null()
        & pl.col("median_listing_price_per_square_foot").is_finite()
        & (pl.col("median_listing_price_per_square_foot") > 0)
    )
    frame = raw.with_columns(
        pl.when(eligible)
        .then(1_000_000.0 / pl.col("median_listing_price_per_square_foot"))
        .otherwise(None)
        .alias("home_sqft_for_1m_unrounded"),
        pl.when(pl.col("quality_flag") != 0)
        .then(pl.lit("source_quality_flag"))
        .when(~eligible)
        .then(pl.lit("invalid_price_per_square_foot"))
        .otherwise(pl.lit("complete"))
        .alias("home_costs_coverage_status"),
    )
    eligible_count = frame.filter(pl.col("home_sqft_for_1m_unrounded").is_not_null()).height
    if eligible_count:
        frame = frame.with_columns(
            pl.when(pl.col("home_sqft_for_1m_unrounded").is_not_null())
            .then(
                pl.col("home_sqft_for_1m_unrounded").rank(method="max")
                * 100.0
                / eligible_count
            )
            .otherwise(None)
            .alias("home_buying_power_percentile")
        )
    else:
        frame = frame.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("home_buying_power_percentile")
        )
    frame = (
        frame.with_columns(
            pl.col("county_name").str.strip_chars().alias("source_county_name"),
            pl.col("median_listing_price").alias("home_median_listing_price"),
            pl.col("median_listing_price_per_square_foot").alias(
                "home_median_listing_price_per_square_foot"
            ),
            pl.col("median_square_feet").alias("home_median_square_feet"),
            pl.col("active_listing_count").alias("home_active_listing_count"),
            pl.col("total_listing_count").alias("home_total_listing_count"),
            pl.lit(release["month"]).alias("home_market_month"),
            pl.col("quality_flag").cast(pl.Int8).alias("source_quality_flag"),
            pl.col("home_sqft_for_1m_unrounded").round(0).cast(pl.Int64).alias(
                "home_sqft_for_1m"
            ),
        )
        .select(*NORMALIZED_COLUMNS)
        .sort("county_fips")
    )
    if not set(frame["home_costs_coverage_status"].unique()).issubset(
        HOME_COSTS_COVERAGE_STATUSES
    ):
        raise SourceContractError("Home-market normalization produced an invalid status")
    return frame


def _logical_rows(frame: pl.DataFrame) -> list[list[Any]]:
    return [[row[column] for column in NORMALIZED_COLUMNS] for row in frame.iter_rows(named=True)]


def logical_checksum(frame: pl.DataFrame) -> str:
    return sha256_bytes(canonical_json(_logical_rows(frame)))


def _release_root(paths: RuntimePaths) -> Path:
    return paths.data / "home-market"


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SourceContractError(f"Home-market {label} must be a real directory")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SourceContractError(f"Home-market {label} permissions are not private")


def _require_private_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise SourceContractError(f"Home-market {label} must be a regular file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SourceContractError(f"Home-market {label} permissions are not private")


def _release_by_sha(lock: dict[str, Any], digest: str) -> dict[str, Any]:
    matches = [release for release in lock["releases"] if release["sha256"] == digest]
    if len(matches) != 1:
        raise SourceContractError("Home-market release is no longer approved")
    return matches[0]


def _manifest_for(
    lock: dict[str, Any],
    release: dict[str, Any],
    frame: pl.DataFrame,
    normalized_path: Path,
    imported_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": RELEASE_SCHEMA,
        "source": lock["source_name"],
        "source_page": release["source_page"],
        "terms_url": lock["terms_url"],
        "usage_notice": lock["usage_notice"],
        "private_use_only": True,
        "month": release["month"],
        "source_filename": release["expected_filename"],
        "source_byte_size": release["byte_size"],
        "source_sha256": release["sha256"],
        "source_header_sha256": release["header_sha256"],
        "release_contract_sha256": _release_contract_sha(lock, release),
        "reviewed_on": release["reviewed_on"],
        "imported_at": imported_at.isoformat(),
        "row_count": frame.height,
        "quality_flagged_row_count": frame.filter(pl.col("source_quality_flag") != 0).height,
        "eligible_row_count": frame.filter(
            pl.col("home_sqft_for_1m_unrounded").is_not_null()
        ).height,
        "logical_sha256": logical_checksum(frame),
        "normalized_sha256": sha256_file(normalized_path),
    }


def _parse_manifest(path: Path) -> dict[str, Any]:
    _require_private_file(path, "manifest")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Cannot read home-market manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise SourceContractError("Home-market manifest is malformed")
    return payload


def validate_release_directory(
    directory: Path, lock: dict[str, Any] | None = None
) -> tuple[pl.DataFrame, dict[str, Any]]:
    reviewed = lock or load_release_lock()
    if directory.is_symlink() or not directory.is_dir() or not _is_sha256(directory.name):
        raise SourceContractError("Home-market release path is invalid")
    marker = directory / OWNERSHIP_MARKER
    _require_private_file(marker, "release ownership marker")
    normalized_path = directory / NORMALIZED_NAME
    _require_private_file(normalized_path, "normalized table")
    manifest = _parse_manifest(directory / MANIFEST_NAME)
    release = _release_by_sha(reviewed, directory.name)
    try:
        frame = pl.read_parquet(normalized_path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"Cannot read home-market normalized table: {exc}") from exc
    if frame.columns != NORMALIZED_COLUMNS:
        raise SourceContractError("Home-market normalized schema is invalid")
    if logical_checksum(frame) != release["normalized_logical_sha256"]:
        raise SourceContractError("Home-market normalized checksum differs from the lock")
    try:
        imported_at = datetime.fromisoformat(str(manifest.get("imported_at")))
    except ValueError as exc:
        raise SourceContractError("Home-market manifest has an invalid import time") from exc
    expected_manifest = _manifest_for(
        reviewed, release, frame, normalized_path, imported_at
    )
    mismatches = {
        key: manifest.get(key)
        for key, value in expected_manifest.items()
        if manifest.get(key) != value
    }
    if mismatches:
        raise SourceContractError(f"Home-market manifest does not match its release: {mismatches}")
    invalid_counties = frame.filter(
        pl.col("county_fips").is_null()
        | ~pl.col("county_fips").str.contains(r"^\d{5}$")
        | ~pl.col("county_fips").is_in(canonical_home_market_county_fips())
    )
    if (
        frame.height != release["row_count"]
        or frame["county_fips"].n_unique() != frame.height
        or invalid_counties.height
    ):
        raise SourceContractError("Home-market normalized row count or uniqueness is invalid")
    if (
        frame.filter(pl.col("source_quality_flag") != 0).height
        != release["quality_flagged_row_count"]
        or frame["home_median_listing_price_per_square_foot"].null_count()
        != release["null_price_per_square_foot_count"]
    ):
        raise SourceContractError(
            "Home-market normalized quality-flag count or null-price count differs from the lock"
        )
    if frame["county_fips"].to_list() != sorted(frame["county_fips"].to_list()):
        raise SourceContractError("Home-market normalized counties are not sorted")
    if not set(frame["home_costs_coverage_status"].unique()).issubset(
        HOME_COSTS_COVERAGE_STATUSES
    ):
        raise SourceContractError("Home-market normalized statuses are invalid")
    ppsf = pl.col("home_median_listing_price_per_square_foot")
    expected_eligible = (
        (pl.col("source_quality_flag") == 0)
        & ppsf.is_not_null()
        & ppsf.is_finite()
        & (ppsf > 0)
    )
    expected_status = (
        pl.when(pl.col("source_quality_flag") != 0)
        .then(pl.lit("source_quality_flag"))
        .when(~expected_eligible)
        .then(pl.lit("invalid_price_per_square_foot"))
        .otherwise(pl.lit("complete"))
    )
    if frame.filter(
        pl.col("source_quality_flag").is_null()
        | ~pl.col("source_quality_flag").is_in([0, 1])
        | (
            ppsf.is_not_null()
            & (~ppsf.is_finite())
        )
        | (pl.col("home_costs_coverage_status") != expected_status)
    ).height:
        raise SourceContractError("Home-market eligibility or status contract is invalid")
    eligible = frame.filter(expected_eligible)
    if eligible.height:
        recalculated = eligible.with_columns(
            (1_000_000.0 / pl.col("home_median_listing_price_per_square_foot")).alias(
                "expected_sqft"
            ),
            (
                pl.col("home_sqft_for_1m_unrounded").rank(method="max")
                * 100.0
                / eligible.height
            ).alias("expected_percentile"),
        ).with_columns(
            pl.col("expected_sqft").round(0).cast(pl.Int64).alias("expected_rounded")
        )
        missing_derived = pl.any_horizontal(
            [
                pl.col("home_sqft_for_1m_unrounded").is_null(),
                pl.col("home_sqft_for_1m").is_null(),
                pl.col("home_buying_power_percentile").is_null(),
            ]
        )
        wrong_derived = (
            (
                pl.col("expected_sqft") - pl.col("home_sqft_for_1m_unrounded")
            ).abs()
            > 1e-9
        ) | (
            pl.col("expected_rounded") != pl.col("home_sqft_for_1m")
        ) | (
            (
                pl.col("expected_percentile")
                - pl.col("home_buying_power_percentile")
            ).abs()
            > 1e-9
        )
        if recalculated.filter(missing_derived | wrong_derived).height:
            raise SourceContractError("Home-market derived values are invalid")
    rejected = frame.filter(~expected_eligible)
    if rejected.filter(
        pl.col("home_sqft_for_1m_unrounded").is_not_null()
        | pl.col("home_sqft_for_1m").is_not_null()
        | pl.col("home_buying_power_percentile").is_not_null()
    ).height:
        raise SourceContractError("Rejected home-market rows contain ranking values")
    return frame, manifest


def _write_release(
    paths: RuntimePaths,
    lock: dict[str, Any],
    release: dict[str, Any],
    frame: pl.DataFrame,
    cancelled: Cancelled | None,
) -> Path:
    root = _release_root(paths)
    if root.exists() or root.is_symlink():
        _require_real_directory(root, "root")
    else:
        root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    releases = root / RELEASES_NAME
    if releases.exists() or releases.is_symlink():
        _require_real_directory(releases, "releases directory")
    else:
        releases.mkdir(mode=0o700)
    releases.chmod(0o700)
    digest = release["sha256"]
    destination = releases / digest
    if destination.exists() or destination.is_symlink():
        validate_release_directory(destination, lock)
        return destination
    temporary = releases / f".{uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    marker = temporary / OWNERSHIP_MARKER
    marker.write_text("HouseHunter private home-market release\n")
    marker.chmod(0o600)
    try:
        normalized_path = temporary / NORMALIZED_NAME
        frame.write_parquet(normalized_path, compression="zstd", statistics=True)
        normalized_path.chmod(0o600)
        manifest = _manifest_for(lock, release, frame, normalized_path, datetime.now(UTC))
        atomic_write_json(temporary / MANIFEST_NAME, manifest)
        (temporary / MANIFEST_NAME).chmod(0o600)
        try:
            staged_frame = pl.read_parquet(normalized_path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise SourceContractError(f"Cannot verify staged home-market release: {exc}") from exc
        if (
            staged_frame.columns != NORMALIZED_COLUMNS
            or logical_checksum(staged_frame) != manifest["logical_sha256"]
            or sha256_file(normalized_path) != manifest["normalized_sha256"]
        ):
            raise SourceContractError("Home-market staged release validation failed")
        if cancelled and cancelled():
            raise InterruptedError("Home-market import cancelled")
        os.replace(temporary, destination)
        validate_release_directory(destination, lock)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _pointer_release(root: Path) -> str | None:
    pointer = root / CURRENT_NAME
    if not pointer.exists() and not pointer.is_symlink():
        return None
    _require_private_file(pointer, "current pointer")
    try:
        payload = json.loads(pointer.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Cannot read home-market current pointer: {exc}") from exc
    release_sha = payload.get("release_sha256") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != POINTER_SCHEMA
        or not _is_sha256(release_sha)
    ):
        raise SourceContractError("Home-market current pointer is malformed")
    return release_sha


def _month_for_release(lock: dict[str, Any], digest: str) -> str:
    return str(_release_by_sha(lock, digest)["month"])


def import_home_market(
    paths: RuntimePaths,
    source_file: Path,
    *,
    acknowledge_personal_use: bool,
    release_lock_path: Path | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> Path:
    if not acknowledge_personal_use:
        raise HouseHunterError(
            "Home-market import requires --acknowledge-personal-use"
        )
    lock = load_release_lock(release_lock_path)
    if source_file.is_symlink() or not source_file.is_file():
        raise SourceContractError("Home-market import path must be a regular file")
    approved_sizes = {
        int(release["byte_size"])
        for release in lock["releases"]
        if release["expected_filename"] == source_file.name
    }
    if not approved_sizes:
        raise SourceContractError(
            "Home-market file does not match an approved filename and size"
        )
    if cancelled and cancelled():
        raise InterruptedError("Home-market import cancelled")
    if progress:
        progress(10, "Verifying approved home-market release")
    content = _read_import_file(source_file, approved_sizes)
    release = _approved_release(source_file, content, lock)
    if progress:
        progress(35, "Normalizing home-market county rows")
    frame = normalize(content, lock, release)
    if logical_checksum(frame) != release["normalized_logical_sha256"]:
        raise SourceContractError("Home-market normalized checksum differs from the lock")
    if cancelled and cancelled():
        raise InterruptedError("Home-market import cancelled")
    destination = _write_release(paths, lock, release, frame, cancelled)
    root = _release_root(paths)
    current_sha = _pointer_release(root)
    candidate_sha = str(release["sha256"])
    selected_sha = candidate_sha
    if current_sha is not None:
        current_directory = root / RELEASES_NAME / current_sha
        validate_release_directory(current_directory, lock)
        if _month_for_release(lock, current_sha) > release["month"]:
            selected_sha = current_sha
    if cancelled and cancelled():
        raise InterruptedError("Home-market import cancelled")
    atomic_write_json(
        root / CURRENT_NAME,
        {"schema_version": POINTER_SCHEMA, "release_sha256": selected_sha},
    )
    (root / CURRENT_NAME).chmod(0o600)
    if progress:
        try:
            progress(100, "Home-market release imported for personal local use")
        except InterruptedError:
            if not cancelled or not cancelled():
                raise
    return destination


def stale_after(month: str) -> date:
    parsed = datetime.strptime(month, "%Y-%m").date()
    month_end = date(parsed.year, parsed.month, calendar.monthrange(parsed.year, parsed.month)[1])
    return month_end + timedelta(days=62)


def is_stale(month: str, *, today: date | None = None) -> bool:
    return (today or datetime.now(UTC).date()) > stale_after(month)


def load_current_release(
    paths: RuntimePaths,
    *,
    release_lock_path: Path | None = None,
    today: date | None = None,
) -> HomeMarketRelease | None:
    root = _release_root(paths)
    if not root.exists() and not root.is_symlink():
        return None
    _require_real_directory(root, "root")
    release_sha = _pointer_release(root)
    if release_sha is None:
        return None
    lock = load_release_lock(release_lock_path)
    releases = root / RELEASES_NAME
    _require_real_directory(releases, "releases directory")
    directory = releases / release_sha
    frame, manifest = validate_release_directory(directory, lock)
    return HomeMarketRelease(
        directory=directory,
        frame=frame,
        manifest=manifest,
        stale=is_stale(str(manifest["month"]), today=today),
    )


def source_status(
    paths: RuntimePaths,
    *,
    release_lock_path: Path | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    lock = load_release_lock(release_lock_path)
    try:
        current = load_current_release(paths, release_lock_path=release_lock_path, today=today)
        if current is None:
            return {
                "source": "home_market",
                "cached": False,
                "release": None,
                "stale": None,
                "sha256": None,
                "row_count": None,
                "attribution": lock["source_page"],
                "usage_notice": lock["usage_notice"],
                "error": None,
            }
        return {
            "source": "home_market",
            "cached": True,
            "release": current.manifest["month"],
            "stale": current.stale,
            "sha256": current.manifest["source_sha256"],
            "row_count": current.manifest["row_count"],
            "attribution": current.manifest["source_page"],
            "usage_notice": current.manifest["usage_notice"],
            "error": None,
        }
    except (HouseHunterError, OSError, KeyError, TypeError, ValueError) as exc:
        return {
            "source": "home_market",
            "cached": True,
            "release": None,
            "stale": None,
            "sha256": None,
            "row_count": None,
            "attribution": lock["source_page"],
            "usage_notice": lock["usage_notice"],
            "error": str(exc),
        }
