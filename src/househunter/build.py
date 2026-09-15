from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import duckdb
import polars as pl

from .chrr import build_processed
from .config import (
    RuntimePaths,
    atomic_write_json,
    canonical_json,
    load_config,
    sha256_bytes,
)
from .contracts import (
    COST_OF_LIVING_COVERAGE_STATUSES,
    COUNTY_METHODOLOGY_NOTICE,
    HOME_COSTS_COVERAGE_STATUSES,
    HOUSING_STOCK_COVERAGE_STATUSES,
    METHODOLOGY_NOTICE,
    MapScoreColumns,
)
from .cost_of_living import build_processed as build_cost_of_living
from .dimensions import (
    COST_OF_LIVING_ATTRIBUTION,
    HOME_MARKET_METHODOLOGY_NOTICE,
    HOUSING_STOCK_ATTRIBUTION,
    SUMMARY_DIMENSION_COLUMNS,
    attach_dimensions_fail_open,
    empty_cost_source,
    empty_home_source,
    empty_housing_sources,
)
from .download import validate_cached_fema, validate_cached_fema_counties
from .errors import HouseHunterError
from .geography import (
    KNOWN_STATES,
    STATE_BY_FIPS,
    UNKNOWN_COUNTY_FIPS,
    UNKNOWN_COUNTY_NAME,
    UNKNOWN_STATE,
)
from .hazards import (
    HAZARD_RATING_COLUMNS,
    HAZARD_RAW_COLUMNS,
    HAZARD_SNAPSHOT_COLUMNS,
    HAZARDS,
    KNOWN_EAL_RATINGS,
    score_residential_hazards,
    with_hazard_columns,
)
from .home_market import (
    load_current_release,
    load_release_lock,
    trailing_twelve_month_identity,
    trailing_twelve_month_metrics,
)
from .housing_stock import HousingStockBundle, validate_housing_stock_assets
from .ranking_reference import (
    CALIBRATION_ID,
    METHODOLOGY_ID,
    RANKING_SIDECAR_COLUMNS,
    assemble_ranking_sidecar,
    empty_ranking_sidecar,
    ranking_bundle_identity,
    validate_ranking_assets,
)

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]

BUILD_SCHEMA_VERSION = 12
MOUNTAIN_RUNTIME_GEOGRAPHY_VERSION = "mountain_runtime_geography_v1"
_CONNECTICUT_UNMATCHED_ZERO_POPULATION_TRACTS = frozenset(
    {"09001990000", "09007990100", "09009990000", "09011990100"}
)
_SNAPSHOT_TABLES = {
    "places": ("places.parquet", ("place_id",)),
    "counties": ("counties.parquet", ("place_id",)),
    "chrr_county": ("chrr_county.parquet", ("county_fips",)),
    "cost_of_living": (
        "cost_of_living.parquet",
        ("cost_of_living_geography_id",),
    ),
    "home_market": ("home_market.parquet", ("county_fips",)),
    "housing_stock_tract": ("housing_stock_tract.parquet", ("tract_id",)),
    "housing_stock_county": ("housing_stock_county.parquet", ("county_fips",)),
    "housing_stock_county_msa": (
        "housing_stock_county_msa.parquet",
        ("county_fips",),
    ),
    "ranking_counties": ("ranking_counties.parquet", ("place_id",)),
}
_SNAPSHOT_FILES = (
    "build.json",
    *(record[0] for record in _SNAPSHOT_TABLES.values()),
    "househunter.duckdb",
)
SNAPSHOT_FILES = _SNAPSHOT_FILES
_CURRENT_MOUNTAIN = object()


def _housing_stock_reference_identity() -> tuple[
    HousingStockBundle | None, dict[str, object], dict[str, str] | None
]:
    try:
        bundle = validate_housing_stock_assets()
    except (HouseHunterError, KeyError, TypeError, ValueError, AttributeError) as exc:
        return (
            None,
            {
                "available": False,
                "checksum": sha256_bytes(b"housing-stock:asset-unavailable"),
                "release_year": "unavailable",
                "omb_delineation": "unavailable",
                "coverage_status": "asset_unavailable",
            },
            {
                "source": "housing_stock",
                "status": "asset_unavailable",
                "message": str(exc),
            },
        )
    provenance = {
        "schema_version": bundle.manifest["schema_version"],
        "release_year": bundle.manifest["release_year"],
        "omb_delineation": bundle.manifest["omb_delineation"],
        "source_lock_sha256": bundle.manifest["source_lock_sha256"],
        "artifact_sha256": {
            key: record["sha256"] for key, record in bundle.manifest["files"].items()
        },
    }
    return (
        bundle,
        {
            "available": True,
            "checksum": sha256_bytes(canonical_json(provenance)),
            "release_year": bundle.manifest["release_year"],
            "omb_delineation": bundle.manifest["omb_delineation"],
            "coverage_status": "complete",
        },
        None,
    )


