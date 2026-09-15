from __future__ import annotations

import json
from pathlib import Path

import pytest

from househunter.config import RuntimePaths, default_config_path, load_config


def test_default_config_does_not_trust_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    untrusted = tmp_path / "config"
    untrusted.mkdir()
    (untrusted / "sources.yml").write_text("schema_version: 1\n")
    monkeypatch.delenv("HOUSEHUNTER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_config_path() != untrusted / "sources.yml"


def test_runtime_paths_tighten_existing_directory_permissions(tmp_path: Path) -> None:
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    paths.data.mkdir(mode=0o755, parents=True)
    paths.cache.mkdir(mode=0o755)
    paths.raw.mkdir(mode=0o755)
    paths.processed.mkdir(mode=0o755)
    paths.builds.mkdir(mode=0o755)

    paths.ensure()

    assert all(
        directory.stat().st_mode & 0o077 == 0
        for directory in (paths.data, paths.cache, paths.raw, paths.processed, paths.builds)
    )


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
        "1450dd1557c600f2b93824cbd52f83f9cdaa8640ec84d33c0e579e91f1b378ac"
    )
    assert config["fema_counties"]["canonical_sha256"] == (
        "8aa3ce57aaf8a934fda9fd55e7c9df1f4cba6dd2d813a524e1c7d1105a3d5b9a"
    )


def test_bea_rpp_source_is_content_and_schema_pinned() -> None:
    source = load_config()["bea_rpp"]
    assert source["release_year"] == 2024
    assert source["expected_size"] == 146479
    assert source["archive_sha256"] == (
        "5dbf2e6ac2af222cc9abc205586c9b480344d89392752eb689c3ec823a34c83e"
    )
    assert source["normalized_logical_sha256"] == (
        "fb8abcc48e12fd24e82be5725c6cbddb5e6443d87d00015c4814bdb7405f4aa9"
    )
    assert source["table_filename"] == "MARPP_MSA_2008_2024.csv"
    assert source["table_size"] == 455899
    assert source["header_sha256"] == (
        "2d08cb976140d2df0214af286d802de96341b74046bd48e2595b354367647427"
    )
    assert source["required_columns"][-1] == "2024"
    assert source["expected_msa_count"] == 387
    assert source["nonmetropolitan_geofips"] == "00999"
    assert source["line_codes"] == {
        "1": "All items",
        "2": "Goods",
        "3": "Services: Housing rents",
        "4": "Services: Utilities",
        "5": "Services: Other",
    }


def test_housing_stock_raw_sources_and_cbsa_derivation_are_pinned() -> None:
    root = Path(__file__).parents[1]
    lock = json.loads((root / "config/housing-stock/source-lock-2024.json").read_text())
    assert lock["release_year"] == 2024
    assert lock["omb_delineation"] == "OMB Bulletin No. 23-01"
    assert lock["sources"]["B25034"]["sha256"] == (
        "806d6cb18e8e6c0855e065eca99164b07334cc32f521899aa45a4415ef7d380e"
    )
    assert lock["sources"]["B25035"]["sha256"] == (
        "a08e68670c401abd470fc8084efc3718eba9b5cb52ff612c2442d67d5665971b"
    )
    geography = lock["county_cbsa_geography"]
    assert geography["source_table"] == "B25034"
    assert geography["summary_level"] == "313"
    assert geography["geo_id_prefix"] == "313M700US"
    assert geography["expected_relationship_rows"] == 1915
    assert geography["expected_cbsa_count"] == 935
    assert geography["expected_bea_msa_matches"] == 387


def test_home_market_release_lock_is_append_only_and_private_use() -> None:
    root = Path(__file__).parents[1]
    lock = json.loads((root / "config/home-market/release-lock.json").read_text())
    assert lock["automatic_download"] is False
    assert lock["private_use_only"] is True
    assert "personal local use" in lock["usage_notice"]
    assert len(lock["releases"]) == 1
    release = lock["releases"][0]
    assert release == {
        "month": "2026-08",
        "month_date_yyyymm": 202608,
        "expected_filename": "RDC_Inventory_Core_Metrics_County.csv",
        "byte_size": 895367,
        "sha256": "4997ef2054c11313d81eb3d5929699744423b92ee3cb1970ebc006dbfeac474e",
        "normalized_logical_sha256": (
            "3d219000570a8e9de5b23e85bfb476c07a53e620236ad5bab743e407b5a8ada2"
        ),
        "header_sha256": (
            "e5bdf06465e3b67e1b43e0209b1597f91434d699b5ad35be7fa8f4c51e6b35f9"
        ),
        "row_count": 3118,
        "quality_flagged_row_count": 845,
        "null_price_per_square_foot_count": 2,
        "source_page": "https://www.realtor.com/research/data/",
        "reviewed_on": "2026-09-14",
    }
