from __future__ import annotations

import json
from pathlib import Path

import httpx
import polars as pl
import pytest
import yaml

from househunter.config import RuntimePaths
from househunter.download import (
    _request_json,
    _validate_county_rows,
    _validate_rows,
    download_fema,
    download_fema_counties,
    page_cache_dir,
    validate_cached_fema,
)
from househunter.errors import SourceContractError


def test_download_paginates_and_reuses_verified_cache(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = {
        "schema_version": 1,
        "fema": {
            "item_id": "fixture",
            "layer_url": "https://example.test/layer/0",
            "item_url": "https://example.test/item",
            "terms_url": "https://example.test/terms",
            "version": "December 2025",
            "release": "v1.20",
            "item_modified_ms": 10,
            "data_last_edit_ms": 20,
            "layer_last_edit_ms": 30,
            "expected_row_count": 3,
            "fields": {
                "TRACTFIPS": "esriFieldTypeString",
                "ALR_NPCTL": "esriFieldTypeDouble",
                "NRI_VER": "esriFieldTypeString",
            },
            "schema_fingerprint": (
                "fdbbc3313928b19a8334cf0883667b996c64e5fc33b956dc42bbff55adf7723e"
            ),
            "canonical_sha256": None,
        },
        "census": {},
    }
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if not request.url.path.endswith("query"):
            return httpx.Response(
                200,
                json={
                    "maxRecordCount": 2,
                    "editingInfo": {"lastEditDate": 30, "dataLastEditDate": 20},
                    "fields": [
                        {"name": name, "type": kind}
                        for name, kind in config["fema"]["fields"].items()
                    ],
                },
            )
        offset = int(request.url.params["resultOffset"])
        records = [
            {"TRACTFIPS": "01001000100", "ALR_NPCTL": 1.0, "NRI_VER": "December 2025"},
            {"TRACTFIPS": "01001000200", "ALR_NPCTL": 2.0, "NRI_VER": "December 2025"},
            {"TRACTFIPS": "01001000300", "ALR_NPCTL": 3.0, "NRI_VER": "December 2025"},
        ][offset : offset + 2]
        return httpx.Response(200, json={"features": [{"attributes": row} for row in records]})

    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = download_fema(paths, client=client)
        first_calls = len(calls)
        assert output.is_file()
        page_cache = page_cache_dir(paths, "fema-pages", config["fema"])
        assert json.loads((page_cache / "000000.json").read_text())["features"]
        assert not (paths.cache / "fema-pages-10").exists()
        pl.DataFrame({"invalid": [True]}).write_parquet(paths.source_manifest)
        download_fema(paths, client=client)
        assert len(calls) == first_calls
        pl.DataFrame({"tract_id": ["broken"]}).write_parquet(output)
        download_fema(paths, client=client)
    assert len(calls) == first_calls + 2
    assert paths.source_manifest.is_file()
    assert pl.read_parquet(paths.source_manifest)["source"].item() == "fema"


def test_county_download_paginates_and_reuses_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "schema_version": 1,
        "fema": {},
        "fema_counties": {
            "item_id": "fixture",
            "layer_url": "https://example.test/counties/0",
            "item_url": "https://example.test/counties",
            "terms_url": "https://example.test/terms",
            "version": "December 2025",
            "release": "v1.20",
            "item_modified_ms": 10,
            "data_last_edit_ms": 20,
            "layer_last_edit_ms": 30,
            "expected_row_count": 2,
            "fields": {
                "STCOFIPS": "esriFieldTypeString",
                "COUNTY": "esriFieldTypeString",
                "COUNTYTYPE": "esriFieldTypeString",
                "STATEABBRV": "esriFieldTypeString",
                "ALR_NPCTL": "esriFieldTypeDouble",
                "NRI_VER": "esriFieldTypeString",
            },
            "schema_fingerprint": (
                "de3fb9c4dd2b2d7f507f908fce69ab95fc7f20bcfc155e93851a9e2ed85767f2"
            ),
            "canonical_sha256": None,
        },
        "census": {},
    }
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if not request.url.path.endswith("query"):
            return httpx.Response(
                200,
                json={
                    "maxRecordCount": 1,
                    "editingInfo": {"lastEditDate": 30, "dataLastEditDate": 20},
                    "fields": [
                        {"name": name, "type": kind}
                        for name, kind in config["fema_counties"]["fields"].items()
                    ],
                },
            )
        offset = int(request.url.params["resultOffset"])
        records = [
            {
                "STCOFIPS": "01001",
                "COUNTY": "Autauga",
                "COUNTYTYPE": "County",
                "STATEABBRV": "AL",
                "ALR_NPCTL": 40.0,
                "NRI_VER": "December 2025",
            },
            {
                "STCOFIPS": "02001",
                "COUNTY": "Aleutians East",
                "COUNTYTYPE": "Borough",
                "STATEABBRV": "AK",
                "ALR_NPCTL": 12.0,
                "NRI_VER": "December 2025",
            },
        ][offset : offset + 1]
        return httpx.Response(200, json={"features": [{"attributes": row} for row in records]})

    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = download_fema_counties(paths, client=client)
        first_calls = len(calls)
        assert output.is_file()
        download_fema_counties(paths, client=client)
        assert len(calls) == first_calls
    assert pl.read_parquet(output)["county_fips"].to_list() == ["01001", "02001"]
    assert "fema_counties" in pl.read_parquet(paths.source_manifest)["source"].to_list()


