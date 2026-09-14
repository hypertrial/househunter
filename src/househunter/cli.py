from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .api import create_app
from .build import build_snapshot
from .chrr import download_chrr
from .config import RuntimePaths, load_config, sha256_file
from .download import download_fema, download_fema_counties, source_statuses
from .errors import AmbiguousPlaceError, HouseHunterError
from .geocode import lookup_address
from .geography import STATE_BY_FIPS
from .locking import exclusive_lock
from .store import Store

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
mountain_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
app.add_typer(
    mountain_app, name="mountain", help="Build and inspect Mountain Magnitude releases."
)


def _paths() -> RuntimePaths:
    return RuntimePaths.from_root()


def _progress(value: int, message: str) -> None:
    typer.echo(f"[{value:3d}%] {message}", err=True)


def _abort(exc: HouseHunterError) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1) from exc


def _release_slug(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise HouseHunterError(
            "--data-release must contain only letters, numbers, dots, underscores, or hyphens"
        )
    return value.lower()


def _record_cleanup_warning(
    label: str, exc: HouseHunterError | OSError, warnings: list[str]
) -> None:
    warning = f"Mountain {label} cleanup pending: {exc}"
    warnings.append(warning)
    typer.echo(f"Warning: {warning}", err=True)


def _rebuild_after_mountain_promotion(
    paths: RuntimePaths,
    previous_pointer: bytes | None,
    promoted_release: Path,
    release_manifest: dict[str, object],
) -> tuple[Path, Path, list[str]]:
    """Publish compact/runtime views or restore both Mountain pointers on failure."""
    from .mountain import (
        prune_owned_compact_fallbacks,
        prune_owned_releases,
        restore_compact_pointer,
        restore_release_pointer,
        write_and_promote_compact_fallback,
    )

    compact_pointer = paths.data / "mountain" / "compact" / "current.json"
    previous_compact = compact_pointer.read_bytes() if compact_pointer.is_file() else None
    if paths.current.is_symlink():
        raise HouseHunterError("HouseHunter snapshot pointer cannot be a symlink")
    previous_snapshot = paths.current.read_bytes() if paths.current.is_file() else None
    try:
        compact = write_and_promote_compact_fallback(paths, promoted_release, release_manifest)
        snapshot = build_snapshot(paths, progress=_progress)
    except BaseException:
        restore_release_pointer(paths, previous_pointer)
        restore_compact_pointer(paths, previous_compact)
        temporary = paths.current.with_name(f".{paths.current.name}.{uuid.uuid4().hex}.tmp")
        if previous_snapshot is None:
            paths.current.unlink(missing_ok=True)
        else:
            temporary.write_bytes(previous_snapshot)
            os.replace(temporary, paths.current)
        raise
    cleanup_warnings: list[str] = []
    for label, cleanup in (
        ("full release", prune_owned_releases),
        ("compact fallback", prune_owned_compact_fallbacks),
    ):
        try:
            cleanup(paths)
        except (HouseHunterError, OSError) as exc:
            _record_cleanup_warning(label, exc, cleanup_warnings)
    return snapshot, compact, cleanup_warnings


@mountain_app.command("download")
def mountain_download(
    source_lock: Annotated[Path, typer.Option("--source-lock", exists=True, dir_okay=False)],
    destination: Annotated[Path | None, typer.Option("--destination")] = None,
    family: Annotated[
        str | None,
        typer.Option(
            "--family",
            help="Download one managed family: blocks, elevation, pad_us, or trails.",
        ),
    ] = None,
    batch: Annotated[
        str | None,
        typer.Option(
            "--batch",
            help="Download one reviewed elevation or trails preparation batch.",
        ),
    ] = None,
) -> None:
    """Download the exact files named by a maintainer source lock."""
    from .mountain_gis import download_sources as download_mountain_sources
    from .mountain_paths import ensure_owned_child, lexical_path

    paths = _paths()
    managed_staging = paths.data / "mountain" / "staging"
    target = (
        destination.expanduser().resolve()
        if destination is not None
        else lexical_path(managed_staging / sha256_file(source_lock)[:16])
    )
    try:
        with exclusive_lock(paths.job_lock):
            if destination is None:
                target = ensure_owned_child(
                    target,
                    managed_staging,
                    name_pattern=r"[0-9a-f]{16}",
                    marker_value="staging-v1\n",
                )
            output = download_mountain_sources(
                source_lock.expanduser().resolve(),
                target,
                managed_root=paths.data / "mountain" if destination is None else None,
                families={family} if family else None,
                batch_id=batch,
            )
        typer.echo(str(output))
    except (
        HouseHunterError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("build")
def mountain_build(
    data_release: Annotated[str, typer.Option("--data-release")],
    source_lock: Annotated[Path | None, typer.Option("--source-lock", dir_okay=False)] = None,
    regions: Annotated[Path | None, typer.Option("--regions", dir_okay=False)] = None,
    source_root: Annotated[Path | None, typer.Option("--source-root", file_okay=False)] = None,
    raw_blocks: Annotated[Path | None, typer.Option("--raw-blocks", dir_okay=False)] = None,
    prepared_pack: Annotated[Path | None, typer.Option("--prepared-pack", file_okay=False)] = None,
    prepared_lock: Annotated[Path | None, typer.Option("--prepared-lock", dir_okay=False)] = None,
    workers: Annotated[int, typer.Option("--workers", min=1, max=4)] = 4,
    resume: Annotated[bool, typer.Option("--resume/--fresh")] = True,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    promote: Annotated[bool, typer.Option("--promote/--no-promote")] = True,
) -> None:
    """Build, validate, and optionally promote a Mountain Magnitude release."""
    import polars as pl

    from .mountain import (
        FULL_RELEASE_MAX_BYTES,
        promote_release,
        validate_national_expectations,
        validate_release,
        write_and_promote_release,
        write_release,
    )
    from .mountain_gis import (
        build_region_raw_metrics,
        load_regions,
        load_source_lock_contract,
        locked_source_paths,
        source_provenance_item,
        verify_region_sources_locked,
        verify_source_lock,
    )
    from .mountain_pack import (
        build_prepared_raw_metrics,
        ensure_storage_budget,
        prepared_build_reservation,
        remove_owned_work_directory,
        verify_prepared_pack,
    )
    from .mountain_paths import ensure_safe_directory, lexical_path

    if sum(bool(value) for value in (raw_blocks, regions, prepared_pack)) != 1:
        _abort(
            HouseHunterError("Provide exactly one of --prepared-pack, --raw-blocks, or --regions")
        )
    if bool(prepared_pack) != bool(prepared_lock):
        _abort(HouseHunterError("--prepared-pack and --prepared-lock must be provided together"))
    if prepared_pack and source_lock is None:
        _abort(HouseHunterError("National prepared builds require --source-lock"))
    if prepared_pack and promote and output is not None:
        _abort(HouseHunterError("Prepared promoted builds write directly to managed releases"))
    paths = _paths()
    try:
        release_slug = _release_slug(data_release)
        candidate = (
            output.expanduser().resolve()
            if output is not None
            else lexical_path(paths.data / "mountain" / "candidates" / release_slug)
        )
        if output is None:
            ensure_safe_directory(candidate.parent)
        with exclusive_lock(paths.job_lock):
            if output is None and not prepared_pack:
                ensure_storage_budget(paths.data / "mountain", reserve_bytes=FULL_RELEASE_MAX_BYTES)
            started = time.monotonic()
            source_metadata: dict[str, object] = {}
            lock: dict[str, object] | None = None
            work: Path | None = None
            performance: dict[str, object] = {}
            if source_lock:
                resolved_lock = source_lock.expanduser().resolve()
                lock = (
                    load_source_lock_contract(resolved_lock, require_v2=True)
                    if prepared_pack
                    else verify_source_lock(
                        resolved_lock,
                        root=source_root.expanduser().resolve() if source_root else None,
                    )
                )
                source_metadata = {
                    "source_lock_schema_version": lock["schema_version"],
                    "source_lock_sha256": sha256_file(resolved_lock),
                    "items": [source_provenance_item(item) for item in lock["sources"]],
                }
                if lock.get("schema_version") != 2:
                    raise HouseHunterError("National Mountain builds require source-lock v2")
                if regions and lock.get("trail_fragment_mode") == "state_clipped_globalid_v1":
                    raise HouseHunterError(
                        "This national source contract requires a prepared pack so cross-state "
                        "trail fragments are deduplicated"
                    )
            if prepared_pack:
                assert prepared_lock is not None
                assert source_lock is not None
                pack = lexical_path(prepared_pack)
                pack_lock = prepared_lock.expanduser().resolve()
                manifest = verify_prepared_pack(
                    pack,
                    pack_lock,
                    reviewed_source_lock_path=source_lock.expanduser().resolve(),
                    require_source_lock_v2=True,
                )
                grid = manifest["grid"]
                if (
                    grid.get("cell_size_m") != 250
                    or grid.get("tile_size_m") != 100_000
                    or grid.get("halo_m") != 100_000
                    or any(tile.get("shape") != [1_200, 1_200] for tile in manifest["tiles"])
                ):
                    raise HouseHunterError(
                        "National Mountain builds require exact v1 250 m/100 km tiles"
                    )
                work_root = paths.data / "mountain" / "work"
                work_root.mkdir(parents=True, exist_ok=True)
                work = work_root / str(manifest["pack_id"])
                if not resume and work.exists():
                    remove_owned_work_directory(work, work_root)
                if promote:
                    # The job lock makes this one aggregate reservation authoritative for
                    # every managed write that follows: remaining shards, the candidate,
                    # and the compact fallback. Each writer also enforces its own cap.
                    ensure_storage_budget(
                        paths.data / "mountain",
                        reserve_bytes=prepared_build_reservation(work),
                    )
                blocks, performance = build_prepared_raw_metrics(
                    pack,
                    pack_lock,
                    work,
                    workers=workers,
                    resume=resume,
                    managed_root=paths.data / "mountain",
                    verified_manifest=manifest,
                )
                expectations = manifest.get("state_expectations")
                provenance = manifest.get("source_provenance", {})
                if not isinstance(expectations, dict) or not isinstance(provenance, dict):
                    raise HouseHunterError("Prepared Mountain pack lacks national provenance")
                source_metadata = {
                    **provenance,
                    "source_lock_schema_version": manifest["source_lock_schema_version"],
                    "source_lock_sha256": manifest["source_lock_sha256"],
                    "prepared_pack": {
                        "schema_version": 1,
                        "pack_id": manifest["pack_id"],
                        "prepared_lock_sha256": sha256_file(pack_lock),
                        "source_lock_sha256": manifest["source_lock_sha256"],
                    },
                }
            elif raw_blocks:
                raw_path = raw_blocks.expanduser().resolve()
                if lock is None:
                    raise HouseHunterError("National raw-block builds require --source-lock")
                if lock is not None and raw_path not in locked_source_paths(
                    lock, root=source_root.expanduser().resolve() if source_root else None
                ):
                    raise HouseHunterError("--raw-blocks is absent from the source lock")
                blocks = pl.read_parquet(raw_path)
            else:
                if not source_lock or not source_root or not regions:
                    raise HouseHunterError(
                        "GIS builds require --source-lock, --source-root, and --regions"
                    )
                assert lock is not None
                configured_regions = load_regions(
                    regions.expanduser().resolve(), source_root.expanduser().resolve()
                )
                verify_region_sources_locked(
                    configured_regions, lock, root=source_root.expanduser().resolve()
                )
                regional = [
                    build_region_raw_metrics(region, state_by_fips=STATE_BY_FIPS)
                    for region in configured_regions
                ]
                blocks = pl.concat(regional)
            if not prepared_pack:
                expectations: dict[str, object] | None = None
            if not prepared_pack:
                expectations = lock.get("expected_states") if lock else None
                if not isinstance(expectations, dict):
                    raise HouseHunterError(
                        "National Mountain builds require expected_states in the source lock"
                    )
                validate_national_expectations(
                    blocks,
                    expectations,
                    expected_block_geoid_sha256=lock.get("block_geoid_sha256")
                    if lock and lock.get("schema_version") == 2
                    else None,
                )
            if prepared_pack:
                assert expectations is not None
                validate_national_expectations(
                    blocks,
                    expectations,
                    expected_block_geoid_sha256=manifest["block_geoid_sha256"],
                )
            pointer_path = paths.data / "mountain" / "current.json"
            previous_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
            if promote and output is None and expectations is not None:
                release_timings: dict[str, float] = {}
                final, release_manifest = write_and_promote_release(
                    paths,
                    blocks,
                    data_release=data_release,
                    sources=source_metadata,
                    national_expectations=expectations,
                    timings=release_timings,
                )
                performance.update(release_timings)
            else:
                release = write_release(
                    blocks,
                    candidate,
                    data_release=data_release,
                    sources=source_metadata,
                    national_expectations=expectations,
                )
                final = (
                    promote_release(
                        paths,
                        release,
                        reviewed_source_lock=lock or {},
                        reviewed_source_lock_sha256=str(source_metadata["source_lock_sha256"]),
                        expected_raw_blocks=blocks,
                    )
                    if promote
                    else release
                )
                release_manifest = validate_release(final if not promote else release)
            snapshot_path: Path | None = None
            if promote:
                snapshot_started = time.monotonic()
                snapshot_path, compact_path, cleanup_warnings = _rebuild_after_mountain_promotion(
                    paths, previous_pointer, final, release_manifest
                )
                performance["snapshot_seconds"] = round(time.monotonic() - snapshot_started, 3)
                performance["compact_path"] = str(compact_path)
                performance["cleanup_warnings"] = cleanup_warnings
                if prepared_pack and work is not None:
                    try:
                        remove_owned_work_directory(work, paths.data / "mountain" / "work")
                    except (HouseHunterError, OSError) as exc:
                        _record_cleanup_warning("work shard", exc, cleanup_warnings)
            total_seconds = round(time.monotonic() - started, 3)
            if prepared_pack:
                reports = ensure_safe_directory(paths.data / "mountain" / "reports")
                report = {
                    **performance,
                    "release_id": release_manifest["release_id"],
                    "snapshot_id": snapshot_path.name if snapshot_path else None,
                    "total_seconds": total_seconds,
                    "under_55_minutes": total_seconds < 55 * 60,
                }
                report_path = reports / f"{release_manifest['release_id']}-{uuid.uuid4().hex}.json"
                report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        typer.echo(str(final))
    except (HouseHunterError, OSError, pl.exceptions.PolarsError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("prepare")
def mountain_prepare(
    source_lock: Annotated[Path, typer.Option("--source-lock", exists=True, dir_okay=False)],
    regions: Annotated[Path, typer.Option("--regions", exists=True, dir_okay=False)],
    source_root: Annotated[Path, typer.Option("--source-root", exists=True, file_okay=False)],
    destination: Annotated[Path | None, typer.Option("--destination")] = None,
    prepared_lock: Annotated[Path | None, typer.Option("--prepared-lock-output")] = None,
    through_family: Annotated[
        str | None,
        typer.Option(
            "--through-family",
            help="Stop successfully after blocks, elevation, or pad_us; trails publishes the pack",
        ),
    ] = None,
    source_batch: Annotated[
        str | None,
        typer.Option(
            "--source-batch",
            help="Materialize one downloaded elevation or trails batch and checkpoint it.",
        ),
    ] = None,
) -> None:
    """Build an immutable v1 prepared source pack outside the timed build SLA."""
    from .mountain_gis import (
        load_regions,
        load_source_lock_contract,
        verify_region_sources_locked,
    )
    from .mountain_pack import (
        prepare_regions,
        prune_owned_preparation_workspaces,
        prune_owned_prepared_packs,
        remove_owned_staging_directory,
    )
    from .mountain_paths import OWNERSHIP_MARKER, lexical_path

    paths = _paths()
    staging_root = paths.data / "mountain" / "staging"
    try:
        lock_path = source_lock.expanduser().resolve()
        root_input = lexical_path(source_root)
        root = root_input.resolve()
        region_path = regions.expanduser().resolve()
        target = (
            destination.expanduser().resolve()
            if destination is not None
            else lexical_path(paths.data / "mountain" / "prepared")
        )
        with exclusive_lock(paths.job_lock):
            lock = load_source_lock_contract(lock_path, require_v2=True)
            configured = load_regions(region_path, root)
            verify_region_sources_locked(configured, lock, root=root, verify_block_partition=False)
            managed_source = (
                root_input.parent == lexical_path(staging_root)
                and (root_input / OWNERSHIP_MARKER).is_file()
            )
            pack, pack_lock = prepare_regions(
                configured,
                target,
                state_by_fips=STATE_BY_FIPS,
                source_lock_path=lock_path,
                region_config_path=region_path,
                prepared_lock_path=prepared_lock.expanduser().resolve() if prepared_lock else None,
                managed_root=paths.data / "mountain" if destination is None else None,
                source_root=root,
                managed_staging_root=staging_root if managed_source else None,
                stop_after_family=through_family,
                source_batch=source_batch,
            )
            if pack is None or pack_lock is None:
                typer.echo(
                    json.dumps(
                        {
                            "status": "checkpoint_complete",
                            "family": through_family,
                            "source_batch": source_batch,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return
            if (
                managed_source
                and (root_input / OWNERSHIP_MARKER).is_file()
                and not pack.is_relative_to(root)
            ):
                remove_owned_staging_directory(root_input, staging_root)
            if destination is None:
                prune_owned_prepared_packs(target, keep=pack)
                prune_owned_preparation_workspaces(target)
        typer.echo(
            json.dumps(
                {"pack": str(pack), "prepared_lock": str(pack_lock)},
                indent=2,
                sort_keys=True,
            )
        )
    except (HouseHunterError, OSError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("inventory")
def mountain_inventory(
    regions: Annotated[Path, typer.Option("--regions", exists=True, dir_okay=False)],
    source_root: Annotated[Path, typer.Option("--source-root", exists=True, file_okay=False)],
    source_lock: Annotated[
        Path | None, typer.Option("--source-lock", exists=True, dir_okay=False)
    ] = None,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    """Compute the exact national block/tile inventory before large source downloads."""
    from .mountain_gis import load_regions, qualify_national_inventory

    try:
        configured = load_regions(
            regions.expanduser().resolve(), source_root.expanduser().resolve()
        )
        sources = None
        if source_lock is not None:
            lock_payload = json.loads(source_lock.expanduser().resolve().read_text())
            if not isinstance(lock_payload, dict) or not isinstance(
                lock_payload.get("sources"), list
            ):
                raise HouseHunterError("Mountain source lock contains an invalid source list")
            sources = lock_payload["sources"]
        report = qualify_national_inventory(
            configured, state_by_fips=STATE_BY_FIPS, sources=sources
        )
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if output:
            destination = output.expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(rendered)
            os.replace(temporary, destination)
            typer.echo(str(destination))
        else:
            typer.echo(rendered, nl=False)
    except (HouseHunterError, OSError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("validate")
def mountain_validate(
    release: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    promote: Annotated[bool, typer.Option("--promote/--no-promote")] = False,
    source_lock: Annotated[
        Path | None, typer.Option("--source-lock", exists=True, dir_okay=False)
    ] = None,
    prepared_pack: Annotated[
        Path | None, typer.Option("--prepared-pack", exists=True, file_okay=False)
    ] = None,
    prepared_lock: Annotated[
        Path | None, typer.Option("--prepared-lock", exists=True, dir_okay=False)
    ] = None,
    workers: Annotated[int, typer.Option("--workers", min=1, max=4)] = 4,
) -> None:
    """Validate a candidate release and optionally promote it."""
    from .mountain import promote_release
    from .mountain import validate_release as validate_mountain_release
    from .mountain_gis import load_source_lock_contract
    from .mountain_pack import (
        build_prepared_raw_metrics,
        remove_owned_work_directory,
        verify_prepared_pack,
    )
    from .mountain_paths import lexical_path

    paths = _paths()
    try:
        with exclusive_lock(paths.job_lock):
            manifest = validate_mountain_release(release.expanduser().resolve())
            report = {
                key: value
                for key, value in manifest.items()
                if key not in {"sources", "national_expectations"}
            }
            if promote:
                if source_lock is None or prepared_pack is None or prepared_lock is None:
                    raise HouseHunterError(
                        "Promoting an external Mountain release requires --source-lock, "
                        "--prepared-pack, and --prepared-lock"
                    )
                reviewed = source_lock.expanduser().resolve()
                reviewed_contract = load_source_lock_contract(reviewed, require_v2=True)
                pack = lexical_path(prepared_pack)
                pack_lock = prepared_lock.expanduser().resolve()
                pack_manifest = verify_prepared_pack(
                    pack,
                    pack_lock,
                    reviewed_source_lock_path=reviewed,
                    require_source_lock_v2=True,
                )
                work_root = paths.data / "mountain" / "work"
                work = work_root / str(pack_manifest["pack_id"])
                expected_raw, _ = build_prepared_raw_metrics(
                    pack,
                    pack_lock,
                    work,
                    workers=workers,
                    resume=True,
                    managed_root=paths.data / "mountain",
                    verified_manifest=pack_manifest,
                )
                pointer_path = paths.data / "mountain" / "current.json"
                previous_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
                promoted = promote_release(
                    paths,
                    release.expanduser().resolve(),
                    reviewed_source_lock=reviewed_contract,
                    reviewed_source_lock_sha256=sha256_file(reviewed),
                    expected_raw_blocks=expected_raw,
                )
                report["promoted_path"] = str(promoted)
                snapshot, compact, cleanup_warnings = _rebuild_after_mountain_promotion(
                    paths, previous_pointer, promoted, manifest
                )
                report["snapshot_path"] = str(snapshot)
                report["compact_path"] = str(compact)
                report["cleanup_warnings"] = cleanup_warnings
                try:
                    remove_owned_work_directory(work, work_root)
                except (HouseHunterError, OSError) as exc:
                    _record_cleanup_warning("work shard", exc, cleanup_warnings)
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
    except (HouseHunterError, OSError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("bundle")
def mountain_bundle(
    release: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Generate a validated tract/county-only bundled runtime fallback."""
    from .mountain import write_compact_bundle

    paths = _paths()
    try:
        with exclusive_lock(paths.job_lock):
            result = write_compact_bundle(
                release.expanduser().resolve(), output.expanduser().resolve()
            )
        typer.echo(str(result))
    except (HouseHunterError, OSError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("rescore-v1")
def mountain_rescore_v1(
    source_lock: Annotated[
        Path,
        typer.Option("--source-lock", exists=True, dir_okay=False),
    ] = Path("config/mountain/source-lock-v2.json"),
) -> None:
    """Offline migration of the active full v1 release to Mountain Magnitude v2."""
    from .mountain_migration import rescore_v1_release

    paths = _paths()
    try:
        with exclusive_lock(paths.job_lock):
            report = rescore_v1_release(
                paths, source_lock.expanduser().resolve(), progress=_progress
            )
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
    except (HouseHunterError, OSError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("inspect")
def mountain_inspect(
    geoid: str,
    release: Annotated[Path | None, typer.Option("--release", file_okay=False)] = None,
) -> None:
    """Inspect a 15-digit block, 11-digit tract, or 5-digit county release row."""
    import polars as pl

    from .mountain import current_compact_release

    if not geoid.isdigit() or len(geoid) not in {5, 11, 15}:
        _abort(HouseHunterError("Mountain GEOID must contain 5, 11, or 15 digits"))
    paths = _paths()
    try:
        root = release.expanduser().resolve() if release else None
        if root is None:
            current = current_compact_release(paths)
            if current is None:
                raise HouseHunterError("No promoted or bundled Mountain release is available")
            root = current[0]
        filename = {5: "counties.parquet", 11: "tracts.parquet", 15: "blocks.parquet"}[len(geoid)]
        artifact = root / filename
        if not artifact.is_file():
            raise HouseHunterError(f"Mountain {len(geoid)}-digit detail is unavailable")
        row = pl.read_parquet(artifact).filter(
            pl.col("block_geoid" if len(geoid) == 15 else "place_id") == geoid
        )
        if row.height != 1:
            raise HouseHunterError(f"Mountain GEOID not found: {geoid}")
        row = row.drop("mountain_score", "mountain_score_version", strict=False)
        typer.echo(json.dumps(row.row(0, named=True), indent=2, sort_keys=True))
    except (HouseHunterError, OSError, KeyError, json.JSONDecodeError) as exc:
        _abort(HouseHunterError(str(exc)))


@app.command()
def sources(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
) -> None:
    """Show pinned source versions and local cache state."""
    try:
        config = load_config()
        statuses = {item.source: item.model_dump(mode="json") for item in source_statuses(_paths())}
        result = {
            key: {
                **statuses[key],
                "release": config[key]["release"],
                "url": config[key]["item_url"],
            }
            for key in ("fema", "fema_counties", "chrr")
        }
        if json_output:
            typer.echo(json.dumps(result, indent=2, sort_keys=True))
        else:
            for key, label in (
                ("fema", "FEMA NRI tracts"),
                ("fema_counties", "FEMA NRI counties"),
                ("chrr", "CHR&R Community Conditions counties"),
            ):
                typer.echo(
                    f"{label} {result[key]['release']} ({result[key]['version']}): "
                    f"{'cached' if result[key]['cached'] else 'not downloaded'}"
                )
                if result[key]["error"]:
                    typer.echo(f"Cache error ({key}): {result[key]['error']}", err=True)
    except HouseHunterError as exc:
        _abort(exc)


@app.command()
def download(
    source: Annotated[str, typer.Option("--source", help="Source to download")] = "fema",
) -> None:
    """Download and verify pinned source data."""
    selected = source.lower().replace("-", "_")
    paths = _paths()
    try:
        with exclusive_lock(paths.job_lock):
            if selected == "fema":
                output = download_fema(paths, progress=_progress)
            elif selected == "fema_counties":
                output = download_fema_counties(paths, progress=_progress)
            elif selected == "chrr":
                output = download_chrr(paths, progress=_progress)
            elif selected == "all":
                download_fema(paths, progress=_progress)
                download_fema_counties(paths, progress=_progress)
                output = download_chrr(paths, progress=_progress)
            else:
                _abort(HouseHunterError("Supported sources are fema, fema_counties, chrr, and all"))
        typer.echo(str(output))
    except HouseHunterError as exc:
        _abort(exc)


@app.command()
def build(
    state: Annotated[str | None, typer.Option("--state", help="Two-letter state scope")] = None,
) -> None:
    """Build and atomically publish a ranking snapshot."""
    paths = _paths()
    try:
        with exclusive_lock(paths.job_lock):
            output = build_snapshot(paths, state=state, progress=_progress)
        typer.echo(str(output))
    except HouseHunterError as exc:
        _abort(exc)


@app.command()
def rank(
    state: Annotated[str | None, typer.Option("--state")] = None,
    county: Annotated[str | None, typer.Option("--county", help="5-digit county FIPS")] = None,
    level: Annotated[str, typer.Option("--level", help="tract or county")] = "tract",
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 25,
    include_unranked: Annotated[bool, typer.Option("--include-unranked")] = False,
    mountain_magnitude_min: Annotated[
        float | None, typer.Option("--mountain-magnitude-min", min=0)
    ] = None,
    metric: Annotated[
        str,
        typer.Option("--metric", help="risk, community-conditions, or mountain"),
    ] = "risk",
    order: Annotated[str, typer.Option("--order", help="best or worst")] = "best",
) -> None:
    """Rank geographies by FEMA risk, Community Conditions, or Mountain Magnitude."""
    selected = level.lower()
    if selected not in {"tract", "county"}:
        _abort(HouseHunterError("Rank level must be tract or county"))
    if selected == "county" and county:
        _abort(HouseHunterError("--county filters tracts; omit it when ranking counties"))
    selected_metric = metric.lower()
    if selected_metric not in {"risk", "community-conditions", "mountain"}:
        _abort(HouseHunterError("Rank metric must be risk, community-conditions, or mountain"))
    selected_order = order.lower()
    if selected_order not in {"best", "worst"}:
        _abort(HouseHunterError("Rank order must be best or worst"))
    sort = {
        "risk": "risk_score",
        "community-conditions": "community_conditions_group",
        "mountain": "mountain_magnitude",
    }[selected_metric]
    if selected_metric == "mountain":
        direction = "desc" if selected_order == "best" else "asc"
    else:
        direction = "asc" if selected_order == "best" else "desc"
    try:
        with Store(_paths()) as store:
            if selected == "county":
                result = store.list_counties(
                    state=state,
                    limit=limit,
                    include_unranked=include_unranked,
                    mountain_magnitude_min=mountain_magnitude_min,
                    sort=sort,
                    direction=direction,
                )
                label = (
                    "GROUP"
                    if selected_metric == "community-conditions"
                    else "MAGNITUDE"
                    if selected_metric == "mountain"
                    else "SCORE"
                )
                typer.echo(f"COUNTY_FIPS  {label:<9}  STATE  NAME")
            else:
                result = store.list_places(
                    state=state,
                    county=county,
                    limit=limit,
                    include_unranked=include_unranked,
                    mountain_magnitude_min=mountain_magnitude_min,
                    sort=sort,
                    direction=direction,
                )
                label = (
                    "GROUP"
                    if selected_metric == "community-conditions"
                    else "MAGNITUDE"
                    if selected_metric == "mountain"
                    else "SCORE"
                )
                typer.echo(f"TRACT_ID     {label:<9}  STATE")
        for row in result["items"]:
            value = (
                row["risk_score"]
                if selected_metric == "risk"
                else row["mountain_magnitude"]
                if selected_metric == "mountain"
                else row["community_conditions_group"]
            )
            score = (
                f"M{value:.2f}"
                if selected_metric == "mountain" and value is not None
                else f"{value:.1f}"
                if selected_metric == "risk" and value is not None
                else str(value)
                if value is not None
                else "—"
            )
            if selected == "county":
                typer.echo(f"{row['place_id']:<12} {score:>9}  {row['state']:<5}  {row['name']}")
            else:
                typer.echo(f"{row['place_id']:<12} {score:>9}  {row['state']}")
    except HouseHunterError as exc:
        _abort(exc)


@app.command("lookup")
def lookup_place(
    address: str,
    allow_approximate: Annotated[
        bool,
        typer.Option(
            "--allow-approximate",
            help="Resolve an OpenStreetMap street match without confirmation",
        ),
    ] = False,
) -> None:
    """Map a US address to a FEMA tract via Census, with Nominatim fallback."""
    try:
        payload = lookup_address(_paths(), address, allow_approximate=allow_approximate)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        if payload.get("status") == "confirmation_required":
            raise typer.Exit(2)
    except AmbiguousPlaceError as exc:
        typer.echo(
            json.dumps({"error": str(exc), "candidates": exc.candidates}, indent=2), err=True
        )
        raise typer.Exit(2) from exc
    except HouseHunterError as exc:
        _abort(exc)


@app.command("inspect")
def inspect_place(query: str) -> None:
    """Inspect a tract by 11-digit FIPS or a county by 5-digit FIPS."""
    try:
        with Store(_paths()) as store:
            if len(query) == 5 and query.isdigit():
                detail = store.county_detail(store.resolve_county(query))
            else:
                detail = store.place_detail(store.resolve_place(query))
        typer.echo(json.dumps(detail, indent=2, sort_keys=True))
    except AmbiguousPlaceError as exc:
        typer.echo(
            json.dumps({"error": str(exc), "candidates": exc.candidates}, indent=2), err=True
        )
        raise typer.Exit(2) from exc
    except HouseHunterError as exc:
        _abort(exc)


@app.command()
def export(
    format: Annotated[str, typer.Option("--format", help="csv, json, or parquet")] = "csv",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    level: Annotated[str, typer.Option("--level", help="tract or county")] = "tract",
) -> None:
    """Export the selected immutable snapshot table."""
    selected = format.lower()
    geography = level.lower()
    if selected not in {"csv", "json", "parquet"}:
        _abort(HouseHunterError("Export format must be csv, json, or parquet"))
    if geography not in {"tract", "county"}:
        _abort(HouseHunterError("Export level must be tract or county"))
    table = "counties" if geography == "county" else "places"
    stem = "househunter-counties" if geography == "county" else "househunter-places"
    destination = (output or Path.cwd() / f"{stem}.{selected}").expanduser().resolve()
    try:
        with Store(_paths()) as store:
            store.export(selected, destination, table=table)
        typer.echo(str(destination))
    except (HouseHunterError, OSError) as exc:
        _abort(HouseHunterError(str(exc)))


@app.command("app")
def run_app(
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open a browser")] = False,
) -> None:
    """Run the local web application on loopback only."""
    url = f"http://127.0.0.1:{port}"
    if not no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    typer.echo(f"HouseHunter is running at {url}")
    uvicorn.run(
        create_app(_paths()), host="127.0.0.1", port=port, log_level="info", lifespan="off"
    )


if __name__ == "__main__":
    app()
