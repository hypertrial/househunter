from __future__ import annotations

import gzip
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import canonical_json, load_config, sha256_file

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
RAW_PROVENANCE_NAME = "provenance.json"
EXPECTED_TRACTS = 85_154
EXPECTED_COUNTIES = 3_232
JURISDICTIONS = frozenset(
    {
        "AK", "AL", "AR", "AS", "AZ", "CA", "CO", "CT", "DC", "DE", "FL", "GA",
        "GU", "HI", "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME",
        "MI", "MN", "MO", "MP", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM",
        "NV", "NY", "OH", "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX",
        "UT", "VA", "VI", "VT", "WA", "WI", "WV", "WY",
    }
)
ENTRY_FIELDS = {
    "key",
    "filename",
    "level",
    "lod",
    "jurisdiction",
    "feature_count",
    "bounds",
    "compressed_size",
    "sha256",
}


@dataclass(frozen=True)
class MapAssetStatus:
    ready: bool
    error: str | None
    schema_version: int | None
    release: str | None
    manifest_url: str

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "error": self.error,
            "schema_version": self.schema_version,
            "release": self.release,
            "manifest_url": self.manifest_url,
        }


def asset_directory() -> Path:
    return Path(__file__).resolve().parent


def source_revisions(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    current = config or load_config()
    return {
        key: {
            "item_id": current[key]["item_id"],
            "item_modified_ms": current[key]["item_modified_ms"],
            "data_last_edit_ms": current[key]["data_last_edit_ms"],
            "layer_last_edit_ms": current[key]["layer_last_edit_ms"],
        }
        for key in ("fema", "fema_counties")
    }


def validate_polygon_geometry(geometry: object) -> None:
    if not isinstance(geometry, dict) or geometry.get("type") not in {
        "Polygon",
        "MultiPolygon",
    }:
        raise ValueError("FEMA geometry contains a non-polygon feature")


def write_raw_provenance(raw_dir: Path, sources: dict[str, dict[str, Any]]) -> None:
    payload = {
        "schema_version": 1,
        "sources": sources,
        "files": {
            name: sha256_file(raw_dir / name) for name in ("tracts.geojson", "counties.geojson")
        },
    }
    (raw_dir / RAW_PROVENANCE_NAME).write_bytes(canonical_json(payload) + b"\n")


def validate_raw_provenance(raw_dir: Path, sources: dict[str, dict[str, Any]]) -> None:
    try:
        payload = json.loads((raw_dir / RAW_PROVENANCE_NAME).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Raw map geometry provenance is missing or invalid; fetch it again"
        ) from exc
    if payload.get("schema_version") != 1 or payload.get("sources") != sources:
        raise ValueError(
            "Raw map geometry does not match the pinned FEMA revisions; fetch it again"
        )
    expected_files = payload.get("files")
    if not isinstance(expected_files, dict) or set(expected_files) != {
        "tracts.geojson",
        "counties.geojson",
    }:
        raise ValueError("Raw map geometry provenance has an invalid file set")
    for name, digest in expected_files.items():
        path = raw_dir / name
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"Raw map geometry changed after download: {name}")


def _topology_ids(path: Path, entry: dict[str, Any]) -> set[str]:
    try:
        topology = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Map asset is corrupt: {path.name}: {exc}") from exc
    if not isinstance(topology, dict) or topology.get("type") != "Topology":
        raise ValueError(f"Map asset is not TopoJSON: {path.name}")
    geography = topology.get("objects", {}).get("geography", {})
    geometries = geography.get("geometries")
    if geography.get("type") != "GeometryCollection" or not isinstance(geometries, list):
        raise ValueError(f"Map asset has no geography collection: {path.name}")
    ids = [str(item.get("id", "")) for item in geometries if isinstance(item, dict)]
    if len(ids) != len(geometries) or not all(ids) or len(ids) != len(set(ids)):
        raise ValueError(f"Map asset has missing or duplicate identifiers: {path.name}")
    if len(ids) != entry["feature_count"]:
        raise ValueError(f"Map asset feature count mismatch: {path.name}")
    allowed_types = {"LineString", "MultiLineString"} if entry["level"] == "state" else {
        "Polygon",
        "MultiPolygon",
    }
    for geometry in geometries:
        if geometry.get("type") not in allowed_types:
            raise ValueError(f"Map asset has an invalid geometry type: {path.name}")
        properties = geometry.get("properties", {})
        if not isinstance(properties, dict) or any(
            key == "risk_score" or "npctl" in key.lower() for key in properties
        ):
            raise ValueError(f"Map asset contains score data: {path.name}")
    return set(ids)


