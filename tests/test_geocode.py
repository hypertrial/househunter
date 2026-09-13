from __future__ import annotations

import httpx
import pytest

from househunter.errors import AmbiguousPlaceError, HouseHunterError
from househunter.geocode import (
    CENSUS_COORDINATES_URL,
    GEOCODER_BENCHMARK,
    GEOCODER_URL,
    GEOCODER_VINTAGE,
    MAX_GEOCODER_BYTES,
    MAX_NOMINATIM_BYTES,
    OSM_ATTRIBUTION,
    USER_AGENT,
    AddressMatch,
    geocode_tract,
    reset_geocode_runtime,
    resolve_address,
)


def _match(
    tract_id: str, matched_address: str = "1 MAIN ST, BOULDER, CO, 80302"
) -> dict[str, object]:
    return {
        "matchedAddress": matched_address,
        "geographies": {"Census Tracts": [{"GEOID": tract_id}]},
    }


def test_geocode_tract_returns_the_2020_geoid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(GEOCODER_URL)
        assert request.url.params["benchmark"] == GEOCODER_BENCHMARK
        assert request.url.params["vintage"] == GEOCODER_VINTAGE
        assert request.url.params["address"] == "1 Main St, Boulder, CO"
        return httpx.Response(
            200,
            json={"result": {"addressMatches": [_match("08013012101")]}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match = geocode_tract("  1 Main St, Boulder, CO  ", client=client)
    assert match.tract_id == "08013012101"
    assert match.matched_address == "1 MAIN ST, BOULDER, CO, 80302"
    assert match.query == "1 Main St, Boulder, CO"


def test_geocode_tract_zero_pads_numeric_geoids() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "addressMatches": [
                        {
                            "matchedAddress": "1 MAIN ST, AUTAUGA, AL, 36003",
                            "geographies": {"Census Tracts": [{"GEOID": 1001000100}]},
                        }
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match = geocode_tract("1 Main St, Autauga, AL", client=client)
    assert match.tract_id == "01001000100"


def test_geocode_tract_rejects_empty_and_unmatched_addresses() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"addressMatches": []}})

    with pytest.raises(HouseHunterError, match="Address is required"):
        geocode_tract("   ")
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        geocode_tract("not an address", client=client)


def test_geocode_tract_rejects_disagreeing_matches() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "addressMatches": [
                        _match("08013012101", "1 MAIN ST, BOULDER, CO, 80302"),
                        _match("01001000100", "1 MAIN ST, AUTAUGA, AL, 36003"),
                    ]
                }
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AmbiguousPlaceError) as caught,
    ):
        geocode_tract("1 Main St", client=client)
    assert [item["place_id"] for item in caught.value.candidates] == [
        "08013012101",
        "01001000100",
    ]


def test_geocode_tract_accepts_duplicate_matches_for_one_tract() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "addressMatches": [
                        _match("08013012101", "1 MAIN ST, BOULDER, CO, 80302"),
                        _match("08013012101", "1 MAIN STREET, BOULDER, CO, 80302"),
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match = geocode_tract("1 Main St, Boulder, CO", client=client)
    assert match.tract_id == "08013012101"


def test_geocode_tract_reports_a_geocoder_http_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="Census geocoder request failed"),
    ):
        geocode_tract("1 Main St, Boulder, CO", client=client)


def test_geocoder_stops_streaming_when_the_response_exceeds_its_limit() -> None:
    chunks_read = 0

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal chunks_read
            for _ in range(4):
                chunks_read += 1
                yield b"x" * (MAX_GEOCODER_BYTES // 2)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedStream())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="malformed"),
    ):
        geocode_tract("1 Main St, Boulder, CO", client=client)

    assert chunks_read == 3


def test_geocoder_rejects_compressed_content_before_reading() -> None:
    class UnreadStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("compressed response body was read")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=UnreadStream(),
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="malformed"),
    ):
        geocode_tract("1 Main St, Boulder, CO", client=client)


def test_geocode_owned_client_disables_redirects_and_env_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host != "geocoding.geo.census.gov":
            return httpx.Response(200, text="stolen")
        return httpx.Response(302, headers={"Location": "https://example.invalid/steal"})

    class CapturingClient(httpx.Client):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("househunter.geocode.httpx.Client", CapturingClient)
    with pytest.raises(HouseHunterError, match="Census geocoder request failed"):
        geocode_tract("1 Main St, Boulder, CO")
    assert captured.get("follow_redirects") is False
    assert captured.get("trust_env") is False
    assert requested == [
        "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
        "?address=1+Main+St%2C+Boulder%2C+CO&benchmark=Public_AR_Current"
        "&vintage=Census2020_Current&format=json"
    ]


