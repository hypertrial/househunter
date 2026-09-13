from __future__ import annotations

from pathlib import Path

import pytest

from househunter.config import default_config_path, load_config


def test_default_config_does_not_trust_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    untrusted = tmp_path / "config"
    untrusted.mkdir()
    (untrusted / "sources.yml").write_text("schema_version: 1\n")
    monkeypatch.delenv("HOUSEHUNTER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_config_path() != untrusted / "sources.yml"


def test_chrr_source_is_fully_pinned() -> None:
    source = load_config()["chrr"]
    assert source["release_year"] == 2025
    assert source["expected_row_count"] == 3144
    assert source["canonical_sha256"] == (
        "516e1e1408fe3dbb65273c9de75c67cfd6d5eed15f1ad18a8e91e6c6d54ca3fb"
    )
    assert source["item_url"].endswith("fed47aeb4d334339a73e20088181e544&sublayer=2")
    assert source["fields"]["CommunityConditions_Group"] == "esriFieldTypeInteger"


def test_fema_sources_are_content_pinned() -> None:
    config = load_config()
    assert config["fema"]["canonical_sha256"] == (
        "7b12ca2f292c658a92cc4c9bc39eef5e11b606fa662a8280aa830b1624754bc6"
    )
    assert config["fema_counties"]["canonical_sha256"] == (
        "9f9be9b86fe1ae0b0637728f0867432367285dd8dfd7bc7badddb3b2b2c6a23a"
    )
