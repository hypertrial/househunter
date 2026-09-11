from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from househunter.config import RuntimePaths
from househunter.errors import BuildNotFoundError, HouseHunterError
from househunter.reference import ReferenceAssets
from househunter.run import main, parse_args, run
from househunter.store import current_build


def _hide_packaged_assets(monkeypatch: pytest.MonkeyPatch, absent: Path) -> None:
    def fake_paths() -> ReferenceAssets:
        configured = os.environ.get("HOUSEHUNTER_ASSETS_DIR")
        root = Path(configured).expanduser().resolve() if configured else absent
        return ReferenceAssets(
            places=root / "places_2020.parquet",
            weights=root / "place_tract_weights_2020.parquet",
            metadata=root / "reference_metadata.json",
        )

    monkeypatch.setattr("househunter.reference.reference_asset_paths", fake_paths)


def test_skip_prepare_starts_server_without_data_work(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    events: list[str] = []

    def download(*_args: object, **_kwargs: object) -> Path:
        events.append("download")
        return paths.cache / "fema_nri_tracts.parquet"

    def generate(*_args: object, **_kwargs: object) -> None:
        events.append("generate")

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
        generate=generate,
        build=build,
        serve=serve,
    )
    assert events == ["serve:9001:False"]


def test_prepare_reuses_fixture_assets_then_builds_and_serves(
    fixture_environment: tuple[RuntimePaths, object],
) -> None:
    paths, _ = fixture_environment
    events: list[str] = []

    def generate(*_args: object, **_kwargs: object) -> None:
        events.append("generate")

    def serve(_paths: RuntimePaths, *, port: int, open_browser: bool) -> None:
        events.append(f"serve:{port}:{open_browser}")

    run(
        paths=paths,
        port=8765,
        open_browser=True,
        generate=generate,
        serve=serve,
    )
    assert "generate" not in events
    assert events == ["serve:8765:True"]
    current_build(paths)


def test_missing_assets_generate_into_data_then_build(
    fixture_environment: tuple[RuntimePaths, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, assets = fixture_environment
    monkeypatch.delenv("HOUSEHUNTER_ASSETS_DIR", raising=False)
    _hide_packaged_assets(monkeypatch, tmp_path / "absent-packaged")
    generated: list[tuple[Path, Path, Path]] = []

    def generate(cache: Path, output: Path, fema_path: Path) -> None:
        generated.append((cache, output, fema_path))
        output.mkdir(parents=True, exist_ok=True)
        for name in (
            "places_2020.parquet",
            "place_tract_weights_2020.parquet",
            "reference_metadata.json",
        ):
            shutil.copy2(assets / name, output / name)

    served: list[tuple[int, bool]] = []

    def serve(_paths: RuntimePaths, *, port: int, open_browser: bool) -> None:
        served.append((port, open_browser))

    run(
        paths=paths,
        port=8123,
        open_browser=False,
        generate=generate,
        serve=serve,
    )
    assert generated
    cache, output, fema_path = generated[0]
    assert cache == paths.data / "reference-source"
    assert output == paths.data / "reference-assets"
    assert fema_path == paths.cache / "fema_nri_tracts.parquet"
    assert output != assets
    assert served == [(8123, False)]
    current_build(paths)


def test_download_failure_does_not_start_server(
    fixture_environment: tuple[RuntimePaths, object],
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


def test_generate_failure_does_not_start_server(
    fixture_environment: tuple[RuntimePaths, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _ = fixture_environment
    monkeypatch.delenv("HOUSEHUNTER_ASSETS_DIR", raising=False)
    _hide_packaged_assets(monkeypatch, tmp_path / "absent-packaged")
    served = False

    def generate(*_args: object, **_kwargs: object) -> None:
        raise HouseHunterError("generation failed")

    def serve(*_args: object, **_kwargs: object) -> None:
        nonlocal served
        served = True

    with pytest.raises(HouseHunterError, match="generation failed"):
        run(paths=paths, generate=generate, serve=serve)
    assert served is False


def test_generate_value_error_is_mapped_and_does_not_start_server(
    fixture_environment: tuple[RuntimePaths, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _ = fixture_environment
    monkeypatch.delenv("HOUSEHUNTER_ASSETS_DIR", raising=False)
    _hide_packaged_assets(monkeypatch, tmp_path / "absent-packaged")
    served = False

    def generate(*_args: object, **_kwargs: object) -> None:
        raise ValueError("truncated DBF header")

    def serve(*_args: object, **_kwargs: object) -> None:
        nonlocal served
        served = True

    with pytest.raises(HouseHunterError, match="Census reference generation failed"):
        run(paths=paths, generate=generate, serve=serve)
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


def test_run_app_script_syncs_then_execs_module() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "run-app"
    text = script.read_text()
    assert "uv sync" in text
    assert "python -m househunter.run" in text
    assert script.stat().st_mode & 0o111
