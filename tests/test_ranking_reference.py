from __future__ import annotations

import gzip
import json
import runpy
import zipfile
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

import househunter.ranking_pipeline as ranking_pipeline
from househunter.config import canonical_json, sha256_bytes, sha256_file
from househunter.errors import HouseHunterError
from househunter.ranking_reference import (
    BUNDLE_SCHEMA_VERSION,
    CALIBRATION_ID,
    COUNTY_COLUMNS,
    CRIME_COVERAGE_FLOOR,
    HOMESCHOOL_NOTICE,
    HOMESCHOOL_SOURCE_CHECK_SCOPE,
    METHODOLOGY_ID,
    PILLAR_INTERNALS,
    RankingBundle,
    assemble_ranking_sidecar,
    average_tie_percentile,
    bundle_coverage_summary,
    load_appalachia_counties,
    load_homeschool_policy,
    load_source_lock,
    pillar_utility,
    ranking_bundle_identity,
    synthetic_fixture_rows,
    validate_fbi_response_manifest,
    validate_ranking_assets,
    write_ranking_bundle,
    write_synthetic_fixture_bundle,
)
from househunter.secure_fetch import (
    deny_restricted_data_class,
    download_locked_file,
    extract_locked_archive,
    request_bounded_bytes,
    validated_https_url,
)


def test_source_lock_is_runtime_fetch_free() -> None:
    lock = load_source_lock()
    assert lock["methodology_id"] == METHODOLOGY_ID
    assert lock["schema"] == 2
    assert lock["policy"] == {
        "credentials": False,
        "package_private_rows": False,
        "package_raw": False,
        "runtime_fetch": False,
    }
    assert lock["county_universe"]["row_count"] == 3144
    assert all(source["runtime_fetch"] is False for source in lock["sources"])
    assert all(source["package_raw"] is False for source in lock["sources"])
    sdwis = next(source for source in lock["sources"] if source["name"].startswith("epa_sdwis"))
    assert sdwis["expected_submission_year_quarter"] == "2026Q2"
    assert "SDWA_PUB_WATER_SYSTEMS.PRIMACY_AGENCY_CODE" in sdwis["required_fields"]
    assert "SDWA_SERVICE_AREAS.SERVICE_AREA_TYPE_CODE" in sdwis["required_fields"]
    assert "SDWA_GEOGRAPHIC_AREAS.SUBMISSION_STATUS_CODE" not in sdwis["required_fields"]
    assert "SDWA_GEOGRAPHIC_AREAS.STATE_SERVED" not in sdwis["required_fields"]
    water = lock["transforms"]["water"]
    assert "exactly {WH}" in water["active_retail_filter"]
    assert "max-intersection proxy" in water["public_water_coverage"]
    deny_restricted_data_class("broadband_aggregate")
    with pytest.raises(HouseHunterError, match="denied"):
        deny_restricted_data_class("fcc_location_fabric")
    with pytest.raises(HouseHunterError, match="denied"):
        deny_restricted_data_class("nibrs_incident")


def test_ranking_maintainer_docs_name_real_ordinary_commands() -> None:
    documentation = (
        Path(__file__).parents[1] / "config" / "ranking" / "README.md"
    ).read_text()
    assert "`househunter prepare`" not in documentation
    assert "`scripts/dev`" in documentation
    assert "`househunter download`" in documentation
    assert "`househunter build`" in documentation


def test_source_lock_binds_project_authored_inputs() -> None:
    root = Path(__file__).parents[1]
    lock = load_source_lock()
    sources = {item["name"]: item for item in lock["sources"]}
    homeschool = sources["homeschool_policy_v1"]["identity"]
    appalachia = sources["arc_appalachia_counties"]["identity"]
    housing = sources["househunter_housing_policy"]["identity"]
    assert sha256_file(root / "config" / "ranking" / homeschool["filename"]) == homeschool["sha256"]
    assert sha256_file(root / "config" / "ranking" / appalachia["filename"]) == appalachia["sha256"]
    assert (
        sha256_bytes(canonical_json(lock["transforms"]["housing"]["knots"]))
        == housing["knots_sha256"]
    )


def test_normalized_checkpoint_contracts_are_source_scoped() -> None:
    lock = load_source_lock()
    original = ranking_pipeline._sources_contract(lock, "census_pep_county_2025")
    homeschool = next(
        source for source in lock["sources"] if source["name"] == "homeschool_policy_v1"
    )
    homeschool["retrieved_on"] = "2099-01-01"
    assert ranking_pipeline._sources_contract(lock, "census_pep_county_2025") == original
    population = next(
        source for source in lock["sources"] if source["name"] == "census_pep_county_2025"
    )
    population["retrieved_on"] = "2099-01-01"
    assert ranking_pipeline._sources_contract(lock, "census_pep_county_2025") != original


def test_etl_revalidates_staged_bytes_before_normalized_checkpoint_reuse(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "raw" / "fixture_source" / "fixture.csv"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"locked\n")
    lock = {
        "sources": [
            {
                "name": "fixture_source",
                "artifacts": [
                    {
                        "filename": staged.name,
                        "bytes": staged.stat().st_size,
                        "sha256": sha256_file(staged),
                    }
                ],
            }
        ]
    }
    identity = ranking_pipeline._validate_locked_raw_artifacts(tmp_path, lock)
    assert len(identity) == 64
    staged.write_bytes(b"drift!\n")
    with pytest.raises(HouseHunterError, match="byte count|checksum"):
        ranking_pipeline._validate_locked_raw_artifacts(tmp_path, lock)


