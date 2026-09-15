from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

import househunter.housing_stock as housing_stock
from househunter.errors import HouseHunterError
from househunter.housing_stock import (
    BUNDLED_HOUSING_STOCK,
    default_source_lock_path,
    load_raw_tables,
    validate_housing_stock_assets,
    write_housing_stock_bundle,
)

B25034_COLUMNS = [
    "GEO_ID",
    "B25034_E001",
    "B25034_M001",
    "B25034_E002",
    "B25034_M002",
    "B25034_E003",
    "B25034_M003",
    "B25034_E004",
    "B25034_M004",
]
B25035_COLUMNS = ["GEO_ID", "B25035_E001", "B25035_M001"]


def _write_raw(path: Path, columns: list[str], rows: list[list[object]]) -> None:
    path.write_text(
        "|".join(columns)
        + "\n"
        + "".join("|".join(str(value) for value in row) + "\n" for row in rows)
    )


def _contract(path: Path, columns: list[str], row_count: int) -> dict[str, object]:
    with path.open("rb") as handle:
        header = handle.readline()
    return {
        "url": f"https://example.test/{path.name}",
        "expected_size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "header_sha256": hashlib.sha256(header).hexdigest(),
        "expected_row_count": row_count,
        "required_columns": columns,
    }


def _sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    b25034 = tmp_path / "b25034.dat"
    b25035 = tmp_path / "b25035.dat"
    age_rows = [
        ["1400000US01001000100", 100, 5, 10, 2, 20, 3, 30, 4],
        ["1400000US01001000200", 0, 0, 0, 0, 0, 0, 0, 0],
        ["1400000US72001000100", -666666666, -222222222, 0, 0, 0, 0, 0, 0],
        ["0500000US01001", 200, 7, 20, 3, 30, 4, 50, 5],
        ["0500000US72001", 50, 4, 50, 4, 0, 0, 0, 0],
        ["313M700US3386001001", 200, 7, 20, 3, 40, 4, 60, 5],
        ["0100000US", 1000, 10, 100, 3, 200, 4, 300, 5],
    ]
    median_rows = [
        ["1400000US01001000100", 1995, 3],
        ["1400000US01001000200", -666666666, -222222222],
        ["1400000US72001000100", 2005, 4],
        ["0500000US01001", 1990, 2],
        ["0500000US72001", 2021, 1],
    ]
    _write_raw(b25034, B25034_COLUMNS, age_rows)
    _write_raw(b25035, B25035_COLUMNS, median_rows)
    lock = {
        "schema": 1,
        "release_year": 2024,
        "survey": "ACS 2024 five-year estimates",
        "omb_delineation": "OMB Bulletin No. 23-01",
        "geography_counts": {"tracts": 3, "counties": 2},
        "county_cbsa_geography": {
            "expected_relationship_rows": 1,
            "expected_cbsa_count": 1,
        },
        "sources": {
            "B25034": _contract(b25034, B25034_COLUMNS, len(age_rows)),
            "B25035": _contract(b25035, B25035_COLUMNS, len(median_rows)),
        },
    }
    lock_path = tmp_path / "source-lock.json"
    lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n")
    return b25034, b25035, lock_path


