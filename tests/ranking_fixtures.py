from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

import househunter.build as build_module
import househunter.ranking_reference as ranking_reference
from househunter.ranking_reference import write_synthetic_fixture_bundle


def ranking_housing_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "county_fips": ["01001", "02001"],
            "housing_valid_months": [12, 12],
            "median_active_listings": [150.0, 220.0],
            "median_ppsf": [1000.0, 500.0],
            "sqft_for_1m_t12": [1000.0, 2000.0],
        }
    )


def install_ranking_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, housing: pl.DataFrame | None = None
) -> Path:
    bundle_dir = tmp_path / "ranking_v2_assets"
    write_synthetic_fixture_bundle(bundle_dir)
    monkeypatch.setattr(ranking_reference, "BUNDLED_RANKING_V2", bundle_dir)
    frame = housing if housing is not None else ranking_housing_frame()
    monkeypatch.setattr(
        build_module,
        "trailing_twelve_month_metrics",
        lambda paths, **kwargs: frame,
    )
    return bundle_dir
