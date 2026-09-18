from __future__ import annotations

import math

import pytest

from househunter.errors import HouseHunterError
from househunter.top_counties import (
    LAYER_PREP,
    PILLARS,
    PRESETS,
    REQUIRED_LAYERS,
    average_tie_percentile,
    evaluate_counties,
    pareto_optimal,
    rank_counties,
    require_complete_national_snapshot,
    resolve_weights,
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


def test_custom_weight_total_honors_the_exact_tolerance_boundary() -> None:
    def weights(total: str) -> dict[str, float]:
        return {
            "safety": float(total),
            "health": 0.0,
            "affordability": 0.0,
            "opportunity": 0.0,
            "lifestyle": 0.0,
            "family": 0.0,
        }

    resolve_weights(None, weights("1.0000000000009"))
    resolve_weights(None, weights("1.000000000001"))
    with pytest.raises(HouseHunterError, match="exactly 1"):
        resolve_weights(None, weights("1.0000000000011"))


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
        [low, high], limit=10, min_population=25_000, min_active_listings=0, min_valid_months=1
    )
    assert by_id["01001"].preference_fit == pytest.approx(unfiltered.items[1].preference_fit)
    assert by_id["02001"].preference_fit == pytest.approx(unfiltered.items[0].preference_fit)


def test_hard_population_floor_precedes_national_rank_but_higher_filter_does_not() -> None:
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
    assert ranking.items[0].national_rank == 1
    assert ranking.items[0].filtered_rank == 1
    stricter = rank_counties([small_leader, large_follower], min_population=50_000, limit=10)
    assert stricter.items[0].national_rank == 1


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
    assert ranking.exclusions["crime_coverage"] == 1


def test_pillar_views_need_only_the_active_pillar_but_apply_the_hard_population_floor() -> None:
    row = _county(
        "01001",
        population=1,
        months=1,
        listings=1,
        u_safety=0.8,
        u_health=None,  # type: ignore[arg-type]
        u_affordability=None,  # type: ignore[arg-type]
        u_opportunity=None,  # type: ignore[arg-type]
        u_lifestyle=None,  # type: ignore[arg-type]
        u_family=None,  # type: ignore[arg-type]
    )
    safety = evaluate_counties([row], view="safety")
    assert safety.filtered_count == 0
    assert safety.rows[0].eligible is False
    assert safety.rows[0].exclusion_reason == "population"
    assert safety.rows[0].active_value is None
    assert safety.rows[0].national_rank is None
    assert safety.rows[0].filtered_rank is None
    assert safety.rows[0].pillars["safety"] == 0.8
    assert safety.rows[0].pareto_optimal is None
    assert safety.gates["population_floor"] == 25_000
    assert safety.gates["min_population"] == 25_000
    assert safety.gates["rank_policy"] == "competition"
    assert safety.gates["min_valid_months"] is None
    assert safety.gates["min_active_listings"] is None


@pytest.mark.parametrize("view", [*PILLARS, "custom"])
@pytest.mark.parametrize(
    ("population", "eligible"),
    [(None, False), (24_999, False), (25_000, True), (25_001, True)],
)
def test_population_floor_boundaries_apply_to_every_view(
    view: str, population: int | None, eligible: bool
) -> None:
    row = _county("01001")
    row["population"] = population
    evaluation = evaluate_counties([row], view=view)
    result = evaluation.rows[0]
    assert result.eligible is eligible
    if eligible:
        assert result.active_value is not None
        assert result.national_rank == 1
        assert result.filtered_rank == 1
    else:
        assert result.exclusion_reason == "population"
        assert result.active_value is None
        assert result.national_rank is None
        assert result.filtered_rank is None
        assert result.pillars["safety"] == 0.5


def test_county_fit_exclusion_precedence_is_stable() -> None:
    missing = _county("01001", u_health=None)  # type: ignore[arg-type]
    crime = _county("02001", crime_coverage=0.899, population=1, months=1, listings=1)
    population = _county("04001", population=24_999, months=1, listings=1)
    months = _county("05001", months=8, listings=1)
    listings = _county("06001", listings=99)
    state = _county("08013", state="CO")
    climate = _county("09001", state="CT", jan_avg_temp_f=20.0)
    pillar = _county("10001", state="DE", u_safety=0.4)
    evaluation = evaluate_counties(
        [missing, crime, population, months, listings, state, climate, pillar],
        view="custom",
        states=["CT", "DE"],
        min_jan_temp_f=25.0,
        min_pillars={"safety": 0.5},
    )
    reasons = {row.place_id: row.exclusion_reason for row in evaluation.rows}
    assert reasons == {
        "01001": "missing_core",
        "02001": "crime_coverage",
        "04001": "population",
        "05001": "valid_months",
        "06001": "active_listings",
        "08013": "state_filter",
        "09001": "climate",
        "10001": "min_safety",
    }