@pytest.fixture(autouse=True)
def _reset_geocode_runtime() -> object:
    reset_geocode_runtime()
    yield
    reset_geocode_runtime()


def _empty_census() -> httpx.Response:
    return httpx.Response(200, json={"result": {"addressMatches": []}})


def _coordinate_tract(tract_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": {"geographies": {"Census Tracts": [{"GEOID": tract_id}]}}},
    )


def _nominatim_row(
    *,
    lat: str,
    lon: str,
    display: str,
    addresstype: str,
    house_number: str | None = None,
    country: str | None = "us",
    category: str | None = None,
) -> dict[str, object]:
    address: dict[str, object] = {}
    if country is not None:
        address["country_code"] = country
    if house_number is not None:
        address["house_number"] = house_number
    row: dict[str, object] = {
        "lat": lat,
        "lon": lon,
        "display_name": display,
        "addresstype": addresstype,
        "address": address,
    }
    if category:
        row["category"] = category
    return row


def test_census_match_does_not_call_nominatim() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        return httpx.Response(
            200,
            json={"result": {"addressMatches": [_match("08013012101")]}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match = resolve_address("1 Main St, Boulder, CO", client=client)
    assert isinstance(match, AddressMatch)
    assert match.tract_id == "08013012101"
    assert match.provider == "census"
    assert "nominatim" not in "".join(hosts)


def test_census_zero_match_auto_resolves_matching_house() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        if request.url.path.endswith("/search"):
            assert request.url.params["format"] == "jsonv2"
            assert request.url.params["addressdetails"] == "1"
            assert request.url.params["countrycodes"] == "us"
            assert request.headers["user-agent"] == USER_AGENT
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="32.5",
                        lon="-86.5",
                        display="1 Main Street, Autauga, Alabama, United States",
                        addresstype="house",
                        house_number="1",
                    )
                ],
            )
        if str(request.url).startswith(CENSUS_COORDINATES_URL):
            assert request.url.params["x"] == "-86.5000000"
            assert request.url.params["y"] == "32.5000000"
            assert request.url.params["vintage"] == GEOCODER_VINTAGE
            return _coordinate_tract("01001000100")
        return _empty_census()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match = resolve_address("1 Main St, Autauga, AL", client=client)
    assert isinstance(match, AddressMatch)
    assert match.tract_id == "01001000100"
    assert match.provider == "nominatim"
    assert match.precision == "house"
    assert match.approximate is False
    assert match.attribution == OSM_ATTRIBUTION
    assert hosts.count("nominatim.openstreetmap.org") == 1


def test_lazy_cat_street_requires_confirmation_then_resolves_tract() -> None:
    query = "1720 Lazy Cat Ln, Monument, CO 80132"
    nominatim_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal nominatim_calls
        if request.url.path.endswith("/search"):
            nominatim_calls += 1
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="39.0695903",
                        lon="-104.7976434",
                        display="Lazy Cat Lane, Monument, El Paso County, Colorado, United States",
                        addresstype="road",
                        category="highway",
                    )
                ],
            )
        if str(request.url).startswith(CENSUS_COORDINATES_URL):
            return _coordinate_tract("08041007301")
        return _empty_census()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = resolve_address(query, client=client)
        assert isinstance(first, dict)
        assert first["status"] == "confirmation_required"
        assert first["attribution"] == OSM_ATTRIBUTION
        assert "lat" not in first["candidates"][0]
        assert first["candidates"][0]["precision"] == "street"
        assert "Lazy Cat" in first["candidates"][0]["matched_address"]
        candidate_id = first["candidates"][0]["candidate_id"]
        resolved = resolve_address(query, candidate_id=candidate_id, client=client)
    assert isinstance(resolved, AddressMatch)
    assert resolved.tract_id == "08041007301"
    assert resolved.provider == "nominatim"
    assert resolved.precision == "street"
    assert resolved.approximate is True
    assert nominatim_calls == 1


def test_typo_street_stays_a_no_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=[])
        return _empty_census()

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="spelled correctly"),
    ):
        resolve_address("1720 Lazt Cat, Monument, CO 80132", client=client)


