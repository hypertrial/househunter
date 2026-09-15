from __future__ import annotations

import math

import pytest

from househunter.errors import HouseHunterError
from househunter.top_counties import (
    LAYER_PREP,
    PRESETS,
    REQUIRED_LAYERS,
    average_tie_percentile,
    rank_counties,
    require_complete_national_snapshot,
)


def _county(
    place_id: str,
    *,
    name: str | None = None,
    state: str = "CO",
    population: int = 50_000,
    months: int = 12,
    listings: float = 150.0,
    u_safety: float = 0.5,
    u_health: float = 0.5,
    u_affordability: float = 0.5,
    u_opportunity: float = 0.5,
    u_lifestyle: float = 0.5,
    u_family: float = 0.5,
    crime_coverage: float = 0.95,
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "place_id": place_id,
        "name": name or place_id,
        "state": state,
        "population": population,
        "housing_valid_months": months,
        "median_active_listings": listings,
        "median_ppsf": 800.0,
        "sqft_for_1m_t12": 1250.0,
        "res_hazard_npctl": 50.0,
        "crime_coverage": crime_coverage,
        "u_hazard": u_safety,
        "u_crime": u_safety,
        "u_water": u_safety,
        "u_safety": u_safety,
        "u_healthcare": u_health,
        "u_community_context": u_health,
        "u_health": u_health,
        "u_housing": u_affordability,
        "u_rpp": u_affordability,
        "u_property_tax": u_affordability,
        "u_affordability": u_affordability,
        "u_employment": u_opportunity,
        "u_broadband": u_opportunity,
        "u_opportunity": u_opportunity,
        "u_mountain": u_lifestyle,
        "u_lifestyle": u_lifestyle,
        "u_family": u_family,
        "homeschool_utility": u_family,
        "rpp_index": 100.0,
        "rpp_geography_type": "state",
        "mountain_magnitude": 1.0,
        "community_context_kind": "reconstructed",
        "jan_avg_temp_f": 30.0,
        "jul_avg_temp_f": 70.0,
        "extreme_heat_days": 5.0,
        "extreme_cold_days": 5.0,
    }
    row.update(extra)
    return row


def test_average_tie_percentile_uses_midranks() -> None:
    assert average_tie_percentile([1.0, 2.0, 2.0, 4.0]) == [0.0, 0.5, 0.5, 1.0]


def test_presets_match_approved_weights() -> None:
    assert PRESETS["balanced"] == (0.20, 0.15, 0.25, 0.15, 0.15, 0.10)
    assert PRESETS["safety-health"] == (0.35, 0.25, 0.15, 0.10, 0.05, 0.10)
    assert PRESETS["affordability"] == (0.15, 0.10, 0.45, 0.15, 0.05, 0.10)
    assert PRESETS["mountain-lifestyle"] == (0.10, 0.10, 0.15, 0.10, 0.45, 0.10)
    for weights in PRESETS.values():
        assert abs(sum(weights) - 1.0) < 1e-12


def test_additive_score_prefers_higher_pillar_utilities() -> None:
    safer = _county("08013", u_safety=0.95, u_health=0.9, name="Boulder")
    worse = _county("08031", u_safety=0.1, u_health=0.1, name="Worse")
    ranking = rank_counties([worse, safer], limit=2)
    assert [item.place_id for item in ranking.items] == ["08013", "08031"]
    assert ranking.items[0].preference_fit > ranking.items[1].preference_fit
    assert ranking.methodology_id == "top-counties-v2"


def test_exclusions_do_not_rescale_utilities() -> None:
    low = _county("01001", u_lifestyle=0.2, u_affordability=0.2)
    high = _county("02001", u_lifestyle=0.8, u_affordability=0.8)
    outlier = _county("08013", u_lifestyle=1.0, u_affordability=1.0, population=24_999)
    ranking = rank_counties([low, high, outlier], limit=10)
    assert ranking.eligible_count == 2
    assert ranking.exclusions["population"] == 1
    by_id = {item.place_id: item for item in ranking.items}
    assert by_id["01001"].pillars["lifestyle"] == 0.2
    assert by_id["02001"].pillars["lifestyle"] == 0.8
    assert by_id["01001"].preference_fit != pytest.approx(0.0)
    unfiltered = rank_counties(
        [low, high], limit=10, min_population=0, min_active_listings=0, min_valid_months=1
    )
    assert by_id["01001"].preference_fit == pytest.approx(unfiltered.items[1].preference_fit)
    assert by_id["02001"].preference_fit == pytest.approx(unfiltered.items[0].preference_fit)