@pytest.mark.parametrize(
    ("stage", "old_contract", "new_contract"),
    [
        (
            "crime",
            {"stage": "crime", "schema": 2, "sources_sha256": "locked"},
            {
                "stage": "crime",
                "schema": ranking_pipeline.CRIME_STAGE_SCHEMA,
                "sources_sha256": "locked",
            },
        ),
        (
            "employment",
            {"stage": "employment", "sources_sha256": "locked"},
            {
                "stage": "employment",
                "schema": ranking_pipeline.EMPLOYMENT_STAGE_SCHEMA,
                "sources_sha256": "locked",
            },
        ),
    ],
)
def test_changed_transform_contract_invalidates_normalized_checkpoint(
    tmp_path: Path,
    stage: str,
    old_contract: dict[str, object],
    new_contract: dict[str, object],
) -> None:
    checkpoint = tmp_path / "normalized" / f"{stage}.parquet"
    ranking_pipeline._write_frame_checkpoint(
        checkpoint,
        pl.DataFrame({"county_fips": ["01001"], "marker": ["stale"]}),
        contract=old_contract,
    )
    builds = 0

    def rebuild() -> pl.DataFrame:
        nonlocal builds
        builds += 1
        return pl.DataFrame({"county_fips": ["01001"], "marker": ["rebuilt"]})

    frame = ranking_pipeline._normalized_stage(
        tmp_path, stage, contract=new_contract, build=rebuild
    )
    assert builds == 1
    assert frame["marker"].to_list() == ["rebuilt"]
    metadata = json.loads(checkpoint.with_suffix(".parquet.checkpoint.json").read_text())
    assert metadata["contract"] == new_contract


def test_etl_requires_immutable_snapshot_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "national-fixture"
    snapshot.mkdir()
    counties = pl.DataFrame(
        {
            "place_id": ["01001", "72001"],
            "res_hazard_npctl": [25.0, 40.0],
            "community_conditions_group": [4, 7],
        }
    )
    counties.write_parquet(snapshot / "counties.parquet")
    logical_checksum = sha256_bytes(
        canonical_json([list(row) for row in counties.sort("place_id").iter_rows()])
    )
    metadata = {
        "schema_version": 13,
        "build_id": snapshot.name,
        "scope": {"kind": "national", "state": None},
        "input_checksums": {"fema_counties": "f" * 64, "chrr": "c" * 64},
        "logical_checksums": {"counties": logical_checksum},
    }
    (snapshot / "build.json").write_text(json.dumps(metadata))
    (snapshot / "househunter.duckdb").write_bytes(b"validated-by-test-double")
    county_fips_sha256 = sha256_bytes(canonical_json(["01001"]))
    source_lock = {
        "county_universe": {
            "row_count": 1,
            "sorted_fips_sha256": county_fips_sha256,
        },
        "sources": [
            {
                "name": "fema_residential_hazard",
                "identity": {"canonical_sha256": "f" * 64},
            },
            {
                "name": "chrr_community_context_2025",
                "identity": {"canonical_sha256": "c" * 64},
            },
        ],
    }
    monkeypatch.setattr(ranking_pipeline, "snapshot_artifacts_are_valid", lambda _: True)
    ranking_pipeline._validate_snapshot_input(snapshot, source_lock)

    source_lock["sources"][0]["identity"]["canonical_sha256"] = "d" * 64
    with pytest.raises(HouseHunterError, match="source identities"):
        ranking_pipeline._validate_snapshot_input(snapshot, source_lock)
    source_lock["sources"][0]["identity"]["canonical_sha256"] = "f" * 64

    monkeypatch.setattr(ranking_pipeline, "snapshot_artifacts_are_valid", lambda _: False)
    with pytest.raises(HouseHunterError, match="immutable artifact validation"):
        ranking_pipeline._validate_snapshot_input(snapshot, source_lock)


def test_packaged_production_ranking_bundle_is_valid() -> None:
    bundle = validate_ranking_assets()
    assert bundle.manifest["scope"] == "national"
    assert bundle.manifest["row_count"] == 3144
    assert bundle.counties.height == 3144
    coverage = bundle.manifest["coverage"]
    assert coverage == bundle_coverage_summary(bundle.counties)
    assert coverage["complete_public_core_count"] == 1284
    assert coverage["partial_public_core_count"] == 1860
    assert coverage["source_status_counts"]["crime_status"]["complete"] == 1378
    assert coverage["source_status_counts"]["water_status"]["complete"] == 2975
    assert coverage["source_status_counts"]["climate_coverage_status"][
        "complete_in_county_station_mean"
    ] == 2817


def test_etl_rejects_tampered_and_extra_fbi_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b'{"locked":true}'
    manifest = {
        "contract_sha256": "a" * 64,
        "responses": [
            {
                "kind": "summary",
                "key": "AL0010000:violent-crime",
                "bytes": len(payload),
                "sha256": __import__("hashlib").sha256(payload).hexdigest(),
            }
        ],
    }
    monkeypatch.setattr(
        ranking_pipeline,
        "validate_fbi_response_manifest",
        lambda **_: manifest,
    )
    summary = (
        tmp_path / "raw" / "fbi" / ("a" * 64) / "summaries" / "violent-crime" / "AL0010000.json"
    )
    summary.parent.mkdir(parents=True)
    summary.write_bytes(payload)
    ranking_pipeline._validate_fbi_summary_inputs(tmp_path, None)

    summary.write_bytes(b'{"locked":false}')
    with pytest.raises(HouseHunterError, match="differs"):
        ranking_pipeline._validate_fbi_summary_inputs(tmp_path, None)
    summary.write_bytes(payload)
    extra = summary.with_name("EXTRA.json")
    extra.write_bytes(payload)
    with pytest.raises(HouseHunterError, match="inventory"):
        ranking_pipeline._validate_fbi_summary_inputs(tmp_path, None)


@pytest.mark.parametrize("field", ["api_key", "authorization_token", "staging_path"])
def test_source_lock_rejects_credentials_and_local_paths(tmp_path: Path, field: str) -> None:
    lock = load_source_lock()
    lock["sources"][0][field] = "must-not-be-present"
    path = tmp_path / "source-lock.json"
    path.write_text(json.dumps(lock))
    with pytest.raises(HouseHunterError, match="forbidden field|unreviewed field"):
        load_source_lock(path)