def test_unit_designator_is_stripped_before_geocoders() -> None:
    seen: list[str] = []
    query = "1689 S 870 W #125, Provo, UT 84601"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen.append(request.url.params["q"])
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="40.2133159",
                        lon="-111.6739323",
                        display="870 West, Lakewood, Provo, Utah County, Utah, United States",
                        addresstype="road",
                        category="highway",
                    )
                ],
            )
        if str(request.url).startswith(CENSUS_COORDINATES_URL):
            return _coordinate_tract("49049001200")
        seen.append(request.url.params["address"])
        return _empty_census()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = resolve_address(query, client=client)
        assert isinstance(first, dict)
        assert first["status"] == "confirmation_required"
        assert first["query"] == query
        assert seen == [
            "1689 S 870 W, Provo, UT 84601",
            "1689 S 870 W, Provo, UT 84601",
        ]
        resolved = resolve_address(
            query, candidate_id=first["candidates"][0]["candidate_id"], client=client
        )
    assert isinstance(resolved, AddressMatch)
    assert resolved.query == query
    assert resolved.tract_id == "49049001200"


def test_apartment_word_is_stripped_before_nominatim() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen.append(request.url.params["q"])
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="40.27",
                        lon="-111.69",
                        display="1780 South, Orem, Utah, United States",
                        addresstype="house",
                        house_number="164",
                    )
                ],
            )
        if str(request.url).startswith(CENSUS_COORDINATES_URL):
            return _coordinate_tract("49049000700")
        return _empty_census()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        match = resolve_address("164 E 1780 S Apt 10, Orem, UT 84058", client=client)
    assert isinstance(match, AddressMatch)
    assert match.query == "164 E 1780 S Apt 10, Orem, UT 84058"
    assert match.precision == "house"
    assert seen == ["164 E 1780 S, Orem, UT 84058"]


def test_unit_strip_does_not_eat_street_names() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            seen.append(request.url.params["q"])
            return httpx.Response(200, json=[])
        seen.append(request.url.params["address"])
        return _empty_census()

    queries = [
        "1000 Space Park, Houston, TX 77058",
        "1 Unit Circle, Dallas, TX 75201",
        "12 Suite B Lane, Austin, TX 78701",
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for query in queries:
            with pytest.raises(HouseHunterError, match="Address not found"):
                resolve_address(query, client=client)
    assert seen == [
        "1000 Space Park, Houston, TX 77058",
        "1000 Space Park, Houston, TX 77058",
        "1 Unit Circle, Dallas, TX 75201",
        "1 Unit Circle, Dallas, TX 75201",
        "12 Suite B Lane, Austin, TX 78701",
        "12 Suite B Lane, Austin, TX 78701",
    ]


def test_census_http_failure_does_not_call_nominatim() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        if "nominatim" in (request.url.host or ""):
            raise AssertionError("Nominatim must not run during a Census outage")
        return httpx.Response(500, text="nope")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="Census geocoder request failed"),
    ):
        resolve_address("1 Main St, Boulder, CO", client=client)
    assert hosts == ["geocoding.geo.census.gov"]


def test_malformed_census_payload_does_not_call_nominatim() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        return httpx.Response(200, json={"result": {}})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="malformed"),
    ):
        resolve_address("1 Main St, Boulder, CO", client=client)
    assert "nominatim" not in "".join(hosts)


def test_conflicting_nominatim_candidates_stay_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="39.0",
                        lon="-104.8",
                        display="Lazy Cat Lane, Monument, CO",
                        addresstype="road",
                        category="highway",
                    ),
                    _nominatim_row(
                        lat="39.2",
                        lon="-104.9",
                        display="Lazy Cat Lane, Other, CO",
                        addresstype="road",
                        category="highway",
                    ),
                ],
            )
        return _empty_census()

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AmbiguousPlaceError),
    ):
        resolve_address("1720 Lazy Cat Ln, Monument, CO 80132", client=client)