def _load_optional_dimension_inputs(
    paths: RuntimePaths, config: dict[str, object]
) -> dict[str, object]:
    warnings: list[dict[str, str]] = []
    housing, housing_identity, housing_warning = _housing_stock_reference_identity()
    if housing_warning is not None:
        warnings.append(housing_warning)

    bea_source_value = config.get("bea_rpp")
    bea_source = bea_source_value if isinstance(bea_source_value, dict) else None
    rpp: pl.DataFrame | None = None
    bea_digest = sha256_bytes(b"bea-rpp:source-unavailable")
    bea_error: str | None = None
    if bea_source is None:
        bea_error = "BEA RPP source configuration is unavailable"
    else:
        try:
            rpp, bea_digest = build_cost_of_living(paths)
        except (HouseHunterError, OSError, KeyError, TypeError, ValueError) as exc:
            bea_error = str(exc)
    if bea_error is not None:
        warnings.append(
            {
                "source": "bea_rpp",
                "status": "source_unavailable",
                "message": bea_error,
            }
        )
    elif housing is None:
        warnings.append(
            {
                "source": "bea_rpp",
                "status": "source_unavailable",
                "message": "County-to-CBSA reference asset is unavailable",
            }
        )

    default_home_attribution = "https://www.realtor.com/research/data/"
    default_home_notice = "For personal local use only."
    home_lock: dict[str, object] | None = None
    home: pl.DataFrame | None = None
    home_manifest: dict[str, object] | None = None
    home_stale: bool | None = None
    home_error: str | None = None
    try:
        loaded_lock = load_release_lock()
        home_lock = loaded_lock
        current_home = load_current_release(paths)
        if current_home is not None:
            home = current_home.frame
            home_manifest = current_home.manifest
            home_stale = current_home.stale
    except (HouseHunterError, OSError, KeyError, TypeError, ValueError) as exc:
        home_error = str(exc)
        warnings.append(
            {
                "source": "home_market",
                "status": "source_unavailable",
                "message": home_error,
            }
        )
    if home_lock is not None:
        home_attribution = str(home_lock["source_page"])
        home_notice = str(home_lock["usage_notice"])
    else:
        home_attribution = default_home_attribution
        home_notice = default_home_notice
    if home is None and home_error is None:
        warnings.append(
            {
                "source": "home_market",
                "status": "source_unavailable",
                "message": (
                    "No validated local home-market import; run "
                    "`househunter import-home-market FILE --acknowledge-personal-use`"
                ),
            }
        )
    try:
        t12_identity = trailing_twelve_month_identity(paths)
    except (HouseHunterError, OSError, KeyError, TypeError, ValueError):
        t12_identity = {
            "as_of_month": None,
            "window": 12,
            "months": [],
            "error": "unavailable",
        }
    current_home_identity = None
    if home_manifest is not None:
        current_home_identity = {
            "source_sha256": home_manifest["source_sha256"],
            "logical_sha256": home_manifest["logical_sha256"],
            "month": home_manifest["month"],
        }
    if current_home_identity is None and not t12_identity.get("months"):
        home_digest = sha256_bytes(b"home-market:source-unavailable")
    else:
        home_digest = sha256_bytes(
            canonical_json({"current": current_home_identity, "trailing_twelve": t12_identity})
        )

    if housing is None:
        housing_tracts, housing_counties, housing_county_msa = empty_housing_sources()
    else:
        housing_tracts = housing.tracts
        housing_counties = housing.counties
        housing_county_msa = housing.county_msa
    return {
        "housing": housing,
        "housing_identity": housing_identity,
        "rpp": rpp,
        "bea_source": bea_source,
        "bea_digest": bea_digest,
        "bea_error": bea_error,
        "home": home,
        "home_manifest": home_manifest,
        "home_stale": home_stale,
        "home_error": home_error,
        "home_attribution": home_attribution,
        "home_usage_notice": home_notice,
        "home_digest": home_digest,
        "warnings": warnings,
        "source_tables": {
            "cost_of_living": rpp if rpp is not None else empty_cost_source(),
            "home_market": home if home is not None else empty_home_source(),
            "housing_stock_tract": housing_tracts,
            "housing_stock_county": housing_counties,
            "housing_stock_county_msa": housing_county_msa,
        },
    }


def _build_ranking_sidecar(
    paths: RuntimePaths, county_scored: pl.DataFrame
) -> tuple[pl.DataFrame, dict[str, object]]:
    try:
        bundle = validate_ranking_assets()
    except (HouseHunterError, OSError, KeyError, TypeError, ValueError) as exc:
        return empty_ranking_sidecar(), {
            "available": False,
            "methodology_id": METHODOLOGY_ID,
            "calibration_id": CALIBRATION_ID,
            "error": str(exc),
            "row_count": 0,
        }
    try:
        housing = trailing_twelve_month_metrics(paths)
    except (HouseHunterError, OSError, KeyError, TypeError, ValueError):
        housing = pl.DataFrame(
            {
                "county_fips": [],
                "housing_valid_months": [],
                "median_active_listings": [],
                "median_ppsf": [],
                "sqft_for_1m_t12": [],
            },
            schema={
                "county_fips": pl.String,
                "housing_valid_months": pl.Int64,
                "median_active_listings": pl.Float64,
                "median_ppsf": pl.Float64,
                "sqft_for_1m_t12": pl.Float64,
            },
        )
    sidecar = assemble_ranking_sidecar(bundle, county_scored, housing)
    if "preference_fit" in sidecar.columns or sidecar.columns != RANKING_SIDECAR_COLUMNS:
        raise HouseHunterError("Ranking sidecar must not persist a blended score")
    return sidecar, {
        "available": sidecar.height > 0,
        "methodology_id": METHODOLOGY_ID,
        "calibration_id": bundle.manifest["calibration_id"],
        "calibration_hash": bundle.manifest["calibration_hash"],
        "scope": bundle.manifest["scope"],
        "vintages": bundle.manifest.get("vintages") or {},
        "row_count": sidecar.height,
        "error": None,
    }


def _layer_descriptors(
    *,
    config: dict[str, object],
    mountain_identity: dict[str, str],
    optional: dict[str, object],
) -> list[dict[str, object]]:
    fema = config["fema"]
    chrr = config["chrr"]
    bea = optional["bea_source"]
    housing_identity = optional["housing_identity"]
    enrichment = optional.get("enrichment_availability", {})
    cost_available = bool(enrichment.get("cost_of_living", False))
    housing_available = bool(enrichment.get("housing_stock", False))
    market_available = bool(enrichment.get("home_market", False))
    home_manifest = optional["home_manifest"]
    home_availability = "available" if market_available else "unavailable"
    home_vintage = (
        f"Realtor.com {home_manifest['month']}; ACS 2024 five-year"
        if isinstance(home_manifest, dict)
        else "Realtor.com unavailable; ACS 2024 five-year"
    )
    return [
        {
            "key": "residential-hazard",
            "display_name": "Residential Hazard Exposure",
            "source": "FEMA National Risk Index",
            "direction": "higher",
            "availability": "available",
            "vintage": str(fema["version"]),
            "geography": "FEMA tract or county",
            "attribution": str(fema["item_url"]),
            "notice": METHODOLOGY_NOTICE,
        },
        {
            "key": "community-conditions",
            "display_name": "Community Conditions",
            "source": "County Health Rankings & Roadmaps",
            "direction": "lower",
            "availability": "available",
            "vintage": str(chrr["version"]),
            "geography": "County; inherited by tracts",
            "attribution": str(chrr["item_url"]),
            "notice": "Groups are source-published county Community Conditions groups.",
        },
        {
            "key": "mountain",
            "display_name": "Mountain Magnitude",
            "source": "HouseHunter",
            "direction": "higher",
            "availability": (
                "unavailable" if mountain_identity["release_id"] == "unavailable" else "available"
            ),
            "vintage": mountain_identity["data_release"],
            "geography": "HouseHunter tract and county aggregates",
            "attribution": "HouseHunter Mountain Magnitude methodology",
            "notice": "Higher values indicate greater nearby mountain magnitude.",
        },
        {
            "key": "cost-of-living",
            "display_name": "Cost of Living",
            "source": "BEA Regional Price Parities",
            "direction": "lower",
            "availability": ("available" if cost_available else "unavailable"),
            "vintage": (str(bea["release_year"]) if isinstance(bea, dict) else "unavailable"),
            "geography": (
                "BEA MSA or U.S. nonmetropolitan portion; inherited by tracts; "
                f"county-to-MSA assignment uses {housing_identity['omb_delineation']}"
            ),
            "attribution": COST_OF_LIVING_ATTRIBUTION,
            "notice": "U.S. = 100. Lower values indicate lower regional prices.",
        },
        {
            "key": "home-costs",
            "display_name": "Home Costs",
            "source": "Realtor.com Research Data and U.S. Census Bureau ACS",
            "direction": "higher",
            "availability": home_availability,
            "vintage": home_vintage,
            "geography": (
                "Market: county, inherited by tracts; housing stock: direct tract and "
                "county estimates"
            ),
            "attribution": (f"{optional['home_attribution']}; {HOUSING_STOCK_ATTRIBUTION}"),
            "notice": (
                f"{HOME_MARKET_METHODOLOGY_NOTICE} {optional['home_usage_notice']} "
                f"Housing-stock context is "
                f"{'available' if housing_available else 'unavailable'}."
            ),
        },
    ]


