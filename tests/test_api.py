from __future__ import annotations

import time

from fastapi.testclient import TestClient

from househunter.api import create_app
from househunter.build import build_snapshot
from househunter.config import RuntimePaths


def test_api_filters_details_exports_and_token(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    build_snapshot(paths)
    with TestClient(create_app(paths, testing=True)) as client:
        assert "HouseHunter" in client.get("/").text
        meta_response = client.get("/api/v1/meta")
        assert meta_response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in meta_response.headers["content-security-policy"]
        meta = meta_response.json()
        assert meta["reference_assets_ready"] is True
        assert meta["methodology"] == "FEMA tract-level ALR_NPCTL"
        response = client.get("/api/v1/places", params={"state": "AL"})
        assert response.status_code == 200
        assert [row["place_id"] for row in response.json()["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        summary = client.get("/api/v1/places/01001000100").json()["summary"]
        assert summary["risk_score"] == 10.0
        assert summary["place_type"] == "tract"
        assert set(summary) == {
            "place_id",
            "name",
            "state",
            "place_type",
            "population_2020",
            "housing_units_2020",
            "risk_score",
            "coverage_status",
            "fema_vintage",
            "census_vintage",
            "county_fips",
            "county_name",
        }
        assert "alr_npctl_wfir" not in client.get("/api/v1/places", params={"state": "AL"}).json()[
            "items"
        ][0]
        tract_detail = client.get("/api/v1/places/01001000100").json()
        hazards = {item["code"]: item for item in tract_detail["hazard_percentiles"]}
        assert len(tract_detail["hazard_percentiles"]) == 18
        assert hazards["WFIR"]["label"] == "Wildfire"
        assert hazards["WFIR"]["percentile"] == 8.0
        assert hazards["TSUN"]["percentile"] is None
        assert tract_detail["member_tract_count"] is None
        assert summary["county_fips"] == "01001"
        assert summary["county_name"] == "Autauga"
        sources = client.get("/api/v1/sources").json()
        assert [source["source"] for source in sources] == ["fema", "fema_counties"]
        unknown = client.get("/api/v1/places/99999999999")
        assert unknown.status_code == 200
        assert unknown.json()["summary"]["state"] == "??"
        assert unknown.json()["summary"]["county_fips"] == "??"
        county_rows = client.get("/api/v1/counties", params={"state": "AL"}).json()
        assert [row["place_id"] for row in county_rows["items"]] == ["01001"]
        assert county_rows["items"][0]["risk_score"] == 40.0
        county_detail = client.get("/api/v1/counties/01001").json()
        assert "ranked among counties" in county_detail["methodology_notice"]
        assert county_detail["summary"]["risk_score"] == 40.0
        assert county_detail["member_tract_count"] == 3
        county_hazards = {item["code"]: item for item in county_detail["hazard_percentiles"]}
        assert county_hazards["WFIR"]["percentile"] == 9.0
        assert county_hazards["TSUN"]["percentile"] is None
        filtered = client.get("/api/v1/places", params={"county": "01001"}).json()
        assert [row["place_id"] for row in filtered["items"]] == [
            "01001000100",
            "01001000200",
            "01001000300",
        ]
        assert client.get("/api/v1/places", params={"county": "0100"}).status_code == 400
        assert client.get("/api/v1/places", params={"county": ""}).status_code == 200
        assert client.get("/api/v1/exports/places.parquet").status_code == 200
        assert client.get("/api/v1/exports/counties.parquet").status_code == 200
        place_header = client.get("/api/v1/exports/places.csv").text.splitlines()[0]
        county_header = client.get("/api/v1/exports/counties.csv").text.splitlines()[0]
        assert "alr_npctl_wfir" in place_header
        assert "alr_npctl_tsun" in place_header
        assert "alr_npctl_wfir" in county_header
        assert client.post("/api/v1/jobs", json={"kind": "build"}).status_code == 403
        accepted = client.post(
            "/api/v1/jobs",
            headers={"X-HouseHunter-Token": meta["mutation_token"]},
            json={"kind": "build", "state": "AL"},
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        for _ in range(100):
            status = client.get(f"/api/v1/jobs/{job_id}").json()
            if status["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert status["state"] == "succeeded"


def test_api_rejects_hostile_origin(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths, testing=True)) as client:
        response = client.get(
            "/api/v1/meta",
            headers={"origin": "https://attacker.example"},
        )
        assert response.status_code == 403


def test_api_rejects_non_loopback_host(fixture_environment: tuple[RuntimePaths, object]) -> None:
    paths, _ = fixture_environment
    with TestClient(create_app(paths)) as client:
        response = client.get("/api/v1/meta", headers={"host": "attacker.example"})
        assert response.status_code == 400
        malformed = client.get("/api/v1/meta", headers={"host": "localhost:80@attacker.example"})
        assert malformed.status_code == 400
