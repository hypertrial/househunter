from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import polars as pl

from .config import RuntimePaths, canonical_json, load_config, sha256_bytes, sha256_file
from .contracts import SourceStatus
from .download import _request_json, _schema_fingerprint
from .errors import SourceContractError

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]

RAW_NAME = "community_conditions_2025.json"
METADATA_NAME = "metadata.json"
PROCESSED_NAME = "chrr_county.parquet"
CURRENT_NAME = "current.json"
GENERATIONS_NAME = "generations"
OWNERSHIP_MARKER = ".househunter-owned"


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in "0123456789abcdef" for character in value)


def _pointer_generation(directory: Path) -> str | None:
    pointer = directory / CURRENT_NAME
    if not pointer.exists() and not pointer.is_symlink():
        return None
    if pointer.is_symlink() or not pointer.is_file():
        raise SourceContractError("CHR&R cache pointer must be a regular file")
    try:
        generation = json.loads(pointer.read_text())["generation"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Cannot read CHR&R cache pointer: {exc}") from exc
    if not isinstance(generation, str) or not _is_hex(generation, 64):
        raise SourceContractError("CHR&R cache pointer contains an invalid generation")
    return generation


def _require_real_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SourceContractError(f"CHR&R managed cache path must be a real directory: {path}")


def _remove_owned_generation(path: Path, generations: Path) -> None:
    if path.parent != generations or path.is_symlink() or not path.is_dir():
        raise SourceContractError("Refusing to remove an unsafe CHR&R cache path")
    marker = path / OWNERSHIP_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise SourceContractError("Refusing to remove an unowned CHR&R cache generation")
    shutil.rmtree(path)


def raw_paths(paths: RuntimePaths) -> tuple[Path, Path]:
    directory = paths.raw / "chrr"
    if directory.exists() or directory.is_symlink():
        _require_real_directory(directory)
    generation = _pointer_generation(directory)
    if generation is not None:
        directory = directory / GENERATIONS_NAME / generation
        if not directory.exists() and not directory.is_symlink():
            raise SourceContractError("CHR&R cache pointer target is missing")
        _require_real_directory(directory)
        marker = directory / OWNERSHIP_MARKER
        if marker.is_symlink() or not marker.is_file():
            raise SourceContractError("CHR&R cache generation is not owned by HouseHunter")
        for name in (RAW_NAME, METADATA_NAME):
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise SourceContractError("CHR&R cache generation contains an invalid artifact")
    return directory / RAW_NAME, directory / METADATA_NAME


def _publish_cache_generation(
    paths: RuntimePaths,
    source: dict[str, Any],
    source_rows: list[dict[str, Any]],
    frame: pl.DataFrame,
    digest: str,
) -> Path:
    directory = paths.raw / "chrr"
    _require_real_directory(directory)
    generations = directory / GENERATIONS_NAME
    generations.mkdir(parents=True, exist_ok=True)
    _require_real_directory(generations)
    previous_generation = _pointer_generation(directory)
    temporary = generations / f".{uuid4().hex}.tmp"
    temporary.mkdir()
    (temporary / OWNERSHIP_MARKER).write_text("HouseHunter CHR&R cache generation\n")
    staged_raw = temporary / RAW_NAME
    staged_metadata = temporary / METADATA_NAME
    try:
        staged_raw.write_bytes(canonical_json({"rows": source_rows}) + b"\n")
        _write_metadata(staged_metadata, source, staged_raw, frame, digest, datetime.now(UTC))
        staged_frame, staged_digest = validate_cached_chrr(staged_raw, source)
        _validate_metadata(staged_metadata, source, staged_raw, staged_frame, staged_digest)
        generation = sha256_bytes(staged_raw.read_bytes() + staged_metadata.read_bytes())
        published = generations / generation
        if published.exists():
            _require_real_directory(published)
            marker = published / OWNERSHIP_MARKER
            if marker.is_symlink() or not marker.is_file():
                raise SourceContractError("Existing CHR&R cache generation is not owned")
            for name in (RAW_NAME, METADATA_NAME):
                path = published / name
                if path.is_symlink() or not path.is_file():
                    raise SourceContractError("Existing CHR&R cache generation is invalid")
            existing_bytes = (published / RAW_NAME).read_bytes() + (
                published / METADATA_NAME
            ).read_bytes()
            if sha256_bytes(existing_bytes) != generation:
                raise SourceContractError("Existing CHR&R cache generation is corrupt")
            _remove_owned_generation(temporary, generations)
        else:
            os.replace(temporary, published)
        for child in generations.iterdir():
            if child.name in {generation, previous_generation}:
                continue
            if _is_hex(child.name, 64) or (
                child.name.startswith(".")
                and child.name.endswith(".tmp")
                and _is_hex(child.name[1:-4], 32)
            ):
                _remove_owned_generation(child, generations)
        pointer_temporary = directory / f".{CURRENT_NAME}.{uuid4().hex}.tmp"
        try:
            pointer_temporary.write_bytes(canonical_json({"generation": generation}) + b"\n")
            os.replace(pointer_temporary, directory / CURRENT_NAME)
        finally:
            pointer_temporary.unlink(missing_ok=True)
        return published / RAW_NAME
    finally:
        if temporary.exists():
            _remove_owned_generation(temporary, generations)


def _logical_rows(frame: pl.DataFrame) -> list[list[Any]]:
    return [
        [
            row["county_fips"],
            row["state"],
            row["county"],
            row["community_conditions_group"],
        ]
        for row in frame.iter_rows(named=True)
    ]


def validate_rows(rows: list[dict[str, Any]], source: dict[str, Any]) -> pl.DataFrame:
    if any(not isinstance(row, dict) for row in rows):
        raise SourceContractError("CHR&R returned a malformed data row")
    for row in rows:
        group = row.get("CommunityConditions_Group")
        if group is not None and (not isinstance(group, int) or isinstance(group, bool)):
            raise SourceContractError("CHR&R Community Conditions groups must be integers or null")
    normalized = [
        {
            "county_fips": "" if row.get("fipscode") is None else str(row["fipscode"]),
            "state": "" if row.get("state") is None else str(row["state"]).strip().upper(),
            "county": "" if row.get("county") is None else str(row["county"]).strip(),
            "community_conditions_group": row.get("CommunityConditions_Group"),
        }
        for row in rows
    ]
    try:
        frame = pl.DataFrame(
            normalized,
            schema={
                "county_fips": pl.String,
                "state": pl.String,
                "county": pl.String,
                "community_conditions_group": pl.Int8,
            },
        ).sort("county_fips")
    except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"CHR&R rows do not match the expected types: {exc}") from exc
    if frame.height != source["expected_row_count"]:
        raise SourceContractError(
            f"CHR&R row count changed: expected {source['expected_row_count']}, got {frame.height}"
        )
    if frame["county_fips"].n_unique() != frame.height:
        raise SourceContractError("CHR&R county FIPS values are not unique")
    if frame.filter(~pl.col("county_fips").str.contains(r"^\d{5}$")).height:
        raise SourceContractError("CHR&R contains invalid county FIPS values")
    if frame.filter(~pl.col("state").str.contains(r"^[A-Z]{2}$")).height:
        raise SourceContractError("CHR&R contains invalid state abbreviations")
    if frame.filter(pl.col("county") == "").height:
        raise SourceContractError("CHR&R contains blank county names")
    group = pl.col("community_conditions_group")
    if frame.filter(group.is_not_null() & ((group < 1) | (group > 10))).height:
        raise SourceContractError("CHR&R contains Community Conditions groups outside 1..10")
    return frame