def test_source_lock_rejects_unreviewed_urls_and_artifact_fields(tmp_path: Path) -> None:
    lock = load_source_lock()
    artifact = lock["sources"][0]["artifacts"][0]
    artifact["url"] = "https://127.0.0.1/private"
    artifact["headers"] = {"Authorization": "secret"}
    path = tmp_path / "source-lock.json"
    path.write_text(json.dumps(lock))
    with pytest.raises(HouseHunterError, match="forbidden field|unreviewed field"):
        load_source_lock(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("catalog_max_bytes", 17 * 1024 * 1024),
        ("summary_max_bytes", 3 * 1024 * 1024),
        ("included_agency_types", ["City", "County"]),
        ("in_scope_states", ["AL"]),
    ],
)
def test_source_lock_rejects_fbi_request_contract_drift(
    tmp_path: Path, field: str, value: object
) -> None:
    lock = load_source_lock()
    source = next(item for item in lock["sources"] if item["name"].startswith("fbi_ucr"))
    source["api"][field] = value
    path = tmp_path / "source-lock.json"
    path.write_text(json.dumps(lock))
    with pytest.raises(HouseHunterError, match="response contract"):
        load_source_lock(path)


def test_fbi_acquisition_script_matches_locked_request_contract() -> None:
    root = Path(__file__).parents[1]
    namespace = runpy.run_path(str(root / "scripts" / "acquire_ranking_fbi.py"))
    script_contract = namespace["_contract"]()
    lock = load_source_lock()
    source = next(item for item in lock["sources"] if item["name"].startswith("fbi_ucr"))
    api = source["api"]
    assert script_contract == {
        "base_url": api["base_url"],
        "catalog_path": api["agency_catalog_path"],
        "summary_path": api["summary_path"],
        "query": api["query"],
        "offenses": api["offenses"],
        "included_agency_types": api["included_agency_types"],
        "states": api["in_scope_states"],
        "catalog_max_bytes": api["catalog_max_bytes"],
        "summary_max_bytes": api["summary_max_bytes"],
    }
    assert sha256_bytes(canonical_json(script_contract)) == api["contract_sha256"]


def test_fbi_response_manifest_matches_lock() -> None:
    manifest = validate_fbi_response_manifest()
    assert manifest["catalog_count"] == 51
    assert manifest["agency_count"] == 14154
    assert manifest["exclusion_count"] == 659
    assert manifest["summary_count"] == 28308
    assert manifest["response_count"] == 28359
    assert len(manifest["county_universe"]) == 3144
    assert all(
        agency["county_fips"] in manifest["county_universe"] for agency in manifest["agencies"]
    )


def test_crime_requires_every_offense_month_but_accepts_explicit_zero(tmp_path: Path) -> None:
    months = [f"{month:02d}-{year}" for year in (2023, 2024, 2025) for month in range(1, 13)]
    contract = "a" * 64
    agencies = [
        {"ori": "AL0010001", "county_fips": "01001"},
        {"ori": "AL0030001", "county_fips": "01003"},
    ]
    for agency in agencies:
        for family in ("violent-crime", "property-crime"):
            actuals = {month: 0 for month in months}
            if agency["county_fips"] == "01001" and family == "violent-crime":
                actuals.pop("12-2025")
            payload = {
                "offenses": {"actuals": {"Fixture Offenses": actuals}},
                "populations": {
                    "population": {"Fixture": {month: 100 for month in months}},
                    "participated_population": {
                        "Fixture": {month: 100 for month in months}
                    },
                },
            }
            path = tmp_path / "raw" / "fbi" / contract / "summaries" / family / (
                agency["ori"] + ".json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))

    result = ranking_pipeline._crime(
        tmp_path, {"contract_sha256": contract, "agencies": agencies}
    ).sort("county_fips")
    missing, explicit_zero = result.iter_rows(named=True)
    assert missing["crime_status"] == "below_coverage_or_missing"
    assert missing["crime_coverage"] is None
    assert missing["crime_violent_rate"] is None
    assert missing["crime_property_rate"] is None
    assert explicit_zero["crime_status"] == "complete"
    assert explicit_zero["crime_coverage"] == pytest.approx(1.0)
    assert explicit_zero["crime_violent_rate"] == pytest.approx(0.0)
    assert explicit_zero["crime_property_rate"] == pytest.approx(0.0)


def test_employment_keeps_suppressed_commute_components_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commute = pl.DataFrame(
        {
            "GEO_ID": ["0500000US01001", "0500000US01003"],
            "B08303_E001": [100, 100],
            "B08303_E002": [10, 10],
            "B08303_E003": [10, 10],
            "B08303_E004": [None, 10],
            "B08303_E005": [10, 10],
            "B08303_E006": [10, 10],
            "B08303_E007": [10, 10],
        }
    )
    commute_path = tmp_path / "acsdt5y2024-b08303.dat"
    commute.write_csv(commute_path, separator="|")
    monkeypatch.setattr(
        ranking_pipeline,
        "_artifact",
        lambda _root, _source, filename: commute_path
        if filename.endswith(".dat")
        else Path(filename),
    )

    def qcew(path: Path, _member: str) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "county_fips": ["01001", "01003"],
                "employment": [100 if "24" in path.name else 110] * 2,
                "average_weekly_wage": [900.0, 1000.0],
            }
        )

    monkeypatch.setattr(ranking_pipeline, "_qcew", qcew)
    result = ranking_pipeline._employment(tmp_path).sort("county_fips")
    suppressed, complete = result.iter_rows(named=True)
    assert suppressed["commute_under_30_share"] is None
    assert suppressed["employment_status"] == "missing_component"
    assert complete["commute_under_30_share"] == pytest.approx(0.6)
    assert complete["employment_status"] == "complete"


