from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby
from math import isfinite
from typing import Any

from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .ranking_reference import (
    CALIBRATION_ID,
    CRIME_COVERAGE_FLOOR,
    HOMESCHOOL_NOTICE,
    METHODOLOGY_ID,
    load_appalachia_counties,
)
from .ranking_reference import (
    average_tie_percentile as average_tie_percentile,
)

PILLARS = ("safety", "health", "affordability", "opportunity", "lifestyle", "family")
PRESETS: dict[str, tuple[float, float, float, float, float, float]] = {
    "balanced": (0.20, 0.15, 0.25, 0.15, 0.15, 0.10),
    "safety-health": (0.35, 0.25, 0.15, 0.10, 0.05, 0.10),
    "affordability": (0.15, 0.10, 0.45, 0.15, 0.05, 0.10),
    "mountain-lifestyle": (0.10, 0.10, 0.15, 0.10, 0.45, 0.10),
}
WEIGHT_TOTAL_TOLERANCE = Decimal("1e-12")
PILLAR_FIELDS = {
    "safety": "u_safety",
    "health": "u_health",
    "affordability": "u_affordability",
    "opportunity": "u_opportunity",
    "lifestyle": "u_lifestyle",
    "family": "u_family",
}
POPULATION_FLOOR = 25_000
DEFAULT_MIN_POPULATION = POPULATION_FLOOR
RANK_POLICY = "competition"
DEFAULT_MIN_ACTIVE_LISTINGS = 100
DEFAULT_MIN_VALID_MONTHS = 9
KNOWN_REGIONS = ("appalachia",)
KNOWN_STATES = frozenset(
    state for state in STATE_BY_FIPS.values() if state not in {"AS", "GU", "MP", "PR", "VI"}
)
PREFERENCE_NOTICE = (
    "Preference-fit is a user-selected weighted-utility model (top-counties-v2), "
    "not a HouseHunter map score, universal livability ranking, or property assessment. "
    + HOMESCHOOL_NOTICE
)
REQUIRED_LAYERS = (
    "residential-hazard",
    "community-conditions",
    "mountain",
    "cost-of-living",
    "home-costs",
)
LAYER_PREP = {
    "residential-hazard": (
        "Download FEMA sources with `househunter download --source all`, "
        "then run `househunter build`."
    ),
    "community-conditions": (
        "Download CHR&R with `househunter download --source all`, then run `househunter build`."
    ),
    "mountain": (
        "Promote a validated national Mountain compact release, then run `househunter build`."
    ),
    "cost-of-living": (
        "Download BEA RPP with `househunter download --source bea_rpp`, "
        "then run `househunter build`."
    ),
    "home-costs": (
        "Import approved county files with "
        "`househunter import-home-market FILE --acknowledge-personal-use`, "
        "then run `househunter build`."
    ),
}


@dataclass(frozen=True, slots=True)
class RankedCounty:
    rank: int
    national_rank: int
    filtered_rank: int
    place_id: str
    name: str
    state: str
    preference_fit: float
    pareto_optimal: bool
    values: dict[str, float | int | str | None]
    utilities: dict[str, float]
    pillars: dict[str, float]
    weights: dict[str, float]


@dataclass(frozen=True, slots=True)
class PreferenceRanking:
    methodology_id: str
    calibration_id: str
    preset: str
    weights: dict[str, float]
    reference_count: int
    eligible_count: int
    filtered_count: int
    limit: int
    gates: dict[str, Any]
    exclusions: dict[str, int]
    vintages: dict[str, Any]
    items: list[RankedCounty]


@dataclass(frozen=True, slots=True)
class CountyFitRow:
    place_id: str
    name: str
    state: str
    active_value: float | None
    eligible: bool
    exclusion_reason: str | None
    national_rank: int | None
    filtered_rank: int | None
    pareto_optimal: bool | None
    values: dict[str, float | int | str | None]
    utilities: dict[str, float | None]
    pillars: dict[str, float | None]


@dataclass(frozen=True, slots=True)
class CountyFitEvaluation:
    methodology_id: str
    calibration_id: str
    view: str
    preset: str | None
    weights: dict[str, float]
    reference_count: int
    national_count: int
    eligible_count: int
    filtered_count: int
    gates: dict[str, Any]
    exclusions: dict[str, int]
    vintages: dict[str, Any]
    rows: list[CountyFitRow]


