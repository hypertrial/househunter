"""Published FEMA per-hazard Expected Annual Loss Rate national percentiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl


@dataclass(frozen=True, slots=True)
class Hazard:
    code: str
    label: str

    @property
    def fema_field(self) -> str:
        return f"{self.code}_ALR_NPCTL"

    @property
    def column(self) -> str:
        return f"alr_npctl_{self.code.lower()}"


HAZARDS: tuple[Hazard, ...] = (
    Hazard("AVLN", "Avalanche"),
    Hazard("CFLD", "Coastal Flooding"),
    Hazard("CWAV", "Cold Wave"),
    Hazard("DRGT", "Drought"),
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

HAZARD_COLUMNS: tuple[str, ...] = tuple(hazard.column for hazard in HAZARDS)
FEMA_HAZARD_FIELDS: dict[str, str] = {
    hazard.fema_field: "esriFieldTypeDouble" for hazard in HAZARDS
}


def tract_out_fields() -> str:
    return ",".join(
        ["TRACTFIPS", "ALR_NPCTL", "NRI_VER", *[hazard.fema_field for hazard in HAZARDS]]
    )


def county_out_fields() -> str:
    return ",".join(
        [
            "STCOFIPS",
            "COUNTY",
            "COUNTYTYPE",
            "STATEABBRV",
            "ALR_NPCTL",
            "NRI_VER",
            *[hazard.fema_field for hazard in HAZARDS],
        ]
    )


def hazard_schema() -> dict[str, pl.DataType]:
    return {column: pl.Float64 for column in HAZARD_COLUMNS}


def hazard_values_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {hazard.column: row.get(hazard.fema_field) for hazard in HAZARDS}


def hazard_fields_from_cached_row(row: dict[str, Any]) -> dict[str, Any]:
    return {hazard.fema_field: row.get(hazard.column) for hazard in HAZARDS}


def with_hazard_columns(frame: pl.DataFrame) -> pl.DataFrame:
    missing = [
        pl.lit(None, dtype=pl.Float64).alias(column)
        for column in HAZARD_COLUMNS
        if column not in frame.columns
    ]
    widened = frame.with_columns(missing) if missing else frame
    return widened.with_columns([pl.col(column).cast(pl.Float64) for column in HAZARD_COLUMNS])


def invalid_optional_hazard_rows(frame: pl.DataFrame) -> pl.DataFrame:
    conditions = [
        pl.col(column).is_not_null()
        & (~pl.col(column).is_finite() | (pl.col(column) < 0) | (pl.col(column) > 100))
        for column in HAZARD_COLUMNS
        if column in frame.columns
    ]
    if not conditions:
        return frame.clear()
    mask = conditions[0]
    for condition in conditions[1:]:
        mask = mask | condition
    return frame.filter(mask)


def logical_hazard_values(row: dict[str, Any]) -> list[Any]:
    return [row.get(column) for column in HAZARD_COLUMNS]


def optional_percentile(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def hazard_percentiles_from_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "code": hazard.code,
            "label": hazard.label,
            "percentile": optional_percentile(record.get(hazard.column)),
        }
        for hazard in HAZARDS
    ]


def hazard_select_sql() -> str:
    return ", ".join(HAZARD_COLUMNS)