def test_fbi_catalog_resolves_current_counties_and_records_unassignable_rows() -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "acquire_ranking_fbi.py")
    )
    validate_catalog = namespace["_validate_catalog"]
    crosswalk = {
        ("VA", "FAIRFAX"): "51059",
        ("VA", "FAIRFAXCITY"): "51600",
        ("VA", "LASALLE"): "51001",
    }
    payload = json.dumps(
        {
            "FAIRFAX": [{"ori": "VA0010001", "agency_type_name": "City", "counties": "FAIRFAX"}],
            "FAIRFAX CITY": [
                {
                    "ori": "VA0010002",
                    "agency_type_name": "City",
                    "counties": "FAIRFAX CITY",
                }
            ],
            "LA SALLE": [{"ori": "VA0010003", "agency_type_name": "City", "counties": "LA SALLE"}],
            "NOT SPECIFIED": [
                {
                    "ori": "VA0010004",
                    "agency_type_name": "City",
                    "counties": "NOT SPECIFIED",
                }
            ],
            "RETIRED": [{"ori": "VA0010005", "agency_type_name": "City", "counties": "RETIRED"}],
            "FAIRFAX, FAIRFAX CITY": [
                {
                    "ori": "VA0010006",
                    "agency_type_name": "City",
                    "counties": "FAIRFAX, FAIRFAX CITY",
                }
            ],
        }
    ).encode()
    agencies, exclusions = validate_catalog(payload, state="VA", county_crosswalk=crosswalk)
    assert [agency["county_fips"] for agency in agencies] == ["51059", "51600", "51001"]
    assert [(item["ori"], item["reason"]) for item in exclusions] == [
        ("VA0010004", "missing_county_attribution"),
        ("VA0010005", "unassignable_current_county"),
        ("VA0010006", "multiple_county_attribution"),
    ]


def test_fbi_response_manifest_rejects_semantic_request_tamper(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    source_manifest = root / "config" / "ranking" / "fbi-ucr-request-manifest-v1.json.gz"
    payload = json.loads(gzip.decompress(source_manifest.read_bytes()))
    payload["responses"][-1]["request"] = "/LATEST/summarized/agency/WY0230200/not-reviewed"
    payload["responses_sha256"] = sha256_bytes(canonical_json(payload["responses"]))
    raw = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    manifest_path = tmp_path / "fbi-ucr-request-manifest-v1.json.gz"
    manifest_path.write_bytes(compressed)

    lock = load_source_lock()
    api = next(
        source["api"] for source in lock["sources"] if source["name"] == "fbi_ucr_agency_2023_2025"
    )
    expected = api["response_manifest"]
    expected.update(
        {
            "bytes": len(compressed),
            "sha256": sha256_bytes(compressed),
            "uncompressed_bytes": len(raw),
            "uncompressed_sha256": sha256_bytes(raw),
            "responses_sha256": payload["responses_sha256"],
        }
    )
    lock_path = tmp_path / "source-lock-v2.json"
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(HouseHunterError, match="summary request differs"):
        validate_fbi_response_manifest(manifest_path, source_lock_path=lock_path)


def test_fbi_response_manifest_bounds_gzip_expansion(tmp_path: Path) -> None:
    lock = load_source_lock()
    api = next(
        source["api"] for source in lock["sources"] if source["name"] == "fbi_ucr_agency_2023_2025"
    )
    expected = api["response_manifest"]
    compressed = gzip.compress(b"x" * (expected["uncompressed_bytes"] + 1), mtime=0)
    manifest_path = tmp_path / "fbi-ucr-request-manifest-v1.json.gz"
    manifest_path.write_bytes(compressed)
    expected.update({"bytes": len(compressed), "sha256": sha256_bytes(compressed)})
    lock_path = tmp_path / "source-lock-v2.json"
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(HouseHunterError, match="expanded size differs"):
        validate_fbi_response_manifest(manifest_path, source_lock_path=lock_path)


def test_fbi_acquisition_resumes_only_verified_checkpoints(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    namespace = runpy.run_path(str(root / "scripts" / "acquire_ranking_fbi.py"))
    request = namespace["_request"]
    destination = tmp_path / "response.json"
    contract_sha256 = "a" * 64
    calls = 0

    def first_handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=httpx.ByteStream(b'{"fresh":true}'), request=incoming)

    with httpx.Client(transport=httpx.MockTransport(first_handler)) as client:
        status, payload = request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/example",
            destination=destination,
            max_bytes=1024,
            label="FBI fixture",
            contract_sha256=contract_sha256,
            validation_schema="fixture-v1",
            request_key="fixture:example",
            validator=json.loads,
        )
    assert (status, payload, calls) == (200, b'{"fresh":true}', 1)

    def forbidden_handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("verified checkpoint must not make a request")

    with httpx.Client(transport=httpx.MockTransport(forbidden_handler)) as client:
        assert request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/example",
            destination=destination,
            max_bytes=1024,
            label="FBI fixture",
            contract_sha256=contract_sha256,
            validation_schema="fixture-v1",
            request_key="fixture:example",
            validator=json.loads,
        ) == (200, b'{"fresh":true}')

    destination.write_bytes(b'{"tampered":true}')
    with httpx.Client(transport=httpx.MockTransport(first_handler)) as client:
        assert request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/example",
            destination=destination,
            max_bytes=1024,
            label="FBI fixture",
            contract_sha256=contract_sha256,
            validation_schema="fixture-v1",
            request_key="fixture:example",
            validator=json.loads,
        ) == (200, b'{"fresh":true}')
    assert calls == 2


