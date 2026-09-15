from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

import polars as pl

from .config import canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .secure_fetch import deny_restricted_data_class

BUNDLE_SCHEMA_VERSION = 1
METHODOLOGY_ID = "top-counties-v2"
CALIBRATION_ID = "ranking-calibration-v2"
HOMESCHOOL_NOTICE = (
    "Homeschool-policy fit is a HouseHunter state-policy rubric, not legal advice, "
    "not a school-quality score, and not a recommendation to homeschool."
)
CRIME_COVERAGE_FLOOR = 0.90
CONNECTICUT_CURRENT_FIPS_PREFIX = "09"
IN_SCOPE_STATE_FIPS = frozenset(
    code for code, state in STATE_BY_FIPS.items() if state not in {"AS", "GU", "MP", "PR", "VI"}
)
PILLAR_INTERNALS = {
    "safety": {"hazard": 0.50, "crime": 0.35, "water": 0.15},
    "health": {"healthcare": 0.60, "community_context": 0.40},
    "affordability": {"housing": 0.45, "rpp": 0.35, "property_tax": 0.20},
    "opportunity": {"employment": 0.60, "broadband": 0.40},
    "lifestyle": {"mountain": 1.00},
    "family": {"homeschool": 1.00},
}
COUNTY_COLUMNS = [
    "county_fips",
    "state",
    "population",
    "population_vintage",
    "res_hazard_npctl",
    "u_hazard",
    "crime_violent_rate",
    "crime_property_rate",
    "crime_coverage",
    "u_crime",
    "water_violation_share",
    "private_well_share",
    "u_water",
    "u_safety",
    "provider_primary_care",
    "provider_mental_health",
    "provider_dental",
    "u_healthcare",
    "community_context",
    "community_context_kind",
    "u_community_context",
    "u_health",
    "rpp_index",
    "rpp_geography_type",
    "u_rpp",
    "property_tax_rate",
    "u_property_tax",
    "employment_opportunity",
    "u_employment",
    "broadband_100_20",
    "u_broadband",
    "u_opportunity",
    "mountain_magnitude",
    "u_mountain",
    "u_lifestyle",
    "homeschool_utility",
    "u_family",
    "jan_avg_temp_f",
    "jul_avg_temp_f",
    "extreme_heat_days",
    "extreme_cold_days",
    "climate_coverage_status",
    "coverage_status",
]
UTILITY_COLUMNS = [
    "u_hazard",
    "u_crime",
    "u_water",
    "u_safety",
    "u_healthcare",
    "u_community_context",
    "u_health",
    "u_rpp",
    "u_property_tax",
    "u_employment",
    "u_broadband",
    "u_opportunity",
    "u_mountain",
    "u_lifestyle",
    "homeschool_utility",
    "u_family",
]
CORE_UTILITY_COLUMNS = [
    "u_safety",
    "u_health",
    "u_opportunity",
    "u_lifestyle",
    "u_family",
]
RANKING_SIDECAR_COLUMNS = [
    "place_id",
    "name",
    "state",
    *COUNTY_COLUMNS[2:],
    "housing_valid_months",
    "median_active_listings",
    "median_ppsf",
    "sqft_for_1m_t12",
    "u_housing",
    "u_affordability",
]
BUNDLED_RANKING_V2 = Path(__file__).with_name("assets") / "ranking_v2"
ARTIFACT_FILES = {
    "counties": "counties.parquet",
    "calibration": "calibration.json",
    "homeschool": "homeschool_policy.json",
    "citations": "citations.json",
}


@dataclass(frozen=True)
class RankingBundle:
    manifest: dict[str, Any]
    counties: pl.DataFrame
    calibration: dict[str, Any]
    homeschool: dict[str, Any]
    citations: dict[str, Any]


def default_source_lock_path() -> Path:
    checkout = Path(__file__).resolve().parents[2] / "config" / "ranking" / "source-lock-v2.json"
    if checkout.is_file():
        return checkout
    packaged = Path(__file__).with_name("assets") / "ranking_source_lock_v2.json"
    if packaged.is_file():
        return packaged
    raise HouseHunterError("Cannot find the ranking v2 source lock")