def test_nominatim_house_auto_resolve_requires_house_type_and_matching_number() -> None:
    query = "1 Main St, Autauga, AL"

    def shop(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="32.5",
                        lon="-86.5",
                        display="A Shop, 1 Main Street, Autauga, Alabama",
                        addresstype="shop",
                        house_number="1",
                        category="shop",
                    )
                ],
            )
        return _empty_census()

    with (
        httpx.Client(transport=httpx.MockTransport(shop)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        resolve_address(query, client=client)

    def wrong_number(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="32.5",
                        lon="-86.5",
                        display="9 Main Street, Autauga, Alabama",
                        addresstype="house",
                        house_number="9",
                    )
                ],
            )
        return _empty_census()

    reset_geocode_runtime()
    with (
        httpx.Client(transport=httpx.MockTransport(wrong_number)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        resolve_address(query, client=client)


def test_nominatim_rejects_locality_non_us_malformed_and_oversized() -> None:
    query = "1 Main St, Autauga, AL"

    def locality(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="32.5",
                        lon="-86.5",
                        display="Autauga, Alabama",
                        addresstype="city",
                    )
                ],
            )
        return _empty_census()

    with (
        httpx.Client(transport=httpx.MockTransport(locality)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        resolve_address(query, client=client)

    def foreign(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="51.5",
                        lon="-0.1",
                        display="1 Main Street, London",
                        addresstype="house",
                        house_number="1",
                        country="gb",
                    )
                ],
            )
        return _empty_census()

    reset_geocode_runtime()
    with (
        httpx.Client(transport=httpx.MockTransport(foreign)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        resolve_address(query, client=client)

    def missing_country(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="32.5",
                        lon="-86.5",
                        display="1 Main Street",
                        addresstype="house",
                        house_number="1",
                        country=None,
                    )
                ],
            )
        return _empty_census()

    reset_geocode_runtime()
    with (
        httpx.Client(transport=httpx.MockTransport(missing_country)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        resolve_address(query, client=client)

    def bad_point(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="nan",
                        lon="-86.5",
                        display="1 Main Street",
                        addresstype="house",
                        house_number="1",
                    )
                ],
            )
        return _empty_census()

    reset_geocode_runtime()
    with (
        httpx.Client(transport=httpx.MockTransport(bad_point)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        resolve_address(query, client=client)

    def oversized(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, content=b"x" * (MAX_NOMINATIM_BYTES + 1))
        return _empty_census()

    reset_geocode_runtime()
    with (
        httpx.Client(transport=httpx.MockTransport(oversized)) as client,
        pytest.raises(HouseHunterError, match="malformed"),
    ):
        resolve_address(query, client=client)


def test_nominatim_cache_and_rate_limit_use_injected_clock() -> None:
    nominatim_calls = 0
    now = [0.0]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal nominatim_calls
        if request.url.path.endswith("/search"):
            nominatim_calls += 1
            return httpx.Response(200, json=[])
        return _empty_census()

    def sleeper(wait: float) -> None:
        sleeps.append(wait)
        now[0] += wait

    reset_geocode_runtime(clock=lambda: now[0], sleeper=sleeper)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HouseHunterError, match="Address not found"):
            resolve_address("1 Main St, Autauga, AL", client=client)
        with pytest.raises(HouseHunterError, match="Address not found"):
            resolve_address("1 Main St, Autauga, AL", client=client)
        assert nominatim_calls == 1
        now[0] = 0.2
        with pytest.raises(HouseHunterError, match="Address not found"):
            resolve_address("2 Main St, Autauga, AL", client=client)
    assert nominatim_calls == 2
    assert sleeps == [pytest.approx(0.8)]


def test_nominatim_can_be_disabled_without_a_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOUSEHUNTER_NOMINATIM_URL", "off")

    def handler(request: httpx.Request) -> httpx.Response:
        if "nominatim" in (request.url.host or ""):
            raise AssertionError("disabled Nominatim must not be called")
        return _empty_census()

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="Address not found"),
    ):
        resolve_address("1 Main St, Autauga, AL", client=client)


def test_nominatim_url_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOUSEHUNTER_NOMINATIM_URL", "http://nominatim.example")

    def handler(_: httpx.Request) -> httpx.Response:
        return _empty_census()

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="must be https"),
    ):
        resolve_address("1 Main St, Autauga, AL", client=client)


def test_confirmation_rejects_a_tampered_candidate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json=[
                    _nominatim_row(
                        lat="39.0695903",
                        lon="-104.7976434",
                        display="Lazy Cat Lane, Monument, Colorado, United States",
                        addresstype="road",
                        category="highway",
                    )
                ],
            )
        return _empty_census()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = resolve_address("1720 Lazy Cat Ln, Monument, CO 80132", client=client)
        assert isinstance(first, dict)
        with pytest.raises(HouseHunterError, match="no longer available"):
            resolve_address(
                "1720 Lazy Cat Ln, Monument, CO 80132",
                candidate_id="not-a-real-candidate",
                client=client,
            )


def test_owned_lookup_client_disables_redirects_and_env_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"addressMatches": [_match("08013012101")]}})

    class CapturingClient(httpx.Client):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("househunter.geocode.httpx.Client", CapturingClient)
    match = resolve_address("1 Main St, Boulder, CO")
    assert isinstance(match, AddressMatch)
    assert match.tract_id == "08013012101"
    assert captured.get("follow_redirects") is False
    assert captured.get("trust_env") is False
    assert captured.get("headers", {}).get("User-Agent") == USER_AGENT