def preset_weights(preset: str) -> dict[str, float]:
    selected = preset.lower()
    if selected not in PRESETS:
        names = ", ".join(PRESETS)
        raise HouseHunterError(f"Top-counties preset must be {names}")
    weights = dict(zip(PILLARS, PRESETS[selected], strict=True))
    if not _weights_total_is_valid(weights):
        raise HouseHunterError("Top-counties preset weights must sum to 1")
    return weights


def _weights_total_is_valid(weights: Mapping[str, float]) -> bool:
    total = sum((Decimal(str(value)) for value in weights.values()), start=Decimal(0))
    return abs(total - Decimal(1)) <= WEIGHT_TOTAL_TOLERANCE


def resolve_weights(
    preset: str | None,
    custom_weights: Mapping[str, float | None] | None = None,
) -> tuple[str | None, dict[str, float]]:
    if custom_weights is None:
        selected = preset or "balanced"
        return selected.lower(), preset_weights(selected)
    missing = sorted(
        pillar
        for pillar in PILLARS
        if pillar not in custom_weights or custom_weights[pillar] is None
    )
    extra = sorted(set(custom_weights) - set(PILLARS))
    if missing or extra:
        detail = [*(f"missing {name}" for name in missing), *(f"unknown {name}" for name in extra)]
        raise HouseHunterError("Custom weights require all six pillars: " + ", ".join(detail))
    weights: dict[str, float] = {}
    for pillar in PILLARS:
        value = _finite(custom_weights[pillar])
        if value is None or value < 0:
            raise HouseHunterError("Custom weights must be finite and nonnegative")
        weights[pillar] = value
    if not _weights_total_is_valid(weights):
        raise HouseHunterError("Custom weights must total exactly 1")
    return None, weights


def require_complete_national_snapshot(metadata: Mapping[str, Any]) -> None:
    scope = metadata.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "national":
        raise HouseHunterError(
            "`househunter top-counties` requires a national snapshot; "
            "rebuild with `househunter build`"
        )
    schema = metadata.get("schema_version")
    if schema != 13:
        raise HouseHunterError(
            "Ranking v2 requires snapshot schema 13; run `househunter build` to rebuild"
        )


