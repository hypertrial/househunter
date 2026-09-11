from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from househunter.cli import app
from househunter.config import RuntimePaths


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

    county_filter = runner.invoke(app, ["rank", "--county", "01001"])
    assert county_filter.exit_code == 0
    assert "02001000100" not in county_filter.output

    inspected = runner.invoke(app, ["inspect", "01001000100"])
    assert inspected.exit_code == 0
    assert json.loads(inspected.output)["summary"]["place_id"] == "01001000100"

    inspected_county = runner.invoke(app, ["inspect", "01001"])
    assert inspected_county.exit_code == 0
    assert json.loads(inspected_county.output)["summary"]["place_id"] == "01001"

    output = tmp_path / "places.csv"
    exported = runner.invoke(app, ["export", "--format", "csv", "--output", str(output)])
    assert exported.exit_code == 0
    assert output.read_text().startswith("place_id,")

    sources = runner.invoke(app, ["sources", "--json"])
    assert sources.exit_code == 0
    source_status = json.loads(sources.output)
    assert source_status["fema"]["version"] == "December 2025"
    assert source_status["fema_counties"]["version"] == "December 2025"
    assert "census" not in source_status
