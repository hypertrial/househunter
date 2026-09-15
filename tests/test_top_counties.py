from __future__ import annotations

import math

import pytest

from househunter.errors import HouseHunterError
from househunter.top_counties import (
    LAYER_PREP,
    PRESETS,
    REQUIRED_LAYERS,
    VALUE_FIELDS,
    average_tie_percentile,
    rank_counties,
    require_complete_national_snapshot,
)


def _county(
    place_id: str,
    *,
    hazard: float = 50,
    group: int = 5,
    mountain: float = 1.0,
    cost: float = 100,
    home: float = 50,
    name: str | None = None,
    state: str = "CO",
    **extra: object,
) -> dict[str, object]:
    return {
        "place_id": place_id,
        "name": name or place_id,
        "state": state,
        "res_hazard_npctl": hazard,
        "community_conditions_group": group,
        "mountain_magnitude": mountain,
        "cost_of_living_index": cost,
        "home_buying_power_percentile": home,
        **extra,
    }


def test_average_tie_percentile_uses_midranks() -> None:
    assert average_tie_percentile([1.0, 2.0, 2.0, 4.0]) == [0.0, 0.5, 0.5, 1.0]
    assert average_tie_percentile([90.0, 90.0, 110.0], invert=True) == [0.75, 0.75, 0.0]
    assert average_tie_percentile([3.0]) == [1.0]
    assert average_tie_percentile([]) == []
    assert average_tie_percentile([100.0, 100.0], invert=True) == [0.5, 0.5]


def test_presets_match_approved_weights() -> None:
    assert PRESETS["balanced"] == (0.20, 0.20, 0.20, 0.20, 0.20)
    assert PRESETS["safety-health"] == (0.40, 0.25, 0.10, 0.10, 0.15)
    assert PRESETS["affordability"] == (0.10, 0.10, 0.05, 0.35, 0.40)
    assert PRESETS["mountain-lifestyle"] == (0.10, 0.10, 0.50, 0.10, 0.20)


def test_directions_prefer_safer_healthier_mountain_cheaper_counties() -> None:
    safer = _county("08013", hazard=10, group=1, mountain=3.0, cost=90, home=80, name="Boulder")
    worse = _county("08031", hazard=90, group=9, mountain=0.2, cost=130, home=10, name="Denver")
    ranking = rank_counties([worse, safer], limit=2)
    assert [item.place_id for item in ranking.items] == ["08013", "08031"]
    assert ranking.items[0].utilities["hazard"] > ranking.items[1].utilities["hazard"]
    assert ranking.items[0].utilities["community"] > ranking.items[1].utilities["community"]
    assert ranking.items[0].utilities["mountain"] > ranking.items[1].utilities["mountain"]
    assert ranking.items[0].utilities["cost"] > ranking.items[1].utilities["cost"]
    assert ranking.items[0].utilities["home"] > ranking.items[1].utilities["home"]
    assert ranking.items[0].preference_fit > ranking.items[1].preference_fit
    assert ranking.items[0].pareto_optimal is True
    assert ranking.items[1].pareto_optimal is False


def test_pareto_dominating_county_is_not_ranked_below_dominated() -> None:
    better = _county("01001", hazard=20, group=2, mountain=2.0, cost=95, home=70)
    worse = _county("02001", hazard=21, group=3, mountain=1.9, cost=96, home=69)
    ranking = rank_counties([worse, better], limit=2)
    assert ranking.items[0].place_id == "01001"
    assert ranking.items[0].preference_fit >= ranking.items[1].preference_fit
    assert ranking.items[0].pareto_optimal is True
    assert ranking.items[1].pareto_optimal is False


def test_missing_primary_values_are_excluded_not_imputed() -> None:
    complete = _county("01001")
    missing_mountain = _county("02001")
    missing_mountain["mountain_magnitude"] = None
    missing_group = _county("08013")
    missing_group["community_conditions_group"] = None
    ranking = rank_counties([complete, missing_mountain, missing_group], limit=10)
    assert ranking.eligible_count == 1
    assert [item.place_id for item in ranking.items] == ["01001"]
    assert ranking.items[0].values["mountain_magnitude"] == 1.0


def test_identical_scores_break_ties_by_county_fips() -> None:
    left = _county("02001", name="B")
    right = _county("01001", name="A")
    ranking = rank_counties([left, right], limit=2)
    assert [item.place_id for item in ranking.items] == ["01001", "02001"]
    assert ranking.items[0].preference_fit == ranking.items[1].preference_fit == 1.0
    assert ranking.items[0].pareto_optimal is True
    assert ranking.items[1].pareto_optimal is True