def default_homeschool_path() -> Path:
    checkout = (
        Path(__file__).resolve().parents[2] / "config" / "ranking" / "homeschool-policy-v1.json"
    )
    if checkout.is_file():
        return checkout
    packaged = Path(__file__).with_name("assets") / "homeschool_policy_v1.json"
    if packaged.is_file():
        return packaged
    raise HouseHunterError("Cannot find the homeschool policy rubric")


def default_appalachia_path() -> Path:
    checkout = (
        Path(__file__).resolve().parents[2] / "config" / "ranking" / "appalachia-counties.json"
    )
    if checkout.is_file():
        return checkout
    packaged = Path(__file__).with_name("assets") / "appalachia_counties.json"
    if packaged.is_file():
        return packaged
    raise HouseHunterError("Cannot find the Appalachia county list")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return number


def load_source_lock(path: Path | None = None) -> dict[str, Any]:
    source = path or default_source_lock_path()
    try:
        lock = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read ranking source lock: {source}: {exc}") from exc
    if (
        not isinstance(lock, dict)
        or lock.get("schema") != 1
        or lock.get("methodology_id") != METHODOLOGY_ID
        or not isinstance(lock.get("allowed_hosts"), list)
        or not lock["allowed_hosts"]
        or not isinstance(lock.get("sources"), list)
    ):
        raise HouseHunterError(f"Unsupported ranking source lock: {source}")
    allowed = {str(host).lower() for host in lock["allowed_hosts"]}
    if any(host != host.lower() for host in lock["allowed_hosts"]):
        raise HouseHunterError("Ranking source lock host allowlist must be lowercase")
    seen_names: set[str] = set()
    for item in lock["sources"]:
        if not isinstance(item, dict):
            raise HouseHunterError("Ranking source lock contains a malformed source")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in seen_names:
            raise HouseHunterError("Ranking source lock contains duplicate or blank source names")
        seen_names.add(name)
        deny_restricted_data_class(item.get("data_class"), label=name)
        if item.get("runtime_fetch") is not False:
            raise HouseHunterError(f"Ranking source {name} must not be fetched at runtime")
        if item.get("package_raw") is not False:
            raise HouseHunterError(f"Ranking source {name} must not package raw downloads")
        hosts = item.get("allowed_hosts", lock["allowed_hosts"])
        if any(str(host).lower() not in allowed for host in hosts):
            raise HouseHunterError(f"Ranking source {name} host is not in the reviewed allowlist")
    return lock


def load_homeschool_policy(path: Path | None = None) -> dict[str, Any]:
    source = path or default_homeschool_path()
    try:
        payload = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read homeschool policy: {exc}") from exc
    jurisdictions = payload.get("jurisdictions") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or payload.get("notice") != HOMESCHOOL_NOTICE
        or not isinstance(jurisdictions, dict)
    ):
        raise HouseHunterError("Homeschool policy rubric is malformed")
    expected = {
        state for code, state in STATE_BY_FIPS.items() if code in IN_SCOPE_STATE_FIPS
    }
    if set(jurisdictions) != expected:
        raise HouseHunterError("Homeschool policy must cover every in-scope state and DC")
    for state, row in jurisdictions.items():
        utility = _finite(row.get("utility") if isinstance(row, dict) else None)
        citations = row.get("citations") if isinstance(row, dict) else None
        if (
            utility is None
            or not 0 <= utility <= 1
            or not isinstance(citations, list)
            or not citations
            or not isinstance(row.get("reviewed_on"), str)
            or not isinstance(row.get("effective_date"), str)
        ):
            raise HouseHunterError(f"Homeschool policy for {state} is incomplete")
    return payload


