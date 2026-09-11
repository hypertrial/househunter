#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from househunter.config import RuntimePaths, canonical_json, load_config, sha256_bytes
from househunter.download import (
    _validate_layer,
    validate_cached_fema,
    validate_cached_fema_counties,
)
from househunter.map_assets import (
    JURISDICTIONS,
    MANIFEST_NAME,
    SCHEMA_VERSION,
    asset_directory,
    load_manifest,
    source_revisions,
    validate_polygon_geometry,
    validate_raw_provenance,
    write_raw_provenance,
)


def request_json(client: httpx.Client, url: str, params: dict[str, object]) -> dict[str, Any]:
    response = client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "error" in payload:
        raise RuntimeError(f"ArcGIS returned an invalid response: {payload!r}")
    return payload


def fetch_geometry(
    client: httpx.Client, source: dict[str, Any], fields: str
) -> list[dict[str, Any]]:
    page_size = _validate_layer(
        client, source, expected_geometry_type="esriGeometryPolygon"
    )
    features: list[dict[str, Any]] = []
    for offset in range(0, int(source["expected_row_count"]), page_size):
        payload = request_json(
            client,
            f"{source['layer_url']}/query",
            {
                "f": "geojson",
                "where": "1=1",
                "outFields": fields,
                "returnGeometry": "true",
                "outSR": 4326,
                "orderByFields": fields.split(",")[0],
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "geometryPrecision": 5,
                "maxAllowableOffset": 0.0005,
            },
        )
        page = payload.get("features")
        if not isinstance(page, list):
            raise RuntimeError("ArcGIS geometry response has no feature list")
        features.extend(page)
        print(f"{source['name']}: {len(features):,}/{source['expected_row_count']:,}", flush=True)
    _validate_layer(client, source, expected_geometry_type="esriGeometryPolygon")
    return features


def coordinates(geometry: dict[str, Any]) -> Iterable[tuple[float, float]]:
    stack = [geometry.get("coordinates")]
    while stack:
        value = stack.pop()
        if (
            isinstance(value, list)
            and len(value) >= 2
            and all(isinstance(item, (int, float)) for item in value[:2])
        ):
            yield float(value[0]), float(value[1])
        elif isinstance(value, list):
            stack.extend(value)


def prepare_features(
    features: list[dict[str, Any]], *, id_field: str
) -> tuple[set[str], list[float]]:
    ids: set[str] = set()
    bounds = [180.0, 90.0, -180.0, -90.0]
    for feature in features:
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            raise RuntimeError("FEMA geometry contains a malformed feature")
        validate_polygon_geometry(geometry)
        place_id = str(properties.get(id_field, ""))
        if place_id in ids:
            raise RuntimeError(f"FEMA geometry contains duplicate identifier {place_id}")
        points = list(coordinates(geometry))
        if not points:
            raise RuntimeError(f"FEMA geometry is empty for {place_id}")
        ids.add(place_id)
        feature["id"] = place_id
        properties["place_id"] = properties.pop(id_field)
        properties["state"] = properties.pop("STATEABBRV")
        if "STCOFIPS" in properties:
            properties["county_fips"] = properties.pop("STCOFIPS")
        if "COUNTY" in properties:
            properties["name"] = properties.pop("COUNTY")
        if "STATE" in properties:
            properties["state_name"] = properties.pop("STATE")
        for x, y in points:
            bounds[0] = min(bounds[0], x)
            bounds[1] = min(bounds[1], y)
            bounds[2] = max(bounds[2], x)
            bounds[3] = max(bounds[3], y)
    features.sort(key=lambda feature: feature["id"])
    return ids, bounds


def gzip_deterministic(payload: bytes) -> bytes:
    return gzip.compress(payload, compresslevel=9, mtime=0)


