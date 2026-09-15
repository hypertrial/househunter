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
from househunter.hazards import HAZARDS, INSUFFICIENT_DATA, NOT_APPLICABLE, with_hazard_columns


def _hazard_inputs(rows: int, *, last_missing: bool = False) -> dict[str, list[object]]:
    return {
        column: values
        for hazard in HAZARDS
        for column, values in (
            (hazard.raw_column, [None] * rows),
            (
                hazard.rating_column,
                [NOT_APPLICABLE] * (rows - int(last_missing))
                + ([INSUFFICIENT_DATA] if last_missing else []),
            ),
        )
    }


def _fema() -> pl.DataFrame:
    hazards = _hazard_inputs(4, last_missing=True)
    hazards["alrb_wfir"] = [0.0, 10.0, 20.0, None]
    hazards["ealr_wfir"] = [
        "No Expected Annual Losses",
        "Relatively Low",
        "Relatively High",
        INSUFFICIENT_DATA,
    ]
    return with_hazard_columns(
        pl.DataFrame(
            {
                "tract_id": [
                    "01001000100",
                    "01001000200",
                    "01001000300",
                    "99999999999",
                ],
                "alr_npctl": [20.0, 60.0, 40.0, 99.0],
                "alr_valb": [0.0, 2.0, 1.0, None],
                "nri_version": ["December 2025"] * 4,
                **hazards,
            }
        )
    )


def _counties() -> pl.DataFrame:
    hazards = _hazard_inputs(2)
    hazards["alrb_wfir"] = [10.0, 0.0]
    hazards["ealr_wfir"] = ["Relatively High", "No Expected Annual Losses"]
    return with_hazard_columns(
        pl.DataFrame(
            {
                "county_fips": ["01001", "02001"],
                "county": ["Autauga", "Aleutians East"],
                "county_type": ["County", "Borough"],
                "state": ["al", "AK"],
                "alr_npctl": [41.0, 12.0],
                "alr_valb": [10.0, 0.0],
                "nri_version": ["December 2025"] * 2,
                **hazards,
            }
        )
    )


def test_scores_use_building_hazards_and_retain_raw_fema_reference() -> None:
    scored, _counties_scored = compute_scores(_fema(), _counties())
    by_id = {row["place_id"]: row for row in scored.iter_rows(named=True)}

    assert by_id["01001000100"]["res_hazard_npctl"] == 0.0
    assert by_id["01001000200"]["res_hazard_npctl"] == 50.0
    assert by_id["01001000300"]["res_hazard_npctl"] == 100.0
    assert by_id["01001000100"]["alr_npctl"] == 20.0
    assert by_id["01001000200"]["alr_npctl"] == 60.0
    assert by_id["99999999999"]["state"] == UNKNOWN_STATE
    assert by_id["99999999999"]["res_hazard_npctl"] is None
    assert by_id["99999999999"]["res_hazard_data_quality"] == "unavailable"


def test_county_and_tract_percentile_universes_are_independent() -> None:
    scored, county_scored = compute_scores(_fema(), _counties())
    tract = scored.filter(pl.col("place_id") == "01001000200").row(0, named=True)
    county = county_scored.filter(pl.col("place_id") == "01001").row(0, named=True)

    assert tract["alrb_npctl_wfir"] == 50.0
    assert county["alrb_npctl_wfir"] == 100.0
    assert county["res_hazard_npctl"] == 100.0
    assert county["alr_npctl"] == 41.0
    assert county["res_hazard_npctl"] != county["alr_npctl"]
    assert scored.filter(pl.col("place_id") == "01001000100")["county_name"].item() == (
        "Autauga"
    )
    unknown = scored.filter(pl.col("place_id") == "99999999999").row(0, named=True)
    assert unknown["county_fips"] == UNKNOWN_COUNTY_FIPS
    assert unknown["county_name"] == UNKNOWN_COUNTY_NAME


def test_state_for_tract_uses_bundled_fips_map() -> None:
    assert state_for_tract("08013012101") == "CO"
    assert state_for_tract("09") == "CT"
    assert state_for_tract("99") == UNKNOWN_STATE
    assert state_for_tract("") == UNKNOWN_STATE


def test_county_display_name_omits_generic_type() -> None:
    assert county_display_name("Autauga", "County") == "Autauga"
    assert county_display_name("Baltimore", "city") == "Baltimore city"
    assert county_display_name("", "County") == UNKNOWN_COUNTY_NAME


def test_county_state_is_normalized_to_uppercase() -> None:
    _scored, county_scored = compute_scores(_fema(), _counties())
    assert county_scored.filter(pl.col("place_id") == "01001")["state"].item() == "AL"
