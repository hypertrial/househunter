from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest


def _load_benchmark():  # type: ignore[no-untyped-def]
    script = Path(__file__).parents[1] / "scripts" / "benchmark_mountain.py"
    spec = importlib.util.spec_from_file_location("benchmark_mountain", script)
    assert spec and spec.loader
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    return benchmark


def test_run_once_records_post_exit_storage_and_swap(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    benchmark = _load_benchmark()

    mountain = tmp_path / "mountain"
    release = mountain / "releases" / "release"
    compact = mountain / "compact" / "release"
    snapshot = tmp_path / "build"
    release.mkdir(parents=True)
    compact.mkdir(parents=True)
    snapshot.mkdir()
    (mountain / "current.json").write_text(json.dumps({"release_id": "release"}))
    (mountain / "compact" / "current.json").write_text(
        json.dumps({"release_id": "release"})
    )
    (tmp_path / "current.json").write_text(json.dumps({"path": str(snapshot)}))
    (release / "data.parquet").write_bytes(b"data")
    (release / "manifest.json").write_text(json.dumps({
        "data_release": "fixture", "files": {"data": {"filename": "data.parquet"}}
    }))
    (snapshot / "build.json").write_text(json.dumps({
        "build_id": "build", "source_vintages": {"mountain": "fixture"}
    }))

    class Process:
        returncode = 0
        pid = 1

        def poll(self):  # type: ignore[no-untyped-def]
            return 0

        def communicate(self):  # type: ignore[no-untyped-def]
            return "done", ""

    allocated = iter([1, 99, 3, 3])
    swap = iter([5, 7])
    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        benchmark.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="header\nrow\n")
    )
    monkeypatch.setattr(benchmark, "allocated_bytes", lambda path: next(allocated))
    monkeypatch.setattr(benchmark, "swap_used_bytes", lambda: next(swap))
    rss = iter([42, 84])
    monkeypatch.setattr(benchmark, "process_tree_rss", lambda pid: next(rss))
    monkeypatch.setattr(benchmark, "sha256_file", lambda path: "hash")
    monkeypatch.setattr(
        benchmark,
        "validate_runtime_coverage",
        lambda path: {"places": {"states": 51}, "counties": {"states": 51}},
    )

    result = benchmark.run_once(["command"], mountain)

    assert result["peak_managed_storage_bytes"] == 99
    assert result["peak_swap_bytes"] == 7
    assert result["peak_aggregate_rss_bytes"] == 84


def test_write_report_redacts_shadow_release_path(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    report = tmp_path / "benchmark.json"

    benchmark.write_report(
        report,
        machine={"chip": "Apple M4"},
        runs=[{"release_id": "release", "release_path": "/private/local/release"}],
        failures=[],
        promotion={"status": "promoted", "release_id": "release"},
    )

    payload = json.loads(report.read_text())
    assert "release_path" not in payload["runs"][0]
    assert payload["promotion"] == {"status": "promoted", "release_id": "release"}


def test_machine_preconditions_require_target_laptop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    benchmark = _load_benchmark()
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Darwin")
    outputs = iter(["Apple M3\n", str(32 * 1024**3), "Now drawing from 'AC Power'\n"])
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=next(outputs)),
    )

    with pytest.raises(RuntimeError, match="requires M4"):
        benchmark.machine_preconditions()


def _accepted_run(release: Path) -> dict[str, object]:
    return {
        "duration_seconds": 1,
        "peak_aggregate_rss_bytes": 1,
        "peak_managed_storage_bytes": 1,
        "swap_start_bytes": 1,
        "peak_swap_bytes": 1,
        "release_id": "same-release",
        "artifact_hashes": {"blocks": "same-hash"},
        "release_path": str(release),
    }