def load_manifest(directory: Path | None = None, *, verify_files: bool = True) -> dict[str, Any]:
    root = (directory or asset_directory()).resolve()
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Map asset manifest is unavailable or invalid: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Map asset manifest schema is unsupported")
    config = load_config()
    expected_sources = source_revisions(config)
    if manifest.get("sources") != expected_sources:
        raise ValueError("Map boundaries do not match the pinned FEMA revisions; regenerate assets")
    if manifest.get("release") != config["fema"]["release"]:
        raise ValueError("Map asset manifest release does not match the pinned FEMA release")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Map asset manifest contains no files")
    expected_keys = {"tracts-national", "counties-national", "states-national"} | {
        f"tracts-{state.lower()}" for state in JURISDICTIONS
    }
    if len(files) != len(expected_keys):
        raise ValueError("Map asset manifest has an incomplete asset set")
    seen_names: set[str] = set()
    seen_keys: set[str] = set()
    topology_ids: dict[str, set[str]] = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise ValueError("Map asset manifest contains a malformed file entry")
        name = entry.get("filename")
        key = entry.get("key")
        digest = entry.get("sha256")
        if not isinstance(key, str) or key not in expected_keys or key in seen_keys:
            raise ValueError("Map asset manifest contains an unknown or duplicate key")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in seen_names
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or name != f"{key}.{digest[:16]}.topojson.gz"
        ):
            raise ValueError("Map asset manifest contains an unsafe or duplicate filename")
        seen_names.add(name)
        seen_keys.add(key)
        count = entry.get("feature_count")
        size = entry.get("compressed_size")
        bounds = entry.get("bounds")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(bounds, list)
            or len(bounds) != 4
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in bounds
            )
        ):
            raise ValueError("Map asset manifest contains invalid counts, sizes, or bounds")
        if key == "tracts-national":
            expected = ("tract", "national", None, EXPECTED_TRACTS)
        elif key == "counties-national":
            expected = ("county", "national", None, EXPECTED_COUNTIES)
        elif key == "states-national":
            expected = ("state", "national", None, len(JURISDICTIONS))
        else:
            expected = ("tract", "detail", key.removeprefix("tracts-").upper(), count)
        if (entry.get("level"), entry.get("lod"), entry.get("jurisdiction"), count) != expected:
            raise ValueError(f"Map asset manifest metadata mismatch: {key}")
        if not verify_files:
            continue
        path = root / name
        if not path.is_file():
            raise ValueError(f"Map asset is missing: {name}")
        if path.stat().st_size != entry.get("compressed_size"):
            raise ValueError(f"Map asset size mismatch: {name}")
        if sha256_file(path) != entry.get("sha256"):
            raise ValueError(f"Map asset checksum mismatch: {name}")
        topology_ids[key] = _topology_ids(path, entry)
    if seen_keys != expected_keys:
        raise ValueError("Map asset manifest has an incomplete asset set")
    detail_entries = [entry for entry in files if entry["lod"] == "detail"]
    if {entry["jurisdiction"] for entry in detail_entries} != JURISDICTIONS:
        raise ValueError("Map asset manifest has incomplete jurisdiction coverage")
    if sum(entry["feature_count"] for entry in detail_entries) != EXPECTED_TRACTS:
        raise ValueError("Detailed tract feature counts do not match the national topology")
    national_size = sum(entry["compressed_size"] for entry in files if entry["lod"] == "national")
    if manifest.get("initial_compressed_size") != national_size or national_size > 15 * 1024 * 1024:
        raise ValueError("Map asset manifest initial payload size is invalid")
    if any(entry["compressed_size"] > 5 * 1024 * 1024 for entry in detail_entries):
        raise ValueError("Map asset manifest contains an oversized detail asset")
    if verify_files:
        detail_ids: set[str] = set()
        for entry in detail_entries:
            ids = topology_ids[entry["key"]]
            if detail_ids & ids:
                raise ValueError("Detailed tract assets contain duplicate identifiers")
            detail_ids.update(ids)
        if detail_ids != topology_ids["tracts-national"]:
            raise ValueError("Detailed tract identifiers do not match the national topology")
        if topology_ids["states-national"] != JURISDICTIONS:
            raise ValueError("State topology identifiers do not match supported jurisdictions")
    return manifest


def topology_ids(
    manifest: dict[str, Any], key: str, directory: Path | None = None
) -> set[str]:
    entry = next((item for item in manifest["files"] if item["key"] == key), None)
    if entry is None:
        raise ValueError(f"Map asset manifest is missing {key}")
    return _topology_ids((directory or asset_directory()) / entry["filename"], entry)


def map_asset_status(directory: Path | None = None) -> MapAssetStatus:
    try:
        manifest = load_manifest(directory)
    except ValueError as exc:
        return MapAssetStatus(False, str(exc), None, None, "/map-assets/manifest.json")
    return MapAssetStatus(
        True,
        None,
        SCHEMA_VERSION,
        str(manifest["release"]),
        "/map-assets/manifest.json",
    )


def manifest_entry(
    filename: str,
    directory: Path | None = None,
    *,
    verify_content: bool = True,
) -> tuple[Path, dict[str, Any]]:
    if Path(filename).name != filename:
        raise ValueError("Invalid map asset path")
    manifest = load_manifest(directory, verify_files=False)
    entry = next((item for item in manifest["files"] if item["filename"] == filename), None)
    if entry is None:
        raise ValueError("Map asset is not listed in the manifest")
    path = (directory or asset_directory()) / filename
    if not path.is_file() or path.stat().st_size != entry["compressed_size"]:
        raise ValueError("Map asset is missing or has the wrong size")
    if not verify_content:
        return path, entry
    if sha256_file(path) != entry["sha256"]:
        raise ValueError("Map asset checksum mismatch")
    try:
        topology = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Map asset is corrupt: {exc}") from exc
    if topology.get("type") != "Topology":
        raise ValueError("Map asset is not TopoJSON")
    return path, entry