def test_empty_and_single_candidate_sets() -> None:
    empty = rank_counties([], limit=10)
    assert empty.eligible_count == 0
    assert empty.items == []
    single = rank_counties([_county("08013")], limit=10)
    assert single.eligible_count == 1
    assert single.items[0].preference_fit == 1.0
    assert single.items[0].pareto_optimal is True


def test_unknown_preset_and_limit_are_rejected() -> None:
    with pytest.raises(HouseHunterError, match="preset"):
        rank_counties([_county("01001")], preset="livability")
    with pytest.raises(HouseHunterError, match="limit"):
        rank_counties([_county("01001")], limit=0)
    with pytest.raises(HouseHunterError, match="limit"):
        rank_counties([_county("01001")], limit=501)
    bounded = rank_counties([_county("01001"), _county("02001")], limit=1)
    assert bounded.limit == 1
    assert bounded.eligible_count == 2
    assert len(bounded.items) == 1
    assert rank_counties([_county("01001")], limit=500).items[0].place_id == "01001"


def test_named_presets_change_order_when_tradeoffs_exist() -> None:
    safe = _county("01001", hazard=0, group=1, mountain=0.0, cost=200, home=0, name="Safe")
    cheap = _county("02001", hazard=80, group=8, mountain=0.1, cost=70, home=100, name="Cheap")
    mountain = _county("08013", hazard=50, group=5, mountain=4.0, cost=120, home=40, name="Peak")
    rows = [safe, cheap, mountain]
    safety = rank_counties(rows, preset="safety-health", limit=3)
    affordability = rank_counties(rows, preset="affordability", limit=3)
    outdoors = rank_counties(rows, preset="mountain-lifestyle", limit=3)
    assert safety.items[0].place_id == "01001"
    assert affordability.items[0].place_id == "02001"
    assert outdoors.items[0].place_id == "08013"


def test_snapshot_gate_requires_national_complete_layers() -> None:
    layers = [
        {"key": key, "availability": "available"}
        for key in (
            "residential-hazard",
            "community-conditions",
            "mountain",
            "cost-of-living",
            "home-costs",
        )
    ]
    require_complete_national_snapshot(
        {"scope": {"kind": "national", "state": None}, "layers": layers}
    )
    with pytest.raises(HouseHunterError, match="national snapshot"):
        require_complete_national_snapshot(
            {"scope": {"kind": "state", "state": "CO"}, "layers": layers}
        )
    incomplete = [dict(layer) for layer in layers]
    incomplete[2]["availability"] = "unavailable"
    incomplete[3]["availability"] = "unavailable"
    with pytest.raises(HouseHunterError, match="mountain, cost-of-living"):
        require_complete_national_snapshot(
            {"scope": {"kind": "national", "state": None}, "layers": incomplete}
        )


def test_utilities_match_specified_higher_is_better_transforms() -> None:
    best = _county("01001", hazard=0, group=1, mountain=4.0, cost=80, home=100)
    worst = _county("02001", hazard=100, group=10, mountain=0.0, cost=120, home=0)
    ranking = rank_counties([worst, best], limit=2)
    by_id = {item.place_id: item for item in ranking.items}
    assert by_id["01001"].utilities == {
        "hazard": 1.0,
        "community": 1.0,
        "mountain": 1.0,
        "cost": 1.0,
        "home": 1.0,
    }
    assert by_id["02001"].utilities == {
        "hazard": 0.0,
        "community": 0.0,
        "mountain": 0.0,
        "cost": 0.0,
        "home": 0.0,
    }


@pytest.mark.parametrize("field", list(VALUE_FIELDS.values()))
@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -float("inf"), "x"])
def test_non_finite_primary_values_are_excluded(field: str, bad: object) -> None:
    complete = _county("01001")
    broken = _county("02001")
    broken[field] = bad
    ranking = rank_counties([complete, broken], limit=10)
    assert ranking.eligible_count == 1
    assert [item.place_id for item in ranking.items] == ["01001"]
    assert not any(
        value is None or (isinstance(value, float) and not math.isfinite(value))
        for value in ranking.items[0].values.values()
    )


def test_missing_primary_keys_are_excluded() -> None:
    complete = _county("01001")
    for field in VALUE_FIELDS.values():
        broken = _county("02001")
        del broken[field]
        ranking = rank_counties([complete, broken], limit=10)
        assert ranking.eligible_count == 1, field
        assert ranking.items[0].place_id == "01001"