def test_ties_break_by_county_fips() -> None:
    left = _county("02001", name="B")
    right = _county("01001", name="A")
    lower = _county("04001", name="C", u_safety=0.1)
    ranking = rank_counties([left, lower, right], limit=3)
    assert [item.place_id for item in ranking.items] == ["01001", "02001", "04001"]
    assert ranking.items[0].preference_fit == ranking.items[1].preference_fit
    assert [item.national_rank for item in ranking.items] == [1, 1, 3]
    assert [item.filtered_rank for item in ranking.items] == [1, 1, 3]


def test_statewide_homeschool_policy_ties_use_competition_ranks() -> None:
    rows = [
        _county("08013", state="CO", u_family=0.8),
        _county("08001", state="CO", u_family=0.8),
        _county("01001", state="AL", u_family=0.2),
    ]
    evaluation = evaluate_counties(rows, view="family")
    ranked = sorted(
        (row for row in evaluation.rows if row.eligible),
        key=lambda row: (row.filtered_rank or 0, row.place_id),
    )
    assert [row.place_id for row in ranked] == ["08001", "08013", "01001"]
    assert [row.national_rank for row in ranked] == [1, 1, 3]
    assert [row.filtered_rank for row in ranked] == [1, 1, 3]


def test_higher_population_filter_preserves_national_rank_and_follows_housing_gates() -> None:
    leader = _county("01001", population=30_000, months=8, u_safety=1.0)
    follower = _county("02001", population=80_000, u_safety=0.1)
    evaluation = evaluate_counties(
        [leader, follower], view="custom", min_population=50_000
    )
    by_id = {row.place_id: row for row in evaluation.rows}
    assert by_id["01001"].national_rank == 1
    assert by_id["01001"].exclusion_reason == "valid_months"
    assert by_id["02001"].national_rank == 2
    assert by_id["02001"].filtered_rank == 1
    assert evaluation.gates["population_floor"] == 25_000
    assert evaluation.gates["min_population"] == 50_000
    assert evaluation.gates["rank_policy"] == "competition"


def test_higher_population_filter_does_not_turn_a_scored_county_into_reference_data() -> None:
    below_filter = _county("01001", population=30_000, u_safety=1.0)
    above_filter = _county("02001", population=80_000, u_safety=0.1)
    evaluation = evaluate_counties(
        [below_filter, above_filter], view="safety", min_population=50_000
    )
    by_id = {row.place_id: row for row in evaluation.rows}

    assert by_id["01001"].eligible is False
    assert by_id["01001"].exclusion_reason == "population"
    assert by_id["01001"].active_value == 1.0
    assert by_id["01001"].national_rank == 1
    assert by_id["01001"].filtered_rank is None
    assert by_id["02001"].filtered_rank == 1


def test_population_threshold_below_hard_floor_is_rejected() -> None:
    with pytest.raises(HouseHunterError, match="25,000"):
        evaluate_counties([_county("01001")], view="safety", min_population=24_999)


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


def test_climate_and_pillar_threshold_equality_is_inclusive() -> None:
    row = _county(
        "01001",
        jan_avg_temp_f=20.0,
        jul_avg_temp_f=80.0,
        extreme_heat_days=10.0,
        extreme_cold_days=40.0,
        u_safety=0.5,
    )
    evaluation = evaluate_counties(
        [row],
        view="safety",
        min_jan_temp_f=20.0,
        max_jan_temp_f=20.0,
        min_jul_temp_f=80.0,
        max_jul_temp_f=80.0,
        max_extreme_heat_days=10.0,
        max_extreme_cold_days=40.0,
        min_pillars={"safety": 0.5},
    )
    assert evaluation.rows[0].eligible is True
    assert evaluation.rows[0].exclusion_reason is None


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
    with pytest.raises(HouseHunterError, match="50-state/DC"):
        evaluate_counties([_county("01001")], view="safety", states=["ZZ"])
    for invalid in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(HouseHunterError, match="finite"):
            evaluate_counties([_county("01001")], view="safety", min_jan_temp_f=invalid)


def test_snapshot_gate_requires_schema_13_national() -> None:
    require_complete_national_snapshot(
        {"scope": {"kind": "national", "state": None}, "schema_version": 13}
    )
    with pytest.raises(HouseHunterError, match="national snapshot"):
        require_complete_national_snapshot(
            {"scope": {"kind": "state", "state": "CO"}, "schema_version": 13}
        )
    with pytest.raises(HouseHunterError, match="schema 13"):
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


def test_pareto_handles_a_full_national_anticorrelated_frontier() -> None:
    points = [
        {
            "safety": index / 3_143,
            "health": 1 - index / 3_143,
            "affordability": 0.5,
            "opportunity": 0.5,
            "lifestyle": 0.5,
            "family": 0.5,
        }
        for index in range(3_144)
    ]
    assert all(pareto_optimal(points))


def test_pareto_keeps_duplicate_frontier_points_and_rejects_strict_dominance() -> None:
    best = {pillar: 0.8 for pillar in PILLARS}
    dominated = {**best, "family": 0.7}
    assert pareto_optimal([best, dict(best), dominated]) == [True, True, False]


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
