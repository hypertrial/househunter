from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


def test_playwright_ci_uses_a_supported_macos_runner() -> None:
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text())
    package = json.loads((root / "web" / "package.json").read_text())
    playwright = package["devDependencies"]["@playwright/test"]
    playwright_version = tuple(map(int, re.search(r"(\d+)\.(\d+)", playwright).groups()))
    runner = workflow["jobs"]["test"]["runs-on"]
    macos_version = int(re.fullmatch(r"macos-(\d+)", runner).group(1))

    if playwright_version >= (1, 62):
        assert macos_version >= 15


def test_ci_actions_are_immutable_and_least_privilege() -> None:
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["test"]["steps"]
    actions = [step for step in steps if "uses" in step]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in actions)
    checkout = next(step for step in actions if step["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
