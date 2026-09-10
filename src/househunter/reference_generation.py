from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import polars as pl


def remove_obsolete_assets(output: Path) -> None:
    expected = {"places_2020.parquet", "place_tract_weights_2020.parquet"}
    metadata_path = output / "reference_metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    generated = metadata.get("row_counts", {})
    if not isinstance(generated, dict):
        return
    for asset in output.glob("*.parquet"):
        if asset.stem in generated and asset.name not in expected:
            asset.unlink()


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
