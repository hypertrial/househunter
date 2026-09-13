from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest


def _load_script():  # type: ignore[no-untyped-def]
    script = Path(__file__).parents[1] / "scripts" / "compare_mountain_builds.py"
    spec = importlib.util.spec_from_file_location("compare_mountain_builds", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_equivalence_failure_uses_managed_scratch_and_cleans_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    data = tmp_path / "data"
    data.mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    inputs = [tmp_path / name for name in ("source.json", "regions.json", "pack", "prepared.json")]
    for path in inputs:
        path.touch()
    lock_events: list[str] = []

    class Lock:
        def __enter__(self):  # type: ignore[no-untyped-def]
            lock_events.append("enter")

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            lock_events.append("exit")

    frame = pl.DataFrame({"block_geoid": ["010010001001001"]})
    monkeypatch.setattr(
        script.RuntimePaths,
        "from_root",
        lambda: SimpleNamespace(data=data, job_lock=tmp_path / "job.lock"),
    )
    monkeypatch.setattr(script, "exclusive_lock", lambda path: Lock())
    monkeypatch.setattr(
        script,
        "verify_source_lock",
        lambda *args, **kwargs: {
            "expected_states": {},
            "block_geoid_sha256": "0" * 64,
        },
    )
    monkeypatch.setattr(
        script,
        "verify_prepared_pack",
        lambda *args, **kwargs: {
            "source_provenance": {},
            "source_lock_schema_version": 2,
            "source_lock_sha256": "0" * 64,
            "pack_id": "a" * 64,
        },
    )
    monkeypatch.setattr(script, "load_regions", lambda *args: [object()])
    monkeypatch.setattr(script, "verify_region_sources_locked", lambda *args, **kwargs: None)
    monkeypatch.setattr(script, "build_region_raw_metrics", lambda *args, **kwargs: frame)
    monkeypatch.setattr(script, "validate_national_expectations", lambda *args, **kwargs: None)
    monkeypatch.setattr(script, "ensure_storage_budget", lambda *args, **kwargs: None)
    def write_release(frame, destination, **kwargs):  # type: ignore[no-untyped-def]
        destination.mkdir()
        (destination / "manifest.json").write_text(
            json.dumps({"release_id": "same", "files": {}})
        )
        return destination

    monkeypatch.setattr(script, "write_release", write_release)
    observed_work: list[Path] = []

    def fail_build(pack, lock, work, **kwargs):  # type: ignore[no-untyped-def]
        observed_work.append(work)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(script, "build_prepared_raw_metrics", fail_build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_mountain_builds.py",
            "--source-lock",
            str(inputs[0]),
            "--regions",
            str(inputs[1]),
            "--source-root",
            str(source_root),
            "--prepared-pack",
            str(inputs[2]),
            "--prepared-lock",
            str(inputs[3]),
            "--data-release",
            "fixture",
        ],
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        script.main()

    work_root = data / "mountain" / "work"
    assert observed_work and observed_work[0].parent.parent == work_root
    assert not list(work_root.glob("[0-9a-f]" * 32))
    assert lock_events == ["enter", "exit"]


def test_equivalence_releases_lock_when_validation_fails_before_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    data = tmp_path / "data"
    data.mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    inputs = [tmp_path / name for name in ("source.json", "regions.json", "pack", "prepared.json")]
    for path in inputs:
        path.touch()
    events: list[str] = []

    class Lock:
        def __enter__(self):  # type: ignore[no-untyped-def]
            events.append("enter")

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            events.append("exit")

    monkeypatch.setattr(
        script.RuntimePaths,
        "from_root",
        lambda: SimpleNamespace(data=data, job_lock=tmp_path / "job.lock"),
    )
    monkeypatch.setattr(script, "exclusive_lock", lambda path: Lock())
    monkeypatch.setattr(
        script,
        "verify_source_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("invalid lock")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_mountain_builds.py",
            "--source-lock",
            str(inputs[0]),
            "--regions",
            str(inputs[1]),
            "--source-root",
            str(source_root),
            "--prepared-pack",
            str(inputs[2]),
            "--prepared-lock",
            str(inputs[3]),
            "--data-release",
            "fixture",
        ],
    )

    with pytest.raises(RuntimeError, match="invalid lock"):
        script.main()

    assert events == ["enter", "exit"]


def test_equivalence_rejects_direct_source_for_fragment_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    paths = [tmp_path / name for name in ("source.json", "regions.json", "pack", "lock.json")]
    for path in paths:
        path.touch()
    monkeypatch.setattr(
        script,
        "verify_source_lock",
        lambda *args, **kwargs: {"trail_fragment_mode": "state_clipped_globalid_v1"},
    )
    args = SimpleNamespace(
        source_lock=paths[0],
        source_root=source_root,
        prepared_pack=paths[2],
        prepared_lock=paths[3],
    )

    with pytest.raises(RuntimeError, match="requires source-derived reference shards"):
        script._run_locked(args, SimpleNamespace(data=tmp_path / "data"))
