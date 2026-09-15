from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any

from .errors import HouseHunterError
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
PILLAR_FIELDS = {
    "safety": "u_safety",
    "health": "u_health",
    "affordability": "u_affordability",
    "opportunity": "u_opportunity",
    "lifestyle": "u_lifestyle",
    "family": "u_family",
}
DEFAULT_MIN_POPULATION = 25_000
DEFAULT_MIN_ACTIVE_LISTINGS = 100
DEFAULT_MIN_VALID_MONTHS = 9
KNOWN_REGIONS = ("appalachia",)
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


def preset_weights(preset: str) -> dict[str, float]:
    selected = preset.lower()
    if selected not in PRESETS:
        names = ", ".join(PRESETS)
        raise HouseHunterError(f"Top-counties preset must be {names}")
    weights = dict(zip(PILLARS, PRESETS[selected], strict=True))
    if abs(sum(weights.values()) - 1.0) > 1e-12:
        raise HouseHunterError("Top-counties preset weights must sum to 1")
    return weights


def require_complete_national_snapshot(metadata: Mapping[str, Any]) -> None:
    scope = metadata.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "national":
        raise HouseHunterError(
            "`househunter top-counties` requires a national snapshot; "
            "rebuild with `househunter build`"
        )
    schema = metadata.get("schema_version")
    if schema != 12:
        raise HouseHunterError(
            "Ranking v2 requires snapshot schema 12; run `househunter build` to rebuild"
        )


def require_ranking_sidecar(metadata: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    ranking = metadata.get("ranking") if isinstance(metadata.get("ranking"), dict) else {}
    if not ranking.get("available") or not rows:
        raise HouseHunterError(
            "`househunter top-counties` needs the ranking_v2 sidecar. Generate the "
            "maintainer county bundle, import trailing home-market months, then run "
            "`househunter build`."
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
    min_population: int,
    min_active_listings: int,
    min_valid_months: int,
    exclude_states: Sequence[str],
    exclude_region: str | None,
    include_states: Sequence[str],
    climate: Mapping[str, float | None],
    min_pillars: Mapping[str, float],
    limit: int,
) -> None:
    if not 1 <= limit <= 500:
        raise HouseHunterError("Top-counties limit must be between 1 and 500")
    if min_population < 0:
        raise HouseHunterError("--min-population must be >= 0")
    if min_active_listings < 0:
        raise HouseHunterError("--min-active-listings must be >= 0")
    if not 1 <= min_valid_months <= 12:
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
        if name.endswith("_days") and bound < 0:
            raise HouseHunterError(f"{name} must be >= 0")
    for pillar, value in min_pillars.items():
        if pillar not in PILLARS:
            raise HouseHunterError(f"Unknown pillar gate: {pillar}")
        if not 0 <= value <= 1:
            raise HouseHunterError(f"Pillar minimum for {pillar} must be in [0, 1]")


def _normalized_states(values: Sequence[str]) -> list[str]:
    states = []
    for value in values:
        state = str(value).strip().upper()
        if len(state) != 2 or not state.isalpha():
            raise HouseHunterError(f"State filter must be a two-letter abbreviation: {value}")
        states.append(state)
    return states


def _region_fips(region: str | None) -> frozenset[str]:
    if region is None:
        return frozenset()
    if region == "appalachia":
        return load_appalachia_counties()
    raise HouseHunterError(f"Unknown region: {region}")


def _candidate(row: Mapping[str, Any]) -> dict[str, Any] | None:
    pillars: dict[str, float] = {}
    for pillar, field in PILLAR_FIELDS.items():
        number = _finite(row.get(field))
        if number is None:
            return None
        pillars[pillar] = number
    crime_coverage = _finite(row.get("crime_coverage"))
    if crime_coverage is None or crime_coverage < CRIME_COVERAGE_FLOOR:
        return None
    if any(_finite(row.get(field)) is None for field in PILLAR_FIELDS.values()):
        return None
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
            "crime": _finite(row.get("u_crime")),
            "water": _finite(row.get("u_water")),
            "healthcare": _finite(row.get("u_healthcare")),
            "community_context": _finite(row.get("u_community_context")),
            "housing": _finite(row.get("u_housing")),
            "rpp": _finite(row.get("u_rpp")),
            "property_tax": _finite(row.get("u_property_tax")),
            "employment": _finite(row.get("u_employment")),
            "broadband": _finite(row.get("u_broadband")),
            "mountain": _finite(row.get("u_mountain")),
            "homeschool": _finite(row.get("u_family")),
        },
    }


