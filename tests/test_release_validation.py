from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from househunter.config import RuntimePaths

spec = importlib.util.spec_from_file_location(
    "househunter_validate_release",
    Path(__file__).parents[1] / "scripts" / "validate_release.py",
)
assert spec and spec.loader
release_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_module)

TRACT_IDS = {"01001000100", "01001000200", "01001000300", "02001000100", "99999999999"}
COUNTY_IDS = {"01001", "02001"}


def manifest() -> dict[str, list[dict[str, object]]]:
    return {
        "files": [
            {"key": "tracts-national", "filename": "tracts.topojson.gz"},
            {"key": "counties-national", "filename": "counties.topojson.gz"},
        ]
    }


def test_release_validator_uses_current_fema_and_map_assets(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    paths, fixture_root = fixture_environment
    config_path = fixture_root / "sources.yml"
    config = yaml.safe_load(config_path.read_text())
    config["bea_rpp"] = {"release_year": 2024}
    config_path.write_text(yaml.safe_dump(config))
    mountain = {
        "release_id": "1" * 16,
        "schema_version": 2,
        "magnitude_version": "mountain_magnitude_v2",
    }
    housing_stock = {
        "release_year": 2024,
        "tracts": 85_382,
        "counties": 3_222,
        "county_msa": 1_915,
    }
    monkeypatch.setattr(release_module, "load_manifest", manifest)
    monkeypatch.setattr(release_module, "validate_bundled_mountain", lambda: mountain)
    monkeypatch.setattr(
        release_module, "validate_bundled_housing_stock", lambda: housing_stock
    )
    monkeypatch.setattr(
        release_module,
        "topology_ids",
        lambda _manifest, key: TRACT_IDS if key == "tracts-national" else COUNTY_IDS,
    )

    assert release_module.validate_release(paths) == {
        "chrr_counties": 3,
        "chrr_fema_matches": 2,
        "chrr_grouped_counties": 2,
        "bea_rpp": {"cached": False, "release_year": 2024},
        "counties": 2,
        "home_market_lock": {
            "approved_releases": ["2026-08"],
            "automatic_download": False,
            "private_use_only": True,
        },
        "map_assets": 2,
        "mountain": mountain,
        "housing_stock": housing_stock,
        "ranked_counties": 2,
        "ranked_tracts": 5,
        "status": "PASS",
        "tracts": 5,
    }


@pytest.mark.parametrize(
    ("key", "ids", "message"),
    [
        ("tracts-national", TRACT_IDS - {"99999999999"}, "tract identifiers"),
        ("counties-national", COUNTY_IDS - {"02001"}, "county identifiers"),
        (
            "tracts-national",
            (TRACT_IDS - {"99999999999"}) | {"03001000100"},
            "tract identifiers",
        ),
        ("counties-national", {"01001", "03001"}, "county identifiers"),
    ],
)
def test_release_validator_rejects_map_ids_that_differ_from_current_caches(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    ids: set[str],
    message: str,
) -> None:
    paths, _ = fixture_environment
    monkeypatch.setattr(release_module, "load_manifest", manifest)
    monkeypatch.setattr(
        release_module,
        "topology_ids",
        lambda _manifest, requested: (
            ids
            if requested == key
            else (TRACT_IDS if requested == "tracts-national" else COUNTY_IDS)
        ),
    )

    with pytest.raises(ValueError, match=message):
        release_module.validate_release(paths)
