from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
import yaml

from househunter.config import RuntimePaths


@pytest.fixture
def fixture_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RuntimePaths, Path]:
    config = {
        "schema_version": 1,
        "fema": {
            "name": "fixture",
            "item_id": "fixture",
            "layer_url": "https://example.test/FeatureServer/0",
            "item_url": "https://example.test/item",
            "terms_url": "https://example.test/terms",
            "version": "December 2025",
            "release": "v1.20",
            "item_modified_ms": 1,
            "data_last_edit_ms": 1,
            "layer_last_edit_ms": 2,
            "expected_row_count": 5,
            "fields": {
                "TRACTFIPS": "esriFieldTypeString",
                "ALR_NPCTL": "esriFieldTypeDouble",
                "NRI_VER": "esriFieldTypeString",
            },
            "schema_fingerprint": (
                "fdbbc3313928b19a8334cf0883667b996c64e5fc33b956dc42bbff55adf7723e"
            ),
            "canonical_sha256": None,
        },
        "fema_counties": {
            "name": "fixture counties",
            "item_id": "fixture-counties",
            "layer_url": "https://example.test/counties/0",
            "item_url": "https://example.test/counties",
            "terms_url": "https://example.test/terms",
            "version": "December 2025",
            "release": "v1.20",
            "item_modified_ms": 1,
            "data_last_edit_ms": 1,
            "layer_last_edit_ms": 2,
            "expected_row_count": 2,
            "fields": {
                "STCOFIPS": "esriFieldTypeString",
                "COUNTY": "esriFieldTypeString",
                "COUNTYTYPE": "esriFieldTypeString",
                "STATEABBRV": "esriFieldTypeString",
                "ALR_NPCTL": "esriFieldTypeDouble",
                "NRI_VER": "esriFieldTypeString",
            },
            "schema_fingerprint": (
                "de3fb9c4dd2b2d7f507f908fce69ab95fc7f20bcfc155e93851a9e2ed85767f2"
            ),
            "canonical_sha256": None,
        },
    }
    config_path = tmp_path / "sources.yml"
    config_path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("HOUSEHUNTER_CONFIG", str(config_path))
    monkeypatch.setenv("HOUSEHUNTER_DATA_DIR", str(tmp_path / "data"))
    paths = RuntimePaths.from_root(tmp_path)
    paths.ensure()
    fema = pl.DataFrame(
        {
            "tract_id": [
                "01001000100",
                "01001000200",
                "01001000300",
                "02001000100",
                "99999999999",
            ],
            "alr_npctl": [10.0, 50.0, 80.0, 25.0, 99.0],
            "nri_version": ["December 2025"] * 5,
        }
    )
    fema.write_parquet(paths.cache / "fema_nri_tracts.parquet")
    counties = pl.DataFrame(
        {
            "county_fips": ["01001", "02001"],
            "county": ["Autauga", "Aleutians East"],
            "county_type": ["County", "Borough"],
            "state": ["AL", "AK"],
            "alr_npctl": [40.0, 12.0],
            "nri_version": ["December 2025", "December 2025"],
        }
    )
    counties.write_parquet(paths.cache / "fema_nri_counties.parquet")
    return paths, tmp_path
