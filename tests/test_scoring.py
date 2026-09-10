from __future__ import annotations

import polars as pl

from househunter.build import compute_scores


def _inputs():  # type: ignore[no-untyped-def]
    places = pl.DataFrame(
        {
            "place_id": ["0100001", "0100002", "0100003", "0100004", "0100005"],
            "name": ["Multi", "Single", "Zero", "Missing", "Unmatched"],
            "state": ["AL"] * 5,
            "place_type": ["city"] * 5,
            "population_2020": [1] * 5,
            "housing_units_2020": [4, 2, 0, 1, 1],
        }
    )
    weights = pl.DataFrame(
        {
            "place_id": ["0100001", "0100001", "0100002", "0100004", "0100005"],
            "tract_id": ["01001000100", "01001000200", "01001000300", "01001000400", None],
            "housing_units": [1, 3, 2, 1, 1],
            "housing_weight": [0.25, 0.75, 1.0, 1.0, 1.0],
        },
        schema_overrides={"tract_id": pl.String},
    )
    fema = pl.DataFrame(
        {
            "tract_id": ["01001000100", "01001000200", "01001000300"],
            "alr_npctl": [20.0, 60.0, 77.0],
        }
    )
    return places, weights, fema


def test_weighted_mean_and_single_tract_identity() -> None:
    scored, contributions = compute_scores(*_inputs())
    by_id = {row["place_id"]: row for row in scored.iter_rows(named=True)}
    assert by_id["0100001"]["risk_score"] == 50.0
    assert by_id["0100002"]["risk_score"] == 77.0
    assert (
        contributions.filter(pl.col("place_id") == "0100001")["weighted_contribution"].sum() == 50.0
    )


def test_incomplete_places_are_retained_without_renormalizing() -> None:
    scored, _ = compute_scores(*_inputs())
    by_id = {row["place_id"]: row for row in scored.iter_rows(named=True)}
    assert by_id["0100003"]["coverage_status"] == "zero_housing"
    assert by_id["0100004"]["coverage_status"] == "missing_fema"
    assert by_id["0100005"]["coverage_status"] == "unmatched_geography"
    assert all(by_id[place]["risk_score"] is None for place in ["0100003", "0100004", "0100005"])


def test_score_is_monotonic_in_source_percentile() -> None:
    places, weights, fema = _inputs()
    baseline, _ = compute_scores(places, weights, fema)
    raised, _ = compute_scores(
        places,
        weights,
        fema.with_columns(
            pl.when(pl.col("tract_id") == "01001000100")
            .then(pl.lit(40.0))
            .otherwise(pl.col("alr_npctl"))
            .alias("alr_npctl")
        ),
    )
    before = baseline.filter(pl.col("place_id") == "0100001")["risk_score"].item()
    after = raised.filter(pl.col("place_id") == "0100001")["risk_score"].item()
    assert after > before
