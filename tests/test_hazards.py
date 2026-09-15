from __future__ import annotations

import math
from pathlib import Path

import polars as pl
import pytest

from househunter.config import canonical_json, load_config, sha256_bytes
from househunter.hazards import (
    FEMA_HAZARD_FIELDS,
    HAZARDS,
    INSUFFICIENT_DATA,
    NO_EXPECTED_ANNUAL_LOSSES,
    NOT_APPLICABLE,
    hazard_availability,
    hazard_percentiles_from_record,
    percentile_ranks,
    raw_hazard_aggregates,
    score_residential_hazards,
    tract_out_fields,
)


def test_catalog_uses_seventeen_building_hazards_and_excludes_drought() -> None:
    assert len(HAZARDS) == 17
    assert len(set(hazard.code for hazard in HAZARDS)) == 17
    assert "DRGT" not in {hazard.code for hazard in HAZARDS}
    wildfire = next(hazard for hazard in HAZARDS if hazard.code == "WFIR")
    assert wildfire.raw_column == "alrb_wfir"
    assert wildfire.percentile_column == "alrb_npctl_wfir"
    assert FEMA_HAZARD_FIELDS[wildfire.alrb_field] == "esriFieldTypeDouble"
    assert FEMA_HAZARD_FIELDS[wildfire.rating_field] == "esriFieldTypeString"
    assert tract_out_fields().startswith("TRACTFIPS,ALR_NPCTL,ALR_VALB,NRI_VER,AVLN_ALRB")
    assert tract_out_fields().endswith("WNTW_EALR")


def test_average_tie_percentiles_include_valid_zero_then_force_zero() -> None:
    assert percentile_ranks([0.0, 1.0, 1.0, 3.0], force_zero=True) == [
        0.0,
        50.0,
        50.0,
        100.0,
    ]
    assert percentile_ranks([0.0, 0.0, 1.0], force_zero=True) == [0.0, 0.0, 100.0]
    assert percentile_ranks([7.0]) == [0.0]
    assert percentile_ranks([None, None]) == [None, None]


@pytest.mark.parametrize(
    ("raw", "rating", "expected"),
    [
        (1.0, "Relatively High", "valid"),
        (0.0, NO_EXPECTED_ANNUAL_LOSSES, "valid"),
        (None, NOT_APPLICABLE, "not_applicable"),
        (None, INSUFFICIENT_DATA, "missing"),
        (-1.0, "Relatively High", "invalid"),
        (None, "Relatively High", "invalid"),
        (1.0, NOT_APPLICABLE, "invalid"),
        (0.0, INSUFFICIENT_DATA, "invalid"),
    ],
)
def test_hazard_availability_preserves_source_states(
    raw: float | None, rating: str, expected: str
) -> None:
    assert hazard_availability(raw, rating) == expected


def test_raw_aggregations_emphasize_tail_and_renormalize_missing() -> None:
    one_extreme = raw_hazard_aggregates([100.0, *([0.0] * 16)])
    two_extremes = raw_hazard_aggregates([100.0, 100.0, *([0.0] * 15)])
    missing_excluded = raw_hazard_aggregates([100.0, None])
    non_applicable_zero = raw_hazard_aggregates([100.0, 0.0])

    assert one_extreme is not None and two_extremes is not None
    assert all(after >= before for before, after in zip(one_extreme, two_extremes, strict=True))
    assert one_extreme[0] > 30
    assert one_extreme[1] == 20.0
    assert missing_excluded == (100.0, 100.0, 100.0)
    assert non_applicable_zero is not None
    assert non_applicable_zero[0] < missing_excluded[0]
    assert raw_hazard_aggregates([None, None]) is None


def test_increasing_a_hazard_cannot_reduce_any_raw_aggregate() -> None:
    baseline = raw_hazard_aggregates([90.0, 40.0, 10.0, 0.0])
    raised = raw_hazard_aggregates([90.0, 60.0, 10.0, 0.0])
    assert baseline is not None and raised is not None
    assert all(after >= before for before, after in zip(baseline, raised, strict=True))


