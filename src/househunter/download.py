from __future__ import annotations

import json
import os
import shutil
import tempfile
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
from .hazards import (
    FEMA_HAZARD_FIELDS,
    HAZARD_COLUMNS,
    county_out_fields,
    hazard_fields_from_cached_row,
    hazard_schema,
    hazard_values_from_row,
    invalid_optional_hazard_rows,
    logical_hazard_values,
    tract_out_fields,
)

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]


def _validate_raw_percentiles(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        for field in ("ALR_NPCTL", *FEMA_HAZARD_FIELDS):
            value = row.get(field)
            if value is not None and type(value) not in {int, float}:
                raise SourceContractError(f"FEMA {field} must be a JSON number or null")


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
    raise SourceContractError(f"Source request failed after retries: {last_error}")


def _validate_layer(
    client: httpx.Client,
    source: dict[str, Any],
    *,
    expected_geometry_type: str | None = None,
) -> int:
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
    if expected_geometry_type and metadata.get("geometryType") != expected_geometry_type:
        raise SourceContractError(
            "FEMA geometry type changed: "
            f"expected {expected_geometry_type}, got {metadata.get('geometryType')}"
        )
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


def _invalid_composite_percentile(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.filter(
        pl.col("alr_npctl").is_null()
        | ~pl.col("alr_npctl").is_finite()
        | (pl.col("alr_npctl") < 0)
        | (pl.col("alr_npctl") > 100)
    )


def _reject_invalid_hazards(frame: pl.DataFrame) -> None:
    invalid_hazards = invalid_optional_hazard_rows(frame)
    if invalid_hazards.height:
        raise SourceContractError(
            f"FEMA contains {invalid_hazards.height} invalid hazard ALR_NPCTL values"
        )


def _validate_rows(rows: list[dict[str, Any]], source: dict[str, Any]) -> pl.DataFrame:
    if any(not isinstance(row, dict) for row in rows):
        raise SourceContractError("FEMA returned a malformed data row")
    _validate_raw_percentiles(rows)
    normalized = [
        {
            "tract_id": str(row.get("TRACTFIPS", "")),
            "alr_npctl": row.get("ALR_NPCTL"),
            "nri_version": row.get("NRI_VER"),
            **hazard_values_from_row(row),
        }
        for row in rows
    ]
    try:
        frame = pl.DataFrame(
            normalized,
            schema={
                "tract_id": pl.String,
                "alr_npctl": pl.Float64,
                "nri_version": pl.String,
                **hazard_schema(),
            },
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
    invalid_values = _invalid_composite_percentile(frame)
    if invalid_values.height:
        raise SourceContractError(f"FEMA contains {invalid_values.height} invalid ALR_NPCTL values")
    _reject_invalid_hazards(frame)
    versions = frame["nri_version"].unique().to_list()
    if versions != [source["version"]]:
        raise SourceContractError(f"Expected NRI_VER {source['version']!r}, got {versions!r}")
    return frame


def _validate_county_rows(rows: list[dict[str, Any]], source: dict[str, Any]) -> pl.DataFrame:
    if any(not isinstance(row, dict) for row in rows):
        raise SourceContractError("FEMA returned a malformed data row")
    _validate_raw_percentiles(rows)
    normalized = [
        {
            "county_fips": str(row.get("STCOFIPS", "")),
            "county": "" if row.get("COUNTY") is None else str(row.get("COUNTY")),
            "county_type": "" if row.get("COUNTYTYPE") is None else str(row.get("COUNTYTYPE")),
            "state": "" if row.get("STATEABBRV") is None else str(row.get("STATEABBRV")),
            "alr_npctl": row.get("ALR_NPCTL"),
            "nri_version": row.get("NRI_VER"),
            **hazard_values_from_row(row),
        }
        for row in rows
    ]
    try:
        frame = pl.DataFrame(
            normalized,
            schema={
                "county_fips": pl.String,
                "county": pl.String,
                "county_type": pl.String,
                "state": pl.String,
                "alr_npctl": pl.Float64,
                "nri_version": pl.String,
                **hazard_schema(),
            },
        ).sort("county_fips")
    except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"FEMA rows do not match the expected types: {exc}") from exc
    if frame.height != source["expected_row_count"]:
        raise SourceContractError(
            f"FEMA row count changed: expected {source['expected_row_count']}, got {frame.height}"
        )
    if frame["county_fips"].n_unique() != frame.height:
        raise SourceContractError("FEMA STCOFIPS values are not unique")
    invalid_ids = frame.filter(~pl.col("county_fips").str.contains(r"^\d{5}$"))
    if invalid_ids.height:
        raise SourceContractError(f"FEMA contains {invalid_ids.height} invalid county identifiers")
    invalid_values = _invalid_composite_percentile(frame)
    if invalid_values.height:
        raise SourceContractError(f"FEMA contains {invalid_values.height} invalid ALR_NPCTL values")
    _reject_invalid_hazards(frame)
    versions = frame["nri_version"].unique().to_list()
    if versions != [source["version"]]:
        raise SourceContractError(f"Expected NRI_VER {source['version']!r}, got {versions!r}")
    return frame


def _logical_rows(frame: pl.DataFrame) -> list[list[Any]]:
    return [
        [row["tract_id"], row["alr_npctl"], row["nri_version"], *logical_hazard_values(row)]
        for row in frame.iter_rows(named=True)
    ]


def _logical_county_rows(frame: pl.DataFrame) -> list[list[Any]]:
    return [
        [
            row["county_fips"],
            row["county"],
            row["county_type"],
            row["state"],
            row["alr_npctl"],
            row["nri_version"],
            *logical_hazard_values(row),
        ]
        for row in frame.iter_rows(named=True)
    ]


def page_cache_dir(paths: RuntimePaths, pages_name: str, source: dict[str, Any]) -> Path:
    fingerprint = str(source["schema_fingerprint"])[:16]
    return paths.cache / f"{pages_name}-{source['item_modified_ms']}-{fingerprint}"


def _ensure_page_cache_directory(path: Path) -> Path:
    target = Path(os.path.abspath(path))
    if any(component.is_symlink() for component in (target, *target.parents)):
        raise SourceContractError("FEMA page cache path cannot contain a symlink")
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise SourceContractError("FEMA page cache path is not a safe directory")
    return target


def validate_cached_fema(path: Path, source: dict[str, Any]) -> tuple[pl.DataFrame, str]:
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"Cannot read cached FEMA data: {exc}") from exc
    required = {"tract_id", "alr_npctl", "nri_version", *HAZARD_COLUMNS}
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
                **hazard_fields_from_cached_row(row),
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


def validate_cached_fema_counties(path: Path, source: dict[str, Any]) -> tuple[pl.DataFrame, str]:
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise SourceContractError(f"Cannot read cached FEMA data: {exc}") from exc
    required = {
        "county_fips",
        "county",
        "county_type",
        "state",
        "alr_npctl",
        "nri_version",
        *HAZARD_COLUMNS,
    }
    missing = required - set(frame.columns)
    if missing:
        raise SourceContractError(
            f"Cached FEMA data is missing columns: {', '.join(sorted(missing))}"
        )
    frame = _validate_county_rows(
        [
            {
                "STCOFIPS": row["county_fips"],
                "COUNTY": row["county"],
                "COUNTYTYPE": row["county_type"],
                "STATEABBRV": row["state"],
                "ALR_NPCTL": row["alr_npctl"],
                "NRI_VER": row["nri_version"],
                **hazard_fields_from_cached_row(row),
            }
            for row in frame.iter_rows(named=True)
        ],
        source,
    )
    logical_sha = sha256_bytes(canonical_json(_logical_county_rows(frame)))
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
    source_key: str = "fema",
    retrieved_at: datetime | None = None,
) -> None:
    record = pl.DataFrame(
        {
            "source": [source_key],
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
    if paths.source_manifest.is_file():
        try:
            existing = pl.read_parquet(paths.source_manifest)
        except (OSError, pl.exceptions.PolarsError):
            existing = record.clear()
        else:
            if "source" not in existing.columns:
                existing = existing.with_columns(pl.lit("fema").alias("source"))
            existing = existing.filter(pl.col("source") != source_key)
            record = pl.concat([existing, record], how="diagonal")
    temporary = paths.source_manifest.with_suffix(".parquet.tmp")
    record.write_parquet(temporary, compression="zstd")
    os.replace(temporary, paths.source_manifest)


def _validate_source_manifest(
    path: Path,
    source: dict[str, Any],
    output: Path,
    logical_sha: str,
    rows: int,
    *,
    source_key: str = "fema",
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
    if "source" not in manifest.columns:
        raise SourceContractError("FEMA source manifest is missing columns: source")
    matched = manifest.filter(pl.col("source") == source_key)
    if matched.height != 1:
        raise SourceContractError(f"FEMA source manifest must contain one {source_key} row")
    row = matched.row(0, named=True)
    expected = {
        "source": source_key,
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


def _fetch_layer_rows(
    http: httpx.Client,
    source: dict[str, Any],
    *,
    out_fields: str,
    order_by: str,
    pages: Path,
    progress: Progress | None,
    cancelled: Cancelled | None,
    noun: str,
    resume: bool = True,
) -> list[dict[str, Any]]:
    pages = _ensure_page_cache_directory(pages)
    required_fields = set(source["fields"])
    page_size = max(1, _validate_layer(http, source))
    rows: list[dict[str, Any]] = []
    offset = 0
    expected = int(source["expected_row_count"])
    while offset < expected:
        if cancelled and cancelled():
            raise InterruptedError("FEMA download cancelled")
        page_path = pages / f"{offset:06d}.json"
        page_rows: list[dict[str, Any]] | None = None
        if resume and page_path.is_symlink():
            page_path.unlink()
        if resume and page_path.is_file():
            try:
                payload = json.loads(page_path.read_text())
                cached_rows = payload.get("features") if isinstance(payload, dict) else None
                if not isinstance(cached_rows, list) or any(
                    not isinstance(row, dict) or not required_fields.issubset(row)
                    for row in cached_rows
                ):
                    raise ValueError("malformed cached feature page")
                page_source = {**source, "expected_row_count": len(cached_rows)}
                if order_by == "TRACTFIPS":
                    _validate_rows(cached_rows, page_source)
                elif order_by == "STCOFIPS":
                    _validate_county_rows(cached_rows, page_source)
                else:
                    raise ValueError(f"unsupported FEMA cache ordering: {order_by}")
                page_rows = cached_rows
            except (OSError, json.JSONDecodeError, SourceContractError, ValueError):
                page_path.unlink(missing_ok=True)
        if page_rows is None:
            payload = _request_json(
                http,
                f"{source['layer_url']}/query",
                {
                    "where": "1=1",
                    "outFields": out_fields,
                    "returnGeometry": "false",
                    "orderByFields": order_by,
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
            descriptor, temporary_name = tempfile.mkstemp(
                dir=pages, prefix=f".{page_path.name}.", suffix=".tmp"
            )
            temporary_page = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(canonical_page)
                os.replace(temporary_page, page_path)
            except BaseException:
                temporary_page.unlink(missing_ok=True)
                raise
            page_rows = attributes
        if not page_rows:
            break
        rows.extend(page_rows)
        offset += len(page_rows)
        if progress:
            progress(min(90, int(90 * len(rows) / expected)), f"Downloaded {len(rows):,} {noun}")
    return rows


def _download_source(
    paths: RuntimePaths,
    *,
    source_key: str,
    output: Path,
    pages_name: str,
    out_fields: str,
    order_by: str,
    noun: str,
    validate_cache: Callable[[Path, dict[str, Any]], tuple[pl.DataFrame, str]],
    validate_rows: Callable[[list[dict[str, Any]], dict[str, Any]], pl.DataFrame],
    logical_sha_for: Callable[[pl.DataFrame], str],
    client: httpx.Client | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    force: bool = False,
) -> Path:
    paths.ensure()
    if cancelled and cancelled():
        raise InterruptedError("FEMA download cancelled")
    source = load_config()[source_key]
    if output.is_file() and not force:
        try:
            frame, logical_sha = validate_cache(output, source)
        except SourceContractError:
            if progress:
                progress(0, "Cached FEMA data failed verification; refreshing")
        else:
            try:
                _validate_source_manifest(
                    paths.source_manifest,
                    source,
                    output,
                    logical_sha,
                    frame.height,
                    source_key=source_key,
                )
            except SourceContractError:
                cache_timestamp = datetime.fromtimestamp(output.stat().st_mtime, UTC)
                _write_source_manifest(
                    paths,
                    source,
                    output,
                    logical_sha,
                    frame.height,
                    source_key=source_key,
                    retrieved_at=cache_timestamp,
                )
            if progress:
                progress(100, "Using verified FEMA cache")
            return output

    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(30, connect=15), follow_redirects=True)
    pages = page_cache_dir(paths, pages_name, source)
    pages.mkdir(exist_ok=True)
    try:
        rows = _fetch_layer_rows(
            http,
            source,
            out_fields=out_fields,
            order_by=order_by,
            pages=pages,
            progress=progress,
            cancelled=cancelled,
            noun=noun,
            resume=not force,
        )
        frame = validate_rows(rows, source)
        logical_sha = logical_sha_for(frame)
        configured_sha = source.get("canonical_sha256")
        if configured_sha and logical_sha != configured_sha:
            raise SourceContractError(
                f"FEMA checksum mismatch: expected {configured_sha}, got {logical_sha}"
            )
        try:
            _validate_layer(http, source)
        except SourceContractError:
            shutil.rmtree(pages, ignore_errors=True)
            raise
        if cancelled and cancelled():
            raise InterruptedError("FEMA download cancelled")
        temporary = output.with_suffix(".parquet.tmp")
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        os.replace(temporary, output)
        _write_source_manifest(
            paths, source, output, logical_sha, frame.height, source_key=source_key
        )
        if progress:
            progress(100, "FEMA download verified")
        return output
    finally:
        if owns_client:
            http.close()


def download_fema(
    paths: RuntimePaths,
    *,
    client: httpx.Client | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    force: bool = False,
) -> Path:
    return _download_source(
        paths,
        source_key="fema",
        output=paths.cache / "fema_nri_tracts.parquet",
        pages_name="fema-pages",
        out_fields=tract_out_fields(),
        order_by="TRACTFIPS",
        noun="tracts",
        validate_cache=validate_cached_fema,
        validate_rows=_validate_rows,
        logical_sha_for=lambda frame: sha256_bytes(canonical_json(_logical_rows(frame))),
        client=client,
        progress=progress,
        cancelled=cancelled,
        force=force,
    )


def download_fema_counties(
    paths: RuntimePaths,
    *,
    client: httpx.Client | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    force: bool = False,
) -> Path:
    return _download_source(
        paths,
        source_key="fema_counties",
        output=paths.cache / "fema_nri_counties.parquet",
        pages_name="fema-county-pages",
        out_fields=county_out_fields(),
        order_by="STCOFIPS",
        noun="counties",
        validate_cache=validate_cached_fema_counties,
        validate_rows=_validate_county_rows,
        logical_sha_for=lambda frame: sha256_bytes(canonical_json(_logical_county_rows(frame))),
        client=client,
        progress=progress,
        cancelled=cancelled,
        force=force,
    )


def _source_status(
    paths: RuntimePaths,
    *,
    source_key: str,
    cache_name: str,
    validate_cache: Callable[[Path, dict[str, Any]], tuple[pl.DataFrame, str]],
) -> SourceStatus:
    source = load_config()[source_key]
    cached = paths.cache / cache_name
    if not cached.is_file():
        return SourceStatus(source=source_key, version=source["version"], cached=False)
    try:
        frame, digest = validate_cache(cached, source)
        retrieved = None
        if paths.source_manifest.is_file():
            retrieved = _validate_source_manifest(
                paths.source_manifest,
                source,
                cached,
                digest,
                frame.height,
                source_key=source_key,
            )
        return SourceStatus(
            source=source_key,
            version=source["version"],
            cached=True,
            sha256=digest,
            row_count=frame.height,
            retrieved_at=retrieved,
        )
    except SourceContractError as exc:
        return SourceStatus(
            source=source_key, version=source["version"], cached=True, error=str(exc)
        )


def source_statuses(paths: RuntimePaths) -> list[SourceStatus]:
    from .chrr import source_status as chrr_source_status

    return [
        _source_status(
            paths,
            source_key="fema",
            cache_name="fema_nri_tracts.parquet",
            validate_cache=validate_cached_fema,
        ),
        _source_status(
            paths,
            source_key="fema_counties",
            cache_name="fema_nri_counties.parquet",
            validate_cache=validate_cached_fema_counties,
        ),
        chrr_source_status(paths),
    ]