def test_national_rank_is_assigned_before_eligibility_gates() -> None:
    small_leader = _county(
        "01001",
        population=24_999,
        u_safety=1.0,
        u_health=1.0,
        u_affordability=1.0,
        u_lifestyle=1.0,
        name="Small",
    )
    large_follower = _county(
        "02001",
        population=80_000,
        u_safety=0.1,
        u_health=0.1,
        u_affordability=0.1,
        u_lifestyle=0.1,
        name="Large",
    )
    ranking = rank_counties([small_leader, large_follower], limit=10)
    assert ranking.eligible_count == 1
    assert ranking.exclusions["population"] == 1
    assert ranking.items[0].place_id == "02001"
    assert ranking.items[0].national_rank == 2
    assert ranking.items[0].filtered_rank == 1
    stricter = rank_counties(
        [small_leader, large_follower], min_population=50_000, limit=10
    )
    assert stricter.items[0].national_rank == 2


def test_threshold_edges() -> None:
    under_pop = _county("01001", population=24_999)
    at_pop = _county("02001", population=25_000)
    ranking = rank_counties([under_pop, at_pop], limit=10)
    assert ranking.eligible_count == 1
    assert ranking.items[0].place_id == "02001"

    under_list = _county("01001", listings=99)
    at_list = _county("02001", listings=100)
    ranking = rank_counties([under_list, at_list], limit=10)
    assert ranking.eligible_count == 1
    assert ranking.items[0].place_id == "02001"

    under_months = _county("01001", months=8)
    at_months = _county("02001", months=9)
    ranking = rank_counties([under_months, at_months], limit=10)
    assert ranking.eligible_count == 1
    assert ranking.items[0].place_id == "02001"

    under_crime = _county("01001", crime_coverage=0.899)
    at_crime = _county("02001", crime_coverage=0.90)
    ranking = rank_counties([under_crime, at_crime], limit=10)
    assert ranking.eligible_count == 1
    assert ranking.items[0].place_id == "02001"
    assert ranking.exclusions["missing_core"] == 1


def test_ties_break_by_county_fips() -> None:
    left = _county("02001", name="B")
    right = _county("01001", name="A")
    ranking = rank_counties([left, right], limit=2)
    assert [item.place_id for item in ranking.items] == ["01001", "02001"]
    assert ranking.items[0].preference_fit == ranking.items[1].preference_fit


def test_named_presets_change_order_when_tradeoffs_exist() -> None:
    safe = _county(
        "01001",
        u_safety=1.0,
        u_health=1.0,
        u_affordability=0.0,
        u_lifestyle=0.0,
        name="Safe",
    )
    cheap = _county(
        "02001",
        u_safety=0.0,
        u_health=0.0,
        u_affordability=1.0,
        u_lifestyle=0.0,
        name="Cheap",
    )
    mountain = _county(
        "08013",
        u_safety=0.2,
        u_health=0.2,
        u_affordability=0.2,
        u_lifestyle=1.0,
        name="Peak",
    )
    rows = [safe, cheap, mountain]
    assert rank_counties(rows, preset="safety-health", limit=3).items[0].place_id == "01001"
    assert rank_counties(rows, preset="affordability", limit=3).items[0].place_id == "02001"
    assert rank_counties(rows, preset="mountain-lifestyle", limit=3).items[0].place_id == "08013"


def test_exclude_region_appalachia_does_not_drop_autauga() -> None:
    autauga = _county("01001", state="AL", u_safety=0.2, name="Autauga")
    bibb = _county("01007", state="AL", u_safety=1.0, name="Bibb")
    barbour_wv = _county("54001", state="WV", u_safety=0.9, name="Barbour")
    ranking = rank_counties(
        [bibb, barbour_wv, autauga],
        exclude_region="appalachia",
        limit=10,
    )
    assert [item.place_id for item in ranking.items] == ["01001"]
    assert ranking.eligible_count == 3
    assert ranking.filtered_count == 1
    assert ranking.exclusions["region_exclusion"] == 2
    assert ranking.items[0].national_rank == 3
    assert ranking.gates["exclude_region"] == "appalachia"


def test_state_and_region_exclusions_are_post_score() -> None:
    alabama = _county("01001", state="AL", u_safety=0.1)
    alaska = _county("02001", state="AK", u_safety=1.0)
    ranking = rank_counties([alabama, alaska], exclude_states=["AK"], limit=10)
    assert ranking.eligible_count == 2
    assert ranking.filtered_count == 1
    assert ranking.items[0].place_id == "01001"
    assert ranking.items[0].national_rank == 2
    assert ranking.items[0].filtered_rank == 1
    assert alabama["u_safety"] == 0.1


