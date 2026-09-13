#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import duckdb

from househunter.config import RuntimePaths
from househunter.locking import exclusive_lock
from househunter.mountain import IN_SCOPE_STATES
from househunter.mountain_pack import ensure_storage_budget, remove_owned_work_directory
from househunter.mountain_paths import ensure_owned_child, ensure_safe_directory

MAX_SECONDS = 55 * 60
MAX_RSS_BYTES = 24 * 1024**3
ENGINEERING_BYTES = 45_000_000_000
HARD_BYTES = 50_000_000_000
RUN_RESERVATION_BYTES = 8_500_000_000
CONNECTICUT_PLANNING_REGIONS = {
    "09110",
    "09120",
    "09130",
    "09140",
    "09150",
    "09160",
    "09170",
    "09180",
    "09190",
}


def allocated_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        total += metadata.st_blocks * 512
        if current.is_dir() and not current.is_symlink():
            try:
                pending.extend(current.iterdir())
            except FileNotFoundError:
                continue
    return total


def swap_used_bytes() -> int:
    if platform.system() == "Darwin":
        output = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=True
        ).stdout
        used = output.split("used =", 1)[1].split("M", 1)[0].strip()
        return int(float(used) * 1024**2)
    memory = Path("/proc/meminfo").read_text().splitlines()
    values = {line.split(":", 1)[0]: int(line.split()[1]) * 1024 for line in memory}
    return values["SwapTotal"] - values["SwapFree"]


def process_tree_rss(root_pid: int) -> int:
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=True
    ).stdout
    rows = [tuple(map(int, line.split())) for line in output.splitlines() if line.strip()]
    selected = {root_pid}
    changed = True
    while changed:
        before = len(selected)
        selected.update(pid for pid, parent, _ in rows if parent in selected)
        changed = len(selected) != before
    return sum(rss_kib * 1024 for pid, _, rss_kib in rows if pid in selected)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_runtime_coverage(snapshot: Path) -> dict[str, object]:
    database = snapshot / "househunter.duckdb"
    expected = set(IN_SCOPE_STATES)
    summary: dict[str, object] = {}
    with duckdb.connect(str(database), read_only=True) as connection:
        for table in ("places", "counties"):
            rows = connection.execute(
                f"""SELECT state,
                           count(*) AS rows,
                           count(mountain_score) AS scored,
                           count(*) FILTER (
                               WHERE mountain_coverage_status = 'unavailable'
                           ) AS unavailable,
                           count(*) FILTER (
                               WHERE mountain_score_version != 'mountain_score_v1'
                                  OR mountain_pipeline_version != 'mountain_pipeline_v1'
                           ) AS wrong_version
                    FROM {table}
                    GROUP BY state"""
            ).fetchall()
            by_state = {str(row[0]): row[1:] for row in rows}
            if set(by_state) & expected != expected:
                raise RuntimeError(f"Published {table} omit an in-scope state or DC")
            fully_scored_states = expected - ({"CT"} if table == "counties" else set())
            if any(
                by_state[state][1] <= 0
                or by_state[state][2] != 0
                or by_state[state][3] != 0
                for state in fully_scored_states
            ):
                raise RuntimeError(
                    f"Published {table} lack usable, versioned Mountain scores in every state"
                )
            if table == "counties":
                connecticut = connection.execute(
                    """SELECT place_id, mountain_score, mountain_coverage_status,
                              mountain_score_version, mountain_pipeline_version
                       FROM counties
                       WHERE state = 'CT'
                       ORDER BY place_id"""
                ).fetchall()
                if connecticut != [
                    (place_id, None, "unavailable", None, None)
                    for place_id in sorted(CONNECTICUT_PLANNING_REGIONS)
                ]:
                    raise RuntimeError(
                        "Published counties differ from the Connecticut planning-region exception"
                    )
            outside = connection.execute(
                f"""SELECT count(*) FILTER (
                           WHERE mountain_coverage_status != 'outside_scope'
                        )
                    FROM {table}
                    WHERE state NOT IN ({','.join('?' for _ in expected)})""",
                sorted(expected),
            ).fetchone()[0]
            if outside:
                raise RuntimeError(f"Published {table} mislabel territory Mountain coverage")
            summary[table] = {
                "states": len(set(by_state) & expected),
                "scored_rows": sum(int(by_state[state][1]) for state in expected),
                "outside_scope_rows": sum(
                    int(values[0]) for state, values in by_state.items() if state not in expected
                ),
            }
    return summary