def _model_frame() -> pl.DataFrame:
    data: dict[str, list[object]] = {
        "geography_id": ["low", "high", "partial", "missing"],
        "alr_npctl": [1.0, 99.0, 50.0, 25.0],
        "alr_valb": [0.0, 10.0, 5.0, None],
    }
    for hazard in HAZARDS:
        data[hazard.raw_column] = [None, None, None, None]
        data[hazard.rating_column] = [
            NOT_APPLICABLE,
            NOT_APPLICABLE,
            NOT_APPLICABLE,
            INSUFFICIENT_DATA,
        ]
    wildfire = next(hazard for hazard in HAZARDS if hazard.code == "WFIR")
    data[wildfire.raw_column] = [0.0, 10.0, None, None]
    data[wildfire.rating_column] = [
        NO_EXPECTED_ANNUAL_LOSSES,
        "Relatively High",
        INSUFFICIENT_DATA,
        INSUFFICIENT_DATA,
    ]
    return pl.DataFrame(data)


def test_model_scores_available_hazards_and_flags_quality() -> None:
    first = score_residential_hazards(_model_frame())
    second = score_residential_hazards(_model_frame())
    assert first.equals(second)

    by_id = {row["geography_id"]: row for row in first.iter_rows(named=True)}
    assert by_id["low"]["alrb_npctl_wfir"] == 0.0
    assert by_id["high"]["alrb_npctl_wfir"] == 100.0
    assert by_id["low"]["res_hazard_npctl"] < by_id["high"]["res_hazard_npctl"]
    assert by_id["low"]["property_loss_npctl"] == 0.0
    assert by_id["high"]["property_loss_npctl"] == 100.0
    assert by_id["low"]["res_hazard_data_quality"] == "complete"
    assert by_id["low"]["res_hazard_available_count"] == 17
    assert by_id["partial"]["res_hazard_data_quality"] == "partial"
    assert by_id["partial"]["res_hazard_available_count"] == 16
    assert by_id["partial"]["res_hazard_coverage_ratio"] == pytest.approx(16 / 17)
    assert by_id["missing"]["res_hazard_data_quality"] == "unavailable"
    assert by_id["missing"]["res_hazard_npctl"] is None
    for column in (
        "res_hazard_npctl",
        "res_hazard_spread",
        "property_loss_npctl",
    ):
        assert all(
            value is None or (math.isfinite(value) and 0 <= value <= 100)
            for value in first[column]
        )


def test_invalid_raw_hazard_is_null_and_excluded_not_zero() -> None:
    frame = _model_frame().with_columns(
        pl.when(pl.col("geography_id") == "partial")
        .then(pl.lit(-1.0))
        .otherwise(pl.col("alrb_wfir"))
        .alias("alrb_wfir"),
        pl.when(pl.col("geography_id") == "partial")
        .then(pl.lit("Relatively High"))
        .otherwise(pl.col("ealr_wfir"))
        .alias("ealr_wfir"),
    )
    record = score_residential_hazards(frame).filter(
        pl.col("geography_id") == "partial"
    ).row(0, named=True)
    assert record["alrb_wfir"] is None
    assert record["alrb_npctl_wfir"] is None
    assert record["res_hazard_available_count"] == 16

    hazards = hazard_percentiles_from_record(record)
    wildfire = next(item for item in hazards if item["code"] == "WFIR")
    assert wildfire == {
        "code": "WFIR",
        "label": "Wildfire",
        "percentile": None,
        "raw_alrb": None,
        "availability": "invalid",
        "fema_eal_rating": "Relatively High",
    }


@pytest.mark.parametrize(
    ("raw", "rating"),
    [(1.0, NOT_APPLICABLE), (0.0, INSUFFICIENT_DATA)],
)
def test_contradictory_status_pair_remains_visible_as_invalid(
    raw: float, rating: str
) -> None:
    frame = _model_frame().head(1).with_columns(
        pl.lit(raw).alias("alrb_wfir"),
        pl.lit(rating).alias("ealr_wfir"),
    )
    record = score_residential_hazards(frame).row(0, named=True)

    assert record["alrb_wfir"] == raw
    assert record["alrb_npctl_wfir"] is None
    assert record["res_hazard_available_count"] == 16
    wildfire = next(
        item for item in hazard_percentiles_from_record(record) if item["code"] == "WFIR"
    )
    assert wildfire["availability"] == "invalid"
    assert wildfire["raw_alrb"] == raw


def test_pinned_source_contracts_include_building_fields() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config" / "sources.yml")
    for key in ("fema", "fema_counties"):
        fields = config[key]["fields"]
        assert fields["ALR_VALB"] == "esriFieldTypeDouble"
        assert "DRGT_ALRB" not in fields
        for fema_field, kind in FEMA_HAZARD_FIELDS.items():
            assert fields[fema_field] == kind
        assert sha256_bytes(canonical_json(fields)) == config[key]["schema_fingerprint"]