def test_county_contract_rejects_duplicates() -> None:
    source = {"expected_row_count": 2, "version": "December 2025"}
    with pytest.raises(SourceContractError, match="not unique"):
        _validate_county_rows(
            [
                {
                    "STCOFIPS": "01001",
                    "COUNTY": "Autauga",
                    "COUNTYTYPE": "County",
                    "STATEABBRV": "AL",
                    "ALR_NPCTL": 1.0,
                    "NRI_VER": "December 2025",
                },
                {
                    "STCOFIPS": "01001",
                    "COUNTY": "Autauga",
                    "COUNTYTYPE": "County",
                    "STATEABBRV": "AL",
                    "ALR_NPCTL": 2.0,
                    "NRI_VER": "December 2025",
                },
            ],
            source,
        )


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {"TRACTFIPS": "01001000100", "ALR_NPCTL": 1.0, "NRI_VER": "December 2025"},
                {"TRACTFIPS": "01001000100", "ALR_NPCTL": 2.0, "NRI_VER": "December 2025"},
            ],
            "not unique",
        ),
        (
            [
                {"TRACTFIPS": "01001000100", "ALR_NPCTL": 101.0, "NRI_VER": "December 2025"},
                {"TRACTFIPS": "01001000200", "ALR_NPCTL": 2.0, "NRI_VER": "December 2025"},
            ],
            "invalid ALR_NPCTL",
        ),
        (
            [
                {
                    "TRACTFIPS": "01001000100",
                    "ALR_NPCTL": float("nan"),
                    "NRI_VER": "December 2025",
                },
                {"TRACTFIPS": "01001000200", "ALR_NPCTL": 2.0, "NRI_VER": "December 2025"},
            ],
            "invalid ALR_NPCTL",
        ),
        (
            [
                {
                    "TRACTFIPS": "01001000100",
                    "ALR_NPCTL": 1.0,
                    "NRI_VER": "December 2025",
                    "WFIR_ALR_NPCTL": 101.0,
                },
                {"TRACTFIPS": "01001000200", "ALR_NPCTL": 2.0, "NRI_VER": "December 2025"},
            ],
            "invalid hazard ALR_NPCTL",
        ),
    ],
)
def test_fema_contract_rejects_duplicates_and_invalid_range(
    rows: list[dict[str, object]], message: str
) -> None:
    source = {"expected_row_count": 2, "version": "December 2025"}
    with pytest.raises(SourceContractError, match=message):
        _validate_rows(rows, source)


def test_fema_contract_allows_null_hazard_percentiles() -> None:
    frame = _validate_rows(
        [
            {
                "TRACTFIPS": "01001000100",
                "ALR_NPCTL": 1.0,
                "NRI_VER": "December 2025",
                "TSUN_ALR_NPCTL": None,
            },
            {
                "TRACTFIPS": "01001000200",
                "ALR_NPCTL": 2.0,
                "NRI_VER": "December 2025",
                "WFIR_ALR_NPCTL": 12.5,
            },
        ],
        {"expected_row_count": 2, "version": "December 2025"},
    )
    assert frame["alr_npctl_tsun"].to_list() == [None, None]
    assert frame["alr_npctl_wfir"].to_list() == [None, 12.5]


def test_cached_fema_rejects_missing_columns(tmp_path: Path) -> None:
    cached = tmp_path / "fema.parquet"
    pl.DataFrame({"tract_id": ["01001000100"]}).write_parquet(cached)
    source = {"expected_row_count": 1, "version": "December 2025"}
    with pytest.raises(SourceContractError, match="missing columns"):
        validate_cached_fema(cached, source)


def test_page_cache_dir_changes_with_schema_fingerprint(tmp_path: Path) -> None:
    paths = RuntimePaths.from_root(tmp_path)
    first = page_cache_dir(
        paths,
        "fema-pages",
        {"item_modified_ms": 10, "schema_fingerprint": "a" * 64},
    )
    second = page_cache_dir(
        paths,
        "fema-pages",
        {"item_modified_ms": 10, "schema_fingerprint": "b" * 64},
    )
    assert first != second
    assert first.name == f"fema-pages-10-{'a' * 16}"
    assert second.name == f"fema-pages-10-{'b' * 16}"
    assert not (paths.cache / "fema-pages-10").exists()


def test_cancelled_download_does_not_make_network_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "sources.yml"
    config_path.write_text("schema_version: 1\nfema: {}\ncensus: {}\n")
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("cancelled download made a request")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(InterruptedError, match="cancelled"),
    ):
        download_fema(RuntimePaths.from_root(tmp_path), client=client, cancelled=lambda: True)


def test_request_json_rejects_a_non_object_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceContractError, match="non-object"),
    ):
        _request_json(client, "https://example.test", {})