def test_mountain_partial_coverage_stays_eligible_when_magnitude_is_finite() -> None:
    complete = _county("01001")
    partial = _county(
        "02001",
        mountain=2.5,
        mountain_coverage_status="partial",
        res_hazard_data_quality="partial",
    )
    ranking = rank_counties([complete, partial], limit=10)
    assert ranking.eligible_count == 2
    assert {item.place_id for item in ranking.items} == {"01001", "02001"}


def test_excluded_counties_do_not_shift_empirical_percentiles() -> None:
    low = _county("01001", mountain=1.0, cost=110)
    high = _county("02001", mountain=2.0, cost=90)
    outlier = _county("08013", mountain=100.0, cost=10)
    outlier["res_hazard_npctl"] = None
    ranking = rank_counties([low, high, outlier], limit=10)
    assert ranking.eligible_count == 2
    by_id = {item.place_id: item for item in ranking.items}
    assert by_id["01001"].utilities["mountain"] == 0.0
    assert by_id["02001"].utilities["mountain"] == 1.0
    assert by_id["01001"].utilities["cost"] == 0.0
    assert by_id["02001"].utilities["cost"] == 1.0


def test_home_costs_uses_buying_power_not_square_feet() -> None:
    low_power = _county("01001", home=1)
    low_power["home_sqft_for_1m"] = 50_000
    high_power = _county("02001", home=99)
    high_power["home_sqft_for_1m"] = 1
    ranking = rank_counties([low_power, high_power], limit=2)
    assert [item.place_id for item in ranking.items] == ["02001", "01001"]
    assert ranking.items[0].utilities["home"] == pytest.approx(0.99)
    assert ranking.items[1].utilities["home"] == pytest.approx(0.01)


def test_pareto_flags_use_eligible_set_not_ineligible_dominators() -> None:
    dominated = _county("02001", hazard=10, group=2, mountain=4.0, cost=90, home=90)
    other = _county("08013", hazard=80, group=8, mountain=6.0, cost=120, home=10)
    ineligible = _county("01001", hazard=0, group=1, mountain=5.0, cost=80, home=100)
    ineligible["home_buying_power_percentile"] = None
    without = rank_counties([ineligible, dominated, other], limit=10)
    assert without.eligible_count == 2
    flags = {item.place_id: item.pareto_optimal for item in without.items}
    assert flags == {"02001": True, "08013": True}

    eligible_dominator = _county("01001", hazard=0, group=1, mountain=5.0, cost=80, home=100)
    with_dominator = rank_counties([eligible_dominator, dominated, other], limit=10)
    flags = {item.place_id: item.pareto_optimal for item in with_dominator.items}
    assert flags["01001"] is True
    assert flags["02001"] is False
    assert flags["08013"] is True


def test_preset_is_case_insensitive_and_empty_name_falls_back() -> None:
    row = _county("01001", name="Named")
    row["name"] = ""
    del row["state"]
    ranking = rank_counties([row], preset="SAFETY-HEALTH", limit=1)
    assert ranking.preset == "safety-health"
    assert ranking.weights["hazard"] == 0.40
    assert ranking.items[0].name == "01001"
    assert ranking.items[0].state == ""


def test_snapshot_gate_rejects_absent_partial_and_unscoped_layers() -> None:
    layers = [{"key": key, "availability": "available"} for key in REQUIRED_LAYERS]
    national = {"scope": {"kind": "national", "state": None}, "layers": layers}
    require_complete_national_snapshot(national)

    with pytest.raises(HouseHunterError, match="national snapshot"):
        require_complete_national_snapshot({"scope": "national", "layers": layers})
    with pytest.raises(HouseHunterError, match="national snapshot"):
        require_complete_national_snapshot({"layers": layers})
    with pytest.raises(HouseHunterError, match="national snapshot"):
        require_complete_national_snapshot(
            {"scope": {"kind": "National", "state": None}, "layers": layers}
        )

    omitted = [layer for layer in layers if layer["key"] != "home-costs"]
    with pytest.raises(HouseHunterError, match="home-costs") as omitted_error:
        require_complete_national_snapshot(
            {"scope": {"kind": "national", "state": None}, "layers": omitted}
        )
    assert LAYER_PREP["home-costs"] in str(omitted_error.value)

    partial = [dict(layer) for layer in layers]
    partial[0]["availability"] = "partial"
    with pytest.raises(HouseHunterError, match="residential-hazard"):
        require_complete_national_snapshot(
            {"scope": {"kind": "national", "state": None}, "layers": partial}
        )

    with pytest.raises(HouseHunterError, match="all five layers available"):
        require_complete_national_snapshot(
            {"scope": {"kind": "national", "state": None}, "layers": None}
        )
