from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from househunter import map_assets as map_assets_module
from househunter.config import canonical_json, load_config, sha256_bytes
from househunter.map_assets import (
    load_manifest,
    manifest_entry,
    map_asset_status,
    validate_polygon_geometry,
    validate_raw_provenance,
    write_raw_provenance,
)


def write_assets(root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(map_assets_module, "EXPECTED_TRACTS", 1)
    monkeypatch.setattr(map_assets_module, "EXPECTED_COUNTIES", 1)
    monkeypatch.setattr(map_assets_module, "JURISDICTIONS", frozenset({"CO"}))
    entries = []

    def add(key: str, level: str, lod: str, jurisdiction: str | None, place_id: str) -> str:
        line = level == "state"
        payload = canonical_json(
            {
                "type": "Topology",
                "objects": {
                    "geography": {
                        "type": "GeometryCollection",
                        "geometries": [
                            {
                                "type": "LineString" if line else "Polygon",
                                "id": place_id,
                                "properties": {"state": "CO", "place_id": place_id},
                                "arcs": [0] if line else [[0]],
                            }
                        ],
                    }
                },
                "arcs": [[[0, 0], [1, 0], [0, 1], [-1, 0], [0, -1]]],
            }
        )
        compressed = gzip.compress(payload, mtime=0)
        digest = sha256_bytes(compressed)
        filename = f"{key}.{digest[:16]}.topojson.gz"
        (root / filename).write_bytes(compressed)
        entries.append(
            {
                "key": key,
                "filename": filename,
                "level": level,
                "lod": lod,
                "jurisdiction": jurisdiction,
                "feature_count": 1,
                "bounds": [-1, -1, 1, 1],
                "compressed_size": len(compressed),
                "sha256": digest,
            }
        )
        return filename

    tract_filename = add("tracts-national", "tract", "national", None, "08013012101")
    add("counties-national", "county", "national", None, "08013")
    add("states-national", "state", "national", None, "CO")
    add("tracts-co", "tract", "detail", "CO", "08013012101")
    config = load_config()
    manifest = {
        "schema_version": 1,
        "release": config["fema"]["release"],
        "sources": {
            key: {
                name: config[key][name]
                for name in (
                    "item_id",
                    "item_modified_ms",
                    "data_last_edit_ms",
                    "layer_last_edit_ms",
                )
            }
            for key in ("fema", "fema_counties")
        },
        "files": entries,
        "initial_compressed_size": sum(
            entry["compressed_size"] for entry in entries if entry["lod"] == "national"
        ),
    }
    (root / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    return tract_filename


def test_manifest_validates_pinned_sources_and_content(
    fixture_environment: tuple[object, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "assets"
    root.mkdir()
    filename = write_assets(root, monkeypatch)
    manifest = load_manifest(root)
    assert manifest["schema_version"] == 1
    assert map_asset_status(root).ready is True
    assert manifest_entry(filename, root)[0].name == filename
    with pytest.raises(ValueError, match="Invalid map asset path"):
        manifest_entry(f"../{filename}", root)


def test_manifest_rejects_revision_drift_and_corruption(
    fixture_environment: tuple[object, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "assets"
    root.mkdir()
    filename = write_assets(root, monkeypatch)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sources"]["fema"]["item_modified_ms"] += 1
    manifest_path.write_bytes(canonical_json(manifest) + b"\n")
    assert "pinned FEMA revisions" in (map_asset_status(root).error or "")
    filename = write_assets(root, monkeypatch)
    original = (root / filename).read_bytes()
    (root / filename).write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    assert "checksum mismatch" in (map_asset_status(root).error or "")
    with pytest.raises(ValueError, match="checksum mismatch"):
        manifest_entry(filename, root)

    filename = write_assets(root, monkeypatch)
    (root / filename).unlink()
    assert "missing" in (map_asset_status(root).error or "").lower()


def test_manifest_rejects_incomplete_inventory(
    fixture_environment: tuple[object, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "assets"
    root.mkdir()
    write_assets(root, monkeypatch)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"] = [entry for entry in manifest["files"] if entry["key"] != "tracts-co"]
    manifest_path.write_bytes(canonical_json(manifest) + b"\n")
    assert "incomplete asset set" in (map_asset_status(root).error or "")


def test_packaged_manifest_has_complete_identity_sets() -> None:
    manifest = load_manifest()
    entries = {entry["key"]: entry for entry in manifest["files"]}
    assert entries["tracts-national"]["feature_count"] == 85_154
    assert entries["counties-national"]["feature_count"] == 3_232
    assert entries["states-national"]["feature_count"] == 56
    assert len([entry for entry in manifest["files"] if entry["lod"] == "detail"]) == 56


def test_raw_geometry_provenance_and_polygon_validation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "tracts.geojson").write_text("tracts")
    (raw / "counties.geojson").write_text("counties")
    sources = {"fema": {"item_id": "one"}, "fema_counties": {"item_id": "two"}}
    write_raw_provenance(raw, sources)
    validate_raw_provenance(raw, sources)
    with pytest.raises(ValueError, match="pinned FEMA revisions"):
        validate_raw_provenance(raw, {**sources, "fema": {"item_id": "changed"}})
    (raw / "tracts.geojson").write_text("changed")
    with pytest.raises(ValueError, match="changed after download"):
        validate_raw_provenance(raw, sources)
    (raw / "provenance.json").unlink()
    with pytest.raises(ValueError, match="missing or invalid"):
        validate_raw_provenance(raw, sources)
    with pytest.raises(ValueError, match="non-polygon"):
        validate_polygon_geometry(
            {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
        )
