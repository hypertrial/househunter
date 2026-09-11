from __future__ import annotations

import polars as pl

from househunter.build import compute_scores
from househunter.geography import UNKNOWN_STATE, state_for_tract


def _fema() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "tract_id": ["01001000100", "01001000200", "99999999999"],
            "alr_npctl": [20.0, 60.0, None],
        }
    )


def test_score_is_the_published_fema_percentile() -> None:
    scored, contributions = compute_scores(_fema())
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
    baseline, _ = compute_scores(_fema())
    raised, _ = compute_scores(
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
