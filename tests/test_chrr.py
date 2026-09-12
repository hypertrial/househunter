from __future__ import annotations

import json
from pathlib import Path

import httpx
import polars as pl
import pytest
import yaml

import househunter.chrr as chrr_module
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


def test_chrr_pointer_failure_preserves_verified_cache(
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
        real_replace = chrr_module.os.replace

        def fail_pointer_replace(source_path: Path, destination: Path) -> None:
            if Path(destination).name == "current.json":
                raise OSError("simulated pointer publication failure")
            real_replace(source_path, destination)

        monkeypatch.setattr("househunter.chrr.os.replace", fail_pointer_replace)
        with pytest.raises(OSError, match="simulated pointer publication failure"):
            download_chrr(paths, client=client, force=True)

    current_raw, current_metadata = raw_paths(paths)
    assert current_raw.read_bytes() == previous_raw
    assert current_metadata.read_bytes() == previous_metadata


def test_chrr_managed_cache_metadata_is_not_repaired_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(transport=_stable_handler(source)) as client:
        download_chrr(paths, client=client)
        first_raw, first_metadata = raw_paths(paths)
        first_metadata.write_text("{}\n")
        refreshed_raw = download_chrr(paths, client=client)

    assert refreshed_raw != first_raw
    assert raw_paths(paths)[1] != first_metadata
    validate_cached_chrr(refreshed_raw, source)


def test_chrr_cancellation_after_pointer_commit_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    cancelled = False
    real_publish = chrr_module._publish_cache_generation

    def publish_then_cancel(*args: object, **kwargs: object) -> Path:
        nonlocal cancelled
        published = real_publish(*args, **kwargs)  # type: ignore[arg-type]
        cancelled = True
        return published

    def progress(_value: int, _message: str) -> None:
        if cancelled:
            raise InterruptedError("Job cancelled")

    monkeypatch.setattr(chrr_module, "_publish_cache_generation", publish_then_cancel)
    with httpx.Client(transport=_stable_handler(source)) as client:
        published = download_chrr(
            paths,
            client=client,
            progress=progress,
            cancelled=lambda: cancelled,
            force=True,
        )

    assert published == raw_paths(paths)[0]
    validate_cached_chrr(published, source)


def test_chrr_rejects_symlinked_generation_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    paths.ensure()
    cache = paths.raw / "chrr"
    cache.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (cache / "generations").symlink_to(outside, target_is_directory=True)

    with (
        httpx.Client(transport=_stable_handler(source)) as client,
        pytest.raises(SourceContractError, match="real directory"),
    ):
        download_chrr(paths, client=client, force=True)

    assert list(outside.iterdir()) == []


def test_chrr_retains_only_current_and_rollback_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)

    with httpx.Client(transport=_stable_handler(source)) as client:
        for _ in range(4):
            download_chrr(paths, client=client, force=True)

    generations = paths.raw / "chrr" / "generations"
    retained = [path for path in generations.iterdir() if len(path.name) == 64]
    assert len(retained) == 2
    assert raw_paths(paths)[0].parent in retained


@pytest.mark.parametrize("broken_symlink", [False, True])
def test_chrr_rejects_missing_pointer_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_symlink: bool
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    paths.ensure()
    cache = paths.raw / "chrr"
    generations = cache / "generations"
    generations.mkdir(parents=True)
    generation = "a" * 64
    if broken_symlink:
        (generations / generation).symlink_to(tmp_path / "missing", target_is_directory=True)
    (cache / "current.json").write_text(json.dumps({"generation": generation}) + "\n")

    with pytest.raises(SourceContractError, match="missing|real directory"):
        raw_paths(paths)

    assert not (generations / generation).is_dir()


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


@pytest.mark.parametrize(
    "field,value", [("fipscode", 10001), ("state", 1), ("county", None)]
)
def test_chrr_contract_rejects_coercible_required_field_types(
    field: str, value: object
) -> None:
    rows = [{**_rows()[0], field: value}]

    with pytest.raises(SourceContractError, match="must be strings"):
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


def test_managed_chrr_generation_identity_is_verified_and_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source()
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "chrr": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    paths = RuntimePaths.from_root(tmp_path)
    query_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_calls
        if request.url.path.endswith("query"):
            query_calls += 1
        return _stable_handler(source).handle_request(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        original = download_chrr(paths, client=client)
        first_query_calls = query_calls
        metadata_path = raw_paths(paths)[1]
        metadata = json.loads(metadata_path.read_text())
        metadata["downloaded_at"] = "2000-01-01T00:00:00+00:00"
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        refreshed = download_chrr(paths, client=client)

    assert query_calls > first_query_calls
    assert refreshed != original
    validate_cached_chrr(refreshed, source)


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
