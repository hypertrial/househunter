from __future__ import annotations

from pathlib import Path

import pytest

import househunter.cli as cli_module
import househunter.run as run_module
from househunter.config import RuntimePaths
from househunter.errors import BuildNotFoundError, HouseHunterError
from househunter.run import main, parse_args, run
from househunter.store import current_build


def test_skip_prepare_starts_server_without_data_work(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, _ = fixture_environment
    events: list[str] = []

    def download(*_args: object, **_kwargs: object) -> Path:
        events.append("download")
        return paths.cache / "fema_nri_tracts.parquet"

    def build(*_args: object, **_kwargs: object) -> Path:
        events.append("build")
        return paths.builds / "unused"

    def serve(_paths: RuntimePaths, *, port: int, open_browser: bool) -> None:
        events.append(f"serve:{port}:{open_browser}")

    run(
        paths=paths,
        skip_prepare=True,
        port=9001,
        open_browser=False,
        download=download,
        build=build,
        serve=serve,
    )
    assert events == ["serve:9001:False"]


def test_server_entry_points_disable_unused_lifespan(
    fixture_environment: tuple[RuntimePaths, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = fixture_environment
    calls: list[dict[str, object]] = []

    def fake_uvicorn_run(_app: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(run_module.uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(cli_module, "_paths", lambda: paths)

    run_module.serve_app(paths, port=8877, open_browser=False)
    cli_module.run_app(port=8878, no_open=True)

    assert calls == [
        {"host": "127.0.0.1", "port": 8877, "log_level": "info", "lifespan": "off"},
        {"host": "127.0.0.1", "port": 8878, "log_level": "info", "lifespan": "off"},
    ]


def test_prepare_downloads_builds_and_serves_without_census(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, _ = fixture_environment
    events: list[str] = []

    def serve(_paths: RuntimePaths, *, port: int, open_browser: bool) -> None:
        events.append(f"serve:{port}:{open_browser}")

    run(paths=paths, port=8765, open_browser=True, serve=serve)
    assert events == ["serve:8765:True"]
    current_build(paths)


def test_prepare_progress_is_non_decreasing(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, _ = fixture_environment
    values: list[int] = []

    def progress(value: int, message: str) -> None:
        values.append(value)

    def serve(*_args: object, **_kwargs: object) -> None:
        return None

    run(paths=paths, open_browser=False, serve=serve, progress=progress)
    assert values
    assert values == sorted(values)
    assert values[0] == 0
    assert values[-1] == 100


def test_download_failure_does_not_start_server(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, _ = fixture_environment
    served = False

    def download(*_args: object, **_kwargs: object) -> Path:
        raise HouseHunterError("download failed")

    def serve(*_args: object, **_kwargs: object) -> None:
        nonlocal served
        served = True

    with pytest.raises(HouseHunterError, match="download failed"):
        run(paths=paths, download=download, serve=serve)
    assert served is False
    with pytest.raises(BuildNotFoundError):
        current_build(paths)


def test_missing_fema_cache_does_not_start_server(
    fixture_environment: tuple[RuntimePaths, Path],
) -> None:
    paths, _ = fixture_environment
    (paths.cache / "fema_nri_tracts.parquet").unlink()
    served = False

    def download(*_args: object, **_kwargs: object) -> Path:
        return paths.cache / "fema_nri_tracts.parquet"

    def serve(*_args: object, **_kwargs: object) -> None:
        nonlocal served
        served = True

    with pytest.raises(HouseHunterError, match="FEMA data is not cached"):
        run(paths=paths, download=download, serve=serve)
    assert served is False


def test_main_forwards_flags_and_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        captured.update(kwargs)
        raise HouseHunterError("boom")

    monkeypatch.setattr("househunter.run.run", fake_run)
    assert main(["--port", "9999", "--no-open", "--state", "CO", "--skip-prepare"]) == 1
    assert captured["port"] == 9999
    assert captured["open_browser"] is False
    assert captured["state"] == "CO"
    assert captured["skip_prepare"] is True


def test_parse_args_rejects_invalid_port() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--port", "0"])


def test_parse_args_normalizes_and_rejects_state() -> None:
    assert parse_args(["--state", "co"]).state == "CO"
    with pytest.raises(SystemExit):
        parse_args(["--state", "COLO"])


def test_dev_script_syncs_then_execs_module() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "dev"
    text = script.read_text()
    assert "uv sync" in text
    assert "python -m househunter.run" in text
    assert script.stat().st_mode & 0o111

    legacy_script = script.with_name("run-app")
    assert 'exec "$(dirname "$0")/dev" "$@"' in legacy_script.read_text()
    assert legacy_script.stat().st_mode & 0o111
