from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from househunter.errors import HouseHunterError
from househunter.ranking_reference import (
    BUNDLE_SCHEMA_VERSION,
    CALIBRATION_ID,
    COUNTY_COLUMNS,
    CRIME_COVERAGE_FLOOR,
    HOMESCHOOL_NOTICE,
    METHODOLOGY_ID,
    PILLAR_INTERNALS,
    RankingBundle,
    assemble_ranking_sidecar,
    average_tie_percentile,
    load_appalachia_counties,
    load_homeschool_policy,
    load_source_lock,
    pillar_utility,
    ranking_bundle_identity,
    synthetic_fixture_rows,
    validate_ranking_assets,
    write_ranking_bundle,
    write_synthetic_fixture_bundle,
)
from househunter.secure_fetch import (
    deny_restricted_data_class,
    download_locked_file,
    extract_locked_archive,
    validated_https_url,
)


def test_source_lock_is_runtime_fetch_free() -> None:
    lock = load_source_lock()
    assert lock["methodology_id"] == METHODOLOGY_ID
    assert lock["runtime_fetch"] is False
    assert all(source["runtime_fetch"] is False for source in lock["sources"])
    assert all(source["package_raw"] is False for source in lock["sources"])
    deny_restricted_data_class("broadband_aggregate")
    with pytest.raises(HouseHunterError, match="denied"):
        deny_restricted_data_class("fcc_location_fabric")
    with pytest.raises(HouseHunterError, match="denied"):
        deny_restricted_data_class("nibrs_incident")


def test_homeschool_policy_covers_states_and_is_not_legal_advice() -> None:
    policy = load_homeschool_policy()
    assert policy["notice"] == HOMESCHOOL_NOTICE
    assert "AL" in policy["jurisdictions"]
    assert "DC" in policy["jurisdictions"]
    assert policy["jurisdictions"]["TX"]["utility"] == 0.95
    assert policy["jurisdictions"]["NY"]["citations"]


def test_appalachia_does_not_include_autauga() -> None:
    counties = load_appalachia_counties()
    assert "01001" not in counties
    assert "54001" in counties


def test_synthetic_bundle_round_trips(tmp_path: Path) -> None:
    output = tmp_path / "ranking_v2"
    bundle = write_synthetic_fixture_bundle(output)
    loaded = validate_ranking_assets(output)
    assert loaded.manifest["schema_version"] == BUNDLE_SCHEMA_VERSION
    assert loaded.manifest["methodology_id"] == METHODOLOGY_ID
    assert loaded.manifest["calibration_id"] == CALIBRATION_ID
    assert loaded.manifest["scope"] == "fixture"
    assert loaded.counties.height == 2
    assert loaded.counties["county_fips"].to_list() == ["01001", "02001"]
    assert bundle.manifest["calibration_hash"] == loaded.manifest["calibration_hash"]
    assert loaded.homeschool["notice"] == HOMESCHOOL_NOTICE
    assert set(loaded.counties.columns) == set(COUNTY_COLUMNS)


def test_checksum_and_schema_drift_are_rejected(tmp_path: Path) -> None:
    output = tmp_path / "ranking_v2"
    write_synthetic_fixture_bundle(output)
    counties = output / "counties.parquet"
    frame = pl.read_parquet(counties).with_columns(pl.col("population") + 1)
    frame.write_parquet(counties)
    with pytest.raises(HouseHunterError, match="artifact differs"):
        validate_ranking_assets(output)


def _write_mutated_bundle(tmp_path: Path, rows: list[dict[str, Any]], *, name: str) -> None:
    frame = pl.DataFrame(rows).select(COUNTY_COLUMNS).sort("county_fips")
    write_ranking_bundle(
        tmp_path / name,
        frame,
        calibration={"id": CALIBRATION_ID},
        homeschool=load_homeschool_policy(),
        citations={"notice": HOMESCHOOL_NOTICE},
        scope="fixture",
    )


def test_suppressed_crime_cannot_be_zeroed(tmp_path: Path) -> None:
    rows = synthetic_fixture_rows()
    rows[0]["crime_coverage"] = 0.5
    rows[0]["u_crime"] = 0.0
    rows[0]["crime_violent_rate"] = None
    rows[0]["crime_property_rate"] = None
    with pytest.raises(HouseHunterError, match="90% coverage"):
        _write_mutated_bundle(tmp_path, rows, name="bad")


def test_suppressed_crime_cannot_keep_a_nonzero_utility(tmp_path: Path) -> None:
    rows = synthetic_fixture_rows()
    rows[0]["crime_coverage"] = 0.95
    rows[0]["u_crime"] = 0.5
    rows[0]["crime_violent_rate"] = None
    rows[0]["crime_property_rate"] = None
    rows[0]["u_safety"] = pillar_utility(
        {
            "hazard": rows[0]["u_hazard"],
            "crime": rows[0]["u_crime"],
            "water": rows[0]["u_water"],
        },
        PILLAR_INTERNALS["safety"],
    )
    with pytest.raises(HouseHunterError, match="suppressed crime"):
        _write_mutated_bundle(tmp_path, rows, name="nonzero-suppressed")


def test_null_or_nonpositive_population_is_rejected(tmp_path: Path) -> None:
    null_rows = synthetic_fixture_rows()
    null_rows[0]["population"] = None
    with pytest.raises(HouseHunterError, match="positive estimate"):
        _write_mutated_bundle(tmp_path, null_rows, name="null-population")

    zero_rows = synthetic_fixture_rows()
    zero_rows[0]["population"] = 0
    with pytest.raises(HouseHunterError, match="positive estimate"):
        _write_mutated_bundle(tmp_path, zero_rows, name="zero-population")

    positive_rows = synthetic_fixture_rows()
    positive_rows[0]["population"] = 1
    _write_mutated_bundle(tmp_path, positive_rows, name="positive-population")