def require_ranking_sidecar(metadata: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    ranking = metadata.get("ranking") if isinstance(metadata.get("ranking"), dict) else {}
    if not ranking.get("available") or not rows:
        raise HouseHunterError(
            "`househunter top-counties` needs the ranking_v2 sidecar. Generate the "
            "maintainer county bundle, import trailing home-market months, then run "
            "`househunter build`."
        )
    if ranking.get("readiness") != "ready":
        raise HouseHunterError(
            "Custom Fit requires at least nine approved housing-history months. Run "
            "`househunter import-home-market FILE --acknowledge-personal-use --history`, "
            "then `househunter build`."
        )


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return number


def _int(value: object) -> int | None:
    number = _finite(value)
    if number is None:
        return None
    return int(number)


def validate_ranking_options(
    *,
    min_population: int | None,
    min_active_listings: int | None,
    min_valid_months: int | None,
    exclude_states: Sequence[str],
    exclude_region: str | None,
    include_states: Sequence[str],
    climate: Mapping[str, float | None],
    min_pillars: Mapping[str, float],
    limit: int | None,
) -> None:
    if limit is not None and not 1 <= limit <= 500:
        raise HouseHunterError("Top-counties limit must be between 1 and 500")
    if min_population is not None and min_population < POPULATION_FLOOR:
        raise HouseHunterError("--min-population must be >= 25,000")
    if min_active_listings is not None and min_active_listings < 0:
        raise HouseHunterError("--min-active-listings must be >= 0")
    if min_valid_months is not None and not 1 <= min_valid_months <= 12:
        raise HouseHunterError("--min-valid-months must be between 1 and 12")
    if exclude_region is not None and exclude_region not in KNOWN_REGIONS:
        raise HouseHunterError("--exclude-region must be appalachia")
    if include_states and exclude_states:
        overlap = sorted(set(include_states) & set(exclude_states))
        if overlap:
            raise HouseHunterError(
                "Cannot include and exclude the same states: " + ", ".join(overlap)
            )
    for name, bound in climate.items():
        if bound is None:
            continue
        if not isfinite(bound):
            raise HouseHunterError(f"{name} must be finite")
        if name.endswith("_days") and bound < 0:
            raise HouseHunterError(f"{name} must be >= 0")
    for low, high in (
        (climate.get("min_jan_temp_f"), climate.get("max_jan_temp_f")),
        (climate.get("min_jul_temp_f"), climate.get("max_jul_temp_f")),
    ):
        if low is not None and high is not None and low > high:
            raise HouseHunterError("Climate minimum cannot exceed its maximum")
    for pillar, value in min_pillars.items():
        if pillar not in PILLARS:
            raise HouseHunterError(f"Unknown pillar gate: {pillar}")
        if not 0 <= value <= 1:
            raise HouseHunterError(f"Pillar minimum for {pillar} must be in [0, 1]")


def _normalized_states(values: Sequence[str]) -> list[str]:
    states = []
    for value in values:
        state = str(value).strip().upper()
        if state not in KNOWN_STATES:
            raise HouseHunterError(f"State filter must be a 50-state/DC abbreviation: {value}")
        states.append(state)
    return states


def _region_fips(region: str | None) -> frozenset[str]:
    if region is None:
        return frozenset()
    if region == "appalachia":
        return load_appalachia_counties()
    raise HouseHunterError(f"Unknown region: {region}")


def _candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    pillars: dict[str, float | None] = {}
    for pillar, field in PILLAR_FIELDS.items():
        pillars[pillar] = _finite(row.get(field))
    crime_coverage = _finite(row.get("crime_coverage"))
    return {
        "place_id": str(row["place_id"]),
        "name": str(row.get("name") or row["place_id"]),
        "state": str(row.get("state") or ""),
        "population": _int(row.get("population")),
        "housing_valid_months": _int(row.get("housing_valid_months")),
        "median_active_listings": _finite(row.get("median_active_listings")),
        "jan_avg_temp_f": _finite(row.get("jan_avg_temp_f")),
        "jul_avg_temp_f": _finite(row.get("jul_avg_temp_f")),
        "extreme_heat_days": _finite(row.get("extreme_heat_days")),
        "extreme_cold_days": _finite(row.get("extreme_cold_days")),
        "pillars": pillars,
        "values": {
            "population": _int(row.get("population")),
            "housing_valid_months": _int(row.get("housing_valid_months")),
            "median_active_listings": _finite(row.get("median_active_listings")),
            "median_ppsf": _finite(row.get("median_ppsf")),
            "sqft_for_1m_t12": _finite(row.get("sqft_for_1m_t12")),
            "res_hazard_npctl": _finite(row.get("res_hazard_npctl")),
            "crime_coverage": crime_coverage,
            "rpp_index": _finite(row.get("rpp_index")),
            "rpp_geography_type": row.get("rpp_geography_type"),
            "mountain_magnitude": _finite(row.get("mountain_magnitude")),
            "community_context_kind": row.get("community_context_kind"),
        },
        "utilities": {
            "hazard": _finite(row.get("u_hazard")),
            "crime_violent": _finite(row.get("u_crime_violent")),
            "crime_property": _finite(row.get("u_crime_property")),
            "crime": _finite(row.get("u_crime")),
            "water": _finite(row.get("u_water")),
            "healthcare": _finite(row.get("u_healthcare")),
            "primary_care": _finite(row.get("u_primary_care")),
            "mental_health": _finite(row.get("u_mental_health")),
            "dental": _finite(row.get("u_dental")),
            "community_context": _finite(row.get("u_community_context")),
            "housing": _finite(row.get("u_housing")),
            "rpp": _finite(row.get("u_rpp")),
            "property_tax": _finite(row.get("u_property_tax")),
            "employment_growth": _finite(row.get("u_employment_growth")),
            "average_weekly_wage": _finite(row.get("u_average_weekly_wage")),
            "commute_under_30": _finite(row.get("u_commute_under_30")),
            "employment": _finite(row.get("u_employment")),
            "broadband": _finite(row.get("u_broadband")),
            "mountain": _finite(row.get("u_mountain")),
            "homeschool": _finite(row.get("u_family")),
        },
    }


def additive_score(pillars: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return sum(weights[name] * pillars[name] for name in PILLARS)


def pareto_optimal(pillars: Sequence[Mapping[str, float]]) -> list[bool]:
    if not pillars:
        return []
    count = len(pillars)
    greater_or_equal_masks = [[0] * count for _ in PILLARS]
    for dimension, name in enumerate(PILLARS):
        ordered = sorted(range(count), key=lambda index: pillars[index][name], reverse=True)
        running_mask = 0
        for _, grouped in groupby(ordered, key=lambda index: pillars[index][name]):
            indexes = list(grouped)
            for index in indexes:
                running_mask |= 1 << index
            for index in indexes:
                greater_or_equal_masks[dimension][index] = running_mask

    exact_masks: dict[tuple[float, ...], int] = {}
    keys: list[tuple[float, ...]] = []
    for index, candidate in enumerate(pillars):
        key = tuple(candidate[name] for name in PILLARS)
        keys.append(key)
        exact_masks[key] = exact_masks.get(key, 0) | (1 << index)

    flags: list[bool] = []
    all_candidates = (1 << count) - 1
    for index, key in enumerate(keys):
        dominators = all_candidates
        for dimension in range(len(PILLARS)):
            dominators &= greater_or_equal_masks[dimension][index]
        flags.append(not (dominators & ~exact_masks[key]))
    return flags


def _climate_excluded(row: Mapping[str, Any], climate: Mapping[str, float | None]) -> bool:
    checks = (
        ("min_jan_temp_f", "jan_avg_temp_f", False),
        ("max_jan_temp_f", "jan_avg_temp_f", True),
        ("min_jul_temp_f", "jul_avg_temp_f", False),
        ("max_jul_temp_f", "jul_avg_temp_f", True),
        ("max_extreme_heat_days", "extreme_heat_days", True),
        ("max_extreme_cold_days", "extreme_cold_days", True),
    )
    active = {key: value for key, value in climate.items() if value is not None}
    if not active:
        return False
    for option, field, upper in checks:
        bound = active.get(option)
        if bound is None:
            continue
        observed = row.get(field)
        if observed is None:
            return True
        if upper and observed > bound:
            return True
        if not upper and observed < bound:
            return True
    return False


def _competition_ranks(
    ordered: Sequence[tuple[float | None, Mapping[str, Any]]],
) -> dict[str, int]:
    ranks: dict[str, int] = {}
    previous_score: float | None = None
    rank = 0
    for position, (score, row) in enumerate(ordered, start=1):
        if position == 1 or score != previous_score:
            rank = position
        ranks[str(row["place_id"])] = rank
        previous_score = score
    return ranks


def evaluate_counties(
    candidates: Sequence[Mapping[str, Any]],
    *,
    view: str = "custom",
    preset: str = "balanced",
    custom_weights: Mapping[str, float | None] | None = None,
    limit: int | None = None,
    min_population: int | None = None,
    min_active_listings: int | None = None,
    min_valid_months: int | None = None,
    states: Sequence[str] = (),
    exclude_states: Sequence[str] = (),
    exclude_region: str | None = None,
    min_jan_temp_f: float | None = None,
    max_jan_temp_f: float | None = None,
    min_jul_temp_f: float | None = None,
    max_jul_temp_f: float | None = None,
    max_extreme_heat_days: float | None = None,
    max_extreme_cold_days: float | None = None,
    min_pillars: Mapping[str, float] | None = None,
    vintages: Mapping[str, Any] | None = None,
    calibration_id: str = CALIBRATION_ID,
) -> CountyFitEvaluation:
    selected_view = (
        view.lower().replace("family-autonomy", "family").replace("custom-fit", "custom")
    )
    if selected_view not in {*PILLARS, "custom"}:
        raise HouseHunterError("County Fit view is invalid")
    if selected_view == "custom":
        selected_preset, weights = resolve_weights(preset, custom_weights)
        if min_active_listings is None:
            min_active_listings = DEFAULT_MIN_ACTIVE_LISTINGS
        if min_valid_months is None:
            min_valid_months = DEFAULT_MIN_VALID_MONTHS
    else:
        if custom_weights is not None:
            raise HouseHunterError("Custom weights apply only to the Custom Fit view")
        selected_preset = None
        weights = {pillar: float(pillar == selected_view) for pillar in PILLARS}
    effective_min_population = (
        POPULATION_FLOOR if min_population is None else min_population
    )
    include_states = _normalized_states(states)
    excluded_states = _normalized_states(exclude_states)
    climate = {
        "min_jan_temp_f": min_jan_temp_f,
        "max_jan_temp_f": max_jan_temp_f,
        "min_jul_temp_f": min_jul_temp_f,
        "max_jul_temp_f": max_jul_temp_f,
        "max_extreme_heat_days": max_extreme_heat_days,
        "max_extreme_cold_days": max_extreme_cold_days,
    }
    pillar_gates = dict(min_pillars or {})
    validate_ranking_options(
        min_population=effective_min_population,
        min_active_listings=min_active_listings,
        min_valid_months=min_valid_months,
        exclude_states=excluded_states,
        exclude_region=exclude_region,
        include_states=include_states,
        climate=climate,
        min_pillars=pillar_gates,
        limit=limit,
    )
    region_fips = _region_fips(exclude_region)
    parsed = [_candidate(row) for row in candidates]
    reason_by_id: dict[str, str | None] = {}
    active_by_id: dict[str, float | None] = {}
    national_candidates: list[tuple[float, dict[str, Any]]] = []
    for row in parsed:
        if selected_view == "custom":
            complete = all(row["pillars"][pillar] is not None for pillar in PILLARS)
            reason = None if complete else "missing_core"
            coverage = _finite(row["values"].get("crime_coverage"))
            if reason is None and (coverage is None or coverage < CRIME_COVERAGE_FLOOR):
                reason = "crime_coverage"
            active = additive_score(row["pillars"], weights) if reason is None else None
        else:
            active = row["pillars"][selected_view]
            reason = None if active is not None else "missing_active"
        if reason is None and (
            row["population"] is None or row["population"] < POPULATION_FLOOR
        ):
            reason = "population"
            active = None
        reason_by_id[row["place_id"]] = reason
        active_by_id[row["place_id"]] = active
        if reason is None and active is not None:
            national_candidates.append((active, row))
    national_candidates.sort(key=lambda item: (-item[0], item[1]["place_id"]))
    national_ranks = _competition_ranks(national_candidates)

    initially_eligible = 0
    for _, row in national_candidates:
        place_id = row["place_id"]
        reason = reason_by_id[place_id]
        if (
            reason is None
            and min_valid_months is not None
            and (
                row["housing_valid_months"] is None
                or row["housing_valid_months"] < min_valid_months
            )
        ):
            reason = "valid_months"
        if (
            reason is None
            and min_active_listings is not None
            and (
                row["median_active_listings"] is None
                or row["median_active_listings"] < min_active_listings
            )
        ):
            reason = "active_listings"
        if (
            reason is None
            and row["population"] is not None
            and row["population"] < effective_min_population
        ):
            reason = "population"
        reason_by_id[place_id] = reason
        if reason is None:
            initially_eligible += 1

    for _, row in national_candidates:
        place_id = row["place_id"]
        if reason_by_id[place_id] is not None:
            continue
        reason = None
        if include_states and row["state"] not in include_states:
            reason = "state_filter"
        elif row["state"] in excluded_states:
            reason = "state_exclusion"
        elif place_id in region_fips:
            reason = "region_exclusion"
        elif _climate_excluded(row, climate):
            reason = "climate"
        else:
            for pillar in PILLARS:
                if pillar not in pillar_gates:
                    continue
                value = row["pillars"][pillar]
                if value is None or value < pillar_gates[pillar]:
                    reason = f"min_{pillar}"
                    break
        reason_by_id[place_id] = reason

    filtered = [
        (active_by_id[row["place_id"]], row)
        for row in parsed
        if reason_by_id[row["place_id"]] is None
    ]
    filtered.sort(key=lambda item: (-float(item[0]), item[1]["place_id"]))
    filtered_ranks = _competition_ranks(filtered)
    pareto_by_id: dict[str, bool] = {}
    if selected_view == "custom":
        flags = pareto_optimal(
            [{pillar: float(row["pillars"][pillar]) for pillar in PILLARS} for _, row in filtered]
        )
        pareto_by_id = {
            row["place_id"]: flag for (_, row), flag in zip(filtered, flags, strict=True)
        }

    exclusions: Counter[str] = Counter()
    for reason in reason_by_id.values():
        if reason is not None:
            exclusions[reason] += 1
    gates = {
        "population_floor": POPULATION_FLOOR,
        "min_population": effective_min_population,
        "rank_policy": RANK_POLICY,
        "min_active_listings": min_active_listings,
        "min_valid_months": min_valid_months,
        "states": include_states,
        "exclude_states": excluded_states,
        "exclude_region": exclude_region,
        "climate": {key: value for key, value in climate.items() if value is not None},
        "min_pillars": pillar_gates,
    }
    rows = [
        CountyFitRow(
            place_id=row["place_id"],
            name=row["name"],
            state=row["state"],
            active_value=active_by_id[row["place_id"]],
            eligible=reason_by_id[row["place_id"]] is None,
            exclusion_reason=reason_by_id[row["place_id"]],
            national_rank=national_ranks.get(row["place_id"]),
            filtered_rank=filtered_ranks.get(row["place_id"]),
            pareto_optimal=pareto_by_id.get(row["place_id"]),
            values=row["values"],
            utilities=row["utilities"],
            pillars=row["pillars"],
        )
        for row in sorted(parsed, key=lambda value: value["place_id"])
    ]
    if limit is not None:
        allowed = {row["place_id"] for _, row in filtered[:limit]} | {
            row.place_id for row in rows if not row.eligible
        }
        rows = [row for row in rows if row.place_id in allowed]
    return CountyFitEvaluation(
        methodology_id=METHODOLOGY_ID,
        calibration_id=calibration_id,
        view=selected_view,
        preset=selected_preset,
        weights=weights,
        reference_count=len(candidates),
        national_count=len(national_candidates),
        eligible_count=initially_eligible,
        filtered_count=len(filtered),
        gates=gates,
        exclusions=dict(exclusions),
        vintages=dict(vintages or {}),
        rows=rows,
    )


def rank_counties(
    candidates: Sequence[Mapping[str, Any]],
    *,
    preset: str = "balanced",
    custom_weights: Mapping[str, float | None] | None = None,
    limit: int = 10,
    min_population: int = DEFAULT_MIN_POPULATION,
    min_active_listings: int = DEFAULT_MIN_ACTIVE_LISTINGS,
    min_valid_months: int = DEFAULT_MIN_VALID_MONTHS,
    states: Sequence[str] = (),
    exclude_states: Sequence[str] = (),
    exclude_region: str | None = None,
    min_jan_temp_f: float | None = None,
    max_jan_temp_f: float | None = None,
    min_jul_temp_f: float | None = None,
    max_jul_temp_f: float | None = None,
    max_extreme_heat_days: float | None = None,
    max_extreme_cold_days: float | None = None,
    min_pillars: Mapping[str, float] | None = None,
    vintages: Mapping[str, Any] | None = None,
    calibration_id: str = CALIBRATION_ID,
) -> PreferenceRanking:
    if not 1 <= limit <= 500:
        raise HouseHunterError("Top-counties limit must be between 1 and 500")
    evaluation = evaluate_counties(
        candidates,
        view="custom",
        preset=preset,
        custom_weights=custom_weights,
        limit=None,
        min_population=min_population,
        min_active_listings=min_active_listings,
        min_valid_months=min_valid_months,
        states=states,
        exclude_states=exclude_states,
        exclude_region=exclude_region,
        min_jan_temp_f=min_jan_temp_f,
        max_jan_temp_f=max_jan_temp_f,
        min_jul_temp_f=min_jul_temp_f,
        max_jul_temp_f=max_jul_temp_f,
        max_extreme_heat_days=max_extreme_heat_days,
        max_extreme_cold_days=max_extreme_cold_days,
        min_pillars=min_pillars,
        vintages=vintages,
        calibration_id=calibration_id,
    )
    ranked = sorted(
        (row for row in evaluation.rows if row.eligible),
        key=lambda row: (int(row.filtered_rank or 0), row.place_id),
    )[:limit]
    items = [
        RankedCounty(
            rank=int(row.filtered_rank or 0),
            national_rank=int(row.national_rank or 0),
            filtered_rank=int(row.filtered_rank or 0),
            place_id=row.place_id,
            name=row.name,
            state=row.state,
            preference_fit=float(row.active_value),
            pareto_optimal=bool(row.pareto_optimal),
            values=row.values,
            utilities={key: value for key, value in row.utilities.items() if value is not None},
            pillars={key: float(value) for key, value in row.pillars.items() if value is not None},
            weights=evaluation.weights,
        )
        for row in ranked
    ]
    return PreferenceRanking(
        methodology_id=evaluation.methodology_id,
        calibration_id=evaluation.calibration_id,
        preset=evaluation.preset or "custom",
        weights=evaluation.weights,
        reference_count=evaluation.reference_count,
        eligible_count=evaluation.eligible_count,
        filtered_count=evaluation.filtered_count,
        limit=limit,
        gates=evaluation.gates,
        exclusions=evaluation.exclusions,
        vintages=evaluation.vintages,
        items=items,
    )
