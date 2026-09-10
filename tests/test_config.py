from __future__ import annotations

from pathlib import Path

import pytest

from househunter.config import default_config_path


def test_default_config_does_not_trust_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    untrusted = tmp_path / "config"
    untrusted.mkdir()
    (untrusted / "sources.yml").write_text("schema_version: 1\n")
    monkeypatch.delenv("HOUSEHUNTER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_config_path() != untrusted / "sources.yml"
