from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from .config import RuntimePaths, canonical_json, load_config, sha256_bytes, sha256_file
from .contracts import SourceStatus
from .errors import SourceContractError

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]


def _schema_fingerprint(fields: dict[str, str]) -> str:
    return sha256_bytes(canonical_json(fields))


def _request_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise SourceContractError("ArcGIS returned a non-object JSON response")
            if "error" in payload:
                raise SourceContractError(f"ArcGIS error: {payload['error']!r}")
            return payload
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * (2**attempt))
    raise SourceContractError(f"FEMA request failed after retries: {last_error}")


def _validate_layer(client: httpx.Client, source: dict[str, Any]) -> int:
    item = _request_json(
        client,
        f"https://www.arcgis.com/sharing/rest/content/items/{source['item_id']}",
        {"f": "json"},
    )
    if item.get("modified") != source["item_modified_ms"]:
        raise SourceContractError(
            "FEMA item changed: "
            f"expected modification {source['item_modified_ms']}, got {item.get('modified')}"
        )
    metadata = _request_json(client, source["layer_url"], {"f": "json"})
    actual = {field["name"]: field["type"] for field in metadata.get("fields", [])}
    required = source["fields"]
    if _schema_fingerprint(required) != source["schema_fingerprint"]:
        raise SourceContractError("Configured FEMA schema fingerprint is inconsistent")
    wrong = {name: actual.get(name) for name, kind in required.items() if actual.get(name) != kind}
    if wrong:
        raise SourceContractError(f"FEMA schema drift: expected {required}, got {wrong}")
    editing_info = metadata.get("editingInfo", {})
    edit_ms = editing_info.get("lastEditDate")
    if edit_ms != source["layer_last_edit_ms"]:
        raise SourceContractError(
            f"FEMA source changed: expected edit {source['layer_last_edit_ms']}, got {edit_ms}"
        )
    data_edit_ms = editing_info.get("dataLastEditDate")
    if data_edit_ms != source["data_last_edit_ms"]:
        raise SourceContractError(
            "FEMA source data changed: "
            f"expected edit {source['data_last_edit_ms']}, got {data_edit_ms}"
        )
    return min(int(metadata.get("maxRecordCount", 2000)), 2000)


def _validate_rows(rows: list[dict[str, Any]], source: dict[str, Any]) -> pl.DataFrame:
    if any(not isinstance(row, dict) for row in rows):
        raise SourceContractError("FEMA returned a malformed data row")
    normalized = [
        {
            "tract_id": str(row.get("TRACTFIPS", "")),
            "alr_npctl": row.get("ALR_NPCTL"),
            "nri_version": row.get("NRI_VER"),
        }
        for row in rows
    ]
    try:
        frame = pl.DataFrame(
            normalized,
            schema={"tract_id": pl.String, "alr_npctl": pl.Float64, "nri_version": pl.String},
        ).sort("tract_id")
    except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"FEMA rows do not match the expected types: {exc}") from exc
    if frame.height != source["expected_row_count"]:
        raise SourceContractError(
            f"FEMA row count changed: expected {source['expected_row_count']}, got {frame.height}"
        )
    if frame["tract_id"].n_unique() != frame.height:
        raise SourceContractError("FEMA TRACTFIPS values are not unique")
    invalid_ids = frame.filter(~pl.col("tract_id").str.contains(r"^\d{11}$"))
    if invalid_ids.height:
        raise SourceContractError(f"FEMA contains {invalid_ids.height} invalid tract identifiers")
    invalid_values = frame.filter(
        pl.col("alr_npctl").is_null()
        | ~pl.col("alr_npctl").is_finite()
        | (pl.col("alr_npctl") < 0)
        | (pl.col("alr_npctl") > 100)
    )
    if invalid_values.height:
        raise SourceContractError(f"FEMA contains {invalid_values.height} invalid ALR_NPCTL values")
    versions = frame["nri_version"].unique().to_list()
    if versions != [source["version"]]:
        raise SourceContractError(f"Expected NRI_VER {source['version']!r}, got {versions!r}")
    return frame