def run_once(
    command: list[str],
    mountain_root: Path,
    *,
    meter_root: Path | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    swap_start = swap_used_bytes()
    peak_rss = 0
    measured_root = meter_root or mountain_root
    peak_storage = allocated_bytes(measured_root)
    peak_swap = swap_start
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    # Sample before polling so even a command that exits between Popen and the first
    # loop condition contributes an RSS observation.
    peak_rss = process_tree_rss(process.pid)
    while process.poll() is None:
        peak_rss = max(peak_rss, process_tree_rss(process.pid))
        peak_storage = max(peak_storage, allocated_bytes(measured_root))
        peak_swap = max(peak_swap, swap_used_bytes())
        time.sleep(0.25)
    stdout, stderr = process.communicate()
    peak_rss = max(peak_rss, process_tree_rss(process.pid))
    peak_storage = max(peak_storage, allocated_bytes(measured_root))
    peak_swap = max(peak_swap, swap_used_bytes())
    duration = time.monotonic() - started
    if process.returncode:
        raise RuntimeError(f"Mountain build failed\n{stdout}\n{stderr}")
    pointer = json.loads((mountain_root / "current.json").read_text())
    release = mountain_root / "releases" / pointer["release_id"]
    manifest = json.loads((release / "manifest.json").read_text())
    snapshot_pointer = json.loads((mountain_root.parent / "current.json").read_text())
    snapshot = Path(snapshot_pointer["path"])
    snapshot_manifest = json.loads((snapshot / "build.json").read_text())
    if snapshot_manifest["source_vintages"]["mountain"] != manifest["data_release"]:
        raise RuntimeError("Published snapshot does not expose the promoted Mountain provenance")
    compact_pointer = json.loads((mountain_root / "compact" / "current.json").read_text())
    if compact_pointer.get("release_id") != pointer["release_id"]:
        raise RuntimeError("Compact Mountain fallback does not match the promoted release")
    compact = mountain_root / "compact" / pointer["release_id"]
    if allocated_bytes(compact) > 50 * 1024**2:
        raise RuntimeError("Compact Mountain fallback exceeds 50 MiB")
    ranked = subprocess.run(
        ["uv", "run", "househunter", "rank", "--metric", "mountain", "--limit", "1"],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    ).stdout.splitlines()
    if len(ranked) < 2:
        raise RuntimeError("Published snapshot has no queryable Mountain values")
    runtime_coverage = validate_runtime_coverage(snapshot)
    artifact_hashes = {
        name: sha256_file(release / metadata["filename"])
        for name, metadata in manifest["files"].items()
    }
    return {
        "duration_seconds": round(duration, 3),
        "peak_aggregate_rss_bytes": peak_rss,
        "peak_managed_storage_bytes": peak_storage,
        "swap_start_bytes": swap_start,
        "peak_swap_bytes": peak_swap,
        "release_id": pointer["release_id"],
        "snapshot_id": snapshot_manifest["build_id"],
        "runtime_smoke": ranked[-1],
        "runtime_coverage": runtime_coverage,
        "compact_fallback_bytes": allocated_bytes(compact),
        "artifact_hashes": artifact_hashes,
        "build_completed": bool(stdout.strip()),
        "release_path": str(release),
    }


def machine_preconditions() -> dict[str, object]:
    if platform.system() != "Darwin":
        raise RuntimeError("The official Mountain benchmark requires the target macOS laptop")
    chip = subprocess.run(
        ["sysctl", "-n", "machdep.cpu.brand_string"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    memory = int(
        subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    power = subprocess.run(
        ["pmset", "-g", "batt"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    if "M4" not in chip or memory < 30 * 1024**3 or "AC Power" not in power:
        raise RuntimeError("The official Mountain benchmark requires M4, 32 GB, and AC power")
    return {"chip": chip, "memory_bytes": memory, "power": "AC"}


def seed_shadow_data(source: Path, destination: Path) -> None:
    destination.mkdir()
    for name in ("cache", "raw", "processed"):
        current = source / name
        if current.is_dir():
            shutil.copytree(current, destination / name, copy_function=os.link)
    if (source / "source_manifest.parquet").is_file():
        os.link(source / "source_manifest.parquet", destination / "source_manifest.parquet")


def write_report(
    path: Path,
    *,
    machine: dict[str, object],
    runs: list[dict[str, object]],
    failures: list[str],
    promotion: dict[str, object],
) -> None:
    report = {
        "schema_version": 1,
        "machine": machine,
        "runs": [
            {key: value for key, value in run.items() if key != "release_path"}
            for run in runs
        ],
        "failures": failures,
        "promotion": promotion,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the two-build Mountain acceptance gate")
    parser.add_argument("--prepared-pack", required=True, type=Path)
    parser.add_argument("--prepared-lock", required=True, type=Path)
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--data-release", required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--report", type=Path, default=Path("data/mountain/benchmark.json"))
    args = parser.parse_args()
    machine = machine_preconditions()
    data = (
        args.data_dir.resolve()
        if args.data_dir
        else RuntimePaths.from_root().data
    )
    mountain_root = ensure_safe_directory(data / "mountain")
    work_root = ensure_safe_directory(mountain_root / "work")
    runs = []
    second_release: Path | None = None
    acceptance: Path | None = None
    with exclusive_lock(data / ".mutating-job.lock"):
        acceptance = ensure_owned_child(
            work_root / uuid.uuid4().hex,
            work_root,
            name_pattern=r"[0-9a-f]{32}",
            marker_value="benchmark-v1\n",
        )
        ensure_storage_budget(mountain_root, reserve_bytes=RUN_RESERVATION_BYTES)
        try:
            for index in (1, 2):
                shadow = acceptance / f"run-{index}"
                seed_shadow_data(data, shadow)
                environment = {**os.environ, "HOUSEHUNTER_DATA_DIR": str(shadow)}
                command = [
                    "uv",
                    "run",
                    "househunter",
                    "mountain",
                    "build",
                    "--data-release",
                    args.data_release,
                    "--prepared-pack",
                    str(args.prepared_pack.resolve()),
                    "--prepared-lock",
                    str(args.prepared_lock.resolve()),
                    "--source-lock",
                    str(args.source_lock.resolve()),
                    "--workers",
                    "4",
                    "--fresh",
                ]
                run = run_once(
                    command,
                    shadow / "mountain",
                    meter_root=mountain_root,
                    environment=environment,
                )
                runs.append(run)
                if index == 1:
                    shutil.rmtree(shadow)
                else:
                    second_release = Path(str(run["release_path"]))
        except BaseException:
            remove_owned_work_directory(acceptance, work_root)
            raise
    assert acceptance is not None
    failures = []
    for index, run in enumerate(runs, 1):
        if run["duration_seconds"] >= MAX_SECONDS:
            failures.append(f"run {index} exceeded 55 minutes")
        if run["peak_aggregate_rss_bytes"] > MAX_RSS_BYTES:
            failures.append(f"run {index} exceeded 24 GiB aggregate RSS")
        if run["peak_managed_storage_bytes"] > ENGINEERING_BYTES:
            failures.append(f"run {index} exceeded the 45 GB engineering ceiling")
        if run["peak_managed_storage_bytes"] >= HARD_BYTES:
            failures.append(f"run {index} reached the 50 GB hard cap")
        if run["peak_swap_bytes"] > run["swap_start_bytes"]:
            failures.append(f"run {index} caused swap growth")
    if runs[0]["release_id"] != runs[1]["release_id"]:
        failures.append("fresh runs produced different release identities")
    if runs[0]["artifact_hashes"] != runs[1]["artifact_hashes"]:
        failures.append("fresh runs produced different Parquet hashes")
    if failures:
        write_report(
            args.report,
            machine=machine,
            runs=runs,
            failures=failures,
            promotion={"status": "not_attempted"},
        )
        remove_owned_work_directory(acceptance, work_root)
        raise RuntimeError("; ".join(failures))
    assert second_release is not None
    promote = [
        "uv",
        "run",
        "househunter",
        "mountain",
        "validate",
        str(second_release),
        "--promote",
        "--source-lock",
        str(args.source_lock.resolve()),
        "--prepared-pack",
        str(args.prepared_pack.resolve()),
        "--prepared-lock",
        str(args.prepared_lock.resolve()),
        "--workers",
        "4",
    ]
    try:
        subprocess.run(
            promote,
            check=True,
            env={**os.environ, "HOUSEHUNTER_DATA_DIR": str(data)},
        )
        pointer = json.loads((mountain_root / "current.json").read_text())
        if pointer.get("release_id") != runs[1]["release_id"]:
            raise RuntimeError("Promoted Mountain release differs from the accepted second run")
        write_report(
            args.report,
            machine=machine,
            runs=runs,
            failures=[],
            promotion={"status": "promoted", "release_id": pointer["release_id"]},
        )
        print(args.report)
    except BaseException as exc:
        write_report(
            args.report,
            machine=machine,
            runs=runs,
            failures=["accepted release promotion failed"],
            promotion={"status": "failed", "error_type": type(exc).__name__},
        )
        raise
    finally:
        remove_owned_work_directory(acceptance, work_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