def test_runtime_coverage_accepts_only_the_connecticut_county_exception(
    tmp_path: Path,
) -> None:
    benchmark = _load_benchmark()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    database = snapshot / "househunter.duckdb"
    columns = """place_id VARCHAR, state VARCHAR, mountain_score DOUBLE,
                 mountain_coverage_status VARCHAR, mountain_score_version VARCHAR,
                 mountain_pipeline_version VARCHAR"""
    with duckdb.connect(str(database)) as connection:
        connection.execute(f"CREATE TABLE places ({columns})")
        connection.execute(f"CREATE TABLE counties ({columns})")

        def scored(place_id: str, state: str) -> tuple[object, ...]:
            return (
                place_id,
                state,
                50.0,
                "complete",
                "mountain_score_v1",
                "mountain_pipeline_v1",
            )

        connection.executemany(
            "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?)",
            [
                scored(f"{index:011d}", state)
                for index, state in enumerate(benchmark.IN_SCOPE_STATES)
            ],
        )
        connection.executemany(
            "INSERT INTO counties VALUES (?, ?, ?, ?, ?, ?)",
            [
                scored(f"{index:05d}", state)
                for index, state in enumerate(benchmark.IN_SCOPE_STATES)
                if state != "CT"
            ]
            + [
                (place_id, "CT", None, "unavailable", None, None)
                for place_id in sorted(benchmark.CONNECTICUT_PLANNING_REGIONS)
            ],
        )

    assert benchmark.validate_runtime_coverage(snapshot)["counties"]["states"] == 51

    with duckdb.connect(str(database)) as connection:
        connection.execute("UPDATE counties SET place_id = '09999' WHERE place_id = '09110'")
    with pytest.raises(RuntimeError, match="Connecticut planning-region exception"):
        benchmark.validate_runtime_coverage(snapshot)


def test_promotion_failure_is_reported_and_shadow_work_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    benchmark = _load_benchmark()
    data = tmp_path / "data"
    data.mkdir()
    report = tmp_path / "benchmark.json"
    prepared = tmp_path / "pack"
    prepared_lock = tmp_path / "prepared-lock.json"
    source_lock = tmp_path / "source-lock.json"
    for path in (prepared, prepared_lock, source_lock):
        path.touch()
    monkeypatch.setattr(benchmark, "machine_preconditions", lambda: {"chip": "Apple M4"})
    monkeypatch.setattr(benchmark, "ensure_storage_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        benchmark,
        "seed_shadow_data",
        lambda source, destination: destination.mkdir(),
    )
    monkeypatch.setattr(
        benchmark,
        "run_once",
        lambda command, mountain_root, **kwargs: _accepted_run(
            mountain_root / "releases" / "same-release"
        ),
    )
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, args[0])
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_mountain.py",
            "--prepared-pack",
            str(prepared),
            "--prepared-lock",
            str(prepared_lock),
            "--source-lock",
            str(source_lock),
            "--data-release",
            "fixture",
            "--data-dir",
            str(data),
            "--report",
            str(report),
        ],
    )

    with pytest.raises(subprocess.CalledProcessError):
        benchmark.main()

    payload = json.loads(report.read_text())
    assert payload["failures"] == ["accepted release promotion failed"]
    assert payload["promotion"] == {
        "status": "failed",
        "error_type": "CalledProcessError",
    }
    work = data / "mountain" / "work"
    assert not list(work.glob("[0-9a-f]" * 32))


def test_failed_acceptance_never_attempts_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    benchmark = _load_benchmark()
    data = tmp_path / "data"
    data.mkdir()
    report = tmp_path / "benchmark.json"
    inputs = [tmp_path / name for name in ("pack", "prepared.json", "source.json")]
    for path in inputs:
        path.touch()
    monkeypatch.setattr(benchmark, "machine_preconditions", lambda: {"chip": "Apple M4"})
    monkeypatch.setattr(benchmark, "ensure_storage_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        benchmark,
        "seed_shadow_data",
        lambda source, destination: destination.mkdir(),
    )
    failing = _accepted_run(tmp_path / "release")
    failing["duration_seconds"] = benchmark.MAX_SECONDS
    monkeypatch.setattr(benchmark, "run_once", lambda *args, **kwargs: failing.copy())
    promoted = False

    def unexpected_promotion(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal promoted
        promoted = True

    monkeypatch.setattr(benchmark.subprocess, "run", unexpected_promotion)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_mountain.py",
            "--prepared-pack",
            str(inputs[0]),
            "--prepared-lock",
            str(inputs[1]),
            "--source-lock",
            str(inputs[2]),
            "--data-release",
            "fixture",
            "--data-dir",
            str(data),
            "--report",
            str(report),
        ],
    )

    with pytest.raises(RuntimeError, match="exceeded 55 minutes"):
        benchmark.main()

    assert promoted is False
    payload = json.loads(report.read_text())
    assert payload["promotion"] == {"status": "not_attempted"}
    assert payload["failures"] == ["run 1 exceeded 55 minutes", "run 2 exceeded 55 minutes"]
