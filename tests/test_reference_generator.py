from pathlib import Path

import polars as pl

from househunter.reference_generation import align_acs_context, reconcile_connecticut


def test_acs_context_is_aligned_to_the_2020_place_universe() -> None:
    places = pl.DataFrame({"place_id": ["0100001", "0100002"]})
    raw_acs = pl.DataFrame(
        {
            "place_id": ["0100001", "0100003"],
            "population_2024": [10, 30],
            "housing_units_2024": [5, 15],
            "median_home_value_2024": [100_000, 300_000],
        }
    )
    aligned, audit = align_acs_context(places, raw_acs)
    assert aligned["place_id"].to_list() == ["0100001", "0100002"]
    assert aligned.row(1, named=True)["population_2024"] is None
    assert audit["missing_2020_place_ids"] == ["0100002"]
    assert audit["excluded_2024_only_place_ids"] == ["0100003"]


def test_connecticut_reconciliation_keeps_positive_housing_and_audits_water_split(
    tmp_path: Path,
) -> None:
    fema = tmp_path / "fema.parquet"
    pl.DataFrame(
        {
            "tract_id": ["09110000100"],
            "alr_npctl": [25.0],
            "nri_version": ["December 2025"],
        }
    ).write_parquet(fema)
    result, audit = reconcile_connecticut(
        {
            ("0900001", "09001000100"): 5,
            ("0900001", "09001990000"): 0,
        },
        {"09001000100": 5, "09001990000": 0},
        fema,
    )
    assert result == {("0900001", "09110000100"): 5}
    assert audit["splits"]["09001990000"]["housing_units"] == 0
    assert audit["splits"]["09001990000"]["fema_rows"] == []
