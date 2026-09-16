#!/usr/bin/env python3
"""Acquire and lock the public FBI CDE inputs used by Ranking v2.

This is a maintainer-only qualification tool.  It writes raw API responses below
``data/ranking-v2/raw`` and a deterministic, compressed request/response manifest.
Neither the responses nor agency-level rows are application assets.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from househunter.config import canonical_json, sha256_bytes
from househunter.errors import HouseHunterError
from househunter.geography import STATE_BY_FIPS
from househunter.ranking_reference import load_source_lock
from househunter.secure_fetch import (
    request_bounded_bytes,
    validate_public_dns,
    validated_https_url,
)

BASE_URL = "https://cde.ucr.cjis.gov"
CATALOG_PATH = "/LATEST/agency/byStateAbbr/{state}"
SUMMARY_PATH = "/LATEST/summarized/agency/{ori}/{offense}"
QUERY = {"from": "01-2023", "to": "12-2025", "type": "totals"}
OFFENSES = ("violent-crime", "property-crime")
INCLUDED_AGENCY_TYPES = frozenset(
    {"Borough", "City", "County", "Metropolitan", "Parish", "Township", "Village"}
)
EXPECTED_MONTHS = frozenset(
    f"{month:02d}-{year}" for year in range(2023, 2026) for month in range(1, 13)
)
CATALOG_MAX_BYTES = 16 * 1024 * 1024
SUMMARY_MAX_BYTES = 2 * 1024 * 1024
MANIFEST_SCHEMA = 1
CHECKPOINT_SCHEMA = 2
MIN_REQUEST_INTERVAL_SECONDS = 0.2
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_STARTED = 0.0


@dataclass(frozen=True, slots=True)
class ResponseEntry:
    kind: str
    key: str
    request: str
    status: int
    byte_count: int
    sha256: str


def _county_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.upper()).encode("ascii", "ignore").decode()
    normalized = normalized.replace("&", " AND ")
    normalized = re.sub(r"[^A-Z0-9 ]+", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for suffix in (
        " CITY AND BOROUGH",
        " CENSUS AREA",
        " MUNICIPALITY",
        " BOROUGH",
        " PARISH",
        " COUNTY",
    ):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].strip()
            break
    return normalized.replace(" ", "")


def _county_crosswalk(
    path: Path, source_lock_path: Path | None = None
) -> tuple[dict[tuple[str, str], str], list[str], str]:
    lock = load_source_lock(source_lock_path)
    census = next(
        source for source in lock["sources"] if source["name"] == "census_pep_county_2025"
    )
    artifact = census["artifacts"][0]
    try:
        if path.is_symlink() or not path.is_file():
            raise HouseHunterError("Census PEP county crosswalk must be a regular file")
        if (
            path.stat().st_size != artifact["bytes"]
            or sha256_bytes(path.read_bytes()) != artifact["sha256"]
        ):
            raise HouseHunterError("Census PEP county crosswalk differs from its source lock")
        with path.open(encoding="latin-1", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise HouseHunterError(f"Cannot read Census PEP county crosswalk: {exc}") from exc
    expected_fields = {"SUMLEV", "STATE", "COUNTY", "CTYNAME"}
    if not rows or not expected_fields <= set(rows[0]):
        raise HouseHunterError("Census PEP county crosswalk schema differs")
    crosswalk: dict[tuple[str, str], str] = {}
    universe: list[str] = []
    for row in rows:
        if row["SUMLEV"] != "050":
            continue
        state = STATE_BY_FIPS.get(row["STATE"])
        fips = row["STATE"] + row["COUNTY"]
        key = (str(state), _county_key(row["CTYNAME"]))
        if state not in _states() or len(fips) != 5 or not fips.isdigit() or not key[1]:
            raise HouseHunterError("Census PEP county crosswalk contains invalid geography")
        if key in crosswalk or fips in universe:
            raise HouseHunterError("Census PEP county crosswalk is not one-to-one")
        crosswalk[key] = fips
        universe.append(fips)
    universe.sort()
    reviewed = lock["county_universe"]
    if (
        len(universe) != reviewed["row_count"]
        or sha256_bytes(canonical_json(universe)) != reviewed["sorted_fips_sha256"]
    ):
        raise HouseHunterError("Census PEP county universe differs from its source lock")
    return crosswalk, universe, artifact["sha256"]


def _states() -> list[str]:
    return sorted(
        state for fips, state in STATE_BY_FIPS.items() if fips not in {"60", "66", "69", "72", "78"}
    )


def _contract() -> dict[str, Any]:
    return {
        "base_url": BASE_URL,
        "catalog_path": CATALOG_PATH,
        "summary_path": SUMMARY_PATH,
        "query": QUERY,
        "offenses": list(OFFENSES),
        "included_agency_types": sorted(INCLUDED_AGENCY_TYPES),
        "states": _states(),
        "catalog_max_bytes": CATALOG_MAX_BYTES,
        "summary_max_bytes": SUMMARY_MAX_BYTES,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _checkpoint_path(response_path: Path) -> Path:
    return response_path.with_suffix(response_path.suffix + ".checkpoint.json")


def _read_checkpointed_response(
    response_path: Path,
    *,
    status: int,
    contract_sha256: str,
    validation_schema: str,
    request_key: str,
    request: str,
    params: dict[str, str] | None,
    max_bytes: int,
    validator: Callable[[bytes], object],
) -> bytes | None:
    checkpoint_path = _checkpoint_path(response_path)
    if (
        response_path.is_symlink()
        or checkpoint_path.is_symlink()
        or not response_path.is_file()
        or not checkpoint_path.is_file()
    ):
        return None
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
        payload = response_path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    digest = hashlib.sha256(payload).hexdigest()
    if checkpoint != {
        "schema": CHECKPOINT_SCHEMA,
        "contract_sha256": contract_sha256,
        "validation_schema": validation_schema,
        "request_key": request_key,
        "request": request,
        "query": dict(sorted((params or {}).items())),
        "status": status,
        "bytes": len(payload),
        "sha256": digest,
    }:
        return None
    if not payload or len(payload) > max_bytes:
        return None
    validator(payload)
    return payload


def _write_response_checkpoint(
    response_path: Path,
    payload: bytes,
    *,
    status: int,
    contract_sha256: str,
    validation_schema: str,
    request_key: str,
    request: str,
    params: dict[str, str] | None,
) -> None:
    checkpoint = {
        "schema": CHECKPOINT_SCHEMA,
        "contract_sha256": contract_sha256,
        "validation_schema": validation_schema,
        "request_key": request_key,
        "request": request,
        "query": dict(sorted((params or {}).items())),
        "status": status,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    encoded = json.dumps(checkpoint, indent=2, sort_keys=True).encode() + b"\n"
    _atomic_write(_checkpoint_path(response_path), encoded)


def _read_json(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"{label} is not valid JSON") from exc


def _validate_catalog(
    payload: bytes,
    *,
    state: str,
    county_crosswalk: dict[tuple[str, str], str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    value = _read_json(payload, label=f"FBI {state} agency catalog")
    if not isinstance(value, dict):
        raise HouseHunterError(f"FBI {state} agency catalog is not an object")
    agencies: dict[str, dict[str, str]] = {}
    exclusions: dict[str, dict[str, str]] = {}
    for county_label, rows in value.items():
        if not isinstance(county_label, str) or not isinstance(rows, list):
            raise HouseHunterError(f"FBI {state} agency catalog has an invalid county group")
        for row in rows:
            if not isinstance(row, dict):
                raise HouseHunterError(f"FBI {state} agency catalog has a malformed agency")
            ori = row.get("ori")
            agency_type = row.get("agency_type_name")
            published_counties = row.get("counties")
            if (
                not isinstance(ori, str)
                or len(ori) != 9
                or not ori.isalnum()
                or not isinstance(agency_type, str)
                or not isinstance(published_counties, str)
                or published_counties != county_label
            ):
                raise HouseHunterError(f"FBI {state} agency catalog has an invalid identity")
            if agency_type not in INCLUDED_AGENCY_TYPES:
                continue
            if "," in published_counties:
                exclusion = {
                    "ori": ori,
                    "state": state,
                    "published_county": published_counties,
                    "reason": "multiple_county_attribution",
                }
                previous_exclusion = exclusions.setdefault(ori, exclusion)
                if previous_exclusion != exclusion:
                    raise HouseHunterError(f"FBI agency {ori} has conflicting exclusions")
                continue
            county_fips = county_crosswalk.get((state, _county_key(published_counties)))
            if county_fips is None:
                exclusion = {
                    "ori": ori,
                    "state": state,
                    "published_county": published_counties,
                    "reason": (
                        "missing_county_attribution"
                        if _county_key(published_counties) in {"", "NOTSPECIFIED"}
                        else "unassignable_current_county"
                    ),
                }
                previous_exclusion = exclusions.setdefault(ori, exclusion)
                if previous_exclusion != exclusion:
                    raise HouseHunterError(f"FBI agency {ori} has conflicting exclusions")
                continue
            candidate = {
                "ori": ori,
                "state": state,
                "published_county": published_counties,
                "agency_type": agency_type,
                "county_fips": county_fips,
            }
            previous = agencies.setdefault(ori, candidate)
            if previous != candidate:
                raise HouseHunterError(f"FBI agency {ori} has conflicting county attribution")
    if set(agencies) & set(exclusions):
        raise HouseHunterError("FBI agency has both resolved and excluded county attribution")
    return (
        [agencies[ori] for ori in sorted(agencies)],
        [exclusions[ori] for ori in sorted(exclusions)],
    )


def _agency_series(container: object, suffix: str) -> dict[str, Any]:
    if not isinstance(container, dict):
        raise HouseHunterError("FBI summary series is not an object")
    matches = [value for key, value in container.items() if str(key).endswith(suffix)]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise HouseHunterError(f"FBI summary must contain one agency {suffix} series")
    return matches[0]


def _validate_summary(payload: bytes, *, ori: str, offense: str) -> None:
    value = _read_json(payload, label=f"FBI {ori} {offense} summary")
    if not isinstance(value, dict):
        raise HouseHunterError(f"FBI {ori} {offense} summary is not an object")
    offenses = value.get("offenses")
    populations = value.get("populations")
    properties = value.get("cde_properties")
    if not isinstance(offenses, dict) or not isinstance(populations, dict):
        raise HouseHunterError(f"FBI {ori} {offense} summary is missing required groups")
    actual_container = offenses.get("actuals")
    actuals = _agency_series(actual_container, " Offenses")
    agency_keys = [
        str(key)[: -len(" Offenses")] for key in actual_container if str(key).endswith(" Offenses")
    ]
    if len(agency_keys) != 1:
        raise HouseHunterError(f"FBI {ori} {offense} agency identity is ambiguous")
    agency_name = agency_keys[0]
    population_container = populations.get("population")
    participated_container = populations.get("participated_population")
    if (
        not isinstance(population_container, dict)
        or not isinstance(participated_container, dict)
        or not isinstance(population_container.get(agency_name), dict)
        or not isinstance(participated_container.get(agency_name), dict)
    ):
        raise HouseHunterError(f"FBI {ori} {offense} agency population is missing")
    population = population_container[agency_name]
    participated = participated_container[agency_name]
    for label, series in (
        ("actuals", actuals),
        ("population", population),
        ("participated population", participated),
    ):
        if set(series) != EXPECTED_MONTHS:
            raise HouseHunterError(f"FBI {ori} {offense} {label} month set drifted")
        if any(
            value is not None and not isinstance(value, (int, float)) for value in series.values()
        ):
            raise HouseHunterError(f"FBI {ori} {offense} {label} value is invalid")
    if (
        not isinstance(properties, dict)
        or not isinstance(properties.get("last_refresh_date"), dict)
        or not isinstance(properties["last_refresh_date"].get("UCR"), str)
    ):
        raise HouseHunterError(f"FBI {ori} {offense} refresh metadata is missing")


def _request(
    client: httpx.Client,
    *,
    request: str,
    destination: Path,
    max_bytes: int,
    label: str,
    contract_sha256: str,
    validation_schema: str,
    request_key: str,
    validator: Callable[[bytes], object],
    expected_dns_addresses: frozenset[str] | None = None,
    validate_dns: bool | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    global _LAST_REQUEST_STARTED
    payload = _read_checkpointed_response(
        destination,
        status=200,
        contract_sha256=contract_sha256,
        validation_schema=validation_schema,
        request_key=request_key,
        request=request,
        params=params,
        max_bytes=max_bytes,
        validator=validator,
    )
    if payload is not None:
        return 200, payload
    last_error: Exception | None = None
    for attempt in range(8):
        try:
            with _REQUEST_LOCK:
                delay = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_STARTED)
                if delay > 0:
                    time.sleep(delay)
                _LAST_REQUEST_STARTED = time.monotonic()
            payload = request_bounded_bytes(
                client,
                request,
                params=params,
                allowed_hosts={"cde.ucr.cjis.gov"},
                max_bytes=max_bytes,
                label=label,
                expected_dns_addresses=expected_dns_addresses,
                validate_dns=(
                    expected_dns_addresses is not None if validate_dns is None else validate_dns
                ),
            )
            validator(payload)
            _atomic_write(destination, payload)
            _write_response_checkpoint(
                destination,
                payload,
                status=200,
                contract_sha256=contract_sha256,
                validation_schema=validation_schema,
                request_key=request_key,
                request=request,
                params=params,
            )
            return 200, payload
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            if attempt == 7:
                break
            time.sleep(min(30.0, 0.5 * (2**attempt)))
    raise HouseHunterError(f"{label} failed after bounded retries: {last_error}")


def _catalogs(
    client: httpx.Client,
    root: Path,
    contract_sha256: str,
    county_crosswalk: dict[tuple[str, str], str],
    expected_dns_addresses: frozenset[str] | None = None,
    validate_dns: bool | None = None,
) -> tuple[list[ResponseEntry], list[dict[str, str]], list[dict[str, str]]]:
    entries: list[ResponseEntry] = []
    agencies: dict[str, dict[str, str]] = {}
    exclusions: dict[str, dict[str, str]] = {}
    for state in _states():
        path = CATALOG_PATH.format(state=state)
        status, payload = _request(
            client,
            request=f"{BASE_URL}{path}",
            destination=root / "catalogs" / f"{state}.json",
            max_bytes=CATALOG_MAX_BYTES,
            label=f"FBI {state} agency catalog",
            contract_sha256=contract_sha256,
            validation_schema="fbi-agency-catalog-v1",
            request_key=f"catalog:{state}",
            validator=lambda value, state=state: _validate_catalog(
                value, state=state, county_crosswalk=county_crosswalk
            ),
            expected_dns_addresses=expected_dns_addresses,
            validate_dns=validate_dns,
        )
        state_agencies, state_exclusions = _validate_catalog(
            payload, state=state, county_crosswalk=county_crosswalk
        )
        for agency in state_agencies:
            previous = agencies.setdefault(agency["ori"], agency)
            if previous != agency:
                raise HouseHunterError(f"FBI agency {agency['ori']} appears in multiple states")
        for exclusion in state_exclusions:
            previous = exclusions.setdefault(exclusion["ori"], exclusion)
            if previous != exclusion:
                raise HouseHunterError(
                    f"FBI excluded agency {exclusion['ori']} appears in multiple states"
                )
        entries.append(
            ResponseEntry(
                kind="catalog",
                key=state,
                request=path,
                status=status,
                byte_count=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    if set(agencies) & set(exclusions):
        raise HouseHunterError("FBI agency is both included and excluded")
    return (
        entries,
        [agencies[ori] for ori in sorted(agencies)],
        [exclusions[ori] for ori in sorted(exclusions)],
    )


def _read_existing_response(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    contract_sha256: str,
    validation_schema: str,
    request_key: str,
    request: str,
    params: dict[str, str] | None,
    validator: Callable[[bytes], object],
) -> bytes:
    if path.is_symlink():
        raise HouseHunterError(f"{label} must be a regular file")
    payload = _read_checkpointed_response(
        path,
        status=200,
        contract_sha256=contract_sha256,
        validation_schema=validation_schema,
        request_key=request_key,
        request=request,
        params=params,
        max_bytes=max_bytes,
        validator=validator,
    )
    if payload is None:
        raise HouseHunterError(f"{label} is missing its exact acquisition checkpoint")
    return payload


def _catalogs_from_existing(
    root: Path,
    county_crosswalk: dict[tuple[str, str], str],
    contract_sha256: str,
) -> tuple[list[ResponseEntry], list[dict[str, str]], list[dict[str, str]]]:
    entries: list[ResponseEntry] = []
    agencies: dict[str, dict[str, str]] = {}
    exclusions: dict[str, dict[str, str]] = {}
    for state in _states():
        path = CATALOG_PATH.format(state=state)
        payload = _read_existing_response(
            root / "catalogs" / f"{state}.json",
            max_bytes=CATALOG_MAX_BYTES,
            label=f"FBI {state} agency catalog",
            contract_sha256=contract_sha256,
            validation_schema="fbi-agency-catalog-v1",
            request_key=f"catalog:{state}",
            request=f"{BASE_URL}{path}",
            params=None,
            validator=lambda value, state=state: _validate_catalog(
                value, state=state, county_crosswalk=county_crosswalk
            ),
        )
        state_agencies, state_exclusions = _validate_catalog(
            payload, state=state, county_crosswalk=county_crosswalk
        )
        for agency in state_agencies:
            previous = agencies.setdefault(agency["ori"], agency)
            if previous != agency:
                raise HouseHunterError(f"FBI agency {agency['ori']} appears in multiple states")
        for exclusion in state_exclusions:
            previous = exclusions.setdefault(exclusion["ori"], exclusion)
            if previous != exclusion:
                raise HouseHunterError(
                    f"FBI excluded agency {exclusion['ori']} appears in multiple states"
                )
        entries.append(
            ResponseEntry(
                kind="catalog",
                key=state,
                request=path,
                status=200,
                byte_count=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    if set(agencies) & set(exclusions):
        raise HouseHunterError("FBI agency is both included and excluded")
    return (
        entries,
        [agencies[ori] for ori in sorted(agencies)],
        [exclusions[ori] for ori in sorted(exclusions)],
    )


def _summaries_from_existing(
    root: Path, agencies: list[dict[str, str]], contract_sha256: str
) -> list[ResponseEntry]:
    entries: list[ResponseEntry] = []
    for agency in agencies:
        ori = agency["ori"]
        for offense in OFFENSES:
            request = SUMMARY_PATH.format(ori=ori, offense=offense)
            payload = _read_existing_response(
                root / "summaries" / offense / f"{ori}.json",
                max_bytes=SUMMARY_MAX_BYTES,
                label=f"FBI {ori} {offense} summary",
                contract_sha256=contract_sha256,
                validation_schema="fbi-agency-summary-v1",
                request_key=f"summary:{ori}:{offense}",
                request=f"{BASE_URL}{request}",
                params=QUERY,
                validator=lambda value, ori=ori, offense=offense: _validate_summary(
                    value, ori=ori, offense=offense
                ),
            )
            entries.append(
                ResponseEntry(
                    kind="summary",
                    key=f"{ori}:{offense}",
                    request=request,
                    status=200,
                    byte_count=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
    return entries


def _summary(
    client: httpx.Client,
    root: Path,
    agency: dict[str, str],
    offense: str,
    contract_sha256: str,
    expected_dns_addresses: frozenset[str],
) -> ResponseEntry:
    ori = agency["ori"]
    path = SUMMARY_PATH.format(ori=ori, offense=offense)
    status, payload = _request(
        client,
        request=f"{BASE_URL}{path}",
        params=QUERY,
        destination=root / "summaries" / offense / f"{ori}.json",
        max_bytes=SUMMARY_MAX_BYTES,
        label=f"FBI {ori} {offense} summary",
        contract_sha256=contract_sha256,
        validation_schema="fbi-agency-summary-v1",
        request_key=f"summary:{ori}:{offense}",
        validator=lambda value: _validate_summary(value, ori=ori, offense=offense),
        expected_dns_addresses=expected_dns_addresses,
    )
    _validate_summary(payload, ori=ori, offense=offense)
    return ResponseEntry(
        kind="summary",
        key=f"{ori}:{offense}",
        request=path,
        status=status,
        byte_count=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _write_manifest(
    destination: Path,
    *,
    contract_sha256: str,
    catalogs: list[ResponseEntry],
    agencies: list[dict[str, str]],
    exclusions: list[dict[str, str]],
    county_universe: list[str],
    county_source_sha256: str,
    summaries: list[ResponseEntry],
) -> None:
    entries = sorted(catalogs + summaries, key=lambda item: (item.kind, item.key))
    entry_rows = [
        {
            "kind": item.kind,
            "key": item.key,
            "request": item.request,
            "status": item.status,
            "bytes": item.byte_count,
            "sha256": item.sha256,
        }
        for item in entries
    ]
    payload = {
        "schema": MANIFEST_SCHEMA,
        "retrieved_on": "2026-09-16",
        "contract_sha256": contract_sha256,
        "catalog_count": len(catalogs),
        "agency_count": len(agencies),
        "exclusion_count": len(exclusions),
        "summary_count": len(summaries),
        "response_count": len(entries),
        "response_bytes": sum(item.byte_count for item in entries),
        "responses_sha256": sha256_bytes(canonical_json(entry_rows)),
        "agencies_sha256": sha256_bytes(canonical_json(agencies)),
        "exclusions_sha256": sha256_bytes(canonical_json(exclusions)),
        "county_source_sha256": county_source_sha256,
        "county_universe_sha256": sha256_bytes(canonical_json(county_universe)),
        "agencies": agencies,
        "agency_exclusions": exclusions,
        "county_universe": county_universe,
        "responses": entry_rows,
    }
    raw = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    _atomic_write(destination, compressed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=Path("data/ranking-v2"), help="Private checkpoint root"
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("config/ranking/fbi-ucr-request-manifest-v1.json.gz"),
    )
    parser.add_argument(
        "--census-pep",
        type=Path,
        required=True,
        help="Exact locked Census PEP 2025 county CSV used for current-FIPS attribution",
    )
    parser.add_argument("--source-lock", type=Path)
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Revalidate existing private responses and rebuild only the compact manifest",
    )
    parser.add_argument("--workers", type=int, default=12)
    arguments = parser.parse_args()
    if not 1 <= arguments.workers <= 32:
        raise HouseHunterError("FBI acquisition workers must be between 1 and 32")
    county_crosswalk, county_universe, county_source_sha256 = _county_crosswalk(
        arguments.census_pep, arguments.source_lock
    )
    contract = _contract()
    contract_sha256 = sha256_bytes(canonical_json(contract))
    root = arguments.data_root / "raw" / "fbi" / contract_sha256
    root.mkdir(parents=True, exist_ok=True)
    contract_bytes = json.dumps(contract, indent=2, sort_keys=True).encode() + b"\n"
    _atomic_write(root / "contract.json", contract_bytes)
    if arguments.manifest_only:
        catalogs, agencies, exclusions = _catalogs_from_existing(
            root, county_crosswalk, contract_sha256
        )
        summaries = _summaries_from_existing(root, agencies, contract_sha256)
    else:
        validated = validated_https_url(BASE_URL, {"cde.ucr.cjis.gov"}, label="FBI CDE")
        expected_dns_addresses = validate_public_dns(validated, label="FBI CDE")
        with httpx.Client(
            timeout=httpx.Timeout(60, connect=20), follow_redirects=False, trust_env=False
        ) as client:
            catalogs, agencies, exclusions = _catalogs(
                client, root, contract_sha256, county_crosswalk, expected_dns_addresses
            )
            print(
                f"qualified {len(catalogs)} catalogs, {len(agencies)} current-county "
                f"agencies, and {len(exclusions)} excluded agencies"
            )
            summaries = []
            with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
                futures: dict[Future[ResponseEntry], tuple[str, str]] = {
                    executor.submit(
                        _summary,
                        client,
                        root,
                        agency,
                        offense,
                        contract_sha256,
                        expected_dns_addresses,
                    ): (
                        agency["ori"],
                        offense,
                    )
                    for agency in agencies
                    for offense in OFFENSES
                }
                for completed, future in enumerate(as_completed(futures), start=1):
                    try:
                        summaries.append(future.result())
                    except Exception:
                        for pending in futures:
                            pending.cancel()
                        raise
                    if completed % 250 == 0 or completed == len(futures):
                        print(f"qualified {completed}/{len(futures)} agency summaries", flush=True)
    _write_manifest(
        arguments.manifest_output,
        contract_sha256=contract_sha256,
        catalogs=catalogs,
        agencies=agencies,
        exclusions=exclusions,
        county_universe=county_universe,
        county_source_sha256=county_source_sha256,
        summaries=summaries,
    )
    compressed_manifest = arguments.manifest_output.read_bytes()
    uncompressed_manifest = gzip.decompress(compressed_manifest)
    print(
        json.dumps(
            {
                "manifest": str(arguments.manifest_output),
                "bytes": len(compressed_manifest),
                "sha256": hashlib.sha256(compressed_manifest).hexdigest(),
                "uncompressed_bytes": len(uncompressed_manifest),
                "uncompressed_sha256": hashlib.sha256(uncompressed_manifest).hexdigest(),
                "catalog_count": len(catalogs),
                "agency_count": len(agencies),
                "exclusion_count": len(exclusions),
                "summary_count": len(summaries),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
