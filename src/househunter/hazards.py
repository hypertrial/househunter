"""Residential building-loss hazard inputs and deterministic exposure scoring."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import polars as pl

SPECTRAL_LAMBDA = 0.45
VALID_EAL_RATINGS = frozenset(
    {
        "Very Low",
        "Relatively Low",
        "Relatively Moderate",
        "Relatively High",
        "Very High",
    }
)
NO_EXPECTED_ANNUAL_LOSSES = "No Expected Annual Losses"
NOT_APPLICABLE = "Not Applicable"
INSUFFICIENT_DATA = "Insufficient Data"
KNOWN_EAL_RATINGS = VALID_EAL_RATINGS | {
    NO_EXPECTED_ANNUAL_LOSSES,
    NOT_APPLICABLE,
    INSUFFICIENT_DATA,
}


@dataclass(frozen=True, slots=True)
class Hazard:
    code: str
    label: str

    @property
    def alrb_field(self) -> str:
        return f"{self.code}_ALRB"

    @property
    def rating_field(self) -> str:
        return f"{self.code}_EALR"

    @property
    def raw_column(self) -> str:
        return f"alrb_{self.code.lower()}"

    @property
    def rating_column(self) -> str:
        return f"ealr_{self.code.lower()}"

    @property
    def percentile_column(self) -> str:
        return f"alrb_npctl_{self.code.lower()}"


HAZARDS: tuple[Hazard, ...] = (
    Hazard("AVLN", "Avalanche"),
    Hazard("CFLD", "Coastal Flooding"),
    Hazard("CWAV", "Cold Wave"),
    Hazard("ERQK", "Earthquake"),
    Hazard("HAIL", "Hail"),
    Hazard("HWAV", "Heat Wave"),
    Hazard("HRCN", "Hurricane"),
    Hazard("ISTM", "Ice Storm"),
    Hazard("LNDS", "Landslide"),
    Hazard("LTNG", "Lightning"),
    Hazard("IFLD", "Inland Flooding"),
    Hazard("SWND", "Strong Wind"),
    Hazard("TRND", "Tornado"),
    Hazard("TSUN", "Tsunami"),
    Hazard("VLCN", "Volcanic Activity"),
    Hazard("WFIR", "Wildfire"),
    Hazard("WNTW", "Winter Weather"),
)

HAZARD_RAW_COLUMNS: tuple[str, ...] = tuple(hazard.raw_column for hazard in HAZARDS)
HAZARD_RATING_COLUMNS: tuple[str, ...] = tuple(hazard.rating_column for hazard in HAZARDS)
HAZARD_PERCENTILE_COLUMNS: tuple[str, ...] = tuple(
    hazard.percentile_column for hazard in HAZARDS
)
# Detail code historically imports HAZARD_COLUMNS for the displayed percentiles.
HAZARD_COLUMNS = HAZARD_PERCENTILE_COLUMNS
HAZARD_SOURCE_COLUMNS: tuple[str, ...] = tuple(
    column
    for hazard in HAZARDS
    for column in (hazard.raw_column, hazard.rating_column)
)
HAZARD_SNAPSHOT_COLUMNS: tuple[str, ...] = tuple(
    column
    for hazard in HAZARDS
    for column in (hazard.raw_column, hazard.rating_column, hazard.percentile_column)
)
FEMA_HAZARD_FIELDS: dict[str, str] = {
    field: kind
    for hazard in HAZARDS
    for field, kind in (
        (hazard.alrb_field, "esriFieldTypeDouble"),
        (hazard.rating_field, "esriFieldTypeString"),
    )
}


def tract_out_fields() -> str:
    return ",".join(
        ["TRACTFIPS", "ALR_NPCTL", "ALR_VALB", "NRI_VER", *FEMA_HAZARD_FIELDS]
    )


def county_out_fields() -> str:
    return ",".join(
        [
            "STCOFIPS",
            "COUNTY",
            "COUNTYTYPE",
            "STATEABBRV",
            "ALR_NPCTL",
            "ALR_VALB",
            "NRI_VER",
            *FEMA_HAZARD_FIELDS,
        ]
    )


def hazard_schema() -> dict[str, pl.DataType]:
    return {
        column: dtype
        for hazard in HAZARDS
        for column, dtype in (
            (hazard.raw_column, pl.Float64),
            (hazard.rating_column, pl.String),
        )
    }


def hazard_values_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        column: value
        for hazard in HAZARDS
        for column, value in (
            (hazard.raw_column, row.get(hazard.alrb_field)),
            (hazard.rating_column, row.get(hazard.rating_field)),
        )
    }


def normalized_hazard_values_from_row(row: dict[str, Any]) -> dict[str, Any]:
    values = hazard_values_from_row(row)
    for hazard in HAZARDS:
        values[hazard.raw_column] = normalized_hazard_raw_value(
            values[hazard.raw_column], values[hazard.rating_column]
        )
    return values


def hazard_fields_from_cached_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        field: value
        for hazard in HAZARDS
        for field, value in (
            (hazard.alrb_field, row.get(hazard.raw_column)),
            (hazard.rating_field, row.get(hazard.rating_column)),
        )
    }


def with_hazard_columns(frame: pl.DataFrame) -> pl.DataFrame:
    missing: list[pl.Expr] = []
    for hazard in HAZARDS:
        if hazard.raw_column not in frame.columns:
            missing.append(pl.lit(None, dtype=pl.Float64).alias(hazard.raw_column))
        if hazard.rating_column not in frame.columns:
            missing.append(pl.lit(None, dtype=pl.String).alias(hazard.rating_column))
        if hazard.percentile_column not in frame.columns:
            missing.append(pl.lit(None, dtype=pl.Float64).alias(hazard.percentile_column))
    widened = frame.with_columns(missing) if missing else frame
    return widened.with_columns(
        [pl.col(column).cast(pl.Float64) for column in HAZARD_RAW_COLUMNS]
        + [pl.col(column).cast(pl.String) for column in HAZARD_RATING_COLUMNS]
        + [pl.col(column).cast(pl.Float64) for column in HAZARD_PERCENTILE_COLUMNS]
    )


def logical_hazard_values(row: dict[str, Any]) -> list[Any]:
    return [row.get(column) for column in HAZARD_SOURCE_COLUMNS]


def optional_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def optional_percentile(value: Any) -> float | None:
    number = optional_number(value)
    return number if number is not None and 0 <= number <= 100 else None


def hazard_availability(raw_value: Any, rating: Any) -> str:
    """Classify one FEMA ALRB/EALR pair without hiding contradictions."""

    raw = optional_number(raw_value)
    if rating in VALID_EAL_RATINGS:
        return "valid" if raw is not None and raw >= 0 else "invalid"
    if rating == NO_EXPECTED_ANNUAL_LOSSES:
        return "valid" if raw == 0 else "invalid"
    if rating == NOT_APPLICABLE:
        return "not_applicable" if raw_value is None else "invalid"
    if rating == INSUFFICIENT_DATA:
        return "missing" if raw_value is None else "invalid"
    # Direct scoring fixtures may omit EALR. Pinned-source validation does not.
    if rating is None:
        if raw_value is None:
            return "missing"
        return "valid" if raw is not None and raw >= 0 else "invalid"
    return "invalid"


def normalized_hazard_raw_value(raw_value: Any, rating: Any) -> float | None:
    """Normalize ALRB without erasing evidence of a contradictory status pair."""

    state = hazard_availability(raw_value, rating)
    number = optional_number(raw_value)
    if state == "valid":
        return number
    if state == "invalid" and hazard_availability(None, rating) != "invalid":
        return number
    return None


def validate_hazard_source_values(rows: list[dict[str, Any]], source: dict[str, Any]) -> None:
    """Fail on source-type/status drift while leaving known bad pairs flaggable."""

    required_fields = source.get("fields", {})
    for row in rows:
        for hazard in HAZARDS:
            require_hazard_contract = (
                hazard.alrb_field in required_fields or hazard.rating_field in required_fields
            )
            raw_present = hazard.alrb_field in row
            rating_present = hazard.rating_field in row
            raw = row.get(hazard.alrb_field)
            rating = row.get(hazard.rating_field)
            if require_hazard_contract and (not raw_present or not rating_present):
                raise ValueError(f"FEMA row is missing {hazard.alrb_field}/{hazard.rating_field}")
            if raw is not None and (isinstance(raw, bool) or type(raw) not in {int, float}):
                raise TypeError(f"FEMA {hazard.alrb_field} must be a JSON number or null")
            if raw is not None and not math.isfinite(float(raw)):
                raise ValueError(f"FEMA {hazard.alrb_field} must be finite or null")
            if rating is not None and not isinstance(rating, str):
                raise TypeError(f"FEMA {hazard.rating_field} must be a JSON string or null")
            if require_hazard_contract and rating not in KNOWN_EAL_RATINGS:
                raise ValueError(f"FEMA {hazard.rating_field} has unknown status {rating!r}")


def percentile_ranks(
    values: Sequence[float | None], *, force_zero: bool = False
) -> list[float | None]:
    """Return deterministic average-tie percentiles on the non-null universe."""

    ranked = sorted((value, index) for index, value in enumerate(values) if value is not None)
    result: list[float | None] = [None] * len(values)
    count = len(ranked)
    position = 0
    while position < count:
        end = position + 1
        while end < count and ranked[end][0] == ranked[position][0]:
            end += 1
        average_rank = ((position + 1) + end) / 2.0
        percentile = 0.0 if count <= 1 else 100.0 * (average_rank - 1.0) / (count - 1)
        for value, original_index in ranked[position:end]:
            result[original_index] = 0.0 if force_zero and value == 0 else percentile
        position = end
    return result


def raw_hazard_aggregates(
    percentiles: Sequence[float | None],
) -> tuple[float, float, float] | None:
    """Compute spectral, worst-quartile, and fourth-power raw aggregates."""

    usable = sorted((value for value in percentiles if value is not None), reverse=True)
    if not usable:
        return None
    weights = [math.exp(-SPECTRAL_LAMBDA * index) for index in range(len(usable))]
    weight_total = sum(weights)
    spectral = sum(weight * value for weight, value in zip(weights, usable, strict=True))
    spectral /= weight_total
    tail_count = max(1, math.ceil(len(usable) * 0.25))
    tail = sum(usable[:tail_count]) / tail_count
    power4 = 100.0 * (sum((value / 100.0) ** 4 for value in usable) / len(usable)) ** 0.25
    return spectral, tail, power4


def score_residential_hazards(frame: pl.DataFrame) -> pl.DataFrame:
    """Add building-specific hazard percentiles and ensemble outputs to one grain."""

    scored = with_hazard_columns(frame)
    records = list(scored.iter_rows(named=True))
    percentile_values: dict[str, list[float | None]] = {}
    sanitized_raw: dict[str, list[float | None]] = {}

    for hazard in HAZARDS:
        ranking_input: list[float | None] = []
        availability: list[str] = []
        raw_output: list[float | None] = []
        for row in records:
            raw_value = row[hazard.raw_column]
            state = hazard_availability(raw_value, row[hazard.rating_column])
            number = optional_number(raw_value)
            availability.append(state)
            ranking_input.append(number if state == "valid" else None)
            raw_output.append(
                normalized_hazard_raw_value(raw_value, row[hazard.rating_column])
            )
        ranked = percentile_ranks(ranking_input, force_zero=True)
        percentile_values[hazard.percentile_column] = [
            0.0 if state == "not_applicable" else value
            for state, value in zip(availability, ranked, strict=True)
        ]
        sanitized_raw[hazard.raw_column] = raw_output

    row_percentiles = [
        [percentile_values[hazard.percentile_column][index] for hazard in HAZARDS]
        for index in range(scored.height)
    ]
    raw_aggregates = [raw_hazard_aggregates(values) for values in row_percentiles]
    spectral = [values[0] if values is not None else None for values in raw_aggregates]
    tail = [values[1] if values is not None else None for values in raw_aggregates]
    power4 = [values[2] if values is not None else None for values in raw_aggregates]
    spectral_pct = percentile_ranks(spectral)
    tail_pct = percentile_ranks(tail)
    power4_pct = percentile_ranks(power4)
    ensemble_raw = [
        sorted((first, second, third))[1]
        if first is not None and second is not None and third is not None
        else None
        for first, second, third in zip(spectral_pct, tail_pct, power4_pct, strict=True)
    ]
    final = percentile_ranks(ensemble_raw)
    spread = [
        max(first, second, third) - min(first, second, third)
        if first is not None and second is not None and third is not None
        else None
        for first, second, third in zip(spectral_pct, tail_pct, power4_pct, strict=True)
    ]
    available_count = [sum(value is not None for value in values) for values in row_percentiles]
    quality = [
        "complete" if count == len(HAZARDS) else "partial" if count else "unavailable"
        for count in available_count
    ]
    property_loss_input = [
        number
        if (number := optional_number(row.get("alr_valb"))) is not None and number >= 0
        else None
        for row in records
    ]

    return scored.with_columns(
        [pl.Series(column, values, dtype=pl.Float64) for column, values in sanitized_raw.items()]
        + [
            pl.Series(column, values, dtype=pl.Float64)
            for column, values in percentile_values.items()
        ]
        + [
            pl.Series("res_hazard_spectral", spectral, dtype=pl.Float64),
            pl.Series("res_hazard_tail", tail, dtype=pl.Float64),
            pl.Series("res_hazard_power4", power4, dtype=pl.Float64),
            pl.Series("res_hazard_npctl", final, dtype=pl.Float64),
            pl.Series("res_hazard_spread", spread, dtype=pl.Float64),
            pl.Series(
                "property_loss_npctl",
                percentile_ranks(property_loss_input, force_zero=True),
                dtype=pl.Float64,
            ),
            pl.Series("res_hazard_data_quality", quality, dtype=pl.String),
            pl.Series("res_hazard_available_count", available_count, dtype=pl.Int8),
            pl.Series(
                "res_hazard_coverage_ratio",
                [count / len(HAZARDS) for count in available_count],
                dtype=pl.Float64,
            ),
        ]
    )


def hazard_percentiles_from_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "code": hazard.code,
            "label": hazard.label,
            "percentile": optional_percentile(record.get(hazard.percentile_column)),
            "raw_alrb": optional_number(record.get(hazard.raw_column)),
            "availability": hazard_availability(
                record.get(hazard.raw_column), record.get(hazard.rating_column)
            ),
            "fema_eal_rating": record.get(hazard.rating_column),
        }
        for hazard in HAZARDS
    ]


def hazard_select_sql() -> str:
    return ", ".join(HAZARD_SNAPSHOT_COLUMNS)
