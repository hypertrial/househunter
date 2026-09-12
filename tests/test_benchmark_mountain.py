from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def test_run_once_records_post_exit_storage_and_swap(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    script = Path(__file__).parents[1] / "scripts" / "benchmark_mountain.py"
    spec = importlib.util.spec_from_file_location("benchmark_mountain", script)
    assert spec and spec.loader
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)

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

    result = benchmark.run_once(["command"], mountain)

    assert result["peak_managed_storage_bytes"] == 99
    assert result["peak_swap_bytes"] == 7
    assert result["peak_aggregate_rss_bytes"] == 84
