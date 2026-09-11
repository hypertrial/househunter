from __future__ import annotations

import httpx
import pytest

from househunter.errors import AmbiguousPlaceError, HouseHunterError
from househunter.geocode import GEOCODER_BENCHMARK, GEOCODER_URL, GEOCODER_VINTAGE, geocode_tract


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
