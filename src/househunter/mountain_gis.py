from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
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

from .config import sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .mountain import IN_SCOPE_STATES, RAW_PRECISION, access_metrics, terrain_metrics

CELL_SIZE_M = 250.0
TILE_SIZE_M = 100_000.0
HALO_M = 100_000.0
SOURCE_LOCK_SCHEMA_VERSION = 2
SOURCE_DOWNLOAD_MAX_BYTES = 50_000_000_000


@dataclass(frozen=True)
class RegionSources:
    name: str
    target_crs: str
    blocks: Path | tuple[Path, ...]
    elevation: tuple[Path, ...]
    pad_us: Path
    trails: Path
    blocks_layer: str | None = None
    pad_layer: str | None = None
    trails_layer: str | None = None
    pad_access_field: str = "Pub_Access"
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
                    trails=source_path(item["trails"]),
                    blocks_layer=item.get("blocks_layer"),
                    pad_layer=item.get("pad_layer"),
                    trails_layer=item.get("trails_layer"),
                    pad_access_field=str(item.get("pad_access_field", "Pub_Access")),
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
    storage = lock.get("storage_projection")
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
        or "PROJ" not in region_crs["hawaii"].upper()
    ):
        raise HouseHunterError("Mountain source lock contains incomplete region CRS definitions")
    try:
        if not CRS.from_user_input(region_crs["hawaii"]).is_projected:
            raise ValueError("Hawaii CRS is not projected")
    except Exception as exc:
        raise HouseHunterError("Mountain source lock contains an invalid Hawaii CRS WKT") from exc
    if (
        not isinstance(precedence, list)
        or not precedence
        or len(precedence) != len(set(precedence))
    ):
        raise HouseHunterError("Mountain source lock lacks ordered elevation precedence")
    try:
        inventory_tiles = inventory.get("tiles") if isinstance(inventory, dict) else None
        storage_values = (
            [
                int(storage[key])
                for key in (
                    "prepared_pack_bytes",
                    "active_and_rollback_release_bytes",
                    "candidate_release_bytes",
                    "work_shard_bytes",
                    "safety_reserve_bytes",
                )
            ]
            if isinstance(storage, dict)
            else []
        )
        if (
            not isinstance(inventory, dict)
            or not isinstance(inventory_tiles, list)
            or len(inventory_tiles) != int(inventory["tile_count"])
            or int(inventory["tile_count"]) <= 0
            or int(inventory["block_count"]) <= 0
            or not isinstance(storage, dict)
            or any(value < 0 for value in storage_values)
            or int(storage["prepared_pack_bytes"]) > 22_000_000_000
            or int(storage["active_and_rollback_release_bytes"]) > 8_000_000_000
            or int(storage["candidate_release_bytes"]) > 4_000_000_000
            or int(storage["work_shard_bytes"]) > 4_000_000_000
            or int(storage["safety_reserve_bytes"]) < 7_000_000_000
            or int(storage["managed_peak_bytes"]) > 45_000_000_000
            or int(storage["managed_peak_bytes"]) < sum(storage_values)
        ):
            raise ValueError("invalid projection")
        normalized_tiles = [
            {
                "region": str(tile["region"]),
                "tile_x": int(tile["tile_x"]),
                "tile_y": int(tile["tile_y"]),
                "blocks": int(tile["blocks"]),
                "population": int(tile["population"]),
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
        ):
            raise ValueError("invalid inventory")
    except (KeyError, TypeError, ValueError) as exc:
        raise HouseHunterError(
            "Mountain source lock lacks a qualified tile and storage projection"
        ) from exc
    families = {"blocks", "elevation", "pad_us", "trails"}
    observed_families = set()
    elevation_names = []
    source_names: list[str] = []
    source_filenames: list[str] = []
    for source in lock["sources"]:
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
                or int(source["size"]) <= 0
                or re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"])) is None
            ):
                raise ValueError("invalid metadata")
        except (KeyError, TypeError, ValueError) as exc:
            raise HouseHunterError("Mountain source lock contains invalid metadata") from exc
        if family == "elevation":
            elevation_names.append(source.get("name"))
        _validated_source_url(source.get("url"), allowed_hosts)
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
            _archive_root(source, Path.cwd())
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
    ):
        raise HouseHunterError("Mountain source lock lacks all required source families")
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
        _verify_gis_dataset(_source_path(source, root), source)


