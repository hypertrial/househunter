from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .api import create_app
from .build import build_snapshot
from .config import RuntimePaths, load_config
from .download import download_fema, download_fema_counties, source_statuses
from .errors import AmbiguousPlaceError, HouseHunterError
from .geocode import lookup_address
from .locking import exclusive_lock
from .store import Store

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


def _paths() -> RuntimePaths:
    return RuntimePaths.from_root()


def _progress(value: int, message: str) -> None:
    typer.echo(f"[{value:3d}%] {message}", err=True)


def _abort(exc: HouseHunterError) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1) from exc


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
            for key in ("fema", "fema_counties")
        }
        if json_output:
            typer.echo(json.dumps(result, indent=2, sort_keys=True))
        else:
            for key, label in (
                ("fema", "FEMA NRI tracts"),
                ("fema_counties", "FEMA NRI counties"),
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
            elif selected == "all":
                download_fema(paths, progress=_progress)
                output = download_fema_counties(paths, progress=_progress)
            else:
                _abort(HouseHunterError("Supported sources are fema, fema_counties, and all"))
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
) -> None:
    """List geographies from lowest to highest FEMA ALR_NPCTL."""
    selected = level.lower()
    if selected not in {"tract", "county"}:
        _abort(HouseHunterError("Rank level must be tract or county"))
    if selected == "county" and county:
        _abort(HouseHunterError("--county filters tracts; omit it when ranking counties"))
    try:
        with Store(_paths()) as store:
            if selected == "county":
                result = store.list_counties(
                    state=state,
                    limit=limit,
                    include_unranked=include_unranked,
                )
                typer.echo("COUNTY_FIPS  SCORE  STATE  NAME")
            else:
                result = store.list_places(
                    state=state,
                    county=county,
                    limit=limit,
                    include_unranked=include_unranked,
                )
                typer.echo("TRACT_ID     SCORE  STATE")
        for row in result["items"]:
            score = f"{row['risk_score']:.1f}" if row["risk_score"] is not None else "—"
            if selected == "county":
                typer.echo(
                    f"{row['place_id']:<12} {score:>5}  {row['state']:<5}  {row['name']}"
                )
            else:
                typer.echo(f"{row['place_id']:<12} {score:>5}  {row['state']}")
    except HouseHunterError as exc:
        _abort(exc)


@app.command("lookup")
def lookup_place(address: str) -> None:
    """Map a US address to a FEMA tract via the Census geocoder."""
    try:
        typer.echo(json.dumps(lookup_address(_paths(), address), indent=2, sort_keys=True))
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