def logical_checksum(frame: pl.DataFrame, columns: list[str], sort_by: list[str]) -> str:
    rows = frame.select(columns).sort(sort_by).iter_rows()
    return sha256_bytes(canonical_json([list(row) for row in rows]))


def _cancelled(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise InterruptedError("Build cancelled")


def _secure_snapshot_artifacts(target: Path) -> None:
    """Keep local-only snapshot data private to the current OS account."""
    if target.is_symlink() or not target.is_dir():
        raise HouseHunterError(f"Snapshot directory is not a private directory: {target}")
    target.chmod(0o700)
    for artifact in target.iterdir():
        if artifact.is_symlink() or not artifact.is_file():
            raise HouseHunterError(f"Unexpected snapshot artifact: {artifact}")
        artifact.chmod(0o600)


def _county_display_expr() -> pl.Expr:
    name = pl.col("county").str.strip_chars().fill_null("")
    kind = pl.col("county_type").str.strip_chars().fill_null("")
    generic = (kind == "") | (kind.str.to_lowercase() == "county")
    return (
        pl.when(name == "")
        .then(pl.lit(UNKNOWN_COUNTY_NAME))
        .when(generic)
        .then(name)
        .otherwise(pl.concat_str([name, pl.lit(" "), kind]))
    )


def _attach_mountain_release(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    current: tuple[Path, dict[str, object], pl.DataFrame, pl.DataFrame] | None,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, str]]:
    from .mountain import AGGREGATE_MEANS, IN_SCOPE_STATES

    def unavailable_status() -> pl.Expr:
        return (
            pl.when(pl.col("state").is_in(sorted(IN_SCOPE_STATES)))
            .then(pl.lit("unavailable"))
            .otherwise(pl.lit("outside_scope"))
            .alias("mountain_coverage_status")
        )

    if current is None:
        numeric = [
            pl.lit(None, dtype=pl.Float64).alias(column)
            for column in [*AGGREGATE_MEANS, "mountain_magnitude"]
        ]
        missing = [
            *numeric,
            pl.lit(None, dtype=pl.String).alias("mountain_magnitude_version"),
            pl.lit(None, dtype=pl.String).alias("mountain_pipeline_version"),
            pl.lit(0.0).alias("mountain_population_coverage"),
        ]
        return (
            places.with_columns(*missing, unavailable_status()),
            counties.with_columns(*missing, unavailable_status()),
            {
                "checksum": sha256_bytes(b"unavailable"),
                "data_release": "unavailable",
                "magnitude_version": "unavailable",
                "release_id": "unavailable",
            },
        )
    release, manifest, mountain_tracts, mountain_counties = current

    def reconcile_connecticut_tracts(frame: pl.DataFrame, mountain: pl.DataFrame) -> pl.DataFrame:
        targets = frame.filter(pl.col("state") == "CT").select(
            pl.col("place_id").alias("_target_id"),
            pl.col("place_id").str.slice(-6).alias("_tract_code"),
        )
        if targets.is_empty():
            return mountain
        sources = mountain.filter(pl.col("place_id").str.starts_with("09")).with_columns(
            pl.col("place_id").str.slice(-6).alias("_tract_code")
        )
        mapped = sources.join(targets, on="_tract_code", how="inner")
        unmatched = sources.join(targets, on="_tract_code", how="anti")
        if (
            targets["_tract_code"].n_unique() != targets.height
            or mapped["_tract_code"].n_unique() != mapped.height
            or mapped.height != targets.height
        ):
            raise HouseHunterError("Mountain Connecticut tract reconciliation is ambiguous")
        if (
            set(unmatched["place_id"]) != _CONNECTICUT_UNMATCHED_ZERO_POPULATION_TRACTS
            or unmatched.filter(
                pl.col("mountain_coverage_status").is_null()
                | (pl.col("mountain_coverage_status") != "zero_population")
                | pl.col("mountain_population_coverage").is_null()
                | (pl.col("mountain_population_coverage") != 0)
                | pl.col("mountain_magnitude").is_not_null()
            ).height
        ):
            raise HouseHunterError("Mountain Connecticut tract exceptions differ")
        return pl.concat(
            [
                mountain.filter(~pl.col("place_id").str.starts_with("09")),
                mapped.drop("place_id", "_tract_code")
                .rename({"_target_id": "place_id"})
                .select(mountain.columns),
            ]
        )

    def attach(
        frame: pl.DataFrame,
        mountain: pl.DataFrame,
        *,
        reconcile_ct: bool = False,
        allow_missing_states: frozenset[str] = frozenset(),
    ) -> pl.DataFrame:
        if reconcile_ct:
            mountain = reconcile_connecticut_tracts(frame, mountain)
        expected = frame.filter(
            pl.col("state").is_in(sorted(IN_SCOPE_STATES - allow_missing_states))
        ).select("place_id")
        missing_ids = expected.join(mountain.select("place_id"), on="place_id", how="anti")
        if missing_ids.height:
            raise HouseHunterError(
                f"Mountain compact release is missing {missing_ids.height} in-scope runtime rows"
            )
        return (
            frame.join(
                mountain.with_columns(pl.lit(True).alias("_mountain_present")),
                on="place_id",
                how="left",
            )
            .with_columns(
                pl.when(~pl.col("state").is_in(sorted(IN_SCOPE_STATES)))
                .then(pl.lit("outside_scope"))
                .when(pl.col("_mountain_present").is_null())
                .then(pl.lit("unavailable"))
                .otherwise(pl.col("mountain_coverage_status"))
                .alias("mountain_coverage_status"),
                pl.col("mountain_population_coverage").fill_null(0.0),
            )
            .drop("_mountain_present")
        )

    return (
        attach(places, mountain_tracts, reconcile_ct=True),
        attach(counties, mountain_counties, allow_missing_states=frozenset({"CT"})),
        {
            "checksum": sha256_bytes(str(manifest["release_id"]).encode()),
            "data_release": str(manifest["data_release"]),
            "magnitude_version": str(manifest["magnitude_version"]),
            "release_id": str(manifest["release_id"]),
        },
    )