def validate_topology(path: Path, expected_ids: set[str]) -> None:
    payload = json.loads(path.read_text())
    if payload.get("type") != "Topology":
        raise RuntimeError(f"Generated asset is not TopoJSON: {path.name}")
    geography = payload.get("objects", {}).get("geography", {})
    geometries = geography.get("geometries")
    if geography.get("type") != "GeometryCollection" or not isinstance(geometries, list):
        raise RuntimeError(f"Generated asset has no geography collection: {path.name}")
    ids = [str(item.get("id", "")) for item in geometries]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"Generated asset has duplicate identifiers: {path.name}")
    if set(ids) != expected_ids:
        raise RuntimeError(f"Generated asset identifier mismatch: {path.name}")

    def arc_references(value: object) -> Iterable[int]:
        if isinstance(value, int):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from arc_references(item)

    arc_count = len(payload.get("arcs", []))
    for geometry in geometries:
        references = list(arc_references(geometry.get("arcs")))
        if not references:
            raise RuntimeError(
                f"Generated asset has empty geometry {geometry.get('id')}: {path.name}"
            )
        if any(
            (reference if reference >= 0 else ~reference) >= arc_count
            for reference in references
        ):
            raise RuntimeError(f"Generated asset has an invalid arc reference: {path.name}")


def expected_topology_stems() -> set[str]:
    return {"tracts-national", "counties-national", "states-national"} | {
        f"tracts-{state.lower()}" for state in JURISDICTIONS
    }


def validate_topology_inventory(work_dir: Path) -> list[Path]:
    topology_files = sorted(work_dir.glob("*.topojson"))
    actual_stems = {path.stem for path in topology_files}
    if actual_stems != expected_topology_stems():
        raise RuntimeError("Generated topology inventory is incomplete or unexpected")
    return topology_files