def test_fbi_manifest_only_requires_exact_request_checkpoint(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "acquire_ranking_fbi.py")
    )
    request = namespace["_request"]
    read_existing = namespace["_read_existing_response"]
    contract_sha256 = "a" * 64
    destination = tmp_path / "first.json"

    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b'{"valid":true}'), request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/first",
            destination=destination,
            max_bytes=1024,
            label="FBI first fixture",
            contract_sha256=contract_sha256,
            validation_schema="fixture-v1",
            request_key="summary:ORI1:violent-crime",
            params={"from": "01-2023"},
            validator=json.loads,
        )

    checkpoint = destination.with_suffix(".json.checkpoint.json")
    checkpoint_bytes = checkpoint.read_bytes()
    common = {
        "max_bytes": 1024,
        "contract_sha256": contract_sha256,
        "validation_schema": "fixture-v1",
        "validator": json.loads,
    }
    assert (
        read_existing(
            destination,
            label="FBI first fixture",
            request_key="summary:ORI1:violent-crime",
            request="https://cde.ucr.cjis.gov/LATEST/first",
            params={"from": "01-2023"},
            **common,
        )
        == b'{"valid":true}'
    )

    checkpoint.unlink()
    with pytest.raises(HouseHunterError, match="exact acquisition checkpoint"):
        read_existing(
            destination,
            label="FBI first fixture",
            request_key="summary:ORI1:violent-crime",
            request="https://cde.ucr.cjis.gov/LATEST/first",
            params={"from": "01-2023"},
            **common,
        )
    checkpoint.write_bytes(checkpoint_bytes)

    copied = tmp_path / "second.json"
    copied.write_bytes(destination.read_bytes())
    copied.with_suffix(".json.checkpoint.json").write_bytes(checkpoint_bytes)
    with pytest.raises(HouseHunterError, match="exact acquisition checkpoint"):
        read_existing(
            copied,
            label="FBI second fixture",
            request_key="summary:ORI2:property-crime",
            request="https://cde.ucr.cjis.gov/LATEST/second",
            params={"from": "01-2023"},
            **common,
        )


def test_fbi_catalog_checkpoint_round_trips_to_manifest_only(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "acquire_ranking_fbi.py")
    )
    namespace["_catalogs"].__globals__["_states"] = lambda: ["AL"]
    catalog = {
        "Autauga": [
            {
                "ori": "AL0010000",
                "agency_type_name": "County",
                "counties": "Autauga",
            }
        ]
    }

    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps(catalog).encode()),
            request=incoming,
        )

    crosswalk = {("AL", "AUTAUGA"): "01001"}
    contract_sha256 = "a" * 64
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        online = namespace["_catalogs"](client, tmp_path, contract_sha256, crosswalk)
    offline = namespace["_catalogs_from_existing"](tmp_path, crosswalk, contract_sha256)
    assert offline == online


def test_fbi_acquisition_rejects_oversize_stream_before_buffering_all_chunks(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "acquire_ranking_fbi.py")
    )
    request = namespace["_request"]

    class OversizeStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.chunks_seen = 0

        def __iter__(self):  # type: ignore[no-untyped-def]
            for _ in range(100):
                self.chunks_seen += 1
                yield b"12345678"

    stream = OversizeStream()

    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=incoming)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="outside its bound"),
    ):
        request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/example",
            destination=tmp_path / "oversize.json",
            max_bytes=10,
            label="FBI fixture",
            contract_sha256="a" * 64,
            validation_schema="fixture-v1",
            request_key="fixture:example",
            validator=json.loads,
        )
    assert stream.chunks_seen < 100
    assert not (tmp_path / "oversize.json").exists()


def test_fbi_acquisition_rejects_compressed_expansion_before_reading(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "acquire_ranking_fbi.py")
    )
    request = namespace["_request"]

    class CompressedStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.was_read = False

        def __iter__(self):  # type: ignore[no-untyped-def]
            self.was_read = True
            yield gzip.compress(b"x" * (8 * 1024 * 1024))

    stream = CompressedStream()

    def handler(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=stream,
            request=incoming,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="encoded response"),
    ):
        request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/example",
            destination=tmp_path / "compressed.json",
            max_bytes=1024,
            label="FBI fixture",
            contract_sha256="a" * 64,
            validation_schema="fixture-v1",
            request_key="fixture:example",
            validator=json.loads,
        )
    assert stream.was_read is False
    assert not (tmp_path / "compressed.json").exists()


def test_fbi_acquisition_does_not_checkpoint_malformed_response(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "acquire_ranking_fbi.py")
    )
    request = namespace["_request"]
    destination = tmp_path / "response.json"

    def malformed(incoming: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"not-json"), request=incoming)

    with (
        httpx.Client(transport=httpx.MockTransport(malformed)) as client,
        pytest.raises(json.JSONDecodeError),
    ):
        request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/example",
            destination=destination,
            max_bytes=1024,
            label="FBI fixture",
            contract_sha256="a" * 64,
            validation_schema="fixture-v1",
            request_key="fixture:example",
            validator=json.loads,
        )
    assert not destination.exists()
    assert not destination.with_suffix(".json.checkpoint.json").exists()


def test_fbi_acquisition_retries_transient_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "acquire_ranking_fbi.py")
    )
    request = namespace["_request"]
    monkeypatch.setattr(namespace["time"], "sleep", lambda _: None)
    calls = 0

    def handler(incoming: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(404, stream=httpx.ByteStream(b"missing"), request=incoming)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"), request=incoming)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert request(
            client,
            request="https://cde.ucr.cjis.gov/LATEST/example",
            destination=tmp_path / "response.json",
            max_bytes=1024,
            label="FBI fixture",
            contract_sha256="a" * 64,
            validation_schema="fixture-v1",
            request_key="fixture:example",
            validator=json.loads,
        ) == (200, b"{}")
    assert calls == 3


def test_homeschool_policy_covers_states_and_is_not_legal_advice() -> None:
    policy = load_homeschool_policy()
    assert policy["notice"] == HOMESCHOOL_NOTICE
    assert policy["source_check_scope"] == HOMESCHOOL_SOURCE_CHECK_SCOPE
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
    assert loaded.manifest["coverage"] == bundle_coverage_summary(loaded.counties)
    assert loaded.homeschool["notice"] == HOMESCHOOL_NOTICE
    assert set(loaded.counties.columns) == set(COUNTY_COLUMNS)