def test_connecticut_crosswalk_is_rejected(tmp_path: Path) -> None:
    rows = synthetic_fixture_rows()
    rows[0]["county_fips"] = "09001"
    rows[0]["state"] = "NY"
    frame = pl.DataFrame(rows).select(COUNTY_COLUMNS).sort("county_fips")
    with pytest.raises(HouseHunterError, match="Connecticut"):
        write_ranking_bundle(
            tmp_path / "ct",
            frame,
            calibration={"id": CALIBRATION_ID},
            homeschool=load_homeschool_policy(),
            citations={"notice": HOMESCHOOL_NOTICE},
            scope="fixture",
        )


def test_https_host_policy_and_hostile_redirect(tmp_path: Path) -> None:
    allowed = {"data.example.test"}
    validated_https_url("https://data.example.test/file.parquet", allowed)
    with pytest.raises(HouseHunterError, match="HTTPS host policy"):
        validated_https_url("https://evil.example/file.parquet", allowed)
    with pytest.raises(HouseHunterError, match="cannot use an IP address"):
        validated_https_url("https://127.0.0.1/file.parquet", {"127.0.0.1"})

    payload = b"ok-bytes-ok-bytes-ok-bytes-ok-bytes\n"
    digest = __import__("hashlib").sha256(payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.example.test":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, content=b"stolen")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)
    with pytest.raises(HouseHunterError, match="HTTPS host policy|redirect"):
        download_locked_file(
            "https://data.example.test/file.parquet",
            tmp_path / "file.parquet",
            expected_size=len(payload),
            expected_sha256=digest,
            allowed_hosts=allowed,
            validate_dns=False,
            client=client,
        )


def test_nested_archive_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "nested.zip"
    with zipfile.ZipFile(archive_path, "w") as handle:
        handle.writestr("inner.zip", b"nested")
    source = {
        "archive": {
            "root": "extracted",
            "total_uncompressed_size": 6,
            "members": [{"path": "inner.zip", "size": 6, "sha256": "00" * 32}],
        }
    }
    with pytest.raises(HouseHunterError, match="nested archive"):
        extract_locked_archive(source, archive_path, tmp_path)


def test_archive_root_cannot_escape_or_skip_verification(tmp_path: Path) -> None:
    content = b"ok-bytes-ok-bytes-ok-bytes-ok-bytes\n"
    source = {
        "archive": {
            "root": "..",
            "total_uncompressed_size": len(content),
            "members": [
                {
                    "path": "data.parquet",
                    "size": len(content),
                    "sha256": __import__("hashlib").sha256(content).hexdigest(),
                }
            ],
        }
    }
    with pytest.raises(HouseHunterError, match="Archive root"):
        extract_locked_archive(source, tmp_path / "missing.zip", tmp_path)

    destination = tmp_path / "sources"
    destination.mkdir()
    (destination / "bundle").mkdir()
    source["archive"]["root"] = "bundle"
    with pytest.raises(HouseHunterError, match="missing"):
        extract_locked_archive(source, tmp_path / "missing.zip", destination)


def test_average_tie_percentile_uses_midranks() -> None:
    assert average_tie_percentile([1.0, 2.0, 2.0, 4.0]) == [0.0, 0.5, 0.5, 1.0]
    assert average_tie_percentile([90.0, 90.0, 110.0], invert=True) == [0.75, 0.75, 0.0]


def test_crime_coverage_floor_is_locked() -> None:
    assert CRIME_COVERAGE_FLOOR == 0.90


def test_bundle_identity_covers_files_not_just_calibration(tmp_path: Path) -> None:
    bundle = write_synthetic_fixture_bundle(tmp_path / "ranking_v2")
    baseline = ranking_bundle_identity(bundle.manifest)
    drifted_files = dict(bundle.manifest)
    files = dict(bundle.manifest["files"])
    citations = dict(files["citations"])
    citations["sha256"] = "ab" * 32
    files["citations"] = citations
    drifted_files["files"] = files
    assert ranking_bundle_identity(drifted_files) != baseline
    drifted_scope = dict(bundle.manifest)
    drifted_scope["scope"] = "national"
    assert ranking_bundle_identity(drifted_scope) != baseline


def test_sidecar_binds_affordability_to_calibration_internals(tmp_path: Path) -> None:
    bundle = write_synthetic_fixture_bundle(tmp_path / "ranking_v2")
    identity = pl.DataFrame(
        {
            "place_id": ["01001", "02001"],
            "name": ["Autauga", "Aleutians East"],
            "state": ["AL", "AK"],
        }
    )
    housing = pl.DataFrame(
        {
            "county_fips": ["01001", "02001"],
            "housing_valid_months": [12, 12],
            "median_active_listings": [150.0, 150.0],
            "median_ppsf": [800.0, 400.0],
            "sqft_for_1m_t12": [1250.0, 2500.0],
        }
    )
    sidecar = assemble_ranking_sidecar(bundle, identity, housing)
    assert sidecar.height == 2
    assert "preference_fit" not in sidecar.columns
    drifted = RankingBundle(
        manifest=bundle.manifest,
        counties=bundle.counties,
        calibration={
            **bundle.calibration,
            "pillar_internals": {
                **PILLAR_INTERNALS,
                "affordability": {"housing": 1.0, "rpp": 0.0, "property_tax": 0.0},
            },
        },
        homeschool=bundle.homeschool,
        citations=bundle.citations,
    )
    with pytest.raises(HouseHunterError, match="pillar internals"):
        assemble_ranking_sidecar(drifted, identity, housing)
