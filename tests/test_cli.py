from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from househunter.cli import app
from househunter.config import RuntimePaths


def test_cli_build_rank_inspect_export_and_sources(
    fixture_environment: tuple[RuntimePaths, object], tmp_path: Path
) -> None:
    _, _ = fixture_environment
    runner = CliRunner()
    built = runner.invoke(app, ["build", "--state", "AL"])
    assert built.exit_code == 0, built.output

    ranked = runner.invoke(app, ["rank", "--state", "AL", "--limit", "2"])
    assert ranked.exit_code == 0
    assert "Alpha" in ranked.output
    assert "22.0" in ranked.output

    inspected = runner.invoke(app, ["inspect", "Alpha, AL"])
    assert inspected.exit_code == 0
    assert json.loads(inspected.output)["summary"]["place_id"] == "0100001"

    output = tmp_path / "places.csv"
    exported = runner.invoke(app, ["export", "--format", "csv", "--output", str(output)])
    assert exported.exit_code == 0
    assert output.read_text().startswith("place_id,")

    sources = runner.invoke(app, ["sources", "--json"])
    assert sources.exit_code == 0
    assert json.loads(sources.output)["fema"]["version"] == "December 2025"