def load_appalachia_counties(path: Path | None = None) -> frozenset[str]:
    source = path or default_appalachia_path()
    try:
        payload = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Appalachia county list: {exc}") from exc
    counties = payload.get("county_fips") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(counties, list) or not counties:
        raise HouseHunterError("Appalachia county list is malformed")
    fips = []
    for value in counties:
        if not isinstance(value, str) or len(value) != 5 or not value.isdigit():
            raise HouseHunterError("Appalachia county list contains an invalid FIPS")
        fips.append(value)
    if len(fips) != len(set(fips)):
        raise HouseHunterError("Appalachia county list contains duplicates")
    return frozenset(fips)


def average_tie_percentile(values: Sequence[float], *, invert: bool = False) -> list[float]:
    count = len(values)
    if count == 0:
        return []
    if count == 1:
        return [1.0]
    indexed = sorted(enumerate(values), key=lambda item: item[1], reverse=invert)
    ranks = [0.0] * count
    index = 0
    while index < count:
        end = index
        while end + 1 < count and indexed[end + 1][1] == indexed[index][1]:
            end += 1
        average = (index + 1 + end + 1) / 2
        for position in range(index, end + 1):
            ranks[indexed[position][0]] = average
        index = end + 1
    return [(rank - 1) / (count - 1) for rank in ranks]


def closed_unit_interval(value: float, low: float, high: float, *, invert: bool = False) -> float:
    if high == low:
        return 1.0
    score = (value - low) / (high - low)
    if invert:
        score = 1.0 - score
    return max(0.0, min(1.0, score))


def interpolate_knots(value: float, knots: Sequence[Mapping[str, float]]) -> float:
    if not knots:
        raise HouseHunterError("Calibration knots are missing")
    ordered = sorted(knots, key=lambda item: item["value"])
    if value <= ordered[0]["value"]:
        return float(ordered[0]["cdf"])
    if value >= ordered[-1]["value"]:
        return float(ordered[-1]["cdf"])
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left["value"] <= value <= right["value"]:
            span = right["value"] - left["value"]
            if span == 0:
                return float(left["cdf"])
            mix = (value - left["value"]) / span
            return float(left["cdf"] + mix * (right["cdf"] - left["cdf"]))
    return float(ordered[-1]["cdf"])


def calibration_hash(calibration: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in calibration.items() if key != "hash"}
    return sha256_bytes(canonical_json(payload))


def ranking_bundle_identity(manifest: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json(
            {
                "calibration_hash": manifest.get("calibration_hash"),
                "scope": manifest.get("scope"),
                "source_lock_sha256": manifest.get("source_lock_sha256"),
                "files": manifest.get("files"),
            }
        )
    )


