from __future__ import annotations

import json

import pytest

from househunter.build import BUILD_SCHEMA_VERSION, build_snapshot
from househunter.config import RuntimePaths
from househunter.errors import HouseHunterError
from househunter.store import Store


def test_build_is_content_addressed_and_queryable(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    first = build_snapshot(paths)
    first_metadata = json.loads((first / "build.json").read_text())
    second = build_snapshot(paths)
    second_metadata = json.loads((second / "build.json").read_text())
    assert first == second
    assert first_metadata["schema_version"] == BUILD_SCHEMA_VERSION
    assert first_metadata["logical_checksums"] == second_metadata["logical_checksums"]
    with Store(paths) as store:
        rows = store.list_places(limit=10)
        assert [item["place_id"] for item in rows["items"]] == [
            "01001000100",
            "02001000100",
            "01001000200",
            "01001000300",
            "99999999999",
        ]
        assert rows["items"][0]["risk_score"] == 10.0
        assert rows["items"][0]["state"] == "AL"
        assert rows["items"][0]["county_fips"] == "01001"
        assert rows["items"][0]["county_name"] == "Autauga"
        detail = store.place_detail("01001000100")
        assert detail["coverage_ratio"] == 1.0
        assert detail["tract_contributions"][0]["fema_percentile"] == 10.0
        counties = store.list_counties(limit=10)
        assert [item["place_id"] for item in counties["items"]] == ["02001", "01001"]
        assert counties["items"][0]["risk_score"] == 12.0
        assert counties["items"][0]["name"] == "Aleutians East Borough"
        assert counties["items"][1]["risk_score"] == 40.0
        assert counties["items"][1]["risk_score"] != pytest.approx((10.0 + 50.0 + 80.0) / 3)
        filtered = store.list_places(county="01001")
        assert [item["place_id"] for item in filtered["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        named = store.list_places(search="Autauga")
        assert {item["place_id"] for item in named["items"]} == {
            "01001000100",
            "01001000200",
            "01001000300",
        }


def test_state_build_records_scope(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths, state="AL")
    metadata = json.loads((output / "build.json").read_text())
    assert metadata["scope"] == {"kind": "state", "state": "AL"}
    assert metadata["place_count"] == 3
    assert metadata["county_count"] == 1
    with Store(paths) as store:
        assert all(item["state"] == "AL" for item in store.list_places(limit=10)["items"])
        assert [item["place_id"] for item in store.list_counties()["items"]] == ["01001"]


def test_unknown_fips_prefix_is_retained(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with Store(paths) as store:
        detail = store.place_detail("99999999999")
        assert detail["summary"]["state"] == "??"
        assert detail["summary"]["risk_score"] == 99.0
        assert detail["summary"]["county_fips"] == "??"
        assert detail["summary"]["county_name"] == "Unknown"


def test_failed_build_preserves_current_pointer(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    published = build_snapshot(paths)
    pointer = paths.current.read_text()
    (paths.cache / "fema_nri_tracts.parquet").unlink()
    with pytest.raises(HouseHunterError, match="FEMA data is not cached"):
        build_snapshot(paths)
    assert paths.current.read_text() == pointer
    assert published.is_dir()


def test_missing_county_cache_fails_build(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    (paths.cache / "fema_nri_counties.parquet").unlink()
    with pytest.raises(HouseHunterError, match="FEMA county data is not cached"):
        build_snapshot(paths)


def test_existing_build_rejects_a_corrupt_database(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    (output / "househunter.duckdb").write_bytes(b"not a database")
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)


def test_export_cannot_overwrite_managed_data(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    protected = output / "places.parquet"
    before = protected.read_bytes()
    with Store(paths) as store, pytest.raises(HouseHunterError, match="managed HouseHunter"):
        store.export("csv", protected)
    assert protected.read_bytes() == before


def test_legacy_build_schema_is_not_reused(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    metadata_path = output / "build.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["schema_version"] = 2
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)


def test_schema_3_build_is_not_reused(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    metadata_path = output / "build.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["schema_version"] = 3
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)


def test_schema_4_build_is_not_reused(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    output = build_snapshot(paths)
    metadata_path = output / "build.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["schema_version"] = 4
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(HouseHunterError, match="immutable build failed validation"):
        build_snapshot(paths)


def test_unknown_state_is_rejected(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with pytest.raises(HouseHunterError, match="Unknown state abbreviation"):
        build_snapshot(paths, state="ZZ")
