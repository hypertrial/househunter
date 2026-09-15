from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import polars as pl
import pytest
import yaml

import househunter.cost_of_living as cost_of_living_module
from househunter.acquisition import download_optional_bea
from househunter.config import RuntimePaths
from househunter.cost_of_living import (
    BEA_CACHE_NAME,
    assign_counties,
    download_bea_rpp,
    inherit_county_costs,
    logical_checksum,
    parse_archive,
    source_status,
    validate_cached,
)
from househunter.errors import SourceContractError


def _archive(
    *,
    missing_line: bool = False,
    duplicate_line: bool = False,
    invalid_value: bool = False,
    value_offset: int = 0,
) -> tuple[bytes, dict[str, object]]:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    header = [
        "GeoFIPS",
        "GeoName",
        "Region",
        "TableName",
        "LineCode",
        "IndustryClassification",
        "Description",
        "Unit",
        "2024",
    ]
    writer.writerow(header)
    geographies = [
        ("00000", "United States"),
        ("00999", "United States (Nonmetropolitan Portion) *"),
        ("10180", "Abilene, TX (Metropolitan Statistical Area)"),
        ("10420", "Akron, OH (Metropolitan Statistical Area)"),
    ]
    for geography_index, (geofips, name) in enumerate(geographies):
        for line_code in range(1, 6):
            if missing_line and geofips == "10180" and line_code == 5:
                continue
            value: object = 80 + geography_index * 5 + line_code + value_offset
            if invalid_value and geofips == "10180" and line_code == 1:
                value = "(NA)"
            writer.writerow(
                [
                    f' "{geofips}"',
                    name,
                    " ",
                    "MARPP",
                    line_code,
                    "...",
                    f"Line {line_code}",
                    "Index",
                    value,
                ]
            )
    if duplicate_line:
        writer.writerow(
            [' "10180"', "Abilene", " ", "MARPP", 1, "...", "Line 1", "Index", 90]
        )
    writer.writerow(["", "Note", "", "", "", "", "", "", ""])
    table = output.getvalue().encode()
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("MARPP_MSA_2008_2024.csv", (2026, 2, 10, 17, 4, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, table)
    content = archive_buffer.getvalue()
    source: dict[str, object] = {
        "name": "fixture BEA RPP",
        "archive_url": "https://example.test/MARPP.zip",
        "item_url": "https://example.test/rpp",
        "terms_url": "https://example.test/open-data",
        "version": "2024 Regional Price Parities",
        "release": 2024,
        "release_year": 2024,
        "expected_size": len(content),
        "archive_sha256": hashlib.sha256(content).hexdigest(),
        "table_filename": "MARPP_MSA_2008_2024.csv",
        "table_size": len(table),
        "header_sha256": hashlib.sha256(table.splitlines(keepends=True)[0]).hexdigest(),
        "required_columns": header,
        "expected_msa_count": 2,
        "nonmetropolitan_geofips": "00999",
        "line_codes": {str(code): f"Line {code}" for code in range(1, 6)},
    }
    if not (missing_line or duplicate_line or invalid_value):
        source["normalized_logical_sha256"] = logical_checksum(parse_archive(content, source))
    else:
        source["normalized_logical_sha256"] = "0" * 64
    return content, source


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: dict[str, object]) -> None:
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "bea_rpp": source}))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))


def test_bea_parses_exact_lines_and_excludes_us_total() -> None:
    content, source = _archive()
    frame = parse_archive(content, source)

    assert frame["cost_of_living_geography_id"].to_list() == ["00999", "10180", "10420"]
    assert frame["cost_of_living_index"].to_list() == [86.0, 91.0, 96.0]
    assert frame["cost_of_living_other_services_index"].to_list() == [90.0, 95.0, 100.0]
    assert frame["cost_of_living_release_year"].to_list() == [2024, 2024, 2024]
    assert frame.row(0, named=True)["cost_of_living_geography_name"] == (
        "United States (Nonmetropolitan Portion)"
    )


def test_bea_assigns_msas_nonmetro_micros_and_territory_then_inherits_to_tracts() -> None:
    content, source = _archive()
    rpp = parse_archive(content, source)
    counties = pl.DataFrame(
        {"county_fips": ["01001", "01003", "02001", "72001", "99999"]}
    )
    crosswalk = pl.DataFrame(
        {
            "county_fips": ["01001", "01003", "72001"],
            "cbsa_id": ["10180", "99999", "10180"],
        }
    )
    assigned = assign_counties(counties, crosswalk, rpp, source)
    assert assigned["cost_of_living_geography_id"].to_list() == [
        "10180",
        "00999",
        "00999",
        None,
        None,
    ]
    assert assigned["cost_of_living_geography_type"].to_list() == [
        "metropolitan",
        "nonmetropolitan",
        "nonmetropolitan",
        None,
        None,
    ]
    assert assigned["cost_of_living_coverage_status"].to_list() == [
        "complete",
        "complete",
        "complete",
        "outside_scope",
        "unmatched_geography",
    ]
    tracts = pl.DataFrame(
        {
            "tract_id": [
                "01001000100",
                "01003000100",
                "02001000100",
                "72001000100",
                "99999000100",
            ]
        }
    )
    inherited = inherit_county_costs(tracts, assigned)
    assert inherited["cost_of_living_index"].to_list() == [91.0, 86.0, 86.0, None, None]


