from __future__ import annotations

from typing import Any

import polars as pl

from .cost_of_living import assign_counties, inherit_county_costs
from .errors import HouseHunterError
from .home_market import NORMALIZED_COLUMNS
from .housing_stock import HOUSING_COLUMNS, HousingStockBundle

COST_OF_LIVING_ATTRIBUTION = "U.S. Bureau of Economic Analysis, 2024 Regional Price Parities"
HOUSING_STOCK_ATTRIBUTION = (
    "U.S. Census Bureau, ACS 2024 five-year estimates, tables B25034 and B25035"
)
HOME_MARKET_METHODOLOGY_NOTICE = (
    "Realtor.com values are asking-market indicators for the source's residential "
    "inventory, not sale prices, property valuations, or total ownership costs. The "
    "$1M buying-power result does not imply that a matching home is listed."
)

COST_VALUE_COLUMNS = (
    "cost_of_living_index",
    "cost_of_living_goods_index",
    "cost_of_living_housing_rents_index",
    "cost_of_living_utilities_index",
    "cost_of_living_other_services_index",
)
COST_PUBLIC_COLUMNS = (
    *COST_VALUE_COLUMNS,
    "cost_of_living_geography_type",
    "cost_of_living_geography_id",
    "cost_of_living_geography_name",
    "cost_of_living_release_year",
    "cost_of_living_coverage_status",
    "cost_of_living_attribution",
)
HOME_VALUE_COLUMNS = (
    "home_sqft_for_1m",
    "home_buying_power_percentile",
    "home_median_listing_price",
    "home_median_listing_price_per_square_foot",
    "home_median_square_feet",
    "home_active_listing_count",
)
HOME_PUBLIC_COLUMNS = (
    *HOME_VALUE_COLUMNS,
    "home_market_month",
    "home_costs_coverage_status",
    "home_market_attribution",
    "home_market_usage_notice",
)
HOUSING_VALUE_COLUMNS = (
    "housing_stock_total_units_estimate",
    "housing_built_2000_plus_pct",
    "housing_built_2010_plus_pct",
    "housing_built_2020_plus_pct",
    "housing_median_year_built",
)
HOUSING_PUBLIC_COLUMNS = (
    *HOUSING_VALUE_COLUMNS,
    "housing_stock_release_year",
    "housing_stock_coverage_status",
    "housing_stock_attribution",
)
SUMMARY_DIMENSION_COLUMNS = (
    *COST_PUBLIC_COLUMNS,
    *HOME_PUBLIC_COLUMNS,
    *HOUSING_PUBLIC_COLUMNS,
)

_COST_OUTSIDE_SCOPE_STATES = frozenset({"AS", "GU", "MP", "PR", "VI"})
_HOME_OUTSIDE_SCOPE_STATES = _COST_OUTSIDE_SCOPE_STATES
_HOUSING_OUTSIDE_SCOPE_STATES = frozenset({"AS", "GU", "MP", "VI"})


def empty_cost_source() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "cost_of_living_geography_id": pl.String,
            "cost_of_living_geography_name": pl.String,
            **{column: pl.Float64 for column in COST_VALUE_COLUMNS},
            "cost_of_living_release_year": pl.Int16,
        }
    )


def empty_home_source() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "county_fips": pl.String,
            "source_county_name": pl.String,
            "home_median_listing_price": pl.Float64,
            "home_median_listing_price_per_square_foot": pl.Float64,
            "home_median_square_feet": pl.Float64,
            "home_active_listing_count": pl.Float64,
            "home_total_listing_count": pl.Float64,
            "home_market_month": pl.String,
            "source_quality_flag": pl.Int8,
            "home_sqft_for_1m_unrounded": pl.Float64,
            "home_sqft_for_1m": pl.Int64,
            "home_buying_power_percentile": pl.Float64,
            "home_costs_coverage_status": pl.String,
        }
    ).select(*NORMALIZED_COLUMNS)