def _logical_rows(frame: pl.DataFrame) -> list[list[Any]]:
    return [
        [row["tract_id"], row["alr_npctl"], row["nri_version"]]
        for row in frame.iter_rows(named=True)
    ]


def validate_cached_fema(path: Path, source: dict[str, Any]) -> tuple[pl.DataFrame, str]:
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"Cannot read cached FEMA data: {exc}") from exc
    required = {"tract_id", "alr_npctl", "nri_version"}
    missing = required - set(frame.columns)
    if missing:
        raise SourceContractError(
            f"Cached FEMA data is missing columns: {', '.join(sorted(missing))}"
        )
    frame = _validate_rows(
        [
            {
                "TRACTFIPS": row["tract_id"],
                "ALR_NPCTL": row["alr_npctl"],
                "NRI_VER": row["nri_version"],
            }
            for row in frame.iter_rows(named=True)
        ],
        source,
    )
    logical_sha = sha256_bytes(canonical_json(_logical_rows(frame)))
    expected = source.get("canonical_sha256")
    if expected and logical_sha != expected:
        raise SourceContractError(f"FEMA checksum mismatch: expected {expected}, got {logical_sha}")
    return frame, logical_sha


def _write_source_manifest(
    paths: RuntimePaths,
    source: dict[str, Any],
    output: Path,
    logical_sha: str,
    rows: int,
    *,
    retrieved_at: datetime | None = None,
) -> None:
    record = pl.DataFrame(
        {
            "source": ["fema"],
            "version": [source["version"]],
            "release": [source["release"]],
            "url": [source["item_url"]],
            "retrieved_at": [retrieved_at or datetime.now(UTC)],
            "sha256": [logical_sha],
            "file_sha256": [sha256_file(output)],
            "row_count": [rows],
            "schema_fingerprint": [source["schema_fingerprint"]],
            "terms_url": [source["terms_url"]],
        }
    )
    temporary = paths.source_manifest.with_suffix(".parquet.tmp")
    record.write_parquet(temporary, compression="zstd")
    os.replace(temporary, paths.source_manifest)


def _validate_source_manifest(
    path: Path, source: dict[str, Any], output: Path, logical_sha: str, rows: int
) -> datetime:
    try:
        manifest = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"Cannot read FEMA source manifest: {exc}") from exc
    required = {
        "source",
        "version",
        "release",
        "url",
        "retrieved_at",
        "sha256",
        "file_sha256",
        "row_count",
        "schema_fingerprint",
        "terms_url",
    }
    missing = required - set(manifest.columns)
    if missing:
        raise SourceContractError(
            f"FEMA source manifest is missing columns: {', '.join(sorted(missing))}"
        )
    if manifest.height != 1:
        raise SourceContractError("FEMA source manifest must contain exactly one row")
    row = manifest.row(0, named=True)
    expected = {
        "source": "fema",
        "version": source["version"],
        "release": source["release"],
        "url": source["item_url"],
        "sha256": logical_sha,
        "file_sha256": sha256_file(output),
        "row_count": rows,
        "schema_fingerprint": source["schema_fingerprint"],
        "terms_url": source["terms_url"],
    }
    mismatches = {key: row[key] for key, value in expected.items() if row[key] != value}
    if mismatches:
        raise SourceContractError(f"FEMA source manifest does not match the cache: {mismatches}")
    retrieved_at = row["retrieved_at"]
    if not isinstance(retrieved_at, datetime):
        raise SourceContractError("FEMA source manifest has an invalid retrieval timestamp")
    return retrieved_at


