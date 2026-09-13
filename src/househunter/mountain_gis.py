from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import stat
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import numpy as np
import polars as pl
import pyogrio
import rasterio
import shapely
from pyogrio import raw as ogr_raw
from pyproj import CRS, Transformer
from rasterio.enums import MergeAlg, Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds
from shapely.strtree import STRtree

from .config import canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .mountain import IN_SCOPE_STATES, RAW_PRECISION, access_metrics, terrain_metrics

CELL_SIZE_M = 250.0
TILE_SIZE_M = 100_000.0
HALO_M = 100_000.0
SOURCE_LOCK_SCHEMA_VERSION = 2
SOURCE_DOWNLOAD_MAX_BYTES = 50_000_000_000
ARCGIS_ASSEMBLY_PAGE_GROUP = 1_024
HAWAII_WKT2_2019 = CRS.from_user_input("ESRI:102007").to_wkt(version="WKT2_2019", pretty=False)
PAD_ACCESS_CODES = {
    "OA": 1,
    "Open": 1,
    "RA": 2,
    "Restricted": 2,
    "XA": 3,
    "Closed": 3,
    "UK": 4,
    "Unknown": 4,
}

_STORAGE_PRIMITIVE_KEYS = (
    "compressed_download_bytes",
    "maximum_part_bytes",
    "extracted_source_bytes",
    "source_staging_bytes",
    "preparation_workspace_bytes",
    "preparation_overlap_bytes",
    "maximum_atomic_write_bytes",
    "prepared_pack_bytes",
    "comparison_shard_bytes",
    "active_and_rollback_release_bytes",
    "candidate_release_bytes",
    "work_shard_bytes",
    "compact_bundle_bytes",
    "report_bytes",
    "retained_baseline_bytes",
    "safety_reserve_bytes",
)
_SECRET_KEY = re.compile(r"(?:api[_-]?key|auth|credential|password|secret|signature|token)", re.I)
_PUBLIC_SOURCE_FIELDS = (
    "name",
    "family",
    "filename",
    "acquired_at",
    "release",
    "license",
    "crs",
    "schema",
    "count",
    "layer",
    "geometry_type",
    "size",
    "sha256",
)
REPRESENTATIVE_TILE_CASES = {
    "alaska",
    "coastal_nodata",
    "colorado_rockies",
    "flat_plains",
    "hawaii",
    "overlap_seam",
}


def _source_staging_allocation(source: dict[str, Any]) -> int:
    acquisition = source.get("acquisition")
    if isinstance(acquisition, dict) and acquisition.get("type") == "arcgis_query_snapshot_v1":
        return (
            int(acquisition["page_shard_bytes"])
            + int(source.get("size", 0))
            + int(acquisition["maximum_response_bytes"])
        )
    return int(source.get("size", 0)) + int(
        source.get("archive", {}).get("total_uncompressed_size", 0)
    )


def _source_maximum_part(source: dict[str, Any]) -> int:
    acquisition = source.get("acquisition")
    if isinstance(acquisition, dict) and acquisition.get("type") == "arcgis_query_snapshot_v1":
        return int(acquisition["maximum_response_bytes"])
    return int(source.get("size", 0))


def storage_projection(
    sources: list[dict[str, Any]],
    *,
    preparation_batches: list[dict[str, Any]] | None = None,
    prepared_pack_bytes: int,
    preparation_workspace_bytes: int | None = None,
    preparation_overlap_bytes: int | None = None,
    active_and_rollback_release_bytes: int,
    candidate_release_bytes: int,
    work_shard_bytes: int,
    comparison_shard_bytes: int,
    compact_bundle_bytes: int = 50 * 1024**2,
    report_bytes: int = 10 * 1024**2,
    retained_baseline_bytes: int = 0,
    safety_reserve_bytes: int = 7_000_000_000,
    maximum_atomic_write_bytes: int = 5_760_000,
) -> dict[str, int]:
    """Return the conservative, reviewable storage envelope for every workflow phase."""
    compressed = sum(
        int(source.get("acquisition", {}).get("download_bytes", source.get("size", 0)))
        if isinstance(source.get("acquisition"), dict)
        else int(source.get("size", 0))
        for source in sources
    )
    extracted = sum(
        int(source.get("archive", {}).get("total_uncompressed_size", 0)) for source in sources
    )
    largest_part = max((_source_maximum_part(source) for source in sources), default=0)
    family_staging: dict[str, int] = {}
    for source in sources:
        family = str(source.get("family", "unknown"))
        family_staging[family] = family_staging.get(family, 0) + _source_staging_allocation(source)
    batched_families = {str(batch.get("family")) for batch in preparation_batches or []}
    unbatched_staging = max(
        (size for family, size in family_staging.items() if family not in batched_families),
        default=0,
    )
    sources_by_name = {str(source.get("name")): source for source in sources}
    family_batches: dict[str, list[dict[str, Any]]] = {}
    for batch in preparation_batches or []:
        names = batch.get("sources", [])
        if not isinstance(names, list):
            raise HouseHunterError("Mountain preparation batch has an invalid source list")
        family_batches.setdefault(str(batch.get("family")), []).append(batch)
    batched_staging: list[int] = []
    for batches in family_batches.values():
        last_use = {
            str(name): max(
                index
                for index, batch in enumerate(batches)
                if name in batch.get("sources", [])
            )
            for batch in batches
            for name in batch.get("sources", [])
        }
        live: set[str] = set()
        family_peak = 0
        for index, batch in enumerate(batches):
            live.update(str(name) for name in batch["sources"])
            family_peak = max(
                family_peak,
                sum(_source_staging_allocation(sources_by_name[name]) for name in live),
            )
            live = {name for name in live if last_use[name] > index}
        batched_staging.append(family_peak)
    staging = max([unbatched_staging, *batched_staging], default=0)
    workspace = (
        prepared_pack_bytes if preparation_workspace_bytes is None else preparation_workspace_bytes
    )
    overlap = (
        staging + workspace + comparison_shard_bytes
        if preparation_overlap_bytes is None
        else preparation_overlap_bytes
    )
    if overlap < max(staging, workspace + comparison_shard_bytes):
        raise HouseHunterError("Mountain preparation overlap understates concurrent storage")
    primitives = {
        "compressed_download_bytes": compressed,
        "maximum_part_bytes": largest_part,
        "extracted_source_bytes": extracted,
        "source_staging_bytes": staging,
        "preparation_workspace_bytes": workspace,
        "preparation_overlap_bytes": overlap,
        "maximum_atomic_write_bytes": maximum_atomic_write_bytes,
        "prepared_pack_bytes": prepared_pack_bytes,
        "comparison_shard_bytes": comparison_shard_bytes,
        "active_and_rollback_release_bytes": active_and_rollback_release_bytes,
        "candidate_release_bytes": candidate_release_bytes,
        "work_shard_bytes": work_shard_bytes,
        "compact_bundle_bytes": compact_bundle_bytes,
        "report_bytes": report_bytes,
        "retained_baseline_bytes": retained_baseline_bytes,
        "safety_reserve_bytes": safety_reserve_bytes,
    }
    acquisition = retained_baseline_bytes + staging + largest_part + safety_reserve_bytes
    preparation = (
        retained_baseline_bytes
        + overlap
        + maximum_atomic_write_bytes
        + safety_reserve_bytes
    )
    timed = (
        prepared_pack_bytes
        + active_and_rollback_release_bytes
        + candidate_release_bytes
        + work_shard_bytes
        + compact_bundle_bytes
        + report_bytes
        + safety_reserve_bytes
    )
    return {
        **primitives,
        "acquisition_peak_bytes": acquisition,
        "preparation_peak_bytes": preparation,
        "timed_build_peak_bytes": timed,
        "managed_peak_bytes": max(acquisition, preparation, timed),
    }


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise HouseHunterError("Mountain source lock contains a secret-bearing field")
            _reject_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_fields(child)


def source_provenance_item(item: dict[str, Any]) -> dict[str, object]:
    """Return only reviewed, distributable source fields for release provenance."""
    result = {key: item[key] for key in _PUBLIC_SOURCE_FIELDS if key in item}
    archive = item.get("archive")
    if isinstance(archive, dict):
        members = archive.get("members", [])
        result["archive"] = {
            "format": archive.get("format"),
            "root": archive.get("root"),
            "datasets": archive.get("datasets"),
            "member_count": len(members),
            "total_uncompressed_size": archive.get("total_uncompressed_size"),
            "member_manifest_sha256": sha256_bytes(canonical_json(members)),
        }
    acquisition = item.get("acquisition")
    if isinstance(acquisition, dict):
        result["acquisition"] = {
            "type": acquisition.get("type"),
            "object_id_field": acquisition.get("object_id_field"),
            "objectid_sha256": acquisition.get("objectid_sha256"),
            "page_manifest_sha256": sha256_bytes(
                canonical_json(acquisition.get("page_sha256", []))
            ),
            "access_counts": acquisition.get("access_counts"),
        }
    return result


@dataclass(frozen=True)
class RegionSources:
    name: str
    target_crs: str
    blocks: Path | tuple[Path, ...]
    elevation: tuple[Path, ...]
    pad_us: Path
    trails: Path | tuple[Path, ...]
    blocks_layer: str | None = None
    pad_layer: str | None = None
    trails_layer: str | None = None
    pad_access_field: str = "Pub_Access"
    pad_order_field: str | None = None
    trail_where: str | None = None


def load_regions(path: Path, source_root: Path) -> tuple[RegionSources, ...]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain region configuration: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("regions"), list):
        raise HouseHunterError("Mountain region configuration is incompatible")
    regions: list[RegionSources] = []

    def source_path(value: object) -> Path:
        relative = Path(str(value))
        root = source_root.resolve()
        candidate = (root / relative).resolve()
        if relative.is_absolute() or not candidate.is_relative_to(root):
            raise HouseHunterError("Mountain region configuration contains an invalid source path")
        return candidate

    try:
        for item in payload["regions"]:
            block_values = item["blocks"]
            if isinstance(block_values, list) and not block_values:
                raise HouseHunterError("Mountain region contains no Census block sources")
            trail_values = item["trails"]
            if isinstance(trail_values, list) and not trail_values:
                raise HouseHunterError("Mountain region contains no trail sources")
            blocks = (
                tuple(source_path(value) for value in block_values)
                if isinstance(block_values, list)
                else source_path(block_values)
            )
            regions.append(
                RegionSources(
                    name=str(item["name"]),
                    target_crs=str(item["target_crs"]),
                    blocks=blocks,
                    elevation=tuple(source_path(value) for value in item["elevation"]),
                    pad_us=source_path(item["pad_us"]),
                    trails=(
                        tuple(source_path(value) for value in trail_values)
                        if isinstance(trail_values, list)
                        else source_path(trail_values)
                    ),
                    blocks_layer=item.get("blocks_layer"),
                    pad_layer=item.get("pad_layer"),
                    trails_layer=item.get("trails_layer"),
                    pad_access_field=str(item.get("pad_access_field", "Pub_Access")),
                    pad_order_field=item.get("pad_order_field"),
                    trail_where=item.get("trail_where"),
                )
            )
    except HouseHunterError:
        raise
    except (KeyError, TypeError) as exc:
        raise HouseHunterError("Mountain region configuration contains an invalid entry") from exc
    if not regions:
        raise HouseHunterError("Mountain region configuration contains no regions")
    return tuple(regions)


def _source_path(source: dict[str, Any], root: Path | None) -> Path:
    if root is not None:
        filename = Path(str(source.get("filename", "")))
        if filename.name != str(source.get("filename", "")):
            raise HouseHunterError("Mountain source lock contains an invalid filename")
        unresolved = root / filename
        if unresolved.is_symlink():
            raise HouseHunterError("Mountain source lock cannot resolve through a symlink")
        candidate = unresolved.resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise HouseHunterError("Mountain source lock contains an invalid filename")
        return candidate
    return Path(str(source.get("path", ""))).expanduser().resolve()


