from __future__ import annotations

import io
import json
import multiprocessing
import os
import platform
import re
import shutil
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyogrio
import pyproj
import rasterio
import scipy
import shapely

from .config import canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .mountain import PIPELINE_VERSION
from .mountain_gis import (
    CELL_SIZE_M,
    HALO_M,
    TILE_SIZE_M,
    RegionSources,
    _raw_metrics_from_tile,
    iter_region_tiles,
    load_source_lock_contract,
)
from .mountain_paths import (
    OWNERSHIP_MARKER,
    ensure_owned_child,
    ensure_safe_directory,
    remove_owned_child,
    require_owned_child,
)

PREPARED_PACK_SCHEMA_VERSION = 1
PREPARED_PACK_MAX_BYTES = 22_000_000_000
WORK_MAX_BYTES = 4_000_000_000
ENGINEERING_MAX_BYTES = 45_000_000_000
HARD_MAX_BYTES = 50_000_000_000
MIN_FREE_BYTES = 10_000_000_000
_PREPARATION_WORK = re.compile(r"^\.[0-9a-f]{32}\.work$")
_PACK_ID = re.compile(r"^[0-9a-f]{64}$")
_TILE_KEY = re.compile(r"^[0-9a-f]{24}$")


def allocated_size(path: Path) -> int:
    """Return allocated bytes without following symlinks."""
    if not path.exists() and not path.is_symlink():
        return 0
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        total += metadata.st_blocks * 512
        if current.is_dir() and not current.is_symlink():
            try:
                pending.extend(current.iterdir())
            except FileNotFoundError:
                continue
    return total


def ensure_storage_budget(managed_root: Path, *, reserve_bytes: int = 0) -> None:
    if reserve_bytes < 0:
        raise HouseHunterError("Mountain storage reservation cannot be negative")
    managed_root = ensure_safe_directory(managed_root)
    used = allocated_size(managed_root)
    if used + reserve_bytes > ENGINEERING_MAX_BYTES:
        raise HouseHunterError(
            f"Mountain storage would exceed the {ENGINEERING_MAX_BYTES:,}-byte engineering ceiling"
        )
    if used + reserve_bytes >= HARD_MAX_BYTES:
        raise HouseHunterError(f"Mountain storage would reach the {HARD_MAX_BYTES:,}-byte hard cap")
    probe = managed_root if managed_root.exists() else managed_root.parent
    probe.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(probe).free - reserve_bytes < MIN_FREE_BYTES:
        raise HouseHunterError(
            f"Mountain work must preserve {MIN_FREE_BYTES:,} bytes of unrelated free space"
        )


def block_geoid_sha256(frame: pl.DataFrame) -> str:
    values = frame.select("block_geoid").sort("block_geoid")["block_geoid"].to_list()
    return sha256_bytes(("\n".join(values) + "\n").encode())


def raw_metric_sha256(frame: pl.DataFrame) -> str:
    output = io.BytesIO()
    frame.sort("block_geoid").write_parquet(output, compression="uncompressed", statistics=False)
    return sha256_bytes(output.getvalue())