def additive_score(pillars: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return sum(weights[name] * pillars[name] for name in PILLARS)


def pareto_optimal(pillars: Sequence[Mapping[str, float]]) -> list[bool]:
    flags = [True] * len(pillars)
    for index, candidate in enumerate(pillars):
        for other_index, other in enumerate(pillars):
            if other_index == index:
                continue
            better_or_equal = all(other[name] >= candidate[name] for name in PILLARS)
            strictly_better = any(other[name] > candidate[name] for name in PILLARS)
            if better_or_equal and strictly_better:
                flags[index] = False
                break
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


def rank_counties(
    candidates: Sequence[Mapping[str, Any]],
    *,
    preset: str = "balanced",
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
        min_population=min_population,
        min_active_listings=min_active_listings,
        min_valid_months=min_valid_months,
        exclude_states=excluded_states,
        exclude_region=exclude_region,
        include_states=include_states,
        climate=climate,
        min_pillars=pillar_gates,
        limit=limit,
    )
    weights = preset_weights(preset)
    region_fips = _region_fips(exclude_region)
    parsed: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    for row in candidates:
        candidate = _candidate(row)
        if candidate is None:
            exclusions["missing_core"] += 1
            continue
        parsed.append(candidate)
    nationally_scored: list[tuple[float, dict[str, Any]]] = [
        (additive_score(row["pillars"], weights), row) for row in parsed
    ]
    nationally_scored.sort(key=lambda item: (-item[0], item[1]["place_id"]))
    national_ranks = {
        row["place_id"]: rank for rank, (_, row) in enumerate(nationally_scored, start=1)
    }
    eligible: list[tuple[float, dict[str, Any]]] = []
    for score, row in nationally_scored:
        if row["population"] is None or row["population"] < min_population:
            exclusions["population"] += 1
            continue
        if row["housing_valid_months"] is None or row["housing_valid_months"] < min_valid_months:
            exclusions["valid_months"] += 1
            continue
        listings = row["median_active_listings"]
        if listings is None or listings < min_active_listings:
            exclusions["active_listings"] += 1
            continue
        eligible.append((score, row))
    filtered: list[tuple[float, dict[str, Any]]] = []
    for score, row in eligible:
        if include_states and row["state"] not in include_states:
            exclusions["state_filter"] += 1
            continue
        if row["state"] in excluded_states:
            exclusions["state_exclusion"] += 1
            continue
        if row["place_id"] in region_fips:
            exclusions["region_exclusion"] += 1
            continue
        if _climate_excluded(row, climate):
            exclusions["climate"] += 1
            continue
        blocked = False
        for pillar, minimum in pillar_gates.items():
            if row["pillars"][pillar] < minimum:
                exclusions[f"min_{pillar}"] += 1
                blocked = True
                break
        if blocked:
            continue
        filtered.append((score, row))
    if parsed and not eligible and not exclusions:
        exclusions["empty"] += 1
    optimal = pareto_optimal([row["pillars"] for _, row in filtered])
    items = [
        RankedCounty(
            rank=rank,
            national_rank=national_ranks[row["place_id"]],
            filtered_rank=rank,
            place_id=row["place_id"],
            name=row["name"],
            state=row["state"],
            preference_fit=score,
            pareto_optimal=optimal[rank - 1],
            values=row["values"],
            utilities={key: value for key, value in row["utilities"].items() if value is not None},
            pillars=dict(row["pillars"]),
            weights=weights,
        )
        for rank, (score, row) in enumerate(filtered[:limit], start=1)
    ]
    gates = {
        "min_population": min_population,
        "min_active_listings": min_active_listings,
        "min_valid_months": min_valid_months,
        "states": include_states,
        "exclude_states": excluded_states,
        "exclude_region": exclude_region,
        "climate": {key: value for key, value in climate.items() if value is not None},
        "min_pillars": pillar_gates,
    }
    return PreferenceRanking(
        methodology_id=METHODOLOGY_ID,
        calibration_id=calibration_id,
        preset=preset.lower(),
        weights=weights,
        reference_count=len(candidates),
        eligible_count=len(eligible),
        filtered_count=len(filtered),
        limit=limit,
        gates=gates,
        exclusions=dict(exclusions),
        vintages=dict(vintages or {}),
        items=items,
    )