def logical_checksum(frame: pl.DataFrame) -> str:
    return sha256_bytes(canonical_json(_logical_rows(frame)))


def _validate_layer(client: httpx.Client, source: dict[str, Any]) -> int:
    metadata = _request_json(client, source["layer_url"], {"f": "json"})
    actual = {field["name"]: field["type"] for field in metadata.get("fields", [])}
    required = source["fields"]
    if _schema_fingerprint(required) != source["schema_fingerprint"]:
        raise SourceContractError("Configured CHR&R schema fingerprint is inconsistent")
    wrong = {name: actual.get(name) for name, kind in required.items() if actual.get(name) != kind}
    if wrong:
        raise SourceContractError(f"CHR&R schema drift: expected {required}, got {wrong}")
    edits = metadata.get("editingInfo", {})
    expected_edits = {
        "lastEditDate": source["layer_last_edit_ms"],
        "schemaLastEditDate": source["schema_last_edit_ms"],
        "dataLastEditDate": source["data_last_edit_ms"],
    }
    mismatches = {
        key: edits.get(key) for key, value in expected_edits.items() if edits.get(key) != value
    }
    if mismatches:
        raise SourceContractError(
            f"CHR&R source changed: expected {expected_edits}, got {mismatches}"
        )
    return min(int(metadata.get("maxRecordCount", 2000)), 2000)