def empty_housing_sources() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    housing_schema: dict[str, pl.DataType] = {
        "housing_stock_total_units_estimate": pl.Int64,
        "housing_stock_total_units_moe": pl.Int64,
        "housing_built_2020_plus_estimate": pl.Int64,
        "housing_built_2020_plus_moe": pl.Int64,
        "housing_built_2010_2019_estimate": pl.Int64,
        "housing_built_2010_2019_moe": pl.Int64,
        "housing_built_2000_2009_estimate": pl.Int64,
        "housing_built_2000_2009_moe": pl.Int64,
        "housing_built_2020_plus_pct": pl.Float64,
        "housing_built_2010_plus_pct": pl.Float64,
        "housing_built_2000_plus_pct": pl.Float64,
        "housing_median_year_built": pl.Int64,
        "housing_median_year_built_moe": pl.Int64,
        "housing_stock_release_year": pl.Int16,
        "housing_stock_coverage_status": pl.String,
    }
    tracts = pl.DataFrame(schema={"tract_id": pl.String, **housing_schema}).select(
        "tract_id", *HOUSING_COLUMNS
    )
    counties = pl.DataFrame(schema={"county_fips": pl.String, **housing_schema}).select(
        "county_fips", *HOUSING_COLUMNS
    )
    crosswalk = pl.DataFrame(schema={"county_fips": pl.String, "cbsa_id": pl.String})
    return tracts, counties, crosswalk


def _assert_preserved(before: pl.DataFrame, after: pl.DataFrame, label: str) -> None:
    if after.height != before.height or after["place_id"].to_list() != before["place_id"].to_list():
        raise HouseHunterError(f"{label} enrichment changed geography rows or ordering")


def _null_outside(
    frame: pl.DataFrame, states: frozenset[str], columns: tuple[str, ...]
) -> list[pl.Expr]:
    return [
        pl.when(pl.col("state").is_in(sorted(states)))
        .then(pl.lit(None, dtype=frame.schema[column]))
        .otherwise(pl.col(column))
        .alias(column)
        for column in columns
    ]


def _missing_cost(frame: pl.DataFrame) -> pl.DataFrame:
    status = (
        pl.when(pl.col("state").is_in(sorted(_COST_OUTSIDE_SCOPE_STATES)))
        .then(pl.lit("outside_scope"))
        .when(pl.col("state") == "??")
        .then(pl.lit("unmatched_geography"))
        .otherwise(pl.lit("source_unavailable"))
        .alias("cost_of_living_coverage_status")
    )
    return frame.with_columns(
        *[pl.lit(None, dtype=pl.Float64).alias(column) for column in COST_VALUE_COLUMNS],
        pl.lit(None, dtype=pl.String).alias("cost_of_living_geography_type"),
        pl.lit(None, dtype=pl.String).alias("cost_of_living_geography_id"),
        pl.lit(None, dtype=pl.String).alias("cost_of_living_geography_name"),
        pl.lit(None, dtype=pl.Int16).alias("cost_of_living_release_year"),
        status,
        pl.lit(COST_OF_LIVING_ATTRIBUTION).alias("cost_of_living_attribution"),
    )