def _attach_current_mountain(
    places: pl.DataFrame,
    counties: pl.DataFrame,
    paths: RuntimePaths,
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, str]]:
    from .mountain import current_compact_release

    return _attach_mountain_release(places, counties, current_compact_release(paths))


def compute_scores(
    fema: pl.DataFrame,
    counties: pl.DataFrame | None = None,
    chrr: pl.DataFrame | None = None,
    *,
    fema_vintage: str = "December 2025",
    chrr_release_year: int = 2025,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    if counties is None:
        counties = pl.DataFrame(
            {
                "county_fips": [],
                "county": [],
                "county_type": [],
                "state": [],
                "alr_npctl": [],
                "alr_valb": [],
                "nri_version": [],
            },
            schema={
                "county_fips": pl.String,
                "county": pl.String,
                "county_type": pl.String,
                "state": pl.String,
                "alr_npctl": pl.Float64,
                "alr_valb": pl.Float64,
                "nri_version": pl.String,
            },
        )
    if chrr is None:
        chrr = pl.DataFrame(
            {"county_fips": [], "community_conditions_group": []},
            schema={"county_fips": pl.String, "community_conditions_group": pl.Int8},
        )
    fema = score_residential_hazards(with_hazard_columns(fema))
    counties = score_residential_hazards(with_hazard_columns(counties))
    hazard_cols = [pl.col(column) for column in HAZARD_SNAPSHOT_COLUMNS]
    stripped_state = pl.col("state").str.strip_chars()
    county_state = (
        pl.when(stripped_state != "")
        .then(stripped_state.str.to_uppercase())
        .otherwise(
            pl.col("county_fips")
            .str.slice(0, 2)
            .replace_strict(STATE_BY_FIPS, default=UNKNOWN_STATE)
        )
    )
    county_scored = (
        counties.select(
            pl.col("county_fips").alias("place_id"),
            _county_display_expr().alias("name"),
            county_state.alias("state"),
            pl.lit("county").alias("place_type"),
            pl.lit(0, dtype=pl.Int64).alias("population_2020"),
            pl.lit(0, dtype=pl.Int64).alias("housing_units_2020"),
            "res_hazard_npctl",
            "res_hazard_spread",
            "res_hazard_spectral",
            "res_hazard_tail",
            "res_hazard_power4",
            "property_loss_npctl",
            "res_hazard_data_quality",
            "res_hazard_available_count",
            "res_hazard_coverage_ratio",
            "alr_npctl",
            "alr_valb",
            pl.lit(fema_vintage).alias("fema_vintage"),
            pl.lit("n/a").alias("census_vintage"),
            pl.col("county_fips"),
            _county_display_expr().alias("county_name"),
            *hazard_cols,
        )
        .join(
            chrr.select("county_fips", "community_conditions_group"),
            on="county_fips",
            how="left",
        )
        .with_columns(
            pl.lit("county").alias("community_conditions_geography"),
            pl.lit(chrr_release_year, dtype=pl.Int16).alias("chrr_release_year"),
        )
        .sort("place_id")
    )
    lookup = county_scored.select(
        pl.col("place_id").alias("matched_county_fips"),
        pl.col("name").alias("matched_county_name"),
        "community_conditions_group",
    )
    tract_state = (
        pl.col("tract_id").str.slice(0, 2).replace_strict(STATE_BY_FIPS, default=UNKNOWN_STATE)
    )
    scored = (
        fema.with_columns(pl.col("tract_id").str.slice(0, 5).alias("prefix"))
        .join(lookup, left_on="prefix", right_on="matched_county_fips", how="left")
        .select(
            pl.col("tract_id").alias("place_id"),
            pl.col("tract_id").alias("name"),
            tract_state.alias("state"),
            pl.lit("tract").alias("place_type"),
            pl.lit(0, dtype=pl.Int64).alias("population_2020"),
            pl.lit(0, dtype=pl.Int64).alias("housing_units_2020"),
            "res_hazard_npctl",
            "res_hazard_spread",
            "res_hazard_spectral",
            "res_hazard_tail",
            "res_hazard_power4",
            "property_loss_npctl",
            "res_hazard_data_quality",
            "res_hazard_available_count",
            "res_hazard_coverage_ratio",
            "alr_npctl",
            "alr_valb",
            pl.lit(fema_vintage).alias("fema_vintage"),
            pl.lit("n/a").alias("census_vintage"),
            pl.when(pl.col("matched_county_name").is_null())
            .then(pl.lit(UNKNOWN_COUNTY_FIPS))
            .otherwise(pl.col("prefix"))
            .alias("county_fips"),
            pl.when(pl.col("matched_county_name").is_null())
            .then(pl.lit(UNKNOWN_COUNTY_NAME))
            .otherwise(pl.col("matched_county_name"))
            .alias("county_name"),
            pl.col("community_conditions_group"),
            pl.lit("county").alias("community_conditions_geography"),
            pl.lit(chrr_release_year, dtype=pl.Int16).alias("chrr_release_year"),
            *hazard_cols,
        )
        .sort("place_id")
    )
    return scored, county_scored


def _write_duckdb(
    path: Path,
    tables: dict[str, Path],
    metadata: dict[str, object],
) -> None:
    connection = duckdb.connect(str(path))
    try:
        for table, parquet in tables.items():
            connection.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_parquet(?)", [str(parquet)]
            )
        connection.execute("CREATE INDEX places_id_idx ON places(place_id)")
        connection.execute("CREATE INDEX places_state_idx ON places(state)")
        connection.execute("CREATE INDEX places_county_idx ON places(county_fips)")
        connection.execute("CREATE INDEX counties_id_idx ON counties(place_id)")
        connection.execute("CREATE INDEX counties_state_idx ON counties(state)")
        connection.execute("CREATE INDEX chrr_county_id_idx ON chrr_county(county_fips)")
        connection.execute(
            "CREATE INDEX cost_of_living_id_idx ON cost_of_living(cost_of_living_geography_id)"
        )
        connection.execute("CREATE INDEX home_market_id_idx ON home_market(county_fips)")
        connection.execute(
            "CREATE INDEX housing_stock_tract_id_idx ON housing_stock_tract(tract_id)"
        )
        connection.execute(
            "CREATE INDEX housing_stock_county_id_idx ON housing_stock_county(county_fips)"
        )
        if "ranking_counties" in tables:
            connection.execute(
                "CREATE INDEX ranking_counties_id_idx ON ranking_counties(place_id)"
            )
        connection.execute(
            "CREATE TABLE build_metadata AS SELECT ? AS metadata_json",
            [json.dumps(metadata, sort_keys=True)],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def _snapshot_signature(target: Path) -> tuple[tuple[int, int, int, int, int], ...]:
    return tuple(
        (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        for filename in _SNAPSHOT_FILES
        for metadata in [(target / filename).stat()]
    )


def _hazard_values_are_valid(frame: pl.DataFrame) -> bool:
    percentile_columns = [hazard.percentile_column for hazard in HAZARDS]
    bounded_columns = [
        "res_hazard_npctl",
        "res_hazard_spread",
        "res_hazard_spectral",
        "res_hazard_tail",
        "res_hazard_power4",
        "property_loss_npctl",
        "alr_npctl",
        *percentile_columns,
    ]
    nonnegative_columns = ["alr_valb", *HAZARD_RAW_COLUMNS]
    available_count = pl.sum_horizontal(
        [pl.col(column).is_not_null().cast(pl.Int8) for column in percentile_columns]
    )
    expected_quality = (
        pl.when(available_count == len(HAZARDS))
        .then(pl.lit("complete"))
        .when(available_count > 0)
        .then(pl.lit("partial"))
        .otherwise(pl.lit("unavailable"))
    )
    invalid_bounded = pl.any_horizontal(
        [
            pl.col(column).is_not_null()
            & (
                ~pl.col(column).is_finite()
                | ~pl.col(column).is_between(0, 100, closed="both")
            )
            for column in bounded_columns
        ]
    )
    invalid_nonnegative = pl.any_horizontal(
        [
            pl.col(column).is_not_null()
            & (~pl.col(column).is_finite() | (pl.col(column) < 0))
            for column in nonnegative_columns
        ]
    )
    invalid_aggregate_nulls = pl.any_horizontal(
        [
            pl.col(column).is_null() != (available_count == 0)
            for column in (
                "res_hazard_npctl",
                "res_hazard_spread",
                "res_hazard_spectral",
                "res_hazard_tail",
                "res_hazard_power4",
            )
        ]
    )
    invalid_zero_percentile = pl.any_horizontal(
        [
            (pl.col(hazard.raw_column) == 0) & (pl.col(hazard.percentile_column) != 0)
            for hazard in HAZARDS
        ]
    )
    invalid_rows = frame.filter(
        pl.col("res_hazard_available_count").is_null()
        | pl.col("res_hazard_coverage_ratio").is_null()
        | (pl.col("res_hazard_available_count") != available_count)
        | (
            (
                pl.col("res_hazard_coverage_ratio")
                - available_count.cast(pl.Float64) / len(HAZARDS)
            ).abs()
            > 1e-12
        )
        | (pl.col("res_hazard_data_quality") != expected_quality)
        | invalid_bounded
        | invalid_nonnegative
        | invalid_aggregate_nulls
        | invalid_zero_percentile
    )
    return (
        invalid_rows.is_empty()
        and all(
            set(frame[column].drop_nulls().unique()).issubset(KNOWN_EAL_RATINGS)
            for column in HAZARD_RATING_COLUMNS
        )
    )


def _validate_snapshot_artifacts(target: Path) -> bool:
    try:
        metadata = json.loads((target / "build.json").read_text())
        frames = {
            table: pl.read_parquet(target / record[0]) for table, record in _SNAPSHOT_TABLES.items()
        }
    except (OSError, json.JSONDecodeError, pl.exceptions.PolarsError):
        return False
    places = frames["places"]
    counties = frames["counties"]
    chrr_counties = frames["chrr_county"]
    if metadata.get("schema_version") != BUILD_SCHEMA_VERSION:
        return False
    if not isinstance(metadata.get("mountain_release_id"), str) or not isinstance(
        metadata.get("mountain_magnitude_version"), str
    ):
        return False
    for frame in (places, counties):
        if (
            "mountain_magnitude" not in frame.columns
            or "mountain_magnitude_version" not in frame.columns
            or "mountain_score" in frame.columns
            or "mountain_score_version" in frame.columns
            or "risk_score" in frame.columns
            or "coverage_status" in frame.columns
            or "coverage_ratio" in frame.columns
            or not set(HAZARD_SNAPSHOT_COLUMNS).issubset(frame.columns)
            or not {
                "res_hazard_npctl",
                "res_hazard_spread",
                "res_hazard_spectral",
                "res_hazard_tail",
                "res_hazard_power4",
                "property_loss_npctl",
                "res_hazard_data_quality",
                "res_hazard_available_count",
                "res_hazard_coverage_ratio",
                "alr_npctl",
                "alr_valb",
            }.issubset(frame.columns)
            or not set(SUMMARY_DIMENSION_COLUMNS).issubset(frame.columns)
        ):
            return False
        if not _hazard_values_are_valid(frame):
            return False
        if (
            not set(frame["cost_of_living_coverage_status"].unique()).issubset(
                COST_OF_LIVING_COVERAGE_STATUSES
            )
            or not set(frame["home_costs_coverage_status"].unique()).issubset(
                HOME_COSTS_COVERAGE_STATUSES
            )
            or not set(frame["housing_stock_coverage_status"].unique()).issubset(
                HOUSING_STOCK_COVERAGE_STATUSES
            )
        ):
            return False
        try:
            MapScoreColumns.model_validate(
                frame.select(
                    "place_id",
                    "res_hazard_npctl",
                    "community_conditions_group",
                    "mountain_magnitude",
                    "cost_of_living_index",
                    "home_buying_power_percentile",
                    "home_sqft_for_1m",
                    "housing_built_2000_plus_pct",
                )
                .sort("place_id")
                .to_dict(as_series=False)
            )
        except ValueError:
            return False
    if metadata.get("place_count") != places.height:
        return False
    if metadata.get("county_count") != counties.height:
        return False
    if metadata.get("chrr_county_count") != chrr_counties.height:
        return False
    if metadata.get("normalized_source_counts") != {
        table: frames[table].height
        for table in (
            "cost_of_living",
            "home_market",
            "housing_stock_tract",
            "housing_stock_county",
            "housing_stock_county_msa",
        )
    }:
        return False
    ranking = frames["ranking_counties"]
    if ranking.columns != list(RANKING_SIDECAR_COLUMNS) or "preference_fit" in ranking.columns:
        return False
    home_market = frames["home_market"]
    if home_market.height:
        eligible = home_market.filter(pl.col("home_costs_coverage_status") == "complete")
        rejected = home_market.filter(pl.col("home_costs_coverage_status") != "complete")
        if (
            eligible.is_empty()
            or eligible.filter(
                pl.col("home_sqft_for_1m_unrounded").is_null()
                | pl.col("home_sqft_for_1m").is_null()
                | pl.col("home_buying_power_percentile").is_null()
            ).height
            or rejected.filter(
                pl.col("home_sqft_for_1m_unrounded").is_not_null()
                | pl.col("home_sqft_for_1m").is_not_null()
                | pl.col("home_buying_power_percentile").is_not_null()
            ).height
        ):
            return False
        expected = eligible.with_columns(
            (
                pl.col("home_sqft_for_1m_unrounded").rank(method="max") * 100.0 / eligible.height
            ).alias("_expected_percentile"),
            pl.col("home_sqft_for_1m_unrounded")
            .round(0)
            .cast(pl.Int64)
            .alias("_expected_square_feet"),
        )
        if expected.filter(
            ((pl.col("home_buying_power_percentile") - pl.col("_expected_percentile")).abs() > 1e-9)
            | (pl.col("home_sqft_for_1m") != pl.col("_expected_square_feet"))
        ).height:
            return False
    if (
        metadata.get("ranked_place_count")
        != places.filter(pl.col("res_hazard_npctl").is_not_null()).height
    ):
        return False
    if (
        metadata.get("ranked_county_count")
        != counties.filter(pl.col("res_hazard_npctl").is_not_null()).height
    ):
        return False
    checksums = metadata.get("logical_checksums", {})
    expected_checksums = {
        table: logical_checksum(frame, frame.columns, list(_SNAPSHOT_TABLES[table][1]))
        for table, frame in frames.items()
    }
    if checksums != expected_checksums:
        return False
    try:
        connection = duckdb.connect(str(target / "househunter.duckdb"), read_only=True)
        try:
            for table, (filename, _) in _SNAPSHOT_TABLES.items():
                parquet = str(target / filename)
                if (
                    connection.execute(f"DESCRIBE {table}").fetchall()
                    != connection.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)", [parquet]
                    ).fetchall()
                ):
                    return False
                differs = connection.execute(
                    f"SELECT EXISTS ("
                    f"(SELECT * FROM {table} EXCEPT ALL SELECT * FROM read_parquet(?)) "
                    f"UNION ALL "
                    f"(SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM {table})"
                    f")",
                    [parquet, parquet],
                ).fetchone()[0]
                if differs:
                    return False
            stored_metadata = json.loads(
                connection.execute("SELECT metadata_json FROM build_metadata").fetchone()[0]
            )
        finally:
            connection.close()
    except (duckdb.Error, json.JSONDecodeError, IndexError, TypeError):
        return False
    return stored_metadata == metadata


