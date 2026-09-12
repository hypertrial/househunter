from __future__ import annotations

import json
from pathlib import Path

import httpx
import polars as pl
import pytest
import yaml

from househunter.chrr import (
    build_processed,
    download_chrr,
    logical_checksum,
    raw_paths,
    validate_cached_chrr,
    validate_rows,
)
from househunter.config import RuntimePaths
from househunter.errors import SourceContractError


def _source(expected: int = 3) -> dict[str, object]:
    return {
        "name": "fixture",
        "layer_url": "https://example.test/FeatureServer/2",
        "item_url": "https://example.test/item",
        "terms_url": "https://example.test/terms",
        "version": "2025 Annual Data Release",
        "release": "2025",
        "release_year": 2025,
        "layer_last_edit_ms": 30,
        "schema_last_edit_ms": 20,
        "data_last_edit_ms": 10,
        "expected_row_count": expected,
        "fields": {
            "fipscode": "esriFieldTypeString",
            "county": "esriFieldTypeString",
            "state": "esriFieldTypeString",
            "CommunityConditions_Group": "esriFieldTypeInteger",
        },
        "schema_fingerprint": (
            "503000d3f66abcea0a8182c522e7365f7175cc4c39ad7a798c3765d885fdcdf3"
        ),
        "canonical_sha256": None,
    }


def _rows() -> list[dict[str, object]]:
    return [
        {
            "fipscode": "01001",
            "county": "Autauga County",
            "state": "AL",
            "CommunityConditions_Group": 5,
        },
        {
            "fipscode": "02016",
            "county": "Aleutians West Census Area",
            "state": "AK",
            "CommunityConditions_Group": 4,
        },
        {
            "fipscode": "02063",
            "county": "Chugach Census Area",
            "state": "AK",
            "CommunityConditions_Group": None,
        },
    ]


def _stable_handler(source: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("query"):
            offset = int(request.url.params["resultOffset"])
            rows = _rows()[: int(source["expected_row_count"])]
            page = rows[offset : offset + 2]
            return httpx.Response(
                200, json={"features": [{"attributes": row} for row in page]}
            )
        return httpx.Response(
            200,
            json={
                "maxRecordCount": 2,
                "editingInfo": {
                    "lastEditDate": 30,
                    "schemaLastEditDate": 20,
                    "dataLastEditDate": 10,
                },
                "fields": [
                    {"name": name, "type": kind}
                    for name, kind in source["fields"].items()  # type: ignore[union-attr]
                ],
            },
        )

    return httpx.MockTransport(handler)


def test_chrr_download_paginates_validates_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("query"):
            offset = int(request.url.params["resultOffset"])
            page = _rows()[offset : offset + 2]
            return httpx.Response(200, json={"features": [{"attributes": row} for row in page]})
        return httpx.Response(
            200,
            json={
                "maxRecordCount": 2,
                "editingInfo": {
                    "lastEditDate": 30,
                    "schemaLastEditDate": 20,
                    "dataLastEditDate": 10,
                },
                "fields": [
                    {"name": name, "type": kind}
                    for name, kind in source["fields"].items()  # type: ignore[union-attr]
                ],
            },
        )

    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = download_chrr(paths, client=client)
        first_calls = len(calls)
        assert first_calls == 4
        download_chrr(paths, client=client)
    assert len(calls) == first_calls
    frame, digest = validate_cached_chrr(raw, source)
    assert frame["community_conditions_group"].to_list() == [5, 4, None]
    metadata = json.loads(raw_paths(paths)[1].read_text())
    assert metadata["logical_sha256"] == digest
    assert metadata["geography"] == "county"
    assert metadata["metric"] == "Community Conditions Health Group"
    assert metadata["source_field"] == "CommunityConditions_Group"


def test_chrr_rechecks_revision_before_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(1)
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    metadata_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_calls
        if request.url.path.endswith("query"):
            return httpx.Response(200, json={"features": [{"attributes": _rows()[0]}]})
        metadata_calls += 1
        return httpx.Response(
            200,
            json={
                "maxRecordCount": 2,
                "editingInfo": {
                    "lastEditDate": 30 + metadata_calls - 1,
                    "schemaLastEditDate": 20,
                    "dataLastEditDate": 10,
                },
                "fields": [
                    {"name": name, "type": kind}
                    for name, kind in source["fields"].items()  # type: ignore[union-attr]
                ],
            },
        )

    paths = RuntimePaths.from_root(tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceContractError, match="source changed"),
    ):
        download_chrr(paths, client=client)
    assert not raw_paths(paths)[0].exists()


