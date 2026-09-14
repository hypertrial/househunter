from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .build import (
    BUILD_SCHEMA_VERSION,
    build_snapshot,
    publish_snapshot,
    snapshot_artifacts_are_valid,
)
from .config import RuntimePaths, atomic_write_json, sha256_file
from .errors import HouseHunterError
from .mountain import (
    MIGRATION_JOURNAL,
    _compact_manifest,
    load_compact_release,
    publish_compact_pointer,
    publish_release_pointer,
    stage_compact_fallback,
    validate_release,
    write_and_stage_release,
)
from .mountain_gis import load_source_lock_contract, source_provenance_item
from .mountain_legacy_v1 import validate_legacy_v1_release
from .mountain_paths import ensure_safe_directory, lexical_path, require_owned_child

Progress = Callable[[int, str], None]
_JOURNAL_PHASES = {
    "staged",
    "full_published",
    "compact_published",
    "snapshot_published",
}
_JOURNAL_KEYS = {"phase", "release_id", "compact_release_id", "snapshot_id"}


def _read_pointer(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise HouseHunterError(f"Mountain migration pointer is missing or unsafe: {path}")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Mountain migration pointer is invalid: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise HouseHunterError("Mountain migration pointer schema is invalid")
    release_id = payload.get("release_id")
    if not isinstance(release_id, str) or re.fullmatch(r"[0-9a-f]{16}", release_id) is None:
        raise HouseHunterError("Mountain migration pointer release ID is invalid")
    return payload


def _load_journal(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise HouseHunterError("Mountain migration journal is missing or unsafe")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Mountain migration journal is invalid: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != _JOURNAL_KEYS:
        raise HouseHunterError("Mountain migration journal has an invalid contract")
    if payload.get("phase") not in _JOURNAL_PHASES:
        raise HouseHunterError("Mountain migration journal phase is invalid")
    for key in ("release_id", "compact_release_id"):
        value = payload.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{16}", value) is None:
            raise HouseHunterError("Mountain migration journal release ID is invalid")
    if payload["compact_release_id"] != payload["release_id"]:
        raise HouseHunterError("Mountain migration journal release identities disagree")
    snapshot_id = payload.get("snapshot_id")
    if (
        not isinstance(snapshot_id, str)
        or re.fullmatch(r"national-[0-9a-f]{16}", snapshot_id) is None
    ):
        raise HouseHunterError("Mountain migration journal snapshot ID is invalid")
    return {key: str(value) for key, value in payload.items()}


def _write_journal(path: Path, journal: dict[str, str], phase: str) -> None:
    if phase not in _JOURNAL_PHASES:
        raise HouseHunterError("Mountain migration journal phase is invalid")
    atomic_write_json(path, {**journal, "phase": phase})


def _remove_journal(path: Path) -> None:
    """Durably remove a completed transaction journal."""
    path.unlink()
    try:
        directory = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _validate_source_contract(
    manifest: dict[str, object],
    source_lock: dict[str, object],
    source_lock_sha256: str,
) -> None:
    sources = manifest.get("sources")
    expected_items = [
        source_provenance_item(item)
        for item in source_lock.get("sources", [])
        if isinstance(item, dict)
    ]
    if (
        not isinstance(sources, dict)
        or sources.get("source_lock_sha256") != source_lock_sha256
        or sources.get("items") != expected_items
        or manifest.get("national_expectations") != source_lock.get("expected_states")
        or manifest.get("block_geoid_sha256") != source_lock.get("block_geoid_sha256")
    ):
        raise HouseHunterError("Mountain v2 release differs from the reviewed source lock")


def _validate_staged_transaction(
    paths: RuntimePaths,
    journal: dict[str, str],
    source_lock: dict[str, object],
    source_lock_sha256: str,
) -> tuple[Path, Path, Path, dict[str, object]]:
    release_id = journal["release_id"]
    full_root = ensure_safe_directory(paths.data / "mountain" / "releases")
    compact_root = ensure_safe_directory(paths.data / "mountain" / "compact")
    if paths.builds.is_symlink():
        raise HouseHunterError("Mountain staged snapshot build directory cannot be a symlink")
    full = lexical_path(full_root / release_id)
    compact = lexical_path(compact_root / journal["compact_release_id"])
    snapshot = lexical_path(paths.builds / journal["snapshot_id"])
    require_owned_child(full, full_root, name_pattern=r"[0-9a-f]{16}")
    require_owned_child(compact, compact_root, name_pattern=r"[0-9a-f]{16}")
    manifest = validate_release(full)
    _validate_source_contract(manifest, source_lock, source_lock_sha256)
    compact_manifest, _, _ = load_compact_release(compact)
    expected_compact = _compact_manifest(
        manifest, full_manifest_sha256=sha256_file(full / "manifest.json")
    )
    if compact_manifest != expected_compact:
        raise HouseHunterError("Mountain staged compact identity disagrees")
    if snapshot.is_symlink() or not snapshot.resolve().is_relative_to(paths.builds.resolve()):
        raise HouseHunterError("Mountain staged snapshot escapes the build directory")
    if not snapshot_artifacts_are_valid(snapshot):
        raise HouseHunterError("Mountain staged snapshot failed validation")
    try:
        snapshot_metadata = json.loads((snapshot / "build.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Mountain staged snapshot metadata is invalid: {exc}") from exc
    if (
        snapshot_metadata.get("schema_version") != BUILD_SCHEMA_VERSION
        or snapshot_metadata.get("build_id") != journal["snapshot_id"]
        or snapshot_metadata.get("mountain_release_id") != release_id
        or snapshot_metadata.get("mountain_magnitude_version") != manifest.get("magnitude_version")
    ):
        raise HouseHunterError("Mountain staged snapshot identity disagrees")
    return full, compact, snapshot, manifest


def _finish_transaction(
    paths: RuntimePaths,
    journal_path: Path,
    journal: dict[str, str],
    source_lock: dict[str, object],
    source_lock_sha256: str,
) -> dict[str, object]:
    full, compact, snapshot, manifest = _validate_staged_transaction(
        paths, journal, source_lock, source_lock_sha256
    )
    del full, compact
    release_id = journal["release_id"]
    full_pointer = paths.data / "mountain" / "current.json"
    compact_pointer = paths.data / "mountain" / "compact" / "current.json"
    if full_pointer.is_symlink() or compact_pointer.is_symlink() or paths.current.is_symlink():
        raise HouseHunterError("Mountain migration cannot publish through symlinked pointers")

    if not full_pointer.is_file() or _read_pointer(full_pointer)["release_id"] != release_id:
        publish_release_pointer(paths, release_id)
    _write_journal(journal_path, journal, "full_published")
    if not compact_pointer.is_file() or _read_pointer(compact_pointer)["release_id"] != release_id:
        publish_compact_pointer(paths, release_id)
    _write_journal(journal_path, journal, "compact_published")

    expected_snapshot_pointer = {
        "schema_version": BUILD_SCHEMA_VERSION,
        "build_id": journal["snapshot_id"],
        "scope": "national",
        "path": str(snapshot),
    }
    snapshot_current = False
    if paths.current.is_file() and not paths.current.is_symlink():
        try:
            current = json.loads(paths.current.read_text())
            snapshot_current = current == expected_snapshot_pointer
        except (OSError, json.JSONDecodeError):
            snapshot_current = False
    if not snapshot_current:
        publish_snapshot(paths, snapshot)
    _write_journal(journal_path, journal, "snapshot_published")

    if (
        _read_pointer(full_pointer)["release_id"] != release_id
        or _read_pointer(compact_pointer)["release_id"] != release_id
    ):
        raise HouseHunterError("Mountain migration pointers do not share one release identity")
    try:
        snapshot_pointer = json.loads(paths.current.read_text())
        snapshot_metadata = json.loads((snapshot / "build.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HouseHunterError(f"Mountain migration snapshot pointer is invalid: {exc}") from exc
    if (
        snapshot_pointer != expected_snapshot_pointer
        or snapshot_metadata.get("mountain_release_id") != release_id
    ):
        raise HouseHunterError("Mountain migration snapshot does not match its release")
    return {"snapshot": snapshot, "manifest": manifest}


def rescore_v1_release(
    paths: RuntimePaths,
    source_lock_path: Path,
    *,
    progress: Progress | None = None,
) -> dict[str, object]:
    """Offline, forward-recoverable migration of the active full v1 release."""
    if paths.builds.is_symlink():
        raise HouseHunterError("Mountain migration build directory cannot be a symlink")
    paths.ensure()
    if source_lock_path.is_symlink() or not source_lock_path.is_file():
        raise HouseHunterError("Mountain rescore source lock must be a regular file")
    source_lock = load_source_lock_contract(source_lock_path, require_v2=True)
    source_lock_sha256 = sha256_file(source_lock_path)
    mountain_root = ensure_safe_directory(paths.data / "mountain")
    journal_path = mountain_root / MIGRATION_JOURNAL
    if journal_path.is_symlink():
        raise HouseHunterError("Mountain migration journal is unsafe")
    legacy_release_id: str | None = None

    if journal_path.exists():
        journal = _load_journal(journal_path)
        if progress:
            progress(80, "Recovering staged Mountain v2 publication")
    else:
        pointer = _read_pointer(mountain_root / "current.json")
        legacy_release_id = str(pointer["release_id"])
        releases = ensure_safe_directory(mountain_root / "releases")
        legacy = lexical_path(releases / legacy_release_id)
        require_owned_child(legacy, releases, name_pattern=r"[0-9a-f]{16}")
        compact_pointer = mountain_root / "compact" / "current.json"
        if compact_pointer.exists() or compact_pointer.is_symlink():
            compact = _read_pointer(compact_pointer)
            if compact["release_id"] != legacy_release_id:
                raise HouseHunterError("Mountain v1 full and compact pointers disagree")
        if progress:
            progress(10, "Validating active Mountain v1 release")
        legacy_manifest, raw_blocks = validate_legacy_v1_release(
            legacy,
            reviewed_source_lock=source_lock,
            reviewed_source_lock_sha256=source_lock_sha256,
        )
        if legacy_manifest.get("release_id") != legacy_release_id:
            raise HouseHunterError(
                "Mountain v1 pointer does not match the validated release identity"
            )
        if progress:
            progress(35, "Rescoring validated raw blocks as Mountain Magnitude v2")
        full, manifest = write_and_stage_release(
            paths,
            raw_blocks,
            data_release=str(legacy_manifest["data_release"]),
            sources=dict(legacy_manifest["sources"]),
            national_expectations=dict(legacy_manifest["national_expectations"]),
        )
        if progress:
            progress(60, "Staging compact Mountain v2 runtime artifact")
        compact = stage_compact_fallback(paths, full, manifest)
        compact_manifest, tracts, counties = load_compact_release(compact)
        if progress:
            progress(70, "Staging schema-9 HouseHunter snapshot")
        snapshot = build_snapshot(
            paths,
            mountain_release=(compact, compact_manifest, tracts, counties),
            publish=False,
        )
        journal = {
            "phase": "staged",
            "release_id": str(manifest["release_id"]),
            "compact_release_id": str(manifest["release_id"]),
            "snapshot_id": snapshot.name,
        }
        _validate_staged_transaction(paths, journal, source_lock, source_lock_sha256)
        _write_journal(journal_path, journal, "staged")

    _finish_transaction(paths, journal_path, journal, source_lock, source_lock_sha256)
    reports = ensure_safe_directory(mountain_root / "reports" / "migrations")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lineage = reports / f"{journal['release_id']}-{timestamp}.json"
    atomic_write_json(
        lineage,
        {
            "legacy_release_id": legacy_release_id or "recovery",
            "release_id": journal["release_id"],
            "snapshot_id": journal["snapshot_id"],
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    _remove_journal(journal_path)
    if progress:
        progress(100, "Mountain Magnitude v2 migration published")
    return {
        "legacy_release_id": legacy_release_id,
        "release_id": journal["release_id"],
        "compact_release_id": journal["compact_release_id"],
        "snapshot_id": journal["snapshot_id"],
        "lineage_report": str(lineage),
    }
