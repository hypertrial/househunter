from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl
import pytest
from test_home_market import _csv, _entry, _lock_payload, _rows

import househunter.home_market as home_market
from househunter.config import RuntimePaths
from househunter.errors import HouseHunterError, SourceContractError
from househunter.home_market import (
    import_home_market,
    import_home_market_history,
    trailing_twelve_month_identity,
    trailing_twelve_month_metrics,
)


def _month_frame(county: str, ppsf: float, listings: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "county_fips": [county],
            "source_county_name": ["Test"],
            "home_median_listing_price": [400000.0],
            "home_median_listing_price_per_square_foot": [ppsf],
            "home_median_square_feet": [1500.0],
            "home_active_listing_count": [listings],
            "home_total_listing_count": [listings],
            "home_market_month": ["2025-01"],
            "source_quality_flag": [0],
            "home_sqft_for_1m_unrounded": [1_000_000.0 / ppsf],
            "home_sqft_for_1m": [int(round(1_000_000.0 / ppsf))],
            "home_buying_power_percentile": [50.0],
            "home_costs_coverage_status": ["complete"],
        }
    )


def test_trailing_twelve_month_median_and_month_counts(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    imported = []
    for index in range(12):
        month = f"2025-{index + 1:02d}"
        frame = _month_frame("01001", ppsf=100.0 + index, listings=80.0 + index)
        imported.append((month, frame, {"month": month}))
    monkeypatch.setattr(home_market, "list_imported_releases", lambda paths, **kwargs: imported)
    metrics = trailing_twelve_month_metrics(paths)
    assert metrics.height == 1
    assert metrics["housing_valid_months"][0] == 12
    assert metrics["median_ppsf"][0] == pytest.approx(105.5)
    assert metrics["median_active_listings"][0] == pytest.approx(85.5)
    assert metrics["sqft_for_1m_t12"][0] == pytest.approx(1_000_000.0 / 105.5)


def test_trailing_window_identity_includes_month_digests(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    imported = [
        (
            "2025-01",
            _month_frame("01001", 100.0, 80.0),
            {
                "month": "2025-01",
                "source_sha256": "aa" * 32,
                "logical_sha256": "bb" * 32,
            },
        ),
        (
            "2025-02",
            _month_frame("01001", 110.0, 90.0),
            {
                "month": "2025-02",
                "source_sha256": "cc" * 32,
                "logical_sha256": "dd" * 32,
            },
        ),
    ]
    monkeypatch.setattr(home_market, "list_imported_releases", lambda paths, **kwargs: imported)
    first = trailing_twelve_month_identity(paths)
    assert first["as_of_month"] == "2025-02"
    assert first["window"] == 12
    assert [item["month"] for item in first["months"]] == ["2025-01", "2025-02"]
    mutated = list(imported)
    mutated[0] = (
        imported[0][0],
        imported[0][1],
        {**imported[0][2], "logical_sha256": "ee" * 32},
    )
    monkeypatch.setattr(home_market, "list_imported_releases", lambda paths, **kwargs: mutated)
    assert trailing_twelve_month_identity(paths) != first


def test_eight_valid_months_are_counted_not_imputed(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    imported = []
    for index in range(8):
        month = f"2025-{index + 1:02d}"
        imported.append((month, _month_frame("01001", 200.0, 50.0), {"month": month}))
    monkeypatch.setattr(home_market, "list_imported_releases", lambda paths, **kwargs: imported)
    metrics = trailing_twelve_month_metrics(paths)
    assert metrics["housing_valid_months"][0] == 8


def test_quality_flagged_months_are_not_valid(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    good = _month_frame("01001", 200.0, 120.0)
    bad = _month_frame("01001", 200.0, 120.0).with_columns(
        pl.lit(1).cast(pl.Int8).alias("source_quality_flag")
    )
    imported = [
        ("2025-01", good, {"month": "2025-01"}),
        ("2025-02", bad, {"month": "2025-02"}),
    ]
    monkeypatch.setattr(home_market, "list_imported_releases", lambda paths, **kwargs: imported)
    metrics = trailing_twelve_month_metrics(paths)
    assert metrics["housing_valid_months"][0] == 1


def test_gapped_months_use_a_calendar_trailing_window(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    imported = []
    for index in range(6):
        month = f"2024-{index + 1:02d}"
        imported.append((month, _month_frame("01001", 100.0, 80.0), {"month": month}))
    for index in range(6):
        month = f"2025-{index + 1:02d}"
        imported.append((month, _month_frame("01001", 200.0, 120.0), {"month": month}))
    monkeypatch.setattr(home_market, "list_imported_releases", lambda paths, **kwargs: imported)
    metrics = trailing_twelve_month_metrics(paths)
    assert metrics["housing_valid_months"][0] == 6
    assert metrics["median_ppsf"][0] == pytest.approx(200.0)


def test_duplicate_month_dirs_with_conflicting_content_are_rejected(
    fixture_environment: tuple[RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    payloads = iter(
        [
            (
                _month_frame("01001", 100.0, 80.0),
                {"month": "2025-01", "logical_sha256": "aa" * 32},
            ),
            (
                _month_frame("01001", 110.0, 90.0),
                {"month": "2025-01", "logical_sha256": "bb" * 32},
            ),
        ]
    )
    monkeypatch.setattr(
        home_market,
        "validate_release_directory",
        lambda _directory, _lock: next(payloads),
    )
    monkeypatch.setattr(home_market, "load_release_lock", lambda path=None: {"releases": []})
    monkeypatch.setattr(home_market, "_require_real_directory", lambda path, label: None)
    releases = paths.data / "home-market" / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    (releases / "one").mkdir()
    (releases / "two").mkdir()
    with pytest.raises(HouseHunterError, match="Duplicate home-market month"):
        home_market.list_imported_releases(paths)


def test_history_import_requires_acknowledgement_and_lock(
    fixture_environment: tuple[RuntimePaths, Path],
    tmp_path: Path,
) -> None:
    paths, _ = fixture_environment
    source = tmp_path / "history.csv"
    source.write_text("month_date_yyyymm,county_fips\n")
    with pytest.raises(HouseHunterError, match="acknowledge-personal-use"):
        import_home_market_history(paths, source, acknowledge_personal_use=False)
    with pytest.raises(SourceContractError, match="no approved history file"):
        import_home_market_history(paths, source, acknowledge_personal_use=True)


def test_cancelled_history_import_preserves_pointer(
    fixture_environment: tuple[RuntimePaths, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _ = fixture_environment
    source = tmp_path / "history.csv"
    source.write_text("month_date_yyyymm,county_fips\n")
    monkeypatch.setattr(
        home_market,
        "load_release_lock",
        lambda path=None: {
            "schema": 1,
            "automatic_download": False,
            "private_use_only": True,
            "source_name": "fixture",
            "source_page": "https://example.test",
            "terms_url": "https://example.test/terms",
            "usage_notice": "personal",
            "required_fields": ["month_date_yyyymm", "county_fips"],
            "releases": [],
            "history": {
                "expected_filename": "history.csv",
                "byte_size": source.stat().st_size,
                "sha256": "00" * 32,
                "header_sha256": "11" * 32,
                "months": ["2025-01"],
            },
        },
    )
    cancelled = {"count": 0}

    def _cancel() -> bool:
        cancelled["count"] += 1
        return True

    with pytest.raises((HouseHunterError, SourceContractError, InterruptedError)):
        import_home_market_history(
            paths,
            source,
            acknowledge_personal_use=True,
            cancelled=_cancel,
        )
    assert not (paths.data / "home-market" / "current.json").exists()


def test_cancelled_history_import_preserves_prior_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    july_rows = _rows(202607)
    august_rows = _rows(202608)
    july_content = _csv(july_rows)
    august_content = _csv(august_rows)
    history_content = _csv(july_rows + august_rows)
    july_file = tmp_path / "july.csv"
    history_file = tmp_path / "history.csv"
    july_file.write_bytes(july_content)
    history_file.write_bytes(history_content)
    july_entry = _entry(july_content, july_file.name, "2026-07", 202607)
    august_entry = _entry(august_content, "august.csv", "2026-08", 202608)
    lock = _lock_payload([july_entry, august_entry])
    lock["history"] = {
        "expected_filename": history_file.name,
        "byte_size": len(history_content),
        "sha256": hashlib.sha256(history_content).hexdigest(),
        "header_sha256": hashlib.sha256(
            history_content.splitlines(keepends=True)[0]
        ).hexdigest(),
        "months": ["2026-07", "2026-08"],
    }
    lock_path = tmp_path / "release-lock.json"
    lock_path.write_text(json.dumps(lock))
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    import_home_market(
        paths,
        july_file,
        acknowledge_personal_use=True,
        release_lock_path=lock_path,
    )
    pointer = paths.data / "home-market" / "current.json"
    previous = pointer.read_bytes()
    published = False
    real_write_release = home_market._write_release

    def publish_then_cancel(*args: object, **kwargs: object) -> Path:
        nonlocal published
        output = real_write_release(*args, **kwargs)
        published = True
        return output

    monkeypatch.setattr(home_market, "_write_release", publish_then_cancel)
    with pytest.raises(InterruptedError, match="cancelled"):
        import_home_market_history(
            paths,
            history_file,
            acknowledge_personal_use=True,
            release_lock_path=lock_path,
            cancelled=lambda: published,
        )
    assert pointer.read_bytes() == previous