def validate_cached_chrr(path: Path, source: dict[str, Any]) -> tuple[pl.DataFrame, str]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Cannot read cached CHR&R data: {exc}") from exc
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceContractError("Cached CHR&R data is missing rows")
    frame = validate_rows(rows, source)
    digest = logical_checksum(frame)
    expected = source.get("canonical_sha256")
    if expected and digest != expected:
        raise SourceContractError(f"CHR&R checksum mismatch: expected {expected}, got {digest}")
    return frame, digest


def _metadata(
    source: dict[str, Any], raw: Path, frame: pl.DataFrame, digest: str, downloaded_at: datetime
) -> dict[str, Any]:
    return {
        "source": "County Health Rankings & Roadmaps",
        "release_year": source["release_year"],
        "source_version": source["version"],
        "downloaded_at": downloaded_at.isoformat(),
        "sha256": sha256_file(raw),
        "logical_sha256": digest,
        "row_count": frame.height,
        "geography": "county",
        "metric": "Community Conditions Health Group",
        "source_field": "CommunityConditions_Group",
        "url": source["layer_url"],
        "layer_last_edit_ms": source["layer_last_edit_ms"],
        "schema_last_edit_ms": source["schema_last_edit_ms"],
        "data_last_edit_ms": source["data_last_edit_ms"],
        "schema_fingerprint": source["schema_fingerprint"],
    }


def _write_metadata(
    metadata_path: Path,
    source: dict[str, Any],
    raw: Path,
    frame: pl.DataFrame,
    digest: str,
    downloaded_at: datetime,
) -> None:
    temporary = metadata_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(_metadata(source, raw, frame, digest, downloaded_at), indent=2, sort_keys=True)
        + "\n"
    )
    os.replace(temporary, metadata_path)


