from __future__ import annotations

import json
import re
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .api import create_app
from .build import build_snapshot
from .chrr import download_chrr
from .config import RuntimePaths, load_config
from .download import download_fema, download_fema_counties, source_statuses
from .errors import AmbiguousPlaceError, HouseHunterError
from .geocode import lookup_address
from .geography import STATE_BY_FIPS
from .locking import exclusive_lock
from .store import Store

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
mountain_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
app.add_typer(mountain_app, name="mountain", help="Build and inspect Mountain Score releases.")


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


@mountain_app.command("download")
def mountain_download(
    source_lock: Annotated[Path, typer.Option("--source-lock", exists=True, dir_okay=False)],
    destination: Annotated[Path | None, typer.Option("--destination")] = None,
) -> None:
    """Download the exact files named by a maintainer source lock."""
    from .mountain_gis import download_sources as download_mountain_sources

    paths = _paths()
    target = (destination or paths.data / "mountain" / "sources").expanduser().resolve()
    try:
        with exclusive_lock(paths.job_lock):
            output = download_mountain_sources(source_lock.expanduser().resolve(), target)
        typer.echo(str(output))
    except (HouseHunterError, OSError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("build")
def mountain_build(
    data_release: Annotated[str, typer.Option("--data-release")],
    source_lock: Annotated[Path | None, typer.Option("--source-lock", dir_okay=False)] = None,
    regions: Annotated[Path | None, typer.Option("--regions", dir_okay=False)] = None,
    source_root: Annotated[Path | None, typer.Option("--source-root", file_okay=False)] = None,
    raw_blocks: Annotated[Path | None, typer.Option("--raw-blocks", dir_okay=False)] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    promote: Annotated[bool, typer.Option("--promote/--no-promote")] = True,
    allow_partial: Annotated[bool, typer.Option("--allow-partial")] = False,
) -> None:
    """Build, validate, and optionally promote a Mountain Score release."""
    import polars as pl

    from .mountain import (
        promote_release,
        validate_national_expectations,
        write_release,
    )
    from .mountain_gis import (
        build_region_raw_metrics,
        load_regions,
        locked_source_paths,
        verify_region_sources_locked,
        verify_source_lock,
    )

    if bool(raw_blocks) == bool(regions):
        _abort(HouseHunterError("Provide exactly one of --raw-blocks or --regions"))
    if allow_partial and promote:
        _abort(HouseHunterError("Partial Mountain builds cannot be promoted"))
    paths = _paths()
    try:
        release_slug = _release_slug(data_release)
        candidate = (
            (output or paths.data / "mountain" / "candidates" / release_slug).expanduser().resolve()
        )
        with exclusive_lock(paths.job_lock):
            source_metadata: dict[str, object] = {}
            lock: dict[str, object] | None = None
            if source_lock:
                lock = verify_source_lock(
                    source_lock.expanduser().resolve(),
                    root=source_root.expanduser().resolve() if source_root else None,
                )
                source_metadata = {
                    "items": [
                        {key: value for key, value in item.items() if key != "path"}
                        for item in lock["sources"]
                    ]
                }
            if raw_blocks:
                raw_path = raw_blocks.expanduser().resolve()
                if not allow_partial and lock is None:
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
            expectations: dict[str, object] | None = None
            if not allow_partial:
                expectations = lock.get("expected_states") if lock else None
                if not isinstance(expectations, dict):
                    raise HouseHunterError(
                        "National Mountain builds require expected_states in the source lock"
                    )
                validate_national_expectations(blocks, expectations)
            release = write_release(
                blocks,
                candidate,
                data_release=data_release,
                sources=source_metadata,
                national_expectations=expectations,
            )
            final = promote_release(paths, release) if promote else release
        typer.echo(str(final))
    except (HouseHunterError, OSError, pl.exceptions.PolarsError) as exc:
        _abort(HouseHunterError(str(exc)))


@mountain_app.command("validate")
def mountain_validate(
    release: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    promote: Annotated[bool, typer.Option("--promote/--no-promote")] = False,
) -> None:
    """Validate a candidate release and optionally promote it."""
    from .mountain import promote_release
    from .mountain import validate_release as validate_mountain_release

    paths = _paths()
    try:
        with exclusive_lock(paths.job_lock):
            report = validate_mountain_release(release.expanduser().resolve())
            if promote:
                report["promoted_path"] = str(
                    promote_release(paths, release.expanduser().resolve())
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

    if not geoid.isdigit() or len(geoid) not in {5, 11, 15}:
        _abort(HouseHunterError("Mountain GEOID must contain 5, 11, or 15 digits"))
    paths = _paths()
    try:
        root = release.expanduser().resolve() if release else None
        if root is None:
            pointer = json.loads((paths.data / "mountain" / "current.json").read_text())
            root = paths.data / "mountain" / "releases" / pointer["release_id"]
        filename = {5: "counties.parquet", 11: "tracts.parquet", 15: "blocks.parquet"}[len(geoid)]
        artifact = root / filename
        if not artifact.is_file():
            raise HouseHunterError(f"Mountain {len(geoid)}-digit detail is unavailable")
        row = pl.read_parquet(artifact).filter(
            pl.col("block_geoid" if len(geoid) == 15 else "place_id") == geoid
        )
        if row.height != 1:
            raise HouseHunterError(f"Mountain GEOID not found: {geoid}")
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
    mountain_min: Annotated[float | None, typer.Option("--mountain-min", min=0, max=100)] = None,
    metric: Annotated[str, typer.Option("--metric", help="risk or community-conditions")] = "risk",
    order: Annotated[str, typer.Option("--order", help="best or worst")] = "best",
) -> None:
    """Rank geographies by FEMA risk or CHR&R Community Conditions."""
    selected = level.lower()
    if selected not in {"tract", "county"}:
        _abort(HouseHunterError("Rank level must be tract or county"))
    if selected == "county" and county:
        _abort(HouseHunterError("--county filters tracts; omit it when ranking counties"))
    selected_metric = metric.lower()
    if selected_metric not in {"risk", "community-conditions"}:
        _abort(HouseHunterError("Rank metric must be risk or community-conditions"))
    selected_order = order.lower()
    if selected_order not in {"best", "worst"}:
        _abort(HouseHunterError("Rank order must be best or worst"))
    sort = "risk_score" if selected_metric == "risk" else "community_conditions_group"
    direction = "asc" if selected_order == "best" else "desc"
    try:
        with Store(_paths()) as store:
            if selected == "county":
                result = store.list_counties(
                    state=state,
                    limit=limit,
                    include_unranked=include_unranked,
                    mountain_min=mountain_min,
                    sort=sort,
                    direction=direction,
                )
                label = "SCORE" if selected_metric == "risk" else "GROUP"
                typer.echo(f"COUNTY_FIPS  {label:<5}  STATE  NAME")
            else:
                result = store.list_places(
                    state=state,
                    county=county,
                    limit=limit,
                    include_unranked=include_unranked,
                    mountain_min=mountain_min,
                    sort=sort,
                    direction=direction,
                )
                label = "SCORE" if selected_metric == "risk" else "GROUP"
                typer.echo(f"TRACT_ID     {label:<5}  STATE")
        for row in result["items"]:
            value = (
                row["risk_score"]
                if selected_metric == "risk"
                else row["community_conditions_group"]
            )
            score = (
                f"{value:.1f}"
                if selected_metric == "risk" and value is not None
                else str(value)
                if value is not None
                else "—"
            )
            if selected == "county":
                typer.echo(f"{row['place_id']:<12} {score:>5}  {row['state']:<5}  {row['name']}")
            else:
                typer.echo(f"{row['place_id']:<12} {score:>5}  {row['state']}")
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
    format: Annotated[str, typer.Option("--format", help="csv or parquet")] = "csv",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    level: Annotated[str, typer.Option("--level", help="tract or county")] = "tract",
) -> None:
    """Export the selected immutable snapshot table."""
    selected = format.lower()
    geography = level.lower()
    if selected not in {"csv", "parquet"}:
        _abort(HouseHunterError("Export format must be csv or parquet"))
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
    uvicorn.run(create_app(_paths()), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    app()