def test_bundle_publication_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_synthetic_fixture_bundle(first)
    write_synthetic_fixture_bundle(second)

    assert [path.name for path in sorted(first.iterdir())] == [
        path.name for path in sorted(second.iterdir())
    ]
    for path in first.iterdir():
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_bundle_publication_uses_explicit_source_lock(tmp_path: Path) -> None:
    lock = load_source_lock()
    lock["compiled_on"] = "2026-09-17"
    lock_path = tmp_path / "source-lock-v2.json"
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    rows = synthetic_fixture_rows()
    for row in rows:
        row["citation_ids_json"] = '["notice"]'
    bundle = write_ranking_bundle(
        tmp_path / "ranking-v2",
        pl.DataFrame(rows).select(COUNTY_COLUMNS).sort("county_fips"),
        calibration={"id": CALIBRATION_ID},
        homeschool=load_homeschool_policy(),
        citations={"notice": HOMESCHOOL_NOTICE},
        source_lock_path=lock_path,
        scope="fixture",
    )
    assert bundle.manifest["source_lock_sha256"] == sha256_file(lock_path)


def test_manual_acquisition_resume_accepts_the_same_stage(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_ranking_reference.py")
    )
    acquire = namespace["_acquire_artifacts"]
    staged = tmp_path / "staged.csv"
    staged.write_bytes(b"locked\n")
    lock_path = tmp_path / "source-lock-v2.json"
    lock_path.write_text("{}\n")
    lock = {
        "allowed_hosts": [],
        "sources": [
            {
                "name": "manual",
                "artifacts": [
                    {
                        "filename": "fixture.csv",
                        "bytes": staged.stat().st_size,
                        "sha256": sha256_file(staged),
                    }
                ],
            }
        ],
    }
    arguments = {
        "lock": lock,
        "lock_path": lock_path,
        "data_root": tmp_path / "data",
        "manual_stages": {"manual": staged},
    }
    acquire(**arguments)
    acquire(**arguments)
    with pytest.raises(HouseHunterError, match="Unused manual ranking stages: unknown"):
        acquire(**{**arguments, "manual_stages": {"unknown": staged}})


def test_checksum_and_schema_drift_are_rejected(tmp_path: Path) -> None:
    output = tmp_path / "ranking_v2"
    write_synthetic_fixture_bundle(output)
    counties = output / "counties.parquet"
    frame = pl.read_parquet(counties).with_columns(pl.col("population") + 1)
    frame.write_parquet(counties)
    with pytest.raises(HouseHunterError, match="artifact differs"):
        validate_ranking_assets(output)


def test_bundle_coverage_summary_tamper_is_rejected(tmp_path: Path) -> None:
    output = tmp_path / "ranking_v2"
    write_synthetic_fixture_bundle(output)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["coverage"]["complete_public_core_count"] += 1
    manifest["release_id"] = ranking_bundle_identity(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    with pytest.raises(HouseHunterError, match="coverage summary differs"):
        validate_ranking_assets(output)


def _write_mutated_bundle(tmp_path: Path, rows: list[dict[str, Any]], *, name: str) -> None:
    for row in rows:
        row["citation_ids_json"] = '["notice"]'
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
    rows[0]["state"] = "CT"
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


def test_bundle_inventory_rejects_extra_files_and_symlinks(tmp_path: Path) -> None:
    output = tmp_path / "ranking_v2"
    write_synthetic_fixture_bundle(output)
    (output / "unexpected.txt").write_text("raw data must not be packaged")
    with pytest.raises(HouseHunterError, match="inventory"):
        validate_ranking_assets(output)

    (output / "unexpected.txt").unlink()
    (output / "unexpected-link").symlink_to(output / "citations.json")
    with pytest.raises(HouseHunterError, match="inventory"):
        validate_ranking_assets(output)


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


def test_locked_download_rejects_encoded_response_before_reading(tmp_path: Path) -> None:
    class UnreadStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("encoded response body was read")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=UnreadStream(),
            request=request,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(HouseHunterError, match="unsupported content encoding"),
    ):
        download_locked_file(
            "https://data.example.test/source.bin",
            tmp_path / "source.bin",
            expected_size=1,
            expected_sha256="0" * 64,
            allowed_hosts={"data.example.test"},
            validate_dns=False,
            client=client,
        )