def _validate_metadata(
    metadata_path: Path, source: dict[str, Any], raw: Path, frame: pl.DataFrame, digest: str
) -> datetime:
    try:
        metadata = json.loads(metadata_path.read_text())
        downloaded_at = datetime.fromisoformat(metadata["downloaded_at"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"Cannot read CHR&R metadata: {exc}") from exc
    expected = _metadata(source, raw, frame, digest, downloaded_at)
    mismatches = {
        key: metadata.get(key) for key, value in expected.items() if metadata.get(key) != value
    }
    if mismatches:
        raise SourceContractError(f"CHR&R metadata does not match the cache: {mismatches}")
    return downloaded_at


def download_chrr(
    paths: RuntimePaths,
    *,
    client: httpx.Client | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    force: bool = False,
) -> Path:
    paths.ensure()
    raw, metadata_path = raw_paths(paths)
    raw.parent.mkdir(parents=True, exist_ok=True)
    source = load_config()["chrr"]
    managed_cache = (paths.raw / "chrr" / CURRENT_NAME).is_file()
    if cancelled and cancelled():
        raise InterruptedError("CHR&R download cancelled")
    if raw.is_file() and not force:
        try:
            frame, digest = validate_cached_chrr(raw, source)
        except SourceContractError:
            if progress:
                progress(0, "Cached CHR&R data failed verification; refreshing")
        else:
            try:
                _validate_metadata(metadata_path, source, raw, frame, digest)
            except SourceContractError:
                if not managed_cache:
                    downloaded_at = datetime.fromtimestamp(raw.stat().st_mtime, UTC)
                    _write_metadata(metadata_path, source, raw, frame, digest, downloaded_at)
                    if progress:
                        progress(100, "Using verified CHR&R cache")
                    return raw
                if progress:
                    progress(0, "Cached CHR&R metadata failed verification; refreshing")
            else:
                if progress:
                    progress(100, "Using verified CHR&R cache")
                return raw

    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30, connect=15), follow_redirects=True)
    try:
        page_size = max(1, _validate_layer(http, source))
        rows: list[dict[str, Any]] = []
        offset = 0
        expected = int(source["expected_row_count"])
        while offset < expected:
            if cancelled and cancelled():
                raise InterruptedError("CHR&R download cancelled")
            payload = _request_json(
                http,
                f"{source['layer_url']}/query",
                {
                    "where": "1=1",
                    "outFields": ",".join(source["fields"]),
                    "returnGeometry": "false",
                    "orderByFields": "fipscode",
                    "resultOffset": offset,
                    "resultRecordCount": page_size,
                    "f": "json",
                },
            )
            features = payload.get("features")
            if not isinstance(features, list) or any(
                not isinstance(feature, dict) or not isinstance(feature.get("attributes"), dict)
                for feature in features
            ):
                raise SourceContractError("CHR&R returned a malformed feature page")
            page_rows = [feature["attributes"] for feature in features]
            if not page_rows:
                break
            rows.extend(page_rows)
            offset += len(page_rows)
            if progress:
                progress(
                    min(90, int(90 * len(rows) / expected)),
                    f"Downloaded {len(rows):,} counties",
                )
        frame = validate_rows(rows, source)
        digest = logical_checksum(frame)
        configured = source.get("canonical_sha256")
        if configured and digest != configured:
            raise SourceContractError(
                f"CHR&R checksum mismatch: expected {configured}, got {digest}"
            )
        _validate_layer(http, source)
        if cancelled and cancelled():
            raise InterruptedError("CHR&R download cancelled")
        source_rows = [
            {
                "fipscode": row["county_fips"],
                "state": row["state"],
                "county": row["county"],
                "CommunityConditions_Group": row["community_conditions_group"],
            }
            for row in frame.iter_rows(named=True)
        ]
        raw = _publish_cache_generation(paths, source, source_rows, frame, digest)
        if progress:
            try:
                progress(100, "CHR&R download verified")
            except InterruptedError:
                if not cancelled or not cancelled():
                    raise
        return raw
    finally:
        if owns_client:
            http.close()


def build_processed(paths: RuntimePaths) -> tuple[pl.DataFrame, str]:
    raw, metadata_path = raw_paths(paths)
    if not raw.is_file():
        raise SourceContractError(
            "CHR&R data is not cached; run `househunter download --source chrr`"
        )
    source = load_config()["chrr"]
    frame, digest = validate_cached_chrr(raw, source)
    try:
        _validate_metadata(metadata_path, source, raw, frame, digest)
    except SourceContractError:
        if (paths.raw / "chrr" / CURRENT_NAME).is_file():
            raise
        downloaded_at = datetime.fromtimestamp(raw.stat().st_mtime, UTC)
        _write_metadata(metadata_path, source, raw, frame, digest, downloaded_at)
    processed = frame.with_columns(
        pl.lit(source["release_year"], dtype=pl.Int16).alias("release_year"),
        pl.lit(source["version"]).alias("source_version"),
    ).select(
        "county_fips",
        "state",
        "county",
        "release_year",
        "community_conditions_group",
        "source_version",
    )
    output = paths.processed / PROCESSED_NAME
    temporary = output.with_suffix(".parquet.tmp")
    processed.write_parquet(temporary, compression="zstd", statistics=True)
    os.replace(temporary, output)
    return processed, digest


def source_status(paths: RuntimePaths) -> SourceStatus:
    source = load_config()["chrr"]
    try:
        raw, metadata_path = raw_paths(paths)
        if not raw.is_file():
            return SourceStatus(source="chrr", version=source["version"], cached=False)
        frame, digest = validate_cached_chrr(raw, source)
        retrieved_at = _validate_metadata(metadata_path, source, raw, frame, digest)
        return SourceStatus(
            source="chrr",
            version=source["version"],
            cached=True,
            sha256=digest,
            row_count=frame.height,
            retrieved_at=retrieved_at,
        )
    except SourceContractError as exc:
        return SourceStatus(
            source="chrr",
            version=source["version"],
            cached=(paths.raw / "chrr").exists(),
            error=str(exc),
        )
