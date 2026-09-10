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
        assert meta["reference_assets_error"] is None
        response = client.get("/api/v1/places", params={"state": "AL", "min_population": 300})
        assert response.status_code == 200
        assert [row["name"] for row in response.json()["items"]] == ["Alpha"]
        summary = client.get("/api/v1/places/0100001").json()["summary"]
        assert summary["risk_score"] == 22.0
        assert summary["population_2020"] == 1000
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
        }
        sources = client.get("/api/v1/sources").json()
        assert [source["source"] for source in sources] == ["fema", "census_2020"]
        unmatched = client.get("/api/v1/places/0200002")
        assert unmatched.status_code == 200
        assert unmatched.json()["tract_contributions"][0]["tract_id"] is None
        assert client.get("/api/v1/exports/places.parquet").status_code == 200
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