def _replace_artifact(directory: Path, key: str, frame: pl.DataFrame) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    record = manifest["files"][key]
    artifact = directory / record["filename"]
    frame.write_parquet(artifact, compression="zstd", statistics=True)
    record.update(
        bytes=artifact.stat().st_size,
        sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        rows=frame.height,
        columns={name: str(dtype) for name, dtype in frame.schema.items()},
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def test_raw_tables_use_direct_geography_formulas_and_explicit_statuses(
    tmp_path: Path,
) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    lock = json.loads(lock_path.read_text())
    tracts, counties, county_msa = load_raw_tables(b25034, b25035, lock)

    tract = tracts.filter(pl.col("tract_id") == "01001000100").row(0, named=True)
    assert tract["housing_built_2020_plus_pct"] == 10.0
    assert tract["housing_built_2010_plus_pct"] == 30.0
    assert tract["housing_built_2000_plus_pct"] == 60.0
    assert tract["housing_median_year_built"] == 1995
    assert tract["housing_stock_coverage_status"] == "complete"
    assert (
        tracts.filter(pl.col("tract_id") == "01001000200")[
            "housing_stock_coverage_status"
        ].item()
        == "zero_housing"
    )
    puerto_rico = tracts.filter(pl.col("tract_id") == "72001000100").row(0, named=True)
    assert puerto_rico["housing_stock_total_units_estimate"] is None
    assert puerto_rico["housing_built_2000_plus_pct"] is None
    assert puerto_rico["housing_stock_coverage_status"] == "missing_acs"
    county = counties.filter(pl.col("county_fips") == "01001").row(0, named=True)
    assert county["housing_built_2000_plus_pct"] == 50.0
    assert (
        counties.filter(pl.col("county_fips") == "72001")[
            "housing_built_2000_plus_pct"
        ].item()
        == 100.0
    )
    assert county_msa.to_dicts() == [{"county_fips": "01001", "cbsa_id": "33860"}]


def test_bundle_is_reproducible_and_validates_physical_artifacts(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_housing_stock_bundle(
        first, b25034, b25035, source_lock_path=lock_path
    )
    write_housing_stock_bundle(
        second, b25034, b25035, source_lock_path=lock_path
    )
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.iterdir()
    } == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.iterdir()
    }
    bundle = validate_housing_stock_assets(first, source_lock_path=lock_path)
    assert bundle.tracts.height == 3
    (first / "tracts.parquet").write_bytes(b"corrupt")
    with pytest.raises(HouseHunterError, match="artifact differs"):
        validate_housing_stock_assets(first, source_lock_path=lock_path)


def test_reviewed_bundled_assets_validate_against_the_source_lock() -> None:
    bundle = validate_housing_stock_assets(
        BUNDLED_HOUSING_STOCK, source_lock_path=default_source_lock_path()
    )

    assert bundle.tracts.height == 85_382
    assert bundle.counties.height == 3_222
    assert bundle.county_msa.height == 1_915


