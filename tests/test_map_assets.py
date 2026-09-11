from __future__ import annotations

import gzip
import json
import runpy
from pathlib import Path

import httpx
import pytest

from househunter import map_assets as map_assets_module
from househunter.config import canonical_json, load_config, sha256_bytes
from househunter.download import _schema_fingerprint
from househunter.errors import SourceContractError
from househunter.map_assets import (
    load_manifest,
    manifest_entry,
    map_asset_status,
    validate_polygon_geometry,
    validate_raw_provenance,
    write_raw_provenance,
)

generator = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "generate_map_assets.py")
)
fetch_geometry = generator["fetch_geometry"]
publish_assets = generator["publish_assets"]
validate_topology_inventory = generator["validate_topology_inventory"]


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


def test_geometry_download_rechecks_revision_after_pagination() -> None:
    fields = {
        "TRACTFIPS": "esriFieldTypeString",
        "STATEABBRV": "esriFieldTypeString",
    }
    source = {
        "name": "fixture",
        "item_id": "fixture",
        "layer_url": "https://example.test/layer/0",
        "item_modified_ms": 10,
        "data_last_edit_ms": 20,
        "layer_last_edit_ms": 30,
        "expected_row_count": 1,
        "fields": fields,
        "schema_fingerprint": _schema_fingerprint(fields),
    }
    metadata_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata_calls
        if "/sharing/" in request.url.path:
            return httpx.Response(200, json={"modified": 10})
        if request.url.path.endswith("query"):
            return httpx.Response(200, json={"features": [{
                "type": "Feature",
                "properties": {"TRACTFIPS": "01001000100", "STATEABBRV": "AL"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]},
            }]})
        metadata_calls += 1
        return httpx.Response(200, json={
            "maxRecordCount": 1,
            "geometryType": "esriGeometryPolygon",
            "editingInfo": {
                "lastEditDate": 30 + (metadata_calls - 1),
                "dataLastEditDate": 20,
            },
            "fields": [{"name": name, "type": kind} for name, kind in fields.items()],
        })

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(SourceContractError, match="source changed"),
    ):
        fetch_geometry(client, source, "TRACTFIPS,STATEABBRV")
    assert metadata_calls == 2


def test_generator_rejects_stale_topology_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(validate_topology_inventory.__globals__, "JURISDICTIONS", {"CO"})
    expected = {
        "tracts-national.topojson",
        "counties-national.topojson",
        "states-national.topojson",
        "tracts-co.topojson",
    }
    for filename in expected:
        (tmp_path / filename).write_text("{}")
    assert {path.name for path in validate_topology_inventory(tmp_path)} == expected
    (tmp_path / "tracts-stale.topojson").write_text("{}")
    with pytest.raises(RuntimeError, match="inventory"):
        validate_topology_inventory(tmp_path)


def test_generator_publishes_validated_candidate_and_prunes_old_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    output = tmp_path / "published"
    candidate.mkdir()
    output.mkdir()
    (candidate / "new.topojson.gz").write_bytes(b"new")
    (candidate / "manifest.json").write_text("new manifest")
    (output / "old.topojson.gz").write_bytes(b"old")
    (output / "manifest.json").write_text("old manifest")
    (output / "__init__.py").write_text("")
    monkeypatch.setitem(publish_assets.__globals__, "load_manifest", lambda _root: {})

    publish_assets(
        candidate,
        output,
        {"files": [{"filename": "new.topojson.gz"}]},
    )

    assert (output / "new.topojson.gz").read_bytes() == b"new"
    assert (output / "manifest.json").read_text() == "new manifest"
    assert not (output / "old.topojson.gz").exists()
    assert (output / "__init__.py").exists()


def test_generator_validation_failure_preserves_published_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    output = tmp_path / "published"
    candidate.mkdir()
    output.mkdir()
    (candidate / "new.topojson.gz").write_bytes(b"new")
    (candidate / "manifest.json").write_text("invalid manifest")
    (output / "old.topojson.gz").write_bytes(b"old")
    (output / "manifest.json").write_text("old manifest")

    def reject_candidate(_root: Path) -> None:
        raise ValueError("candidate is invalid")

    monkeypatch.setitem(publish_assets.__globals__, "load_manifest", reject_candidate)
    with pytest.raises(ValueError, match="candidate is invalid"):
        publish_assets(
            candidate,
            output,
            {"files": [{"filename": "new.topojson.gz"}]},
        )

    assert (output / "old.topojson.gz").read_bytes() == b"old"
    assert (output / "manifest.json").read_text() == "old manifest"
    assert not (output / "new.topojson.gz").exists()


def test_generator_rejects_unexpected_candidate_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    output = tmp_path / "published"
    candidate.mkdir()
    output.mkdir()
    (candidate / "new.topojson.gz").write_bytes(b"new")
    (candidate / "manifest.json").write_text("new manifest")
    (candidate / "stale.topojson.gz").write_bytes(b"stale")
    (output / "old.topojson.gz").write_bytes(b"old")
    (output / "manifest.json").write_text("old manifest")
    monkeypatch.setitem(publish_assets.__globals__, "load_manifest", lambda _root: {})

    with pytest.raises(RuntimeError, match="unexpected file inventory"):
        publish_assets(candidate, output, {"files": [{"filename": "new.topojson.gz"}]})

    assert (output / "old.topojson.gz").read_bytes() == b"old"
    assert (output / "manifest.json").read_text() == "old manifest"
    assert not (output / "new.topojson.gz").exists()


def test_generator_publish_failure_keeps_previous_manifest_and_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    output = tmp_path / "published"
    candidate.mkdir()
    output.mkdir()
    (candidate / "new.topojson.gz").write_bytes(b"new")
    (candidate / "manifest.json").write_text("new manifest")
    (output / "old.topojson.gz").write_bytes(b"old")
    (output / "manifest.json").write_text("old manifest")
    monkeypatch.setitem(publish_assets.__globals__, "load_manifest", lambda _root: {})
    replace = publish_assets.__globals__["os"].replace

    def fail_manifest(source: Path, destination: Path) -> None:
        if Path(source).name == "manifest.json":
            raise OSError("simulated publication interruption")
        replace(source, destination)

    monkeypatch.setattr(publish_assets.__globals__["os"], "replace", fail_manifest)
    with pytest.raises(OSError, match="publication interruption"):
        publish_assets(candidate, output, {"files": [{"filename": "new.topojson.gz"}]})

    assert (output / "old.topojson.gz").read_bytes() == b"old"
    assert (output / "manifest.json").read_text() == "old manifest"
    assert (output / "new.topojson.gz").read_bytes() == b"new"