def locked_pillar_internals(calibration: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    internals = calibration.get("pillar_internals")
    if internals != PILLAR_INTERNALS:
        raise HouseHunterError("Calibration pillar internals differ from the locked v2 formula")
    return PILLAR_INTERNALS


def pillar_utility(
    values: Mapping[str, float | None], internals: Mapping[str, float]
) -> float | None:
    total = 0.0
    for name, weight in internals.items():
        number = values.get(name)
        if number is None or not isfinite(number):
            return None
        total += weight * number
    return total


def _validate_utilities(frame: pl.DataFrame) -> None:
    for column in UTILITY_COLUMNS:
        invalid = frame.filter(
            pl.col(column).is_not_null()
            & (~pl.col(column).is_finite() | (pl.col(column) < 0) | (pl.col(column) > 1))
        )
        if invalid.height:
            raise HouseHunterError(f"Ranking bundle utility {column} is outside [0, 1]")


def _require_precomputed_pillars(frame: pl.DataFrame) -> None:
    for row in frame.iter_rows(named=True):
        expected = {
            "u_safety": pillar_utility(
                {
                    "hazard": _finite(row["u_hazard"]),
                    "crime": _finite(row["u_crime"]),
                    "water": _finite(row["u_water"]),
                },
                PILLAR_INTERNALS["safety"],
            ),
            "u_health": pillar_utility(
                {
                    "healthcare": _finite(row["u_healthcare"]),
                    "community_context": _finite(row["u_community_context"]),
                },
                PILLAR_INTERNALS["health"],
            ),
            "u_opportunity": pillar_utility(
                {
                    "employment": _finite(row["u_employment"]),
                    "broadband": _finite(row["u_broadband"]),
                },
                PILLAR_INTERNALS["opportunity"],
            ),
            "u_lifestyle": _finite(row["u_mountain"]),
            "u_family": _finite(row["homeschool_utility"]),
        }
        for column, value in expected.items():
            observed = _finite(row[column])
            if value is None:
                if observed is not None:
                    raise HouseHunterError(
                        f"Ranking bundle {column} is present without its core inputs"
                    )
                continue
            if observed is None or abs(value - observed) > 1e-9:
                raise HouseHunterError(
                    f"Ranking bundle {column} drifted from its component utilities"
                )


def _reject_zeroed_suppression(frame: pl.DataFrame) -> None:
    scored_below_floor = frame.filter(
        pl.col("u_crime").is_not_null()
        & (
            pl.col("crime_coverage").is_null()
            | (pl.col("crime_coverage") < CRIME_COVERAGE_FLOOR)
        )
    )
    if scored_below_floor.height:
        raise HouseHunterError("Ranking bundle scores crime below the 90% coverage floor")
    scored_without_rates = frame.filter(
        pl.col("u_crime").is_not_null()
        & (pl.col("crime_violent_rate").is_null() | pl.col("crime_property_rate").is_null())
    )
    if scored_without_rates.height:
        raise HouseHunterError("Ranking bundle must not treat suppressed crime as scored")


def _validate_counties(frame: pl.DataFrame, *, scope: str) -> None:
    if frame.columns != COUNTY_COLUMNS:
        raise HouseHunterError("Ranking bundle county schema differs")
    fips = frame["county_fips"].to_list()
    if fips != sorted(set(fips)):
        raise HouseHunterError("Ranking bundle counties are not sorted and unique")
    if frame.filter(~pl.col("county_fips").str.contains(r"^\d{5}$")).height:
        raise HouseHunterError("Ranking bundle contains invalid county FIPS")
    connecticut = frame.filter(
        pl.col("county_fips").str.starts_with(CONNECTICUT_CURRENT_FIPS_PREFIX)
    )
    if connecticut.filter(~pl.col("state").eq("CT")).height:
        raise HouseHunterError("Connecticut FIPS must not use an invented crosswalk")
    unknown_state = frame.filter(
        ~pl.col("county_fips").str.slice(0, 2).is_in(list(IN_SCOPE_STATE_FIPS))
    )
    if unknown_state.height:
        raise HouseHunterError("Ranking bundle contains out-of-scope county FIPS")
    if frame.filter(pl.col("population").is_null() | (pl.col("population") <= 0)).height:
        raise HouseHunterError("Ranking bundle population must be a positive estimate")
    if not set(frame["rpp_geography_type"].unique()) <= {"metropolitan", "state"}:
        raise HouseHunterError("Ranking bundle RPP geography must be metropolitan or state")
    if frame.filter(
        pl.col("rpp_geography_type").eq("state") & pl.col("rpp_index").is_null()
    ).height:
        raise HouseHunterError("State RPP assignment left values null")
    _validate_utilities(frame)
    _reject_zeroed_suppression(frame)
    _require_precomputed_pillars(frame)
    if scope == "national" and frame.height < 3000:
        raise HouseHunterError("National ranking bundle county count is too small")
    if scope == "fixture" and frame.height < 2:
        raise HouseHunterError("Fixture ranking bundle must contain at least two counties")


def validate_ranking_assets(
    directory: Path | None = None,
    *,
    source_lock_path: Path | None = None,
) -> RankingBundle:
    root = directory or BUNDLED_RANKING_V2
    if root.is_symlink() or not root.is_dir():
        raise HouseHunterError("Ranking v2 bundle must be a real directory")
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read ranking v2 manifest: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
        or manifest.get("methodology_id") != METHODOLOGY_ID
        or manifest.get("calibration_id") != CALIBRATION_ID
        or manifest.get("scope") not in {"national", "fixture"}
    ):
        raise HouseHunterError("Ranking v2 manifest is incompatible")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise HouseHunterError("Ranking v2 manifest is missing file contracts")
    frames: dict[str, Any] = {}
    for key, filename in ARTIFACT_FILES.items():
        record = files.get(key)
        path = root / filename
        if not isinstance(record, dict) or record.get("filename") != filename:
            raise HouseHunterError(f"Ranking v2 manifest is missing {key}")
        if (
            not path.is_file()
            or path.stat().st_size != record.get("bytes")
            or sha256_file(path) != record.get("sha256")
        ):
            raise HouseHunterError(f"Ranking v2 artifact differs for {key}")
        if key == "counties":
            try:
                frames[key] = pl.read_parquet(path)
            except Exception as exc:
                raise HouseHunterError(f"Cannot read ranking counties: {exc}") from exc
            if frames[key].height != record.get("rows"):
                raise HouseHunterError("Ranking v2 county row count differs")
        else:
            try:
                frames[key] = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise HouseHunterError(f"Cannot read ranking {key}: {exc}") from exc
    calibration = frames["calibration"]
    if calibration_hash(calibration) != manifest.get("calibration_hash"):
        raise HouseHunterError("Ranking v2 calibration hash differs from the manifest")
    if calibration.get("id") != CALIBRATION_ID:
        raise HouseHunterError("Ranking v2 calibration identity differs")
    _validate_counties(frames["counties"], scope=str(manifest["scope"]))
    if source_lock_path is not None and sha256_file(source_lock_path) != manifest.get(
        "source_lock_sha256"
    ):
        raise HouseHunterError("Ranking v2 source lock hash differs")
    homeschool = frames["homeschool"]
    if homeschool.get("notice") != HOMESCHOOL_NOTICE:
        raise HouseHunterError("Ranking v2 homeschool notice is missing")
    return RankingBundle(
        manifest=manifest,
        counties=frames["counties"],
        calibration=calibration,
        homeschool=homeschool,
        citations=frames["citations"],
    )


