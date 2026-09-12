#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path

MAX_SECONDS = 55 * 60
MAX_RSS_BYTES = 24 * 1024**3
ENGINEERING_BYTES = 45_000_000_000
HARD_BYTES = 50_000_000_000


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


def run_once(command: list[str], mountain_root: Path) -> dict[str, object]:
    started = time.monotonic()
    swap_start = swap_used_bytes()
    peak_rss = 0
    peak_storage = allocated_bytes(mountain_root)
    peak_swap = swap_start
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # Sample before polling so even a command that exits between Popen and the first
    # loop condition contributes an RSS observation.
    peak_rss = process_tree_rss(process.pid)
    while process.poll() is None:
        peak_rss = max(peak_rss, process_tree_rss(process.pid))
        peak_storage = max(peak_storage, allocated_bytes(mountain_root))
        peak_swap = max(peak_swap, swap_used_bytes())
        time.sleep(0.25)
    stdout, stderr = process.communicate()
    peak_rss = max(peak_rss, process_tree_rss(process.pid))
    peak_storage = max(peak_storage, allocated_bytes(mountain_root))
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
    ).stdout.splitlines()
    if len(ranked) < 2:
        raise RuntimeError("Published snapshot has no queryable Mountain values")
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
        "compact_fallback_bytes": allocated_bytes(compact),
        "artifact_hashes": artifact_hashes,
        "build_completed": bool(stdout.strip()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the two-build Mountain acceptance gate")
    parser.add_argument("--prepared-pack", required=True, type=Path)
    parser.add_argument("--prepared-lock", required=True, type=Path)
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--data-release", required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--report", type=Path, default=Path("data/mountain/benchmark.json"))
    args = parser.parse_args()
    if args.data_dir:
        os.environ["HOUSEHUNTER_DATA_DIR"] = str(args.data_dir.resolve())
    data = Path(os.environ.get("HOUSEHUNTER_DATA_DIR", "data")).resolve()
    mountain_root = data / "mountain"
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
    runs = [run_once(command, mountain_root) for _ in range(2)]
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
    report = {"schema_version": 1, "runs": runs, "failures": failures}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(args.report)
    if failures:
        raise RuntimeError("; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