def test_bundle_validator_rejects_manifest_authorized_row_loss(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    output = tmp_path / "bundle"
    write_housing_stock_bundle(output, b25034, b25035, source_lock_path=lock_path)
    tracts = pl.read_parquet(output / "tracts.parquet").slice(1)
    _replace_artifact(output, "tracts", tracts)

    with pytest.raises(HouseHunterError, match="row count"):
        validate_housing_stock_assets(output, source_lock_path=lock_path)


def test_bundle_validator_normalizes_malformed_manifest_errors(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    output = tmp_path / "bundle"
    write_housing_stock_bundle(output, b25034, b25035, source_lock_path=lock_path)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["release_year"]
    manifest_path.write_text(json.dumps(manifest) + "\n")

    with pytest.raises(HouseHunterError, match="manifest structure"):
        validate_housing_stock_assets(output, source_lock_path=lock_path)


def test_bundle_validator_rejects_crosswalk_county_missing_from_county_asset(
    tmp_path: Path,
) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    output = tmp_path / "bundle"
    write_housing_stock_bundle(output, b25034, b25035, source_lock_path=lock_path)
    county_msa = pl.read_parquet(output / "county_msa.parquet").with_columns(
        pl.lit("99999").alias("county_fips")
    )
    _replace_artifact(output, "county_msa", county_msa)

    with pytest.raises(HouseHunterError, match="unknown county"):
        validate_housing_stock_assets(output, source_lock_path=lock_path)


def test_bundle_validator_rejects_tract_with_missing_parent_county(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    output = tmp_path / "bundle"
    write_housing_stock_bundle(output, b25034, b25035, source_lock_path=lock_path)
    tracts = (
        pl.read_parquet(output / "tracts.parquet")
        .with_columns(
            pl.when(pl.col("tract_id") == "72001000100")
            .then(pl.lit("71001000100"))
            .otherwise(pl.col("tract_id"))
            .alias("tract_id")
        )
        .sort("tract_id")
    )
    _replace_artifact(output, "tracts", tracts)

    with pytest.raises(HouseHunterError, match="tract references an unknown county"):
        validate_housing_stock_assets(output, source_lock_path=lock_path)


def test_invalid_raw_data_does_not_replace_existing_bundle(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    output = tmp_path / "bundle"
    write_housing_stock_bundle(
        output, b25034, b25035, source_lock_path=lock_path
    )
    before = (output / "manifest.json").read_bytes()
    b25034.write_text(b25034.read_text().replace("|100|5|", "|-1|5|", 1))
    lock = json.loads(lock_path.read_text())
    lock["sources"]["B25034"] = _contract(
        b25034, B25034_COLUMNS, lock["sources"]["B25034"]["expected_row_count"]
    )
    lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n")

    with pytest.raises(HouseHunterError, match="negative value"):
        write_housing_stock_bundle(
            output, b25034, b25035, source_lock_path=lock_path
        )
    assert (output / "manifest.json").read_bytes() == before


def test_publish_failure_restores_the_complete_previous_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    output = tmp_path / "bundle"
    write_housing_stock_bundle(output, b25034, b25035, source_lock_path=lock_path)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    real_replace = housing_stock.os.replace
    calls = 0

    def fail_publish(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(housing_stock.os, "replace", fail_publish)
    with pytest.raises(OSError, match="injected publication failure"):
        write_housing_stock_bundle(output, b25034, b25035, source_lock_path=lock_path)

    assert {path.name: path.read_bytes() for path in output.iterdir()} == before
    assert list(tmp_path.glob(".*.staging")) == []
    assert list(tmp_path.glob(".*.backup")) == []


def test_raw_checksum_drift_is_rejected(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    b25034.write_text(b25034.read_text() + "0100000US|0|0|0|0|0|0|0|0\n")

    with pytest.raises(HouseHunterError, match="unexpected size"):
        load_raw_tables(b25034, b25035, json.loads(lock_path.read_text()))


def test_raw_tables_reject_nonnumeric_estimates(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    b25034.write_text(b25034.read_text().replace("|100|5|", "|not-a-number|5|", 1))
    lock = json.loads(lock_path.read_text())
    lock["sources"]["B25034"] = _contract(
        b25034, B25034_COLUMNS, lock["sources"]["B25034"]["expected_row_count"]
    )
    lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n")

    with pytest.raises(HouseHunterError, match="Cannot parse pinned ACS"):
        load_raw_tables(b25034, b25035, lock)


def test_raw_tables_reject_multiple_cbsa_assignments_for_a_county(tmp_path: Path) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    b25034.write_text(
        b25034.read_text() + "313M700US9999901001|200|7|20|3|30|4|50|5\n"
    )
    lock = json.loads(lock_path.read_text())
    lock["sources"]["B25034"] = _contract(
        b25034, B25034_COLUMNS, lock["sources"]["B25034"]["expected_row_count"] + 1
    )
    lock["county_cbsa_geography"] = {
        "expected_relationship_rows": 2,
        "expected_cbsa_count": 2,
    }
    lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n")

    with pytest.raises(HouseHunterError, match="multiple CBSAs"):
        load_raw_tables(b25034, b25035, lock)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("1400000US01001000100", "1400000US0100100010X", "malformed GEO_ID"),
        ("1400000US01001000200", "1400000US01001000100", "duplicate tract_id"),
    ],
)
def test_raw_tables_reject_malformed_and_duplicate_geographies(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    b25034, b25035, lock_path = _sources(tmp_path)
    b25034.write_text(b25034.read_text().replace(old, new, 1))
    lock = json.loads(lock_path.read_text())
    lock["sources"]["B25034"] = _contract(
        b25034, B25034_COLUMNS, lock["sources"]["B25034"]["expected_row_count"]
    )
    lock_path.write_text(json.dumps(lock, sort_keys=True) + "\n")
    with pytest.raises(HouseHunterError, match=message):
        load_raw_tables(b25034, b25035, lock)
