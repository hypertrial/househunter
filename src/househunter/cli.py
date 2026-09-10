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
from .download import download_fema, source_status
from .errors import AmbiguousPlaceError, HouseHunterError
from .locking import exclusive_lock
from .reference import reference_asset_status
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
        status = source_status(_paths()).model_dump(mode="json")
        census_cached, census_error = reference_asset_status()
        result = {
            "fema": {
                **status,
                "release": config["fema"]["release"],
                "url": config["fema"]["item_url"],
            },
            "census": {
                "decennial_vintage": config["census"]["decennial_vintage"],
                "acs_vintage": config["census"]["acs_vintage"],
                "packaged": census_cached,
                "error": census_error,
            },
        }
        if json_output:
            typer.echo(json.dumps(result, indent=2, sort_keys=True))
        else:
            census_state = (
                "invalid" if census_error else "packaged" if census_cached else "assets missing"
            )
            typer.echo(
                f"FEMA NRI {result['fema']['release']} ({result['fema']['version']}): "
                f"{'cached' if result['fema']['cached'] else 'not downloaded'}"
            )
            typer.echo(f"Census: 2020 Decennial; 2024 ACS 5-year ({census_state})")
            if result["fema"]["error"]:
                typer.echo(f"Cache error: {result['fema']['error']}", err=True)
            if census_error:
                typer.echo(f"Reference asset error: {census_error}", err=True)
    except HouseHunterError as exc:
        _abort(exc)


@app.command()
def download(
    source: Annotated[str, typer.Option("--source", help="Source to download")] = "fema",
) -> None:
    """Download and verify pinned source data."""
    if source.lower() != "fema":
        _abort(HouseHunterError("V1 supports only --source fema"))
    paths = _paths()
    try:
        with exclusive_lock(paths.job_lock):
            output = download_fema(paths, progress=_progress)
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
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 25,
    min_population: Annotated[int | None, typer.Option("--min-population", min=0)] = None,
    include_unranked: Annotated[bool, typer.Option("--include-unranked")] = False,
) -> None:
    """List Places from lowest to highest risk score."""
    try:
        with Store(_paths()) as store:
            result = store.list_places(
                state=state,
                limit=limit,
                min_population=min_population,
                include_unranked=include_unranked,
            )
        typer.echo("PLACE_ID  SCORE  POPULATION  PLACE")
        for row in result["items"]:
            score = f"{row['risk_score']:.1f}" if row["risk_score"] is not None else "—"
            population = (
                row["population_2024"]
                if row["population_2024"] is not None
                else row["population_2020"]
            )
            typer.echo(
                f"{row['place_id']:<9} {score:>5}  {population:>10,}  {row['name']}, {row['state']}"
            )
    except HouseHunterError as exc:
        _abort(exc)


@app.command("inspect")
def inspect_place(query: str) -> None:
    """Inspect a Place by ID or exact 'Name, ST'."""
    try:
        with Store(_paths()) as store:
            place_id = store.resolve_place(query)
            detail = store.place_detail(place_id)
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
) -> None:
    """Export the selected immutable Place snapshot."""
    selected = format.lower()
    if selected not in {"csv", "parquet"}:
        _abort(HouseHunterError("Export format must be csv or parquet"))
    destination = (output or Path.cwd() / f"househunter-places.{selected}").expanduser().resolve()
    try:
        with Store(_paths()) as store:
            store.export(selected, destination)
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