def test_bea_assignment_rejects_null_county_fips() -> None:
    content, source = _archive()
    rpp = parse_archive(content, source)
    counties = pl.DataFrame({"county_fips": ["01001", None]}, schema={"county_fips": pl.String})
    crosswalk = pl.DataFrame(
        {"county_fips": ["01001"], "cbsa_id": ["10180"]}
    )

    with pytest.raises(SourceContractError, match="invalid or duplicate FIPS"):
        assign_counties(counties, crosswalk, rpp, source)


def test_bea_tract_inheritance_marks_missing_parent_as_unmatched() -> None:
    content, source = _archive()
    rpp = parse_archive(content, source)
    assigned = assign_counties(
        pl.DataFrame({"county_fips": ["01001"]}),
        pl.DataFrame({"county_fips": ["01001"], "cbsa_id": ["10180"]}),
        rpp,
        source,
    )
    inherited = inherit_county_costs(
        pl.DataFrame({"tract_id": ["01001000100", "02001000100"]}), assigned
    )

    missing = inherited.filter(pl.col("tract_id") == "02001000100").row(0, named=True)
    assert missing["cost_of_living_index"] is None
    assert missing["cost_of_living_coverage_status"] == "unmatched_geography"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"missing_line": True}, "all target line codes"),
        ({"duplicate_line": True}, "duplicate geography"),
        ({"invalid_value": True}, "invalid target values"),
    ],
)
def test_bea_rejects_structural_drift(kwargs: dict[str, bool], message: str) -> None:
    content, source = _archive(**kwargs)
    with pytest.raises(SourceContractError, match=message):
        parse_archive(content, source)


def test_bea_download_verifies_reuses_and_reports_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, source = _archive()
    _configure(tmp_path, monkeypatch, source)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Length": str(len(content)), "Content-Encoding": "identity"},
        )

    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = download_bea_rpp(paths, client=client)
        first_calls = calls
        assert output == paths.cache / BEA_CACHE_NAME
        assert download_bea_rpp(paths, client=client) == output
    assert calls == first_calls == 1
    status = source_status(paths)
    assert status.cached is True
    assert status.row_count == 3
    assert status.error is None


def test_bea_failed_refresh_preserves_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, source = _archive()
    _configure(tmp_path, monkeypatch, source)
    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
    ) as client:
        output = download_bea_rpp(paths, client=client)
    previous = output.read_bytes()
    corrupt = content[:-1] + bytes([content[-1] ^ 1])
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=corrupt))
        ) as client,
        pytest.raises(SourceContractError, match="checksum mismatch"),
    ):
        download_bea_rpp(paths, client=client, force=True)
    assert output.read_bytes() == previous
    assert source_status(paths).error is None


def test_bea_manifest_write_failure_restores_previous_verified_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_content, first_source = _archive()
    _configure(tmp_path, monkeypatch, first_source)
    paths = RuntimePaths.from_root(tmp_path)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=first_content))
    ) as client:
        output = download_bea_rpp(paths, client=client)
    previous_cache = output.read_bytes()
    previous_manifest = paths.source_manifest.read_bytes()

    second_content, second_source = _archive(value_offset=10)
    _configure(tmp_path, monkeypatch, second_source)

    def fail_manifest(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cost_of_living_module, "_write_source_manifest", fail_manifest)
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=second_content)
            )
        ) as client,
        pytest.raises(OSError, match="disk full"),
    ):
        download_bea_rpp(paths, client=client, force=True)

    assert output.read_bytes() == previous_cache
    assert paths.source_manifest.read_bytes() == previous_manifest


def test_bea_cache_rejects_invalid_msa_identifier_even_with_valid_values(
    tmp_path: Path,
) -> None:
    content, source = _archive()
    frame = (
        parse_archive(content, source)
        .with_columns(
            pl.when(pl.col("cost_of_living_geography_id") == "10420")
            .then(pl.lit("ABCDE"))
            .otherwise(pl.col("cost_of_living_geography_id"))
            .alias("cost_of_living_geography_id")
        )
        .sort("cost_of_living_geography_id")
    )
    cached = tmp_path / BEA_CACHE_NAME
    frame.write_parquet(cached)

    with pytest.raises(SourceContractError, match="geography scope"):
        validate_cached(cached, source)


def test_bea_cache_rejects_self_consistent_value_forgery(tmp_path: Path) -> None:
    content, source = _archive()
    frame = parse_archive(content, source).with_columns(
        pl.when(pl.col("cost_of_living_geography_id") == "10180")
        .then(pl.col("cost_of_living_index") + 1)
        .otherwise(pl.col("cost_of_living_index"))
        .alias("cost_of_living_index")
    )
    cached = tmp_path / BEA_CACHE_NAME
    frame.write_parquet(cached)

    with pytest.raises(SourceContractError, match="normalized checksum mismatch"):
        validate_cached(cached, source)


def test_optional_bea_failure_warns_without_raising(tmp_path: Path) -> None:
    paths = RuntimePaths.from_root(tmp_path)
    messages: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> Path:
        raise SourceContractError("offline")

    output, warning = download_optional_bea(
        paths,
        download=fail,
        progress=lambda _value, message: messages.append(message),
    )
    assert output is None
    assert warning == "Optional BEA RPP source unavailable: offline"
    assert messages == [warning]