def attach_cost_of_living(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    *,
    rpp: pl.DataFrame | None,
    housing: HousingStockBundle | None,
    source: dict[str, Any] | None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    if rpp is None or housing is None or source is None:
        return _missing_cost(places), _missing_cost(counties)
    county_costs = assign_counties(counties.select("county_fips"), housing.county_msa, rpp, source)
    county_public = (
        county_costs.rename({"county_fips": "place_id"})
        .select("place_id", *COST_PUBLIC_COLUMNS[:-1])
        .with_columns(pl.lit(COST_OF_LIVING_ATTRIBUTION).alias("cost_of_living_attribution"))
    )
    attached_counties = counties.join(county_public, on="place_id", how="left", validate="1:1")
    tract_costs = (
        inherit_county_costs(places.select(pl.col("place_id").alias("tract_id")), county_costs)
        .drop("county_fips")
        .rename({"tract_id": "place_id"})
        .select("place_id", *COST_PUBLIC_COLUMNS[:-1])
        .with_columns(pl.lit(COST_OF_LIVING_ATTRIBUTION).alias("cost_of_living_attribution"))
    )
    attached_places = places.join(tract_costs, on="place_id", how="left", validate="1:1")
    _assert_preserved(places, attached_places, "Cost-of-living tract")
    _assert_preserved(counties, attached_counties, "Cost-of-living county")
    return attached_places, attached_counties


def _home_status(*, available: bool) -> pl.Expr:
    return (
        pl.when(pl.col("state").is_in(sorted(_HOME_OUTSIDE_SCOPE_STATES)))
        .then(pl.lit("outside_scope"))
        .when(pl.col("state") == "??")
        .then(pl.lit("missing_market"))
        .when(pl.col("home_costs_coverage_status").is_not_null())
        .then(pl.col("home_costs_coverage_status"))
        .otherwise(pl.lit("missing_market" if available else "source_unavailable"))
        .alias("home_costs_coverage_status")
    )


def attach_home_market(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    *,
    home: pl.DataFrame | None,
    attribution: str,
    usage_notice: str,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    source = home if home is not None else empty_home_source()
    public = source.select("county_fips", *HOME_PUBLIC_COLUMNS[:-2]).rename(
        {"county_fips": "place_id"}
    )
    attached_counties = counties.join(public, on="place_id", how="left", validate="1:1")
    attached_counties = attached_counties.with_columns(
        _home_status(available=home is not None),
        *_null_outside(
            attached_counties,
            _HOME_OUTSIDE_SCOPE_STATES,
            (*HOME_VALUE_COLUMNS, "home_market_month"),
        ),
        pl.lit(attribution).alias("home_market_attribution"),
        pl.lit(usage_notice).alias("home_market_usage_notice"),
    )
    inherited = public.rename({"place_id": "_home_county_fips"})
    attached_places = (
        places.with_columns(pl.col("county_fips").alias("_home_county_fips"))
        .join(inherited, on="_home_county_fips", how="left", validate="m:1")
        .drop("_home_county_fips")
        .with_columns(
            _home_status(available=home is not None),
            *_null_outside(
                public,
                _HOME_OUTSIDE_SCOPE_STATES,
                (*HOME_VALUE_COLUMNS, "home_market_month"),
            ),
            pl.lit(attribution).alias("home_market_attribution"),
            pl.lit(usage_notice).alias("home_market_usage_notice"),
        )
    )
    _assert_preserved(places, attached_places, "Home-market tract")
    _assert_preserved(counties, attached_counties, "Home-market county")
    return attached_places, attached_counties


def _housing_status(*, available: bool) -> pl.Expr:
    return (
        pl.when(pl.col("state").is_in(sorted(_HOUSING_OUTSIDE_SCOPE_STATES)))
        .then(pl.lit("outside_scope"))
        .when(pl.col("housing_stock_coverage_status").is_not_null())
        .then(pl.col("housing_stock_coverage_status"))
        .otherwise(pl.lit("missing_acs" if available else "asset_unavailable"))
        .alias("housing_stock_coverage_status")
    )


def attach_housing_stock(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    *,
    housing: HousingStockBundle | None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    if housing is None:
        tract_source, county_source, _ = empty_housing_sources()
    else:
        tract_source, county_source = housing.tracts, housing.counties

    def public(frame: pl.DataFrame, identifier: str) -> pl.DataFrame:
        return (
            frame.select(identifier, *HOUSING_PUBLIC_COLUMNS[:-1])
            .rename({identifier: "place_id"})
            .with_columns(
                [
                    pl.col(column).round(2).alias(column)
                    for column in (
                        "housing_built_2000_plus_pct",
                        "housing_built_2010_plus_pct",
                        "housing_built_2020_plus_pct",
                    )
                ]
            )
        )

    attached_places = places.join(
        public(tract_source, "tract_id"), on="place_id", how="left", validate="1:1"
    )
    attached_places = attached_places.with_columns(
        _housing_status(available=housing is not None),
        *_null_outside(
            attached_places,
            _HOUSING_OUTSIDE_SCOPE_STATES,
            (*HOUSING_VALUE_COLUMNS, "housing_stock_release_year"),
        ),
        pl.lit(HOUSING_STOCK_ATTRIBUTION).alias("housing_stock_attribution"),
    )
    attached_counties = counties.join(
        public(county_source, "county_fips"),
        on="place_id",
        how="left",
        validate="1:1",
    )
    attached_counties = attached_counties.with_columns(
        _housing_status(available=housing is not None),
        *_null_outside(
            attached_counties,
            _HOUSING_OUTSIDE_SCOPE_STATES,
            (*HOUSING_VALUE_COLUMNS, "housing_stock_release_year"),
        ),
        pl.lit(HOUSING_STOCK_ATTRIBUTION).alias("housing_stock_attribution"),
    )
    _assert_preserved(places, attached_places, "Housing-stock tract")
    _assert_preserved(counties, attached_counties, "Housing-stock county")
    return attached_places, attached_counties


def attach_dimensions(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    *,
    rpp: pl.DataFrame | None,
    home: pl.DataFrame | None,
    housing: HousingStockBundle | None,
    bea_source: dict[str, Any] | None,
    home_attribution: str,
    home_usage_notice: str,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    original_place_count = places.height
    original_county_count = counties.height
    places, counties = attach_cost_of_living(
        places, counties, rpp=rpp, housing=housing, source=bea_source
    )
    places, counties = attach_home_market(
        places,
        counties,
        home=home,
        attribution=home_attribution,
        usage_notice=home_usage_notice,
    )
    places, counties = attach_housing_stock(places, counties, housing=housing)
    if places.height != original_place_count or counties.height != original_county_count:
        raise HouseHunterError("Optional enrichment changed snapshot row counts")
    return places, counties


def attach_dimensions_fail_open(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    *,
    rpp: pl.DataFrame | None,
    home: pl.DataFrame | None,
    housing: HousingStockBundle | None,
    bea_source: dict[str, Any] | None,
    home_attribution: str,
    home_usage_notice: str,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, bool], list[dict[str, str]]]:
    warnings: list[dict[str, str]] = []
    availability = {
        "cost_of_living": rpp is not None and housing is not None and bea_source is not None,
        "home_market": home is not None,
        "housing_stock": housing is not None,
    }
    failures = (
        HouseHunterError,
        pl.exceptions.PolarsError,
        KeyError,
        TypeError,
        ValueError,
    )
    try:
        places, counties = attach_cost_of_living(
            places, counties, rpp=rpp, housing=housing, source=bea_source
        )
    except failures as exc:
        availability["cost_of_living"] = False
        warnings.append(
            {
                "source": "bea_rpp",
                "status": "source_unavailable",
                "message": f"Cost-of-living enrichment rejected: {exc}",
            }
        )
        places, counties = _missing_cost(places), _missing_cost(counties)
    try:
        places, counties = attach_home_market(
            places,
            counties,
            home=home,
            attribution=home_attribution,
            usage_notice=home_usage_notice,
        )
    except failures as exc:
        availability["home_market"] = False
        warnings.append(
            {
                "source": "home_market",
                "status": "source_unavailable",
                "message": f"Home-market enrichment rejected: {exc}",
            }
        )
        places, counties = attach_home_market(
            places,
            counties,
            home=None,
            attribution=home_attribution,
            usage_notice=home_usage_notice,
        )
    try:
        places, counties = attach_housing_stock(places, counties, housing=housing)
    except failures as exc:
        availability["housing_stock"] = False
        warnings.append(
            {
                "source": "housing_stock",
                "status": "asset_unavailable",
                "message": f"Housing-stock enrichment rejected: {exc}",
            }
        )
        places, counties = attach_housing_stock(places, counties, housing=None)
    return places, counties, availability, warnings
