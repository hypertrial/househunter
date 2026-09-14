from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import househunter.cli as cli_module
from househunter.cli import app
from househunter.config import RuntimePaths
from househunter.errors import HouseHunterError


def test_mountain_rank_uses_high_scores_for_best_and_low_scores_for_worst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directions: list[str] = []

    class FakeStore:
        def __init__(self, paths: RuntimePaths) -> None:
            pass

        def __enter__(self) -> FakeStore:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def list_places(self, **kwargs: object) -> dict[str, object]:
            directions.append(str(kwargs["direction"]))
            return {"items": []}

    monkeypatch.setattr(cli_module, "Store", FakeStore)
    runner = CliRunner()

    best = runner.invoke(app, ["rank", "--metric", "mountain", "--order", "best"])
    worst = runner.invoke(app, ["rank", "--metric", "mountain", "--order", "worst"])

    assert best.exit_code == 0, best.output
    assert worst.exit_code == 0, worst.output
    assert directions == ["desc", "asc"]


def test_mountain_snapshot_rebuild_restores_previous_pointer_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_root(tmp_path)
    pointer = paths.data / "mountain" / "current.json"
    pointer.parent.mkdir(parents=True)
    previous = b'{"release_id":"previous"}\n'
    pointer.write_bytes(b'{"release_id":"candidate"}\n')
    previous_snapshot = b'{"build_id":"previous"}\n'
    paths.current.write_bytes(previous_snapshot)

    def publish_compact(*args: object, **kwargs: object) -> Path:
        compact_pointer = paths.data / "mountain" / "compact" / "current.json"
        compact_pointer.parent.mkdir(parents=True)
        compact_pointer.write_bytes(b'{"release_id":"candidate"}\n')
        return compact_pointer.parent / "candidate"

    def fail_build(*args: object, **kwargs: object) -> Path:
        paths.current.write_bytes(b'{"build_id":"candidate"}\n')
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr("househunter.mountain.write_and_promote_compact_fallback", publish_compact)
    monkeypatch.setattr(cli_module, "build_snapshot", fail_build)

    with pytest.raises(RuntimeError, match="snapshot failed"):
        cli_module._rebuild_after_mountain_promotion(
            paths,
            previous,
            tmp_path / "release",
            {"release_id": "candidate"},
        )

    assert pointer.read_bytes() == previous
    assert not (paths.data / "mountain" / "compact" / "current.json").exists()
    assert paths.current.read_bytes() == previous_snapshot


def test_mountain_publication_reports_cleanup_as_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = RuntimePaths.from_root(tmp_path)
    monkeypatch.setattr(
        "househunter.mountain.write_and_promote_compact_fallback",
        lambda *args, **kwargs: tmp_path / "compact",
    )
    monkeypatch.setattr(cli_module, "build_snapshot", lambda *args, **kwargs: tmp_path / "snapshot")
    monkeypatch.setattr(
        "househunter.mountain.prune_owned_releases",
        lambda *args, **kwargs: (_ for _ in ()).throw(HouseHunterError("disk busy")),
    )
    monkeypatch.setattr("househunter.mountain.prune_owned_compact_fallbacks", lambda *args: [])

    snapshot, compact, warnings = cli_module._rebuild_after_mountain_promotion(
        paths,
        None,
        tmp_path / "release",
        {"release_id": "candidate"},
    )

    assert snapshot == tmp_path / "snapshot"
    assert compact == tmp_path / "compact"
    assert warnings == ["Mountain full release cleanup pending: disk busy"]
    assert "cleanup pending" in capsys.readouterr().err