def _file_metadata(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "filename": path.name,
        "size": stat.st_size,
        "allocated_bytes": stat.st_blocks * 512,
        "sha256": sha256_file(path),
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _remove_owned_preparation(path: Path, parent: Path) -> None:
    remove_owned_child(path, parent, name_pattern=r"\.[0-9a-f]{32}\.work")


def _toolchain() -> dict[str, str]:
    return {
        "numpy": np.__version__,
        "python": platform.python_version(),
        "polars": pl.__version__,
        "pyogrio": pyogrio.__version__,
        "pyproj": pyproj.__version__,
        "rasterio": rasterio.__version__,
        "gdal": rasterio.__gdal_version__,
        "scipy": scipy.__version__,
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
    }


def _source_provenance_item(item: dict[str, Any]) -> dict[str, object]:
    result = {key: value for key, value in item.items() if key not in {"path", "archive", "url"}}
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
    return result


def _tile_key(region: str, tile_x: int, tile_y: int) -> str:
    return sha256_bytes(canonical_json([region, tile_x, tile_y]))[:24]


def _state_expectations(frames: list[pl.DataFrame]) -> dict[str, dict[str, int]]:
    blocks = pl.concat(frames)
    return {
        row["state"]: {"blocks": row["blocks"], "population": row["population"]}
        for row in blocks.group_by("state")
        .agg(pl.len().alias("blocks"), pl.col("pop20").sum().alias("population"))
        .sort("state")
        .iter_rows(named=True)
    }


def _resumable_tiles(
    workspace: Path, *, cell_size_m: float
) -> tuple[list[dict[str, object]], list[pl.DataFrame]]:
    entries: list[dict[str, object]] = []
    frames: list[pl.DataFrame] = []
    tile_root = workspace / "tiles"
    for child in tile_root.iterdir():
        if child.is_symlink() or not child.is_dir() or _TILE_KEY.fullmatch(child.name) is None:
            raise HouseHunterError("Mountain preparation contains an unsafe tile path")
        try:
            entry = _load_json(child / "tile.json", "preparation tile")
            if entry.get("key") != child.name:
                raise HouseHunterError("Mountain preparation tile identity differs")
            files = entry["files"]
            if not isinstance(files, dict) or set(files) != {
                "elevation",
                "pad",
                "trails",
                "blocks",
            }:
                raise HouseHunterError("Mountain preparation tile file set differs")
            resolved = {name: child / str(metadata["filename"]) for name, metadata in files.items()}
            if any(
                path.is_symlink() or not path.is_file() or _file_metadata(path) != files[name]
                for name, path in resolved.items()
            ):
                raise HouseHunterError("Mountain preparation tile checksum differs")
            elevation = np.load(resolved["elevation"], mmap_mode="r", allow_pickle=False)
            pad = np.load(resolved["pad"], mmap_mode="r", allow_pickle=False)
            trails = np.load(resolved["trails"], mmap_mode="r", allow_pickle=False)
            blocks = pl.read_parquet(resolved["blocks"])
            raw = _raw_metrics_from_tile(blocks, elevation, pad, trails, cell_size_m=cell_size_m)
            if raw_metric_sha256(raw) != entry.get("raw_metric_sha256"):
                raise HouseHunterError("Mountain preparation tile raw metrics differ")
        except (
            HouseHunterError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            pl.exceptions.PolarsError,
        ):
            shutil.rmtree(child)
            continue
        entries.append(entry)
        frames.append(blocks.select("block_geoid", "state", "pop20"))
    return entries, frames


def prepare_regions(
    regions: tuple[RegionSources, ...],
    destination: Path,
    *,
    state_by_fips: dict[str, str],
    source_lock_path: Path,
    region_config_path: Path,
    prepared_lock_path: Path | None = None,
    cell_size_m: float = CELL_SIZE_M,
    tile_size_m: float = TILE_SIZE_M,
    maximum_bytes: int = PREPARED_PACK_MAX_BYTES,
) -> tuple[Path, Path]:
    """Create one immutable, content-addressed pack from the exact serial tile inputs."""
    if cell_size_m <= 0 or tile_size_m <= 0:
        raise HouseHunterError("Mountain prepared-pack grid sizes must be positive")
    destination = ensure_safe_directory(destination)
    source_lock = _load_json(source_lock_path, "source lock")
    source_items = source_lock.get("sources")
    if not isinstance(source_items, list) or any(
        not isinstance(item, dict) for item in source_items
    ):
        raise HouseHunterError("Mountain source lock contains invalid provenance")
    source_digest = sha256_file(source_lock_path)
    config_digest = sha256_file(region_config_path)
    known_locks = (
        [prepared_lock_path]
        if prepared_lock_path is not None and prepared_lock_path.is_file()
        else sorted(destination.glob("*.lock.json"))
        if prepared_lock_path is None
        else []
    )
    for known_lock in known_locks:
        try:
            known = _load_json(known_lock, "prepared-pack lock")
            known_pack = destination / str(known["pack_id"])
            known_manifest = verify_prepared_pack(
                known_pack, known_lock, maximum_bytes=maximum_bytes
            )
            if (
                known_manifest.get("source_lock_sha256") == source_digest
                and known_manifest.get("region_config_sha256") == config_digest
                and known_manifest.get("grid", {}).get("cell_size_m") == cell_size_m
                and known_manifest.get("grid", {}).get("tile_size_m") == tile_size_m
            ):
                return known_pack, known_lock
        except (HouseHunterError, KeyError, OSError):
            if prepared_lock_path is not None:
                raise
    preparation_run: dict[str, object] = {
        "schema_version": PREPARED_PACK_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "source_lock_sha256": source_digest,
        "source_lock_schema_version": source_lock.get("schema_version"),
        "region_config_sha256": config_digest,
        "cell_size_m": cell_size_m,
        "tile_size_m": tile_size_m,
        "halo_m": HALO_M,
        "toolchain": _toolchain(),
    }
    preparation_id = sha256_bytes(canonical_json(preparation_run))[:32]
    temporary = destination / f".{preparation_id}.work"
    if temporary.exists():
        if (
            temporary.is_symlink()
            or not (temporary / OWNERSHIP_MARKER).is_file()
            or (temporary / OWNERSHIP_MARKER).is_symlink()
            or _load_json(temporary / "run.json", "preparation run") != preparation_run
        ):
            raise HouseHunterError("Mountain preparation workspace is incompatible or unowned")
    else:
        temporary = ensure_owned_child(
            temporary,
            destination,
            name_pattern=r"\.[0-9a-f]{32}\.work",
            marker_value="prepared-pack-v1\n",
        )
        _write_json(temporary / "run.json", preparation_run)
        (temporary / "tiles").mkdir()
    tile_root = temporary / "tiles"
    tile_entries, block_frames = _resumable_tiles(temporary, cell_size_m=cell_size_m)
    try:
        for region in regions:
            completed = {
                (int(entry["tile_x"]), int(entry["tile_y"]))
                for entry in tile_entries
                if entry["region"] == region.name
            }
            for samples, elevation, pad, trails in iter_region_tiles(
                region,
                state_by_fips=state_by_fips,
                cell_size_m=cell_size_m,
                tile_size_m=tile_size_m,
                skip_tiles=completed,
            ):
                tile_x = int(samples["tile_x"][0])
                tile_y = int(samples["tile_y"][0])
                core = [
                    tile_x * tile_size_m,
                    tile_y * tile_size_m,
                    (tile_x + 1) * tile_size_m,
                    (tile_y + 1) * tile_size_m,
                ]
                bounds = [
                    core[0] - HALO_M,
                    core[1] - HALO_M,
                    core[2] + HALO_M,
                    core[3] + HALO_M,
                ]
                key = _tile_key(region.name, tile_x, tile_y)
                tile_dir = tile_root / key
                if tile_dir.exists():
                    if tile_dir.is_symlink():
                        raise HouseHunterError("Mountain preparation tile path is unsafe")
                    shutil.rmtree(tile_dir)
                tile_dir.mkdir()
                paths = {
                    "elevation": tile_dir / "elevation.npy",
                    "pad": tile_dir / "pad.npy",
                    "trails": tile_dir / "trails.npy",
                    "blocks": tile_dir / "blocks.parquet",
                }
                np.save(paths["elevation"], elevation, allow_pickle=False)
                np.save(paths["pad"], pad, allow_pickle=False)
                np.save(paths["trails"], trails, allow_pickle=False)
                samples.sort("block_geoid").write_parquet(
                    paths["blocks"], compression="zstd", statistics=True
                )
                raw = _raw_metrics_from_tile(
                    samples, elevation, pad, trails, cell_size_m=cell_size_m
                )
                entry: dict[str, object] = {
                    "key": key,
                    "region": region.name,
                    "target_crs": region.target_crs,
                    "tile_x": tile_x,
                    "tile_y": tile_y,
                    "core_bounds": core,
                    "bounds": bounds,
                    "transform": [
                        cell_size_m,
                        0.0,
                        bounds[0],
                        0.0,
                        -cell_size_m,
                        bounds[3],
                    ],
                    "shape": list(elevation.shape),
                    "rows": samples.height,
                    "population": int(samples["pop20"].sum()),
                    "block_geoid_sha256": block_geoid_sha256(samples),
                    "raw_metric_sha256": raw_metric_sha256(raw),
                    "files": {name: _file_metadata(path) for name, path in paths.items()},
                }
                _write_json(tile_dir / "tile.json", entry)
                tile_entries.append(entry)
                block_frames.append(samples.select("block_geoid", "state", "pop20"))
                if allocated_size(temporary) > maximum_bytes:
                    raise HouseHunterError(
                        f"Mountain prepared pack exceeds its {maximum_bytes:,}-byte budget"
                    )
        if not tile_entries:
            raise HouseHunterError("Mountain prepared pack contains no tiles")
        tile_entries.sort(key=lambda item: (item["region"], item["tile_x"], item["tile_y"]))
        blocks = pl.concat(block_frames)
        if blocks["block_geoid"].n_unique() != blocks.height:
            raise HouseHunterError("Mountain prepared tiles do not form a disjoint block union")
        manifest: dict[str, object] = {
            "schema_version": PREPARED_PACK_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "source_lock_sha256": sha256_file(source_lock_path),
            "source_lock_schema_version": source_lock.get("schema_version"),
            "source_provenance": {
                "items": [_source_provenance_item(item) for item in source_items]
            },
            "region_config_sha256": sha256_file(region_config_path),
            "grid": {
                "cell_size_m": cell_size_m,
                "tile_size_m": tile_size_m,
                "halo_m": HALO_M,
                "lattice_origin": [0, 0],
                "block_assignment": "floor",
                "elevation_resampling": "bilinear",
                "pad_merge": "replace-source-order",
                "pad_all_touched": False,
                "pad_codes": {
                    "outside": 0,
                    "Open": 1,
                    "Restricted": 2,
                    "Closed": 3,
                    "Unknown": 4,
                    "unrecognized": 4,
                },
                "trail_merge": "add-touched-cell-count",
                "trail_cell_length_km": cell_size_m / 1_000,
            },
            "toolchain": _toolchain(),
            "block_count": blocks.height,
            "block_geoid_sha256": block_geoid_sha256(blocks),
            "state_expectations": _state_expectations(block_frames),
            "raw_metric_sha256": sha256_bytes(
                canonical_json([item["raw_metric_sha256"] for item in tile_entries])
            ),
            "tiles": tile_entries,
        }
        manifest["pack_id"] = sha256_bytes(canonical_json(manifest))
        manifest_path = temporary / "manifest.json"
        _write_json(manifest_path, manifest)
        pack_bytes = allocated_size(temporary)
        if source_lock.get("schema_version") == 2:
            inventory = source_lock["tile_inventory"]
            projection = source_lock["storage_projection"]
            actual_inventory = [
                {
                    "region": entry["region"],
                    "tile_x": entry["tile_x"],
                    "tile_y": entry["tile_y"],
                    "blocks": entry["rows"],
                    "population": entry["population"],
                }
                for entry in tile_entries
            ]
            if (
                inventory.get("tile_count") != len(tile_entries)
                or inventory.get("block_count") != blocks.height
                or inventory.get("tiles") != actual_inventory
                or source_lock.get("block_geoid_sha256") != manifest["block_geoid_sha256"]
                or source_lock.get("expected_states") != manifest["state_expectations"]
                or int(projection["prepared_pack_bytes"]) < pack_bytes
            ):
                raise HouseHunterError(
                    "Mountain prepared pack differs from the reviewed national projection"
                )
        if pack_bytes > maximum_bytes:
            raise HouseHunterError(
                f"Mountain prepared pack is {pack_bytes:,} bytes; maximum is {maximum_bytes:,}"
            )
        lock: dict[str, object] = {
            "schema_version": 1,
            "prepared_pack_schema_version": PREPARED_PACK_SCHEMA_VERSION,
            "pack_id": manifest["pack_id"],
            "manifest_sha256": sha256_file(manifest_path),
            "source_lock_sha256": manifest["source_lock_sha256"],
            "source_lock_schema_version": manifest["source_lock_schema_version"],
            "tile_count": len(tile_entries),
            "allocated_bytes": pack_bytes,
            "block_count": blocks.height,
            "block_geoid_sha256": manifest["block_geoid_sha256"],
            "raw_metric_sha256": manifest["raw_metric_sha256"],
        }
        final = destination / str(manifest["pack_id"])
        if final.exists():
            existing = final / "manifest.json"
            if not existing.is_file() or sha256_file(existing) != lock["manifest_sha256"]:
                raise HouseHunterError(f"Mountain prepared-pack identity collision: {final.name}")
            _remove_owned_preparation(temporary, destination)
        else:
            os.replace(temporary, final)
        lock_path = prepared_lock_path or destination / f"{final.name}.lock.json"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(lock_path, lock)
        verify_prepared_pack(final, lock_path, maximum_bytes=maximum_bytes)
        return final, lock_path
    except BaseException:
        raise


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HouseHunterError(f"Mountain {label} is invalid")
    return payload


def verify_prepared_pack(
    pack: Path,
    prepared_lock_path: Path,
    *,
    maximum_bytes: int = PREPARED_PACK_MAX_BYTES,
    reviewed_source_lock_path: Path | None = None,
    require_source_lock_v2: bool = False,
) -> dict[str, Any]:
    """Verify a pack against its separately reviewed lock without recomputing metrics."""
    pack = ensure_safe_directory(pack)
    if prepared_lock_path.is_symlink():
        raise HouseHunterError("Mountain prepared-pack lock cannot be a symlink")
    lock = _load_json(prepared_lock_path, "prepared-pack lock")
    manifest_path = pack / "manifest.json"
    if pack.is_symlink() or manifest_path.is_symlink():
        raise HouseHunterError("Mountain prepared pack cannot contain symlinked roots")
    manifest = _load_json(manifest_path, "prepared-pack manifest")
    if (
        lock.get("schema_version") != 1
        or lock.get("prepared_pack_schema_version") != PREPARED_PACK_SCHEMA_VERSION
        or manifest.get("schema_version") != PREPARED_PACK_SCHEMA_VERSION
        or manifest.get("pipeline_version") != PIPELINE_VERSION
        or manifest.get("toolchain") != _toolchain()
        or manifest.get("source_lock_schema_version") not in {1, 2}
    ):
        raise HouseHunterError("Mountain prepared pack is incompatible")
    if reviewed_source_lock_path is not None:
        reviewed = load_source_lock_contract(
            reviewed_source_lock_path, require_v2=require_source_lock_v2
        )
        if (
            sha256_file(reviewed_source_lock_path) != manifest.get("source_lock_sha256")
            or reviewed.get("schema_version") != manifest.get("source_lock_schema_version")
            or lock.get("source_lock_sha256") != manifest.get("source_lock_sha256")
            or lock.get("source_lock_schema_version") != manifest.get("source_lock_schema_version")
        ):
            raise HouseHunterError("Mountain prepared pack does not match the reviewed source lock")
    elif require_source_lock_v2:
        raise HouseHunterError("National prepared builds require --source-lock")
    grid = manifest.get("grid")
    if (
        not isinstance(grid, dict)
        or not isinstance(grid.get("cell_size_m"), (int, float))
        or not isinstance(grid.get("tile_size_m"), (int, float))
        or not isinstance(grid.get("trail_cell_length_km"), (int, float))
        or grid.get("halo_m") != HALO_M
        or grid.get("lattice_origin") != [0, 0]
        or grid.get("block_assignment") != "floor"
        or grid.get("elevation_resampling") != "bilinear"
        or grid.get("pad_merge") != "replace-source-order"
        or grid.get("pad_all_touched") is not False
        or grid.get("pad_codes")
        != {
            "outside": 0,
            "Open": 1,
            "Restricted": 2,
            "Closed": 3,
            "Unknown": 4,
            "unrecognized": 4,
        }
        or grid.get("trail_merge") != "add-touched-cell-count"
        or float(grid.get("trail_cell_length_km", -1)) != float(grid.get("cell_size_m", 0)) / 1_000
    ):
        raise HouseHunterError("Mountain prepared-pack grid semantics are incompatible")
    expected_run = {
        "schema_version": PREPARED_PACK_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "source_lock_sha256": manifest.get("source_lock_sha256"),
        "source_lock_schema_version": manifest.get("source_lock_schema_version"),
        "region_config_sha256": manifest.get("region_config_sha256"),
        "cell_size_m": grid.get("cell_size_m"),
        "tile_size_m": grid.get("tile_size_m"),
        "halo_m": grid.get("halo_m"),
        "toolchain": manifest.get("toolchain"),
    }
    if (
        not (pack / OWNERSHIP_MARKER).is_file()
        or (pack / OWNERSHIP_MARKER).is_symlink()
        or (pack / "run.json").is_symlink()
        or _load_json(pack / "run.json", "prepared-pack run") != expected_run
    ):
        raise HouseHunterError("Mountain prepared-pack ownership or run metadata is invalid")
    identity = {key: value for key, value in manifest.items() if key != "pack_id"}
    pack_id = sha256_bytes(canonical_json(identity))
    if (
        manifest.get("pack_id") != pack_id
        or lock.get("pack_id") != pack_id
        or _PACK_ID.fullmatch(pack.name) is None
        or pack.name != pack_id
        or lock.get("manifest_sha256") != sha256_file(manifest_path)
    ):
        raise HouseHunterError("Mountain prepared-pack identity does not match its lock")
    tiles = manifest.get("tiles")
    if not isinstance(tiles, list) or not tiles:
        raise HouseHunterError("Mountain prepared pack contains no tile inventory")
    tiles_root = pack / "tiles"
    if tiles_root.is_symlink() or not tiles_root.is_dir():
        raise HouseHunterError("Mountain prepared pack cannot use a symlinked tile root")
    if {child.name for child in pack.iterdir()} != {
        OWNERSHIP_MARKER,
        "manifest.json",
        "run.json",
        "tiles",
    }:
        raise HouseHunterError("Mountain prepared pack contains unexpected root entries")
    if tiles != sorted(tiles, key=lambda item: (item["region"], item["tile_x"], item["tile_y"])):
        raise HouseHunterError("Mountain prepared-pack tile inventory is not canonical")
    expected_tile_keys = {str(tile.get("key")) for tile in tiles if isinstance(tile, dict)}
    if {child.name for child in tiles_root.iterdir()} != expected_tile_keys:
        raise HouseHunterError("Mountain prepared pack contains unexpected tile entries")
    expected_shape = int(
        (float(manifest["grid"]["tile_size_m"]) + 2 * HALO_M)
        / float(manifest["grid"]["cell_size_m"])
    )
    block_frames: list[pl.DataFrame] = []
    for tile in tiles:
        if not isinstance(tile, dict) or _TILE_KEY.fullmatch(str(tile.get("key", ""))) is None:
            raise HouseHunterError("Mountain prepared pack contains an invalid tile entry")
        tile_dir = tiles_root / str(tile["key"])
        tile_x = int(tile["tile_x"])
        tile_y = int(tile["tile_y"])
        tile_size = float(manifest["grid"]["tile_size_m"])
        cell_size = float(manifest["grid"]["cell_size_m"])
        core = [
            tile_x * tile_size,
            tile_y * tile_size,
            (tile_x + 1) * tile_size,
            (tile_y + 1) * tile_size,
        ]
        bounds = [
            core[0] - HALO_M,
            core[1] - HALO_M,
            core[2] + HALO_M,
            core[3] + HALO_M,
        ]
        if (
            tile.get("core_bounds") != core
            or tile.get("bounds") != bounds
            or tile.get("transform") != [cell_size, 0.0, bounds[0], 0.0, -cell_size, bounds[3]]
        ):
            raise HouseHunterError("Mountain prepared tile grid is not lattice anchored")
        if tile_dir.is_symlink() or tile_dir.resolve().parent != tiles_root.resolve():
            raise HouseHunterError("Mountain prepared pack contains an unsafe tile directory")
        if (tile_dir / "tile.json").is_symlink() or _load_json(
            tile_dir / "tile.json", "prepared-pack tile"
        ) != tile:
            raise HouseHunterError("Mountain prepared tile metadata differs from its manifest")
        files = tile.get("files")
        if (
            not isinstance(files, dict)
            or set(files) != {"elevation", "pad", "trails", "blocks"}
            or any(not isinstance(metadata, dict) for metadata in files.values())
        ):
            raise HouseHunterError("Mountain prepared pack contains an invalid tile file set")
        expected_tile_files = {
            "tile.json",
            *(str(metadata.get("filename")) for metadata in files.values()),
        }
        if {child.name for child in tile_dir.iterdir()} != expected_tile_files:
            raise HouseHunterError("Mountain prepared tile contains unexpected files")
        resolved: dict[str, Path] = {}
        for name, metadata in files.items():
            if not isinstance(metadata, dict):
                raise HouseHunterError("Mountain prepared pack contains invalid file metadata")
            filename = str(metadata.get("filename", ""))
            if Path(filename).name != filename:
                raise HouseHunterError("Mountain prepared pack contains an invalid filename")
            file_path = tile_dir / filename
            if (
                not file_path.is_file()
                or file_path.is_symlink()
                or file_path.stat().st_size != metadata.get("size")
                or sha256_file(file_path) != metadata.get("sha256")
            ):
                raise HouseHunterError(
                    f"Mountain prepared tile file is corrupt: {tile['key']}/{filename}"
                )
            resolved[name] = file_path
        elevation = np.load(resolved["elevation"], mmap_mode="r", allow_pickle=False)
        pad = np.load(resolved["pad"], mmap_mode="r", allow_pickle=False)
        trails = np.load(resolved["trails"], mmap_mode="r", allow_pickle=False)
        if (
            elevation.dtype != np.float32
            or pad.dtype != np.uint8
            or trails.dtype != np.float32
            or list(elevation.shape) != tile.get("shape")
            or elevation.shape != pad.shape
            or elevation.shape != trails.shape
            or elevation.shape != (expected_shape, expected_shape)
        ):
            raise HouseHunterError("Mountain prepared tile arrays are incompatible")
        blocks = pl.read_parquet(resolved["blocks"])
        required = {
            "block_geoid",
            "tract_geoid",
            "county_fips",
            "state",
            "pop20",
            "row",
            "column",
            "region",
            "tile_x",
            "tile_y",
        }
        if not required <= set(blocks.columns) or blocks.height != tile.get("rows"):
            raise HouseHunterError("Mountain prepared tile block schema is incompatible")
        if (
            blocks["row"].dtype != pl.Int32
            or blocks["column"].dtype != pl.Int32
            or not blocks.schema["pop20"].is_integer()
            or blocks.select(pl.col("pop20").is_null().any()).item()
            or blocks.filter(
                ~pl.col("block_geoid").str.contains(r"^\d{15}$")
                | (pl.col("tract_geoid") != pl.col("block_geoid").str.slice(0, 11))
                | (pl.col("county_fips") != pl.col("block_geoid").str.slice(0, 5))
                | (pl.col("pop20") < 0)
                | (pl.col("state").str.len_chars() != 2)
                | (pl.col("row") < 0)
                | (pl.col("column") < 0)
                | (pl.col("row") >= expected_shape)
                | (pl.col("column") >= expected_shape)
                | (pl.col("region") != tile["region"])
                | (pl.col("tile_x") != tile["tile_x"])
                | (pl.col("tile_y") != tile["tile_y"])
            ).height
            or block_geoid_sha256(blocks) != tile.get("block_geoid_sha256")
            or any(
                STATE_BY_FIPS.get(geoid[:2]) != state
                for geoid, state in blocks.select("block_geoid", "state").iter_rows()
            )
        ):
            raise HouseHunterError("Mountain prepared tile block samples are invalid")
        block_frames.append(blocks.select("block_geoid", "state", "pop20"))
    blocks = pl.concat(block_frames)
    if (
        blocks.height != manifest.get("block_count")
        or blocks["block_geoid"].n_unique() != blocks.height
        or block_geoid_sha256(blocks) != manifest.get("block_geoid_sha256")
        or lock.get("block_count") != blocks.height
        or lock.get("block_geoid_sha256") != manifest.get("block_geoid_sha256")
        or lock.get("tile_count") != len(tiles)
        or lock.get("source_lock_sha256") != manifest.get("source_lock_sha256")
        or lock.get("source_lock_schema_version") != manifest.get("source_lock_schema_version")
        or lock.get("raw_metric_sha256") != manifest.get("raw_metric_sha256")
        or _state_expectations(block_frames) != manifest.get("state_expectations")
        or sha256_bytes(canonical_json([tile["raw_metric_sha256"] for tile in tiles]))
        != manifest.get("raw_metric_sha256")
    ):
        raise HouseHunterError("Mountain prepared tiles do not match the national block lock")
    observed_bytes = allocated_size(pack)
    if observed_bytes > maximum_bytes or observed_bytes > int(lock.get("allocated_bytes", -1)):
        raise HouseHunterError("Mountain prepared pack exceeds its locked storage envelope")
    return manifest


def _reservation_bytes(tile: dict[str, Any]) -> int:
    return max(2_000_000, int(tile["rows"]) * 1_024)


def _process_tile(task: dict[str, Any]) -> dict[str, object]:
    started = time.monotonic()
    pack = Path(task["pack"])
    work = Path(task["work"])
    tile = task["tile"]
    tile_dir = pack / "tiles" / tile["key"]
    elevation = np.load(tile_dir / tile["files"]["elevation"]["filename"], mmap_mode="r")
    pad = np.load(tile_dir / tile["files"]["pad"]["filename"], mmap_mode="r")
    trails = np.load(tile_dir / tile["files"]["trails"]["filename"], mmap_mode="r")
    blocks = pl.read_parquet(tile_dir / tile["files"]["blocks"]["filename"])
    raw = _raw_metrics_from_tile(
        blocks, elevation, pad, trails, cell_size_m=float(task["cell_size_m"])
    )
    logical_digest = raw_metric_sha256(raw)
    if logical_digest != tile["raw_metric_sha256"]:
        raise HouseHunterError(f"Mountain prepared tile changed raw metrics: {tile['key']}")
    shard = work / f"{tile['key']}.parquet"
    temporary = work / f".{tile['key']}.{uuid.uuid4().hex}.part"
    raw.write_parquet(temporary, compression="zstd", statistics=True)
    if allocated_size(temporary) > int(task["reservation_bytes"]):
        temporary.unlink(missing_ok=True)
        raise HouseHunterError(f"Mountain shard exceeded its reservation: {tile['key']}")
    os.replace(temporary, shard)
    metadata: dict[str, object] = {
        "schema_version": 1,
        "pack_id": task["pack_id"],
        "pipeline_version": PIPELINE_VERSION,
        "tile_key": tile["key"],
        "rows": raw.height,
        "raw_metric_sha256": logical_digest,
        "parquet_sha256": sha256_file(shard),
        "allocated_bytes": allocated_size(shard),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    _write_json(work / f"{tile['key']}.json", metadata)
    return metadata


def _valid_shard(work: Path, pack_id: str, tile: dict[str, Any]) -> bool:
    shard = work / f"{tile['key']}.parquet"
    sidecar = work / f"{tile['key']}.json"
    try:
        metadata = _load_json(sidecar, "work-shard metadata")
        return bool(
            metadata.get("schema_version") == 1
            and metadata.get("pack_id") == pack_id
            and metadata.get("pipeline_version") == PIPELINE_VERSION
            and metadata.get("tile_key") == tile["key"]
            and metadata.get("rows") == tile["rows"]
            and metadata.get("raw_metric_sha256") == tile["raw_metric_sha256"]
            and shard.is_file()
            and not shard.is_symlink()
            and metadata.get("parquet_sha256") == sha256_file(shard)
            and raw_metric_sha256(pl.read_parquet(shard)) == tile["raw_metric_sha256"]
        )
    except (HouseHunterError, OSError, pl.exceptions.PolarsError):
        return False


def _prepare_work_directory(work: Path, run: dict[str, object], *, resume: bool) -> None:
    if work.exists():
        work = require_owned_child(
            work,
            work.parent,
            name_pattern=r"(?:[0-9a-f]{64}|[0-9a-f]{32})",
        )
        if (work / "run.json").is_symlink():
            raise HouseHunterError(f"Mountain work directory is not pipeline-owned: {work}")
        existing = _load_json(work / "run.json", "work run")
        if existing != run:
            raise HouseHunterError("Mountain work directory belongs to incompatible inputs")
        if not resume:
            raise HouseHunterError("A fresh Mountain build requires a new empty work directory")
        for child in work.iterdir():
            if re.fullmatch(r"\.[0-9a-f]{24}(?:\.json)?\.[0-9a-f]{32}\.part", child.name):
                if child.is_symlink() or not child.is_file():
                    raise HouseHunterError("Mountain work directory contains an unsafe part file")
                child.unlink()
        return
    work = ensure_owned_child(
        work,
        work.parent,
        name_pattern=r"(?:[0-9a-f]{64}|[0-9a-f]{32})",
        marker_value="work-v1\n",
    )
    _write_json(work / "run.json", run)


def build_prepared_raw_metrics(
    pack: Path,
    prepared_lock_path: Path,
    work: Path,
    *,
    workers: int = 4,
    resume: bool = True,
    managed_root: Path | None = None,
    verified_manifest: dict[str, Any] | None = None,
) -> tuple[pl.DataFrame, dict[str, object]]:
    """Recompute raw metrics in a bounded persistent process pool and assemble canonically."""
    if not 1 <= workers <= 4:
        raise HouseHunterError("Mountain prepared builds require between one and four workers")
    started = time.monotonic()
    manifest = verified_manifest or verify_prepared_pack(pack, prepared_lock_path)
    verification_seconds = time.monotonic() - started
    run: dict[str, object] = {
        "schema_version": 1,
        "pack_id": manifest["pack_id"],
        "pipeline_version": PIPELINE_VERSION,
        "raw_metric_sha256": manifest["raw_metric_sha256"],
    }
    _prepare_work_directory(work, run, resume=resume)
    tiles: list[dict[str, Any]] = manifest["tiles"]
    allowed_work_entries = {OWNERSHIP_MARKER, "run.json"}
    for tile in tiles:
        allowed_work_entries.update({f"{tile['key']}.parquet", f"{tile['key']}.json"})
    if any(
        child.name not in allowed_work_entries or child.is_symlink() or not child.is_file()
        for child in work.iterdir()
    ):
        raise HouseHunterError("Mountain work directory contains unexpected or unsafe entries")
    completed = [tile for tile in tiles if _valid_shard(work, str(manifest["pack_id"]), tile)]
    pending = [tile for tile in tiles if tile not in completed]
    for tile in pending:
        (work / f"{tile['key']}.parquet").unlink(missing_ok=True)
        (work / f"{tile['key']}.json").unlink(missing_ok=True)
    if managed_root is not None:
        reserved = max(0, WORK_MAX_BYTES - allocated_size(work))
        ensure_storage_budget(managed_root, reserve_bytes=reserved)
    results: list[dict[str, object]] = []

    def task(tile: dict[str, Any]) -> dict[str, Any]:
        return {
            "pack": str(pack),
            "work": str(work),
            "pack_id": manifest["pack_id"],
            "cell_size_m": manifest["grid"]["cell_size_m"],
            "reservation_bytes": _reservation_bytes(tile),
            "tile": tile,
        }

    if workers == 1:
        for tile in pending:
            results.append(_process_tile(task(tile)))
            if allocated_size(work) > WORK_MAX_BYTES:
                raise HouseHunterError("Mountain work shards exceed their 4 GB budget")
    elif pending:
        iterator = iter(pending)
        executor = ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        )
        futures: dict[Any, int] = {}
        try:
            while len(futures) < workers * 2:
                tile = next(iterator, None)
                if tile is None:
                    break
                reservation = _reservation_bytes(tile)
                if allocated_size(work) + sum(futures.values()) + reservation > WORK_MAX_BYTES:
                    raise HouseHunterError("Mountain worker reservations exceed the 4 GB budget")
                future = executor.submit(_process_tile, task(tile))
                futures[future] = reservation
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future)
                    results.append(future.result())
                    if allocated_size(work) > WORK_MAX_BYTES:
                        raise HouseHunterError("Mountain work shards exceed their 4 GB budget")
                    tile = next(iterator, None)
                    if tile is not None:
                        reservation = _reservation_bytes(tile)
                        if (
                            allocated_size(work) + sum(futures.values()) + reservation
                            > WORK_MAX_BYTES
                        ):
                            raise HouseHunterError(
                                "Mountain worker reservations exceed the 4 GB budget"
                            )
                        submitted = executor.submit(_process_tile, task(tile))
                        futures[submitted] = reservation
        except BaseException:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
    shard_paths = [work / f"{tile['key']}.parquet" for tile in tiles]
    if not all(_valid_shard(work, str(manifest["pack_id"]), tile) for tile in tiles):
        raise HouseHunterError("Mountain prepared build did not produce every verified shard")
    frame = (
        pl.scan_parquet([str(path) for path in shard_paths])
        .sort("block_geoid")
        .collect(engine="streaming")
    )
    if (
        frame.height != manifest["block_count"]
        or block_geoid_sha256(frame) != manifest["block_geoid_sha256"]
    ):
        raise HouseHunterError("Mountain prepared shards do not match the pack block inventory")
    report: dict[str, object] = {
        "schema_version": 1,
        "pack_id": manifest["pack_id"],
        "workers": workers,
        "resumed_tiles": len(completed),
        "computed_tiles": len(pending),
        "tile_count": len(tiles),
        "block_count": frame.height,
        "verification_seconds": round(verification_seconds, 3),
        "metric_seconds": round(time.monotonic() - started - verification_seconds, 3),
        "work_allocated_bytes": allocated_size(work),
        "shards": sorted(results, key=lambda item: str(item["tile_key"])),
    }
    return frame, report