def _artifact_manifest(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    payload = {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if rows is not None:
        payload["rows"] = rows
    return payload


def write_ranking_bundle(
    output: Path,
    counties: pl.DataFrame,
    *,
    calibration: dict[str, Any],
    homeschool: dict[str, Any],
    citations: dict[str, Any],
    source_lock_path: Path | None = None,
    scope: str = "fixture",
    vintages: dict[str, Any] | None = None,
) -> RankingBundle:
    if scope not in {"national", "fixture"}:
        raise HouseHunterError("Ranking bundle scope must be national or fixture")
    _validate_counties(counties, scope=scope)
    calibration_payload = dict(calibration)
    calibration_payload["id"] = CALIBRATION_ID
    digest = calibration_hash(calibration_payload)
    calibration_payload["hash"] = digest
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid.uuid4().hex}.staging"
    backup = output.parent / f".{output.name}.{uuid.uuid4().hex}.backup"
    staging.mkdir()
    try:
        counties.write_parquet(
            staging / ARTIFACT_FILES["counties"], compression="zstd", statistics=True
        )
        (staging / ARTIFACT_FILES["calibration"]).write_text(
            json.dumps(calibration_payload, indent=2, sort_keys=True) + "\n"
        )
        (staging / ARTIFACT_FILES["homeschool"]).write_text(
            json.dumps(homeschool, indent=2, sort_keys=True) + "\n"
        )
        (staging / ARTIFACT_FILES["citations"]).write_text(
            json.dumps(citations, indent=2, sort_keys=True) + "\n"
        )
        lock_path = source_lock_path or default_source_lock_path()
        lock_sha = (
            sha256_file(lock_path) if lock_path.is_file() else sha256_bytes(b"unlocked-fixture")
        )
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "methodology_id": METHODOLOGY_ID,
            "calibration_id": CALIBRATION_ID,
            "calibration_hash": digest,
            "scope": scope,
            "source_lock_sha256": lock_sha,
            "vintages": vintages or {},
            "files": {
                "counties": _artifact_manifest(
                    staging / ARTIFACT_FILES["counties"], rows=counties.height
                ),
                "calibration": _artifact_manifest(staging / ARTIFACT_FILES["calibration"]),
                "homeschool": _artifact_manifest(staging / ARTIFACT_FILES["homeschool"]),
                "citations": _artifact_manifest(staging / ARTIFACT_FILES["citations"]),
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        validate_ranking_assets(staging)
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
        return validate_ranking_assets(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def empty_ranking_sidecar() -> pl.DataFrame:
    schema = {
        "place_id": pl.String,
        "name": pl.String,
        "state": pl.String,
        "population": pl.Int64,
        "population_vintage": pl.String,
        "res_hazard_npctl": pl.Float64,
        "u_hazard": pl.Float64,
        "crime_violent_rate": pl.Float64,
        "crime_property_rate": pl.Float64,
        "crime_coverage": pl.Float64,
        "u_crime": pl.Float64,
        "water_violation_share": pl.Float64,
        "private_well_share": pl.Float64,
        "u_water": pl.Float64,
        "u_safety": pl.Float64,
        "provider_primary_care": pl.Float64,
        "provider_mental_health": pl.Float64,
        "provider_dental": pl.Float64,
        "u_healthcare": pl.Float64,
        "community_context": pl.Float64,
        "community_context_kind": pl.String,
        "u_community_context": pl.Float64,
        "u_health": pl.Float64,
        "rpp_index": pl.Float64,
        "rpp_geography_type": pl.String,
        "u_rpp": pl.Float64,
        "property_tax_rate": pl.Float64,
        "u_property_tax": pl.Float64,
        "employment_opportunity": pl.Float64,
        "u_employment": pl.Float64,
        "broadband_100_20": pl.Float64,
        "u_broadband": pl.Float64,
        "u_opportunity": pl.Float64,
        "mountain_magnitude": pl.Float64,
        "u_mountain": pl.Float64,
        "u_lifestyle": pl.Float64,
        "homeschool_utility": pl.Float64,
        "u_family": pl.Float64,
        "jan_avg_temp_f": pl.Float64,
        "jul_avg_temp_f": pl.Float64,
        "extreme_heat_days": pl.Float64,
        "extreme_cold_days": pl.Float64,
        "climate_coverage_status": pl.String,
        "coverage_status": pl.String,
        "housing_valid_months": pl.Int64,
        "median_active_listings": pl.Float64,
        "median_ppsf": pl.Float64,
        "sqft_for_1m_t12": pl.Float64,
        "u_housing": pl.Float64,
        "u_affordability": pl.Float64,
    }
    return pl.DataFrame({name: [] for name in schema}, schema=schema)


def housing_utility(sqft: float | None, calibration: Mapping[str, Any]) -> float | None:
    number = _finite(sqft)
    if number is None:
        return None
    knots = calibration.get("housing_sqft_knots")
    if isinstance(knots, list) and knots:
        return interpolate_knots(number, knots)
    bounds = calibration.get("housing_sqft_bounds")
    if isinstance(bounds, dict):
        return closed_unit_interval(number, float(bounds["low"]), float(bounds["high"]))
    return None


def assemble_ranking_sidecar(
    bundle: RankingBundle,
    identity: pl.DataFrame,
    housing: pl.DataFrame,
) -> pl.DataFrame:
    required_identity = {"place_id", "name", "state"}
    if not required_identity <= set(identity.columns):
        raise HouseHunterError("Ranking identity table is missing county fields")
    counties = bundle.counties.rename({"county_fips": "place_id"})
    identity_ids = set(identity["place_id"].to_list())
    bundle_ids = set(counties["place_id"].to_list())
    connecticut_missing = {
        fips for fips in identity_ids if fips.startswith("09") and fips not in bundle_ids
    }
    if connecticut_missing:
        raise HouseHunterError("Connecticut FIPS must not use an invented crosswalk")
    if bundle.manifest.get("scope") == "national" and identity_ids - bundle_ids:
        raise HouseHunterError("Ranking bundle is missing snapshot counties")
    joined = identity.select("place_id", "name", "state").join(
        counties, on="place_id", how="inner", validate="1:1"
    )
    if "state_right" in joined.columns:
        joined = joined.drop("state_right")
    if housing.height:
        if housing["county_fips"].n_unique() != housing.height:
            raise HouseHunterError("Trailing home-market metrics duplicated a county FIPS")
        joined = joined.join(
            housing, left_on="place_id", right_on="county_fips", how="left", validate="m:1"
        )
    else:
        joined = joined.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("housing_valid_months"),
            pl.lit(None, dtype=pl.Float64).alias("median_active_listings"),
            pl.lit(None, dtype=pl.Float64).alias("median_ppsf"),
            pl.lit(None, dtype=pl.Float64).alias("sqft_for_1m_t12"),
        )
    housing_scores = [
        housing_utility(value, bundle.calibration)
        for value in joined["sqft_for_1m_t12"].to_list()
    ]
    joined = joined.with_columns(pl.Series("u_housing", housing_scores, dtype=pl.Float64))
    internals = locked_pillar_internals(bundle.calibration)
    affordability = [
        pillar_utility(
            {
                "housing": _finite(row["u_housing"]),
                "rpp": _finite(row["u_rpp"]),
                "property_tax": _finite(row["u_property_tax"]),
            },
            internals["affordability"],
        )
        for row in joined.iter_rows(named=True)
    ]
    joined = joined.with_columns(pl.Series("u_affordability", affordability, dtype=pl.Float64))
    missing = [name for name in RANKING_SIDECAR_COLUMNS if name not in joined.columns]
    if missing:
        raise HouseHunterError(f"Ranking sidecar is missing columns: {', '.join(missing)}")
    return joined.select(RANKING_SIDECAR_COLUMNS).sort("place_id")


def synthetic_fixture_rows() -> list[dict[str, Any]]:
    """Two in-scope counties used by HouseHunter tests: Autauga AL and Aleutians East AK."""
    better = {
        "county_fips": "02001",
        "state": "AK",
        "population": 80_000,
        "population_vintage": "2024",
        "res_hazard_npctl": 10.0,
        "u_hazard": 0.90,
        "crime_violent_rate": 100.0,
        "crime_property_rate": 800.0,
        "crime_coverage": 0.95,
        "u_crime": 0.85,
        "water_violation_share": 0.01,
        "private_well_share": 0.10,
        "u_water": 0.90,
        "provider_primary_care": 90.0,
        "provider_mental_health": 40.0,
        "provider_dental": 70.0,
        "u_healthcare": 0.80,
        "community_context": 0.80,
        "community_context_kind": "reconstructed",
        "u_community_context": 0.80,
        "rpp_index": 90.0,
        "rpp_geography_type": "state",
        "u_rpp": 0.80,
        "property_tax_rate": 0.008,
        "u_property_tax": 0.80,
        "employment_opportunity": 0.70,
        "u_employment": 0.70,
        "broadband_100_20": 0.90,
        "u_broadband": 0.90,
        "mountain_magnitude": 4.0,
        "u_mountain": 1.0,
        "homeschool_utility": 0.95,
        "jan_avg_temp_f": 20.0,
        "jul_avg_temp_f": 55.0,
        "extreme_heat_days": 0.0,
        "extreme_cold_days": 40.0,
        "climate_coverage_status": "complete",
        "coverage_status": "complete",
    }
    worse = {
        "county_fips": "01001",
        "state": "AL",
        "population": 58_000,
        "population_vintage": "2024",
        "res_hazard_npctl": 70.0,
        "u_hazard": 0.30,
        "crime_violent_rate": 400.0,
        "crime_property_rate": 2200.0,
        "crime_coverage": 0.92,
        "u_crime": 0.40,
        "water_violation_share": 0.08,
        "private_well_share": 0.20,
        "u_water": 0.40,
        "provider_primary_care": 40.0,
        "provider_mental_health": 10.0,
        "provider_dental": 30.0,
        "u_healthcare": 0.35,
        "community_context": 0.40,
        "community_context_kind": "reconstructed",
        "u_community_context": 0.40,
        "rpp_index": 110.0,
        "rpp_geography_type": "metropolitan",
        "u_rpp": 0.30,
        "property_tax_rate": 0.012,
        "u_property_tax": 0.40,
        "employment_opportunity": 0.40,
        "u_employment": 0.40,
        "broadband_100_20": 0.70,
        "u_broadband": 0.70,
        "mountain_magnitude": 0.2,
        "u_mountain": 0.10,
        "homeschool_utility": 0.82,
        "jan_avg_temp_f": 45.0,
        "jul_avg_temp_f": 81.0,
        "extreme_heat_days": 20.0,
        "extreme_cold_days": 2.0,
        "climate_coverage_status": "complete",
        "coverage_status": "complete",
    }
    rows = []
    for row in (worse, better):
        row["u_safety"] = pillar_utility(
            {"hazard": row["u_hazard"], "crime": row["u_crime"], "water": row["u_water"]},
            PILLAR_INTERNALS["safety"],
        )
        row["u_health"] = pillar_utility(
            {
                "healthcare": row["u_healthcare"],
                "community_context": row["u_community_context"],
            },
            PILLAR_INTERNALS["health"],
        )
        row["u_opportunity"] = pillar_utility(
            {"employment": row["u_employment"], "broadband": row["u_broadband"]},
            PILLAR_INTERNALS["opportunity"],
        )
        row["u_lifestyle"] = row["u_mountain"]
        row["u_family"] = row["homeschool_utility"]
        rows.append(row)
    return rows


def write_synthetic_fixture_bundle(
    output: Path, *, extra_rows: list[dict[str, Any]] | None = None
) -> RankingBundle:
    rows = synthetic_fixture_rows()
    if extra_rows:
        rows.extend(extra_rows)
    frame = pl.DataFrame(rows).select(COUNTY_COLUMNS).sort("county_fips")
    calibration = {
        "id": CALIBRATION_ID,
        "hazard": {"low": 0.0, "high": 100.0, "invert": True},
        "rpp_bounds": {"low": 80.0, "high": 130.0, "invert": True},
        "housing_sqft_bounds": {"low": 500.0, "high": 2500.0},
        "housing_sqft_knots": [
            {"value": 500.0, "cdf": 0.0},
            {"value": 1000.0, "cdf": 0.4},
            {"value": 2000.0, "cdf": 0.9},
            {"value": 2500.0, "cdf": 1.0},
        ],
        "crime_coverage_floor": CRIME_COVERAGE_FLOOR,
        "pillar_internals": PILLAR_INTERNALS,
    }
    homeschool = load_homeschool_policy()
    citations = {
        "population": "U.S. Census Bureau Population Estimates Program",
        "hazard": "FEMA National Risk Index building-loss inputs",
        "crime": "FBI UCR/NIBRS agency-attributed rates; coverage >= 90%",
        "water": "EPA SDWIS health-based drinking-water violations",
        "healthcare": "HRSA Area Health Resources Files",
        "community_context": "CHR&R Community Conditions; reconstructed if unpublished",
        "rpp": "BEA Regional Price Parities",
        "property_tax": "ACS B25103 / B25077 effective rate",
        "employment": "BLS QCEW / ACS commute-access",
        "broadband": "FCC public aggregate terrestrial fixed 100/20 availability",
        "mountain": "HouseHunter Mountain Magnitude v2",
        "climate": "NOAA climate normals; gates only",
        "homeschool": HOMESCHOOL_NOTICE,
    }
    return write_ranking_bundle(
        output,
        frame,
        calibration=calibration,
        homeschool=homeschool,
        citations=citations,
        scope="fixture",
        vintages={
            "population": "2024",
            "fema": "fixture",
            "crime": "2022-2024",
            "bea_rpp": "2024",
        },
    )