def download_fema(
    paths: RuntimePaths,
    *,
    client: httpx.Client | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    force: bool = False,
) -> Path:
    paths.ensure()
    if cancelled and cancelled():
        raise InterruptedError("FEMA download cancelled")
    source = load_config()["fema"]
    output = paths.cache / "fema_nri_tracts.parquet"
    if output.is_file() and not force:
        try:
            frame, logical_sha = validate_cached_fema(output, source)
        except SourceContractError:
            if progress:
                progress(0, "Cached FEMA data failed verification; refreshing")
        else:
            try:
                _validate_source_manifest(
                    paths.source_manifest, source, output, logical_sha, frame.height
                )
            except SourceContractError:
                cache_timestamp = datetime.fromtimestamp(output.stat().st_mtime, UTC)
                _write_source_manifest(
                    paths,
                    source,
                    output,
                    logical_sha,
                    frame.height,
                    retrieved_at=cache_timestamp,
                )
            if progress:
                progress(100, "Using verified FEMA cache")
            return output

    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30, connect=15), follow_redirects=True)
    pages = paths.cache / f"fema-pages-{source['item_modified_ms']}"
    pages.mkdir(exist_ok=True)
    try:
        page_size = max(1, _validate_layer(http, source))
        rows: list[dict[str, Any]] = []
        offset = 0
        expected = int(source["expected_row_count"])
        while offset < expected:
            if cancelled and cancelled():
                raise InterruptedError("FEMA download cancelled")
            page_path = pages / f"{offset:06d}.json"
            if page_path.is_file():
                try:
                    payload = json.loads(page_path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise SourceContractError(f"Cached FEMA page is invalid: {page_path}") from exc
                if not isinstance(payload, dict):
                    raise SourceContractError(f"Cached FEMA page is invalid: {page_path}")
                page_rows = payload.get("features", [])
                if not isinstance(page_rows, list):
                    raise SourceContractError(f"Cached FEMA page is invalid: {page_path}")
            else:
                payload = _request_json(
                    http,
                    f"{source['layer_url']}/query",
                    {
                        "where": "1=1",
                        "outFields": "TRACTFIPS,ALR_NPCTL,NRI_VER",
                        "returnGeometry": "false",
                        "orderByFields": "TRACTFIPS",
                        "resultOffset": offset,
                        "resultRecordCount": page_size,
                        "f": "json",
                    },
                )
                features = payload.get("features", [])
                if not isinstance(features, list) or any(
                    not isinstance(feature, dict) or not isinstance(feature.get("attributes"), dict)
                    for feature in features
                ):
                    raise SourceContractError("FEMA returned a malformed feature page")
                attributes = [feature["attributes"] for feature in features]
                canonical_page = canonical_json({"features": attributes})
                temporary_page = page_path.with_suffix(".json.tmp")
                temporary_page.write_bytes(canonical_page)
                os.replace(temporary_page, page_path)
                page_rows = attributes
            if not page_rows:
                break
            rows.extend(page_rows)
            offset += len(page_rows)
            if progress:
                progress(
                    min(90, int(90 * len(rows) / expected)), f"Downloaded {len(rows):,} tracts"
                )

        frame = _validate_rows(rows, source)
        logical_sha = sha256_bytes(canonical_json(_logical_rows(frame)))
        configured_sha = source.get("canonical_sha256")
        if configured_sha and logical_sha != configured_sha:
            raise SourceContractError(
                f"FEMA checksum mismatch: expected {configured_sha}, got {logical_sha}"
            )
        if cancelled and cancelled():
            raise InterruptedError("FEMA download cancelled")
        temporary = output.with_suffix(".parquet.tmp")
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        os.replace(temporary, output)
        _write_source_manifest(paths, source, output, logical_sha, frame.height)
        if progress:
            progress(100, "FEMA download verified")
        return output
    finally:
        if owns_client:
            http.close()


def source_status(paths: RuntimePaths) -> SourceStatus:
    source = load_config()["fema"]
    cached = paths.cache / "fema_nri_tracts.parquet"
    if not cached.is_file():
        return SourceStatus(source="fema", version=source["version"], cached=False)
    try:
        frame, digest = validate_cached_fema(cached, source)
        retrieved = None
        if paths.source_manifest.is_file():
            retrieved = _validate_source_manifest(
                paths.source_manifest, source, cached, digest, frame.height
            )
        return SourceStatus(
            source="fema",
            version=source["version"],
            cached=True,
            sha256=digest,
            row_count=frame.height,
            retrieved_at=retrieved,
        )
    except SourceContractError as exc:
        return SourceStatus(source="fema", version=source["version"], cached=True, error=str(exc))