def test_bounded_request_revalidates_pinned_dns_on_every_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    addresses = iter(["93.184.216.34", "8.8.8.8"])

    def resolved(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        address = next(addresses)
        return [(2, 1, 6, "", (address, 443))]

    monkeypatch.setattr("househunter.secure_fetch.socket.getaddrinfo", resolved)
    from househunter.secure_fetch import validate_public_dns

    url = "https://data.example.test/source.json"
    baseline = validate_public_dns(url, label="Pinned source")
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"{}", request=request)
            )
        ) as client,
        pytest.raises(HouseHunterError, match="addresses changed"),
    ):
        request_bounded_bytes(
            client,
            url,
            allowed_hosts={"data.example.test"},
            max_bytes=10,
            expected_dns_addresses=baseline,
            label="Pinned source",
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


def test_qualification_golden_counties_lock_representative_formulas() -> None:
    path = Path(__file__).parents[1] / "config" / "ranking" / "golden-counties-v2.json"
    payload = json.loads(path.read_text())
    assert payload["methodology_id"] == METHODOLOGY_ID
    cases = {item["id"]: item for item in payload["cases"]}

    normal = cases["normal-current-county"]
    inputs = normal["inputs"]
    expected = normal["expected"]
    assert 1 - inputs["res_hazard_npctl"] / 100 == pytest.approx(expected["u_hazard"])
    safety = pillar_utility(
        {
            "hazard": expected["u_hazard"],
            "crime": inputs["u_crime"],
            "water": inputs["u_water"],
        },
        PILLAR_INTERNALS["safety"],
    )
    assert safety == pytest.approx(expected["u_safety"])
    healthcare = sum(inputs["provider_component_utilities"]) / 3
    context = (10 - inputs["community_context_group"]) / 9
    health = pillar_utility(
        {"healthcare": healthcare, "community_context": context},
        PILLAR_INTERNALS["health"],
    )
    affordability = pillar_utility(
        {
            "housing": inputs["u_housing"],
            "rpp": inputs["u_rpp"],
            "property_tax": inputs["u_property_tax"],
        },
        PILLAR_INTERNALS["affordability"],
    )
    opportunity = pillar_utility(
        {"employment": inputs["u_employment"], "broadband": inputs["u_broadband"]},
        PILLAR_INTERNALS["opportunity"],
    )
    assert healthcare == pytest.approx(expected["u_healthcare"])
    assert context == pytest.approx(expected["u_community_context"])
    assert health == pytest.approx(expected["u_health"])
    assert affordability == pytest.approx(expected["u_affordability"])
    assert opportunity == pytest.approx(expected["u_opportunity"])
    balanced = sum(
        value * weight
        for value, weight in zip(
            [safety, health, affordability, opportunity, inputs["u_lifestyle"], inputs["u_family"]],
            [0.20, 0.15, 0.25, 0.15, 0.15, 0.10],
            strict=True,
        )
    )
    assert balanced == pytest.approx(expected["balanced_fit"])

    tie = cases["average-tie-ecdf"]
    assert average_tie_percentile(tie["cohort"]) == tie["expected_higher_is_better"]
    assert average_tie_percentile(tie["cohort"], invert=True) == tie["expected_lower_is_better"]
    boundaries = cases["coverage-boundaries"]
    assert boundaries["inputs"]["crime_coverage_below"] < CRIME_COVERAGE_FLOOR
    assert boundaries["inputs"]["crime_coverage_at"] == CRIME_COVERAGE_FLOOR

    crime = cases["crime-person-years-and-coverage"]
    crime_inputs = crime["inputs"]
    crime_expected = crime["expected"]
    violent_coverage = [
        numerator / denominator
        for numerator, denominator in zip(
            crime_inputs["violent_participated_population_month_sums"],
            crime_inputs["violent_eligible_population_month_sums"],
            strict=True,
        )
    ]
    property_coverage = [
        numerator / denominator
        for numerator, denominator in zip(
            crime_inputs["property_participated_population_month_sums"],
            crime_inputs["property_eligible_population_month_sums"],
            strict=True,
        )
    ]
    violent_person_years = sum(crime_inputs["violent_participated_population_month_sums"]) / 12
    property_person_years = sum(crime_inputs["property_participated_population_month_sums"]) / 12
    assert violent_coverage == pytest.approx(crime_expected["violent_annual_coverage"])
    assert property_coverage == pytest.approx(crime_expected["property_annual_coverage"])
    assert violent_person_years == pytest.approx(crime_expected["violent_person_years"])
    assert property_person_years == pytest.approx(crime_expected["property_person_years"])
    assert sum(crime_inputs["violent_offenses"]) / violent_person_years * 100_000 == pytest.approx(
        crime_expected["violent_rate"]
    )
    property_rate = sum(crime_inputs["property_offenses"]) / property_person_years * 100_000
    assert property_rate == pytest.approx(crime_expected["property_rate"])
    assert all(value >= CRIME_COVERAGE_FLOOR for value in violent_coverage)
    assert all(value >= CRIME_COVERAGE_FLOOR for value in property_coverage)

    water = cases["water-population-allocation"]
    water_inputs = water["inputs"]
    water_expected = water["expected"]
    allocated = [
        system["reported_population"]
        * system["county_block_population"]
        / system["system_block_population"]
        for system in water_inputs["systems"]
    ]
    violating = sum(
        value
        for value, system in zip(allocated, water_inputs["systems"], strict=True)
        if system["violating"]
    )
    assert allocated == pytest.approx(
        [water_expected["system_a_allocated"], water_expected["system_b_allocated"]]
    )
    allocation_denominator = (
        water_inputs["allocatable_population"]
        + water_inputs["unallocatable_single_county_population"]
    )
    assert water_inputs["allocatable_population"] / allocation_denominator == pytest.approx(
        water_expected["allocation_coverage"]
    )
    assert water_inputs["covered_max_intersection_population_proxy"] / water_inputs[
        "county_2020_estimates_base_population"
    ] == pytest.approx(water_expected["public_water_coverage_proxy"])
    violation_share = violating / sum(allocated)
    assert violation_share == pytest.approx(water_expected["water_violation_share"])
    assert 1 - violation_share == pytest.approx(water_expected["u_water"])

    raw = cases["raw-provider-tax-employment-broadband"]
    raw_inputs = raw["inputs"]
    raw_expected = raw["expected"]
    for field, expected_field in (
        ("primary_care_count", "primary_care_per_100k"),
        ("mental_health_count", "mental_health_per_100k"),
        ("dental_count", "dental_per_100k"),
    ):
        assert raw_inputs[field] / raw_inputs["population"] * 100_000 == pytest.approx(
            raw_expected[expected_field]
        )
    assert raw_inputs["property_tax"] / raw_inputs["property_value"] == pytest.approx(
        raw_expected["property_tax_rate"]
    )
    assert (raw_inputs["qcew_employment_2025"] - raw_inputs["qcew_employment_2024"]) / raw_inputs[
        "qcew_employment_2024"
    ] == pytest.approx(raw_expected["qcew_growth"])
    assert raw_inputs["commute_under_30"] / raw_inputs["non_home_workers"] == pytest.approx(
        raw_expected["commute_under_30_share"]
    )
    growth, wage, commute = raw_inputs["employment_component_utilities"]
    assert 0.50 * growth + 0.25 * wage + 0.25 * commute == pytest.approx(
        raw_expected["u_employment"]
    )
    residential = [row for row in raw_inputs["fcc_rows"] if row["biz_res"] == "R"]
    assert len(residential) == 1
    assert residential[0]["speed_100_20"] == pytest.approx(raw_expected["u_broadband"])

    served_areas = cases["water-served-area-attribution"]
    systems = {row["pwsid"]: row for row in served_areas["inputs"]["systems"]}
    county_rows = [
        row
        for row in served_areas["inputs"]["geographic_rows"]
        if systems[row["pwsid"]]["submission_status"] == "Y"
        and systems[row["pwsid"]]["activity"] == "A"
        and systems[row["pwsid"]]["type"] == "CWS"
        and row["area_type"] == "CN"
    ]
    valid_rows = [
        row
        for row in county_rows
        if systems[row["pwsid"]]["primacy_agency_code"] == row["pwsid"][:2] == "AL"
    ]
    attributed: dict[str, list[str]] = {}
    for row in valid_rows:
        attributed.setdefault(row["pwsid"], []).append("01" + row["ansi_entity_code"])
    assert {"AL0000001": attributed["AL0000001"]} == served_areas["expected"]["single_county"]
    assert {"AL0000002": attributed["AL0000002"]} == served_areas["expected"]["multi_county"]
    ignored = served_areas["expected"]["non_county_area_ignored"]
    assert all(row["pwsid"] != ignored for row in county_rows)
    non_postal = next(row for row in county_rows if row["pwsid"] == "090000004")
    assert not systems[non_postal["pwsid"]]["primacy_agency_code"].isalpha()
    assert served_areas["expected"]["non_postal_without_boundary"] == {
        "090000004": "unassignable_served_state"
    }
    mismatch = next(row for row in county_rows if row["pwsid"] == "AL0000005")
    assert systems[mismatch["pwsid"]]["primacy_agency_code"] != mismatch["pwsid"][:2]
    assert served_areas["expected"]["primacy_prefix_mismatch"] == {
        "AL0000005": "publication-failure"
    }
    retired = next(row for row in county_rows if row["pwsid"] == "AK0000006")
    retired_fips = "02" + retired["ansi_entity_code"]
    assert served_areas["expected"]["retired_county_without_boundary"] == {
        "AK0000006": {
            "candidate_fips": retired_fips,
            "status": "unassignable_legacy_county",
        }
    }

    retail = cases["water-retail-wholesale-classification"]
    included: list[str] = []
    wholesale_only: list[str] = []
    unknown: list[str] = []
    for system in retail["inputs"]["systems"]:
        codes = set(system["service_area_codes"])
        if system["is_wholesaler"] == "N":
            included.append(system["pwsid"])
        elif not codes:
            unknown.append(system["pwsid"])
        elif codes == {"WH"}:
            wholesale_only.append(system["pwsid"])
        else:
            included.append(system["pwsid"])
    assert included == retail["expected"]["included"]
    assert wholesale_only == retail["expected"]["wholesale_only"]
    assert unknown == retail["expected"]["unknown_wholesale_only"]
    unknown_mass = sum(
        system["reported_population"]
        for system in retail["inputs"]["systems"]
        if system["pwsid"] in unknown and len(system["served_counties"]) == 1
    )
    assert unknown_mass == retail["expected"]["unknown_single_county_coverage_denominator_mass"]

    overlap = cases["water-block-overlap-proxy"]
    intersections: dict[str, list[float]] = {}
    for row in overlap["inputs"]["block_intersections"]:
        intersections.setdefault(row["geoid20"], []).append(row["pop20_aw"])
    covered = sum(max(values) for values in intersections.values())
    total_intersection = sum(sum(values) for values in intersections.values())
    duplicate = total_intersection - covered
    assert covered == pytest.approx(overlap["expected"]["covered_population_proxy"])
    assert covered / overlap["inputs"]["county_2020_estimates_base_population"] == pytest.approx(
        overlap["expected"]["public_water_coverage_proxy"]
    )
    assert duplicate == pytest.approx(overlap["expected"]["overlap_duplicate_population_proxy"])
    assert duplicate / total_intersection == pytest.approx(
        overlap["expected"]["overlap_duplicate_share_proxy"]
    )
    assert overlap["expected"]["status"] == "max_intersection_proxy"

    climate = cases["climate-complete-stations"]
    for field in (
        "jan_avg_temp_f",
        "jul_avg_temp_f",
        "extreme_heat_days",
        "extreme_cold_days",
    ):
        values = [station[field] for station in climate["inputs"]["stations"]]
        assert sum(values) / len(values) == pytest.approx(climate["expected"][field])
    assert len(climate["inputs"]["stations"]) == climate["expected"]["station_count"]

    missing_climate = cases["climate-missing-element"]
    qualifying = [
        station
        for station in missing_climate["inputs"]["stations"]
        if all(value is not None for value in station.values())
    ]
    assert len(qualifying) == missing_climate["expected"]["station_count"] == 0
    assert all(
        missing_climate["expected"][field] is None
        for field in (
            "jan_avg_temp_f",
            "jul_avg_temp_f",
            "extreme_heat_days",
            "extreme_cold_days",
        )
    )

    nulls = cases["null-suppression-and-join-failures"]
    assert nulls["inputs"]["suppressed_property_tax"] is None
    assert len(set(nulls["inputs"]["duplicate_county_fips"])) != len(
        nulls["inputs"]["duplicate_county_fips"]
    )
    assert (
        nulls["inputs"]["multiplying_join_result_rows"]
        > nulls["inputs"]["multiplying_join_left_rows"]
    )
    geography = cases["geography-rejections"]
    assert geography["county_fips"] == "09110"
    assert geography["expected"]["legacy_connecticut_fips"] == "09001"
    assert geography["expected"]["territory_state_fips"] == ["60", "66", "69", "72", "78"]


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
    drifted_coverage = dict(bundle.manifest)
    drifted_coverage["coverage"] = {
        **bundle.manifest["coverage"],
        "complete_public_core_count": 0,
    }
    assert ranking_bundle_identity(drifted_coverage) != baseline


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
