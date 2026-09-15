from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
import yaml

from househunter.config import RuntimePaths
from househunter.hazards import HAZARDS, INSUFFICIENT_DATA, NOT_APPLICABLE, with_hazard_columns


@pytest.fixture
def fixture_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RuntimePaths, Path]:
    config = {
        "schema_version": 1,
        "chrr": {
            "name": "fixture community conditions",
            "layer_url": "https://example.test/chrr/2",
            "item_url": "https://example.test/chrr",
            "terms_url": "https://example.test/terms",
            "version": "2025 Annual Data Release",
            "release": "2025",
            "release_year": 2025,
            "layer_last_edit_ms": 3,
            "schema_last_edit_ms": 2,
            "data_last_edit_ms": 1,
            "expected_row_count": 3,
            "fields": {
                "fipscode": "esriFieldTypeString",
                "county": "esriFieldTypeString",
                "state": "esriFieldTypeString",
                "CommunityConditions_Group": "esriFieldTypeInteger",
            },
            "schema_fingerprint": (
                "503000d3f66abcea0a8182c522e7365f7175cc4c39ad7a798c3765d885fdcdf3"
            ),
            "canonical_sha256": None,
        },
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
    monkeypatch.setattr(
        "househunter.mountain.BUNDLED_COMPACT_RELEASE",
        tmp_path / "no-synthetic-mountain-bundle",
    )
    paths = RuntimePaths.from_root(tmp_path)
    paths.ensure()
    chrr_raw = paths.raw / "chrr" / "community_conditions_2025.json"
    chrr_raw.parent.mkdir(parents=True)
    chrr_raw.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "fipscode": "01001",
                        "state": "AL",
                        "county": "Autauga County",
                        "CommunityConditions_Group": 5,
                    },
                    {
                        "fipscode": "02001",
                        "state": "AK",
                        "county": "Aleutians East Borough",
                        "CommunityConditions_Group": 2,
                    },
                    {
                        "fipscode": "02063",
                        "state": "AK",
                        "county": "Chugach Census Area",
                        "CommunityConditions_Group": None,
                    },
                ]
            },
            sort_keys=True,
        )
        + "\n"
    )
    tract_hazards = {
        column: values
        for hazard in HAZARDS
        for column, values in (
            (hazard.raw_column, [None] * 5),
            (
                hazard.rating_column,
                [NOT_APPLICABLE, NOT_APPLICABLE, NOT_APPLICABLE, NOT_APPLICABLE, INSUFFICIENT_DATA],
            ),
        )
    }
    tract_hazards["alrb_wfir"] = [0.0, 10.0, 20.0, 5.0, None]
    tract_hazards["ealr_wfir"] = [
        "No Expected Annual Losses",
        "Relatively Low",
        "Relatively High",
        "Very Low",
        INSUFFICIENT_DATA,
    ]
    fema = with_hazard_columns(
        pl.DataFrame(
            {
                "tract_id": [
                    "01001000100",
                    "01001000200",
                    "01001000300",
                    "02001000100",
                    "99999999999",
                ],
                "alr_npctl": [10.0, 50.0, 80.0, 25.0, 99.0],
                "alr_valb": [0.0, 5.0, 10.0, 2.0, None],
                "nri_version": ["December 2025"] * 5,
                **tract_hazards,
            }
        )
    )
    fema.write_parquet(paths.cache / "fema_nri_tracts.parquet")
    county_hazards = {
        column: values
        for hazard in HAZARDS
        for column, values in (
            (hazard.raw_column, [None, None]),
            (hazard.rating_column, [NOT_APPLICABLE, NOT_APPLICABLE]),
        )
    }
    county_hazards["alrb_wfir"] = [10.0, 0.0]
    county_hazards["ealr_wfir"] = ["Relatively High", "No Expected Annual Losses"]
    counties = with_hazard_columns(
        pl.DataFrame(
            {
                "county_fips": ["01001", "02001"],
                "county": ["Autauga", "Aleutians East"],
                "county_type": ["County", "Borough"],
                "state": ["AL", "AK"],
                "alr_npctl": [40.0, 12.0],
                "alr_valb": [8.0, 0.0],
                "nri_version": ["December 2025", "December 2025"],
                **county_hazards,
            }
        )
    )
    counties.write_parquet(paths.cache / "fema_nri_counties.parquet")
    return paths, tmp_path
