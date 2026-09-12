from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import polars as pl
import pyogrio
import rasterio
import shapely
from pyogrio import raw as ogr_raw
from pyproj import Transformer
from rasterio.enums import MergeAlg, Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds
from shapely.strtree import STRtree

from .config import sha256_file
from .errors import HouseHunterError
from .mountain import RAW_PRECISION, access_metrics, terrain_metrics


@dataclass(frozen=True)
class RegionSources:
    name: str
    target_crs: str
    blocks: Path
    elevation: tuple[Path, ...]
    pad_us: Path
    trails: Path
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
            regions.append(
                RegionSources(
                    name=str(item["name"]),
                    target_crs=str(item["target_crs"]),
                    blocks=source_path(item["blocks"]),
                    elevation=tuple(source_path(value) for value in item["elevation"]),
                    pad_us=source_path(item["pad_us"]),
                    trails=source_path(item["trails"]),
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
        return root / filename
    return Path(str(source.get("path", ""))).expanduser().resolve()


def verify_source_lock(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Validate all local files against a release lock before GIS processing."""
    try:
        lock = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
    if lock.get("schema_version") != 1 or not isinstance(lock.get("sources"), list):
        raise HouseHunterError("Mountain source lock is incompatible")
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
        if not file_path.is_file():
            raise HouseHunterError(f"Mountain source is missing: {file_path}")
        if file_path.stat().st_size != expected_size or sha256_file(file_path) != expected_sha:
            raise HouseHunterError(f"Mountain source checksum mismatch: {file_path}")
    return lock


def locked_source_paths(lock: dict[str, Any], *, root: Path | None = None) -> set[Path]:
    return {_source_path(source, root).resolve() for source in lock["sources"]}


def verify_region_sources_locked(
    regions: tuple[RegionSources, ...], lock: dict[str, Any], *, root: Path
) -> None:
    locked = locked_source_paths(lock, root=root)
    consumed = {
        path.resolve()
        for region in regions
        for path in (region.blocks, *region.elevation, region.pad_us, region.trails)
    }
    missing = sorted(str(path) for path in consumed - locked)
    if missing:
        raise HouseHunterError(
            "Mountain region uses files absent from the source lock: " + ", ".join(missing)
        )


def download_sources(
    lock_path: Path,
    destination: Path,
    *,
    client: httpx.Client | None = None,
) -> Path:
    """Download exactly locked source files and publish each only after verification."""
    try:
        lock = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Cannot read Mountain source lock: {exc}") from exc
    if lock.get("schema_version") != 1 or not isinstance(lock.get("sources"), list):
        raise HouseHunterError("Mountain source lock is incompatible")
    destination.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(120, connect=30), follow_redirects=True)
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
                        continue
                except (KeyError, TypeError, ValueError):
                    pass
            temporary = target.with_suffix(target.suffix + ".part")
            try:
                with http.stream("GET", source["url"]) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        for chunk in response.iter_bytes(1024 * 1024):
                            output.write(chunk)
                if (
                    temporary.stat().st_size != int(source["size"])
                    or sha256_file(temporary) != source["sha256"]
                ):
                    raise HouseHunterError(
                        f"Mountain source checksum mismatch: {source['filename']}"
                    )
                os.replace(temporary, target)
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


def _read_fields(path: Path, columns: list[str]) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    try:
        metadata, _, _, arrays = ogr_raw.read(path, columns=columns, read_geometry=False)
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
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    try:
        info = pyogrio.read_info(path)
        source_crs = info.get("crs")
        if not source_crs:
            raise HouseHunterError(f"Mountain vector source has no CRS: {path}")
        bbox = transform_bounds(target_crs, source_crs, *target_bounds, densify_pts=21)
        metadata, _, geometry, arrays = ogr_raw.read(
            path, columns=columns, bbox=bbox, where=where, force_2d=True
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
    _, fields = _read_fields(source.blocks, columns)
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
    frame = pl.DataFrame(
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
    if frame.filter(~pl.col("block_geoid").str.contains(r"^\d{15}$")).height:
        raise HouseHunterError("Mountain Census source contains invalid GEOID20 values")
    return frame


def _sample(array: np.ndarray, rows: np.ndarray, columns: np.ndarray) -> np.ndarray:
    values = array[rows, columns]
    if values.dtype == bool:
        return values.astype(float)
    return values


def build_region_raw_metrics(
    source: RegionSources,
    *,
    state_by_fips: dict[str, str],
    cell_size_m: float = 250,
    tile_size_m: float = 100_000,
) -> pl.DataFrame:
    """Compute one region in bounded tiles and return block-level raw metrics."""
    blocks = _blocks(source)
    elevation_paths, elevation_index = _elevation_index(source.elevation, source.target_crs)
    blocks = blocks.with_columns(
        pl.col("state_fips").replace_strict(state_by_fips).alias("state"),
        (pl.col("x") / tile_size_m).floor().cast(pl.Int64).alias("tile_x"),
        (pl.col("y") / tile_size_m).floor().cast(pl.Int64).alias("tile_y"),
    )
    outputs: list[pl.DataFrame] = []
    for tile in blocks.partition_by(["tile_x", "tile_y"], maintain_order=True):
        tile_x = tile["tile_x"][0]
        tile_y = tile["tile_y"][0]
        core = (
            tile_x * tile_size_m,
            tile_y * tile_size_m,
            (tile_x + 1) * tile_size_m,
            (tile_y + 1) * tile_size_m,
        )
        halo = 100_000
        bounds = (core[0] - halo, core[1] - halo, core[2] + halo, core[3] + halo)
        matching = elevation_index.query(shapely.box(*bounds))
        if not len(matching):
            raise HouseHunterError(f"Mountain elevation does not cover region tile {core}")
        elevation, transform = _read_elevation(
            tuple(elevation_paths[int(index)] for index in matching),
            bounds=bounds,
            target_crs=source.target_crs,
            cell_size_m=cell_size_m,
        )
        terrain = terrain_metrics(elevation, cell_size_m=cell_size_m)
        pad_geometry, pad_fields = _read_geometries(
            source.pad_us,
            target_bounds=bounds,
            target_crs=source.target_crs,
            columns=[source.pad_access_field],
            expected_type_ids=frozenset({3, 6}),
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
                merge_alg=MergeAlg.add,
            )
            if trail_shapes
            else np.zeros(elevation.shape, dtype=np.float32)
        )
        access = access_metrics(
            terrain["mountain_mask"],
            pad,
            trail_cells * cell_size_m / 1_000,
            cell_size_m=cell_size_m,
        )
        columns = np.floor((tile["x"].to_numpy() - bounds[0]) / cell_size_m).astype(int)
        rows = np.floor((bounds[3] - tile["y"].to_numpy()) / cell_size_m).astype(int)
        sampled = {
            key: _sample(values, rows, columns)
            for key, values in {**terrain, **access}.items()
            if key in RAW_PRECISION
        }
        outputs.append(
            tile.select("block_geoid", "tract_geoid", "county_fips", "state", "pop20").with_columns(
                *(pl.Series(name, values, nan_to_null=True) for name, values in sampled.items())
            )
        )
    return pl.concat(outputs).sort("block_geoid")
