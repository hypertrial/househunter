from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import polars as pl


def align_acs_context(
    places: pl.DataFrame, acs: pl.DataFrame
) -> tuple[pl.DataFrame, dict[str, Any]]:
    if acs["place_id"].n_unique() != acs.height:
        raise ValueError("ACS context contains duplicate Place IDs")
    place_ids = places.select("place_id")
    missing = place_ids.join(acs.select("place_id"), on="place_id", how="anti").sort("place_id")
    excluded = acs.select("place_id").join(place_ids, on="place_id", how="anti").sort("place_id")
    aligned = place_ids.join(acs, on="place_id", how="left").sort("place_id")
    return aligned, {
        "strategy": "left join 2024 ACS context onto canonical 2020 Place IDs",
        "matched_places": aligned.height - missing.height,
        "missing_2020_place_ids": missing["place_id"].to_list(),
        "excluded_2024_only_place_ids": excluded["place_id"].to_list(),
    }


def reconcile_connecticut(
    tract_housing: dict[tuple[str, str], int],
    ct_tract_totals: dict[str, int],
    fema_path: Path,
) -> tuple[dict[tuple[str, str], int], dict[str, Any]]:
    fema_ids = pl.read_parquet(fema_path).filter(pl.col("tract_id").str.starts_with("09"))[
        "tract_id"
    ]
    by_tract_code: dict[str, list[str]] = defaultdict(list)
    for tract_id in fema_ids:
        by_tract_code[tract_id[-6:]].append(tract_id)
    splits: dict[str, dict[str, Any]] = {}
    mapping: dict[str, str] = {}
    for old_tract, housing in ct_tract_totals.items():
        candidates = by_tract_code.get(old_tract[-6:], [])
        if len(candidates) == 1:
            mapping[old_tract] = candidates[0]
        elif old_tract == "09001990000" and housing == 0 and not candidates:
            splits[old_tract] = {
                "documented_new_tracts": ["09120990000", "09190990000"],
                "fema_rows": [],
                "housing_units": housing,
            }
        else:
            raise ValueError(
                f"Connecticut tract {old_tract} has {housing} housing units "
                f"and candidates {candidates}"
            )
    if set(splits) != {"09001990000"}:
        raise ValueError(f"Expected only the documented Connecticut water split, got {splits}")

    output: dict[tuple[str, str], int] = defaultdict(int)
    for (place_id, old_tract), housing in tract_housing.items():
        if not old_tract.startswith("09"):
            output[(place_id, old_tract)] += housing
        elif old_tract in mapping:
            output[(place_id, mapping[old_tract])] += housing
    return dict(output), {
        "strategy": "unique tract-code match to FEMA v1.20",
        "checked_tracts": len(ct_tract_totals),
        "splits": splits,
    }
