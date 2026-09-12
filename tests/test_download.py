from __future__ import annotations

import json
from pathlib import Path

import httpx
import polars as pl
import pytest
import yaml

from househunter.config import RuntimePaths
from househunter.download import (
    _fetch_layer_rows,
    _request_json,
    _schema_fingerprint,
    _validate_county_rows,
    _validate_layer,
    _validate_rows,
    download_fema,
    download_fema_counties,
    page_cache_dir,
    validate_cached_fema,
)
from househunter.errors import SourceContractError


def test_geometry_contract_rejects_non_polygon_layer() -> None:
    fields = {"TRACTFIPS": "esriFieldTypeString"}
    source = {
        "item_id": "fixture",
        "layer_url": "https://example.test/layer/0",
        "item_modified_ms": 10,
        "data_last_edit_ms": 20,
        "layer_last_edit_ms": 30,
        "fields": fields,
        "schema_fingerprint": _schema_fingerprint(fields),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        return httpx.Response(
            200,
            json={
                "geometryType": "esriGeometryPoint",
                "editingInfo": {"lastEditDate": 30, "dataLastEditDate": 20},
                "fields": [{"name": "TRACTFIPS", "type": "esriFieldTypeString"}],
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceContractError, match="geometry type changed"),
    ):
        _validate_layer(client, source, expected_geometry_type="esriGeometryPolygon")


def test_attribute_download_rechecks_revision_after_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fields = {
        "TRACTFIPS": "esriFieldTypeString",
        "ALR_NPCTL": "esriFieldTypeDouble",
        "NRI_VER": "esriFieldTypeString",
    }
    source = {
        "item_id": "fixture",
        "layer_url": "https://example.test/layer/0",
        "item_modified_ms": 10,
        "data_last_edit_ms": 20,
        "layer_last_edit_ms": 30,
        "expected_row_count": 1,
        "fields": fields,
        "schema_fingerprint": _schema_fingerprint(fields),
        "item_url": "https://example.test/item",
        "terms_url": "https://example.test/terms",
        "version": "December 2025",
        "release": "v1.20",
        "canonical_sha256": None,
    }
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "fema": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    metadata_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_calls
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if request.url.path.endswith("query"):
            return httpx.Response(200, json={"features": [{"attributes": {
                "TRACTFIPS": "01001000100", "ALR_NPCTL": 1.0,
                "NRI_VER": "December 2025",
            }}]})
        metadata_calls += 1
        return httpx.Response(200, json={
            "maxRecordCount": 1,
            "editingInfo": {
                "lastEditDate": 30 + (metadata_calls - 1),
                "dataLastEditDate": 20,
            },
            "fields": [{"name": name, "type": kind} for name, kind in fields.items()],
        })

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceContractError, match="source changed"),
    ):
        download_fema(RuntimePaths.from_root(tmp_path), client=client)
    assert metadata_calls == 2
    assert not (tmp_path / "cache" / "fema_nri_tracts.parquet").exists()
    assert not page_cache_dir(RuntimePaths.from_root(tmp_path), "fema-pages", source).exists()


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
    assert len(calls) == first_calls + 4
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


@pytest.mark.parametrize("validator,id_field,id_value", [
    (_validate_rows, "TRACTFIPS", "01001000100"),
    (_validate_county_rows, "STCOFIPS", "01001"),
])
@pytest.mark.parametrize("field", ["ALR_NPCTL", "WFIR_ALR_NPCTL"])
@pytest.mark.parametrize("value", ["5.0", True, False])
def test_fema_contract_rejects_coercible_percentile_types(
    validator, id_field: str, id_value: str, field: str, value: object
) -> None:  # type: ignore[no-untyped-def]
    row = {
        id_field: id_value,
        "COUNTY": "Autauga",
        "COUNTYTYPE": "County",
        "STATEABBRV": "AL",
        "ALR_NPCTL": 5.0,
        "NRI_VER": "December 2025",
        field: value,
    }
    with pytest.raises(SourceContractError, match=f"FEMA {field} must be"):
        validator([row], {"expected_row_count": 1, "version": "December 2025"})


