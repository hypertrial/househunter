#!/usr/bin/env python3
"""Reject private home-market rows and derivatives from source and distributions."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_LOCK = REPO_ROOT / "config" / "home-market" / "release-lock.json"
ALLOWED_LOCK_PATHS = {
    "config/home-market/release-lock.json",
    "househunter/assets/home_market_release_lock.json",
}
SYNTHETIC_FIXTURE_SHA256 = {
    "tests/fixtures/home_market_synthetic.csv": (
        "81bb0297127ea03179675134fc57b381c0cfbacb582530e7e5df32bc0069bed4"
    )
}
DATA_SUFFIXES = (
    ".csv",
    ".csv.gz",
    ".json",
    ".json.gz",
    ".jsonl",
    ".ndjson",
    ".parquet",
    ".duckdb",
    ".sqlite",
    ".db",
)
RAW_SIGNATURE_COLUMNS = frozenset(
    {
        "month_date_yyyymm",
        "county_fips",
        "median_listing_price_per_square_foot",
        "quality_flag",
    }
)
DERIVED_MARKET_COLUMNS = frozenset(
    {
        "home_sqft_for_1m",
        "home_buying_power_percentile",
        "home_median_listing_price",
        "home_median_listing_price_per_square_foot",
        "home_median_square_feet",
        "home_active_listing_count",
        "home_market_month",
        "home_costs_coverage_status",
        "sqft_for_1m_t12",
        "median_ppsf",
        "median_active_listings",
        "housing_valid_months",
    }
)
DENIED_DATA_NAME_TOKENS = (
    "rdc_inventory_core_metrics_county",
    "home-market",
    "home_market",
    "home-costs",
    "home_costs",
    "realtor",
    "nibrs_incident",
    "nibrs_person",
    "location_fabric",
    "fcc_location_fabric",
    "student_record",
    "cms_identifiable",
    "tax_identifiable",
)


@dataclass(frozen=True)
class LockSignatures:
    raw_sha256: frozenset[str]
    header_sha256: frozenset[str]
    filenames: frozenset[str]


def load_lock_signatures(path: Path = RELEASE_LOCK) -> LockSignatures:
    lock = json.loads(path.read_text())
    releases = lock["releases"]
    history = lock.get("history") if isinstance(lock.get("history"), dict) else {}
    return LockSignatures(
        raw_sha256=frozenset(
            [str(item["sha256"]) for item in releases]
            + ([str(history["sha256"])] if history.get("sha256") else [])
        ),
        header_sha256=frozenset(
            [str(item["header_sha256"]) for item in releases]
            + ([str(history["header_sha256"])] if history.get("header_sha256") else [])
        ),
        filenames=frozenset(
            [str(item["expected_filename"]).lower() for item in releases]
            + (
                [str(history["expected_filename"]).lower()]
                if history.get("expected_filename")
                else []
            )
        ),
    )


def _normalized_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("./")


def _canonical_archive_name(name: str) -> str:
    normalized = _normalized_name(name)
    parts = PurePosixPath(normalized).parts
    if len(parts) > 1 and parts[0].startswith("househunter-"):
        return PurePosixPath(*parts[1:]).as_posix()
    return normalized


def _is_allowed_lock(name: str) -> bool:
    return _canonical_archive_name(name) in ALLOWED_LOCK_PATHS


def _is_approved_synthetic(name: str, digest: str) -> bool:
    canonical = _canonical_archive_name(name)
    return SYNTHETIC_FIXTURE_SHA256.get(canonical) == digest


def _looks_like_data(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(DATA_SUFFIXES)


def _decoded_payload(name: str, payload: bytes) -> bytes:
    if name.lower().endswith(".gz"):
        try:
            return gzip.decompress(payload)
        except (EOFError, OSError):
            return payload
    return payload


def _is_bundled_asset(name: str) -> bool:
    normalized = f"/{_normalized_name(name).lower()}"
    return any(
        root in normalized
        for root in (
            "/web/dist/",
            "/househunter/static/",
            "/src/househunter/assets/",
            "/househunter/assets/",
        )
    )


def _nested_archive_entries(name: str, payload: bytes) -> Iterable[tuple[str, bytes]] | None:
    lowered = name.lower()
    if lowered.endswith((".zip", ".whl")):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return [
                (f"{name}!/{member.filename}", archive.read(member))
                for member in archive.infolist()
                if not member.is_dir()
            ]
    if lowered.endswith((".tar.gz", ".tgz")):
        nested: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    extracted = archive.extractfile(member)
                    assert extracted is not None
                    nested.append((f"{name}!/{member.name}", extracted.read()))
        return nested
    return None


def validate_entry(name: str, payload: bytes, signatures: LockSignatures) -> None:
    """Validate one repository or archive member against deterministic signatures."""
    normalized = _normalized_name(name)
    lowered = normalized.lower()
    basename = PurePosixPath(lowered).name
    digest = hashlib.sha256(payload).hexdigest()
    decoded = _decoded_payload(normalized, payload)
    decoded_digest = hashlib.sha256(decoded).hexdigest()
    if (
        basename in signatures.filenames
        or digest in signatures.raw_sha256
        or decoded_digest in signatures.raw_sha256
    ):
        raise ValueError(f"private home-market source file is not publishable: {normalized}")

    first_line = decoded.splitlines(keepends=True)[:1]
    if first_line and hashlib.sha256(first_line[0]).hexdigest() in signatures.header_sha256:
        raise ValueError(f"private home-market source header is not publishable: {normalized}")
    if _is_allowed_lock(normalized) or _is_approved_synthetic(normalized, digest):
        return

    nested_entries = _nested_archive_entries(normalized, payload)
    if nested_entries is not None:
        for nested_name, nested_payload in nested_entries:
            validate_entry(nested_name, nested_payload, signatures)
        return

    has_denied_name = any(token in lowered for token in DENIED_DATA_NAME_TOKENS)
    if has_denied_name and (_looks_like_data(normalized) or _is_bundled_asset(normalized)):
        raise ValueError(f"private home-market data filename is not publishable: {normalized}")
    if not _looks_like_data(normalized):
        return

    searchable = decoded.lower()
    raw_matches = sum(column.encode() in searchable for column in RAW_SIGNATURE_COLUMNS)
    derived_matches = sum(column.encode() in searchable for column in DERIVED_MARKET_COLUMNS)
    if raw_matches == len(RAW_SIGNATURE_COLUMNS):
        raise ValueError(f"private home-market source columns are not publishable: {normalized}")
    if derived_matches:
        raise ValueError(f"private home-market derived columns are not publishable: {normalized}")


def _git_names(root: Path, *arguments: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", *arguments],
        check=True,
        capture_output=True,
    )
    return [item.decode() for item in result.stdout.split(b"\0") if item]


def _index_payload(root: Path, name: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f":{name}"],
        check=True,
        capture_output=True,
    )
    return result.stdout


def validate_repository(root: Path, signatures: LockSignatures) -> int:
    checked = 0
    tracked = _git_names(root, "--cached")
    for name in tracked:
        index_payload = _index_payload(root, name)
        validate_entry(name, index_payload, signatures)
        checked += 1
        worktree_path = root / name
        if worktree_path.is_file():
            worktree_payload = worktree_path.read_bytes()
            if worktree_payload != index_payload:
                validate_entry(name, worktree_payload, signatures)
                checked += 1
    for name in _git_names(root, "--others", "--exclude-standard"):
        path = root / name
        if path.is_file():
            validate_entry(name, path.read_bytes(), signatures)
            checked += 1
    return checked


def _zip_entries(path: Path) -> Iterable[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if not member.is_dir():
                yield member.filename, archive.read(member)


def _tar_entries(path: Path) -> Iterable[tuple[str, bytes]]:
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            if member.isfile():
                extracted = archive.extractfile(member)
                assert extracted is not None
                yield member.name, extracted.read()


def validate_distribution(path: Path, signatures: LockSignatures) -> int:
    if path.suffix in {".whl", ".zip"}:
        entries = _zip_entries(path)
    elif path.name.endswith((".tar.gz", ".tgz")):
        entries = _tar_entries(path)
    else:
        raise ValueError(f"unsupported distribution archive: {path}")
    checked = 0
    for name, payload in entries:
        validate_entry(name, payload, signatures)
        checked += 1
    return checked


def distribution_paths(directory: Path) -> list[Path]:
    supported = (".whl", ".zip", ".tar.gz", ".tgz")
    return sorted(path for path in directory.iterdir() if path.name.endswith(supported))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", action="append", type=Path, default=[])
    parser.add_argument("--distribution-dir", action="append", type=Path, default=[])
    arguments = parser.parse_args()
    signatures = load_lock_signatures()
    repository_count = validate_repository(REPO_ROOT, signatures)
    distributions = list(arguments.distribution)
    for directory in arguments.distribution_dir:
        distributions.extend(distribution_paths(directory))
    distribution_counts = {
        str(path): validate_distribution(path, signatures) for path in distributions
    }
    print(
        json.dumps(
            {
                "repository_files_checked": repository_count,
                "distribution_members_checked": distribution_counts,
                "status": "PASS",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
