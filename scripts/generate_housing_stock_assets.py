#!/usr/bin/env python3
"""Generate the pinned, offline ACS 2024 housing-stock reference bundle."""

from __future__ import annotations

import argparse
import os
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from househunter.housing_stock import (
    BUNDLED_HOUSING_STOCK,
    default_source_lock_path,
    load_source_lock,
    verify_raw_table,
    write_housing_stock_bundle,
)


def download_pinned(url: str, destination: Path, contract: dict[str, Any]) -> Path:
    if destination.is_file():
        verify_raw_table(destination, contract)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    last_error: Exception | None = None
    try:
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "HouseHunter/2.0"})
                with (
                    urllib.request.urlopen(request, timeout=60) as response,
                    temporary.open("xb") as output,
                ):
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                verify_raw_table(temporary, contract)
                os.replace(temporary, destination)
                return destination
            except OSError as exc:
                last_error = exc
                temporary.unlink(missing_ok=True)
                if attempt < 3:
                    time.sleep(2**attempt)
        raise RuntimeError(f"Could not download pinned source {url}: {last_error}")
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-lock", type=Path, default=default_source_lock_path()
    )
    parser.add_argument(
        "--cache", type=Path, default=Path("data/reference-source/housing-stock")
    )
    parser.add_argument("--output", type=Path, default=BUNDLED_HOUSING_STOCK)
    parser.add_argument("--b25034", type=Path)
    parser.add_argument("--b25035", type=Path)
    arguments = parser.parse_args()
    lock = load_source_lock(arguments.source_lock)
    paths: dict[str, Path] = {}
    for key, explicit in (("B25034", arguments.b25034), ("B25035", arguments.b25035)):
        contract = lock["sources"][key]
        destination = arguments.cache / Path(contract["url"]).name
        paths[key] = explicit or download_pinned(contract["url"], destination, contract)
        verify_raw_table(paths[key], contract)
    bundle = write_housing_stock_bundle(
        arguments.output,
        paths["B25034"],
        paths["B25035"],
        source_lock_path=arguments.source_lock,
    )
    print(
        f"Generated housing-stock bundle: {bundle.tracts.height} tracts, "
        f"{bundle.counties.height} counties, {bundle.county_msa.height} county-CBSA rows"
    )


if __name__ == "__main__":
    main()