def test_cli_build_rank_inspect_export_and_sources(
    fixture_environment: tuple[RuntimePaths, Path], tmp_path: Path
) -> None:
    _, _ = fixture_environment
    runner = CliRunner()
    built = runner.invoke(app, ["build", "--state", "AL"])
    assert built.exit_code == 0, built.output

    ranked = runner.invoke(app, ["rank", "--state", "AL", "--limit", "2"])
    assert ranked.exit_code == 0
    assert "01001000100" in ranked.output
    assert "10.0" in ranked.output

    counties = runner.invoke(app, ["rank", "--level", "county", "--state", "AL"])
    assert counties.exit_code == 0
    assert "01001" in counties.output
    assert "40.0" in counties.output

    community = runner.invoke(
        app,
        [
            "rank",
            "--level",
            "county",
            "--metric",
            "community-conditions",
            "--order",
            "worst",
        ],
    )
    assert community.exit_code == 0, community.output
    assert "GROUP" in community.output
    assert "01001" in community.output
    assert "    5" in community.output

    mountain = runner.invoke(
        app,
        ["rank", "--metric", "mountain", "--state", "AL", "--include-unranked"],
    )
    assert mountain.exit_code == 0, mountain.output
    assert "MAGNITUDE" in mountain.output
    assert "01001000100" in mountain.output

    county_filter = runner.invoke(app, ["rank", "--county", "01001"])
    assert county_filter.exit_code == 0
    assert "02001000100" not in county_filter.output

    inspected = runner.invoke(app, ["inspect", "01001000100"])
    assert inspected.exit_code == 0
    tract_detail = json.loads(inspected.output)
    assert tract_detail["summary"]["place_id"] == "01001000100"
    assert tract_detail["member_tract_count"] is None
    tract_hazards = {
        item["code"]: item["percentile"] for item in tract_detail["hazard_percentiles"]
    }
    assert tract_hazards["WFIR"] == 8.0
    assert tract_hazards["TSUN"] is None

    inspected_county = runner.invoke(app, ["inspect", "01001"])
    assert inspected_county.exit_code == 0
    county_detail = json.loads(inspected_county.output)
    assert county_detail["summary"]["place_id"] == "01001"
    assert county_detail["member_tract_count"] == 3
    county_hazards = {
        item["code"]: item["percentile"] for item in county_detail["hazard_percentiles"]
    }
    assert county_hazards["WFIR"] == 9.0

    output = tmp_path / "places.csv"
    exported = runner.invoke(app, ["export", "--format", "csv", "--output", str(output)])
    assert exported.exit_code == 0
    header = output.read_text().splitlines()[0]
    assert header.startswith("place_id,")
    assert "alr_npctl_wfir" in header
    assert "alr_npctl_tsun" in header
    assert "community_conditions_group" in header
    assert "community_conditions_geography" in header
    assert "chrr_release_year" in header

    for level, expected_id in (("tract", "01001000100"), ("county", "01001")):
        json_output = tmp_path / f"{level}.json"
        exported = runner.invoke(
            app,
            [
                "export",
                "--format",
                "json",
                "--level",
                level,
                "--output",
                str(json_output),
            ],
        )
        assert exported.exit_code == 0, exported.output
        rows = json.loads(json_output.read_text())
        assert rows[0]["place_id"] == expected_id
        assert "community_conditions_group" in rows[0]
        assert rows[0]["community_conditions_geography"] == "county"
        assert rows[0]["chrr_release_year"] == 2025

    sources = runner.invoke(app, ["sources", "--json"])
    assert sources.exit_code == 0
    source_status = json.loads(sources.output)
    assert source_status["fema"]["version"] == "December 2025"
    assert source_status["fema_counties"]["version"] == "December 2025"
    assert source_status["chrr"]["version"] == "2025 Annual Data Release"
    assert "census" not in source_status


def test_cli_lookup_prints_tract_detail(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: object
) -> None:
    from househunter.geocode import AddressMatch

    _, _ = fixture_environment
    runner = CliRunner()
    built = runner.invoke(app, ["build"])
    assert built.exit_code == 0, built.output
    monkeypatch.setattr(
        "househunter.geocode.geocode_tract",
        lambda address, client=None: AddressMatch(
            query=address,
            matched_address="1 MAIN ST, AUTAUGA, AL, 36003",
            tract_id="01001000100",
        ),
    )
    looked = runner.invoke(app, ["lookup", "1 Main St, Autauga, AL"])
    assert looked.exit_code == 0, looked.output
    payload = json.loads(looked.output)
    assert payload["tract_id"] == "01001000100"
    assert payload["matched_address"] == "1 MAIN ST, AUTAUGA, AL, 36003"
    assert payload["detail"]["summary"]["place_id"] == "01001000100"
    assert payload["provider"] == "census"


def test_cli_lookup_requires_opt_in_for_street_matches(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: object
) -> None:
    _, _ = fixture_environment
    runner = CliRunner()
    built = runner.invoke(app, ["build"])
    assert built.exit_code == 0, built.output

    def fake_lookup(
        paths: object,
        address: str,
        *,
        allow_approximate: bool = False,
        candidate_id: str | None = None,
        client: object = None,
    ) -> dict[str, object]:
        if allow_approximate:
            return {
                "status": "resolved",
                "query": address,
                "matched_address": "Lazy Cat Lane, Monument, Colorado, United States",
                "tract_id": "01001000100",
                "detail": {"summary": {"place_id": "01001000100"}},
                "provider": "nominatim",
                "precision": "street",
                "approximate": True,
                "attribution": "© OpenStreetMap contributors",
            }
        return {
            "status": "confirmation_required",
            "query": address,
            "message": "Census has no street range for that address.",
            "attribution": "© OpenStreetMap contributors",
            "candidates": [
                {
                    "candidate_id": "abc",
                    "matched_address": "Lazy Cat Lane, Monument, Colorado, United States",
                    "precision": "street",
                }
            ],
        }

    monkeypatch.setattr("househunter.cli.lookup_address", fake_lookup)
    blocked = runner.invoke(app, ["lookup", "1720 Lazy Cat Ln, Monument, CO 80132"])
    assert blocked.exit_code == 2
    blocked_payload = json.loads(blocked.output)
    assert blocked_payload["status"] == "confirmation_required"
    assert blocked_payload["attribution"] == "© OpenStreetMap contributors"
    allowed = runner.invoke(
        app, ["lookup", "1720 Lazy Cat Ln, Monument, CO 80132", "--allow-approximate"]
    )
    assert allowed.exit_code == 0, allowed.output
    allowed_payload = json.loads(allowed.output)
    assert allowed_payload["provider"] == "nominatim"
    assert allowed_payload["precision"] == "street"
    assert allowed_payload["approximate"] is True
