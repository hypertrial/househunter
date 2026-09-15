from __future__ import annotations

import polars as pl

from househunter.dimensions import attach_dimensions, attach_dimensions_fail_open
from househunter.housing_stock import HousingStockBundle


def _housing_row(identifier: str, *, total: int, built: float) -> dict[str, object]:
    key = "tract_id" if len(identifier) == 11 else "county_fips"
    return {
        key: identifier,
        "housing_stock_total_units_estimate": total,
        "housing_stock_total_units_moe": 10,
        "housing_built_2020_plus_estimate": 10,
        "housing_built_2020_plus_moe": 2,
        "housing_built_2010_2019_estimate": 20,
        "housing_built_2010_2019_moe": 3,
        "housing_built_2000_2009_estimate": 30,
        "housing_built_2000_2009_moe": 4,
        "housing_built_2020_plus_pct": 10.0,
        "housing_built_2010_plus_pct": 20.0,
        "housing_built_2000_plus_pct": built,
        "housing_median_year_built": 1985,
        "housing_median_year_built_moe": 2,
        "housing_stock_release_year": 2024,
        "housing_stock_coverage_status": "complete",
    }


def _bundle() -> HousingStockBundle:
    return HousingStockBundle(
        manifest={"release_year": 2024},
        tracts=pl.DataFrame(
            [
                _housing_row("01001000100", total=100, built=30.123),
                _housing_row("02001000100", total=200, built=60.987),
                _housing_row("72001000100", total=300, built=40.555),
            ]
        ),
        counties=pl.DataFrame(
            [
                _housing_row("01001", total=1000, built=31.123),
                _housing_row("02001", total=2000, built=61.987),
                _housing_row("72001", total=3000, built=41.555),
            ]
        ),
        county_msa=pl.DataFrame(
            {"county_fips": ["01001", "02001"], "cbsa_id": ["12345", "22222"]}
        ),
    )


def test_dimensions_preserve_rows_grain_statuses_and_inheritance() -> None:
    places = pl.DataFrame(
        {
            "place_id": [
                "01001000100",
                "02001000100",
                "72001000100",
                "99999999999",
            ],
            "state": ["AL", "AK", "PR", "??"],
            "county_fips": ["01001", "02001", "72001", "??"],
        }
    )
    counties = pl.DataFrame(
        {
            "place_id": ["01001", "02001", "72001", "99999"],
            "county_fips": ["01001", "02001", "72001", "99999"],
            "county_name": ["Canonical AL", "Canonical AK", "Canonical PR", "Unknown"],
            "state": ["AL", "AK", "PR", "??"],
        }
    )
    rpp = pl.DataFrame(
        {
            "cost_of_living_geography_id": ["00999", "12345"],
            "cost_of_living_geography_name": ["U.S. Nonmetropolitan Portion", "Test MSA"],
            "cost_of_living_index": [90.0, 110.0],
            "cost_of_living_goods_index": [91.0, 111.0],
            "cost_of_living_housing_rents_index": [92.0, 112.0],
            "cost_of_living_utilities_index": [93.0, 113.0],
            "cost_of_living_other_services_index": [94.0, 114.0],
            "cost_of_living_release_year": [2024, 2024],
        }
    )
    home = pl.DataFrame(
        {
            "county_fips": ["01001", "02001"],
            "home_sqft_for_1m": [1000, None],
            "home_buying_power_percentile": [50.0, None],
            "home_median_listing_price": [500000.0, 600000.0],
            "home_median_listing_price_per_square_foot": [1000.0, 500.0],
            "home_median_square_feet": [1200.0, 1400.0],
            "home_active_listing_count": [10.0, 20.0],
            "home_market_month": ["2026-08", "2026-08"],
            "home_costs_coverage_status": ["complete", "source_quality_flag"],
        }
    )
    attached_places, attached_counties = attach_dimensions(
        places,
        counties,
        rpp=rpp,
        home=home,
        housing=_bundle(),
        bea_source={"expected_msa_count": 1, "nonmetropolitan_geofips": "00999"},
        home_attribution="Realtor.com Research Data",
        home_usage_notice="Personal local use only",
    )

    assert attached_places["place_id"].to_list() == places["place_id"].to_list()
    assert attached_counties["place_id"].to_list() == counties["place_id"].to_list()
    by_county = {row["place_id"]: row for row in attached_counties.iter_rows(named=True)}
    assert by_county["01001"]["cost_of_living_geography_type"] == "metropolitan"
    assert by_county["01001"]["cost_of_living_index"] == 110.0
    assert by_county["02001"]["cost_of_living_geography_type"] == "nonmetropolitan"
    assert by_county["02001"]["cost_of_living_index"] == 90.0
    assert by_county["72001"]["cost_of_living_coverage_status"] == "outside_scope"
    assert by_county["72001"]["cost_of_living_index"] is None
    assert by_county["99999"]["cost_of_living_coverage_status"] == "unmatched_geography"
    assert by_county["01001"]["home_sqft_for_1m"] == 1000
    assert by_county["02001"]["home_costs_coverage_status"] == "source_quality_flag"
    assert by_county["02001"]["home_sqft_for_1m"] is None
    assert by_county["72001"]["home_costs_coverage_status"] == "outside_scope"
    assert by_county["72001"]["home_median_listing_price"] is None
    assert by_county["72001"]["housing_stock_coverage_status"] == "complete"
    assert by_county["72001"]["housing_built_2000_plus_pct"] == 41.56
    assert by_county["99999"]["housing_stock_coverage_status"] == "missing_acs"
    by_tract = {row["place_id"]: row for row in attached_places.iter_rows(named=True)}
    assert by_tract["01001000100"]["cost_of_living_index"] == 110.0
    assert by_tract["01001000100"]["home_sqft_for_1m"] == 1000
    assert by_tract["01001000100"]["housing_built_2000_plus_pct"] == 30.12
    assert by_tract["72001000100"]["cost_of_living_coverage_status"] == "outside_scope"
    assert by_tract["72001000100"]["housing_stock_coverage_status"] == "complete"


