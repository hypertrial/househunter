from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import polars as pl

from .config import canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .secure_fetch import deny_restricted_data_class, validated_https_url

BUNDLE_SCHEMA_VERSION = 2
METHODOLOGY_ID = "top-counties-v2"
CALIBRATION_ID = "ranking-calibration-v2"
HOMESCHOOL_NOTICE = (
    "Approximate, project-authored policy preference rubric. It is not legal advice, "
    "a legal-compliance determination, school-quality evidence, or a recommendation. "
    "Laws and interpretations may change; verify current requirements with official "
    "state sources or qualified counsel."
)
HOMESCHOOL_SOURCE_CHECK_SCOPE = (
    "Official-source link identity and cited-date presence only; no legal review, "
    "statutory interpretation, or currency determination was performed."
)
CRIME_COVERAGE_FLOOR = 0.90
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
HOUSING_SQFT_BOUNDS = {"low": 500.0, "high": 2500.0}
HOUSING_SQFT_KNOTS = [
    {"value": 500.0, "cdf": 0.0},
    {"value": 1000.0, "cdf": 0.4},
    {"value": 2000.0, "cdf": 0.9},
    {"value": 2500.0, "cdf": 1.0},
]
ECDF_COMPONENTS = (
    ("crime_violent_rate", "u_crime_violent", True),
    ("crime_property_rate", "u_crime_property", True),
    ("provider_primary_care", "u_primary_care", False),
    ("provider_mental_health", "u_mental_health", False),
    ("provider_dental", "u_dental", False),
    ("rpp_index", "u_rpp", True),
    ("property_tax_rate", "u_property_tax", True),
    ("employment_growth", "u_employment_growth", False),
    ("average_weekly_wage", "u_average_weekly_wage", False),
    ("commute_under_30_share", "u_commute_under_30", False),
    ("mountain_magnitude", "u_mountain", False),
)
COUNTY_COLUMNS = [
    "county_fips",
    "state",
    "population",
    "population_vintage",
    "population_status",
    "res_hazard_npctl",
    "u_hazard",
    "hazard_status",
    "crime_violent_rate",
    "crime_property_rate",
    "crime_coverage",
    "crime_violent_coverage_2023",
    "crime_violent_coverage_2024",
    "crime_violent_coverage_2025",
    "crime_property_coverage_2023",
    "crime_property_coverage_2024",
    "crime_property_coverage_2025",
    "u_crime_violent",
    "u_crime_property",
    "u_crime",
    "crime_status",
    "water_violation_share",
    "public_water_coverage",
    "public_water_coverage_kind",
    "water_allocation_coverage",
    "water_boundary_provenance",
    "water_overlap_duplicate_share_proxy",
    "water_overlap_quality_status",
    "u_water",
    "water_status",
    "u_safety",
    "provider_primary_care",
    "provider_mental_health",
    "provider_dental",
    "u_primary_care",
    "u_mental_health",
    "u_dental",
    "provider_primary_care_source",
    "provider_mental_health_source",
    "provider_dental_source",
    "provider_primary_care_vintage",
    "provider_mental_health_vintage",
    "provider_dental_vintage",
    "u_healthcare",
    "healthcare_status",
    "community_context",
    "community_context_kind",
    "u_community_context",
    "community_context_status",
    "u_health",
    "rpp_index",
    "rpp_geography_type",
    "u_rpp",
    "rpp_status",
    "property_tax_rate",
    "u_property_tax",
    "property_tax_status",
    "employment_growth",
    "average_weekly_wage",
    "commute_under_30_share",
    "u_employment_growth",
    "u_average_weekly_wage",
    "u_commute_under_30",
    "u_employment",
    "employment_status",
    "broadband_100_20",
    "broadband_denominator_label",
    "u_broadband",
    "broadband_status",
    "u_opportunity",
    "mountain_magnitude",
    "u_mountain",
    "mountain_status",
    "u_lifestyle",
    "homeschool_utility",
    "homeschool_rubric_json",
    "family_status",
    "u_family",
    "jan_avg_temp_f",
    "jul_avg_temp_f",
    "extreme_heat_days",
    "extreme_cold_days",
    "climate_station_count",
    "climate_coverage_status",
    "citation_ids_json",
    "limitations_json",
    "coverage_status",
]
UTILITY_COLUMNS = [
    "u_hazard",
    "u_crime_violent",
    "u_crime_property",
    "u_crime",
    "u_water",
    "u_safety",
    "u_healthcare",
    "u_primary_care",
    "u_mental_health",
    "u_dental",
    "u_community_context",
    "u_health",
    "u_rpp",
    "u_property_tax",
    "u_employment_growth",
    "u_average_weekly_wage",
    "u_commute_under_30",
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

_SOURCE_LOCK_TOP_LEVEL_KEYS = {
    "allowed_hosts",
    "compiled_on",
    "county_universe",
    "denied_data_classes",
    "join_contracts",
    "methodology_id",
    "policy",
    "qualification_cutoff",
    "schema",
    "sources",
    "transforms",
}
_SOURCE_KEYS = {
    "acquisition",
    "agency",
    "api",
    "artifacts",
    "coverage_floor",
    "data_class",
    "denominator_label",
    "expected_submission_year_quarter",
    "grain",
    "identity",
    "license",
    "measurement_period",
    "name",
    "package_raw",
    "pii_class",
    "reference_artifacts",
    "required_fields",
    "retrieved_on",
    "runtime_fetch",
    "terms_url",
    "vintage",
}
_ARTIFACT_KEYS = {
    "archive_inventory",
    "archive_members",
    "bytes",
    "filename",
    "manual_stage",
    "sha256",
    "url",
}
_ARCHIVE_MEMBER_KEYS = {"bytes", "name", "sha256"}
_ARCHIVE_INVENTORY_KEYS = {
    "format",
    "logical_sha256",
    "member_count",
    "total_uncompressed_bytes",
}
_REFERENCE_ARTIFACT_KEYS = {
    "archive_inventory",
    "bytes",
    "filename",
    "manual_stage",
    "sha256",
}
_API_KEYS = {
    "agency_catalog_path",
    "base_url",
    "catalog_max_bytes",
    "contract_sha256",
    "included_agency_types",
    "in_scope_states",
    "offenses",
    "query",
    "response_manifest",
    "summary_max_bytes",
    "summary_path",
}
_FBI_MANIFEST_LOCK_KEYS = {
    "agencies_sha256",
    "agency_count",
    "bytes",
    "catalog_count",
    "contract_sha256",
    "county_source_sha256",
    "county_universe_sha256",
    "exclusion_count",
    "exclusions_sha256",
    "filename",
    "response_bytes",
    "response_count",
    "responses_sha256",
    "retrieved_on",
    "schema",
    "sha256",
    "summary_count",
    "uncompressed_bytes",
    "uncompressed_sha256",
}
_FBI_MANIFEST_PAYLOAD_KEYS = {
    "agencies",
    "agency_exclusions",
    "agencies_sha256",
    "agency_count",
    "catalog_count",
    "contract_sha256",
    "county_source_sha256",
    "county_universe",
    "county_universe_sha256",
    "exclusion_count",
    "exclusions_sha256",
    "response_bytes",
    "response_count",
    "responses",
    "responses_sha256",
    "retrieved_on",
    "schema",
    "summary_count",
}
_FBI_AGENCY_KEYS = {"agency_type", "county_fips", "ori", "published_county", "state"}
_FBI_EXCLUSION_KEYS = {"ori", "published_county", "reason", "state"}
_FBI_RESPONSE_KEYS = {"bytes", "key", "kind", "request", "sha256", "status"}
_TRANSFORM_KEYS = {
    "calibration",
    "broadband",
    "climate",
    "community_context",
    "crime",
    "employment",
    "healthcare",
    "homeschool",
    "housing",
    "mountain",
    "nulls",
    "population",
    "property_tax",
    "rpp",
    "safety_hazard",
    "water",
}
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:access_?key(?:_id)?|api_?key|auth(?:orization|_header)?|bearer|cookie|"
    r"credential|password|private_?key|secret|token)(?:$|_)",
    re.IGNORECASE,
)
_LOCAL_PATH_KEYS = frozenset(
    {
        "directory",
        "headers",
        "local_path",
        "path",
        "raw_path",
        "staging_path",
        "work_path",
    }
)
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+ -]*")
_ISO_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")
_IDENTITY_KEYS_BY_SOURCE = {
    "arc_appalachia_counties": {"filename", "sha256"},
    "chrr_community_context_2025": {
        "canonical_sha256",
        "expected_row_count",
        "schema_fingerprint",
    },
    "fema_residential_hazard": {
        "canonical_sha256",
        "item_id",
        "release",
        "schema_fingerprint",
        "version",
    },
    "homeschool_policy_v1": {"approximate", "compiled_on", "filename", "sha256"},
    "househunter_housing_policy": {
        "knots_sha256",
        "minimum_active_listings",
        "minimum_valid_months",
        "private_rows_packaged",
    },
    "househunter_mountain_v2": {
        "county_artifact_sha256",
        "county_rows",
        "release_id",
        "schema_version",
        "source_lock_sha256",
    },
    "omb_county_cbsa_2023": {"artifact_sha256", "omb_delineation", "rows"},
}
_EXPECTED_SOURCE_NAMES = frozenset(
    {
        "acs_commute_2024",
        "acs_property_tax_2024",
        "arc_appalachia_counties",
        "bea_marpp_2024",
        "bea_sarpp_2024",
        "bls_qcew_2024",
        "bls_qcew_2025",
        "census_pep_county_2025",
        "census_tiger_county_2025",
        "chrr_community_context_2025",
        "chrr_nppes_mental_health_2025",
        "epa_cws_service_areas_v2_1",
        "epa_sdwis_2026_q2",
        "fbi_ucr_agency_2023_2025",
        "fcc_fixed_summary_2025_12",
        "fema_residential_hazard",
        "homeschool_policy_v1",
        "househunter_housing_policy",
        "househunter_mountain_v2",
        "hrsa_ahrf_2024_2025",
        "noaa_normals_1991_2020",
        "omb_county_cbsa_2023",
    }
)
_ACQUISITION_KINDS = frozenset(
    {
        "api_manifest",
        "download",
        "existing_packaged_bundle",
        "existing_runtime_source",
        "manual_stage",
        "project_authored",
    }
)
_FBI_AGENCY_TYPES = [
    "Borough",
    "City",
    "County",
    "Metropolitan",
    "Parish",
    "Township",
    "Village",
]
_FBI_IN_SCOPE_STATES = sorted(
    state for code, state in STATE_BY_FIPS.items() if code in IN_SCOPE_STATE_FIPS
)


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
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
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