def publish_assets(candidate: Path, output: Path, manifest: dict[str, Any]) -> None:
    expected_files = {entry["filename"] for entry in manifest["files"]} | {MANIFEST_NAME}
    actual_files = {path.name for path in candidate.iterdir()}
    if actual_files != expected_files:
        raise RuntimeError("Generated map candidate has an unexpected file inventory")
    load_manifest(candidate)
    output.mkdir(parents=True, exist_ok=True)
    for filename in sorted(expected_files - {MANIFEST_NAME}):
        os.replace(candidate / filename, output / filename)
    os.replace(candidate / MANIFEST_NAME, output / MANIFEST_NAME)
    for old in output.glob("*.topojson.gz"):
        if old.name not in expected_files:
            old.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate pinned FEMA TopoJSON map assets")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, default=asset_directory())
    parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args()
    paths = RuntimePaths.from_root()
    if args.data_dir:
        os.environ["HOUSEHUNTER_DATA_DIR"] = str(args.data_dir.resolve())
        paths = RuntimePaths.from_root()
    config = load_config()
    tract_frame, _ = validate_cached_fema(paths.cache / "fema_nri_tracts.parquet", config["fema"])
    county_frame, _ = validate_cached_fema_counties(
        paths.cache / "fema_nri_counties.parquet", config["fema_counties"]
    )
    raw_dir = paths.cache / "map-geometry"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tract_raw = raw_dir / "tracts.geojson"
    county_raw = raw_dir / "counties.geojson"
    if args.reuse_raw:
        with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
            for key in ("fema", "fema_counties"):
                _validate_layer(
                    client,
                    config[key],
                    expected_geometry_type="esriGeometryPolygon",
                )
        validate_raw_provenance(raw_dir, source_revisions(config))
        tracts = json.loads(tract_raw.read_text())["features"]
        counties = json.loads(county_raw.read_text())["features"]
        for feature in tracts:
            properties = feature["properties"]
            properties["TRACTFIPS"] = properties.pop("place_id")
            properties["STATEABBRV"] = properties.pop("state")
            properties["STCOFIPS"] = properties.pop("county_fips")
        for feature in counties:
            properties = feature["properties"]
            properties["STCOFIPS"] = properties.pop("place_id")
            properties["STATEABBRV"] = properties.pop("state")
            properties["STATE"] = properties.pop("state_name")
            properties["COUNTY"] = properties.pop("name")
    else:
        with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
            tracts = fetch_geometry(client, config["fema"], "TRACTFIPS,STATEABBRV,STCOFIPS")
            counties = fetch_geometry(
                client, config["fema_counties"], "STCOFIPS,STATEABBRV,STATE,COUNTY"
            )
            for key in ("fema", "fema_counties"):
                _validate_layer(
                    client,
                    config[key],
                    expected_geometry_type="esriGeometryPolygon",
                )
    tract_ids, tract_bounds = prepare_features(tracts, id_field="TRACTFIPS")
    county_ids, county_bounds = prepare_features(counties, id_field="STCOFIPS")
    if tract_ids != set(tract_frame["tract_id"].to_list()):
        raise RuntimeError("Tract geometry identifiers do not exactly match pinned FEMA attributes")
    if county_ids != set(county_frame["county_fips"].to_list()):
        raise RuntimeError(
            "County geometry identifiers do not exactly match pinned FEMA attributes"
        )
    states = {feature["properties"]["state"] for feature in tracts}
    if states != JURISDICTIONS:
        expected = sorted(JURISDICTIONS)
        actual = sorted(states)
        raise RuntimeError(
            f"Jurisdiction coverage mismatch: expected {expected}, got {actual}"
        )
    tract_raw.write_bytes(canonical_json({"type": "FeatureCollection", "features": tracts}))
    county_raw.write_bytes(canonical_json({"type": "FeatureCollection", "features": counties}))
    write_raw_provenance(raw_dir, source_revisions(config))
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".map-assets-", dir=output.parent) as temporary:
        temporary_root = Path(temporary)
        work_dir = temporary_root / "topologies"
        candidate = temporary_root / "candidate"
        work_dir.mkdir()
        candidate.mkdir()
        helper = Path(__file__).resolve().parents[1] / "web" / "scripts" / "build-topologies.mjs"
        subprocess.run(
            ["node", str(helper), str(tract_raw), str(county_raw), str(work_dir)], check=True
        )
        topology_files = validate_topology_inventory(work_dir)

        entries: list[dict[str, Any]] = []
        for source_path in topology_files:
            stem = source_path.stem
            payload = source_path.read_bytes()
            compressed = gzip_deterministic(payload)
            digest = sha256_bytes(compressed)
            filename = f"{stem}.{digest[:16]}.topojson.gz"
            (candidate / filename).write_bytes(compressed)
            if stem == "tracts-national":
                level, lod, jurisdiction, count, bounds = (
                    "tract",
                    "national",
                    None,
                    len(tracts),
                    tract_bounds,
                )
                expected_ids = tract_ids
            elif stem == "counties-national":
                level, lod, jurisdiction, count, bounds = (
                    "county",
                    "national",
                    None,
                    len(counties),
                    county_bounds,
                )
                expected_ids = county_ids
            elif stem == "states-national":
                level, lod, jurisdiction, count, bounds = (
                    "state",
                    "national",
                    None,
                    len(states),
                    county_bounds,
                )
                expected_ids = states
            else:
                jurisdiction = stem.removeprefix("tracts-").upper()
                level, lod = "tract", "detail"
                expected_ids = {
                    feature["id"]
                    for feature in tracts
                    if feature["properties"]["state"] == jurisdiction
                }
                count = len(expected_ids)
                state_points = [
                    point
                    for feature in tracts
                    if feature["properties"]["state"] == jurisdiction
                    for point in coordinates(feature["geometry"])
                ]
                bounds = [
                    min(x for x, _ in state_points),
                    min(y for _, y in state_points),
                    max(x for x, _ in state_points),
                    max(y for _, y in state_points),
                ]
            validate_topology(source_path, expected_ids)
            if lod == "detail" and len(compressed) > 5 * 1024 * 1024:
                raise RuntimeError(f"Regional map asset exceeds 5 MiB: {filename}")
            entries.append(
                {
                    "key": stem,
                    "filename": filename,
                    "level": level,
                    "lod": lod,
                    "jurisdiction": jurisdiction,
                    "feature_count": count,
                    "bounds": [round(value, 5) for value in bounds],
                    "compressed_size": len(compressed),
                    "sha256": digest,
                }
            )
        initial_size = sum(
            entry["compressed_size"] for entry in entries if entry["lod"] == "national"
        )
        if initial_size > 15 * 1024 * 1024:
            raise RuntimeError(f"Initial map assets exceed 15 MiB: {initial_size:,} bytes")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "release": config["fema"]["release"],
            "sources": source_revisions(config),
            "files": entries,
            "initial_compressed_size": initial_size,
        }
        (candidate / MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")
        publish_assets(candidate, output, manifest)
    print(f"Wrote {len(entries)} assets; initial payload {initial_size / 1024 / 1024:.2f} MiB")


if __name__ == "__main__":
    main()