def test_unavailable_dimensions_keep_typed_statuses_and_null_values() -> None:
    places = pl.DataFrame(
        {
            "place_id": ["01001000100", "72001000100", "60010000100"],
            "state": ["AL", "PR", "AS"],
            "county_fips": ["01001", "72001", "60010"],
        }
    )
    counties = pl.DataFrame(
        {
            "place_id": ["01001", "72001", "60010"],
            "county_fips": ["01001", "72001", "60010"],
            "state": ["AL", "PR", "AS"],
        }
    )
    attached_places, attached_counties = attach_dimensions(
        places,
        counties,
        rpp=None,
        home=None,
        housing=None,
        bea_source=None,
        home_attribution="source",
        home_usage_notice="notice",
    )

    for frame in (attached_places, attached_counties):
        rows = {row["state"]: row for row in frame.iter_rows(named=True)}
        assert rows["AL"]["cost_of_living_coverage_status"] == "source_unavailable"
        assert rows["AL"]["home_costs_coverage_status"] == "source_unavailable"
        assert rows["AL"]["housing_stock_coverage_status"] == "asset_unavailable"
        assert rows["PR"]["cost_of_living_coverage_status"] == "outside_scope"
        assert rows["PR"]["home_costs_coverage_status"] == "outside_scope"
        assert rows["PR"]["housing_stock_coverage_status"] == "asset_unavailable"
        assert rows["AS"]["housing_stock_coverage_status"] == "outside_scope"
        assert rows["AL"]["cost_of_living_index"] is None
        assert rows["AL"]["home_sqft_for_1m"] is None
        assert rows["AL"]["housing_built_2000_plus_pct"] is None


def test_rejected_cost_enrichment_does_not_discard_home_or_housing() -> None:
    places = pl.DataFrame(
        {"place_id": ["01001000100"], "state": ["AL"], "county_fips": ["01001"]}
    )
    counties = pl.DataFrame(
        {"place_id": ["01001"], "county_fips": ["01001"], "state": ["AL"]}
    )
    invalid_rpp = pl.DataFrame(
        {
            "cost_of_living_geography_id": ["00999"],
            "cost_of_living_geography_name": ["U.S. Nonmetropolitan Portion"],
            "cost_of_living_index": [90.0],
            "cost_of_living_goods_index": [91.0],
            "cost_of_living_housing_rents_index": [92.0],
            "cost_of_living_utilities_index": [93.0],
            "cost_of_living_other_services_index": [94.0],
            "cost_of_living_release_year": [2024],
        }
    )
    home = pl.DataFrame(
        {
            "county_fips": ["01001"],
            "home_sqft_for_1m": [1000],
            "home_buying_power_percentile": [100.0],
            "home_median_listing_price": [500000.0],
            "home_median_listing_price_per_square_foot": [1000.0],
            "home_median_square_feet": [1200.0],
            "home_active_listing_count": [10.0],
            "home_market_month": ["2026-08"],
            "home_costs_coverage_status": ["complete"],
        }
    )

    attached_places, _, availability, warnings = attach_dimensions_fail_open(
        places,
        counties,
        rpp=invalid_rpp,
        home=home,
        housing=_bundle(),
        bea_source={"expected_msa_count": 1, "nonmetropolitan_geofips": "00999"},
        home_attribution="source",
        home_usage_notice="notice",
    )

    row = attached_places.row(0, named=True)
    assert availability == {
        "cost_of_living": False,
        "home_market": True,
        "housing_stock": True,
    }
    assert warnings[0]["source"] == "bea_rpp"
    assert row["cost_of_living_coverage_status"] == "source_unavailable"
    assert row["home_sqft_for_1m"] == 1000
    assert row["housing_stock_coverage_status"] == "complete"


def test_home_market_inheritance_requires_canonical_county_match() -> None:
    places = pl.DataFrame(
        {
            "place_id": ["01001999999"],
            "state": ["AL"],
            "county_fips": ["??"],
        }
    )
    counties = pl.DataFrame(
        {
            "place_id": ["01001"],
            "county_fips": ["01001"],
            "state": ["AL"],
        }
    )
    home = pl.DataFrame(
        {
            "county_fips": ["01001"],
            "home_sqft_for_1m": [1000],
            "home_buying_power_percentile": [100.0],
            "home_median_listing_price": [500000.0],
            "home_median_listing_price_per_square_foot": [1000.0],
            "home_median_square_feet": [1200.0],
            "home_active_listing_count": [10.0],
            "home_market_month": ["2026-08"],
            "home_costs_coverage_status": ["complete"],
        }
    )

    attached_places, _ = attach_dimensions(
        places,
        counties,
        rpp=None,
        home=home,
        housing=None,
        bea_source=None,
        home_attribution="source",
        home_usage_notice="notice",
    )

    row = attached_places.row(0, named=True)
    assert row["home_costs_coverage_status"] == "missing_market"
    assert row["home_sqft_for_1m"] is None
    assert row["home_buying_power_percentile"] is None