def _require_exact_keys(value: object, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise HouseHunterError(f"{label} fields differ from the reviewed schema")
    return value


def _require_allowed_keys(value: object, allowed: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= allowed:
        raise HouseHunterError(f"{label} contains an unreviewed field")
    return value


def _reject_sensitive_lock_fields(value: object, *, location: str = "source lock") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise HouseHunterError(f"Ranking {location} contains a non-string field")
            if _SECRET_KEY.search(key) or key.lower() in _LOCAL_PATH_KEYS:
                raise HouseHunterError(f"Ranking {location} contains a forbidden field: {key}")
            _reject_sensitive_lock_fields(nested, location=location)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_lock_fields(nested, location=location)


def _validate_locked_filename(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _FILENAME.fullmatch(value) is None:
        raise HouseHunterError(f"{label} filename is invalid")
    return value


def _validate_locked_artifact(
    value: object,
    *,
    allowed_hosts: set[str],
    label: str,
    reference: bool = False,
) -> None:
    allowed_keys = _REFERENCE_ARTIFACT_KEYS if reference else _ARTIFACT_KEYS
    artifact = _require_allowed_keys(value, allowed_keys, label=label)
    _validate_locked_filename(artifact.get("filename"), label=label)
    if not _is_sha256(artifact.get("sha256")):
        raise HouseHunterError(f"{label} SHA-256 is invalid")
    byte_count = artifact.get("bytes")
    if not isinstance(byte_count, int) or byte_count <= 0:
        raise HouseHunterError(f"{label} byte count is invalid")
    filename = artifact["filename"].lower()
    if filename.endswith((".tar.gz", ".tgz")):
        archive_format = "tar.gz"
    elif filename.endswith(".zip"):
        archive_format = "zip"
    else:
        archive_format = None
    inventory = artifact.get("archive_inventory")
    if archive_format is not None:
        inventory = _require_exact_keys(
            inventory, _ARCHIVE_INVENTORY_KEYS, label=f"{label} archive inventory"
        )
        if (
            inventory.get("format") != archive_format
            or not isinstance(inventory.get("member_count"), int)
            or inventory["member_count"] <= 0
            or not isinstance(inventory.get("total_uncompressed_bytes"), int)
            or inventory["total_uncompressed_bytes"] <= 0
            or not _is_sha256(inventory.get("logical_sha256"))
        ):
            raise HouseHunterError(f"{label} archive inventory is invalid")
    elif inventory is not None:
        raise HouseHunterError(f"{label} cannot declare archive metadata for a plain file")
    if reference:
        if artifact.get("manual_stage") is not True:
            raise HouseHunterError(f"{label} must be explicitly manually staged")
        return
    url = artifact.get("url")
    manual_stage = artifact.get("manual_stage")
    if url is None:
        if manual_stage is not True:
            raise HouseHunterError(f"{label} manual input is not completely locked")
    else:
        if manual_stage is not None:
            raise HouseHunterError(f"{label} download identity is not completely locked")
        validated_https_url(url, allowed_hosts, label=label)
    members = artifact.get("archive_members")
    if members is not None:
        if not isinstance(members, list) or not members:
            raise HouseHunterError(f"{label} archive member manifest is invalid")
        names: set[str] = set()
        for index, value in enumerate(members):
            member = _require_exact_keys(
                value, _ARCHIVE_MEMBER_KEYS, label=f"{label} archive member {index}"
            )
            name = member.get("name")
            if (
                not isinstance(name, str)
                or not name
                or "\\" in name
                or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or name.casefold() in names
                or not isinstance(member.get("bytes"), int)
                or member["bytes"] <= 0
                or not _is_sha256(member.get("sha256"))
            ):
                raise HouseHunterError(f"{label} archive member manifest is invalid")
            names.add(name.casefold())
        if inventory is None or len(members) > inventory["member_count"]:
            raise HouseHunterError(f"{label} selected members disagree with archive inventory")


def _validate_api_lock(value: object, *, allowed_hosts: set[str], label: str) -> None:
    api = _require_exact_keys(value, _API_KEYS, label=label)
    validated_https_url(api.get("base_url"), allowed_hosts, label=label)
    for key in ("agency_catalog_path", "summary_path"):
        path = api.get(key)
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or ".." in path.split("/")
            or "?" in path
            or "#" in path
        ):
            raise HouseHunterError(f"{label} {key} is invalid")
    query = api.get("query")
    if (
        not isinstance(query, dict)
        or not query
        or any(
            not isinstance(key, str) or not key or not isinstance(item, str) or not item
            for key, item in query.items()
        )
    ):
        raise HouseHunterError(f"{label} query contract is invalid")
    offenses = api.get("offenses")
    manifest = _require_exact_keys(
        api.get("response_manifest"),
        _FBI_MANIFEST_LOCK_KEYS,
        label=f"{label} response manifest",
    )
    manifest_count_keys = (
        "bytes",
        "uncompressed_bytes",
        "catalog_count",
        "agency_count",
        "exclusion_count",
        "summary_count",
        "response_count",
        "response_bytes",
    )
    manifest_counts_valid = all(
        isinstance(manifest.get(key), int) and manifest[key] > 0 for key in manifest_count_keys
    )
    if (
        offenses != ["violent-crime", "property-crime"]
        or api.get("included_agency_types") != _FBI_AGENCY_TYPES
        or api.get("in_scope_states") != _FBI_IN_SCOPE_STATES
        or api.get("catalog_max_bytes") != 16 * 1024 * 1024
        or api.get("summary_max_bytes") != 2 * 1024 * 1024
        or manifest.get("schema") != 1
        or manifest.get("retrieved_on") != "2026-09-16"
        or not manifest_counts_valid
        or manifest.get("catalog_count") != len(_FBI_IN_SCOPE_STATES)
        or manifest.get("summary_count") != 2 * manifest.get("agency_count", -1)
        or manifest.get("response_count")
        != manifest.get("catalog_count", -1) + manifest.get("summary_count", -1)
    ):
        raise HouseHunterError(f"{label} response contract is invalid")
    contract = {
        "base_url": api["base_url"],
        "catalog_path": api["agency_catalog_path"],
        "summary_path": api["summary_path"],
        "query": api["query"],
        "offenses": api["offenses"],
        "included_agency_types": api["included_agency_types"],
        "states": api["in_scope_states"],
        "catalog_max_bytes": api["catalog_max_bytes"],
        "summary_max_bytes": api["summary_max_bytes"],
    }
    contract_sha256 = sha256_bytes(canonical_json(contract))
    if (
        api.get("contract_sha256") != contract_sha256
        or manifest.get("contract_sha256") != contract_sha256
        or _validate_locked_filename(manifest.get("filename"), label=f"{label} response manifest")
        != "fbi-ucr-request-manifest-v1.json.gz"
        or any(
            not _is_sha256(manifest.get(key))
            for key in (
                "sha256",
                "uncompressed_sha256",
                "responses_sha256",
                "agencies_sha256",
                "exclusions_sha256",
                "county_source_sha256",
                "county_universe_sha256",
            )
        )
    ):
        raise HouseHunterError(f"{label} request contract hash is invalid")


def _validate_identity(value: object, *, source_name: str) -> None:
    expected = _IDENTITY_KEYS_BY_SOURCE.get(source_name)
    if expected is None:
        raise HouseHunterError(f"Ranking source {source_name} has no reviewed identity schema")
    identity = _require_exact_keys(value, expected, label=f"Ranking source {source_name} identity")
    for key, item in identity.items():
        if key.endswith("sha256") and not _is_sha256(item):
            raise HouseHunterError(f"Ranking source {source_name} identity hash is invalid")


def load_source_lock(path: Path | None = None) -> dict[str, Any]:
    source = path or default_source_lock_path()
    try:
        lock = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read ranking source lock: {source}: {exc}") from exc
    if not isinstance(lock, dict):
        raise HouseHunterError(f"Unsupported ranking source lock: {source}")
    _require_exact_keys(lock, _SOURCE_LOCK_TOP_LEVEL_KEYS, label="Ranking source lock")
    _reject_sensitive_lock_fields(lock)
    if (
        lock.get("schema") != 2
        or lock.get("methodology_id") != METHODOLOGY_ID
        or not isinstance(lock.get("compiled_on"), str)
        or _ISO_DATE.fullmatch(lock["compiled_on"]) is None
        or lock.get("qualification_cutoff") != "2026-09-16"
        or not isinstance(lock.get("allowed_hosts"), list)
        or not lock["allowed_hosts"]
        or not isinstance(lock.get("sources"), list)
        or not lock["sources"]
    ):
        raise HouseHunterError(f"Unsupported ranking source lock: {source}")
    allowed_hosts = lock["allowed_hosts"]
    allowed = {str(host).lower() for host in allowed_hosts}
    if len(allowed) != len(allowed_hosts) or any(
        not isinstance(host, str)
        or host != host.lower()
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is None
        for host in allowed_hosts
    ):
        raise HouseHunterError("Ranking source lock host allowlist must be lowercase")
    policy = _require_exact_keys(
        lock.get("policy"),
        {"credentials", "package_private_rows", "package_raw", "runtime_fetch"},
        label="Ranking source lock policy",
    )
    if any(policy.values()):
        raise HouseHunterError("Ranking source lock policy cannot enable private or runtime data")
    universe = _require_exact_keys(
        lock.get("county_universe"),
        {
            "connecticut_current_fips",
            "excluded_state_fips",
            "legacy_connecticut_fips",
            "row_count",
            "scope",
            "sorted_fips_sha256",
        },
        label="Ranking county universe",
    )
    if (
        universe.get("row_count") != 3144
        or not _is_sha256(universe.get("sorted_fips_sha256"))
        or universe.get("connecticut_current_fips")
        != [f"09{value}" for value in range(110, 200, 10)]
        or universe.get("legacy_connecticut_fips")
        != [f"09{value:03d}" for value in range(1, 16, 2)]
        or universe.get("excluded_state_fips") != ["60", "66", "69", "72", "78"]
    ):
        raise HouseHunterError("Ranking county universe differs from the reviewed contract")
    denied = lock.get("denied_data_classes")
    if (
        not isinstance(denied, list)
        or not denied
        or len(denied) != len(set(denied))
        or any(not isinstance(item, str) or not item for item in denied)
    ):
        raise HouseHunterError("Ranking denied-data-class contract is invalid")
    transforms = lock.get("transforms")
    if not isinstance(transforms, dict) or set(transforms) != _TRANSFORM_KEYS:
        raise HouseHunterError("Ranking transformations differ from the reviewed contract")
    if (
        transforms["crime"].get("coverage_floor") != CRIME_COVERAGE_FLOOR
        or transforms["water"].get("coverage_floor") != 0.90
        or transforms["climate"].get("nearest_station_fallback") is not False
        or transforms["homeschool"].get("notice") != HOMESCHOOL_NOTICE
        or transforms["homeschool"].get("approximate") is not True
    ):
        raise HouseHunterError("Ranking critical transformation gates differ from their lock")
    joins = lock.get("join_contracts")
    if (
        not isinstance(joins, dict)
        or not joins
        or any(
            not isinstance(name, str)
            or not isinstance(contract, dict)
            or set(contract) != {"cardinality", "left_key", "right_key"}
            or contract["cardinality"] not in {"1:1", "m:1"}
            or not isinstance(contract["left_key"], str)
            or not isinstance(contract["right_key"], str)
            for name, contract in joins.items()
        )
    ):
        raise HouseHunterError("Ranking join contracts differ from the reviewed schema")
    seen_names: set[str] = set()
    reviewed_hosts: set[str] = set()
    for item in lock["sources"]:
        item = _require_allowed_keys(item, _SOURCE_KEYS, label="Ranking source")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in seen_names:
            raise HouseHunterError("Ranking source lock contains duplicate or blank source names")
        seen_names.add(name)
        deny_restricted_data_class(item.get("data_class"), label=name)
        if item.get("runtime_fetch") is not False:
            raise HouseHunterError(f"Ranking source {name} must not be fetched at runtime")
        if item.get("package_raw") is not False:
            raise HouseHunterError(f"Ranking source {name} must not package raw downloads")
        if (
            not isinstance(item.get("retrieved_on"), str)
            or _ISO_DATE.fullmatch(item["retrieved_on"]) is None
            or not all(
                isinstance(item.get(field), str) and bool(item[field])
                for field in (
                    "acquisition",
                    "agency",
                    "data_class",
                    "grain",
                    "license",
                    "measurement_period",
                    "pii_class",
                    "terms_url",
                    "vintage",
                )
            )
        ):
            raise HouseHunterError(f"Ranking source {name} identity is incomplete")
        acquisition = item["acquisition"]
        if acquisition not in _ACQUISITION_KINDS:
            raise HouseHunterError(f"Ranking source {name} acquisition mode is unreviewed")
        validated_https_url(item["terms_url"], allowed, label=f"Ranking source {name} terms")
        reviewed_hosts.add(str(urlsplit(item["terms_url"]).hostname).lower())
        artifacts = item.get("artifacts", [])
        references = item.get("reference_artifacts", [])
        if not isinstance(artifacts, list) or not isinstance(references, list):
            raise HouseHunterError(f"Ranking source {name} artifact contract is invalid")
        for index, artifact in enumerate(artifacts):
            _validate_locked_artifact(
                artifact,
                allowed_hosts=allowed,
                label=f"Ranking source {name} artifact {index}",
            )
            if artifact.get("url") is not None:
                reviewed_hosts.add(str(urlsplit(artifact["url"]).hostname).lower())
        for index, artifact in enumerate(references):
            _validate_locked_artifact(
                artifact,
                allowed_hosts=allowed,
                label=f"Ranking source {name} reference {index}",
                reference=True,
            )
        api = item.get("api")
        if api is not None:
            _validate_api_lock(api, allowed_hosts=allowed, label=f"Ranking source {name} API")
            reviewed_hosts.add(str(urlsplit(api["base_url"]).hostname).lower())
        identity = item.get("identity")
        if identity is not None:
            _validate_identity(identity, source_name=name)
        fields = item.get("required_fields")
        if fields is not None and (
            not isinstance(fields, list)
            or not fields
            or len(fields) != len(set(fields))
            or any(not isinstance(field, str) or not field for field in fields)
        ):
            raise HouseHunterError(f"Ranking source {name} required fields are invalid")
        if acquisition == "download" and (
            not artifacts or references or api is not None or identity is not None
        ):
            raise HouseHunterError(f"Ranking source {name} download contract is inconsistent")
        if acquisition == "manual_stage" and (
            not artifacts
            or references
            or api is not None
            or identity is not None
            or any(artifact.get("manual_stage") is not True for artifact in artifacts)
        ):
            raise HouseHunterError(f"Ranking source {name} manual contract is inconsistent")
        if acquisition == "api_manifest" and (
            api is None or not references or artifacts or identity is not None or not fields
        ):
            raise HouseHunterError(f"Ranking source {name} API contract is inconsistent")
        if name == "epa_sdwis_2026_q2" and item.get("expected_submission_year_quarter") != "2026Q2":
            raise HouseHunterError("EPA SDWIS submission quarter differs from its lock")
        if acquisition in {
            "existing_packaged_bundle",
            "existing_runtime_source",
            "project_authored",
        } and (identity is None or artifacts or references or api is not None):
            raise HouseHunterError(f"Ranking source {name} identity contract is inconsistent")
        if not artifacts and not references and api is None and identity is None:
            raise HouseHunterError(f"Ranking source {name} has no immutable identity")
    if seen_names != _EXPECTED_SOURCE_NAMES:
        raise HouseHunterError("Ranking source inventory differs from the reviewed contract")
    if reviewed_hosts != allowed:
        raise HouseHunterError("Ranking source lock host allowlist contains unused hosts")
    return lock


def validate_fbi_response_manifest(
    path: Path | None = None,
    *,
    source_lock_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the compact public FBI request manifest without loading raw responses."""
    lock_path = source_lock_path or default_source_lock_path()
    lock = load_source_lock(lock_path)
    source = next(item for item in lock["sources"] if item["name"] == "fbi_ucr_agency_2023_2025")
    api = source["api"]
    expected = api["response_manifest"]
    manifest_path = path or lock_path.parent / expected["filename"]
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise HouseHunterError("FBI response manifest must be a regular file")
        if manifest_path.stat().st_size != expected["bytes"]:
            raise HouseHunterError("FBI response manifest byte count differs from its lock")
        compressed = manifest_path.read_bytes()
    except OSError as exc:
        raise HouseHunterError(f"Cannot read FBI response manifest: {exc}") from exc
    if sha256_bytes(compressed) != expected["sha256"]:
        raise HouseHunterError("FBI response manifest SHA-256 differs from its lock")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as handle:
            raw = handle.read(expected["uncompressed_bytes"] + 1)
    except (OSError, EOFError) as exc:
        raise HouseHunterError(f"Cannot decompress FBI response manifest: {exc}") from exc
    if len(raw) != expected["uncompressed_bytes"]:
        raise HouseHunterError("FBI response manifest expanded size differs from its lock")
    if sha256_bytes(raw) != expected["uncompressed_sha256"]:
        raise HouseHunterError("FBI response manifest logical SHA-256 differs from its lock")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HouseHunterError("FBI response manifest is not valid JSON") from exc
    payload = _require_exact_keys(
        payload, _FBI_MANIFEST_PAYLOAD_KEYS, label="FBI response manifest"
    )
    for key in _FBI_MANIFEST_PAYLOAD_KEYS - {
        "agencies",
        "agency_exclusions",
        "county_universe",
        "responses",
    }:
        if payload[key] != expected[key]:
            raise HouseHunterError(f"FBI response manifest {key} differs from its lock")

    census = next(item for item in lock["sources"] if item["name"] == "census_pep_county_2025")
    if expected["county_source_sha256"] != census["artifacts"][0]["sha256"]:
        raise HouseHunterError("FBI response manifest county source differs from its lock")
    county_universe = payload["county_universe"]
    reviewed_universe = lock["county_universe"]
    if (
        not isinstance(county_universe, list)
        or len(county_universe) != reviewed_universe["row_count"]
        or county_universe != sorted(set(county_universe))
        or any(
            not isinstance(fips, str) or re.fullmatch(r"\d{5}", fips) is None
            for fips in county_universe
        )
        or sha256_bytes(canonical_json(county_universe)) != reviewed_universe["sorted_fips_sha256"]
        or expected["county_universe_sha256"] != reviewed_universe["sorted_fips_sha256"]
    ):
        raise HouseHunterError("FBI response manifest county universe differs")
    county_set = set(county_universe)

    agencies = payload["agencies"]
    if not isinstance(agencies, list) or len(agencies) != expected["agency_count"]:
        raise HouseHunterError("FBI response manifest agency count differs")
    agency_oris: list[str] = []
    for value in agencies:
        agency = _require_exact_keys(value, _FBI_AGENCY_KEYS, label="FBI manifest agency")
        ori = agency.get("ori")
        state = agency.get("state")
        agency_type = agency.get("agency_type")
        county = agency.get("published_county")
        county_fips = agency.get("county_fips")
        if (
            not isinstance(ori, str)
            or len(ori) != 9
            or not ori.isalnum()
            or state not in api["in_scope_states"]
            or agency_type not in api["included_agency_types"]
            or not isinstance(county, str)
            or not county
            or "," in county
            or county_fips not in county_set
            or STATE_BY_FIPS.get(str(county_fips)[:2]) != state
        ):
            raise HouseHunterError("FBI response manifest contains an invalid agency")
        agency_oris.append(ori)
    if agency_oris != sorted(set(agency_oris)):
        raise HouseHunterError("FBI response manifest agencies are not sorted and unique")
    if sha256_bytes(canonical_json(agencies)) != expected["agencies_sha256"]:
        raise HouseHunterError("FBI response manifest agency digest differs")

    exclusions = payload["agency_exclusions"]
    if not isinstance(exclusions, list) or len(exclusions) != expected["exclusion_count"]:
        raise HouseHunterError("FBI response manifest exclusion count differs")
    exclusion_oris: list[str] = []
    for value in exclusions:
        exclusion = _require_exact_keys(
            value, _FBI_EXCLUSION_KEYS, label="FBI manifest agency exclusion"
        )
        ori = exclusion.get("ori")
        if (
            not isinstance(ori, str)
            or len(ori) != 9
            or not ori.isalnum()
            or exclusion.get("state") not in api["in_scope_states"]
            or not isinstance(exclusion.get("published_county"), str)
            or not exclusion["published_county"]
            or exclusion.get("reason")
            not in {
                "missing_county_attribution",
                "multiple_county_attribution",
                "unassignable_current_county",
            }
        ):
            raise HouseHunterError("FBI response manifest contains an invalid exclusion")
        exclusion_oris.append(ori)
    if exclusion_oris != sorted(set(exclusion_oris)) or set(exclusion_oris) & set(agency_oris):
        raise HouseHunterError("FBI response manifest exclusions are not sorted and unique")
    if sha256_bytes(canonical_json(exclusions)) != expected["exclusions_sha256"]:
        raise HouseHunterError("FBI response manifest exclusion digest differs")

    responses = payload["responses"]
    if not isinstance(responses, list) or len(responses) != expected["response_count"]:
        raise HouseHunterError("FBI response manifest response count differs")
    response_order: list[tuple[str, str]] = []
    catalog_states: set[str] = set()
    summary_pairs: set[tuple[str, str]] = set()
    response_bytes = 0
    agency_set = set(agency_oris)
    for value in responses:
        response = _require_exact_keys(value, _FBI_RESPONSE_KEYS, label="FBI manifest response")
        kind = response.get("kind")
        key = response.get("key")
        request = response.get("request")
        byte_count = response.get("bytes")
        if (
            kind not in {"catalog", "summary"}
            or not isinstance(key, str)
            or not key
            or not isinstance(request, str)
            or "?" in request
            or response.get("status") != 200
            or not isinstance(byte_count, int)
            or byte_count <= 0
            or not _is_sha256(response.get("sha256"))
        ):
            raise HouseHunterError("FBI response manifest contains an invalid response")
        if kind == "catalog":
            if (
                key not in api["in_scope_states"]
                or request != api["agency_catalog_path"].format(state=key)
                or byte_count > api["catalog_max_bytes"]
            ):
                raise HouseHunterError("FBI response manifest catalog request differs")
            catalog_states.add(key)
        else:
            parts = key.split(":")
            if len(parts) != 2:
                raise HouseHunterError("FBI response manifest summary key differs")
            ori, offense = parts
            if (
                ori not in agency_set
                or offense not in api["offenses"]
                or request != api["summary_path"].format(ori=ori, offense=offense)
                or byte_count > api["summary_max_bytes"]
            ):
                raise HouseHunterError("FBI response manifest summary request differs")
            summary_pairs.add((ori, offense))
        response_order.append((kind, key))
        response_bytes += byte_count
    if response_order != sorted(set(response_order)):
        raise HouseHunterError("FBI response manifest responses are not sorted and unique")
    if catalog_states != set(api["in_scope_states"]):
        raise HouseHunterError("FBI response manifest catalog coverage differs")
    expected_pairs = {(ori, offense) for ori in agency_oris for offense in api["offenses"]}
    if summary_pairs != expected_pairs:
        raise HouseHunterError("FBI response manifest summary coverage differs")
    if response_bytes != expected["response_bytes"]:
        raise HouseHunterError("FBI response manifest response bytes differ")
    if sha256_bytes(canonical_json(responses)) != expected["responses_sha256"]:
        raise HouseHunterError("FBI response manifest response digest differs")
    return payload


def load_homeschool_policy(path: Path | None = None) -> dict[str, Any]:
    source = path or default_homeschool_path()
    try:
        payload = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read homeschool policy: {exc}") from exc
    jurisdictions = payload.get("jurisdictions") if isinstance(payload, dict) else None
    official_sources = payload.get("official_sources") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or payload.get("approximate") is not True
        or payload.get("compiled_on") != "2026-09-16"
        or payload.get("notice") != HOMESCHOOL_NOTICE
        or payload.get("source_check_scope") != HOMESCHOOL_SOURCE_CHECK_SCOPE
        or not isinstance(jurisdictions, dict)
        or not isinstance(official_sources, dict)
    ):
        raise HouseHunterError("Homeschool policy rubric is malformed")
    expected = {state for code, state in STATE_BY_FIPS.items() if code in IN_SCOPE_STATE_FIPS}
    if set(jurisdictions) != expected:
        raise HouseHunterError("Homeschool policy must cover every in-scope state and DC")
    if set(official_sources) != expected:
        raise HouseHunterError("Homeschool policy must cite an official source for every state")
    for state, row in jurisdictions.items():
        utility = _finite(row.get("utility") if isinstance(row, dict) else None)
        citations = row.get("citations") if isinstance(row, dict) else None
        official_url = official_sources.get(state)
        official_host = urlsplit(str(official_url)).hostname
        if (
            utility is None
            or not 0 <= utility <= 1
            or not isinstance(citations, list)
            or not citations
            or any(
                not isinstance(citation, dict)
                or set(citation) != {"effective_date", "statute"}
                or not isinstance(citation["effective_date"], str)
                or _ISO_DATE.fullmatch(citation["effective_date"]) is None
                or not isinstance(citation["statute"], str)
                or not citation["statute"]
                for citation in citations
            )
            or not isinstance(official_url, str)
            or not isinstance(official_host, str)
            or row.get("source_checked_on") != payload["compiled_on"]
            or not isinstance(row.get("effective_date"), str)
            or _ISO_DATE.fullmatch(row["effective_date"]) is None
        ):
            raise HouseHunterError(f"Homeschool policy for {state} is incomplete")
        validated_https_url(official_url, {official_host}, label=f"Homeschool policy {state}")
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


def ecdf_calibration_audit(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
    audit: dict[str, dict[str, Any]] = {}
    for raw_column, utility_column, invert in ECDF_COMPONENTS:
        pairs = [
            [county_fips, _finite(raw), _finite(utility)]
            for county_fips, raw, utility in frame.select(
                "county_fips", raw_column, utility_column
            ).iter_rows()
            if _finite(raw) is not None
        ]
        audit[utility_column] = {
            "raw_column": raw_column,
            "direction": "lower_is_better" if invert else "higher_is_better",
            "tie_policy": "average_tie_national_ecdf",
            "valid_count": len(pairs),
            "county_fips_sha256": sha256_bytes(canonical_json([pair[0] for pair in pairs])),
            "raw_utility_sha256": sha256_bytes(canonical_json(pairs)),
        }
    return audit


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


def bundle_coverage_summary(frame: pl.DataFrame) -> dict[str, Any]:
    """Return the release-bound aggregate coverage contract for a county bundle."""
    source_status_counts: dict[str, dict[str, int]] = {}
    for column in COUNTY_COLUMNS:
        if not column.endswith("_status") or column == "coverage_status":
            continue
        counts: dict[str, int] = {}
        for value in frame[column].to_list():
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
        source_status_counts[column] = dict(sorted(counts.items()))
    complete_public_core = int(
        frame.select(
            pl.all_horizontal(
                *[pl.col(column).is_not_null() for column in CORE_UTILITY_COLUMNS]
            ).sum()
        ).item()
    )
    return {
        "schema_version": 1,
        "row_count": frame.height,
        "source_status_counts": source_status_counts,
        "bundle_pillar_non_null_counts": {
            column.removeprefix("u_"): int(
                frame.select(pl.col(column).is_not_null().sum()).item()
            )
            for column in CORE_UTILITY_COLUMNS
        },
        "complete_public_core_count": complete_public_core,
        "partial_public_core_count": frame.height - complete_public_core,
    }


def ranking_bundle_identity(manifest: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json(
            {
                "schema_version": manifest.get("schema_version"),
                "methodology_id": manifest.get("methodology_id"),
                "calibration_id": manifest.get("calibration_id"),
                "calibration_hash": manifest.get("calibration_hash"),
                "scope": manifest.get("scope"),
                "source_lock_sha256": manifest.get("source_lock_sha256"),
                "county_fips_sha256": manifest.get("county_fips_sha256"),
                "row_count": manifest.get("row_count"),
                "coverage": manifest.get("coverage"),
                "vintages": manifest.get("vintages"),
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
        expected_healthcare = pillar_utility(
            {
                "primary_care": _finite(row["u_primary_care"]),
                "mental_health": _finite(row["u_mental_health"]),
                "dental": _finite(row["u_dental"]),
            },
            {"primary_care": 1 / 3, "mental_health": 1 / 3, "dental": 1 / 3},
        )
        expected_employment = pillar_utility(
            {
                "growth": _finite(row["u_employment_growth"]),
                "weekly_wage": _finite(row["u_average_weekly_wage"]),
                "commute": _finite(row["u_commute_under_30"]),
            },
            {"growth": 0.50, "weekly_wage": 0.25, "commute": 0.25},
        )
        for column, value in {
            "u_healthcare": expected_healthcare,
            "u_employment": expected_employment,
        }.items():
            observed = _finite(row[column])
            if (value is None) != (observed is None) or (
                value is not None and observed is not None and abs(value - observed) > 1e-9
            ):
                raise HouseHunterError(
                    f"Ranking bundle {column} drifted from its component utilities"
                )
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
    annual_coverage = [
        f"crime_{family}_coverage_{year}"
        for family in ("violent", "property")
        for year in (2023, 2024, 2025)
    ]
    scored_below_floor = frame.filter(
        pl.col("u_crime").is_not_null()
        & (
            pl.col("crime_coverage").is_null()
            | (pl.col("crime_coverage") < CRIME_COVERAGE_FLOOR)
            | pl.any_horizontal(
                *[
                    pl.col(column).is_null() | (pl.col(column) < CRIME_COVERAGE_FLOOR)
                    for column in annual_coverage
                ]
            )
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
    scored_water_below_floor = frame.filter(
        pl.col("u_water").is_not_null()
        & (
            pl.col("water_allocation_coverage").is_null()
            | (pl.col("water_allocation_coverage") < 0.90)
        )
    )
    if scored_water_below_floor.height:
        raise HouseHunterError("Ranking bundle scores water below the 90% allocation floor")


def _validate_calibration_identity(frame: pl.DataFrame) -> None:
    for raw_column, utility_column, invert in ECDF_COMPONENTS:
        raw = [_finite(value) for value in frame[raw_column].to_list()]
        valid = [(index, value) for index, value in enumerate(raw) if value is not None]
        expected = average_tie_percentile([value for _, value in valid], invert=invert)
        observed = [_finite(value) for value in frame[utility_column].to_list()]
        expected_by_index = {
            index: value for (index, _), value in zip(valid, expected, strict=True)
        }
        for index, actual in enumerate(observed):
            wanted = expected_by_index.get(index)
            if (wanted is None) != (actual is None) or (
                wanted is not None and actual is not None and abs(wanted - actual) > 1e-12
            ):
                county = frame["county_fips"][index]
                raise HouseHunterError(
                    f"Ranking bundle {utility_column} calibration drifted for {county}"
                )
    for row in frame.select(
        "county_fips",
        "u_crime_violent",
        "u_crime_property",
        "u_crime",
        "res_hazard_npctl",
        "u_hazard",
        "water_violation_share",
        "u_water",
        "community_context",
        "u_community_context",
        "broadband_100_20",
        "u_broadband",
        "homeschool_utility",
        "u_family",
    ).iter_rows(named=True):
        direct = {
            "u_crime": (
                None
                if row["u_crime_violent"] is None or row["u_crime_property"] is None
                else 0.60 * row["u_crime_violent"] + 0.40 * row["u_crime_property"]
            ),
            "u_hazard": (
                None if row["res_hazard_npctl"] is None else 1.0 - row["res_hazard_npctl"] / 100.0
            ),
            "u_water": (None if row["u_water"] is None else 1.0 - row["water_violation_share"]),
            "u_community_context": (
                None
                if row["community_context"] is None
                else (10.0 - row["community_context"]) / 9.0
            ),
            "u_broadband": row["broadband_100_20"],
            "u_family": row["homeschool_utility"],
        }
        for column, wanted in direct.items():
            actual = _finite(row[column])
            wanted = _finite(wanted)
            if (wanted is None) != (actual is None) or (
                wanted is not None and actual is not None and abs(wanted - actual) > 1e-12
            ):
                raise HouseHunterError(
                    f"Ranking bundle {column} identity drifted for {row['county_fips']}"
                )


def _validate_counties(
    frame: pl.DataFrame,
    *,
    scope: str,
    source_lock: Mapping[str, Any] | None = None,
) -> None:
    if frame.columns != COUNTY_COLUMNS:
        raise HouseHunterError("Ranking bundle county schema differs")
    fips = frame["county_fips"].to_list()
    if fips != sorted(set(fips)):
        raise HouseHunterError("Ranking bundle counties are not sorted and unique")
    if frame.filter(~pl.col("county_fips").str.contains(r"^\d{5}$")).height:
        raise HouseHunterError("Ranking bundle contains invalid county FIPS")
    connecticut = frame.filter(pl.col("county_fips").str.starts_with("09"))
    current_connecticut = [f"09{value}" for value in range(110, 200, 10)]
    if (
        connecticut.filter(~pl.col("state").eq("CT")).height
        or any(value not in current_connecticut for value in connecticut["county_fips"])
        or (scope == "national" and connecticut["county_fips"].to_list() != current_connecticut)
    ):
        raise HouseHunterError("Connecticut must use exactly the current planning-region FIPS")
    unknown_state = frame.filter(
        ~pl.col("county_fips").str.slice(0, 2).is_in(list(IN_SCOPE_STATE_FIPS))
    )
    if unknown_state.height:
        raise HouseHunterError("Ranking bundle contains out-of-scope county FIPS")
    if frame.filter(pl.col("population").is_null() | (pl.col("population") <= 0)).height:
        raise HouseHunterError("Ranking bundle population must be a positive estimate")
    status_columns = [column for column in COUNTY_COLUMNS if column.endswith("_status")]
    if frame.filter(
        pl.any_horizontal(
            *[
                pl.col(column).is_null() | pl.col(column).str.strip_chars().eq("")
                for column in status_columns
            ]
        )
    ).height:
        raise HouseHunterError("Ranking bundle source statuses must be explicit")
    complete_public_core = pl.all_horizontal(
        *[pl.col(column).is_not_null() for column in CORE_UTILITY_COLUMNS]
    )
    if frame.filter(
        (complete_public_core & ~pl.col("coverage_status").eq("complete_public_core"))
        | (~complete_public_core & ~pl.col("coverage_status").eq("partial_public_core"))
    ).height:
        raise HouseHunterError("Ranking bundle public-core coverage status differs")
    if not set(frame["rpp_geography_type"].unique()) <= {"metropolitan", "state"}:
        raise HouseHunterError("Ranking bundle RPP geography must be metropolitan or state")
    if frame.filter(
        pl.col("rpp_geography_type").eq("state") & pl.col("rpp_index").is_null()
    ).height:
        raise HouseHunterError("State RPP assignment left values null")
    if frame.filter(
        pl.col("u_broadband").is_not_null()
        & ~pl.col("broadband_denominator_label").eq("broadband-serviceable locations")
    ).height:
        raise HouseHunterError("Ranking broadband denominator label differs")
    if frame.filter(
        pl.col("public_water_coverage").is_not_null()
        & ~pl.col("public_water_coverage_kind").eq("max_intersection_proxy")
    ).height:
        raise HouseHunterError("Ranking public-water coverage is not labeled as a proxy")
    if not set(frame["water_boundary_provenance"].drop_nulls().unique()) <= {
        "supplied",
        "modeled",
        "mixed",
    }:
        raise HouseHunterError("Ranking water boundary provenance differs")
    incomplete_climate = frame.filter(
        (pl.col("climate_station_count") <= 0)
        & pl.any_horizontal(
            *[
                pl.col(column).is_not_null()
                for column in (
                    "jan_avg_temp_f",
                    "jul_avg_temp_f",
                    "extreme_heat_days",
                    "extreme_cold_days",
                )
            ]
        )
    )
    if incomplete_climate.height:
        raise HouseHunterError("Ranking climate values exist without a qualifying station")
    for row in frame.select("county_fips", "homeschool_rubric_json", "limitations_json").iter_rows(
        named=True
    ):
        try:
            rubric = json.loads(row["homeschool_rubric_json"])
            limitations = json.loads(row["limitations_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HouseHunterError(
                f"Ranking bundle detail JSON is malformed for {row['county_fips']}"
            ) from exc
        if not isinstance(rubric, dict) or not isinstance(limitations, list):
            raise HouseHunterError(f"Ranking bundle detail JSON differs for {row['county_fips']}")
    _validate_utilities(frame)
    _reject_zeroed_suppression(frame)
    _require_precomputed_pillars(frame)
    if scope == "national":
        if source_lock is None:
            raise HouseHunterError("National ranking validation requires the reviewed source lock")
        _validate_calibration_identity(frame)
        universe = source_lock["county_universe"]
        digest = sha256_bytes(canonical_json(fips))
        if frame.height != universe["row_count"] or digest != universe["sorted_fips_sha256"]:
            raise HouseHunterError("National ranking bundle county universe differs from its lock")
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
    expected_entries = {"manifest.json", *ARTIFACT_FILES.values()}
    actual_entries = {entry.name for entry in root.iterdir()}
    if actual_entries != expected_entries or any(entry.is_symlink() for entry in root.iterdir()):
        raise HouseHunterError("Ranking v2 bundle inventory differs from its reviewed contract")
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
        or manifest.get("release_id") != ranking_bundle_identity(manifest)
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
    lock_path = source_lock_path or default_source_lock_path()
    source_lock = load_source_lock(lock_path)
    if sha256_file(lock_path) != manifest.get("source_lock_sha256"):
        raise HouseHunterError("Ranking v2 source lock hash differs")
    _validate_counties(
        frames["counties"],
        scope=str(manifest["scope"]),
        source_lock=source_lock if manifest["scope"] == "national" else None,
    )
    if manifest["scope"] == "national":
        locked_pillar_internals(calibration)
        if (
            calibration.get("housing_sqft_bounds") != HOUSING_SQFT_BOUNDS
            or calibration.get("housing_sqft_knots") != HOUSING_SQFT_KNOTS
            or calibration.get("crime_coverage_floor") != CRIME_COVERAGE_FLOOR
            or calibration.get("water_allocation_coverage_floor") != 0.90
        ):
            raise HouseHunterError("Ranking v2 locked calibration policy differs")
        if calibration.get("ecdf_components") != ecdf_calibration_audit(frames["counties"]):
            raise HouseHunterError("Ranking v2 ECDF calibration audit differs")
    homeschool = frames["homeschool"]
    if homeschool.get("notice") != HOMESCHOOL_NOTICE:
        raise HouseHunterError("Ranking v2 homeschool notice is missing")
    citations = frames["citations"]
    if (
        not isinstance(citations, dict)
        or not citations
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in citations.items()
        )
    ):
        raise HouseHunterError("Ranking v2 citations are incomplete")
    county_fips = frames["counties"]["county_fips"].to_list()
    if manifest.get("row_count") != len(county_fips) or manifest.get(
        "county_fips_sha256"
    ) != sha256_bytes(canonical_json(county_fips)):
        raise HouseHunterError("Ranking v2 manifest county identity differs")
    if manifest.get("coverage") != bundle_coverage_summary(frames["counties"]):
        raise HouseHunterError("Ranking v2 manifest coverage summary differs")
    for row in frames["counties"].select("county_fips", "citation_ids_json").iter_rows(named=True):
        try:
            citation_ids = json.loads(row["citation_ids_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HouseHunterError(
                f"Ranking v2 county {row['county_fips']} citation IDs are malformed"
            ) from exc
        if (
            not isinstance(citation_ids, list)
            or not citation_ids
            or len(citation_ids) != len(set(citation_ids))
            or any(citation_id not in citations for citation_id in citation_ids)
        ):
            raise HouseHunterError(
                f"Ranking v2 county {row['county_fips']} citations are incomplete"
            )
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
    source_lock = load_source_lock(source_lock_path or default_source_lock_path())
    _validate_counties(
        counties,
        scope=scope,
        source_lock=source_lock if scope == "national" else None,
    )
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
            "county_fips_sha256": sha256_bytes(canonical_json(counties["county_fips"].to_list())),
            "row_count": counties.height,
            "coverage": bundle_coverage_summary(counties),
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
        manifest["release_id"] = ranking_bundle_identity(manifest)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        validate_ranking_assets(staging, source_lock_path=lock_path)
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
        return validate_ranking_assets(output, source_lock_path=lock_path)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def empty_ranking_sidecar() -> pl.DataFrame:
    string_columns = {
        "place_id",
        "name",
        "state",
        *(
            column
            for column in COUNTY_COLUMNS
            if column.endswith(("_status", "_source", "_vintage", "_json", "_kind"))
        ),
        "rpp_geography_type",
        "water_boundary_provenance",
        "broadband_denominator_label",
    }
    integer_columns = {"population", "climate_station_count", "housing_valid_months"}
    schema = {
        column: (
            pl.String
            if column in string_columns
            else pl.Int64
            if column in integer_columns
            else pl.Float64
        )
        for column in RANKING_SIDECAR_COLUMNS
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
    unexpected_missing = {
        fips for fips in identity_ids - bundle_ids if fips[:2] in IN_SCOPE_STATE_FIPS
    }
    if bundle.manifest.get("scope") == "national" and unexpected_missing:
        raise HouseHunterError("Ranking bundle is missing in-scope snapshot counties")
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
        if months is not None and int(months) >= 9
        else None
        for value, months in zip(
            joined["sqft_for_1m_t12"].to_list(),
            joined["housing_valid_months"].to_list(),
            strict=True,
        )
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
    policy = load_homeschool_policy()
    better = {
        "county_fips": "02001",
        "state": "AK",
        "population": 80_000,
        "population_vintage": "2025",
        "res_hazard_npctl": 10.0,
        "u_hazard": 0.90,
        "crime_violent_rate": 100.0,
        "crime_property_rate": 800.0,
        "crime_coverage": 0.95,
        "u_crime": 0.85,
        "water_violation_share": 0.01,
        "public_water_coverage": 0.90,
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
        "employment_growth": 0.03,
        "average_weekly_wage": 1450.0,
        "commute_under_30_share": 0.70,
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
        "coverage_status": "complete_public_core",
    }
    worse = {
        "county_fips": "01001",
        "state": "AL",
        "population": 58_000,
        "population_vintage": "2025",
        "res_hazard_npctl": 70.0,
        "u_hazard": 0.30,
        "crime_violent_rate": 400.0,
        "crime_property_rate": 2200.0,
        "crime_coverage": 0.92,
        "u_crime": 0.40,
        "water_violation_share": 0.08,
        "public_water_coverage": 0.80,
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
        "employment_growth": -0.01,
        "average_weekly_wage": 850.0,
        "commute_under_30_share": 0.45,
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
        "coverage_status": "complete_public_core",
    }
    rows = []
    for row in (worse, better):
        complete = "complete"
        for column in (
            "population_status",
            "hazard_status",
            "crime_status",
            "water_status",
            "healthcare_status",
            "community_context_status",
            "rpp_status",
            "property_tax_status",
            "employment_status",
            "broadband_status",
            "mountain_status",
            "family_status",
        ):
            row[column] = complete
        for column in (
            "crime_violent_coverage_2023",
            "crime_violent_coverage_2024",
            "crime_violent_coverage_2025",
            "crime_property_coverage_2023",
            "crime_property_coverage_2024",
            "crime_property_coverage_2025",
        ):
            row[column] = row["crime_coverage"]
        row.update(
            {
                "public_water_coverage_kind": "max_intersection_proxy",
                "water_allocation_coverage": 0.96,
                "water_boundary_provenance": "mixed",
                "water_overlap_duplicate_share_proxy": 0.05,
                "water_overlap_quality_status": "max_intersection_proxy",
                "u_crime_violent": row["u_crime"],
                "u_crime_property": row["u_crime"],
                "u_primary_care": row["u_healthcare"],
                "u_mental_health": row["u_healthcare"],
                "u_dental": row["u_healthcare"],
                "provider_primary_care_source": "HRSA AHRF",
                "provider_mental_health_source": "CHR&R NPPES",
                "provider_dental_source": "HRSA AHRF",
                "provider_primary_care_vintage": "2023",
                "provider_mental_health_vintage": "2025",
                "provider_dental_vintage": "2023",
                "u_employment_growth": row["u_employment"],
                "u_average_weekly_wage": row["u_employment"],
                "u_commute_under_30": row["u_employment"],
                "broadband_denominator_label": "broadband-serviceable locations",
                "homeschool_rubric_json": json.dumps(
                    {"official_source": policy["official_sources"][row["state"]]},
                    separators=(",", ":"),
                ),
                "climate_station_count": 1,
                "citation_ids_json": json.dumps(
                    [
                        "population",
                        "hazard",
                        "crime",
                        "water",
                        "healthcare",
                        "community_context",
                        "rpp",
                        "property_tax",
                        "employment",
                        "broadband",
                        "mountain",
                        "climate",
                        "homeschool",
                    ],
                    separators=(",", ":"),
                ),
                "limitations_json": "[]",
            }
        )
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
        "housing_sqft_bounds": HOUSING_SQFT_BOUNDS,
        "housing_sqft_knots": HOUSING_SQFT_KNOTS,
        "crime_coverage_floor": CRIME_COVERAGE_FLOOR,
        "water_allocation_coverage_floor": 0.90,
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
