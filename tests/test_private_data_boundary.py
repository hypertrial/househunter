from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "househunter_private_data_boundary",
    Path(__file__).parents[1] / "scripts" / "check_private_data_boundary.py",
)
assert spec and spec.loader
boundary = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = boundary
spec.loader.exec_module(boundary)


def signatures() -> boundary.LockSignatures:
    header = b"month_date_yyyymm,county_fips,quality_flag\n"
    return boundary.LockSignatures(
        raw_sha256=frozenset({hashlib.sha256(b"approved raw").hexdigest()}),
        header_sha256=frozenset({hashlib.sha256(header).hexdigest()}),
        filenames=frozenset({"rdc_inventory_core_metrics_county.csv"}),
    )


def test_boundary_allows_policy_lock_and_synthetic_fixtures() -> None:
    locked = json.dumps(
        {
            "required_fields": sorted(boundary.RAW_SIGNATURE_COLUMNS),
            "sha256": hashlib.sha256(b"approved raw").hexdigest(),
        }
    ).encode()
    boundary.validate_entry("config/home-market/release-lock.json", locked, signatures())
    fixture = Path(__file__).parent / "fixtures/home_market_synthetic.csv"
    boundary.validate_entry(
        fixture.relative_to(Path(__file__).parents[1]).as_posix(),
        fixture.read_bytes(),
        signatures(),
    )


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    [
        (
            "RDC_Inventory_Core_Metrics_County.csv",
            b"not even the approved contents",
            "source file",
        ),
        ("release.csv", b"approved raw", "source file"),
        ("release.csv.gz", gzip.compress(b"approved raw"), "source file"),
        (
            "inventory.csv",
            b"month_date_yyyymm,county_fips,quality_flag\n",
            "source header",
        ),
        (
            "inventory.csv",
            (
                b"month_date_yyyymm,county_fips,"
                b"median_listing_price_per_square_foot,quality_flag\n"
            ),
            "source columns",
        ),
        (
            "snapshot.json",
            b'{"home_sqft_for_1m": 5000}',
            "derived columns",
        ),
        ("assets/home-costs.json", b"{}", "data filename"),
        ("web/dist/home-costs.js", b"export default {}", "data filename"),
        ("src/househunter/assets/realtor.bin", b"opaque", "data filename"),
        ("househunter/assets/realtor.bin", b"opaque", "data filename"),
        (
            "tests/fixtures/unapproved.csv",
            (
                b"month_date_yyyymm,county_fips,"
                b"median_listing_price_per_square_foot,quality_flag\n"
            ),
            "source columns",
        ),
    ],
)
def test_boundary_rejects_private_source_and_derivative_signatures(
    name: str, payload: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        boundary.validate_entry(name, payload, signatures())


def test_distribution_scanner_checks_archive_members(tmp_path: Path) -> None:
    wheel = tmp_path / "househunter-3.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "househunter/private_snapshot.json",
            '{"home_buying_power_percentile": 80, "home_market_month": "2026-08"}',
        )

    with pytest.raises(ValueError, match="derived columns"):
        boundary.validate_distribution(wheel, signatures())


def test_repository_scanner_checks_staged_index_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        boundary,
        "_git_names",
        lambda _root, *arguments: ["renamed.bin"] if arguments == ("--cached",) else [],
    )
    monkeypatch.setattr(boundary, "_index_payload", lambda _root, _name: b"approved raw")

    with pytest.raises(ValueError, match="source file"):
        boundary.validate_repository(tmp_path, signatures())


def test_checked_in_repository_passes_boundary() -> None:
    signatures_from_lock = boundary.load_lock_signatures()
    assert boundary.validate_repository(Path(__file__).parents[1], signatures_from_lock) > 0