def test_climate_and_pillar_gates() -> None:
    cold = _county("02001", jan_avg_temp_f=10.0, u_lifestyle=1.0)
    warm = _county("01001", jan_avg_temp_f=40.0, u_lifestyle=0.2)
    gated = rank_counties([cold, warm], min_jan_temp_f=20.0, limit=10)
    assert [item.place_id for item in gated.items] == ["01001"]
    assert gated.exclusions["climate"] == 1
    pillar = rank_counties([cold, warm], min_pillars={"lifestyle": 0.5}, limit=10)
    assert [item.place_id for item in pillar.items] == ["02001"]


def test_missing_core_is_excluded_not_imputed() -> None:
    complete = _county("01001")
    missing = _county("02001")
    missing["u_safety"] = None
    ranking = rank_counties([complete, missing], limit=10)
    assert ranking.eligible_count == 1
    assert ranking.items[0].place_id == "01001"


def test_unknown_preset_and_limit_and_incompatible_flags() -> None:
    with pytest.raises(HouseHunterError, match="preset"):
        rank_counties([_county("01001")], preset="livability")
    with pytest.raises(HouseHunterError, match="limit"):
        rank_counties([_county("01001")], limit=0)
    with pytest.raises(HouseHunterError, match="include and exclude"):
        rank_counties(
            [_county("01001")],
            states=["AL"],
            exclude_states=["AL"],
        )


def test_snapshot_gate_requires_schema_12_national() -> None:
    require_complete_national_snapshot(
        {"scope": {"kind": "national", "state": None}, "schema_version": 12}
    )
    with pytest.raises(HouseHunterError, match="national snapshot"):
        require_complete_national_snapshot(
            {"scope": {"kind": "state", "state": "CO"}, "schema_version": 12}
        )
    with pytest.raises(HouseHunterError, match="schema 12"):
        require_complete_national_snapshot(
            {"scope": {"kind": "national", "state": None}, "schema_version": 11}
        )


def test_pareto_is_annotation_not_a_ranking_input() -> None:
    dominated = _county("02001", u_safety=0.1, u_health=0.1, u_affordability=0.1)
    better = _county("01001", u_safety=1.0, u_health=1.0, u_affordability=1.0)
    ranking = rank_counties([dominated, better], limit=2)
    flags = {item.place_id: item.pareto_optimal for item in ranking.items}
    assert flags["01001"] is True
    assert flags["02001"] is False
    assert ranking.items[0].place_id == "01001"


def test_non_finite_cores_are_excluded() -> None:
    complete = _county("01001")
    broken = _county("02001")
    broken["u_health"] = float("nan")
    ranking = rank_counties([complete, broken], limit=10)
    assert ranking.eligible_count == 1
    assert not any(
        value is None or (isinstance(value, float) and not math.isfinite(value))
        for value in ranking.items[0].pillars.values()
    )


def test_leave_one_factor_out_changes_order() -> None:
    mountain = _county("08013", u_lifestyle=1.0, u_affordability=0.1, state="CO")
    cheap = _county("02001", u_lifestyle=0.1, u_affordability=1.0, state="AK")
    outdoor = rank_counties([mountain, cheap], preset="mountain-lifestyle", limit=2)
    affordability = rank_counties([mountain, cheap], preset="affordability", limit=2)
    assert outdoor.items[0].place_id == "08013"
    assert affordability.items[0].place_id == "02001"


def test_sensitivity_population_and_listings_floors() -> None:
    small = _county("01001", population=30_000, listings=120)
    large = _county("02001", population=80_000, listings=400, u_safety=0.1)
    default = rank_counties([small, large], limit=10)
    assert {item.place_id for item in default.items} == {"01001", "02001"}
    stricter = rank_counties(
        [small, large], min_population=50_000, min_active_listings=250, limit=10
    )
    assert [item.place_id for item in stricter.items] == ["02001"]


def test_golden_fixture_order_is_stable() -> None:
    rows = [
        _county(
            "01001",
            state="AL",
            u_safety=0.38,
            u_health=0.37,
            u_affordability=0.36,
            u_lifestyle=0.10,
            u_family=0.82,
        ),
        _county(
            "02001",
            state="AK",
            u_safety=0.88,
            u_health=0.80,
            u_affordability=0.80,
            u_lifestyle=1.0,
            u_family=0.95,
        ),
    ]
    expected = {
        "balanced": "02001",
        "safety-health": "02001",
        "affordability": "02001",
        "mountain-lifestyle": "02001",
    }
    for preset, winner in expected.items():
        ranking = rank_counties(rows, preset=preset, limit=2)
        assert ranking.items[0].place_id == winner, preset


def test_layer_prep_still_documents_optional_map_sources() -> None:
    assert set(REQUIRED_LAYERS) == set(LAYER_PREP)
    assert "import-home-market" in LAYER_PREP["home-costs"]