def remove_owned_work_directory(work: Path, managed_work_root: Path) -> None:
    """Remove only a direct, marked, non-symlink child of the managed work root."""
    remove_owned_child(
        work,
        managed_work_root,
        name_pattern=r"(?:[0-9a-f]{64}|[0-9a-f]{32})",
    )


def remove_owned_staging_directory(staging: Path, managed_staging_root: Path) -> None:
    """Remove a verified source staging directory only after pack publication succeeds."""
    remove_owned_child(staging, managed_staging_root, name_pattern=r"[0-9a-f]{16}")


def prune_owned_prepared_packs(prepared_root: Path, *, keep: Path) -> list[str]:
    """Retain one verified prepared pack and remove older pipeline-owned packs and locks."""
    root = ensure_safe_directory(prepared_root)
    kept = require_owned_child(keep, root, name_pattern=r"[0-9a-f]{64}")
    removed: list[str] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if child == kept or _PACK_ID.fullmatch(child.name) is None:
            continue
        pack_id = child.name
        remove_owned_child(child, root, name_pattern=r"[0-9a-f]{64}")
        lock_path = root / f"{pack_id}.lock.json"
        if lock_path.is_symlink():
            raise HouseHunterError("Mountain prepared-pack lock cannot be a symlink")
        if lock_path.is_file():
            lock = _load_json(lock_path, "prepared-pack lock")
            if lock.get("pack_id") != pack_id:
                raise HouseHunterError("Mountain prepared-pack lock identity differs")
            lock_path.unlink()
        removed.append(pack_id)
    return removed
