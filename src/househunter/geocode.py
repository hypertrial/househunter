"""Map a US address to a 2020 Census tract GEOID via Census, with Nominatim fallback."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .config import RuntimePaths
from .errors import AmbiguousPlaceError, CensusNoMatchError, HouseHunterError
from .store import Store

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_COORDINATES_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
GEOCODER_BENCHMARK = "Public_AR_Current"
GEOCODER_VINTAGE = "Census2020_Current"
NOMINATIM_DEFAULT_URL = "https://nominatim.openstreetmap.org"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
USER_AGENT = "HouseHunter/1.0 (local address lookup)"
MAX_ADDRESS_LENGTH = 200
MAX_GEOCODER_BYTES = 512_000
MAX_ADDRESS_MATCHES = 20
MAX_NOMINATIM_BYTES = 512_000
MAX_NOMINATIM_RESULTS = 5
NOMINATIM_CACHE_SIZE = 64
NOMINATIM_MIN_INTERVAL = 1.0
LOOKUP_EXAMPLE = "1670 Broadway, Denver, CO"
ADDRESS_NOT_FOUND = (
    "Address not found. Use a real number, street, city, and 2-letter state "
    f"(for example {LOOKUP_EXAMPLE}). Street names must be spelled correctly. "
    "New subdivisions may need an OpenStreetMap street confirmation."
)
_TRACT_ID = re.compile(r"^\d{11}$")
_HOUSE_NUMBER = re.compile(r"^\s*(\d+[A-Za-z]?)\b")
_UNIT_DESIGNATOR = re.compile(
    r"(?:,\s*)?(?:#\s*|"
    r"\b(?:apt|apartment|unit|ste|suite|bldg|building|rm|room|fl|floor)\.?\s+)"
    r"[A-Za-z]*\d[A-Za-z0-9-]*(?=\s*,|\s*$)",
    re.IGNORECASE,
)
_HOUSE_TYPES = frozenset({"house", "building", "yes"})
_STREET_TYPES = frozenset(
    {
        "road",
        "residential",
        "living_street",
        "tertiary",
        "secondary",
        "primary",
        "unclassified",
        "service",
    }
)
_LOCALITY_TYPES = frozenset(
    {
        "city",
        "town",
        "village",
        "municipality",
        "suburb",
        "neighbourhood",
        "hamlet",
        "county",
        "state",
        "country",
        "postcode",
        "postcode_area",
        "administrative",
        "island",
        "archipelago",
    }
)

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class AddressMatch:
    query: str
    matched_address: str
    tract_id: str
    provider: Literal["census", "nominatim"] = "census"
    precision: Literal["house", "street"] = "house"
    approximate: bool = False
    attribution: str | None = None


@dataclass(frozen=True, slots=True)
class NominatimHit:
    matched_address: str
    lat: float
    lon: float
    precision: Literal["house", "street"]


@dataclass
class _CandidateRecord:
    query: str
    matched_address: str
    lat: float
    lon: float
    precision: Literal["street"]


class _BoundedCache:
    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._items: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> object | None:
        with self._lock:
            if key not in self._items:
                return None
            self._items.move_to_end(key)
            return self._items[key]

    def set(self, key: str, value: object) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._maxsize:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class _RateLimiter:
    def __init__(self, interval: float, clock: Clock, sleeper: Sleeper) -> None:
        self.interval = interval
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last: float | None = None

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                wait_for = self.interval - (now - self._last)
                if wait_for > 0:
                    self._sleeper(wait_for)
                    now = self._clock()
            self._last = now


_nominatim_cache = _BoundedCache(NOMINATIM_CACHE_SIZE)
_candidates: dict[str, _CandidateRecord] = {}
_candidates_lock = threading.Lock()
_limiter = _RateLimiter(NOMINATIM_MIN_INTERVAL, time.monotonic, time.sleep)


def reset_geocode_runtime(
    *,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> None:
    global _limiter
    _nominatim_cache.clear()
    with _candidates_lock:
        _candidates.clear()
    _limiter = _RateLimiter(
        NOMINATIM_MIN_INTERVAL,
        clock or time.monotonic,
        sleeper or time.sleep,
    )


def _http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(15, connect=10),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": USER_AGENT},
    )


def _geoid_from_row(row: dict[str, Any]) -> str:
    raw = row.get("GEOID")
    if isinstance(raw, bool) or raw is None:
        return ""
    if isinstance(raw, int):
        if 0 <= raw <= 99_999_999_999:
            return f"{raw:011d}"
        return ""
    return str(raw)


def _tract_ids_from_geographies(geographies: object) -> list[str]:
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


def _tract_ids_from_match(match: dict[str, Any]) -> list[str]:
    return _tract_ids_from_geographies(match.get("geographies"))


def _query(address: str) -> str:
    query = address.strip()
    if not query:
        raise HouseHunterError("Address is required")
    if len(query) > MAX_ADDRESS_LENGTH:
        raise HouseHunterError(f"Address must be at most {MAX_ADDRESS_LENGTH} characters")
    return query


def _strip_secondary_unit(query: str) -> str:
    stripped = _UNIT_DESIGNATOR.sub("", query)
    stripped = re.sub(r"\s+,", ",", stripped)
    stripped = re.sub(r",\s*,+", ",", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip(" ,")
    return stripped or query


def _with_query(match: AddressMatch, query: str) -> AddressMatch:
    if match.query == query:
        return match
    return AddressMatch(
        query=query,
        matched_address=match.matched_address,
        tract_id=match.tract_id,
        provider=match.provider,
        precision=match.precision,
        approximate=match.approximate,
        attribution=match.attribution,
    )


def _http_json(
    http: httpx.Client,
    url: str,
    params: dict[str, str],
    *,
    label: str,
    maximum_bytes: int,
) -> object:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    with http.stream("GET", url, params=params, headers=headers) as response:
        response.raise_for_status()
        try:
            length = response.headers.get("content-length")
            encoding = response.headers.get("content-encoding", "identity").lower()
            if encoding != "identity":
                raise HouseHunterError(f"{label} returned a malformed response")
            if length is not None and int(length) > maximum_bytes:
                raise HouseHunterError(f"{label} returned a malformed response")
        except ValueError as exc:
            raise HouseHunterError(f"{label} returned a malformed response") from exc
        if response.is_stream_consumed:
            content: bytes | bytearray = response.content
            if len(content) > maximum_bytes:
                raise HouseHunterError(f"{label} returned a malformed response")
        else:
            content = bytearray()
            for chunk in response.iter_raw():
                if len(content) + len(chunk) > maximum_bytes:
                    raise HouseHunterError(f"{label} returned a malformed response")
                content.extend(chunk)
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"{label} returned invalid JSON") from exc


def geocode_tract(address: str, *, client: httpx.Client | None = None) -> AddressMatch:
    query = _query(address)
    owns_client = client is None
    http = client or _http_client()
    try:
        return _census_forward(query, http)
    finally:
        if owns_client:
            http.close()


def _census_forward(query: str, http: httpx.Client) -> AddressMatch:
    try:
        payload = _http_json(
            http,
            GEOCODER_URL,
            {
                "address": query,
                "benchmark": GEOCODER_BENCHMARK,
                "vintage": GEOCODER_VINTAGE,
                "format": "json",
            },
            label="Census geocoder",
            maximum_bytes=MAX_GEOCODER_BYTES,
        )
    except httpx.HTTPError as exc:
        raise HouseHunterError("Census geocoder request failed") from exc
    if not isinstance(payload, dict):
        raise HouseHunterError("Census geocoder returned a malformed response")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HouseHunterError("Census geocoder returned a malformed response")
    matches = result.get("addressMatches")
    if not isinstance(matches, list):
        raise HouseHunterError("Census geocoder returned a malformed response")
    if not matches:
        raise CensusNoMatchError(ADDRESS_NOT_FOUND)
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
        raise HouseHunterError(ADDRESS_NOT_FOUND)
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


def _census_tract_at(lat: float, lon: float, http: httpx.Client) -> str:
    try:
        payload = _http_json(
            http,
            CENSUS_COORDINATES_URL,
            {
                "x": f"{lon:.7f}",
                "y": f"{lat:.7f}",
                "benchmark": GEOCODER_BENCHMARK,
                "vintage": GEOCODER_VINTAGE,
                "format": "json",
            },
            label="Census geocoder",
            maximum_bytes=MAX_GEOCODER_BYTES,
        )
    except httpx.HTTPError as exc:
        raise HouseHunterError("Census geocoder request failed") from exc
    if not isinstance(payload, dict):
        raise HouseHunterError("Census geocoder returned a malformed response")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HouseHunterError("Census geocoder returned a malformed response")
    tract_ids = _tract_ids_from_geographies(result.get("geographies"))
    if len(tract_ids) != 1:
        raise HouseHunterError(ADDRESS_NOT_FOUND)
    return tract_ids[0]


def nominatim_base_url() -> str | None:
    raw = os.environ.get("HOUSEHUNTER_NOMINATIM_URL", NOMINATIM_DEFAULT_URL).strip()
    if raw.lower() in {"", "off", "disabled"}:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HouseHunterError("Nominatim URL must be https")
    return raw.rstrip("/")


def _house_number(text: str) -> str:
    match = _HOUSE_NUMBER.match(text)
    return match.group(1).upper() if match else ""


def _finite_point(lat: object, lon: object) -> tuple[float, float] | None:
    try:
        parsed_lat = float(str(lat))
        parsed_lon = float(str(lon))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed_lat) or not math.isfinite(parsed_lon):
        return None
    if not (-90 <= parsed_lat <= 90 and -180 <= parsed_lon <= 180):
        return None
    return parsed_lat, parsed_lon


def _classify_nominatim(query: str, row: dict[str, Any]) -> NominatimHit | None:
    point = _finite_point(row.get("lat"), row.get("lon"))
    if point is None:
        return None
    lat, lon = point
    address = row.get("address")
    details = address if isinstance(address, dict) else {}
    country = str(details.get("country_code") or "").lower()
    if country != "us":
        return None
    addresstype = str(row.get("addresstype") or row.get("type") or "").lower()
    category = str(row.get("category") or "").lower()
    if addresstype in _LOCALITY_TYPES or (category == "place" and addresstype in _LOCALITY_TYPES):
        return None
    display = str(row.get("display_name") or "").strip()
    if not display:
        return None
    returned_house = str(details.get("house_number") or "").upper()
    wanted_house = _house_number(query)
    house_like = addresstype in _HOUSE_TYPES or category == "building"
    if house_like and wanted_house and returned_house == wanted_house:
        return NominatimHit(matched_address=display, lat=lat, lon=lon, precision="house")
    highway_street = category == "highway" and addresstype in _STREET_TYPES | {""}
    if addresstype in _STREET_TYPES or highway_street:
        return NominatimHit(matched_address=display, lat=lat, lon=lon, precision="street")
    if category == "highway" and addresstype not in _LOCALITY_TYPES:
        return NominatimHit(matched_address=display, lat=lat, lon=lon, precision="street")
    return None


def _unique_hits(hits: list[NominatimHit]) -> list[NominatimHit]:
    unique: dict[tuple[str, float, float], NominatimHit] = {}
    for hit in hits:
        key = (hit.precision, round(hit.lat, 5), round(hit.lon, 5))
        unique.setdefault(key, hit)
    return list(unique.values())


def _search_nominatim(query: str, http: httpx.Client) -> list[NominatimHit]:
    cached = _nominatim_cache.get(query)
    if cached is not None:
        return list(cached)  # type: ignore[arg-type]
    base = nominatim_base_url()
    if base is None:
        raise CensusNoMatchError(ADDRESS_NOT_FOUND)
    _limiter.wait()
    try:
        payload = _http_json(
            http,
            f"{base}/search",
            {
                "q": query,
                "format": "jsonv2",
                "addressdetails": "1",
                "countrycodes": "us",
                "limit": str(MAX_NOMINATIM_RESULTS),
            },
            label="Nominatim",
            maximum_bytes=MAX_NOMINATIM_BYTES,
        )
    except httpx.HTTPError as exc:
        raise HouseHunterError("Nominatim request failed") from exc
    if not isinstance(payload, list):
        raise HouseHunterError("Nominatim returned a malformed response")
    hits = [
        classified
        for row in payload[:MAX_NOMINATIM_RESULTS]
        if isinstance(row, dict)
        for classified in [_classify_nominatim(query, row)]
        if classified is not None
    ]
    unique = _unique_hits(hits)
    _nominatim_cache.set(query, unique)
    return unique


def _remember_candidate(query: str, hit: NominatimHit) -> str:
    candidate_id = secrets.token_urlsafe(16)
    with _candidates_lock:
        if len(_candidates) >= NOMINATIM_CACHE_SIZE:
            _candidates.pop(next(iter(_candidates)))
        _candidates[candidate_id] = _CandidateRecord(
            query=query,
            matched_address=hit.matched_address,
            lat=hit.lat,
            lon=hit.lon,
            precision="street",
        )
    return candidate_id


def _load_candidate(query: str, candidate_id: str) -> _CandidateRecord:
    with _candidates_lock:
        record = _candidates.get(candidate_id)
    if record is None or record.query != query:
        raise HouseHunterError("Approximate street match is no longer available")
    return record


def _resolved_payload(paths: RuntimePaths, match: AddressMatch) -> dict[str, object]:
    with Store(paths) as store:
        detail = store.place_detail(store.resolve_place(match.tract_id))
    return {
        "status": "resolved",
        "query": match.query,
        "matched_address": match.matched_address,
        "tract_id": match.tract_id,
        "detail": detail,
        "provider": match.provider,
        "precision": match.precision,
        "approximate": match.approximate,
        "attribution": match.attribution,
    }


def _match_from_hit(query: str, hit: NominatimHit, http: httpx.Client) -> AddressMatch:
    tract_id = _census_tract_at(hit.lat, hit.lon, http)
    return AddressMatch(
        query=query,
        matched_address=hit.matched_address,
        tract_id=tract_id,
        provider="nominatim",
        precision=hit.precision,
        approximate=hit.precision == "street",
        attribution=OSM_ATTRIBUTION,
    )


def _nominatim_fallback(
    query: str,
    http: httpx.Client,
    *,
    candidate_id: str | None,
    allow_approximate: bool,
    lookup_text: str,
) -> AddressMatch | dict[str, object]:
    if candidate_id:
        record = _load_candidate(query, candidate_id)
        return _match_from_hit(
            query,
            NominatimHit(
                matched_address=record.matched_address,
                lat=record.lat,
                lon=record.lon,
                precision=record.precision,
            ),
            http,
        )
    hits = _search_nominatim(lookup_text, http)
    if not hits:
        raise CensusNoMatchError(ADDRESS_NOT_FOUND)
    houses = [hit for hit in hits if hit.precision == "house"]
    streets = [hit for hit in hits if hit.precision == "street"]
    if len(houses) > 1:
        raise AmbiguousPlaceError(
            query,
            [{"place_id": "", "matched_address": hit.matched_address} for hit in houses],
        )
    if houses:
        return _match_from_hit(query, houses[0], http)
    if len(streets) > 1:
        raise AmbiguousPlaceError(
            query,
            [{"place_id": "", "matched_address": hit.matched_address} for hit in streets],
        )
    hit = streets[0]
    if allow_approximate:
        return _match_from_hit(query, hit, http)
    stored_id = _remember_candidate(query, hit)
    return {
        "status": "confirmation_required",
        "query": query,
        "message": (
            "Census has no street range for that address. OpenStreetMap matched a road. "
            "A road representative point may cross tract boundaries."
        ),
        "attribution": OSM_ATTRIBUTION,
        "candidates": [
            {
                "candidate_id": stored_id,
                "matched_address": hit.matched_address,
                "precision": "street",
            }
        ],
    }


def resolve_address(
    address: str,
    *,
    candidate_id: str | None = None,
    allow_approximate: bool = False,
    client: httpx.Client | None = None,
) -> AddressMatch | dict[str, object]:
    query = _query(address)
    lookup = _strip_secondary_unit(query)
    owns_client = client is None
    http = client or _http_client()
    try:
        try:
            return _with_query(geocode_tract(lookup, client=http), query)
        except CensusNoMatchError:
            return _nominatim_fallback(
                query,
                http,
                candidate_id=candidate_id,
                allow_approximate=allow_approximate,
                lookup_text=lookup,
            )
    finally:
        if owns_client:
            http.close()


def lookup_address(
    paths: RuntimePaths,
    address: str,
    *,
    candidate_id: str | None = None,
    allow_approximate: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    result = resolve_address(
        address,
        candidate_id=candidate_id,
        allow_approximate=allow_approximate,
        client=client,
    )
    if isinstance(result, AddressMatch):
        return _resolved_payload(paths, result)
    return result