def verify_source_lock(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Validate all local files against a release lock before GIS processing."""
    try:
        lock = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
    _validate_source_lock_contract(lock)
    for source in lock["sources"]:
        if not isinstance(source, dict):
            raise HouseHunterError("Mountain source lock contains an invalid entry")
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


def verify_region_sources_locked(
    regions: tuple[RegionSources, ...], lock: dict[str, Any], *, root: Path
) -> None:
    locked = locked_source_paths(lock, root=root)
    consumed: set[Path] = set()
    for region in regions:
        block_paths = region.blocks if isinstance(region.blocks, tuple) else (region.blocks,)
        consumed.update(
            path.resolve()
            for path in (*block_paths, *region.elevation, region.pad_us, region.trails)
        )
    missing = sorted(str(path) for path in consumed - locked)
    if missing:
        raise HouseHunterError(
            "Mountain region uses files absent from the source lock: " + ", ".join(missing)
        )
    if lock.get("schema_version") == 2:
        region_crs = lock["region_crs"]
        by_path: dict[Path, tuple[str, str, str | None]] = {}
        for source in lock["sources"]:
            source_path = _source_path(source, root).resolve()
            if source.get("archive") is None:
                by_path[source_path] = (
                    source["name"],
                    source["family"],
                    source.get("layer"),
                )
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
            roles = [
                *((path.resolve(), "blocks", region.blocks_layer) for path in block_paths),
                *((path.resolve(), "elevation", None) for path in region.elevation),
                (region.pad_us.resolve(), "pad_us", region.pad_layer),
                (region.trails.resolve(), "trails", region.trails_layer),
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


def _source_download_reservation(lock: dict[str, Any], destination: Path) -> int:
    reserve_bytes = 0
    for source in lock["sources"]:
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
        reserve_bytes += extract_bytes + (0 if valid else int(source["size"]))
    return reserve_bytes


def download_sources(
    lock_path: Path,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    managed_root: Path | None = None,
) -> Path:
    """Download exactly locked source files and publish each only after verification."""
    try:
        lock = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
    allowed_hosts = _validate_source_lock_contract(lock)
    if lock.get("schema_version") != SOURCE_LOCK_SCHEMA_VERSION:
        raise HouseHunterError("Mountain downloads require reviewed source-lock v2")
    try:
        projected_bytes = sum(
            int(source["size"]) + int(source.get("archive", {}).get("total_uncompressed_size", 0))
            for source in lock["sources"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HouseHunterError(f"Mountain source lock contains an invalid size: {exc}") from exc
    if projected_bytes > SOURCE_DOWNLOAD_MAX_BYTES:
        raise HouseHunterError("Mountain locked downloads exceed the 50 GB managed-data cap")
    destination.mkdir(parents=True, exist_ok=True)
    if managed_root is not None:
        from .mountain_pack import ensure_storage_budget

        ensure_storage_budget(
            managed_root, reserve_bytes=_source_download_reservation(lock, destination)
        )
    owns_client = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(120, connect=30), follow_redirects=False, trust_env=False
    )
    try:
        for source in lock["sources"]:
            if not isinstance(source, dict):
                raise HouseHunterError("Mountain source lock contains an invalid entry")
            target = _source_path(source, destination)
            if target.is_file():
                try:
                    if (
                        target.stat().st_size == int(source["size"])
                        and sha256_file(target) == source["sha256"]
                    ):
                        if source.get("archive") is not None:
                            extract_locked_archive(source, target, destination)
                        continue
                except (KeyError, TypeError, ValueError):
                    pass
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
            try:
                expected_size = int(source["size"])
                url = str(source["url"])
                redirects = 0
                while True:
                    _validated_source_url(url, allowed_hosts)
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
    verify_source_lock(lock_path, root=destination)
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


def _read_geometries(
    path: Path,
    *,
    target_bounds: tuple[float, float, float, float],
    target_crs: str,
    columns: list[str] | None = None,
    where: str | None = None,
    expected_type_ids: frozenset[int] | None = None,
    layer: str | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    try:
        info = pyogrio.read_info(path, layer=layer)
        source_crs = info.get("crs")
        if not source_crs:
            raise HouseHunterError(f"Mountain vector source has no CRS: {path}")
        bbox = transform_bounds(target_crs, source_crs, *target_bounds, densify_pts=21)
        metadata, _, geometry, arrays = ogr_raw.read(
            path,
            layer=layer,
            columns=columns,
            bbox=bbox,
            where=where,
            force_2d=True,
        )
        if geometry is None:
            return np.array([], dtype=object), {}
        values = shapely.from_wkb(geometry)
        if source_crs != target_crs:
            transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
            values = shapely.transform(values, transformer.transform, interleaved=False)
        present = ~shapely.is_missing(values) & ~shapely.is_empty(values)
        if np.any(present & ~shapely.is_valid(values)):
            raise HouseHunterError(f"Mountain vector source has invalid geometry: {path}")
        if expected_type_ids is not None:
            type_ids = shapely.get_type_id(values)
            if np.any(present & ~np.isin(type_ids, list(expected_type_ids))):
                raise HouseHunterError(
                    f"Mountain vector source has unexpected geometry type: {path}"
                )
        return values, dict(zip(metadata["fields"], arrays, strict=True))
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
        tiles.extend(
            {
                "region": region.name,
                "tile_x": row["tile_x"],
                "tile_y": row["tile_y"],
                "blocks": row["blocks"],
                "population": row["population"],
            }
            for row in blocks.group_by("tile_x", "tile_y")
            .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
            .iter_rows(named=True)
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
    peak_bytes = prepared_bytes + active_rollback_bytes + release_bytes + work_bytes + safety_bytes
    projection = {
        "prepared_pack_bytes": prepared_bytes,
        "active_and_rollback_release_bytes": active_rollback_bytes,
        "candidate_release_bytes": release_bytes,
        "work_shard_bytes": work_bytes,
        "safety_reserve_bytes": safety_bytes,
        "managed_peak_bytes": peak_bytes,
    }
    if (
        prepared_bytes > 22_000_000_000
        or active_rollback_bytes > 8_000_000_000
        or release_bytes > 4_000_000_000
        or work_bytes > 4_000_000_000
        or peak_bytes > 45_000_000_000
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


def iter_region_tiles(
    source: RegionSources,
    *,
    state_by_fips: dict[str, str],
    cell_size_m: float = CELL_SIZE_M,
    tile_size_m: float = TILE_SIZE_M,
    skip_tiles: set[tuple[int, int]] | None = None,
) -> Iterator[tuple[pl.DataFrame, np.ndarray, np.ndarray, np.ndarray]]:
    """Yield the exact aligned source arrays and block samples used by v1."""
    blocks = _blocks(source)
    elevation_paths, elevation_index = _elevation_index(source.elevation, source.target_crs)
    blocks = blocks.with_columns(
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
        matching = elevation_index.query(shapely.box(*bounds))
        if not len(matching):
            raise HouseHunterError(f"Mountain elevation does not cover region tile {core}")
        ordered_elevation = tuple(
            elevation_paths[index] for index in sorted(int(value) for value in matching)
        )
        elevation, transform = _read_elevation(
            ordered_elevation,
            bounds=bounds,
            target_crs=source.target_crs,
            cell_size_m=cell_size_m,
        )
        pad_geometry, pad_fields = _read_geometries(
            source.pad_us,
            target_bounds=bounds,
            target_crs=source.target_crs,
            columns=[source.pad_access_field],
            expected_type_ids=frozenset({3, 6}),
            layer=source.pad_layer,
        )
        access_codes = {"Open": 1, "Restricted": 2, "Closed": 3, "Unknown": 4}
        pad_shapes = [
            (geometry, access_codes.get(str(value), 4))
            for geometry, value in zip(
                pad_geometry, pad_fields.get(source.pad_access_field, []), strict=True
            )
            if geometry is not None and not shapely.is_empty(geometry)
        ]
        pad = (
            rasterize(
                pad_shapes,
                out_shape=elevation.shape,
                transform=transform,
                fill=0,
                dtype="uint8",
                all_touched=False,
                merge_alg=MergeAlg.replace,
            )
            if pad_shapes
            else np.zeros(elevation.shape, dtype=np.uint8)
        )
        trail_geometry, _ = _read_geometries(
            source.trails,
            target_bounds=bounds,
            target_crs=source.target_crs,
            where=source.trail_where,
            expected_type_ids=frozenset({1, 5}),
            layer=source.trails_layer,
        )
        trail_shapes = [
            (geometry, 1.0) for geometry in trail_geometry if not shapely.is_empty(geometry)
        ]
        trail_cells = (
            rasterize(
                trail_shapes,
                out_shape=elevation.shape,
                transform=transform,
                fill=0,
                dtype="float32",
                all_touched=False,
                merge_alg=MergeAlg.add,
            )
            if trail_shapes
            else np.zeros(elevation.shape, dtype=np.float32)
        )
        columns = np.floor((tile["x"].to_numpy() - bounds[0]) / cell_size_m).astype(int)
        rows = np.floor((bounds[3] - tile["y"].to_numpy()) / cell_size_m).astype(int)
        if (
            np.any(rows < 0)
            or np.any(columns < 0)
            or np.any(rows >= elevation.shape[0])
            or np.any(columns >= elevation.shape[1])
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