@pytest.mark.parametrize(
    "cached_page",
    [
        "not-json",
        json.dumps({"features": [{"attributes": {
            "TRACTFIPS": "01001000100",
            "ALR_NPCTL": 5.0,
            "NRI_VER": "December 2025",
        }}]}),
        json.dumps({"features": [{
            "TRACTFIPS": "01001000100",
            "ALR_NPCTL": "5.0",
            "NRI_VER": "December 2025",
        }]}),
    ],
)
def test_corrupt_page_cache_is_refetched_once(tmp_path: Path, cached_page: str) -> None:
    fields = {
        "TRACTFIPS": "esriFieldTypeString",
        "ALR_NPCTL": "esriFieldTypeDouble",
        "NRI_VER": "esriFieldTypeString",
    }
    source = {
        "item_id": "fixture",
        "layer_url": "https://example.test/layer/0",
        "item_modified_ms": 10,
        "data_last_edit_ms": 20,
        "layer_last_edit_ms": 30,
        "expected_row_count": 1,
        "version": "December 2025",
        "fields": fields,
        "schema_fingerprint": _schema_fingerprint(fields),
    }
    (tmp_path / "000000.json").write_text(cached_page)
    query_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if request.url.path.endswith("query"):
            query_calls += 1
            return httpx.Response(200, json={"features": [{"attributes": {
                "TRACTFIPS": "01001000100",
                "ALR_NPCTL": 5.0,
                "NRI_VER": "December 2025",
            }}]})
        return httpx.Response(200, json={
            "maxRecordCount": 1,
            "geometryType": "esriGeometryPolygon",
            "editingInfo": {"lastEditDate": 30, "dataLastEditDate": 20},
            "fields": [
                {"name": name, "type": field_type} for name, field_type in fields.items()
            ],
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = _fetch_layer_rows(
            client, source, out_fields=",".join(fields), order_by="TRACTFIPS",
            pages=tmp_path, progress=None, cancelled=None, noun="tracts",
        )
    assert rows == [{
        "TRACTFIPS": "01001000100",
        "ALR_NPCTL": 5.0,
        "NRI_VER": "December 2025",
    }]
    assert query_calls == 1
    assert json.loads((tmp_path / "000000.json").read_text())["features"] == rows


def test_symlinked_page_cache_is_ignored_and_replaced(tmp_path: Path) -> None:
    fields = {"TRACTFIPS": "esriFieldTypeString"}
    source = {
        "item_id": "fixture",
        "layer_url": "https://example.test/layer/0",
        "item_modified_ms": 10,
        "data_last_edit_ms": 20,
        "layer_last_edit_ms": 30,
        "expected_row_count": 1,
        "fields": fields,
        "schema_fingerprint": _schema_fingerprint(fields),
    }
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"features": [{"TRACTFIPS": "stale"}]}))
    pages = tmp_path / "pages"
    pages.mkdir()
    page = pages / "000000.json"
    page.symlink_to(outside)
    query_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if request.url.path.endswith("query"):
            query_calls += 1
            return httpx.Response(
                200,
                json={"features": [{"attributes": {"TRACTFIPS": "01001000100"}}]},
            )
        return httpx.Response(
            200,
            json={
                "maxRecordCount": 1,
                "geometryType": "esriGeometryPolygon",
                "editingInfo": {"lastEditDate": 30, "dataLastEditDate": 20},
                "fields": [{"name": "TRACTFIPS", "type": "esriFieldTypeString"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = _fetch_layer_rows(
            client,
            source,
            out_fields="TRACTFIPS",
            order_by="TRACTFIPS",
            pages=pages,
            progress=None,
            cancelled=None,
            noun="tracts",
        )

    assert rows == [{"TRACTFIPS": "01001000100"}]
    assert query_calls == 1
    assert page.is_file() and not page.is_symlink()
    assert json.loads(page.read_text())["features"] == rows
    assert json.loads(outside.read_text())["features"] == [{"TRACTFIPS": "stale"}]


def test_symlinked_page_cache_directory_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    pages = tmp_path / "pages"
    pages.symlink_to(outside, target_is_directory=True)
    with (
        httpx.Client() as client,
        pytest.raises(SourceContractError, match="cannot contain a symlink"),
    ):
        _fetch_layer_rows(
            client, {}, out_fields="x", order_by="x", pages=pages,
            progress=None, cancelled=None, noun="rows",
        )
    assert list(outside.iterdir()) == []


def test_page_cache_write_does_not_follow_predictable_temporary_symlink(
    tmp_path: Path,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("preserve")
    (pages / "000000.json.tmp").symlink_to(outside)
    fields = {"TRACTFIPS": "esriFieldTypeString"}
    source = {
        "item_id": "fixture",
        "layer_url": "https://example.test/layer/0",
        "item_modified_ms": 10,
        "data_last_edit_ms": 20,
        "layer_last_edit_ms": 30,
        "expected_row_count": 1,
        "fields": fields,
        "schema_fingerprint": _schema_fingerprint(fields),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if request.url.path.endswith("query"):
            return httpx.Response(
                200, json={"features": [{"attributes": {"TRACTFIPS": "01001000100"}}]}
            )
        return httpx.Response(
            200,
            json={
                "maxRecordCount": 1,
                "geometryType": "esriGeometryPolygon",
                "editingInfo": {"lastEditDate": 30, "dataLastEditDate": 20},
                "fields": [{"name": "TRACTFIPS", "type": "esriFieldTypeString"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = _fetch_layer_rows(
            client,
            source,
            out_fields="TRACTFIPS",
            order_by="TRACTFIPS",
            pages=pages,
            progress=None,
            cancelled=None,
            noun="tracts",
        )

    assert rows == [{"TRACTFIPS": "01001000100"}]
    assert outside.read_text() == "preserve"


def test_force_download_bypasses_valid_page_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fields = {
        "TRACTFIPS": "esriFieldTypeString",
        "ALR_NPCTL": "esriFieldTypeDouble",
        "NRI_VER": "esriFieldTypeString",
    }
    source = {
        "item_id": "fixture", "layer_url": "https://example.test/layer/0",
        "item_url": "https://example.test/item", "terms_url": "https://example.test/terms",
        "version": "December 2025", "release": "v1.20", "item_modified_ms": 10,
        "data_last_edit_ms": 20, "layer_last_edit_ms": 30, "expected_row_count": 1,
        "fields": fields, "schema_fingerprint": _schema_fingerprint(fields),
        "canonical_sha256": None,
    }
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "fema": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    pages = page_cache_dir(paths, "fema-pages", source)
    pages.mkdir(parents=True)
    stale = {"TRACTFIPS": "01001000100", "ALR_NPCTL": 1.0, "NRI_VER": "December 2025"}
    (pages / "000000.json").write_text(json.dumps({"features": [stale]}))
    query_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if request.url.path.endswith("query"):
            query_calls += 1
            fresh = {**stale, "ALR_NPCTL": 2.0}
            return httpx.Response(200, json={"features": [{"attributes": fresh}]})
        return httpx.Response(200, json={
            "maxRecordCount": 1, "geometryType": "esriGeometryPolygon",
            "editingInfo": {"lastEditDate": 30, "dataLastEditDate": 20},
            "fields": [{"name": name, "type": kind} for name, kind in fields.items()],
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = download_fema(paths, client=client, force=True)
    assert query_calls == 1
    assert pl.read_parquet(output)["alr_npctl"].item() == 2.0


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
    config_path.write_text("schema_version: 1\nfema: {}\n")
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
