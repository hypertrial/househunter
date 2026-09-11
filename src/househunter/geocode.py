"""Map a US address to a 2020 Census tract GEOID via the public Census geocoder."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import RuntimePaths
from .errors import AmbiguousPlaceError, HouseHunterError
from .store import Store

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
GEOCODER_BENCHMARK = "Public_AR_Current"
GEOCODER_VINTAGE = "Census2020_Current"
MAX_ADDRESS_LENGTH = 200
MAX_GEOCODER_BYTES = 512_000
MAX_ADDRESS_MATCHES = 20
_TRACT_ID = re.compile(r"^\d{11}$")


@dataclass(frozen=True, slots=True)
class AddressMatch:
    query: str
    matched_address: str
    tract_id: str


def _geoid_from_row(row: dict[str, Any]) -> str:
    raw = row.get("GEOID")
    if isinstance(raw, bool) or raw is None:
        return ""
    if isinstance(raw, int):
        if 0 <= raw <= 99_999_999_999:
            return f"{raw:011d}"
        return ""
    return str(raw)


def _tract_ids_from_match(match: dict[str, Any]) -> list[str]:
    geographies = match.get("geographies")
    if not isinstance(geographies, dict):
        return []
    found: list[str] = []
    for key, value in geographies.items():
        if "tract" not in str(key).lower() or not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            geoid = _geoid_from_row(row)
            if _TRACT_ID.fullmatch(geoid):
                found.append(geoid)
    return list(dict.fromkeys(found))


def geocode_tract(address: str, *, client: httpx.Client | None = None) -> AddressMatch:
    query = address.strip()
    if not query:
        raise HouseHunterError("Address is required")
    if len(query) > MAX_ADDRESS_LENGTH:
        raise HouseHunterError(f"Address must be at most {MAX_ADDRESS_LENGTH} characters")
    owns_client = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(15, connect=10),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "HouseHunter/1.0"},
    )
    try:
        try:
            response = http.get(
                GEOCODER_URL,
                params={
                    "address": query,
                    "benchmark": GEOCODER_BENCHMARK,
                    "vintage": GEOCODER_VINTAGE,
                    "format": "json",
                },
            )
            response.raise_for_status()
            if len(response.content) > MAX_GEOCODER_BYTES:
                raise HouseHunterError("Census geocoder returned a malformed response")
            payload = response.json()
        except httpx.HTTPError as exc:
            raise HouseHunterError("Census geocoder request failed") from exc
        except json.JSONDecodeError as exc:
            raise HouseHunterError("Census geocoder returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HouseHunterError("Census geocoder returned a malformed response")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise HouseHunterError("Census geocoder returned a malformed response")
        matches = result.get("addressMatches")
        if not matches:
            raise HouseHunterError("Address not found")
        if not isinstance(matches, list):
            raise HouseHunterError("Census geocoder returned a malformed response")
        resolved: list[tuple[str, str]] = []
        for match in matches[:MAX_ADDRESS_MATCHES]:
            if not isinstance(match, dict):
                continue
            tract_ids = _tract_ids_from_match(match)
            matched_address = str(match.get("matchedAddress") or query)
            if len(tract_ids) != 1:
                continue
            resolved.append((tract_ids[0], matched_address))
        unique_ids = list(dict.fromkeys(tract_id for tract_id, _address in resolved))
        if not unique_ids:
            raise HouseHunterError("Address not found")
        if len(unique_ids) > 1:
            raise AmbiguousPlaceError(
                query,
                [
                    {"place_id": tract_id, "matched_address": matched_address}
                    for tract_id, matched_address in resolved
                ],
            )
        tract_id, matched_address = resolved[0]
        return AddressMatch(query=query, matched_address=matched_address, tract_id=tract_id)
    finally:
        if owns_client:
            http.close()


def lookup_address(
    paths: RuntimePaths,
    address: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    match = geocode_tract(address, client=client)
    with Store(paths) as store:
        detail = store.place_detail(store.resolve_place(match.tract_id))
    return {
        "query": match.query,
        "matched_address": match.matched_address,
        "tract_id": match.tract_id,
        "detail": detail,
    }
