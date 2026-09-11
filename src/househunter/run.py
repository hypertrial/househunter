"""Prepare local HouseHunter data and start the loopback web app."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .api import create_app
from .build import build_snapshot
from .config import RuntimePaths
from .download import download_fema
from .errors import HouseHunterError
from .locking import exclusive_lock
from .reference import reference_asset_status

Progress = Callable[[int, str], None]
Generate = Callable[[Path, Path, Path], None]
Download = Callable[..., Path]
Build = Callable[..., Path]
Serve = Callable[..., None]


def _progress(value: int, message: str) -> None:
    print(f"[{value:3d}%] {message}", file=sys.stderr)


def _assets_ok() -> bool:
    present, error = reference_asset_status()
    return present and error is None


def packaged_assets_directory() -> Path:
    return Path(__file__).resolve().parent / "assets"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def generator_script() -> Path:
    candidates = [
        Path.cwd() / "scripts" / "generate_reference_assets.py",
        repository_root() / "scripts" / "generate_reference_assets.py",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise HouseHunterError(
        "Cannot find scripts/generate_reference_assets.py. "
        "Run ./scripts/run-app from the HouseHunter checkout."
    )


def local_assets_directory(paths: RuntimePaths) -> Path:
    packaged = packaged_assets_directory()
    configured = os.environ.get("HOUSEHUNTER_ASSETS_DIR")
    target = (
        Path(configured).expanduser().resolve()
        if configured
        else (paths.data / "reference-assets").resolve()
    )
    if target == packaged:
        target = (paths.data / "reference-assets").resolve()
    os.environ["HOUSEHUNTER_ASSETS_DIR"] = str(target)
    return target


def default_generate(cache: Path, output: Path, fema_path: Path) -> None:
    script = generator_script()
    spec = importlib.util.spec_from_file_location("househunter_generate_reference_assets", script)
    if spec is None or spec.loader is None:
        raise HouseHunterError(f"Cannot load Census generator: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.generate(cache, output, fema_path)


def ensure_census_assets(
    paths: RuntimePaths,
    *,
    generate: Generate | None = default_generate,
    progress: Progress | None = None,
) -> None:
    if _assets_ok():
        return
    target = local_assets_directory(paths)
    if _assets_ok():
        return
    if generate is None:
        raise HouseHunterError(
            "Packaged Census reference assets are missing. "
            "See DATA_SOURCES.md, or omit --skip-prepare so this runner can generate a local copy."
        )
    if progress:
        progress(15, "Generating local Census reference assets")
    fema_path = paths.cache / "fema_nri_tracts.parquet"
    if not fema_path.is_file():
        raise HouseHunterError(
            "FEMA data is not cached; download it before generating Census assets"
        )
    try:
        generate(paths.data / "reference-source", target, fema_path)
    except HouseHunterError:
        raise
    except Exception as exc:
        raise HouseHunterError(f"Census reference generation failed: {exc}") from exc
    if _assets_ok():
        return
    _, error = reference_asset_status()
    raise HouseHunterError(
        error
        or (
            "Census reference assets are still missing after generation. "
            "See DATA_SOURCES.md for the maintainer workflow."
        )
    )


def prepare_runtime(
    paths: RuntimePaths,
    *,
    state: str | None = None,
    download: Download = download_fema,
    build: Build = build_snapshot,
    generate: Generate | None = default_generate,
    progress: Progress | None = None,
) -> Path:
    paths.ensure()
    with exclusive_lock(paths.job_lock):
        download(paths, progress=progress)
        ensure_census_assets(paths, generate=generate, progress=progress)
        if progress:
            progress(55, "Publishing ranking snapshot")
        return build(paths, state=state, progress=progress)


def serve_app(
    paths: RuntimePaths,
    *,
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"HouseHunter is running at {url}")
    uvicorn.run(create_app(paths), host="127.0.0.1", port=port, log_level="info")


def run(
    *,
    port: int = 8765,
    open_browser: bool = True,
    state: str | None = None,
    skip_prepare: bool = False,
    paths: RuntimePaths | None = None,
    download: Download = download_fema,
    build: Build = build_snapshot,
    generate: Generate | None = default_generate,
    serve: Serve = serve_app,
    progress: Progress | None = _progress,
) -> None:
    runtime = paths or RuntimePaths.from_root()
    if not skip_prepare:
        if progress:
            progress(0, "Preparing HouseHunter data")
        prepare_runtime(
            runtime,
            state=state,
            download=download,
            build=build,
            generate=generate,
            progress=progress,
        )
        if progress:
            progress(100, "Starting local app")
    serve(runtime, port=port, open_browser=open_browser)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare HouseHunter data and run the local loopback app."
    )
    parser.add_argument("--port", type=int, default=8765, help="Loopback port (default 8765)")
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    parser.add_argument(
        "--state",
        help="Optional two-letter snapshot scope. Census generation remains national.",
    )
    parser.add_argument(
        "--skip-prepare",
        action="store_true",
        help="Start the app without downloading, generating assets, or building",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.state is not None:
        state = args.state.strip().upper()
        if len(state) != 2 or not state.isalpha():
            parser.error("--state must be a two-letter abbreviation")
        args.state = state
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(
            port=args.port,
            open_browser=not args.no_open,
            state=args.state,
            skip_prepare=args.skip_prepare,
        )
    except HouseHunterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
