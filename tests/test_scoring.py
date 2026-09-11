from __future__ import annotations

import polars as pl

from househunter.build import compute_scores
from househunter.geography import (
    UNKNOWN_COUNTY_FIPS,
    UNKNOWN_COUNTY_NAME,
    UNKNOWN_STATE,
    county_display_name,
    state_for_tract,
)
from househunter.hazards import with_hazard_columns


def _fema() -> pl.DataFrame:
    return with_hazard_columns(
        pl.DataFrame(
            {
                "tract_id": ["01001000100", "01001000200", "99999999999"],
                "alr_npctl": [20.0, 60.0, None],
                "alr_npctl_wfir": [5.0, 80.0, None],
            }
        )
    )


def test_score_is_the_published_fema_percentile() -> None:
    scored, contributions, _counties = compute_scores(_fema())
    by_id = {row["place_id"]: row for row in scored.iter_rows(named=True)}
    assert by_id["01001000100"]["risk_score"] == 20.0
    assert by_id["01001000100"]["state"] == "AL"
    assert by_id["01001000100"]["coverage_status"] == "complete"
    assert by_id["01001000200"]["risk_score"] == 60.0
    assert by_id["99999999999"]["state"] == UNKNOWN_STATE
    assert by_id["99999999999"]["risk_score"] is None
    assert by_id["99999999999"]["coverage_status"] == "missing_fema"
    own = contributions.filter(pl.col("place_id") == "01001000100")
    assert own["weighted_contribution"].item() == 20.0


def test_score_is_monotonic_in_source_percentile() -> None:
    baseline, _, _counties = compute_scores(_fema())
    raised, _, _ = compute_scores(
        _fema().with_columns(
            pl.when(pl.col("tract_id") == "01001000100")
            .then(pl.lit(40.0))
            .otherwise(pl.col("alr_npctl"))
            .alias("alr_npctl")
        )
    )
    before = baseline.filter(pl.col("place_id") == "01001000100")["risk_score"].item()
    after = raised.filter(pl.col("place_id") == "01001000100")["risk_score"].item()
    assert after > before


def test_state_for_tract_uses_bundled_fips_map() -> None:
    assert state_for_tract("08013012101") == "CO"
    assert state_for_tract("09") == "CT"
    assert state_for_tract("99") == UNKNOWN_STATE
    assert state_for_tract("") == UNKNOWN_STATE


def test_county_display_name_omits_generic_type() -> None:
    assert county_display_name("Autauga", "County") == "Autauga"
    assert county_display_name("Baltimore", "city") == "Baltimore city"
    assert county_display_name("", "County") == UNKNOWN_COUNTY_NAME


def test_county_score_is_fema_county_percentile_not_tract_mean() -> None:
    counties = with_hazard_columns(
        pl.DataFrame(
            {
                "county_fips": ["01001"],
                "county": ["Autauga"],
                "county_type": ["County"],
                "state": ["AL"],
                "alr_npctl": [41.0],
                "nri_version": ["December 2025"],
                "alr_npctl_wfir": [11.0],
            }
        )
    )
    scored, _, county_scored = compute_scores(_fema(), counties)
    tract_mean = (
        scored.filter(pl.col("county_fips") == "01001")["risk_score"].drop_nulls().mean()
    )
    county_row = county_scored.filter(pl.col("place_id") == "01001").row(0, named=True)
    assert county_row["risk_score"] == 41.0
    assert county_row["risk_score"] != tract_mean
    tract_wildfire_mean = (
        scored.filter(pl.col("county_fips") == "01001")["alr_npctl_wfir"].drop_nulls().mean()
    )
    assert county_row["alr_npctl_wfir"] == 11.0
    assert county_row["alr_npctl_wfir"] != tract_wildfire_mean
    assert scored.filter(pl.col("place_id") == "01001000100")["county_name"].item() == "Autauga"
    assert scored.filter(pl.col("place_id") == "01001000100")["alr_npctl_wfir"].item() == 5.0
    unknown = scored.filter(pl.col("place_id") == "99999999999").row(0, named=True)
    assert unknown["county_fips"] == UNKNOWN_COUNTY_FIPS
    assert unknown["county_name"] == UNKNOWN_COUNTY_NAME


def test_county_state_is_normalized_to_uppercase() -> None:
    counties = with_hazard_columns(
        pl.DataFrame(
            {
                "county_fips": ["01001"],
                "county": ["Autauga"],
                "county_type": ["County"],
                "state": ["al"],
                "alr_npctl": [41.0],
                "nri_version": ["December 2025"],
            }
        )
    )
    _scored, _, county_scored = compute_scores(_fema(), counties)
    assert county_scored.filter(pl.col("place_id") == "01001")["state"].item() == "AL"
