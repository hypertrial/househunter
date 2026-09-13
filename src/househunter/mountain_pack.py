from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import platform
import re
import shutil
import sqlite3
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import replace
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
from .mountain import COMPACT_RELEASE_MAX_BYTES, FULL_RELEASE_MAX_BYTES, PIPELINE_VERSION
from .mountain_gis import (
    CELL_SIZE_M,
    HALO_M,
    PAD_ACCESS_CODES,
    SOURCE_LOCK_SCHEMA_VERSION,
    TILE_SIZE_M,
    RegionSources,
    _elevation_index,
    _query_bounds,
    _raw_metrics_from_tile,
    delete_managed_source_family,
    delete_managed_sources,
    iter_region_block_samples,
    iter_region_tiles,
    load_source_lock_contract,
    preparation_batch,
    read_tile_elevation,
    read_tile_pad,
    read_tile_trails,
    source_provenance_item,
    verify_source_families,
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
TRAIL_FRAGMENT_MODE = "state_clipped_globalid_v1"


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


def _remaining_preparation_reservation(projected_bytes: int, work: Path) -> int:
    return max(0, projected_bytes - allocated_size(work))


def prepared_build_reservation(work: Path) -> int:
    """Return the aggregate remaining allocation for a promoted prepared build."""
    return (
        max(0, WORK_MAX_BYTES - allocated_size(work))
        + FULL_RELEASE_MAX_BYTES
        + COMPACT_RELEASE_MAX_BYTES
    )


def block_geoid_sha256(frame: pl.DataFrame) -> str:
    values = frame.select("block_geoid").sort("block_geoid")["block_geoid"].to_list()
    return sha256_bytes(("\n".join(values) + "\n").encode())


def block_sample_sha256(frame: pl.DataFrame) -> str:
    rows = frame.sort("block_geoid").select(
        "block_geoid", "pop20", "region", "tile_x", "tile_y", "row", "column"
    )
    return sha256_bytes(canonical_json([list(row) for row in rows.iter_rows()]))


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


def _save_array(path: Path, array: np.ndarray) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with temporary.open("wb") as handle:
            np.save(handle, array, allow_pickle=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _phase_path(workspace: Path, name: str) -> Path:
    return workspace / "checkpoints" / f"{name}.json"


def _valid_phase(
    workspace: Path,
    name: str,
    *,
    dependency_sha256: str | None = None,
    verified_source_sha256: str | None = None,
) -> dict[str, Any] | None:
    path = _phase_path(workspace, name)
    try:
        checkpoint = _load_json(path, f"{name} preparation checkpoint")
        if (
            checkpoint.get("schema_version") != 1
            or checkpoint.get("phase") != name
            or checkpoint.get("dependency_sha256") != dependency_sha256
            or checkpoint.get("verified_source_sha256") != verified_source_sha256
            or not isinstance(checkpoint.get("files"), dict)
        ):
            return None
        for key, metadata in checkpoint["files"].items():
            if _TILE_KEY.fullmatch(str(key)) is None or not isinstance(metadata, dict):
                return None
            file_path = workspace / "tiles" / str(key) / str(metadata.get("filename", ""))
            if (
                file_path.is_symlink()
                or not file_path.is_file()
                or _file_metadata(file_path) != metadata
            ):
                return None
        return checkpoint
    except (HouseHunterError, OSError, TypeError, ValueError):
        return None


def _write_phase(
    workspace: Path,
    name: str,
    files: dict[str, dict[str, object]],
    *,
    dependency_sha256: str | None = None,
    verified_source_sha256: str | None = None,
    entries: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "phase": name,
        "dependency_sha256": dependency_sha256,
        "verified_source_sha256": verified_source_sha256,
        "files": files,
    }
    if entries is not None:
        payload["entries"] = entries
    path = _phase_path(workspace, name)
    _write_json(path, payload)
    return payload


def _phase_digest(workspace: Path, name: str) -> str:
    return sha256_file(_phase_path(workspace, name))


def _source_family_digest(source_lock: dict[str, Any], family: str) -> str:
    sources = [
        {"name": item["name"], "sha256": item["sha256"]}
        for item in source_lock["sources"]
        if item.get("family") == family
    ]
    if not sources:
        raise HouseHunterError(f"Mountain source lock has no {family} sources")
    return sha256_bytes(canonical_json(sources))


def _source_names_digest(source_lock: dict[str, Any], source_names: set[str]) -> str:
    sources = [
        {"name": item["name"], "sha256": item["sha256"]}
        for item in source_lock["sources"]
        if item.get("name") in source_names
    ]
    if len(sources) != len(source_names):
        raise HouseHunterError("Mountain preparation batch source set is not locked")
    return sha256_bytes(canonical_json(sources))


def _complete_batch_sources(
    workspace: Path,
    checkpoint_name: str,
    *,
    source_lock: dict[str, Any],
    source_names: set[str],
    source_root: Path | None,
    managed_staging_root: Path | None,
) -> None:
    if source_root is None:
        return
    path = _phase_path(workspace, checkpoint_name)
    checkpoint = _load_json(path, f"{checkpoint_name} preparation checkpoint")
    if checkpoint.get("verified_source_sha256") != _source_names_digest(source_lock, source_names):
        raise HouseHunterError("Mountain batch is not bound to verified source bytes")
    managed = managed_staging_root is not None
    checkpoint["deletion_authorized"] = managed
    _write_json(path, checkpoint)
    if managed and not checkpoint.get("sources_deleted"):
        delete_managed_sources(
            source_lock,
            source_names=source_names,
            root=source_root,
            staging_root=managed_staging_root,
        )
        checkpoint["sources_deleted"] = True
        _write_json(path, checkpoint)


def _valid_elevation_batch_phase(
    workspace: Path,
    batch: dict[str, Any],
    *,
    entries: list[dict[str, object]],
    blocks_digest: str,
    source_lock: dict[str, Any],
) -> dict[str, Any] | None:
    source_names = {str(name) for name in batch["sources"]}
    phase = _valid_phase(
        workspace,
        f"elevation--{batch['id']}",
        dependency_sha256=blocks_digest,
        verified_source_sha256=_source_names_digest(source_lock, source_names),
    )
    requested = {str(value) for value in batch["tile_keys"]}
    expected = {
        str(entry["key"])
        for entry in entries
        if f"{entry['region']}:{entry['tile_x']}:{entry['tile_y']}" in requested
    }
    if phase is None or set(phase["files"]) != expected:
        return None
    return phase


def _sweep_releasable_elevation_sources(
    workspace: Path,
    checkpoint_name: str,
    *,
    completed_batch: dict[str, Any],
    entries: list[dict[str, object]],
    blocks_digest: str,
    source_lock: dict[str, Any],
    source_root: Path | None,
    managed_staging_root: Path | None,
) -> None:
    if source_root is None:
        return
    checkpoint_path = _phase_path(workspace, checkpoint_name)
    checkpoint = _load_json(checkpoint_path, f"{checkpoint_name} preparation checkpoint")
    managed = managed_staging_root is not None
    checkpoint["deletion_authorized"] = managed
    checkpoint.pop("sources_deleted", None)
    _write_json(checkpoint_path, checkpoint)
    if not managed:
        return
    batches = [
        batch
        for batch in source_lock.get("preparation_batches", [])
        if batch.get("family") == "elevation"
    ]
    if not batches:
        batches = [completed_batch]
    completed = {
        str(batch["id"])
        for batch in batches
        if _valid_elevation_batch_phase(
            workspace,
            batch,
            entries=entries,
            blocks_digest=blocks_digest,
            source_lock=source_lock,
        )
        is not None
    }
    dependents: dict[str, set[str]] = {}
    for batch in batches:
        for name in batch["sources"]:
            dependents.setdefault(str(name), set()).add(str(batch["id"]))
    releasable = {
        name for name, batch_ids in dependents.items() if batch_ids <= completed
    }
    if releasable:
        delete_managed_sources(
            source_lock,
            source_names=releasable,
            root=source_root,
            staging_root=managed_staging_root,
        )


def _verify_phase_sources_before_read(
    *,
    family: str,
    source_lock_path: Path,
    source_lock: dict[str, Any],
    source_root: Path | None,
) -> str:
    digest = _source_family_digest(source_lock, family)
    if source_root is None:
        raise HouseHunterError("Mountain source-lock v2 preparation requires a source root")
    verify_source_families(source_lock_path, root=source_root, families={family})
    return digest


def _complete_phase_sources(
    workspace: Path,
    name: str,
    *,
    family: str,
    source_lock_path: Path,
    source_lock: dict[str, Any],
    source_root: Path | None,
    managed_staging_root: Path | None,
) -> None:
    if source_root is None:
        return
    path = _phase_path(workspace, name)
    checkpoint = _load_json(path, f"{name} preparation checkpoint")
    source_digest = _source_family_digest(source_lock, family)
    managed = managed_staging_root is not None
    if checkpoint.get("verified_source_sha256") != source_digest:
        raise HouseHunterError(f"Mountain {family} phase is not bound to verified source bytes")
    checkpoint["deletion_authorized"] = managed
    _write_json(path, checkpoint)
    if managed and not checkpoint.get("sources_deleted"):
        delete_managed_source_family(
            source_lock,
            family=family,
            root=source_root,
            staging_root=managed_staging_root,
        )
        checkpoint["sources_deleted"] = True
        _write_json(path, checkpoint)


def _clear_phase_files(workspace: Path, filename: str) -> None:
    for tile in (workspace / "tiles").iterdir():
        if tile.is_dir() and not tile.is_symlink() and _TILE_KEY.fullmatch(tile.name):
            (tile / filename).unlink(missing_ok=True)


def _import_compatible_blocks_checkpoint(
    destination: Path,
    workspace: Path,
    source_lock: dict[str, Any],
) -> bool:
    """Reuse only a fully verified block-family checkpoint across unrelated lock completion."""
    (workspace / "checkpoints").mkdir(exist_ok=True)
    tile_root = workspace / "tiles"
    for child in list(tile_root.iterdir()):
        if child.is_symlink() or not child.is_dir() or _TILE_KEY.fullmatch(child.name) is None:
            raise HouseHunterError("Mountain block checkpoint import contains an unsafe tile path")
        shutil.rmtree(child)
    source_digest = _source_family_digest(source_lock, "blocks")
    expected = [
        (
            str(tile["region"]),
            int(tile["tile_x"]),
            int(tile["tile_y"]),
            int(tile["blocks"]),
            int(tile["population"]),
            str(tile["block_geoid_sha256"]),
            str(tile["block_sample_sha256"]),
        )
        for tile in source_lock["tile_inventory"]["tiles"]
    ]
    for candidate in sorted(destination.iterdir()):
        if candidate == workspace or _PREPARATION_WORK.fullmatch(candidate.name) is None:
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        checkpoint = _valid_phase(
            candidate,
            "blocks",
            verified_source_sha256=source_digest,
        )
        entries = checkpoint.get("entries") if checkpoint is not None else None
        if not isinstance(entries, list):
            continue
        observed = [
            (
                str(entry["region"]),
                int(entry["tile_x"]),
                int(entry["tile_y"]),
                int(entry["rows"]),
                int(entry["population"]),
                str(entry["block_geoid_sha256"]),
                str(entry["block_sample_sha256"]),
            )
            for entry in entries
        ]
        if observed != expected:
            continue
        files: dict[str, dict[str, object]] = {}
        for entry in entries:
            key = str(entry["key"])
            source = candidate / "tiles" / key / "blocks.parquet"
            tile = workspace / "tiles" / key
            tile.mkdir(exist_ok=False)
            target = tile / "blocks.parquet"
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
            files[key] = _file_metadata(target)
        _write_phase(
            workspace,
            "blocks",
            files,
            verified_source_sha256=source_digest,
            entries=entries,
        )
        return True
    return False


def _import_compatible_trail_ingestion(
    destination: Path,
    workspace: Path,
    source_lock: dict[str, Any],
) -> bool:
    batches = [
        batch
        for batch in source_lock.get("preparation_batches", [])
        if batch.get("family") == "trails"
    ]
    if not batches or _trail_fragment_store(workspace).exists():
        return False
    for candidate in sorted(destination.iterdir()):
        store = _trail_fragment_store(candidate)
        if (
            candidate == workspace
            or _PREPARATION_WORK.fullmatch(candidate.name) is None
            or candidate.is_symlink()
            or not store.is_file()
            or store.is_symlink()
        ):
            continue
        completed: list[tuple[dict[str, Any], str]] = []
        for batch in batches:
            names = {str(value) for value in batch["sources"]}
            source_digest = _source_names_digest(source_lock, names)
            if (
                _valid_phase(
                    candidate,
                    f"trails--{batch['id']}",
                    dependency_sha256=_source_family_digest(source_lock, "trails"),
                    verified_source_sha256=source_digest,
                )
                is None
            ):
                break
            completed.append((batch, source_digest))
        if not completed:
            continue
        connection = _open_trail_fragment_store(candidate)
        try:
            stored = dict(connection.execute("SELECT batch_id, source_sha256 FROM ingested_batch"))
        finally:
            connection.close()
        if stored != {str(batch["id"]): digest for batch, digest in completed}:
            continue
        shutil.copy2(store, _trail_fragment_store(workspace))
        for batch, _ in completed:
            shutil.copy2(
                _phase_path(candidate, f"trails--{batch['id']}"),
                _phase_path(workspace, f"trails--{batch['id']}"),
            )
        return True
    return False


def _write_reference_shard(
    comparison: Path,
    tile_key: str,
    raw: pl.DataFrame,
) -> dict[str, object]:
    shard = comparison / f"{tile_key}.parquet"
    sidecar = comparison / f"{tile_key}.json"
    expected_raw = raw_metric_sha256(raw)
    try:
        metadata = _load_json(sidecar, "comparison-shard metadata")
        if (
            metadata.get("raw_metric_sha256") == expected_raw
            and metadata.get("rows") == raw.height
            and shard.is_file()
            and not shard.is_symlink()
            and metadata.get("parquet_sha256") == sha256_file(shard)
            and raw_metric_sha256(pl.read_parquet(shard)) == expected_raw
        ):
            return metadata
    except (HouseHunterError, OSError, pl.exceptions.PolarsError):
        pass
    temporary = shard.with_name(f".{shard.name}.{uuid.uuid4().hex}.part")
    raw.write_parquet(temporary, compression="zstd", statistics=True)
    os.replace(temporary, shard)
    metadata = {
        "filename": shard.name,
        "rows": raw.height,
        "raw_metric_sha256": expected_raw,
        "parquet_sha256": sha256_file(shard),
        "allocated_bytes": allocated_size(shard),
    }
    _write_json(sidecar, metadata)
    return metadata


def _materialize_elevation_batch(
    regions: tuple[RegionSources, ...],
    entries: list[dict[str, object]],
    workspace: Path,
    *,
    batch: dict[str, Any],
    blocks_digest: str,
    cell_size_m: float,
    maximum_bytes: int,
    source_lock_path: Path,
    source_lock: dict[str, Any],
    source_root: Path | None,
    managed_staging_root: Path | None,
) -> dict[str, Any]:
    source_names = {str(name) for name in batch["sources"]}
    source_digest = _source_names_digest(source_lock, source_names)
    checkpoint_name = f"elevation--{batch['id']}"
    requested = set(str(value) for value in batch["tile_keys"])
    selected = [
        entry
        for entry in entries
        if f"{entry['region']}:{entry['tile_x']}:{entry['tile_y']}" in requested
    ]
    valid = _valid_elevation_batch_phase(
        workspace,
        batch,
        entries=entries,
        blocks_digest=blocks_digest,
        source_lock=source_lock,
    )
    ordered_batches = [
        item
        for item in source_lock.get("preparation_batches", [])
        if item.get("family") == "elevation"
    ]
    if not ordered_batches:
        ordered_batches = [batch]
    batch_phases = [
        _valid_elevation_batch_phase(
            workspace,
            item,
            entries=entries,
            blocks_digest=blocks_digest,
            source_lock=source_lock,
        )
        for item in ordered_batches
    ]
    completed_prefix = next(
        (index for index, phase in enumerate(batch_phases) if phase is None),
        len(batch_phases),
    )
    if any(phase is not None for phase in batch_phases[completed_prefix:]):
        raise HouseHunterError("Mountain elevation batch checkpoints are not in canonical order")
    if valid is None:
        if completed_prefix >= len(ordered_batches) or ordered_batches[completed_prefix] != batch:
            raise HouseHunterError(
                "Mountain elevation batches must be completed in canonical order"
            )
        if source_names:
            if source_root is None:
                raise HouseHunterError(
                    "Mountain batched elevation preparation requires a source root"
                )
            verify_source_families(
                source_lock_path,
                root=source_root,
                families={"elevation"},
                source_names=source_names,
            )
        filenames = {
            str(source["filename"])
            for source in source_lock["sources"]
            if source.get("name") in source_names
        }
        filtered_regions = {
            region.name: replace(
                region,
                elevation=tuple(path for path in region.elevation if path.name in filenames),
            )
            for region in regions
        }
        observed = {path.name for region in filtered_regions.values() for path in region.elevation}
        if observed != filenames:
            raise HouseHunterError(
                "Mountain elevation batch differs from the region source contract"
            )
        indexes = {
            name: _elevation_index(region.elevation, region.target_crs)
            for name, region in filtered_regions.items()
            if region.elevation
        }
        files: dict[str, dict[str, object]] = {}
        for entry in selected:
            region = filtered_regions[str(entry["region"])]
            shape = tuple(int(value) for value in entry["shape"])
            if region.elevation:
                array, _ = read_tile_elevation(
                    region,
                    tuple(float(value) for value in entry["bounds"]),
                    cell_size_m=cell_size_m,
                    indexed=indexes[region.name],
                )
            else:
                array = np.full(shape, np.nan, dtype=np.float32)
            path = workspace / "tiles" / str(entry["key"]) / "elevation.npy"
            _save_array(path, array.astype(np.float32, copy=False))
            files[str(entry["key"])] = _file_metadata(path)
            if allocated_size(workspace) > maximum_bytes:
                raise HouseHunterError(
                    f"Mountain prepared pack exceeds its {maximum_bytes:,}-byte budget"
                )
        valid = _write_phase(
            workspace,
            checkpoint_name,
            files,
            dependency_sha256=blocks_digest,
            verified_source_sha256=source_digest,
        )
    _sweep_releasable_elevation_sources(
        workspace,
        checkpoint_name,
        completed_batch=batch,
        entries=entries,
        blocks_digest=blocks_digest,
        source_lock=source_lock,
        source_root=source_root,
        managed_staging_root=managed_staging_root,
    )
    return valid


def _metadata_matches(path: Path, metadata: dict[str, object]) -> bool:
    try:
        observed = _file_metadata(path)
    except OSError:
        return False
    return all(observed.get(key) == metadata.get(key) for key in ("size", "sha256"))


def _trail_source_paths(
    source_lock: dict[str, Any], source_root: Path, source_names: set[str]
) -> set[Path]:
    paths: set[Path] = set()
    for source in source_lock["sources"]:
        if source.get("name") not in source_names:
            continue
        archive = source.get("archive")
        if isinstance(archive, dict):
            extracted = source_root / str(archive["root"])
            paths.update((extracted / str(item)).resolve() for item in archive["datasets"])
        else:
            paths.add((source_root / str(source["filename"])).resolve())
    return paths


def _trail_fragment_store(workspace: Path) -> Path:
    return workspace / "trail-fragments.sqlite3"


def _remove_trail_fragment_store(workspace: Path) -> None:
    store = _trail_fragment_store(workspace)
    paths = [store, *(Path(f"{store}{suffix}") for suffix in ("-journal", "-wal", "-shm"))]
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise HouseHunterError("Mountain trail fragment store path is unsafe")
    for path in paths:
        path.unlink(missing_ok=True)


def _normalize_trail_uuid(value: object, *, optional: bool = False) -> str | None:
    if value is None or str(value).strip() == "":
        if optional:
            return None
        raise HouseHunterError("Mountain trail GLOBALID is null")
    text = str(value).strip().removeprefix("{").removesuffix("}")
    try:
        return str(uuid.UUID(text))
    except (AttributeError, ValueError) as exc:
        raise HouseHunterError("Mountain trail identity is not a valid UUID") from exc


def _open_trail_fragment_store(workspace: Path) -> sqlite3.Connection:
    path = _trail_fragment_store(workspace)
    if path.is_symlink():
        raise HouseHunterError("Mountain trail fragment store path is unsafe")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.executescript(
            """
        CREATE TABLE IF NOT EXISTS ingested_batch (
            batch_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            feature_rows INTEGER NOT NULL,
            fragment_rows INTEGER NOT NULL,
            semantic_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fragment (
            globalid TEXT NOT NULL,
            region TEXT NOT NULL,
            source_rank INTEGER NOT NULL,
            feature_ordinal INTEGER NOT NULL,
            part_ordinal INTEGER NOT NULL,
            permanentidentifier TEXT,
            trailtype TEXT NOT NULL,
            wkb BLOB NOT NULL,
            PRIMARY KEY (source_rank, feature_ordinal, part_ordinal)
        );
        CREATE INDEX IF NOT EXISTS fragment_identity
            ON fragment (globalid, source_rank, feature_ordinal, part_ordinal);
        CREATE TABLE IF NOT EXISTS store_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
        )
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise HouseHunterError("Mountain trail fragment store is corrupt")
        return connection
    except (OSError, sqlite3.DatabaseError) as exc:
        if connection is not None:
            connection.close()
        raise HouseHunterError("Mountain trail fragment store is corrupt") from exc


def _trail_path_regions(
    regions: tuple[RegionSources, ...], paths: set[Path]
) -> dict[Path, RegionSources]:
    observed: dict[Path, RegionSources] = {}
    for region in regions:
        region_paths = region.trails if isinstance(region.trails, tuple) else (region.trails,)
        for path in region_paths:
            resolved = path.resolve()
            if resolved not in paths:
                continue
            if resolved in observed:
                raise HouseHunterError("Mountain trail source belongs to multiple regions")
            observed[resolved] = region
    if set(observed) != paths:
        raise HouseHunterError("Mountain trail batch differs from the region source contract")
    return observed


def _ingest_trail_fragment_batch(
    regions: tuple[RegionSources, ...],
    workspace: Path,
    *,
    batch: dict[str, Any],
    ordered_batches: list[dict[str, Any]],
    dependency_sha256: str,
    maximum_bytes: int,
    source_lock_path: Path,
    source_lock: dict[str, Any],
    source_root: Path | None,
    managed_staging_root: Path | None,
) -> None:
    if source_root is None:
        raise HouseHunterError("Mountain batched trails preparation requires a source root")
    batch_id = str(batch["id"])
    source_names = {str(value) for value in batch["sources"]}
    source_digest = _source_names_digest(source_lock, source_names)
    checkpoint_name = f"trails--{batch_id}"
    completed = [
        str(item["id"])
        for item in ordered_batches
        if _valid_phase(
            workspace,
            f"trails--{item['id']}",
            dependency_sha256=dependency_sha256,
            verified_source_sha256=_source_names_digest(
                source_lock, {str(value) for value in item["sources"]}
            ),
        )
        is not None
    ]
    expected_prefix = [str(item["id"]) for item in ordered_batches[: len(completed)]]
    if completed != expected_prefix:
        raise HouseHunterError("Mountain trails batches were not completed in canonical order")
    checkpoint = _valid_phase(
        workspace,
        checkpoint_name,
        dependency_sha256=dependency_sha256,
        verified_source_sha256=source_digest,
    )
    if checkpoint is not None:
        final = _valid_phase(
            workspace,
            "trails",
            dependency_sha256=dependency_sha256,
            verified_source_sha256=_source_family_digest(source_lock, "trails"),
        )
        if final is None and not _trail_fragment_store(workspace).is_file():
            raise HouseHunterError("Mountain trail fragment store is missing")
        if final is None:
            connection = _open_trail_fragment_store(workspace)
            try:
                stored = connection.execute(
                    "SELECT source_sha256 FROM ingested_batch WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
            finally:
                connection.close()
            if stored != (source_digest,):
                raise HouseHunterError("Mountain trail batch store digest differs")
        _complete_batch_sources(
            workspace,
            checkpoint_name,
            source_lock=source_lock,
            source_names=source_names,
            source_root=source_root,
            managed_staging_root=managed_staging_root,
        )
        return
    if len(completed) >= len(ordered_batches) or ordered_batches[len(completed)] != batch:
        raise HouseHunterError("Mountain trails batch must follow canonical source order")
    verify_source_families(
        source_lock_path,
        root=source_root,
        families={"trails"},
        source_names=source_names,
    )
    selected_paths = _trail_source_paths(source_lock, source_root, source_names)
    path_regions = _trail_path_regions(regions, selected_paths)
    source_rank = {
        str(source["name"]): index
        for index, source in enumerate(source_lock["sources"])
        if source.get("family") == "trails"
    }
    ordered_sources = [
        source
        for source in source_lock["sources"]
        if source.get("family") == "trails" and source.get("name") in source_names
    ]
    connection = _open_trail_fragment_store(workspace)
    feature_rows = 0
    fragment_rows = 0
    semantic = hashlib.sha256()
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT source_sha256 FROM ingested_batch WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if prior is not None:
            if prior != (source_digest,):
                raise HouseHunterError("Mountain trail batch store digest differs")
            connection.rollback()
        else:
            for source in ordered_sources:
                name = str(source["name"])
                paths = sorted(
                    _trail_source_paths(source_lock, source_root, {name}),
                    key=lambda value: str(value),
                )
                for path in paths:
                    region = path_regions[path.resolve()]
                    metadata, fids, geometry_wkb, arrays = pyogrio.raw.read(
                        path,
                        layer=region.trails_layer,
                        columns=["GLOBALID", "permanentidentifier", "trailtype"],
                        where=region.trail_where,
                        force_2d=True,
                        return_fids=True,
                    )
                    fields = dict(zip(metadata["fields"], arrays, strict=True))
                    geometries = shapely.from_wkb(geometry_wkb)
                    seen: set[str] = set()
                    for ordinal, geometry in enumerate(geometries):
                        globalid = _normalize_trail_uuid(fields["GLOBALID"][ordinal])
                        assert globalid is not None
                        if globalid in seen:
                            raise HouseHunterError(
                                "Mountain trail source contains a duplicate GLOBALID"
                            )
                        seen.add(globalid)
                        permanentidentifier = _normalize_trail_uuid(
                            fields["permanentidentifier"][ordinal], optional=True
                        )
                        trailtype = fields["trailtype"][ordinal]
                        if trailtype is None or str(trailtype) == "Water Trail":
                            raise HouseHunterError("Mountain trail predicate result differs")
                        if (
                            geometry is None
                            or shapely.is_missing(geometry)
                            or shapely.is_empty(geometry)
                            or not shapely.is_valid(geometry)
                            or geometry.geom_type not in {"LineString", "MultiLineString"}
                        ):
                            raise HouseHunterError("Mountain trail geometry is invalid")
                        parts = (
                            list(geometry.geoms)
                            if geometry.geom_type == "MultiLineString"
                            else [geometry]
                        )
                        feature_ordinal = int(fids[ordinal]) if fids is not None else ordinal
                        feature_rows += 1
                        for part_ordinal, part in enumerate(parts):
                            wkb = bytes(
                                shapely.to_wkb(
                                    part,
                                    hex=False,
                                    output_dimension=2,
                                    byte_order=1,
                                )
                            )
                            record = [
                                globalid,
                                region.name,
                                source_rank[name],
                                feature_ordinal,
                                part_ordinal,
                                permanentidentifier,
                                str(trailtype),
                                hashlib.sha256(wkb).hexdigest(),
                            ]
                            semantic.update(
                                (json.dumps(record, separators=(",", ":")) + "\n").encode()
                            )
                            connection.execute(
                                "INSERT INTO fragment VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (*record[:-1], sqlite3.Binary(wkb)),
                            )
                            fragment_rows += 1
            connection.execute(
                "INSERT INTO ingested_batch VALUES (?, ?, ?, ?, ?)",
                (batch_id, source_digest, feature_rows, fragment_rows, semantic.hexdigest()),
            )
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _write_phase(
        workspace,
        checkpoint_name,
        {},
        dependency_sha256=dependency_sha256,
        verified_source_sha256=source_digest,
    )
    if allocated_size(workspace) > maximum_bytes:
        raise HouseHunterError(f"Mountain prepared pack exceeds its {maximum_bytes:,}-byte budget")
    _complete_batch_sources(
        workspace,
        checkpoint_name,
        source_lock=source_lock,
        source_names=source_names,
        source_root=source_root,
        managed_staging_root=managed_staging_root,
    )


def _finalize_trail_fragment_store(
    workspace: Path, source_lock: dict[str, Any]
) -> tuple[sqlite3.Connection, str]:
    connection = _open_trail_fragment_store(workspace)
    try:
        metadata = dict(connection.execute("SELECT key, value FROM store_metadata"))
        if "logical_sha256" in metadata:
            # The SQLite file is resumable work state, not an external trust anchor.
            # Rebuild the derived table and spatial index from the canonical fragments
            # so a valid SQLite file with modified logical rows cannot bypass the
            # reviewed fragment contract.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS logical_rtree")
            connection.execute("DROP TABLE IF EXISTS logical_feature")
            connection.execute("DELETE FROM store_metadata")
            connection.commit()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS logical_feature (
                id INTEGER PRIMARY KEY,
                globalid TEXT NOT NULL UNIQUE,
                region TEXT NOT NULL,
                permanentidentifier TEXT,
                trailtype TEXT NOT NULL,
                wkb BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS logical_rtree USING rtree(
                id, minx, maxx, miny, maxy
            );
            """
        )
        rows = connection.execute(
            """
            SELECT globalid, region, permanentidentifier, trailtype, wkb
            FROM fragment
            ORDER BY globalid, source_rank, feature_ordinal, part_ordinal
            """
        )
        digest = hashlib.sha256()
        logical_count = 0
        fragment_count = 0
        current_id: str | None = None
        group: list[tuple[str, str | None, str, bytes]] = []

        def commit_group(
            globalid: str, fragments: list[tuple[str, str | None, str, bytes]]
        ) -> None:
            nonlocal logical_count, fragment_count
            regions = {item[0] for item in fragments}
            permanentidentifiers = {item[1] for item in fragments}
            trailtypes = {item[2] for item in fragments}
            if len(regions) != 1 or len(permanentidentifiers) != 1 or len(trailtypes) != 1:
                raise HouseHunterError("Mountain cross-state trail attributes conflict")
            parts = [shapely.from_wkb(item[3]) for item in fragments]
            geometry = shapely.MultiLineString([part.coords for part in parts])
            wkb = bytes(shapely.to_wkb(geometry, hex=False, output_dimension=2, byte_order=1))
            logical_count += 1
            fragment_count += len(fragments)
            part_hashes = [hashlib.sha256(item[3]).hexdigest() for item in fragments]
            record = [
                globalid,
                next(iter(regions)),
                next(iter(permanentidentifiers)),
                next(iter(trailtypes)),
                part_hashes,
            ]
            digest.update((json.dumps(record, separators=(",", ":")) + "\n").encode())
            cursor = connection.execute(
                "INSERT INTO logical_feature"
                "(globalid, region, permanentidentifier, trailtype, wkb) "
                "VALUES (?, ?, ?, ?, ?)",
                (*record[:4], sqlite3.Binary(wkb)),
            )
            minx, miny, maxx, maxy = geometry.bounds
            connection.execute(
                "INSERT INTO logical_rtree VALUES (?, ?, ?, ?, ?)",
                (cursor.lastrowid, minx, maxx, miny, maxy),
            )

        connection.execute("BEGIN IMMEDIATE")
        for globalid, region, permanentidentifier, trailtype, wkb in rows:
            if current_id is not None and globalid != current_id:
                commit_group(current_id, group)
                group = []
            current_id = str(globalid)
            group.append((str(region), permanentidentifier, str(trailtype), bytes(wkb)))
        if current_id is not None:
            commit_group(current_id, group)
        contract = source_lock.get("trail_fragment_contract", {})
        expected = {
            "feature_rows": sum(
                row[0] for row in connection.execute("SELECT feature_rows FROM ingested_batch")
            ),
            "fragment_rows": fragment_count,
            "logical_features": logical_count,
            "logical_sha256": digest.hexdigest(),
        }
        for key, value in expected.items():
            if key in contract and contract[key] != value:
                raise HouseHunterError(f"Mountain trail fragment contract differs: {key}")
            connection.execute("INSERT INTO store_metadata VALUES (?, ?)", (key, str(value)))
        connection.commit()
        return connection, digest.hexdigest()
    except Exception:
        connection.rollback()
        connection.close()
        raise


def _query_trail_fragment_wkbs(
    connection: sqlite3.Connection,
    *,
    region: str,
    target_crs: str,
    bounds: tuple[float, float, float, float],
) -> list[bytes]:
    records: dict[str, bytes] = {}
    for west, south, east, north in _query_bounds(target_crs, "EPSG:4269", bounds):
        for globalid, wkb in connection.execute(
            """
            SELECT logical_feature.globalid, logical_feature.wkb
            FROM logical_feature
            JOIN logical_rtree ON logical_rtree.id = logical_feature.id
            WHERE logical_feature.region = ? AND logical_rtree.maxx >= ?
              AND logical_rtree.minx <= ? AND logical_rtree.maxy >= ?
              AND logical_rtree.miny <= ?
            ORDER BY logical_feature.globalid
            """,
            (region, west, east, south, north),
        ).fetchall():
            records[str(globalid)] = bytes(wkb)
    return [records[key] for key in sorted(records)]


def _materialize_fragment_trails(
    regions: tuple[RegionSources, ...],
    entries: list[dict[str, object]],
    workspace: Path,
    *,
    dependency_sha256: str,
    maximum_bytes: int,
    source_lock: dict[str, Any],
) -> dict[str, dict[str, object]]:
    source_digest = _source_family_digest(source_lock, "trails")
    valid = _valid_phase(
        workspace,
        "trails",
        dependency_sha256=dependency_sha256,
        verified_source_sha256=source_digest,
    )
    if valid is not None:
        _remove_trail_fragment_store(workspace)
        return valid["files"]
    connection, logical_digest = _finalize_trail_fragment_store(workspace, source_lock)
    progress_path = _phase_path(workspace, "trails-progress")
    progress = (
        _load_json(progress_path, "trails progress")
        if progress_path.is_file()
        else {
            "schema_version": 2,
            "phase": "trails-progress",
            "dependency_sha256": dependency_sha256,
            "logical_sha256": logical_digest,
            "files": {},
        }
    )
    if (
        progress.get("schema_version") != 2
        or progress.get("dependency_sha256") != dependency_sha256
        or progress.get("logical_sha256") != logical_digest
        or not isinstance(progress.get("files"), dict)
    ):
        connection.close()
        raise HouseHunterError("Mountain trails progress is incompatible")
    files: dict[str, dict[str, object]] = progress["files"]
    by_region = {region.name: region for region in regions}
    try:
        for entry in entries:
            key = str(entry["key"])
            target = workspace / "tiles" / key / "trails.npy"
            if key in files and _file_metadata(target) == files[key]:
                continue
            region = by_region[str(entry["region"])]
            bounds = tuple(float(value) for value in entry["bounds"])
            geometries = shapely.from_wkb(
                _query_trail_fragment_wkbs(
                    connection,
                    region=region.name,
                    target_crs=region.target_crs,
                    bounds=bounds,
                )
            )
            if len(geometries):
                transformer = pyproj.Transformer.from_crs(
                    "EPSG:4269", region.target_crs, always_xy=True
                )
                geometries = shapely.transform(geometries, transformer.transform, interleaved=False)
                array = rasterio.features.rasterize(
                    ((geometry, 1.0) for geometry in geometries),
                    out_shape=tuple(int(value) for value in entry["shape"]),
                    transform=rasterio.Affine(*entry["transform"]),
                    fill=0.0,
                    all_touched=False,
                    merge_alg=rasterio.enums.MergeAlg.add,
                    dtype=np.float32,
                )
            else:
                array = np.zeros(tuple(int(value) for value in entry["shape"]), np.float32)
            _save_array(target, array.astype(np.float32, copy=False))
            files[key] = _file_metadata(target)
            progress["files"] = files
            _write_json(progress_path, progress)
            if allocated_size(workspace) > maximum_bytes:
                raise HouseHunterError(
                    f"Mountain prepared pack exceeds its {maximum_bytes:,}-byte budget"
                )
    finally:
        connection.close()
    if set(files) != {str(entry["key"]) for entry in entries}:
        raise HouseHunterError("Mountain trail fragment store did not cover every tile")
    _write_phase(
        workspace,
        "trails",
        files,
        dependency_sha256=dependency_sha256,
        verified_source_sha256=source_digest,
    )
    _remove_trail_fragment_store(workspace)
    return files


def _load_trails_progress(
    workspace: Path,
    *,
    dependency_sha256: str,
    entries: list[dict[str, object]],
    pending_batch_id: str | None = None,
) -> dict[str, Any] | None:
    path = _phase_path(workspace, "trails-progress")
    if not path.exists():
        return None
    progress = _load_json(path, "trails progress")
    expected_keys = {str(entry["key"]) for entry in entries}
    if (
        progress.get("schema_version") != 1
        or progress.get("phase") != "trails-progress"
        or progress.get("dependency_sha256") != dependency_sha256
        or not isinstance(progress.get("completed_batches"), list)
        or not isinstance(progress.get("files"), dict)
        or set(progress["files"]) != expected_keys
    ):
        raise HouseHunterError("Mountain trails progress is incompatible")
    pending_updates: dict[str, Any] = {}
    if pending_batch_id is not None:
        manifest_path = (
            workspace / "checkpoints" / f".trails-{pending_batch_id}.txn" / "manifest.json"
        )
        if manifest_path.is_file():
            pending = _load_json(manifest_path, "trails transaction")
            if pending.get("batch_id") != pending_batch_id:
                raise HouseHunterError("Mountain trails transaction is incompatible")
            pending_updates = pending.get("updates", {})
    for key, metadata in progress["files"].items():
        target = workspace / "tiles" / key / "trails.npy"
        if not isinstance(metadata, dict):
            raise HouseHunterError("Mountain trails progress checksum differs")
        if _file_metadata(target) == metadata:
            continue
        pending_update = pending_updates.get(key)
        if not isinstance(pending_update, dict) or not _metadata_matches(
            target, pending_update.get("after", {})
        ):
            raise HouseHunterError("Mountain trails progress checksum differs")
    return progress


def _initialize_trails_progress(
    workspace: Path,
    *,
    dependency_sha256: str,
    entries: list[dict[str, object]],
) -> dict[str, Any]:
    _clear_phase_files(workspace, "trails.npy")
    files: dict[str, dict[str, object]] = {}
    for entry in entries:
        target = workspace / "tiles" / str(entry["key"]) / "trails.npy"
        _save_array(target, np.zeros(tuple(int(value) for value in entry["shape"]), np.float32))
        files[str(entry["key"])] = _file_metadata(target)
    progress: dict[str, Any] = {
        "schema_version": 1,
        "phase": "trails-progress",
        "dependency_sha256": dependency_sha256,
        "completed_batches": [],
        "files": files,
    }
    _write_json(_phase_path(workspace, "trails-progress"), progress)
    return progress


def _materialize_trails_batch(
    regions: tuple[RegionSources, ...],
    entries: list[dict[str, object]],
    workspace: Path,
    *,
    batch: dict[str, Any],
    ordered_batches: list[dict[str, Any]],
    dependency_sha256: str,
    maximum_bytes: int,
    source_lock_path: Path,
    source_lock: dict[str, Any],
    source_root: Path | None,
    managed_staging_root: Path | None,
) -> None:
    if source_root is None:
        raise HouseHunterError("Mountain batched trails preparation requires a source root")
    batch_id = str(batch["id"])
    progress = _load_trails_progress(
        workspace,
        dependency_sha256=dependency_sha256,
        entries=entries,
        pending_batch_id=batch_id,
    )
    if progress is None:
        progress = _initialize_trails_progress(
            workspace, dependency_sha256=dependency_sha256, entries=entries
        )
    completed = [str(value) for value in progress["completed_batches"]]
    expected_prefix = [str(item["id"]) for item in ordered_batches[: len(completed)]]
    if completed != expected_prefix:
        raise HouseHunterError("Mountain trails batches were not completed in canonical order")
    if batch_id in completed:
        source_names = {str(value) for value in batch["sources"]}
        _complete_batch_sources(
            workspace,
            f"trails--{batch_id}",
            source_lock=source_lock,
            source_names=source_names,
            source_root=source_root,
            managed_staging_root=managed_staging_root,
        )
        return
    if len(completed) >= len(ordered_batches) or ordered_batches[len(completed)] != batch:
        raise HouseHunterError("Mountain trails batch must follow canonical source order")
    source_names = {str(value) for value in batch["sources"]}
    verify_source_families(
        source_lock_path,
        root=source_root,
        families={"trails"},
        source_names=source_names,
    )
    selected_paths = _trail_source_paths(source_lock, source_root, source_names)
    filtered_regions: dict[str, RegionSources] = {}
    for region in regions:
        paths = region.trails if isinstance(region.trails, tuple) else (region.trails,)
        filtered_regions[region.name] = replace(
            region, trails=tuple(path for path in paths if path.resolve() in selected_paths)
        )
    observed = {
        path.resolve()
        for region in filtered_regions.values()
        for path in (region.trails if isinstance(region.trails, tuple) else (region.trails,))
    }
    if observed != selected_paths:
        raise HouseHunterError("Mountain trails batch differs from the region source contract")
    requested = set(str(value) for value in batch["tile_keys"])
    selected = [
        entry
        for entry in entries
        if f"{entry['region']}:{entry['tile_x']}:{entry['tile_y']}" in requested
    ]
    if len(selected) != len(requested):
        raise HouseHunterError("Mountain trails batch contains an unknown tile")
    transaction = workspace / "checkpoints" / f".trails-{batch_id}.txn"
    manifest_path = transaction / "manifest.json"
    if transaction.exists() and (transaction.is_symlink() or not transaction.is_dir()):
        raise HouseHunterError("Mountain trails transaction path is unsafe")
    if not manifest_path.is_file():
        if transaction.exists():
            shutil.rmtree(transaction)
        building = transaction.with_name(f"{transaction.name}.{uuid.uuid4().hex}.part")
        building.mkdir()
        updates: dict[str, dict[str, object]] = {}
        try:
            for entry in selected:
                key = str(entry["key"])
                target = workspace / "tiles" / key / "trails.npy"
                before = _file_metadata(target)
                region = filtered_regions[str(entry["region"])]
                if not region.trails:
                    raise HouseHunterError("Mountain trails batch tile has no selected source")
                contribution = read_tile_trails(
                    region,
                    tuple(float(value) for value in entry["bounds"]),
                    shape=tuple(int(value) for value in entry["shape"]),
                    transform=rasterio.Affine(*entry["transform"]),
                )
                output = (
                    np.asarray(np.load(target, mmap_mode="r", allow_pickle=False)) + contribution
                )
                shadow = building / f"{key}.npy"
                _save_array(shadow, output.astype(np.float32, copy=False))
                after = _file_metadata(shadow)
                after["filename"] = "trails.npy"
                updates[key] = {"before": before, "after": after}
                if allocated_size(workspace) > maximum_bytes:
                    raise HouseHunterError(
                        f"Mountain prepared pack exceeds its {maximum_bytes:,}-byte budget"
                    )
            _write_json(
                building / "manifest.json",
                {
                    "schema_version": 1,
                    "batch_id": batch_id,
                    "dependency_sha256": dependency_sha256,
                    "verified_source_sha256": _source_names_digest(source_lock, source_names),
                    "updates": updates,
                },
            )
            os.replace(building, transaction)
        finally:
            if building.exists():
                shutil.rmtree(building)
    manifest = _load_json(manifest_path, "trails transaction")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("batch_id") != batch_id
        or manifest.get("dependency_sha256") != dependency_sha256
        or manifest.get("verified_source_sha256") != _source_names_digest(source_lock, source_names)
        or set(manifest.get("updates", {})) != {str(entry["key"]) for entry in selected}
    ):
        raise HouseHunterError("Mountain trails transaction is incompatible")
    for key, update in manifest["updates"].items():
        target = workspace / "tiles" / key / "trails.npy"
        shadow = transaction / f"{key}.npy"
        before = update["before"]
        after = update["after"]
        if _metadata_matches(target, after):
            continue
        if not _metadata_matches(target, before) or not _metadata_matches(shadow, after):
            raise HouseHunterError("Mountain trails transaction checksum differs")
        os.replace(shadow, target)
    for key, update in manifest["updates"].items():
        if not _metadata_matches(workspace / "tiles" / key / "trails.npy", update["after"]):
            raise HouseHunterError("Mountain trails transaction did not commit")
        progress["files"][key] = update["after"]
    progress["completed_batches"] = [*completed, batch_id]
    _write_json(_phase_path(workspace, "trails-progress"), progress)
    _write_phase(
        workspace,
        f"trails--{batch_id}",
        {},
        dependency_sha256=dependency_sha256,
        verified_source_sha256=_source_names_digest(source_lock, source_names),
    )
    shutil.rmtree(transaction)
    _complete_batch_sources(
        workspace,
        f"trails--{batch_id}",
        source_lock=source_lock,
        source_names=source_names,
        source_root=source_root,
        managed_staging_root=managed_staging_root,
    )


def _materialize_v2_tiles(
    regions: tuple[RegionSources, ...],
    workspace: Path,
    *,
    state_by_fips: dict[str, str],
    cell_size_m: float,
    tile_size_m: float,
    maximum_bytes: int,
    source_lock_path: Path,
    source_lock: dict[str, Any],
    source_root: Path | None,
    managed_staging_root: Path | None,
    comparison: Path | None,
    stop_after_phase: str | None = None,
    source_batch: str | None = None,
) -> tuple[list[dict[str, object]], list[pl.DataFrame]] | None:
    """Run the four fixed, resumable national preparation phases."""
    checkpoints = workspace / "checkpoints"
    checkpoints.mkdir(exist_ok=True)
    tile_root = workspace / "tiles"
    blocks_source_digest = _source_family_digest(source_lock, "blocks")
    blocks_phase = _valid_phase(workspace, "blocks", verified_source_sha256=blocks_source_digest)
    entries = blocks_phase.get("entries") if blocks_phase else None
    if not isinstance(entries, list) or not entries:
        blocks_source_digest = _verify_phase_sources_before_read(
            family="blocks",
            source_lock_path=source_lock_path,
            source_lock=source_lock,
            source_root=source_root,
        )
        for child in list(tile_root.iterdir()):
            if child.is_symlink() or not child.is_dir() or _TILE_KEY.fullmatch(child.name) is None:
                raise HouseHunterError("Mountain preparation contains an unsafe tile path")
            shutil.rmtree(child)
        entries = []
        files: dict[str, dict[str, object]] = {}
        for region in regions:
            for samples, bounds in iter_region_block_samples(
                region,
                state_by_fips=state_by_fips,
                cell_size_m=cell_size_m,
                tile_size_m=tile_size_m,
            ):
                tile_x = int(samples["tile_x"][0])
                tile_y = int(samples["tile_y"][0])
                key = _tile_key(region.name, tile_x, tile_y)
                tile_dir = tile_root / key
                tile_dir.mkdir()
                blocks_path = tile_dir / "blocks.parquet"
                samples.write_parquet(blocks_path, compression="zstd", statistics=True)
                core = [
                    tile_x * tile_size_m,
                    tile_y * tile_size_m,
                    (tile_x + 1) * tile_size_m,
                    (tile_y + 1) * tile_size_m,
                ]
                shape = int((tile_size_m + 2 * HALO_M) / cell_size_m)
                entries.append(
                    {
                        "key": key,
                        "region": region.name,
                        "target_crs": region.target_crs,
                        "tile_x": tile_x,
                        "tile_y": tile_y,
                        "core_bounds": core,
                        "bounds": list(bounds),
                        "transform": [
                            cell_size_m,
                            0.0,
                            bounds[0],
                            0.0,
                            -cell_size_m,
                            bounds[3],
                        ],
                        "shape": [shape, shape],
                        "rows": samples.height,
                        "population": int(samples["pop20"].sum()),
                        "block_geoid_sha256": block_geoid_sha256(samples),
                        "block_sample_sha256": block_sample_sha256(samples),
                    }
                )
                files[key] = _file_metadata(blocks_path)
        entries.sort(key=lambda item: (item["region"], item["tile_x"], item["tile_y"]))
        blocks_phase = _write_phase(
            workspace,
            "blocks",
            files,
            verified_source_sha256=blocks_source_digest,
            entries=entries,
        )
    _complete_phase_sources(
        workspace,
        "blocks",
        family="blocks",
        source_lock_path=source_lock_path,
        source_lock=source_lock,
        source_root=source_root,
        managed_staging_root=managed_staging_root,
    )
    if stop_after_phase == "blocks":
        return None
    blocks_digest = _phase_digest(workspace, "blocks")
    by_region = {region.name: region for region in regions}
    elevation_batches = [
        batch
        for batch in source_lock.get("preparation_batches", [])
        if batch.get("family") == "elevation"
    ]
    trails_batches = [
        batch
        for batch in source_lock.get("preparation_batches", [])
        if batch.get("family") == "trails"
    ]
    fragment_trails = source_lock.get("trail_fragment_mode") == TRAIL_FRAGMENT_MODE
    selected_batch = (
        preparation_batch(source_lock, source_batch) if source_batch is not None else None
    )
    trail_ingest_digest = _source_family_digest(source_lock, "trails")
    dependency = blocks_digest
    if elevation_batches:
        if selected_batch is not None and selected_batch.get("family") == "elevation":
            _materialize_elevation_batch(
                regions,
                entries,
                workspace,
                batch=selected_batch,
                blocks_digest=blocks_digest,
                cell_size_m=cell_size_m,
                maximum_bytes=maximum_bytes,
                source_lock_path=source_lock_path,
                source_lock=source_lock,
                source_root=source_root,
                managed_staging_root=managed_staging_root,
            )
            return None
        files: dict[str, dict[str, object]] = {}
        for batch in elevation_batches:
            source_names = {str(name) for name in batch["sources"]}
            batch_phase = _valid_phase(
                workspace,
                f"elevation--{batch['id']}",
                dependency_sha256=blocks_digest,
                verified_source_sha256=_source_names_digest(source_lock, source_names),
            )
            if batch_phase is None:
                raise HouseHunterError(f"Mountain elevation batch is incomplete: {batch['id']}")
            files.update(batch_phase["files"])
        if set(files) != {str(entry["key"]) for entry in entries}:
            raise HouseHunterError("Mountain elevation batches do not cover every tile")
        source_digest = _source_family_digest(source_lock, "elevation")
        valid = _valid_phase(
            workspace,
            "elevation",
            dependency_sha256=blocks_digest,
            verified_source_sha256=source_digest,
        )
        if valid is None or valid["files"] != files:
            _write_phase(
                workspace,
                "elevation",
                files,
                dependency_sha256=blocks_digest,
                verified_source_sha256=source_digest,
            )
        dependency = _phase_digest(workspace, "elevation")
        if stop_after_phase == "elevation":
            return None
    elif selected_batch is not None and selected_batch.get("family") == "elevation":
        raise HouseHunterError("Mountain source lock does not use elevation batches")
    arrays = (
        *(() if elevation_batches else (("elevation", "elevation.npy", np.float32),)),
        ("pad", "pad.npy", np.uint8),
        ("trails", "trails.npy", np.float32),
    )
    for phase, filename, dtype in arrays:
        family = "pad_us" if phase == "pad" else phase
        if phase == "trails" and trails_batches:
            if selected_batch is not None:
                if selected_batch.get("family") != "trails":
                    raise HouseHunterError("Mountain source batch has already been completed")
                arguments = {
                    "batch": selected_batch,
                    "ordered_batches": trails_batches,
                    "dependency_sha256": dependency,
                    "maximum_bytes": maximum_bytes,
                    "source_lock_path": source_lock_path,
                    "source_lock": source_lock,
                    "source_root": source_root,
                    "managed_staging_root": managed_staging_root,
                }
                if fragment_trails:
                    arguments["dependency_sha256"] = trail_ingest_digest
                    _ingest_trail_fragment_batch(regions, workspace, **arguments)
                else:
                    _materialize_trails_batch(regions, entries, workspace, **arguments)
                return None
            if fragment_trails:
                for batch in trails_batches:
                    source_names = {str(value) for value in batch["sources"]}
                    if (
                        _valid_phase(
                            workspace,
                            f"trails--{batch['id']}",
                            dependency_sha256=trail_ingest_digest,
                            verified_source_sha256=_source_names_digest(source_lock, source_names),
                        )
                        is None
                    ):
                        raise HouseHunterError(
                            f"Mountain trails batch is incomplete: {batch['id']}"
                        )
                _materialize_fragment_trails(
                    regions,
                    entries,
                    workspace,
                    dependency_sha256=dependency,
                    maximum_bytes=maximum_bytes,
                    source_lock=source_lock,
                )
                dependency = _phase_digest(workspace, "trails")
                continue
            progress = _load_trails_progress(
                workspace, dependency_sha256=dependency, entries=entries
            )
            expected_batches = [str(batch["id"]) for batch in trails_batches]
            if progress is None or progress["completed_batches"] != expected_batches:
                missing = expected_batches[len(progress["completed_batches"] if progress else [])]
                raise HouseHunterError(f"Mountain trails batch is incomplete: {missing}")
            files = progress["files"]
            source_digest = _source_family_digest(source_lock, "trails")
            valid = _valid_phase(
                workspace,
                "trails",
                dependency_sha256=dependency,
                verified_source_sha256=source_digest,
            )
            if valid is None or valid["files"] != files:
                valid = _write_phase(
                    workspace,
                    "trails",
                    files,
                    dependency_sha256=dependency,
                    verified_source_sha256=source_digest,
                )
            dependency = _phase_digest(workspace, "trails")
            continue
        source_digest = _source_family_digest(source_lock, family)
        valid = _valid_phase(
            workspace,
            phase,
            dependency_sha256=dependency,
            verified_source_sha256=source_digest,
        )
        if valid is None or set(valid["files"]) != {str(entry["key"]) for entry in entries}:
            source_digest = _verify_phase_sources_before_read(
                family=family,
                source_lock_path=source_lock_path,
                source_lock=source_lock,
                source_root=source_root,
            )
            _clear_phase_files(workspace, filename)
            files = {}
            indexes = (
                {
                    region.name: _elevation_index(region.elevation, region.target_crs)
                    for region in regions
                }
                if phase == "elevation"
                else {}
            )
            for entry in entries:
                region = by_region[str(entry["region"])]
                bounds = tuple(float(value) for value in entry["bounds"])
                transform = rasterio.Affine(*entry["transform"])
                shape = tuple(int(value) for value in entry["shape"])
                if phase == "elevation":
                    array, _ = read_tile_elevation(
                        region,
                        bounds,
                        cell_size_m=cell_size_m,
                        indexed=indexes[region.name],
                    )
                elif phase == "pad":
                    array = read_tile_pad(region, bounds, shape=shape, transform=transform)
                else:
                    array = read_tile_trails(region, bounds, shape=shape, transform=transform)
                path = tile_root / str(entry["key"]) / filename
                _save_array(path, array.astype(dtype, copy=False))
                files[str(entry["key"])] = _file_metadata(path)
                if allocated_size(workspace) > maximum_bytes:
                    raise HouseHunterError(
                        f"Mountain prepared pack exceeds its {maximum_bytes:,}-byte budget"
                    )
            valid = _write_phase(
                workspace,
                phase,
                files,
                dependency_sha256=dependency,
                verified_source_sha256=source_digest,
            )
        if phase != "trails":
            _complete_phase_sources(
                workspace,
                phase,
                family=family,
                source_lock_path=source_lock_path,
                source_lock=source_lock,
                source_root=source_root,
                managed_staging_root=managed_staging_root,
            )
            if stop_after_phase == phase:
                return None
        dependency = _phase_digest(workspace, phase)
    frames = [
        pl.read_parquet(tile_root / str(entry["key"]) / "blocks.parquet").select(
            "block_geoid", "state", "pop20"
        )
        for entry in entries
    ]
    completed: list[dict[str, object]] = []
    reference_shards: list[dict[str, object]] = []
    for entry in entries:
        tile_dir = tile_root / str(entry["key"])
        blocks = pl.read_parquet(tile_dir / "blocks.parquet")
        elevation = np.load(tile_dir / "elevation.npy", mmap_mode="r", allow_pickle=False)
        pad = np.load(tile_dir / "pad.npy", mmap_mode="r", allow_pickle=False)
        trails = np.load(tile_dir / "trails.npy", mmap_mode="r", allow_pickle=False)
        raw = _raw_metrics_from_tile(blocks, elevation, pad, trails, cell_size_m=cell_size_m)
        if comparison is not None:
            reference_shards.append(
                {
                    "tile_key": entry["key"],
                    **_write_reference_shard(comparison, str(entry["key"]), raw),
                }
            )
        completed.append(
            {
                **entry,
                "raw_metric_sha256": raw_metric_sha256(raw),
                "files": {
                    name: _file_metadata(tile_dir / filename)
                    for name, filename in (
                        ("elevation", "elevation.npy"),
                        ("pad", "pad.npy"),
                        ("trails", "trails.npy"),
                        ("blocks", "blocks.parquet"),
                    )
                },
            }
        )
        _write_json(tile_dir / "tile.json", completed[-1])
    if comparison is not None:
        reference_shards.sort(key=lambda item: str(item["tile_key"]))
        _write_json(
            comparison / "manifest.json",
            {
                "schema_version": 1,
                "source_lock_sha256": sha256_file(source_lock_path),
                "raw_metric_sha256": sha256_bytes(
                    canonical_json([item["raw_metric_sha256"] for item in reference_shards])
                ),
                "block_count": sum(int(item["rows"]) for item in reference_shards),
                "shards": reference_shards,
            },
        )
    _complete_phase_sources(
        workspace,
        "trails",
        family="trails",
        source_lock_path=source_lock_path,
        source_lock=source_lock,
        source_root=source_root,
        managed_staging_root=managed_staging_root,
    )
    return completed, frames


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
    managed_root: Path | None = None,
    source_root: Path | None = None,
    managed_staging_root: Path | None = None,
    stop_after_family: str | None = None,
    source_batch: str | None = None,
) -> tuple[Path | None, Path | None]:
    """Create one immutable, content-addressed pack from the exact serial tile inputs."""
    if cell_size_m <= 0 or tile_size_m <= 0:
        raise HouseHunterError("Mountain prepared-pack grid sizes must be positive")
    phase_by_family = {
        "blocks": "blocks",
        "elevation": "elevation",
        "pad_us": "pad",
        "trails": "trails",
    }
    if stop_after_family is not None and stop_after_family not in phase_by_family:
        raise HouseHunterError("Mountain preparation family is invalid")
    if source_batch is not None and stop_after_family is not None:
        raise HouseHunterError(
            "Mountain preparation accepts a source batch or family checkpoint, not both"
        )
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
    comparison: Path | None = None
    if managed_root is not None and source_lock.get("schema_version") == SOURCE_LOCK_SCHEMA_VERSION:
        work_root = ensure_safe_directory(managed_root / "work")
        comparison = ensure_owned_child(
            work_root / preparation_id,
            work_root,
            name_pattern=r"[0-9a-f]{32}",
            marker_value="comparison-v1\n",
        )
        comparison_run = {**preparation_run, "kind": "source-derived-comparison"}
        comparison_run_path = comparison / "run.json"
        if comparison_run_path.is_file():
            if _load_json(comparison_run_path, "comparison run") != comparison_run:
                raise HouseHunterError("Mountain comparison workspace is incompatible")
        else:
            _write_json(comparison_run_path, comparison_run)
    if managed_root is not None:
        projection = source_lock.get("storage_projection")
        if not isinstance(projection, dict):
            raise HouseHunterError("Mountain source lock lacks a storage projection")
        remaining = _remaining_preparation_reservation(
            int(projection["preparation_workspace_bytes"]), temporary
        )
        if comparison is not None:
            remaining += max(
                0,
                int(projection["comparison_shard_bytes"]) - allocated_size(comparison),
            )
        remaining += int(projection["maximum_atomic_write_bytes"])
        ensure_storage_budget(managed_root, reserve_bytes=remaining)
    tile_root = temporary / "tiles"
    tile_entries: list[dict[str, object]] = []
    block_frames: list[pl.DataFrame] = []
    try:
        if source_lock.get("schema_version") == SOURCE_LOCK_SCHEMA_VERSION:
            if (
                _valid_phase(
                    temporary,
                    "blocks",
                    verified_source_sha256=_source_family_digest(source_lock, "blocks"),
                )
                is None
            ):
                _import_compatible_blocks_checkpoint(destination, temporary, source_lock)
            if source_lock.get("trail_fragment_mode") == TRAIL_FRAGMENT_MODE:
                _import_compatible_trail_ingestion(destination, temporary, source_lock)
            materialized = _materialize_v2_tiles(
                regions,
                temporary,
                state_by_fips=state_by_fips,
                cell_size_m=cell_size_m,
                tile_size_m=tile_size_m,
                maximum_bytes=maximum_bytes,
                source_lock_path=source_lock_path,
                source_lock=source_lock,
                source_root=source_root,
                managed_staging_root=managed_staging_root,
                comparison=comparison,
                stop_after_phase=phase_by_family.get(stop_after_family),
                source_batch=source_batch,
            )
            if materialized is None:
                return None, None
            tile_entries, block_frames = materialized
        else:
            tile_entries, block_frames = _resumable_tiles(temporary, cell_size_m=cell_size_m)
        for region in () if source_lock.get("schema_version") == 2 else regions:
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
                    "block_sample_sha256": block_sample_sha256(samples),
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
            "source_provenance": {"items": [source_provenance_item(item) for item in source_items]},
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
                "pad_codes": {"outside": 0, **PAD_ACCESS_CODES, "unrecognized": 4},
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
        checkpoints = temporary / "checkpoints"
        if checkpoints.exists():
            shutil.rmtree(checkpoints)
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
                    "block_geoid_sha256": entry["block_geoid_sha256"],
                    "block_sample_sha256": entry["block_sample_sha256"],
                }
                for entry in tile_entries
            ]
            raw_by_tile = {
                (entry["region"], entry["tile_x"], entry["tile_y"]): entry["raw_metric_sha256"]
                for entry in tile_entries
            }
            representative_match = all(
                raw_by_tile.get((item["region"], item["tile_x"], item["tile_y"]))
                == item["raw_metric_sha256"]
                for item in source_lock["representative_tiles"]
            )
            if (
                inventory.get("tile_count") != len(tile_entries)
                or inventory.get("block_count") != blocks.height
                or inventory.get("tiles") != actual_inventory
                or not representative_match
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
        if comparison is not None:
            comparison_manifest = comparison / "manifest.json"
            lock["comparison_id"] = comparison.name
            lock["comparison_manifest_sha256"] = sha256_file(comparison_manifest)
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
        or grid.get("pad_codes") != {"outside": 0, **PAD_ACCESS_CODES, "unrecognized": 4}
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
            or block_sample_sha256(blocks) != tile.get("block_sample_sha256")
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


def prune_owned_preparation_workspaces(prepared_root: Path) -> list[str]:
    """Remove obsolete, directly owned preparation workspaces after pack publication."""
    root = ensure_safe_directory(prepared_root)
    removed: list[str] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if _PREPARATION_WORK.fullmatch(child.name) is None:
            continue
        owned = require_owned_child(child, root, name_pattern=r"\.[0-9a-f]{32}\.work")
        marker = owned / OWNERSHIP_MARKER
        if marker.read_text() != "prepared-pack-v1\n":
            raise HouseHunterError("Mountain preparation workspace ownership marker differs")
        remove_owned_child(owned, root, name_pattern=r"\.[0-9a-f]{32}\.work")
        removed.append(child.name)
    return removed


def remove_owned_comparison_directory(
    comparison: Path,
    managed_work_root: Path,
    *,
    expected_id: str,
) -> None:
    """Remove only the source-derived comparison directory named by its external lock."""
    if comparison.name != expected_id or re.fullmatch(r"[0-9a-f]{32}", expected_id) is None:
        raise HouseHunterError("Mountain comparison directory identity differs")
    owned = require_owned_child(comparison, managed_work_root, name_pattern=r"[0-9a-f]{32}")
    if (owned / OWNERSHIP_MARKER).read_text() != "comparison-v1\n":
        raise HouseHunterError("Mountain comparison directory ownership marker differs")
    remove_owned_child(owned, managed_work_root, name_pattern=r"[0-9a-f]{32}")