def _has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _safe_archive_relative(value: object) -> Path:
    text = str(value)
    pure = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or "\x00" in text
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise HouseHunterError("Mountain archive contains an unsafe path")
    return Path(*pure.parts)


def _archive_root(source: dict[str, Any], destination: Path) -> Path:
    archive = source.get("archive")
    if not isinstance(archive, dict):
        raise HouseHunterError("Mountain archive metadata is invalid")
    root_name = str(archive.get("root", ""))
    if Path(root_name).name != root_name or re.fullmatch(r"[A-Za-z0-9._-]+", root_name) is None:
        raise HouseHunterError("Mountain archive root is invalid")
    root = (destination / root_name).resolve()
    if not root.is_relative_to(destination.resolve()):
        raise HouseHunterError("Mountain archive root escapes the source directory")
    return root


def _archive_member_metadata(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    archive = source.get("archive")
    members = archive.get("members") if isinstance(archive, dict) else None
    if not isinstance(members, list) or not members:
        raise HouseHunterError("Mountain archive lacks a locked member inventory")
    result: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    for member in members:
        if not isinstance(member, dict):
            raise HouseHunterError("Mountain archive has invalid member metadata")
        path = _safe_archive_relative(member.get("path")).as_posix()
        if path.casefold() in folded or path in result:
            raise HouseHunterError("Mountain archive has duplicate or case-colliding members")
        try:
            size = int(member["size"])
            digest = str(member["sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError("Mountain archive has invalid member metadata") from exc
        if size < 0 or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise HouseHunterError("Mountain archive has invalid member metadata")
        if Path(path).suffix.lower() in {".7z", ".gz", ".rar", ".tar", ".tgz", ".zip"}:
            raise HouseHunterError("Mountain archive cannot contain nested archives")
        result[path] = {**member, "path": path, "size": size, "sha256": digest}
        folded.add(path.casefold())
    return result


def _verify_extracted_archive(source: dict[str, Any], destination: Path) -> Path:
    root = _archive_root(source, destination)
    if not root.is_dir() or root.is_symlink():
        raise HouseHunterError("Mountain archive extraction is missing or unsafe")
    members = _archive_member_metadata(source)
    expected_files = set(members)
    expected_directories = {
        parent.as_posix()
        for relative in expected_files
        for parent in PurePosixPath(relative).parents
        if parent.as_posix() != "."
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for current, directory_names, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            child = current_path / name
            if child.is_symlink():
                raise HouseHunterError("Mountain archive extraction contains a symlink")
            actual_directories.add(child.relative_to(root).as_posix())
        for name in filenames:
            child = current_path / name
            if child.is_symlink():
                raise HouseHunterError("Mountain archive extraction contains a symlink")
            actual_files.add(child.relative_to(root).as_posix())
    if actual_files != expected_files or actual_directories != expected_directories:
        raise HouseHunterError("Mountain archive extraction differs from its reviewed inventory")
    for relative, metadata in members.items():
        path = root / _safe_archive_relative(relative)
        if (
            not path.is_file()
            or _has_symlink_component(path, root)
            or path.stat().st_size != metadata["size"]
            or sha256_file(path) != metadata["sha256"]
        ):
            raise HouseHunterError(f"Mountain extracted archive member is corrupt: {relative}")
    archive = source["archive"]
    for value in archive["datasets"]:
        dataset = root / _safe_archive_relative(value)
        if not dataset.exists() or _has_symlink_component(dataset, root):
            raise HouseHunterError("Mountain archive dataset root is missing or unsafe")
    return root


def extract_locked_archive(source: dict[str, Any], archive_path: Path, destination: Path) -> Path:
    """Extract exactly reviewed ZIP members with bounded expansion and no link handling."""
    final = _archive_root(source, destination)
    if final.exists():
        return _verify_extracted_archive(source, destination)
    members = _archive_member_metadata(source)
    archive = source["archive"]
    expected_total = int(archive["total_uncompressed_size"])
    if expected_total != sum(int(item["size"]) for item in members.values()):
        raise HouseHunterError("Mountain archive expansion total differs from its lock")
    if expected_total > 30_000_000_000 or len(members) > 200_000:
        raise HouseHunterError("Mountain archive exceeds extraction limits")
    temporary = destination / f".{final.name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        with zipfile.ZipFile(archive_path) as handle:
            infos = handle.infolist()
            if len(infos) > 200_000:
                raise HouseHunterError("Mountain archive entry count exceeds its limit")
            files: dict[str, zipfile.ZipInfo] = {}
            folded: set[str] = set()
            for info in infos:
                relative = _safe_archive_relative(info.filename.rstrip("/"))
                normalized = relative.as_posix()
                if normalized.casefold() in folded:
                    raise HouseHunterError(
                        "Mountain archive has duplicate or case-colliding entries"
                    )
                folded.add(normalized.casefold())
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 0x1:
                    raise HouseHunterError(
                        "Mountain archive contains a link, device, or encryption"
                    )
                if not info.is_dir():
                    files[normalized] = info
            if set(files) != set(members):
                raise HouseHunterError("Mountain archive entries differ from its reviewed lock")
            for relative, metadata in members.items():
                info = files[relative]
                if (
                    info.file_size != metadata["size"]
                    or info.file_size / max(1, info.compress_size) > 100
                ):
                    raise HouseHunterError("Mountain archive member exceeds expansion limits")
                output = temporary / _safe_archive_relative(relative)
                output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                received = 0
                with handle.open(info) as source_handle, output.open("wb") as output_handle:
                    for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                        received += len(chunk)
                        if received > metadata["size"]:
                            raise HouseHunterError("Mountain archive member is oversized")
                        digest.update(chunk)
                        output_handle.write(chunk)
                if received != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
                    raise HouseHunterError("Mountain archive member checksum mismatch")
        for value in archive["datasets"]:
            dataset = temporary / _safe_archive_relative(value)
            if not dataset.exists() or dataset.is_symlink():
                raise HouseHunterError("Mountain archive dataset root is missing or unsafe")
        os.replace(temporary, final)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HouseHunterError(f"Cannot extract Mountain archive: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return _verify_extracted_archive(source, destination)


def _validated_source_url(value: object, allowed_hosts: set[str]) -> str:
    url = str(value)
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise HouseHunterError("Mountain source URL has an invalid port") from exc
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise HouseHunterError("Mountain source URL cannot use an IP address")
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in allowed_hosts
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise HouseHunterError("Mountain source URL violates the reviewed HTTPS host policy")
    return url


def _validate_public_dns(url: str) -> None:
    hostname = urlsplit(url).hostname
    assert hostname is not None
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise HouseHunterError("Mountain source hostname cannot be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise HouseHunterError("Mountain source hostname resolves to a non-public address")


def _validate_gis_contract_shape(contract: dict[str, Any], suffix: str) -> None:
    try:
        count = int(contract["count"])
        schema = contract["schema"]
        crs = contract["crs"]
    except (KeyError, TypeError, ValueError) as exc:
        raise HouseHunterError("Mountain GIS dataset contract is incomplete") from exc
    if count <= 0 or not isinstance(schema, dict) or not schema:
        raise HouseHunterError("Mountain GIS dataset contract has invalid schema/count")
    if suffix in {".tif", ".tiff"}:
        if (
            set(schema) != {"bands", "dtypes", "nodata", "width", "height"}
            or type(schema["bands"]) is not int
            or schema["bands"] <= 0
            or not isinstance(schema["dtypes"], list)
            or len(schema["dtypes"]) != schema["bands"]
            or any(not isinstance(value, str) for value in schema["dtypes"])
            or type(schema["width"]) is not int
            or schema["width"] <= 0
            or type(schema["height"]) is not int
            or schema["height"] <= 0
        ):
            raise HouseHunterError("Mountain raster metadata contract is invalid")
    elif suffix == ".parquet":
        if crs not in {None, "none"} or any(
            not isinstance(name, str) or not isinstance(dtype, str)
            for name, dtype in schema.items()
        ):
            raise HouseHunterError("Mountain Parquet metadata contract is invalid")
        return
    elif suffix in {".fgb", ".gdb", ".gpkg", ".shp"}:
        if not isinstance(contract.get("geometry_type"), str):
            raise HouseHunterError("Mountain vector geometry type contract is invalid")
        if any(
            not isinstance(name, str) or not isinstance(dtype, str)
            for name, dtype in schema.items()
        ):
            raise HouseHunterError("Mountain vector metadata contract is invalid")
    else:
        raise HouseHunterError("Mountain GIS dataset contract uses an unapproved format")
    if not isinstance(crs, str) or not crs or not _crs_matches(crs, crs):
        raise HouseHunterError("Mountain GIS dataset contract has an invalid CRS")


def _validate_source_lock_contract(lock: dict[str, Any]) -> set[str]:
    schema = lock.get("schema_version")
    if schema not in {1, SOURCE_LOCK_SCHEMA_VERSION} or not isinstance(lock.get("sources"), list):
        raise HouseHunterError("Mountain source lock is incompatible")
    if any(not isinstance(source, dict) for source in lock["sources"]):
        raise HouseHunterError("Mountain source lock contains an invalid entry")
    if schema == 1:
        for source in lock["sources"]:
            parsed = urlsplit(str(source.get("url", "")))
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise HouseHunterError("Mountain source URL cannot contain credentials")
        return set()
    allowed = lock.get("allowed_hosts")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(value, str) or value != value.lower() for value in allowed)
    ):
        raise HouseHunterError("Mountain source lock lacks a reviewed host allowlist")
    allowed_hosts = set(allowed)
    expectations = lock.get("expected_states")
    digest = lock.get("block_geoid_sha256")
    region_crs = lock.get("region_crs")
    precedence = lock.get("elevation_precedence")
    inventory = lock.get("tile_inventory")
    representative_tiles = lock.get("representative_tiles")
    preparation_batches = lock.get("preparation_batches", [])
    trail_fragment_mode = lock.get("trail_fragment_mode")
    trail_fragment_contract = lock.get("trail_fragment_contract")
    storage = lock.get("storage_projection")
    if not isinstance(preparation_batches, list) or any(
        not isinstance(batch, dict) for batch in preparation_batches
    ):
        raise HouseHunterError("Mountain source lock has invalid preparation batches")
    if trail_fragment_mode is not None:
        if (
            trail_fragment_mode != "state_clipped_globalid_v1"
            or not isinstance(trail_fragment_contract, dict)
            or set(trail_fragment_contract)
            != {"feature_rows", "fragment_rows", "logical_features", "logical_sha256"}
            or any(
                type(trail_fragment_contract[key]) is not int or trail_fragment_contract[key] <= 0
                for key in ("feature_rows", "fragment_rows", "logical_features")
            )
            or trail_fragment_contract["logical_features"] > trail_fragment_contract["feature_rows"]
            or trail_fragment_contract["feature_rows"] > trail_fragment_contract["fragment_rows"]
            or re.fullmatch(r"[0-9a-f]{64}", str(trail_fragment_contract["logical_sha256"])) is None
        ):
            raise HouseHunterError("Mountain source lock has an invalid trail fragment contract")
    elif trail_fragment_contract is not None:
        raise HouseHunterError("Mountain source lock has an unexpected trail fragment contract")
    if not isinstance(expectations, dict) or set(expectations) != IN_SCOPE_STATES:
        raise HouseHunterError("Mountain source lock lacks 50-state-plus-DC expectations")
    for state, expectation in expectations.items():
        if (
            not isinstance(expectation, dict)
            or type(expectation.get("blocks")) is not int
            or type(expectation.get("population")) is not int
            or expectation["blocks"] <= 0
            or expectation["population"] < 0
        ):
            raise HouseHunterError(f"Mountain source lock has invalid expectations for {state}")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise HouseHunterError("Mountain source lock lacks the national block GEOID digest")
    if (
        not isinstance(region_crs, dict)
        or set(region_crs) != {"conus_dc", "alaska", "hawaii"}
        or region_crs.get("conus_dc") != "EPSG:5070"
        or region_crs.get("alaska") != "EPSG:3338"
        or not isinstance(region_crs.get("hawaii"), str)
    ):
        raise HouseHunterError("Mountain source lock contains incomplete region CRS definitions")
    if region_crs["hawaii"] != HAWAII_WKT2_2019:
        raise HouseHunterError("Mountain source lock contains an invalid Hawaii CRS WKT")
    if (
        not isinstance(representative_tiles, list)
        or {item.get("case") for item in representative_tiles if isinstance(item, dict)}
        != REPRESENTATIVE_TILE_CASES
        or len(representative_tiles) != len(REPRESENTATIVE_TILE_CASES)
    ):
        raise HouseHunterError("Mountain source lock lacks representative tile digests")
    representative_keys: set[tuple[str, int, int]] = set()
    required_regions = {
        "alaska": "alaska",
        "hawaii": "hawaii",
        "coastal_nodata": "conus_dc",
        "colorado_rockies": "conus_dc",
        "flat_plains": "conus_dc",
        "overlap_seam": "conus_dc",
    }
    for item in representative_tiles:
        try:
            case = str(item["case"])
            tile_key = (
                str(item["region"]),
                int(item["tile_x"]),
                int(item["tile_y"]),
            )
            raw_digest = str(item["raw_metric_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError(
                "Mountain source lock has invalid representative tile metadata"
            ) from exc
        if (
            re.fullmatch(r"[0-9a-f]{64}", raw_digest) is None
            or tile_key[0] != required_regions[case]
            or tile_key in representative_keys
        ):
            raise HouseHunterError("Mountain source lock has invalid representative tile metadata")
        representative_keys.add(tile_key)
    if (
        not isinstance(precedence, list)
        or not precedence
        or len(precedence) != len(set(precedence))
    ):
        raise HouseHunterError("Mountain source lock lacks ordered elevation precedence")
    try:
        inventory_tiles = inventory.get("tiles") if isinstance(inventory, dict) else None
        storage_values = (
            {key: int(storage[key]) for key in _STORAGE_PRIMITIVE_KEYS}
            if isinstance(storage, dict)
            else {}
        )
        expected_storage = storage_projection(
            lock["sources"],
            preparation_batches=preparation_batches,
            prepared_pack_bytes=storage_values["prepared_pack_bytes"],
            preparation_workspace_bytes=storage_values["preparation_workspace_bytes"],
            preparation_overlap_bytes=storage_values["preparation_overlap_bytes"],
            active_and_rollback_release_bytes=storage_values["active_and_rollback_release_bytes"],
            candidate_release_bytes=storage_values["candidate_release_bytes"],
            work_shard_bytes=storage_values["work_shard_bytes"],
            comparison_shard_bytes=storage_values["comparison_shard_bytes"],
            compact_bundle_bytes=storage_values["compact_bundle_bytes"],
            report_bytes=storage_values["report_bytes"],
            retained_baseline_bytes=storage_values["retained_baseline_bytes"],
            safety_reserve_bytes=storage_values["safety_reserve_bytes"],
            maximum_atomic_write_bytes=storage_values["maximum_atomic_write_bytes"],
        )
        if (
            not isinstance(inventory, dict)
            or not isinstance(inventory_tiles, list)
            or len(inventory_tiles) != int(inventory["tile_count"])
            or int(inventory["tile_count"]) <= 0
            or int(inventory["block_count"]) <= 0
            or not isinstance(storage, dict)
            or any(value < 0 for value in storage_values.values())
            or storage != expected_storage
            or int(storage["prepared_pack_bytes"]) > 22_000_000_000
            or int(storage["active_and_rollback_release_bytes"]) > 8_000_000_000
            or int(storage["candidate_release_bytes"]) > 4_000_000_000
            or int(storage["work_shard_bytes"]) > 4_000_000_000
            or int(storage["safety_reserve_bytes"]) < 7_000_000_000
            or any(
                int(storage[key]) > 45_000_000_000 or int(storage[key]) >= SOURCE_DOWNLOAD_MAX_BYTES
                for key in (
                    "acquisition_peak_bytes",
                    "preparation_peak_bytes",
                    "timed_build_peak_bytes",
                    "managed_peak_bytes",
                )
            )
        ):
            raise ValueError("invalid projection")
        normalized_tiles = [
            {
                "region": str(tile["region"]),
                "tile_x": int(tile["tile_x"]),
                "tile_y": int(tile["tile_y"]),
                "blocks": int(tile["blocks"]),
                "population": int(tile["population"]),
                "block_geoid_sha256": str(tile["block_geoid_sha256"]),
                "block_sample_sha256": str(tile["block_sample_sha256"]),
            }
            for tile in inventory_tiles
        ]
        if (
            normalized_tiles
            != sorted(
                normalized_tiles,
                key=lambda item: (item["region"], item["tile_x"], item["tile_y"]),
            )
            or len({(item["region"], item["tile_x"], item["tile_y"]) for item in normalized_tiles})
            != len(normalized_tiles)
            or sum(item["blocks"] for item in normalized_tiles) != int(inventory["block_count"])
            or sum(item["blocks"] for item in normalized_tiles)
            != sum(int(value["blocks"]) for value in expectations.values())
            or sum(item["population"] for item in normalized_tiles)
            != sum(int(value["population"]) for value in expectations.values())
            or any(item["blocks"] <= 0 or item["population"] < 0 for item in normalized_tiles)
            or any(
                re.fullmatch(r"[0-9a-f]{64}", item["block_geoid_sha256"]) is None
                or re.fullmatch(r"[0-9a-f]{64}", item["block_sample_sha256"]) is None
                for item in normalized_tiles
            )
        ):
            raise ValueError("invalid inventory")
        inventory_keys = {
            (item["region"], item["tile_x"], item["tile_y"]) for item in normalized_tiles
        }
        if representative_keys - inventory_keys:
            raise ValueError("representative tile is absent from inventory")
    except (KeyError, TypeError, ValueError) as exc:
        raise HouseHunterError(
            "Mountain source lock lacks a qualified tile and storage projection"
        ) from exc
    families = {"blocks", "elevation", "pad_us", "trails"}
    observed_families = set()
    elevation_names = []
    source_names: list[str] = []
    source_filenames: list[str] = []
    archive_roots: list[str] = []
    for source in lock["sources"]:
        _reject_secret_fields(source)
        family = source.get("family")
        if family not in families:
            raise HouseHunterError("Mountain source lock contains an invalid source family")
        observed_families.add(family)
        source_names.append(str(source.get("name", "")))
        source_filenames.append(str(source.get("filename", "")))
        try:
            if (
                not source_names[-1]
                or not source_filenames[-1]
                or not isinstance(source.get("acquired_at"), str)
                or not source["acquired_at"]
                or not isinstance(source.get("release"), str)
                or not source["release"]
                or not isinstance(source.get("license"), str)
                or not source["license"]
                or source.get("crs") in (None, "", {})
                or not isinstance(source.get("schema"), dict)
                or not source["schema"]
                or type(source.get("count")) is not int
                or source["count"] <= 0
                or int(source["size"]) <= 0
                or re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"])) is None
            ):
                raise ValueError("invalid metadata")
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError("Mountain source lock contains invalid metadata") from exc
        if family == "elevation":
            elevation_names.append(source.get("name"))
        _validated_source_url(source.get("url"), allowed_hosts)
        acquisition = source.get("acquisition")
        if acquisition is not None:
            page_sha256 = acquisition.get("page_sha256") if isinstance(acquisition, dict) else None
            page_artifact_sha256 = (
                acquisition.get("page_artifact_sha256") if isinstance(acquisition, dict) else None
            )
            page_artifact_size = (
                acquisition.get("page_artifact_size") if isinstance(acquisition, dict) else None
            )
            access_counts = (
                acquisition.get("access_counts") if isinstance(acquisition, dict) else None
            )
            if (
                family != "pad_us"
                or not isinstance(acquisition, dict)
                or acquisition.get("type") != "arcgis_query_snapshot_v1"
                or acquisition.get("object_id_field") != "OBJECTID"
                or acquisition.get("fields") != ["OBJECTID", "Pub_Access"]
                or acquisition.get("where") != "1=1"
                or acquisition.get("order_by") != "OBJECTID ASC"
                or type(acquisition.get("page_size")) is not int
                or not 1 <= acquisition["page_size"] <= 2_000
                or acquisition.get("output_crs") != "EPSG:4326"
                or re.fullmatch(r"[0-9a-f]{64}", str(acquisition.get("service_metadata_sha256")))
                is None
                or any(
                    type(acquisition.get(key)) is not int or acquisition[key] <= 0
                    for key in (
                        "download_bytes",
                        "page_shard_bytes",
                        "maximum_response_bytes",
                    )
                )
                or acquisition["maximum_response_bytes"] > 250_000_000
                or not isinstance(page_sha256, list)
                or not page_sha256
                or any(re.fullmatch(r"[0-9a-f]{64}", str(value)) is None for value in page_sha256)
                or not isinstance(page_artifact_sha256, list)
                or len(page_artifact_sha256) != len(page_sha256)
                or any(
                    re.fullmatch(r"[0-9a-f]{64}", str(value)) is None
                    for value in page_artifact_sha256
                )
                or not isinstance(page_artifact_size, list)
                or len(page_artifact_size) != len(page_sha256)
                or any(type(value) is not int or value <= 0 for value in page_artifact_size)
                or sum(page_artifact_size) != acquisition["page_shard_bytes"]
                or re.fullmatch(r"[0-9a-f]{64}", str(acquisition.get("objectid_sha256"))) is None
                or not isinstance(access_counts, dict)
                or set(access_counts) != {"OA", "RA", "UK", "XA"}
                or any(type(value) is not int or value < 0 for value in access_counts.values())
                or sum(access_counts.values()) != source["count"]
                or len(page_sha256) != math.ceil(source["count"] / acquisition["page_size"])
            ):
                raise HouseHunterError("Mountain source lock has an invalid ArcGIS snapshot")
        suffix = Path(str(source.get("filename", ""))).suffix.lower()
        if suffix not in {
            ".fgb",
            ".gpkg",
            ".parquet",
            ".tif",
            ".tiff",
            ".zip",
        }:
            raise HouseHunterError("Mountain source lock contains an unapproved GIS format")
        if suffix == ".parquet" and family != "blocks":
            raise HouseHunterError("Only locked block metrics may use Parquet source inputs")
        archive = source.get("archive")
        if suffix == ".zip":
            if (
                not isinstance(archive, dict)
                or archive.get("format") != "zip"
                or not isinstance(archive.get("datasets"), list)
                or not archive["datasets"]
            ):
                raise HouseHunterError("Mountain ZIP source lacks reviewed extraction metadata")
            archive_roots.append(_archive_root(source, Path.cwd()).name)
            _archive_member_metadata(source)
            contracts = archive.get("dataset_contracts")
            if (
                not isinstance(contracts, list)
                or len(contracts) != len(archive["datasets"])
                or {
                    str(contract.get("path"))
                    for contract in contracts
                    if isinstance(contract, dict)
                }
                != set(archive["datasets"])
            ):
                raise HouseHunterError("Mountain ZIP source lacks dataset metadata contracts")
            for dataset in archive["datasets"]:
                dataset_path = _safe_archive_relative(dataset)
                if dataset_path.suffix.lower() not in {
                    ".fgb",
                    ".gdb",
                    ".gpkg",
                    ".shp",
                    ".tif",
                    ".tiff",
                }:
                    raise HouseHunterError("Mountain archive names an unapproved GIS dataset")
                contract = next(
                    item
                    for item in contracts
                    if isinstance(item, dict) and item.get("path") == dataset
                )
                _validate_gis_contract_shape(contract, dataset_path.suffix.lower())
            try:
                if int(archive["total_uncompressed_size"]) < 0:
                    raise ValueError("negative expansion")
            except (KeyError, TypeError, ValueError) as exc:
                raise HouseHunterError("Mountain ZIP source has an invalid expansion size") from exc
        elif archive is not None:
            raise HouseHunterError("Mountain non-archive source has archive metadata")
        else:
            _validate_gis_contract_shape(source, suffix)
    if (
        observed_families != families
        or set(elevation_names) != set(precedence)
        or len(source_names) != len(set(source_names))
        or len(source_filenames) != len({value.casefold() for value in source_filenames})
        or len(archive_roots) != len({value.casefold() for value in archive_roots})
        or {value.casefold() for value in archive_roots}
        & {value.casefold() for value in source_filenames}
    ):
        raise HouseHunterError("Mountain source lock lacks all required source families")
    if preparation_batches:
        source_by_name = {str(source["name"]): source for source in lock["sources"]}
        inventory_tile_keys = {
            f"{tile['region']}:{int(tile['tile_x'])}:{int(tile['tile_y'])}"
            for tile in inventory_tiles
        }
        batch_ids: set[str] = set()
        covered_sources: dict[str, list[str]] = {}
        covered_tiles: dict[str, list[str]] = {}
        for batch in preparation_batches:
            batch_id = str(batch.get("id", ""))
            family = str(batch.get("family", ""))
            names = batch.get("sources")
            tile_keys = batch.get("tile_keys")
            fragment_batch = family == "trails" and trail_fragment_mode is not None
            if (
                re.fullmatch(r"(?:elevation|trails)-[0-9]{3}", batch_id) is None
                or batch_id in batch_ids
                or family not in {"elevation", "trails"}
                or not isinstance(names, list)
                or len(names) != len(set(names))
                or any(
                    name not in source_by_name or source_by_name[name].get("family") != family
                    for name in names
                )
                or (
                    not fragment_batch
                    and (
                        not isinstance(tile_keys, list)
                        or not tile_keys
                        or len(tile_keys) != len(set(tile_keys))
                        or not set(tile_keys) <= inventory_tile_keys
                    )
                )
                or (fragment_batch and tile_keys is not None)
            ):
                raise HouseHunterError("Mountain source lock has an invalid preparation batch")
            calculated_staging = sum(
                _source_staging_allocation(source_by_name[name]) for name in names
            )
            calculated_part = max(
                (_source_maximum_part(source_by_name[name]) for name in names), default=0
            )
            if (
                batch.get("source_staging_bytes") != calculated_staging
                or batch.get("maximum_part_bytes") != calculated_part
            ):
                raise HouseHunterError("Mountain preparation batch storage differs from sources")
            batch_ids.add(batch_id)
            covered_sources.setdefault(family, []).extend(str(name) for name in names)
            covered_tiles.setdefault(family, []).extend(
                str(key) for key in ([] if tile_keys is None else tile_keys)
            )
        for family in covered_sources:
            expected_sources = {
                str(source["name"]) for source in lock["sources"] if source.get("family") == family
            }
            sources = covered_sources[family]
            tiles = covered_tiles[family]
            invalid_tiles = family == "elevation" and (
                len(tiles) != len(inventory_tile_keys) or set(tiles) != inventory_tile_keys
            )
            if (
                set(sources) != expected_sources
                or (family == "trails" and len(sources) != len(set(sources)))
                or invalid_tiles
            ):
                raise HouseHunterError(
                    f"Mountain {family} preparation batches are incomplete or overlapping"
                )
        for family in ("elevation", "trails"):
            family_batches = [
                batch for batch in preparation_batches if batch.get("family") == family
            ]
            if not family_batches:
                continue
            if [batch["id"] for batch in family_batches] != [
                f"{family}-{index:03d}" for index in range(1, len(family_batches) + 1)
            ]:
                raise HouseHunterError(
                    f"Mountain {family} preparation batches are not canonically ordered"
                )
            source_order = (
                precedence
                if family == "elevation"
                else [
                    str(source["name"])
                    for source in lock["sources"]
                    if source.get("family") == "trails"
                ]
            )
            for batch in family_batches:
                names = [str(name) for name in batch["sources"]]
                if names != [name for name in source_order if name in set(names)]:
                    raise HouseHunterError(
                        f"Mountain {family} preparation batch source order differs"
                    )
            if (
                family == "trails"
                and [str(name) for batch in family_batches for name in batch["sources"]]
                != source_order
            ):
                raise HouseHunterError(
                    "Mountain trails preparation batches differ from canonical source order"
                )
    if trail_fragment_mode is not None and not any(
        batch.get("family") == "trails" for batch in preparation_batches
    ):
        raise HouseHunterError("Mountain trail fragment mode requires trail ingestion batches")
    return allowed_hosts


def _crs_matches(actual: object, expected: object) -> bool:
    if actual in {None, "none"} and expected in {None, "none"}:
        return True
    try:
        return CRS.from_user_input(actual) == CRS.from_user_input(expected)
    except Exception:
        return False


def _verify_gis_dataset(path: Path, contract: dict[str, Any]) -> None:
    suffix = path.suffix.lower()
    layer = contract.get("layer")
    expected_schema = contract.get("schema")
    try:
        expected_count = int(contract["count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HouseHunterError("Mountain GIS dataset contract has an invalid count") from exc
    if suffix in {".tif", ".tiff"}:
        try:
            with rasterio.open(path) as dataset:
                actual_schema = {
                    "bands": dataset.count,
                    "dtypes": list(dataset.dtypes),
                    "nodata": dataset.nodata,
                    "width": dataset.width,
                    "height": dataset.height,
                }
                actual_count = dataset.width * dataset.height
                actual_crs = dataset.crs
        except (OSError, rasterio.errors.RasterioError) as exc:
            raise HouseHunterError(
                f"Cannot inspect locked Mountain raster {path.name}: {exc}"
            ) from exc
    elif suffix in {".fgb", ".gdb", ".gpkg", ".shp"}:
        try:
            info = pyogrio.read_info(path, layer=layer)
            actual_schema = {
                str(field): str(dtype)
                for field, dtype in zip(info["fields"], info["dtypes"], strict=True)
            }
            actual_count = int(info["features"])
            actual_crs = info.get("crs")
            actual_geometry_type = info.get("geometry_type")
        except Exception as exc:
            raise HouseHunterError(
                f"Cannot inspect locked Mountain vector {path.name}: {exc}"
            ) from exc
    elif suffix == ".parquet":
        try:
            actual_schema = {name: str(dtype) for name, dtype in pl.read_schema(path).items()}
            actual_count = pl.scan_parquet(path).select(pl.len()).collect().item()
            actual_crs = None
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise HouseHunterError(
                f"Cannot inspect locked Mountain Parquet {path.name}: {exc}"
            ) from exc
    else:
        raise HouseHunterError(f"Mountain GIS dataset format is not approved: {path.name}")
    if (
        actual_count != expected_count
        or actual_schema != expected_schema
        or not _crs_matches(actual_crs, contract.get("crs"))
        or (
            suffix in {".fgb", ".gdb", ".gpkg", ".shp"}
            and actual_geometry_type != contract.get("geometry_type")
        )
    ):
        raise HouseHunterError(f"Mountain GIS metadata differs from its lock: {path.name}")


def _verify_source_gis_contract(source: dict[str, Any], root: Path | None) -> None:
    archive = source.get("archive")
    if isinstance(archive, dict):
        assert root is not None
        extracted = _archive_root(source, root)
        for contract in archive["dataset_contracts"]:
            if not isinstance(contract, dict):
                raise HouseHunterError("Mountain archive dataset contract is invalid")
            _verify_gis_dataset(extracted / _safe_archive_relative(contract.get("path")), contract)
    else:
        path = _source_path(source, root)
        _verify_gis_dataset(path, source)
        acquisition = source.get("acquisition")
        if isinstance(acquisition, dict) and acquisition.get("type") == "arcgis_query_snapshot_v1":
            _, fields = _read_fields(path, acquisition["fields"], layer=source.get("layer"))
            objectids = fields[acquisition["object_id_field"]]
            if (
                len(objectids) != int(source["count"])
                or objectids.dtype.kind not in {"i", "u"}
                or len(np.unique(objectids)) != len(objectids)
            ):
                raise HouseHunterError("Mountain ArcGIS snapshot identities are invalid")
            counts = {
                str(code): int(np.count_nonzero(fields["Pub_Access"] == code))
                for code in acquisition["access_counts"]
            }
            if counts != acquisition["access_counts"]:
                raise HouseHunterError("Mountain ArcGIS snapshot access totals differ")


def verify_source_families(
    path: Path,
    *,
    root: Path,
    families: set[str],
    source_names: set[str] | None = None,
) -> dict[str, Any]:
    """Validate selected local source families against the reviewed lock."""
    try:
        lock = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
    _validate_source_lock_contract(lock)
    available_names = {
        str(source.get("name")) for source in lock["sources"] if source.get("family") in families
    }
    if source_names is not None and (not source_names or not source_names <= available_names):
        raise HouseHunterError("Mountain source verification selection is not locked")
    for source in lock["sources"]:
        if not isinstance(source, dict):
            raise HouseHunterError("Mountain source lock contains an invalid entry")
        if source.get("family") not in families:
            continue
        if source_names is not None and source.get("name") not in source_names:
            continue
        try:
            for key in ("name", "url", "acquired_at", "crs", "schema", "count"):
                if source.get(key) in (None, "", {}):
                    raise ValueError(f"missing {key}")
            file_path = _source_path(source, root)
            expected_size = int(source["size"])
            expected_sha = str(source["sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError(
                f"Mountain source lock contains an invalid entry: {exc}"
            ) from exc
        if not file_path.is_file() or (lock["schema_version"] == 2 and file_path.is_symlink()):
            raise HouseHunterError(f"Mountain source is missing: {file_path}")
        if file_path.stat().st_size != expected_size or sha256_file(file_path) != expected_sha:
            raise HouseHunterError(f"Mountain source checksum mismatch: {file_path}")
        if source.get("archive") is not None:
            if root is None:
                raise HouseHunterError("Mountain archive verification requires a source root")
            _verify_extracted_archive(source, root)
        if lock["schema_version"] == SOURCE_LOCK_SCHEMA_VERSION:
            _verify_source_gis_contract(source, root)
    return lock


def preparation_batch(lock: dict[str, Any], batch_id: str) -> dict[str, Any]:
    """Return one reviewed preparation batch from an already validated v2 lock."""
    matches = [
        batch
        for batch in lock.get("preparation_batches", [])
        if isinstance(batch, dict) and batch.get("id") == batch_id
    ]
    if len(matches) != 1:
        raise HouseHunterError(f"Mountain preparation batch is not locked: {batch_id}")
    return matches[0]


def verify_source_lock(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Validate all local files against a release lock before GIS processing."""
    if root is None:
        try:
            lock = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
        if lock.get("schema_version") == SOURCE_LOCK_SCHEMA_VERSION:
            raise HouseHunterError("Mountain source-lock v2 verification requires a source root")
        _validate_source_lock_contract(lock)
        for source in lock["sources"]:
            file_path = _source_path(source, None)
            if (
                not file_path.is_file()
                or file_path.stat().st_size != int(source["size"])
                or sha256_file(file_path) != source["sha256"]
            ):
                raise HouseHunterError(f"Mountain source checksum mismatch: {file_path}")
        return lock
    lock = load_source_lock_contract(path)
    families = {
        str(source["family"])
        for source in lock["sources"]
        if isinstance(source, dict) and source.get("family") is not None
    }
    return verify_source_families(path, root=root, families=families)


def load_source_lock_contract(path: Path, *, require_v2: bool = False) -> dict[str, Any]:
    """Load a reviewed lock without requiring its large source files to be present."""
    try:
        lock = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
    _validate_source_lock_contract(lock)
    if require_v2 and lock.get("schema_version") != SOURCE_LOCK_SCHEMA_VERSION:
        raise HouseHunterError("National Mountain operations require reviewed source-lock v2")
    return lock


def locked_source_paths(lock: dict[str, Any], *, root: Path | None = None) -> set[Path]:
    paths = {_source_path(source, root).resolve() for source in lock["sources"]}
    if root is not None:
        for source in lock["sources"]:
            archive = source.get("archive")
            if not isinstance(archive, dict):
                continue
            extracted = _archive_root(source, root)
            paths.update(
                (extracted / _safe_archive_relative(value)).resolve()
                for value in archive["datasets"]
            )
    return paths


def delete_managed_source_family(
    lock: dict[str, Any],
    *,
    family: str,
    root: Path,
    staging_root: Path,
) -> None:
    """Delete only reviewed family inputs inside one owned managed staging directory."""
    from .mountain_paths import require_owned_child

    owned = require_owned_child(root, staging_root, name_pattern=r"[0-9a-f]{16}")
    for source in lock["sources"]:
        if source.get("family") != family:
            continue
        source_path = _source_path(source, owned)
        if source_path.is_symlink() or source_path.parent != owned:
            raise HouseHunterError("Mountain managed source path is unsafe")
        if source_path.exists():
            source_path.unlink()
        if isinstance(source.get("archive"), dict):
            extracted = _archive_root(source, owned)
            if extracted.is_symlink() or extracted.parent != owned:
                raise HouseHunterError("Mountain managed archive path is unsafe")
            if extracted.exists():
                shutil.rmtree(extracted)


def delete_managed_sources(
    lock: dict[str, Any],
    *,
    source_names: set[str],
    root: Path,
    staging_root: Path,
) -> None:
    """Delete an exact reviewed source subset from an owned staging directory."""
    from .mountain_paths import require_owned_child

    owned = require_owned_child(root, staging_root, name_pattern=r"[0-9a-f]{16}")
    matched = {
        str(source["name"]) for source in lock["sources"] if source.get("name") in source_names
    }
    if matched != source_names:
        raise HouseHunterError("Mountain managed source deletion is not fully locked")
    for source in lock["sources"]:
        if source.get("name") not in source_names:
            continue
        source_path = _source_path(source, owned)
        if source_path.is_symlink() or source_path.parent != owned:
            raise HouseHunterError("Mountain managed source path is unsafe")
        if source_path.exists():
            source_path.unlink()
        if isinstance(source.get("archive"), dict):
            extracted = _archive_root(source, owned)
            if extracted.is_symlink() or extracted.parent != owned:
                raise HouseHunterError("Mountain managed archive path is unsafe")
            if extracted.exists():
                shutil.rmtree(extracted)


def verify_region_sources_locked(
    regions: tuple[RegionSources, ...],
    lock: dict[str, Any],
    *,
    root: Path,
    verify_block_partition: bool = True,
) -> None:
    locked = locked_source_paths(lock, root=root)
    consumed: set[Path] = set()
    for region in regions:
        block_paths = region.blocks if isinstance(region.blocks, tuple) else (region.blocks,)
        trail_paths = region.trails if isinstance(region.trails, tuple) else (region.trails,)
        consumed.update(
            path.resolve()
            for path in (*block_paths, *region.elevation, region.pad_us, *trail_paths)
        )
    missing = sorted(str(path) for path in consumed - locked)
    if missing:
        raise HouseHunterError(
            "Mountain region uses files absent from the source lock: " + ", ".join(missing)
        )
    if lock.get("schema_version") == 2:
        region_crs = lock["region_crs"]
        by_path: dict[Path, tuple[str, str, str | None]] = {}
        pad_order_by_path: dict[Path, str] = {}
        for source in lock["sources"]:
            source_path = _source_path(source, root).resolve()
            if source.get("archive") is None:
                by_path[source_path] = (
                    source["name"],
                    source["family"],
                    source.get("layer"),
                )
                acquisition = source.get("acquisition")
                if (
                    isinstance(acquisition, dict)
                    and acquisition.get("type") == "arcgis_query_snapshot_v1"
                ):
                    pad_order_by_path[source_path] = str(acquisition["object_id_field"])
            archive = source.get("archive")
            if isinstance(archive, dict):
                extracted = _archive_root(source, root)
                contracts = {
                    str(contract["path"]): contract for contract in archive["dataset_contracts"]
                }
                for dataset in archive["datasets"]:
                    by_path[(extracted / _safe_archive_relative(dataset)).resolve()] = (
                        source["name"],
                        source["family"],
                        contracts[str(dataset)].get("layer"),
                    )
        for region in regions:
            block_paths = region.blocks if isinstance(region.blocks, tuple) else (region.blocks,)
            trail_paths = region.trails if isinstance(region.trails, tuple) else (region.trails,)
            roles = [
                *((path.resolve(), "blocks", region.blocks_layer) for path in block_paths),
                *((path.resolve(), "elevation", None) for path in region.elevation),
                (region.pad_us.resolve(), "pad_us", region.pad_layer),
                *((path.resolve(), "trails", region.trails_layer) for path in trail_paths),
            ]
            if len({path for path, _, _ in roles}) != len(roles):
                raise HouseHunterError(
                    f"Mountain region {region.name} assigns one dataset to duplicate roles"
                )
            if any(
                by_path[path][1] != family or by_path[path][2] != layer
                for path, family, layer in roles
            ):
                raise HouseHunterError(
                    f"Mountain region {region.name} assigns a locked source to the wrong role"
                )
            expected_pad_order = pad_order_by_path.get(region.pad_us.resolve())
            if expected_pad_order is not None and region.pad_order_field != expected_pad_order:
                raise HouseHunterError(
                    f"Mountain region {region.name} does not preserve locked PAD ordering"
                )
            if (
                region.pad_access_field != "Pub_Access"
                or region.trail_where != "trailtype <> 'Water Trail'"
            ):
                raise HouseHunterError(
                    f"Mountain region {region.name} changes mountain_score_v1 source semantics"
                )
        if {region.name for region in regions} != set(region_crs):
            raise HouseHunterError("Mountain region configuration is not the locked national set")
        if consumed != set(by_path):
            raise HouseHunterError(
                "Mountain region configuration must consume every locked GIS dataset"
            )
        precedence = lock["elevation_precedence"]
        national_fips = {fips for fips, state in STATE_BY_FIPS.items() if state in IN_SCOPE_STATES}
        expected_region_fips = {
            "alaska": {"02"},
            "hawaii": {"15"},
            "conus_dc": national_fips - {"02", "15"},
        }
        for region in regions:
            if not _crs_matches(region_crs.get(region.name), region.target_crs):
                raise HouseHunterError(
                    f"Mountain region {region.name} does not use its locked complete CRS"
                )
            block_paths = region.blocks if isinstance(region.blocks, tuple) else (region.blocks,)
            names = [by_path[path.resolve()][0] for path in region.elevation]
            expected = [name for name in precedence if name in names]
            if names != expected:
                raise HouseHunterError(
                    f"Mountain region {region.name} elevation precedence differs from its lock"
                )
            if verify_block_partition:
                observed_fips: set[str] = set()
                for block_path in block_paths:
                    _, fields = _read_fields(block_path, ["GEOID20"], layer=region.blocks_layer)
                    observed_fips.update(
                        str(value)[:2] for value in fields["GEOID20"] if value is not None
                    )
                if observed_fips != expected_region_fips[region.name]:
                    raise HouseHunterError(
                        f"Mountain region {region.name} Census states differ from "
                        "its locked CRS partition"
                    )


def _source_download_reservation(
    lock: dict[str, Any],
    destination: Path,
    *,
    families: set[str] | None = None,
    source_names: set[str] | None = None,
) -> int:
    reserve_bytes = 0
    for source in lock["sources"]:
        if families is not None and source.get("family") not in families:
            continue
        if source_names is not None and source.get("name") not in source_names:
            continue
        target = _source_path(source, destination)
        try:
            valid = (
                target.is_file()
                and not target.is_symlink()
                and target.stat().st_size == int(source["size"])
                and sha256_file(target) == source["sha256"]
            )
        except OSError:
            valid = False
        archive = source.get("archive")
        extract_bytes = (
            int(archive.get("total_uncompressed_size", 0))
            if isinstance(archive, dict)
            and not (destination / str(archive.get("root", "missing"))).exists()
            else 0
        )
        acquisition = source.get("acquisition")
        if (
            isinstance(acquisition, dict)
            and acquisition.get("type") == "arcgis_query_snapshot_v1"
            and not valid
        ):
            reserve_bytes += _source_staging_allocation(source)
        else:
            reserve_bytes += extract_bytes + (0 if valid else int(source["size"]))
    return reserve_bytes


def _download_arcgis_snapshot(
    source: dict[str, Any],
    target: Path,
    *,
    http: httpx.Client,
    owns_client: bool,
    allowed_hosts: set[str],
) -> None:
    """Materialize one locked, anonymous ArcGIS layer as deterministic FlatGeobuf."""
    acquisition = source["acquisition"]
    base_url = _validated_source_url(source["url"], allowed_hosts).rstrip("/")
    query_url = _validated_source_url(f"{base_url}/query", allowed_hosts)
    if owns_client:
        _validate_public_dns(base_url)
    maximum_response = int(acquisition["maximum_response_bytes"])

    def request(url: str, parameters: dict[str, str]) -> dict[str, Any]:
        body: bytes | None = None
        for attempt in range(3):
            with http.stream("POST", url, data=parameters, follow_redirects=False) as response:
                if response.is_redirect:
                    raise HouseHunterError("Mountain ArcGIS snapshot cannot redirect")
                if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                    continue
                response.raise_for_status()
                length = response.headers.get("content-length")
                try:
                    if length is not None and int(length) > maximum_response:
                        raise HouseHunterError("Mountain ArcGIS response exceeds its locked bound")
                except ValueError as exc:
                    raise HouseHunterError("Mountain ArcGIS response length is invalid") from exc
                collected = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    if len(collected) + len(chunk) > maximum_response:
                        raise HouseHunterError("Mountain ArcGIS response exceeds its locked bound")
                    collected.extend(chunk)
                body = bytes(collected)
                break
        assert body is not None
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HouseHunterError("Mountain ArcGIS snapshot response is invalid") from exc
        if (
            not isinstance(payload, dict)
            or "error" in payload
            or payload.get("exceededTransferLimit") is True
        ):
            raise HouseHunterError("Mountain ArcGIS snapshot response is invalid")
        return payload

    def service_digest() -> str:
        payload = request(base_url, {"f": "json"})
        fields = {
            str(item.get("name")): item
            for item in payload.get("fields", [])
            if isinstance(item, dict) and item.get("name") in {"OBJECTID", "Pub_Access"}
        }
        fingerprint = {
            key: payload.get(key)
            for key in (
                "currentVersion",
                "name",
                "type",
                "geometryType",
                "objectIdField",
                "maxRecordCount",
                "supportedQueryFormats",
            )
        }
        fingerprint["fields"] = fields
        return sha256_bytes(canonical_json(fingerprint))

    if service_digest() != acquisition["service_metadata_sha256"]:
        raise HouseHunterError("Mountain ArcGIS service metadata differs")

    def object_id_inventory() -> tuple[list[int], str]:
        payload = request(
            query_url,
            {"f": "json", "where": acquisition["where"], "returnIdsOnly": "true"},
        )
        try:
            values = sorted(int(value) for value in payload["objectIds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError("Mountain ArcGIS object ID inventory is invalid") from exc
        if len(values) != len(set(values)) or len(values) != int(source["count"]):
            raise HouseHunterError("Mountain ArcGIS object ID count differs")
        digest = sha256_bytes("".join(f"{value}\n" for value in values).encode())
        if digest != acquisition["objectid_sha256"]:
            raise HouseHunterError("Mountain ArcGIS object ID digest differs")
        return values, digest

    objectids, objectid_sha256 = object_id_inventory()
    page_root = _arcgis_page_workspace(target, acquisition)
    marker = page_root / ".househunter-mountain-owned"
    if page_root.exists():
        if (
            page_root.is_symlink()
            or not page_root.is_dir()
            or not marker.is_file()
            or marker.is_symlink()
            or marker.read_text() != "arcgis-pages-v1\n"
        ):
            raise HouseHunterError("Mountain ArcGIS page workspace is unsafe")
    else:
        page_root.mkdir()
        marker.write_text("arcgis-pages-v1\n")

    def file_metadata(path: Path) -> dict[str, object]:
        return {"size": path.stat().st_size, "sha256": sha256_file(path)}

    page_size = int(acquisition["page_size"])
    access_counts = {key: 0 for key in ("OA", "RA", "UK", "XA")}
    page_paths: list[Path] = []
    for page_index, offset in enumerate(range(0, len(objectids), page_size)):
        page_ids = objectids[offset : offset + page_size]
        shard = page_root / f"{page_index:06d}.fgb"
        manifest = shard.with_suffix(".json")
        valid = False
        if shard.is_file() and manifest.is_file() and not shard.is_symlink():
            try:
                saved = json.loads(manifest.read_text())
                valid = (
                    saved.get("page_sha256") == acquisition["page_sha256"][page_index]
                    and saved.get("objectids") == page_ids
                    and saved.get("file") == file_metadata(shard)
                    and saved.get("file")
                    == {
                        "size": acquisition["page_artifact_size"][page_index],
                        "sha256": acquisition["page_artifact_sha256"][page_index],
                    }
                )
            except (OSError, json.JSONDecodeError):
                valid = False
        if not valid:
            shard.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            payload = request(
                query_url,
                {
                    "f": "geojson",
                    "objectIds": ",".join(map(str, page_ids)),
                    "outFields": ",".join(acquisition["fields"]),
                    "returnGeometry": "true",
                    "outSR": "4326",
                    "orderByFields": acquisition["order_by"],
                },
            )
            features = payload.get("features")
            if not isinstance(features, list):
                raise HouseHunterError("Mountain ArcGIS feature page is invalid")
            try:
                features.sort(key=lambda item: int(item["properties"]["OBJECTID"]))
                observed_ids = [int(item["properties"]["OBJECTID"]) for item in features]
            except (KeyError, TypeError, ValueError) as exc:
                raise HouseHunterError("Mountain ArcGIS feature identity is invalid") from exc
            if observed_ids != page_ids:
                raise HouseHunterError("Mountain ArcGIS feature page differs from its ID inventory")
            page_digest = sha256_bytes(canonical_json(features))
            if page_digest != acquisition["page_sha256"][page_index]:
                raise HouseHunterError("Mountain ArcGIS feature page digest differs")
            geometries = shapely.from_geojson(
                [json.dumps(item["geometry"], separators=(",", ":")) for item in features]
            )
            if any(
                geometry is None
                or shapely.is_missing(geometry)
                or shapely.is_empty(geometry)
                or geometry.geom_type not in {"Polygon", "MultiPolygon"}
                for geometry in geometries
            ):
                raise HouseHunterError("Mountain ArcGIS polygon geometry is invalid")
            geometries = [
                shapely.MultiPolygon([geometry]) if geometry.geom_type == "Polygon" else geometry
                for geometry in geometries
            ]
            codes = np.array(
                [str(item["properties"]["Pub_Access"]) for item in features], dtype=object
            )
            if any(str(code) not in access_counts for code in codes):
                raise HouseHunterError("Mountain ArcGIS public-access code differs")
            building = shard.with_name(f".{shard.stem}.{uuid.uuid4().hex}.part.fgb")
            try:
                ogr_raw.write(
                    building,
                    shapely.to_wkb(geometries, hex=False, output_dimension=2, byte_order=1),
                    [np.asarray(observed_ids, dtype=np.int64), codes],
                    acquisition["fields"],
                    layer="page",
                    driver="FlatGeobuf",
                    geometry_type="MultiPolygon",
                    crs=acquisition["output_crs"],
                )
                os.replace(building, shard)
            finally:
                building.unlink(missing_ok=True)
            if file_metadata(shard) != {
                "size": acquisition["page_artifact_size"][page_index],
                "sha256": acquisition["page_artifact_sha256"][page_index],
            }:
                shard.unlink(missing_ok=True)
                raise HouseHunterError("Mountain ArcGIS page artifact checksum differs")
            saved = {
                "schema_version": 1,
                "objectid_sha256": objectid_sha256,
                "objectids": page_ids,
                "page_sha256": page_digest,
                "file": file_metadata(shard),
            }
            part = manifest.with_name(f".{manifest.name}.{uuid.uuid4().hex}.part")
            part.write_text(json.dumps(saved, sort_keys=True) + "\n")
            os.replace(part, manifest)
        metadata, _, _, arrays = ogr_raw.read(
            shard, columns=acquisition["fields"], read_geometry=False
        )
        fields = dict(zip(metadata["fields"], arrays, strict=True))
        if sorted(int(value) for value in fields["OBJECTID"]) != page_ids:
            raise HouseHunterError("Mountain ArcGIS page shard identity differs")
        for value in fields["Pub_Access"]:
            code = str(value)
            if code not in access_counts:
                raise HouseHunterError("Mountain ArcGIS public-access code differs")
            access_counts[code] += 1
        page_paths.append(shard)
    if access_counts != acquisition["access_counts"]:
        raise HouseHunterError("Mountain ArcGIS public-access totals differ")
    if service_digest() != acquisition["service_metadata_sha256"]:
        raise HouseHunterError("Mountain ArcGIS service changed during acquisition")
    final_ids, final_digest = object_id_inventory()
    if final_ids != objectids or final_digest != objectid_sha256:
        raise HouseHunterError("Mountain ArcGIS IDs changed during acquisition")

    candidate = target.with_name(f".{target.stem}.assemble.part.fgb")
    candidate.unlink(missing_ok=True)
    for group_offset in range(0, len(page_paths), ARCGIS_ASSEMBLY_PAGE_GROUP):
        geometries: list[np.ndarray] = []
        field_arrays: list[list[np.ndarray]] = []
        field_names: list[str] | None = None
        for shard in page_paths[group_offset : group_offset + ARCGIS_ASSEMBLY_PAGE_GROUP]:
            metadata, _, geometry, arrays = ogr_raw.read(
                shard, columns=acquisition["fields"], force_2d=True
            )
            names = list(metadata["fields"])
            if field_names is not None and names != field_names:
                raise HouseHunterError("Mountain ArcGIS page shard schema differs")
            field_names = names
            geometries.append(geometry)
            field_arrays.append(list(arrays))
        assert field_names is not None
        ogr_raw.write(
            candidate,
            np.concatenate(geometries),
            [
                np.concatenate([arrays[index] for arrays in field_arrays])
                for index in range(len(field_names))
            ],
            field_names,
            layer=source.get("layer"),
            driver="FlatGeobuf",
            geometry_type="MultiPolygon",
            crs=acquisition["output_crs"],
            append=group_offset > 0,
        )
    if (
        candidate.stat().st_size != int(source["size"])
        or sha256_file(candidate) != source["sha256"]
    ):
        candidate.unlink(missing_ok=True)
        raise HouseHunterError("Mountain ArcGIS snapshot artifact checksum differs")
    os.replace(candidate, target)
    shutil.rmtree(page_root)


def _arcgis_page_workspace(target: Path, acquisition: dict[str, Any]) -> Path:
    return target.with_name(f".{sha256_bytes(canonical_json(acquisition))[:32]}.arcgis-pages")


def _remove_arcgis_page_workspace(target: Path, acquisition: dict[str, Any]) -> None:
    page_root = _arcgis_page_workspace(target, acquisition)
    if not page_root.exists() and not page_root.is_symlink():
        return
    marker = page_root / ".househunter-mountain-owned"
    try:
        safe = (
            not page_root.is_symlink()
            and page_root.is_dir()
            and marker.is_file()
            and not marker.is_symlink()
            and marker.read_text() == "arcgis-pages-v1\n"
            and all(
                child == marker
                or (
                    child.is_file()
                    and not child.is_symlink()
                    and re.fullmatch(r"\d{6}\.(?:fgb|json)", child.name) is not None
                )
                for child in page_root.iterdir()
            )
        )
    except OSError:
        safe = False
    if not safe:
        raise HouseHunterError("Mountain ArcGIS page workspace is unsafe")
    shutil.rmtree(page_root)


def download_sources(
    lock_path: Path,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    managed_root: Path | None = None,
    families: set[str] | None = None,
    batch_id: str | None = None,
) -> Path:
    """Download exactly locked source files and publish each only after verification."""
    try:
        lock = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
    allowed_hosts = _validate_source_lock_contract(lock)
    if lock.get("schema_version") != SOURCE_LOCK_SCHEMA_VERSION:
        raise HouseHunterError("Mountain downloads require reviewed source-lock v2")
    allowed_families = {"blocks", "elevation", "pad_us", "trails"}
    if batch_id is not None and families is not None:
        raise HouseHunterError("Mountain download accepts a family or batch, not both")
    selected_names: set[str] | None = None
    if batch_id is not None:
        batch = preparation_batch(lock, batch_id)
        selected_families = {str(batch["family"])}
        selected_names = {str(name) for name in batch["sources"]}
    else:
        selected_families = families or allowed_families
    if not selected_families or not selected_families <= allowed_families:
        raise HouseHunterError("Mountain download source family is invalid")
    selected_sources = [
        source
        for source in lock["sources"]
        if source.get("family") in selected_families
        and (selected_names is None or source.get("name") in selected_names)
    ]
    try:
        projected_bytes = sum(_source_staging_allocation(source) for source in selected_sources)
    except (KeyError, TypeError, ValueError) as exc:
        raise HouseHunterError(f"Mountain source lock contains an invalid size: {exc}") from exc
    if projected_bytes > SOURCE_DOWNLOAD_MAX_BYTES:
        raise HouseHunterError("Mountain locked downloads exceed the 50 GB managed-data cap")
    destination.mkdir(parents=True, exist_ok=True)
    if managed_root is not None:
        from .mountain_pack import ensure_storage_budget

        projected_staging = int(lock["storage_projection"]["source_staging_bytes"])
        if projected_bytes > projected_staging:
            raise HouseHunterError(
                "Mountain managed acquisition must download one qualified source family at a time"
            )
        ensure_storage_budget(
            managed_root,
            reserve_bytes=_source_download_reservation(
                lock,
                destination,
                families=selected_families,
                source_names=selected_names,
            ),
        )
    owns_client = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(120, connect=30), follow_redirects=False, trust_env=False
    )
    try:
        for source in lock["sources"]:
            if not isinstance(source, dict):
                raise HouseHunterError("Mountain source lock contains an invalid entry")
            if source.get("family") not in selected_families:
                continue
            if selected_names is not None and source.get("name") not in selected_names:
                continue
            target = _source_path(source, destination)
            if target.is_file():
                try:
                    if (
                        target.stat().st_size == int(source["size"])
                        and sha256_file(target) == source["sha256"]
                    ):
                        acquisition = source.get("acquisition")
                        if (
                            isinstance(acquisition, dict)
                            and acquisition.get("type") == "arcgis_query_snapshot_v1"
                        ):
                            _remove_arcgis_page_workspace(target, acquisition)
                        if source.get("archive") is not None:
                            extract_locked_archive(source, target, destination)
                        continue
                except (KeyError, TypeError, ValueError):
                    pass
            acquisition = source.get("acquisition")
            if (
                isinstance(acquisition, dict)
                and acquisition.get("type") == "arcgis_query_snapshot_v1"
            ):
                _download_arcgis_snapshot(
                    source,
                    target,
                    http=http,
                    owns_client=owns_client,
                    allowed_hosts=allowed_hosts,
                )
                continue
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
            try:
                expected_size = int(source["size"])
                url = str(source["url"])
                redirects = 0
                while True:
                    _validated_source_url(url, allowed_hosts)
                    if owns_client:
                        _validate_public_dns(url)
                    with http.stream("GET", url, follow_redirects=False) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location or redirects >= 5:
                                raise HouseHunterError("Mountain source redirect policy failed")
                            url = urljoin(url, location)
                            redirects += 1
                            continue
                        response.raise_for_status()
                        content_length = response.headers.get("content-length")
                        if content_length is not None and int(content_length) != expected_size:
                            raise HouseHunterError(
                                f"Mountain source length differs: {source['filename']}"
                            )
                        received = 0
                        with temporary.open("wb") as output:
                            for chunk in response.iter_bytes(1024 * 1024):
                                received += len(chunk)
                                if received > expected_size:
                                    raise HouseHunterError(
                                        f"Mountain source stream is oversized: {source['filename']}"
                                    )
                                output.write(chunk)
                        break
                if (
                    temporary.stat().st_size != expected_size
                    or sha256_file(temporary) != source["sha256"]
                ):
                    raise HouseHunterError(
                        f"Mountain source checksum mismatch: {source['filename']}"
                    )
                os.replace(temporary, target)
                if source.get("archive") is not None:
                    extract_locked_archive(source, target, destination)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise HouseHunterError(f"Mountain source download failed: {exc}") from exc
    finally:
        if owns_client:
            http.close()
    verify_source_families(
        lock_path,
        root=destination,
        families=selected_families,
        source_names=selected_names,
    )
    return destination


def _read_fields(
    path: Path, columns: list[str], *, layer: str | None = None
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    try:
        metadata, _, _, arrays = ogr_raw.read(
            path, layer=layer, columns=columns, read_geometry=False
        )
    except Exception as exc:
        raise HouseHunterError(f"Cannot read Mountain vector source {path}: {exc}") from exc
    return metadata, dict(zip(metadata["fields"], arrays, strict=True))


def _query_bounds(
    target_crs: str,
    source_crs: str,
    target_bounds: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], ...]:
    """Transform a window, splitting geographic antimeridian crossings."""
    west, south, east, north = transform_bounds(
        target_crs, source_crs, *target_bounds, densify_pts=21
    )
    if CRS.from_user_input(source_crs).is_geographic and west > east:
        return ((west, south, 180.0, north), (-180.0, south, east, north))
    return ((west, south, east, north),)


def _read_geometries(
    path: Path,
    *,
    target_bounds: tuple[float, float, float, float],
    target_crs: str,
    columns: list[str] | None = None,
    where: str | None = None,
    expected_type_ids: frozenset[int] | None = None,
    layer: str | None = None,
    require_valid: bool = True,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    try:
        info = pyogrio.read_info(path, layer=layer)
        source_crs = info.get("crs")
        if not source_crs:
            raise HouseHunterError(f"Mountain vector source has no CRS: {path}")
        metadata: dict[str, Any] | None = None
        seen_fids: set[int] = set()
        geometries: list[bytes] = []
        field_values: list[list[object]] = []
        for bbox in _query_bounds(target_crs, str(source_crs), target_bounds):
            current, fids, geometry, arrays = ogr_raw.read(
                path,
                layer=layer,
                columns=columns,
                bbox=bbox,
                where=where,
                force_2d=True,
                return_fids=True,
            )
            if metadata is not None and tuple(current["fields"]) != tuple(metadata["fields"]):
                raise HouseHunterError(f"Mountain vector source schema changed: {path}")
            metadata = current
            if geometry is None:
                continue
            if fids is None:
                raise HouseHunterError(f"Mountain vector source lacks stable feature IDs: {path}")
            if not field_values:
                field_values = [[] for _ in arrays]
            for index, fid in enumerate(fids):
                identifier = int(fid)
                if identifier in seen_fids:
                    continue
                seen_fids.add(identifier)
                geometries.append(geometry[index])
                for values, array in zip(field_values, arrays, strict=True):
                    values.append(array[index])
        if metadata is None or not geometries:
            return np.array([], dtype=object), {}
        values = shapely.from_wkb(geometries)
        if source_crs != target_crs:
            transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
            values = shapely.transform(values, transformer.transform, interleaved=False)
        present = ~shapely.is_missing(values) & ~shapely.is_empty(values)
        if require_valid and np.any(present & ~shapely.is_valid(values)):
            raise HouseHunterError(f"Mountain vector source has invalid geometry: {path}")
        if expected_type_ids is not None:
            type_ids = shapely.get_type_id(values)
            if np.any(present & ~np.isin(type_ids, list(expected_type_ids))):
                raise HouseHunterError(
                    f"Mountain vector source has unexpected geometry type: {path}"
                )
        return values, {
            name: np.asarray(items)
            for name, items in zip(metadata["fields"], field_values, strict=True)
        }
    except HouseHunterError:
        raise
    except Exception as exc:
        raise HouseHunterError(f"Cannot read Mountain vector source {path}: {exc}") from exc


def _read_elevation(
    paths: tuple[Path, ...],
    *,
    bounds: tuple[float, float, float, float],
    target_crs: str,
    cell_size_m: float,
) -> tuple[np.ndarray, rasterio.Affine]:
    left, bottom, right, top = bounds
    width = math.ceil((right - left) / cell_size_m)
    height = math.ceil((top - bottom) / cell_size_m)
    transform = from_origin(left, top, cell_size_m, cell_size_m)
    elevation = np.full((height, width), np.nan, dtype=np.float32)
    for path in paths:
        with rasterio.open(path) as source:
            tile = np.full_like(elevation, np.nan)
            reproject(
                rasterio.band(source, 1),
                tile,
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=source.nodata,
                dst_transform=transform,
                dst_crs=target_crs,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            empty = ~np.isfinite(elevation)
            elevation[empty] = tile[empty]
    return elevation, transform


def _elevation_index(paths: tuple[Path, ...], target_crs: str) -> tuple[tuple[Path, ...], STRtree]:
    footprints = []
    for path in paths:
        try:
            with rasterio.open(path) as source:
                if source.crs is None:
                    raise HouseHunterError(f"Mountain elevation source has no CRS: {path}")
                footprints.append(
                    shapely.box(
                        *transform_bounds(source.crs, target_crs, *source.bounds, densify_pts=21)
                    )
                )
        except HouseHunterError:
            raise
        except (OSError, rasterio.errors.RasterioError) as exc:
            raise HouseHunterError(f"Cannot index Mountain elevation source {path}: {exc}") from exc
    if not footprints:
        raise HouseHunterError("Mountain region contains no elevation sources")
    return paths, STRtree(footprints)


def _blocks(source: RegionSources) -> pl.DataFrame:
    columns = ["GEOID20", "POP20", "INTPTLAT20", "INTPTLON20"]
    paths = source.blocks if isinstance(source.blocks, tuple) else (source.blocks,)
    frames = []
    for path in paths:
        _, fields = _read_fields(path, columns, layer=source.blocks_layer)
        try:
            longitude = np.asarray(fields["INTPTLON20"], dtype=float)
            latitude = np.asarray(fields["INTPTLAT20"], dtype=float)
            x, y = Transformer.from_crs("EPSG:4326", source.target_crs, always_xy=True).transform(
                longitude, latitude
            )
            block = np.asarray(fields["GEOID20"], dtype=str)
            population = np.asarray(fields["POP20"], dtype=np.int64)
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError(f"Mountain Census block schema is invalid: {exc}") from exc
        frames.append(
            pl.DataFrame(
                {
                    "block_geoid": block,
                    "tract_geoid": [value[:11] for value in block],
                    "county_fips": [value[:5] for value in block],
                    "state_fips": [value[:2] for value in block],
                    "pop20": population,
                    "x": x,
                    "y": y,
                }
            )
        )
    frame = pl.concat(frames)
    if frame.filter(~pl.col("block_geoid").str.contains(r"^\d{15}$")).height:
        raise HouseHunterError("Mountain Census source contains invalid GEOID20 values")
    return frame


def _sample(array: np.ndarray, rows: np.ndarray, columns: np.ndarray) -> np.ndarray:
    values = array[rows, columns]
    if values.dtype == bool:
        return values.astype(float)
    return values


def qualify_national_inventory(
    regions: tuple[RegionSources, ...],
    *,
    state_by_fips: dict[str, str],
    sources: list[dict[str, Any]] | None = None,
    cell_size_m: float = CELL_SIZE_M,
    tile_size_m: float = TILE_SIZE_M,
) -> dict[str, object]:
    """Derive the exact block-driven tile inventory and a conservative storage envelope."""
    if cell_size_m != CELL_SIZE_M or tile_size_m != TILE_SIZE_M:
        raise HouseHunterError("National Mountain qualification requires the v1 250 m/100 km grid")
    frames = []
    tiles: list[dict[str, object]] = []
    for region in regions:
        blocks = _blocks(region).with_columns(
            pl.col("state_fips").replace_strict(state_by_fips).alias("state"),
            (pl.col("x") / tile_size_m).floor().cast(pl.Int64).alias("tile_x"),
            (pl.col("y") / tile_size_m).floor().cast(pl.Int64).alias("tile_y"),
        )
        frames.append(blocks.select("block_geoid", "state", "pop20"))
        for tile in blocks.partition_by(["tile_x", "tile_y"], maintain_order=True):
            tile_x = int(tile["tile_x"][0])
            tile_y = int(tile["tile_y"][0])
            left = tile_x * tile_size_m - HALO_M
            top = (tile_y + 1) * tile_size_m + HALO_M
            sampled = tile.with_columns(
                ((pl.col("x") - left) / cell_size_m).floor().cast(pl.Int32).alias("column"),
                ((top - pl.col("y")) / cell_size_m).floor().cast(pl.Int32).alias("row"),
            ).sort("block_geoid")
            geoids = sampled["block_geoid"].to_list()
            sample_rows = sampled.select("block_geoid", "pop20", "row", "column").rows()
            tiles.append(
                {
                    "region": region.name,
                    "tile_x": tile_x,
                    "tile_y": tile_y,
                    "blocks": tile.height,
                    "population": int(tile["pop20"].sum()),
                    "block_geoid_sha256": hashlib.sha256(
                        ("\n".join(geoids) + "\n").encode()
                    ).hexdigest(),
                    "block_sample_sha256": sha256_bytes(
                        canonical_json(
                            [
                                [
                                    geoid,
                                    int(pop),
                                    region.name,
                                    tile_x,
                                    tile_y,
                                    int(row),
                                    int(column),
                                ]
                                for geoid, pop, row, column in sample_rows
                            ]
                        )
                    ),
                }
            )
    national = pl.concat(frames).sort("block_geoid")
    if national["block_geoid"].n_unique() != national.height:
        raise HouseHunterError("Mountain qualification found duplicate national block GEOIDs")
    expectations = {
        row["state"]: {"blocks": row["blocks"], "population": row["population"]}
        for row in national.group_by("state")
        .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
        .sort("state")
        .iter_rows(named=True)
    }
    if set(expectations) != IN_SCOPE_STATES:
        raise HouseHunterError("Mountain qualification does not cover exactly 50 states plus DC")
    tiles.sort(key=lambda item: (item["region"], item["tile_x"], item["tile_y"]))
    cells = int((tile_size_m + 2 * HALO_M) / cell_size_m)
    prepared_bytes = len(tiles) * (cells * cells * 9 + 512) + national.height * 192
    release_bytes = national.height * 300
    work_bytes = national.height * 240
    active_rollback_bytes = release_bytes * 2
    safety_bytes = 7_000_000_000
    projection = storage_projection(
        sources or [],
        prepared_pack_bytes=prepared_bytes,
        active_and_rollback_release_bytes=active_rollback_bytes,
        candidate_release_bytes=release_bytes,
        work_shard_bytes=work_bytes,
        comparison_shard_bytes=work_bytes,
        safety_reserve_bytes=safety_bytes,
    )
    if (
        prepared_bytes > 22_000_000_000
        or active_rollback_bytes > 8_000_000_000
        or release_bytes > 4_000_000_000
        or work_bytes > 4_000_000_000
        or projection["managed_peak_bytes"] > 45_000_000_000
    ):
        raise HouseHunterError("Mountain national inventory cannot fit the storage budgets")
    digest = hashlib.sha256(("\n".join(national["block_geoid"].to_list()) + "\n").encode())
    return {
        "schema_version": 1,
        "grid": {
            "cell_size_m": cell_size_m,
            "tile_size_m": tile_size_m,
            "halo_m": HALO_M,
            "shape": [cells, cells],
        },
        "tile_inventory": {
            "tile_count": len(tiles),
            "block_count": national.height,
            "tiles": tiles,
        },
        "expected_states": expectations,
        "block_geoid_sha256": digest.hexdigest(),
        "storage_projection": projection,
    }


def build_region_raw_metrics(
    source: RegionSources,
    *,
    state_by_fips: dict[str, str],
    cell_size_m: float = CELL_SIZE_M,
    tile_size_m: float = TILE_SIZE_M,
) -> pl.DataFrame:
    """Compute one region in bounded tiles and return block-level raw metrics."""
    outputs = [
        _raw_metrics_from_tile(tile, elevation, pad, trails, cell_size_m=cell_size_m)
        for tile, elevation, pad, trails in iter_region_tiles(
            source,
            state_by_fips=state_by_fips,
            cell_size_m=cell_size_m,
            tile_size_m=tile_size_m,
        )
    ]
    return pl.concat(outputs).sort("block_geoid")


def iter_region_block_samples(
    source: RegionSources,
    *,
    state_by_fips: dict[str, str],
    cell_size_m: float = CELL_SIZE_M,
    tile_size_m: float = TILE_SIZE_M,
    skip_tiles: set[tuple[int, int]] | None = None,
) -> Iterator[tuple[pl.DataFrame, tuple[float, float, float, float]]]:
    """Yield canonical block samples and halo bounds without reading other GIS families."""
    blocks = _blocks(source).with_columns(
        pl.col("state_fips").replace_strict(state_by_fips).alias("state"),
        (pl.col("x") / tile_size_m).floor().cast(pl.Int64).alias("tile_x"),
        (pl.col("y") / tile_size_m).floor().cast(pl.Int64).alias("tile_y"),
    )
    for tile in blocks.partition_by(["tile_x", "tile_y"], maintain_order=True):
        tile_x = int(tile["tile_x"][0])
        tile_y = int(tile["tile_y"][0])
        if skip_tiles and (tile_x, tile_y) in skip_tiles:
            continue
        core = (
            tile_x * tile_size_m,
            tile_y * tile_size_m,
            (tile_x + 1) * tile_size_m,
            (tile_y + 1) * tile_size_m,
        )
        bounds = (
            core[0] - HALO_M,
            core[1] - HALO_M,
            core[2] + HALO_M,
            core[3] + HALO_M,
        )
        columns = np.floor((tile["x"].to_numpy() - bounds[0]) / cell_size_m).astype(int)
        rows = np.floor((bounds[3] - tile["y"].to_numpy()) / cell_size_m).astype(int)
        shape = math.ceil((bounds[2] - bounds[0]) / cell_size_m)
        if (
            np.any(rows < 0)
            or np.any(columns < 0)
            or np.any(rows >= shape)
            or np.any(columns >= shape)
        ):
            raise HouseHunterError(f"Mountain block sample falls outside region tile {core}")
        samples = tile.select(
            "block_geoid", "tract_geoid", "county_fips", "state", "pop20"
        ).with_columns(
            pl.Series("row", rows.astype(np.int32)),
            pl.Series("column", columns.astype(np.int32)),
            pl.lit(source.name).alias("region"),
            pl.lit(tile_x, dtype=pl.Int64).alias("tile_x"),
            pl.lit(tile_y, dtype=pl.Int64).alias("tile_y"),
        )
        yield samples.sort("block_geoid"), bounds


def read_tile_elevation(
    source: RegionSources,
    bounds: tuple[float, float, float, float],
    *,
    cell_size_m: float = CELL_SIZE_M,
    indexed: tuple[tuple[Path, ...], STRtree] | None = None,
    allow_empty: bool = False,
) -> tuple[np.ndarray, rasterio.Affine]:
    elevation_paths, elevation_index = indexed or _elevation_index(
        source.elevation, source.target_crs
    )
    matching = elevation_index.query(shapely.box(*bounds))
    if not len(matching):
        if allow_empty:
            return _read_elevation(
                (),
                bounds=bounds,
                target_crs=source.target_crs,
                cell_size_m=cell_size_m,
            )
        raise HouseHunterError(f"Mountain elevation does not cover region tile {bounds}")
    ordered = tuple(elevation_paths[index] for index in sorted(int(value) for value in matching))
    return _read_elevation(
        ordered,
        bounds=bounds,
        target_crs=source.target_crs,
        cell_size_m=cell_size_m,
    )


def read_tile_pad(
    source: RegionSources,
    bounds: tuple[float, float, float, float],
    *,
    shape: tuple[int, int],
    transform: rasterio.Affine,
) -> np.ndarray:
    geometry, fields = _read_geometries(
        source.pad_us,
        target_bounds=bounds,
        target_crs=source.target_crs,
        columns=[
            source.pad_access_field,
            *(() if source.pad_order_field is None else (source.pad_order_field,)),
        ],
        expected_type_ids=frozenset({3, 6}),
        layer=source.pad_layer,
        require_valid=False,
    )
    values = fields.get(source.pad_access_field, [])
    order = (
        np.argsort(fields[source.pad_order_field], kind="stable")
        if source.pad_order_field is not None
        else np.arange(len(geometry))
    )
    shapes = [
        (geometry[index], PAD_ACCESS_CODES.get(str(values[index]), 4))
        for index in order
        if geometry[index] is not None and not shapely.is_empty(geometry[index])
    ]
    return (
        rasterize(
            shapes,
            out_shape=shape,
            transform=transform,
            fill=0,
            dtype="uint8",
            all_touched=False,
            merge_alg=MergeAlg.replace,
        )
        if shapes
        else np.zeros(shape, dtype=np.uint8)
    )


def read_tile_trails(
    source: RegionSources,
    bounds: tuple[float, float, float, float],
    *,
    shape: tuple[int, int],
    transform: rasterio.Affine,
) -> np.ndarray:
    result = np.zeros(shape, dtype=np.float32)
    paths = source.trails if isinstance(source.trails, tuple) else (source.trails,)
    for path in paths:
        geometry, _ = _read_geometries(
            path,
            target_bounds=bounds,
            target_crs=source.target_crs,
            where=source.trail_where,
            expected_type_ids=frozenset({1, 5}),
            layer=source.trails_layer,
        )
        shapes = [(item, 1.0) for item in geometry if not shapely.is_empty(item)]
        if shapes:
            result += rasterize(
                shapes,
                out_shape=shape,
                transform=transform,
                fill=0,
                dtype="float32",
                all_touched=False,
                merge_alg=MergeAlg.add,
            )
    return result


def iter_region_tiles(
    source: RegionSources,
    *,
    state_by_fips: dict[str, str],
    cell_size_m: float = CELL_SIZE_M,
    tile_size_m: float = TILE_SIZE_M,
    skip_tiles: set[tuple[int, int]] | None = None,
) -> Iterator[tuple[pl.DataFrame, np.ndarray, np.ndarray, np.ndarray]]:
    """Yield the exact aligned source arrays and block samples used by v1."""
    indexed = _elevation_index(source.elevation, source.target_crs)
    for samples, bounds in iter_region_block_samples(
        source,
        state_by_fips=state_by_fips,
        cell_size_m=cell_size_m,
        tile_size_m=tile_size_m,
        skip_tiles=skip_tiles,
    ):
        elevation, transform = read_tile_elevation(
            source,
            bounds,
            cell_size_m=cell_size_m,
            indexed=indexed,
            allow_empty=int(samples["pop20"].sum()) == 0,
        )
        pad = read_tile_pad(source, bounds, shape=elevation.shape, transform=transform)
        trail_cells = read_tile_trails(source, bounds, shape=elevation.shape, transform=transform)
        yield samples, elevation.astype(np.float32, copy=False), pad, trail_cells


def _raw_metrics_from_tile(
    samples: pl.DataFrame,
    elevation: np.ndarray,
    pad: np.ndarray,
    trail_cells: np.ndarray,
    *,
    cell_size_m: float,
) -> pl.DataFrame:
    """Recompute and sample every v1 raw metric from one prepared logical tile."""
    if elevation.dtype != np.float32 or pad.dtype != np.uint8 or trail_cells.dtype != np.float32:
        raise HouseHunterError("Mountain prepared arrays have incompatible dtypes")
    if elevation.ndim != 2 or pad.shape != elevation.shape or trail_cells.shape != elevation.shape:
        raise HouseHunterError("Mountain prepared arrays are not aligned")
    rows = samples["row"].to_numpy().astype(np.int64, copy=False)
    columns = samples["column"].to_numpy().astype(np.int64, copy=False)
    if (
        np.any(rows < 0)
        or np.any(columns < 0)
        or np.any(rows >= elevation.shape[0])
        or np.any(columns >= elevation.shape[1])
    ):
        raise HouseHunterError("Mountain prepared block sample is outside its tile")
    if not np.isfinite(elevation).any():
        if samples["pop20"].sum() != 0:
            raise HouseHunterError("Mountain elevation grid contains no valid cells")
        return (
            samples.select("block_geoid", "tract_geoid", "county_fips", "state", "pop20")
            .with_columns(*(pl.lit(None).cast(pl.Float64).alias(name) for name in RAW_PRECISION))
            .sort("block_geoid")
        )
    terrain = terrain_metrics(elevation, cell_size_m=cell_size_m)
    access = access_metrics(
        terrain["mountain_mask"],
        pad,
        trail_cells * cell_size_m / 1_000,
        cell_size_m=cell_size_m,
    )
    sampled = {
        key: _sample(values, rows, columns)
        for key, values in {**terrain, **access}.items()
        if key in RAW_PRECISION
    }
    return (
        samples.select("block_geoid", "tract_geoid", "county_fips", "state", "pop20")
        .with_columns(
            *(pl.Series(name, values, nan_to_null=True) for name, values in sampled.items())
        )
        .sort("block_geoid")
    )
