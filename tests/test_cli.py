from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_build_store import _install_dimension_fixture, _promote_mountain_fixture
from typer.testing import CliRunner

import househunter.cli as cli_module
from househunter.cli import app
from househunter.config import RuntimePaths
from househunter.errors import HouseHunterError
from househunter.top_counties import PREFERENCE_NOTICE, PRESETS


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


def test_residential_hazard_filters_use_documented_cli_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeStore:
        def __init__(self, paths: RuntimePaths) -> None:
            pass

        def __enter__(self) -> FakeStore:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def list_places(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"items": []}

    monkeypatch.setattr(cli_module, "Store", FakeStore)
    result = CliRunner().invoke(
        app, ["rank", "--res-hazard-min", "10", "--res-hazard-max", "90"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["min_res_hazard"] == 10
    assert calls[0]["max_res_hazard"] == 90


@pytest.mark.parametrize(
    ("metric", "expected_sort", "best_direction", "worst_direction"),
    [
        ("cost-of-living", "cost_of_living_index", "asc", "desc"),
        ("home-costs", "home_sqft_for_1m", "desc", "asc"),
    ],
)
def test_new_dimension_rank_directions_and_cross_filters(
    monkeypatch: pytest.MonkeyPatch,
    metric: str,
    expected_sort: str,
    best_direction: str,
    worst_direction: str,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeStore:
        def __init__(self, paths: RuntimePaths) -> None:
            pass

        def __enter__(self) -> FakeStore:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def list_places(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"items": []}

    monkeypatch.setattr(cli_module, "Store", FakeStore)
    runner = CliRunner()
    filters = [
        "--max-community-conditions-group",
        "3",
        "--cost-of-living-index-min",
        "80",
        "--cost-of-living-index-max",
        "120",
        "--home-sqft-for-1m-min",
        "1000",
        "--home-sqft-for-1m-max",
        "5000",
        "--housing-built-2000-plus-pct-min",
        "20",
        "--housing-built-2000-plus-pct-max",
        "80",
    ]

    best = runner.invoke(app, ["rank", "--metric", metric, "--order", "best", *filters])
    worst = runner.invoke(app, ["rank", "--metric", metric, "--order", "worst"])

    assert best.exit_code == 0, best.output
    assert worst.exit_code == 0, worst.output
    assert calls[0]["sort"] == calls[1]["sort"] == expected_sort
    assert [calls[0]["direction"], calls[1]["direction"]] == [
        best_direction,
        worst_direction,
    ]
    assert calls[0]["max_community_conditions_group"] == 3
    assert calls[0]["cost_of_living_index_min"] == 80
    assert calls[0]["home_sqft_for_1m_max"] == 5000
    assert calls[0]["housing_built_2000_plus_pct_min"] == 20


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
    assert "0.0" in ranked.output

    counties = runner.invoke(app, ["rank", "--level", "county", "--state", "AL"])
    assert counties.exit_code == 0
    assert "01001" in counties.output
    assert "100.0" in counties.output

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

    cost = runner.invoke(
        app,
        ["rank", "--metric", "cost-of-living", "--state", "AL", "--include-unranked"],
    )
    home = runner.invoke(
        app,
        ["rank", "--metric", "home-costs", "--state", "AL", "--include-unranked"],
    )
    assert cost.exit_code == 0, cost.output
    assert home.exit_code == 0, home.output
    assert "RPP" in cost.output
    assert "SQFT/$1M" in home.output

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
    assert tract_hazards["WFIR"] == 0.0
    assert tract_hazards["TSUN"] == 0.0

    inspected_county = runner.invoke(app, ["inspect", "01001"])
    assert inspected_county.exit_code == 0
    county_detail = json.loads(inspected_county.output)
    assert county_detail["summary"]["place_id"] == "01001"
    assert county_detail["member_tract_count"] == 3
    county_hazards = {
        item["code"]: item["percentile"] for item in county_detail["hazard_percentiles"]
    }
    assert county_hazards["WFIR"] == 100.0

    output = tmp_path / "places.csv"
    exported = runner.invoke(app, ["export", "--format", "csv", "--output", str(output)])
    assert exported.exit_code == 0
    header = output.read_text().splitlines()[0]
    assert header.startswith("place_id,")
    assert "RES_HAZARD_NPCTL" in header
    assert "RES_HAZARD_SPREAD" in header
    assert "PROPERTY_LOSS_NPCTL" in header
    assert "RES_HAZARD_SPECTRAL" in header
    assert "RES_HAZARD_TAIL" in header
    assert "RES_HAZARD_POWER4" in header
    assert "WFIR_ALRB_NPCTL" in header
    assert "TSUN_ALRB_NPCTL" in header
    assert "ALR_NPCTL" in header
    assert "ALR_VALB" in header
    assert "community_conditions_group" in header
    assert "community_conditions_geography" in header
    assert "chrr_release_year" in header
    assert "cost_of_living_index" in header
    assert "home_sqft_for_1m" in header
    assert "home_market_usage_notice" in header
    assert "housing_built_2000_plus_pct" in header

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


def _complete_national_snapshot(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[RuntimePaths, Path, CliRunner]:
    paths, root = fixture_environment
    _install_dimension_fixture(monkeypatch)
    _promote_mountain_fixture(paths, root)
    runner = CliRunner()
    built = runner.invoke(app, ["build"])
    assert built.exit_code == 0, built.output
    return paths, root, runner


def test_top_counties_requires_published_snapshot(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    result = CliRunner().invoke(app, ["top-counties"])
    assert result.exit_code == 1, result.output
    assert "No published build" in result.output


def test_top_counties_requires_available_optional_layers(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    _, _ = fixture_environment
    runner = CliRunner()
    built = runner.invoke(app, ["build"])
    assert built.exit_code == 0, built.output
    missing = runner.invoke(app, ["top-counties"])
    assert missing.exit_code == 1, missing.output
    assert "all five layers available" in missing.output
    assert "mountain" in missing.output
    assert "cost-of-living" in missing.output
    assert "home-costs" in missing.output
    assert "RANK" not in missing.output

    ranked = runner.invoke(app, ["rank", "--level", "county", "--limit", "2"])
    assert ranked.exit_code == 0, ranked.output
    assert "COUNTY_FIPS" in ranked.output
    assert "preference_fit" not in ranked.output
    assert "PRESET" not in ranked.output


def test_top_counties_rejects_state_scoped_snapshots(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, root = fixture_environment
    _install_dimension_fixture(monkeypatch)
    _promote_mountain_fixture(paths, root)
    runner = CliRunner()
    built = runner.invoke(app, ["build", "--state", "AL"])
    assert built.exit_code == 0, built.output
    result = runner.invoke(app, ["top-counties"])
    assert result.exit_code == 1, result.output
    assert "national snapshot" in result.output

    ranked = runner.invoke(app, ["rank", "--level", "county", "--state", "AL"])
    assert ranked.exit_code == 0, ranked.output
    assert "01001" in ranked.output
    assert "preference_fit" not in ranked.output


def test_top_counties_rejects_unknown_preset(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _complete_national_snapshot(fixture_environment, monkeypatch)
    result = CliRunner().invoke(app, ["top-counties", "--preset", "livability"])
    assert result.exit_code == 1, result.output
    assert "preset" in result.output


def test_top_counties_rejects_out_of_range_limit() -> None:
    runner = CliRunner()
    too_small = runner.invoke(app, ["top-counties", "--limit", "0"])
    too_large = runner.invoke(app, ["top-counties", "--limit", "501"])
    assert too_small.exit_code != 0
    assert too_large.exit_code != 0
    assert "RANK" not in too_small.output
    assert "RANK" not in too_large.output


def test_top_counties_ranks_complete_counties_for_each_preset(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, runner = _complete_national_snapshot(fixture_environment, monkeypatch)
    default = runner.invoke(app, ["top-counties"])
    assert default.exit_code == 0, default.output
    assert "BUILD  " in default.output
    assert "PRESET  balanced" in default.output
    assert "ELIGIBLE  2" in default.output
    assert "LIMIT  10" in default.output
    assert "PARETO" in default.output
    assert PREFERENCE_NOTICE in default.output
    assert "02001" in default.output
    assert default.output.index("02001") < default.output.index("01001")
    assert "HAZARD" in default.output
    assert "HOME%" in default.output

    cased = runner.invoke(app, ["top-counties", "--preset", "BALANCED", "--limit", "1"])
    assert cased.exit_code == 0, cased.output
    assert "PRESET  balanced" in cased.output
    assert "01001" not in cased.output

    for preset in PRESETS:
        ranked = runner.invoke(app, ["top-counties", "--preset", preset, "--limit", "1"])
        assert ranked.exit_code == 0, ranked.output
        assert f"PRESET  {preset}" in ranked.output
        assert "02001" in ranked.output
        assert "01001" not in ranked.output


def test_top_counties_json_includes_utilities_and_weights(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, runner = _complete_national_snapshot(fixture_environment, monkeypatch)
    result = runner.invoke(app, ["top-counties", "--json", "--limit", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["preset"] == "balanced"
    assert isinstance(payload["build_id"], str) and payload["build_id"]
    assert payload["weights"] == {
        "hazard": 0.2,
        "community": 0.2,
        "mountain": 0.2,
        "cost": 0.2,
        "home": 0.2,
    }
    assert payload["eligible_count"] == 2
    assert payload["notice"] == PREFERENCE_NOTICE
    assert payload["scope"]["kind"] == "national"
    assert [item["place_id"] for item in payload["items"]] == ["02001", "01001"]
    first = payload["items"][0]
    assert first["rank"] == 1
    assert first["pareto_optimal"] is True
    assert first["preference_fit"] > payload["items"][1]["preference_fit"]
    assert set(first["values"]) == {
        "res_hazard_npctl",
        "community_conditions_group",
        "mountain_magnitude",
        "cost_of_living_index",
        "home_buying_power_percentile",
    }
    assert "housing_built_2000_plus_pct" not in first["values"]
    assert "home_sqft_for_1m" not in first["values"]
    assert set(first["utilities"]) == {"hazard", "community", "mountain", "cost", "home"}
    assert all(value is not None for value in first["values"].values())