def test_chrr_rejects_checksum_drift_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(1)
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(transport=_stable_handler(source)) as client:
        download_chrr(paths, client=client)
        raw, metadata = raw_paths(paths)
        previous_raw = raw.read_bytes()
        previous_metadata = metadata.read_bytes()
        source["canonical_sha256"] = "0" * 64
        config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
        with pytest.raises(SourceContractError, match="checksum mismatch"):
            download_chrr(paths, client=client, force=True)
    assert raw.read_bytes() == previous_raw
    assert metadata.read_bytes() == previous_metadata


def test_chrr_cancellation_preserves_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(transport=_stable_handler(source)) as client:
        download_chrr(paths, client=client)
        raw, metadata = raw_paths(paths)
        previous_raw = raw.read_bytes()
        previous_metadata = metadata.read_bytes()
        cancellation_checks = 0

        def cancelled() -> bool:
            nonlocal cancellation_checks
            cancellation_checks += 1
            return cancellation_checks >= 3

        with pytest.raises(InterruptedError, match="cancelled"):
            download_chrr(paths, client=client, force=True, cancelled=cancelled)
    assert raw.read_bytes() == previous_raw
    assert metadata.read_bytes() == previous_metadata


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_rows()[0], _rows()[0]], "not unique"),
        ([{**_rows()[0], "fipscode": "100"}], "invalid county FIPS"),
        ([{**_rows()[0], "CommunityConditions_Group": 11}], "outside 1..10"),
    ],
)
def test_chrr_contract_rejects_invalid_rows(
    rows: list[dict[str, object]], message: str
) -> None:
    source = _source(len(rows))
    with pytest.raises(SourceContractError, match=message):
        validate_rows(rows, source)


@pytest.mark.parametrize("value", [5.5, True, "5"])
def test_chrr_contract_rejects_non_integer_groups(value: object) -> None:
    rows = [{**_rows()[0], "CommunityConditions_Group": value}]

    with pytest.raises(SourceContractError, match="must be integers or null"):
        validate_rows(rows, _source(1))


def test_cached_chrr_rejects_coercible_group_before_checksum(tmp_path: Path) -> None:
    valid = [{**_rows()[0], "CommunityConditions_Group": 5}]
    source = _source(1)
    source["canonical_sha256"] = logical_checksum(validate_rows(valid, source))
    cache = tmp_path / "chrr.json"
    cache.write_text(
        json.dumps({"rows": [{**valid[0], "CommunityConditions_Group": 5.5}]})
    )

    with pytest.raises(SourceContractError, match="must be integers or null"):
        validate_cached_chrr(cache, source)


def test_processed_chrr_has_stable_six_column_contract(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, _ = fixture_environment
    frame, digest = build_processed(paths)
    assert frame.columns == [
        "county_fips",
        "state",
        "county",
        "release_year",
        "community_conditions_group",
        "source_version",
    ]
    assert "qol_sort_score" not in frame.columns
    assert (paths.processed / "chrr_county.parquet").is_file()
    raw_frame, _ = validate_cached_chrr(raw_paths(paths)[0], yaml.safe_load(
        (fixture_environment[1] / "sources.yml").read_text()
    )["chrr"])
    assert digest == logical_checksum(raw_frame)
    assert pl.read_parquet(paths.processed / "chrr_county.parquet").equals(frame)
