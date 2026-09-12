"""Prepare local HouseHunter data and start the loopback web app."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .api import create_app
from .build import build_snapshot
from .chrr import download_chrr
from .config import RuntimePaths
from .download import download_fema, download_fema_counties
from .errors import HouseHunterError
from .locking import exclusive_lock

Progress = Callable[[int, str], None]
Cancelled = Callable[[], bool]
Download = Callable[..., Path]
Build = Callable[..., Path]
Serve = Callable[..., None]


def _progress(value: int, message: str) -> None:
    print(f"[{value:3d}%] {message}", file=sys.stderr)


def _scale_progress(
    progress: Progress | None, start: int, end: int
) -> Progress | None:
    if progress is None:
        return None

    def scaled(value: int, message: str) -> None:
        clamped = min(100, max(0, value))
        progress(start + (end - start) * clamped // 100, message)

    return scaled


def prepare_runtime(
    paths: RuntimePaths,
    *,
    state: str | None = None,
    download: Download = download_fema,
    download_counties: Download = download_fema_counties,
    download_community_conditions: Download = download_chrr,
    build: Build = build_snapshot,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
    hold_lock: bool = True,
) -> Path:
    paths.ensure()

    def _run() -> Path:
        download(paths, progress=_scale_progress(progress, 0, 35), cancelled=cancelled)
        download_counties(paths, progress=_scale_progress(progress, 35, 50), cancelled=cancelled)
        download_community_conditions(
            paths, progress=_scale_progress(progress, 50, 60), cancelled=cancelled
        )
        if progress:
            progress(60, "Publishing ranking snapshot")
        return build(
            paths,
            state=state,
            progress=_scale_progress(progress, 60, 99),
            cancelled=cancelled,
        )

    if hold_lock:
        with exclusive_lock(paths.job_lock):
            return _run()
    return _run()


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
    download_counties: Download = download_fema_counties,
    download_community_conditions: Download = download_chrr,
    build: Build = build_snapshot,
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
            download_counties=download_counties,
            download_community_conditions=download_community_conditions,
            build=build,
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
        help="Optional two-letter snapshot scope.",
    )
    parser.add_argument(
        "--skip-prepare",
        action="store_true",
        help="Start the app without downloading or building",
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
