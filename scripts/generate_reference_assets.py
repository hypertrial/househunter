#!/usr/bin/env python3
"""Generate compact, public Census reference assets for a HouseHunter release.

Raw TIGER/Line and Block Assignment archives remain in the ignored cache directory.
The generated Parquet files contain no geometry or person-level records.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import struct
import time
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import polars as pl

from househunter.reference_generation import align_acs_context, reconcile_connecticut

STATES = {
    "01": ("AL", "ALABAMA"),
    "02": ("AK", "ALASKA"),
    "04": ("AZ", "ARIZONA"),
    "05": ("AR", "ARKANSAS"),
    "06": ("CA", "CALIFORNIA"),
    "08": ("CO", "COLORADO"),
    "09": ("CT", "CONNECTICUT"),
    "10": ("DE", "DELAWARE"),
    "11": ("DC", "DISTRICT_OF_COLUMBIA"),
    "12": ("FL", "FLORIDA"),
    "13": ("GA", "GEORGIA"),
    "15": ("HI", "HAWAII"),
    "16": ("ID", "IDAHO"),
    "17": ("IL", "ILLINOIS"),
    "18": ("IN", "INDIANA"),
    "19": ("IA", "IOWA"),
    "20": ("KS", "KANSAS"),
    "21": ("KY", "KENTUCKY"),
    "22": ("LA", "LOUISIANA"),
    "23": ("ME", "MAINE"),
    "24": ("MD", "MARYLAND"),
    "25": ("MA", "MASSACHUSETTS"),
    "26": ("MI", "MICHIGAN"),
    "27": ("MN", "MINNESOTA"),
    "28": ("MS", "MISSISSIPPI"),
    "29": ("MO", "MISSOURI"),
    "30": ("MT", "MONTANA"),
    "31": ("NE", "NEBRASKA"),
    "32": ("NV", "NEVADA"),
    "33": ("NH", "NEW_HAMPSHIRE"),
    "34": ("NJ", "NEW_JERSEY"),
    "35": ("NM", "NEW_MEXICO"),
    "36": ("NY", "NEW_YORK"),
    "37": ("NC", "NORTH_CAROLINA"),
    "38": ("ND", "NORTH_DAKOTA"),
    "39": ("OH", "OHIO"),
    "40": ("OK", "OKLAHOMA"),
    "41": ("OR", "OREGON"),
    "42": ("PA", "PENNSYLVANIA"),
    "44": ("RI", "RHODE_ISLAND"),
    "45": ("SC", "SOUTH_CAROLINA"),
    "46": ("SD", "SOUTH_DAKOTA"),
    "47": ("TN", "TENNESSEE"),
    "48": ("TX", "TEXAS"),
    "49": ("UT", "UTAH"),
    "50": ("VT", "VERMONT"),
    "51": ("VA", "VIRGINIA"),
    "53": ("WA", "WASHINGTON"),
    "54": ("WV", "WEST_VIRGINIA"),
    "55": ("WI", "WISCONSIN"),
    "56": ("WY", "WYOMING"),
}


def download(url: str, destination: Path, *, display_url: str | None = None) -> Path:
    if destination.is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "HouseHunter/1.0"})
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                temporary.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            os.replace(temporary, destination)
            return destination
        except OSError as exc:
            last_error = exc
            time.sleep(2**attempt)
    detail = type(last_error).__name__ if display_url else str(last_error)
    raise RuntimeError(f"Could not download {display_url or url}: {detail}")


def _dbf_records(source: BinaryIO) -> Iterator[dict[str, str]]:
    header = source.read(32)
    if len(header) != 32:
        raise ValueError("Truncated DBF header")
    record_count = struct.unpack("<I", header[4:8])[0]
    header_length = struct.unpack("<H", header[8:10])[0]
    record_length = struct.unpack("<H", header[10:12])[0]
    fields: list[tuple[str, int]] = []
    while True:
        descriptor = source.read(32)
        if descriptor[0] == 0x0D:
            source.seek(-31, io.SEEK_CUR)
            break
        name = descriptor[:11].split(b"\0", 1)[0].decode("ascii")
        fields.append((name, descriptor[16]))
    source.seek(header_length)
    for _ in range(record_count):
        raw = source.read(record_length)
        if len(raw) != record_length:
            raise ValueError("Truncated DBF record")
        if raw[:1] == b"*":
            continue
        offset = 1
        result: dict[str, str] = {}
        for name, width in fields:
            result[name] = raw[offset : offset + width].decode("latin-1").strip()
            offset += width
        yield result


def dbf_records(archive: Path, suffix: str) -> Iterator[dict[str, str]]:
    with zipfile.ZipFile(archive) as zipped:
        members = [name for name in zipped.namelist() if name.lower().endswith(suffix.lower())]
        if len(members) != 1:
            raise ValueError(f"Expected one {suffix} in {archive}, found {members}")
        with zipped.open(members[0]) as handle:
            yield from _dbf_records(handle)


def place_assignments(archive: Path) -> dict[str, str]:
    with zipfile.ZipFile(archive) as zipped:
        members = [
            name for name in zipped.namelist() if "INCPLACE_CDP" in name and name.endswith(".txt")
        ]
        if len(members) != 1:
            raise ValueError(f"Cannot identify INCPLACE_CDP file in {archive}")
        with zipped.open(members[0]) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"), delimiter="|")
            return {
                row["BLOCKID"]: row["PLACEFP"]
                for row in reader
                if row.get("PLACEFP") and row["PLACEFP"] != "99999"
            }


def census_urls(state_fips: str) -> dict[str, str]:
    abbreviation, name = STATES[state_fips]
    tiger = f"https://www2.census.gov/geo/tiger/TIGER2020PL/STATE/{state_fips}_{name}/{state_fips}"
    return {
        "blocks": f"{tiger}/tl_2020_{state_fips}_tabblock20.zip",
        "places": f"{tiger}/tl_2020_{state_fips}_place20.zip",
        "assignments": (
            "https://www2.census.gov/geo/docs/maps-data/data/baf2020/"
            f"BlockAssign_ST{state_fips}_{abbreviation}.zip"
        ),
    }


def fetch_acs(
    state_fips: str, api_key: str | None, cache: Path
) -> tuple[list[dict[str, str]], Path]:
    output = cache / f"acs_2024_{state_fips}.json"
    if not output.is_file():
        if not api_key:
            raise ValueError(f"Missing cached {output}; provide --census-api-key or CENSUS_API_KEY")
        parameters = urllib.parse.urlencode(
            {
                "get": "NAME,B01003_001E,B25001_001E,B25077_001E",
                "for": "place:*",
                "in": f"state:{state_fips}",
                "key": api_key,
            }
        )
        base_url = "https://api.census.gov/data/2024/acs/acs5"
        download(f"{base_url}?{parameters}", output, display_url=base_url)
    rows = json.loads(output.read_text())
    return [dict(zip(rows[0], values, strict=True)) for values in rows[1:]], output


def integer_or_none(value: str | None) -> int | None:
    if value in {None, ""}:
        return None
    parsed = int(value)
    return parsed if parsed >= 0 else None


def frame_checksum(frame: pl.DataFrame, sort_by: list[str]) -> str:
    payload = json.dumps(
        [list(row) for row in frame.sort(sort_by).iter_rows()],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def generate(cache: Path, output: Path, api_key: str | None, fema_path: Path) -> None:
    all_places: list[dict[str, Any]] = []
    all_acs: list[dict[str, Any]] = []
    tract_housing: dict[tuple[str, str], int] = defaultdict(int)
    ct_tract_totals: dict[str, int] = defaultdict(int)
    source_urls: list[str] = ["https://api.census.gov/data/2024/acs/acs5"]
    raw_checksums: dict[str, str] = {}
    for state_fips, (state, _) in STATES.items():
        print(f"Preparing {state} ({state_fips})", flush=True)
        urls = census_urls(state_fips)
        source_urls.extend(urls.values())
        state_cache = cache / state_fips
        blocks = download(urls["blocks"], state_cache / "blocks.zip")
        places_archive = download(urls["places"], state_cache / "places.zip")
        assignments_archive = download(urls["assignments"], state_cache / "assignments.zip")
        for name, path in {
            "blocks": blocks,
            "places": places_archive,
            "assignments": assignments_archive,
        }.items():
            raw_checksums[f"{state_fips}/{name}"] = sha256_file(path)
        assignments = place_assignments(assignments_archive)
        place_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for block in dbf_records(blocks, ".dbf"):
            block_id = block["GEOID20"]
            housing = int(block["HOUSING20"] or 0)
            if state_fips == "09":
                ct_tract_totals[block_id[:11]] += housing
            place_fips = assignments.get(block_id)
            if not place_fips:
                continue
            place_id = state_fips + place_fips
            population = int(block["POP20"] or 0)
            place_totals[place_id][0] += population
            place_totals[place_id][1] += housing
            tract_housing[(place_id, block_id[:11])] += housing
        for place in dbf_records(places_archive, ".dbf"):
            place_id = place["GEOID20"]
            population, housing = place_totals[place_id]
            all_places.append(
                {
                    "place_id": place_id,
                    "name": place["NAME20"],
                    "state": state,
                    "place_type": "incorporated place" if place["MTFCC20"] == "G4110" else "CDP",
                    "population_2020": population,
                    "housing_units_2020": housing,
                }
            )
        acs_rows, acs_path = fetch_acs(state_fips, api_key, cache / "acs")
        raw_checksums[f"{state_fips}/acs"] = sha256_file(acs_path)
        for row in acs_rows:
            all_acs.append(
                {
                    "place_id": row["state"] + row["place"],
                    "population_2024": integer_or_none(row["B01003_001E"]),
                    "housing_units_2024": integer_or_none(row["B25001_001E"]),
                    "median_home_value_2024": integer_or_none(row["B25077_001E"]),
                }
            )

    tract_housing, ct_audit = reconcile_connecticut(tract_housing, ct_tract_totals, fema_path)
    positive = {key: value for key, value in tract_housing.items() if value > 0}
    totals: dict[str, int] = defaultdict(int)
    for (place_id, _), housing in positive.items():
        totals[place_id] += housing
    weights = pl.DataFrame(
        [
            {
                "place_id": place_id,
                "tract_id": tract_id,
                "housing_units": housing,
                "housing_weight": housing / totals[place_id],
            }
            for (place_id, tract_id), housing in positive.items()
        ],
        schema={
            "place_id": pl.String,
            "tract_id": pl.String,
            "housing_units": pl.Int64,
            "housing_weight": pl.Float64,
        },
    ).sort(["place_id", "tract_id"])
    places = pl.DataFrame(all_places).sort("place_id")
    raw_acs = pl.DataFrame(
        all_acs,
        schema={
            "place_id": pl.String,
            "population_2024": pl.Int64,
            "housing_units_2024": pl.Int64,
            "median_home_value_2024": pl.Int64,
        },
    ).sort("place_id")
    acs, acs_audit = align_acs_context(places, raw_acs)
    if places.height != places["place_id"].n_unique():
        raise ValueError("Generated Place IDs are not unique")
    if weights.filter((pl.col("housing_weight") <= 0) | (pl.col("housing_units") <= 0)).height:
        raise ValueError("Generated weights contain nonpositive values")
    bad_sums = (
        weights.group_by("place_id")
        .agg(pl.col("housing_weight").sum())
        .filter((pl.col("housing_weight") - 1).abs() > 1e-9)
    )
    if bad_sums.height:
        raise ValueError(f"Generated weights fail normalization for {bad_sums.height} Places")
    output.mkdir(parents=True, exist_ok=True)
    assets = {
        "places_2020": places,
        "place_tract_weights_2020": weights,
        "acs_2024_context": acs,
    }
    checksums: dict[str, str] = {}
    for name, frame in assets.items():
        sort = ["place_id", "tract_id"] if "tract_id" in frame.columns else ["place_id"]
        checksums[name] = frame_checksum(frame, sort)
        frame.write_parquet(output / f"{name}.parquet", compression="zstd", statistics=True)
    metadata = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "50 states and District of Columbia",
        "census_decennial_vintage": 2020,
        "acs_vintage": 2024,
        "source_urls": sorted(source_urls),
        "raw_source_checksums": raw_checksums,
        "logical_checksums": checksums,
        "connecticut_reconciliation": ct_audit,
        "acs_reconciliation": acs_audit,
        "row_counts": {name: frame.height for name, frame in assets.items()},
    }
    (output / "reference_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("data/reference-source"))
    parser.add_argument("--output", type=Path, default=Path("src/househunter/assets"))
    parser.add_argument(
        "--fema-parquet", type=Path, default=Path("data/cache/fema_nri_tracts.parquet")
    )
    parser.add_argument("--census-api-key", default=os.environ.get("CENSUS_API_KEY"))
    arguments = parser.parse_args()
    generate(arguments.cache, arguments.output, arguments.census_api_key, arguments.fema_parquet)


if __name__ == "__main__":
    main()