@lru_cache(maxsize=8)
def _cached_snapshot_artifacts_valid(
    target: str, signature: tuple[tuple[int, int, int, int, int], ...]
) -> bool:
    del signature
    return _validate_snapshot_artifacts(Path(target))


def snapshot_artifacts_are_valid(target: Path) -> bool:
    artifacts = [target / filename for filename in _SNAPSHOT_FILES]
    if any(not path.is_file() or path.is_symlink() for path in artifacts):
        return False
    try:
        return _cached_snapshot_artifacts_valid(str(target), _snapshot_signature(target))
    except OSError:
        return False


def _existing_build_is_valid(
    target: Path,
    build_id: str,
    input_hashes: dict[str, str],
    source_vintages: dict[str, str | int],
) -> bool:
    if not snapshot_artifacts_are_valid(target):
        return False
    try:
        metadata = json.loads((target / "build.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        metadata.get("build_id") == build_id
        and metadata.get("input_checksums") == input_hashes
        and metadata.get("source_vintages") == source_vintages
    )


def build_snapshot(
    paths: RuntimePaths,
    *,
    state: str | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    mountain_release: tuple[Path, dict[str, object], pl.DataFrame, pl.DataFrame]
    | None
    | object = _CURRENT_MOUNTAIN,
    publish: bool = True,
) -> Path:
    from .mountain import current_compact_release

    if paths.builds.is_symlink():
        raise HouseHunterError("Build directory cannot be a symlink")
    paths.ensure()
    state = state.upper() if state else None
    if state and state not in KNOWN_STATES:
        raise HouseHunterError(f"Unknown state abbreviation: {state}")
    _cancelled(cancelled)
    fema_path = paths.cache / "fema_nri_tracts.parquet"
    county_path = paths.cache / "fema_nri_counties.parquet"
    if not fema_path.is_file():
        raise HouseHunterError("FEMA data is not cached; run `househunter download --source fema`")
    if not county_path.is_file():
        raise HouseHunterError(
            "FEMA county data is not cached; run `househunter download --source fema_counties`"
        )
    config = load_config()
    source = config["fema"]
    county_source = config["fema_counties"]
    chrr_source = config["chrr"]
    fema, fema_sha = validate_cached_fema(fema_path, source)
    counties, county_sha = validate_cached_fema_counties(county_path, county_source)
    chrr_counties, chrr_sha = build_processed(paths)
    chrr_source_row_count = chrr_counties.height
    optional = _load_optional_dimension_inputs(paths, config)
    housing_stock_identity = optional["housing_identity"]
    input_hashes = {"fema": fema_sha, "fema_counties": county_sha, "chrr": chrr_sha}
    input_hashes["housing_stock"] = str(housing_stock_identity["checksum"])
    input_hashes["bea_rpp"] = str(optional["bea_digest"])
    input_hashes["home_market"] = str(optional["home_digest"])
    try:
        ranking_identity = ranking_bundle_identity(validate_ranking_assets().manifest)
    except (HouseHunterError, OSError, KeyError, TypeError, ValueError):
        ranking_identity = sha256_bytes(b"ranking-unavailable")
    input_hashes["ranking_v2"] = str(ranking_identity)
    bea_source_value = optional["bea_source"]
    bea_release = (
        bea_source_value["release_year"] if isinstance(bea_source_value, dict) else "unavailable"
    )
    home_manifest_value = optional["home_manifest"]
    home_release = (
        home_manifest_value["month"] if isinstance(home_manifest_value, dict) else "unavailable"
    )
    source_vintages = {
        "fema": source["version"],
        "fema_release": source["release"],
        "fema_counties": county_source["version"],
        "fema_counties_release": county_source["release"],
        "chrr": chrr_source["version"],
        "chrr_release_year": chrr_source["release_year"],
        "mountain_runtime_geography": MOUNTAIN_RUNTIME_GEOGRAPHY_VERSION,
        "housing_stock": housing_stock_identity["release_year"],
        "omb_delineation": housing_stock_identity["omb_delineation"],
        "bea_rpp": bea_release,
        "home_market": home_release,
    }
    if mountain_release is _CURRENT_MOUNTAIN:
        mountain_release = current_compact_release(paths)
    if mountain_release is not None and not isinstance(mountain_release, tuple):
        raise HouseHunterError("Explicit Mountain release is invalid")
    if mountain_release is None:
        mountain_identity = {
            "checksum": sha256_bytes(b"unavailable"),
            "data_release": "unavailable",
            "magnitude_version": "unavailable",
            "release_id": "unavailable",
        }
    else:
        mountain_path, mountain_manifest, _, _ = mountain_release
        mountain_identity = {
            "checksum": sha256_bytes(str(mountain_manifest["release_id"]).encode()),
            "data_release": str(mountain_manifest["data_release"]),
            "magnitude_version": str(mountain_manifest["magnitude_version"]),
            "release_id": str(mountain_manifest["release_id"]),
        }
    input_hashes["mountain"] = mountain_identity["checksum"]
    source_vintages["mountain"] = mountain_identity["data_release"]
    source_vintages["mountain_magnitude"] = mountain_identity["magnitude_version"]
    source_vintages["mountain_release_id"] = mountain_identity["release_id"]
    scope = state or "national"
    build_key = sha256_bytes(
        canonical_json(
            {
                "schema_version": BUILD_SCHEMA_VERSION,
                "scope": scope,
                "inputs": input_hashes,
                "source_vintages": source_vintages,
            }
        )
    )[:16]
    build_id = f"{scope.lower()}-{build_key}"
    target = paths.builds / build_id
    if target.is_dir():
        if not _existing_build_is_valid(target, build_id, input_hashes, source_vintages):
            raise HouseHunterError(
                f"Existing immutable build failed validation: {target}; move it aside and rebuild"
            )
        _secure_snapshot_artifacts(target)
        if publish:
            _publish_current(paths, target, build_id, scope)
        if progress:
            progress(100, "Using verified existing build")
        return target
    if progress:
        progress(25, "Ranking FEMA geographies")
    scored, county_scored = compute_scores(
        fema,
        counties,
        chrr_counties,
        fema_vintage=source["version"],
        chrr_release_year=chrr_source["release_year"],
    )
    scored, county_scored, attached_mountain = _attach_mountain_release(
        scored, county_scored, mountain_release
    )
    if attached_mountain != mountain_identity:
        raise HouseHunterError("Mountain release changed during snapshot build")
    scored, county_scored, enrichment_availability, enrichment_warnings = (
        attach_dimensions_fail_open(
            scored,
            county_scored,
            rpp=optional["rpp"],
            home=optional["home"],
            housing=optional["housing"],
            bea_source=optional["bea_source"],
            home_attribution=str(optional["home_attribution"]),
            home_usage_notice=str(optional["home_usage_notice"]),
        )
    )
    optional["enrichment_availability"] = enrichment_availability
    optional["warnings"].extend(enrichment_warnings)
    if state:
        scored = scored.filter(pl.col("state") == state)
        county_scored = county_scored.filter(pl.col("state") == state)
        chrr_counties = chrr_counties.filter(pl.col("state") == state)
        if scored.height == 0:
            raise HouseHunterError(f"Unknown state abbreviation: {state}")
    _cancelled(cancelled)
    complete = scored.filter(pl.col("res_hazard_npctl").is_not_null())
    county_complete = county_scored.filter(pl.col("res_hazard_npctl").is_not_null())
    source_tables = optional["source_tables"]
    ranking_counties, ranking_meta = _build_ranking_sidecar(paths, county_scored)
    snapshot_frames = {
        "places": scored,
        "counties": county_scored,
        "chrr_county": chrr_counties,
        **source_tables,
        "ranking_counties": ranking_counties,
    }
    checksums = {
        table: logical_checksum(
            frame,
            frame.columns,
            list(_SNAPSHOT_TABLES[table][1]),
        )
        for table, frame in snapshot_frames.items()
    }
    enrichment_availability = optional["enrichment_availability"]
    bea_available = bool(enrichment_availability["cost_of_living"])
    home_available = bool(enrichment_availability["home_market"])
    housing_available = bool(enrichment_availability["housing_stock"])
    bea_loaded = optional["rpp"] is not None
    home_loaded = optional["home"] is not None
    housing_loaded = optional["housing"] is not None
    home_manifest = optional["home_manifest"]
    source_descriptors = [
        {
            "source": "fema",
            "version": str(source["version"]),
            "release": str(source["release"]),
            "cached": True,
            "sha256": fema_sha,
            "row_count": fema.height,
            "stale": None,
            "attribution": str(source["item_url"]),
            "usage_notice": None,
            "coverage_status": "complete",
            "error": None,
        },
        {
            "source": "fema_counties",
            "version": str(county_source["version"]),
            "release": str(county_source["release"]),
            "cached": True,
            "sha256": county_sha,
            "row_count": counties.height,
            "stale": None,
            "attribution": str(county_source["item_url"]),
            "usage_notice": None,
            "coverage_status": "complete",
            "error": None,
        },
        {
            "source": "chrr",
            "version": str(chrr_source["version"]),
            "release": str(chrr_source["release"]),
            "cached": True,
            "sha256": chrr_sha,
            "row_count": chrr_source_row_count,
            "stale": None,
            "attribution": str(chrr_source["item_url"]),
            "usage_notice": None,
            "coverage_status": "complete",
            "error": None,
        },
        {
            "source": "bea_rpp",
            "version": (
                str(bea_source_value["version"])
                if isinstance(bea_source_value, dict)
                else "unavailable"
            ),
            "release": bea_release,
            "cached": bea_loaded,
            "sha256": str(optional["bea_digest"]) if bea_loaded else None,
            "row_count": optional["rpp"].height if bea_loaded else None,
            "stale": None,
            "attribution": COST_OF_LIVING_ATTRIBUTION,
            "usage_notice": None,
            "coverage_status": "complete" if bea_available else "source_unavailable",
            "error": optional["bea_error"],
        },
        {
            "source": "home_market",
            "version": home_release,
            "release": home_release if home_available else None,
            "cached": home_loaded,
            "sha256": (
                str(home_manifest["source_sha256"]) if isinstance(home_manifest, dict) else None
            ),
            "row_count": optional["home"].height if home_loaded else None,
            "stale": optional["home_stale"],
            "attribution": str(optional["home_attribution"]),
            "usage_notice": str(optional["home_usage_notice"]),
            "coverage_status": "complete" if home_available else "source_unavailable",
            "error": optional["home_error"],
        },
        {
            "source": "housing_stock",
            "version": (
                str(housing_stock_identity["release_year"]) if housing_loaded else "unavailable"
            ),
            "release": (housing_stock_identity["release_year"] if housing_loaded else None),
            "cached": housing_loaded,
            "sha256": (str(housing_stock_identity["checksum"]) if housing_loaded else None),
            "row_count": (
                optional["source_tables"]["housing_stock_tract"].height if housing_loaded else None
            ),
            "stale": None,
            "attribution": HOUSING_STOCK_ATTRIBUTION,
            "usage_notice": None,
            "coverage_status": "complete" if housing_available else "asset_unavailable",
            "error": None,
        },
    ]
    metadata: dict[str, object] = {
        "schema_version": BUILD_SCHEMA_VERSION,
        "build_id": build_id,
        "scope": {"kind": "state" if state else "national", "state": state},
        "created_at": datetime.now(UTC).isoformat(),
        "place_count": scored.height,
        "ranked_place_count": complete.height,
        "county_count": county_scored.height,
        "ranked_county_count": county_complete.height,
        "chrr_county_count": chrr_counties.height,
        "chrr_grouped_count": chrr_counties.filter(
            pl.col("community_conditions_group").is_not_null()
        ).height,
        "normalized_source_counts": {
            table: snapshot_frames[table].height
            for table in (
                "cost_of_living",
                "home_market",
                "housing_stock_tract",
                "housing_stock_county",
                "housing_stock_county_msa",
            )
        },
        "source_vintages": source_vintages,
        "input_checksums": input_hashes,
        "logical_checksums": checksums,
        "methodology_notice": METHODOLOGY_NOTICE,
        "county_methodology_notice": COUNTY_METHODOLOGY_NOTICE,
        "mountain_release_id": mountain_identity["release_id"],
        "mountain_magnitude_version": mountain_identity["magnitude_version"],
        "housing_stock_reference": housing_stock_identity,
        "layers": _layer_descriptors(
            config=config, mountain_identity=mountain_identity, optional=optional
        ),
        "ranking": ranking_meta,
        "sources": source_descriptors,
        "detail_notices": [
            HOME_MARKET_METHODOLOGY_NOTICE,
            str(optional["home_usage_notice"]),
        ],
        "optional_source_warnings": optional["warnings"],
    }
    temporary = paths.builds / f".{build_id}.{os.getpid()}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(mode=0o700)
    try:
        table_paths: dict[str, Path] = {}
        for table, frame in snapshot_frames.items():
            output = temporary / _SNAPSHOT_TABLES[table][0]
            frame.write_parquet(output, compression="zstd", statistics=True)
            output.chmod(0o600)
            table_paths[table] = output
        metadata_path = temporary / "build.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        metadata_path.chmod(0o600)
        if progress:
            progress(75, "Creating read-only query snapshot")
        _write_duckdb(
            temporary / "househunter.duckdb",
            table_paths,
            metadata,
        )
        (temporary / "househunter.duckdb").chmod(0o600)
        _cancelled(cancelled)
        os.replace(temporary, target)
        _secure_snapshot_artifacts(target)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    if publish:
        _publish_current(paths, target, build_id, scope)
    if progress:
        progress(100, "Build published")
    return target


def _publish_current(paths: RuntimePaths, target: Path, build_id: str, scope: str) -> None:
    pointer = {
        "schema_version": BUILD_SCHEMA_VERSION,
        "build_id": build_id,
        "scope": scope,
        "path": str(target),
    }
    atomic_write_json(paths.current, pointer)


def publish_snapshot(paths: RuntimePaths, target: Path) -> None:
    """Validate and atomically publish an already staged schema-10 snapshot."""
    if (
        paths.builds.is_symlink()
        or target.is_symlink()
        or not target.resolve().is_relative_to(paths.builds.resolve())
    ):
        raise HouseHunterError("Mountain migration snapshot escapes the build directory")
    if not snapshot_artifacts_are_valid(target):
        raise HouseHunterError("Mountain migration snapshot failed validation")
    try:
        metadata = json.loads((target / "build.json").read_text())
        build_id = str(metadata["build_id"])
        scope_payload = metadata["scope"]
        scope = str(scope_payload["state"] or "national")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Mountain migration snapshot metadata is invalid: {exc}") from exc
    if metadata.get("schema_version") != BUILD_SCHEMA_VERSION or target.name != build_id:
        raise HouseHunterError("Mountain migration snapshot identity is invalid")
    _publish_current(paths, target, build_id, scope)
