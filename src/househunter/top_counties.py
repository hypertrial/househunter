from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Any

from .errors import HouseHunterError

DIMENSIONS = ("hazard", "community", "mountain", "cost", "home")
VALUE_FIELDS = {
    "hazard": "res_hazard_npctl",
    "community": "community_conditions_group",
    "mountain": "mountain_magnitude",
    "cost": "cost_of_living_index",
    "home": "home_buying_power_percentile",
}
REQUIRED_LAYERS = (
    "residential-hazard",
    "community-conditions",
    "mountain",
    "cost-of-living",
    "home-costs",
)
PRESETS: dict[str, tuple[float, float, float, float, float]] = {
    "balanced": (0.20, 0.20, 0.20, 0.20, 0.20),
    "safety-health": (0.40, 0.25, 0.10, 0.10, 0.15),
    "affordability": (0.10, 0.10, 0.05, 0.35, 0.40),
    "mountain-lifestyle": (0.10, 0.10, 0.50, 0.10, 0.20),
}
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
        "Import an approved county file with "
        "`househunter import-home-market FILE --acknowledge-personal-use`, "
        "then run `househunter build`."
    ),
}
PREFERENCE_NOTICE = (
    "Preference-fit is a user-selected TOPSIS model, not a HouseHunter map score "
    "or property assessment."
)


@dataclass(frozen=True, slots=True)
class RankedCounty:
    rank: int
    place_id: str
    name: str
    state: str
    preference_fit: float
    pareto_optimal: bool
    values: dict[str, float]
    utilities: dict[str, float]


@dataclass(frozen=True, slots=True)
class PreferenceRanking:
    preset: str
    weights: dict[str, float]
    eligible_count: int
    limit: int
    items: list[RankedCounty]


def preset_weights(preset: str) -> dict[str, float]:
    selected = preset.lower()
    if selected not in PRESETS:
        names = ", ".join(PRESETS)
        raise HouseHunterError(f"Top-counties preset must be {names}")
    return dict(zip(DIMENSIONS, PRESETS[selected], strict=True))


def require_complete_national_snapshot(metadata: Mapping[str, Any]) -> None:
    scope = metadata.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "national":
        raise HouseHunterError(
            "`househunter top-counties` requires a national snapshot; "
            "rebuild with `househunter build`"
        )
    layers = {
        item["key"]: item
        for item in metadata.get("layers") or []
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    missing = [
        key
        for key in REQUIRED_LAYERS
        if not isinstance(layers.get(key), dict) or layers[key].get("availability") != "available"
    ]
    if not missing:
        return
    details = "; ".join(f"{key}: {LAYER_PREP[key]}" for key in missing)
    raise HouseHunterError(
        "`househunter top-counties` needs all five layers available. Missing "
        f"{', '.join(missing)}. {details}"
    )


def average_tie_percentile(values: Sequence[float], *, invert: bool = False) -> list[float]:
    count = len(values)
    if count == 0:
        return []
    if count == 1:
        return [1.0]
    indexed = sorted(
        enumerate(values),
        key=lambda item: item[1],
        reverse=invert,
    )
    ranks = [0.0] * count
    index = 0
    while index < count:
        end = index
        while end + 1 < count and indexed[end + 1][1] == indexed[index][1]:
            end += 1
        average = (index + 1 + end + 1) / 2
        for position in range(index, end + 1):
            ranks[indexed[position][0]] = average
        index = end + 1
    return [(rank - 1) / (count - 1) for rank in ranks]


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


def eligible_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for row in candidates:
        values: dict[str, float] = {}
        complete = True
        for dimension, field in VALUE_FIELDS.items():
            number = _finite(row.get(field))
            if number is None:
                complete = False
                break
            values[dimension] = number
        if not complete:
            continue
        eligible.append(
            {
                "place_id": str(row["place_id"]),
                "name": str(row.get("name") or row["place_id"]),
                "state": str(row.get("state") or ""),
                "values": values,
            }
        )
    return eligible


def utilities_for(eligible: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    mountain = average_tie_percentile([row["values"]["mountain"] for row in eligible])
    cost = average_tie_percentile([row["values"]["cost"] for row in eligible], invert=True)
    utilities: list[dict[str, float]] = []
    for index, row in enumerate(eligible):
        values = row["values"]
        utilities.append(
            {
                "hazard": 1 - values["hazard"] / 100,
                "community": (10 - values["community"]) / 9,
                "mountain": mountain[index],
                "cost": cost[index],
                "home": values["home"] / 100,
            }
        )
    return utilities


def topsis_closeness(
    utilities: Sequence[Mapping[str, float]],
    weights: Mapping[str, float],
) -> list[float]:
    if not utilities:
        return []
    weighted = [
        [weights[dimension] * row[dimension] for dimension in DIMENSIONS] for row in utilities
    ]
    ideal = [max(column[index] for column in weighted) for index in range(len(DIMENSIONS))]
    nadir = [min(column[index] for column in weighted) for index in range(len(DIMENSIONS))]
    scores: list[float] = []
    for point in weighted:
        toward_ideal = sqrt(
            sum((value - best) ** 2 for value, best in zip(point, ideal, strict=True))
        )
        toward_nadir = sqrt(
            sum((value - worst) ** 2 for value, worst in zip(point, nadir, strict=True))
        )
        total = toward_ideal + toward_nadir
        scores.append(1.0 if total == 0 else toward_nadir / total)
    return scores


def pareto_optimal(utilities: Sequence[Mapping[str, float]]) -> list[bool]:
    flags = [True] * len(utilities)
    for index, candidate in enumerate(utilities):
        for other_index, other in enumerate(utilities):
            if other_index == index:
                continue
            better_or_equal = all(
                other[dimension] >= candidate[dimension] for dimension in DIMENSIONS
            )
            strictly_better = any(
                other[dimension] > candidate[dimension] for dimension in DIMENSIONS
            )
            if better_or_equal and strictly_better:
                flags[index] = False
                break
    return flags


def rank_counties(
    candidates: Sequence[Mapping[str, Any]],
    *,
    preset: str = "balanced",
    limit: int = 10,
) -> PreferenceRanking:
    if not 1 <= limit <= 500:
        raise HouseHunterError("Top-counties limit must be between 1 and 500")
    weights = preset_weights(preset)
    eligible = eligible_candidates(candidates)
    utilities = utilities_for(eligible)
    scores = topsis_closeness(utilities, weights)
    optimal = pareto_optimal(utilities)
    ordered = sorted(
        range(len(eligible)),
        key=lambda index: (-scores[index], eligible[index]["place_id"]),
    )
    items = [
        RankedCounty(
            rank=rank,
            place_id=eligible[index]["place_id"],
            name=eligible[index]["name"],
            state=eligible[index]["state"],
            preference_fit=scores[index],
            pareto_optimal=optimal[index],
            values={
                VALUE_FIELDS[dimension]: eligible[index]["values"][dimension]
                for dimension in DIMENSIONS
            },
            utilities=dict(utilities[index]),
        )
        for rank, index in enumerate(ordered[:limit], start=1)
    ]
    return PreferenceRanking(
        preset=preset.lower(),
        weights=weights,
        eligible_count=len(eligible),
        limit=limit,
        items=items,
    )
