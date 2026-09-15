from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

import househunter.cli as cli_module
import househunter.home_market as home_market_module
from househunter.cli import app
from househunter.config import RuntimePaths, sha256_file
from househunter.errors import HouseHunterError, SourceContractError
from househunter.home_market import (
    MAX_IMPORT_BYTES,
    import_home_market,
    is_stale,
    load_current_release,
    load_release_lock,
    logical_checksum,
    normalize,
    source_status,
    stale_after,
)

FIELDS = [
    "month_date_yyyymm",
    "county_fips",
    "county_name",
    "median_listing_price",
    "active_listing_count",
    "median_listing_price_per_square_foot",
    "median_square_feet",
    "total_listing_count",
    "quality_flag",
]


def _rows(month: int = 202608) -> list[dict[str, object]]:
    return [
        dict(
            zip(
                FIELDS,
                [month, "01001", "Source Autauga", 300000, 10, 100, 1800, 12, 0],
                strict=True,
            )
        ),
        dict(
            zip(
                FIELDS,
                [month, "01003", "Source Baldwin", 400000, 20, 200, 1900, 22, 0],
                strict=True,
            )
        ),
        dict(
            zip(
                FIELDS,
                [month, "01005", "Source Barbour", 250000, 8, 200, 1600, 9, 0],
                strict=True,
            )
        ),
        dict(
            zip(
                FIELDS,
                [month, "01007", "Source Bibb", 200000, 4, 50, 1400, 5, 1],
                strict=True,
            )
        ),
        dict(
            zip(
                FIELDS,
                [month, "01009", "Source Blount", 350000, 7, 0, 1700, 8, 0],
                strict=True,
            )
        ),
        dict(
            zip(
                FIELDS,
                [month, "01011", "Source Bullock", 180000, 2, "", 1200, 3, 0],
                strict=True,
            )
        ),
    ]


def _csv(rows: list[dict[str, object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _entry(content: bytes, filename: str, month: str, month_code: int) -> dict[str, object]:
    frame = pl.read_csv(io.BytesIO(content), schema_overrides={"county_fips": pl.String})
    entry: dict[str, object] = {
        "month": month,
        "month_date_yyyymm": month_code,
        "expected_filename": filename,
        "byte_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "header_sha256": hashlib.sha256(content.splitlines(keepends=True)[0]).hexdigest(),
        "row_count": frame.height,
        "quality_flagged_row_count": frame.filter(pl.col("quality_flag") != 0).height,
        "null_price_per_square_foot_count": frame[
            "median_listing_price_per_square_foot"
        ].null_count(),
        "source_page": "https://example.test/research",
        "reviewed_on": "2026-09-14",
    }
    lock = _lock_payload([entry])
    try:
        normalized_digest = logical_checksum(normalize(content, lock, entry))
    except SourceContractError:
        normalized_digest = "0" * 64
    entry["normalized_logical_sha256"] = normalized_digest
    return entry


def _lock_payload(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": 1,
        "source_name": "Synthetic home market",
        "source_page": "https://example.test/research",
        "terms_url": "https://example.test/terms",
        "automatic_download": False,
        "private_use_only": True,
        "usage_notice": "Personal local use only.",
        "required_fields": FIELDS,
        "releases": entries,
    }


def _write_lock(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    lock = _lock_payload(entries)
    path = tmp_path / "release-lock.json"
    path.write_text(json.dumps(lock))
    return path


def _fixture_release(
    tmp_path: Path,
    *,
    rows: list[dict[str, object]] | None = None,
    filename: str = "approved.csv",
    month: str = "2026-08",
    month_code: int = 202608,
) -> tuple[Path, Path, bytes, dict[str, object]]:
    content = _csv(rows or _rows(month_code))
    source = tmp_path / filename
    source.write_bytes(content)
    entry = _entry(content, filename, month, month_code)
    lock = _write_lock(tmp_path, [entry])
    return source, lock, content, entry


def test_import_requires_explicit_personal_use_acknowledgement(tmp_path: Path) -> None:
    source, lock, _, _ = _fixture_release(tmp_path)
    with pytest.raises(HouseHunterError, match="acknowledge-personal-use"):
        import_home_market(
            RuntimePaths.from_root(tmp_path),
            source,
            acknowledge_personal_use=False,
            release_lock_path=lock,
        )


def test_cli_import_home_market_requires_ack_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, lock, _, _ = _fixture_release(tmp_path)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    monkeypatch.setattr(cli_module, "_paths", lambda: paths)
    monkeypatch.setattr(home_market_module, "default_release_lock_path", lambda: lock)
    runner = CliRunner()

    rejected = runner.invoke(app, ["import-home-market", str(source)])
    assert rejected.exit_code == 1
    assert "--acknowledge-personal-use" in rejected.output

    imported = runner.invoke(
        app,
        ["import-home-market", str(source), "--acknowledge-personal-use"],
    )
    assert imported.exit_code == 0, imported.output
    assert "Home-market release imported" in imported.output
    assert load_current_release(paths, release_lock_path=lock) is not None


def test_import_normalizes_quality_and_national_tied_percentiles(tmp_path: Path) -> None:
    source, lock, content, _ = _fixture_release(tmp_path)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    directory = import_home_market(
        paths,
        source,
        acknowledge_personal_use=True,
        release_lock_path=lock,
    )
    current = load_current_release(paths, release_lock_path=lock, today=date(2026, 10, 1))
    assert current is not None
    assert current.directory == directory
    frame = current.frame
    assert frame["county_fips"].to_list() == sorted(frame["county_fips"].to_list())
    assert frame.filter(pl.col("county_fips") == "01001").row(0, named=True)[
        "home_sqft_for_1m"
    ] == 10000
    ties = frame.filter(pl.col("county_fips").is_in(["01003", "01005"]))
    assert ties["home_buying_power_percentile"].to_list() == pytest.approx(
        [200 / 3, 200 / 3]
    )
    state_scoped = frame.filter(pl.col("county_fips") == "01001")
    assert state_scoped["home_buying_power_percentile"].item() == 100.0
    rejected = frame.filter(pl.col("county_fips").is_in(["01007", "01009", "01011"]))
    assert rejected["home_sqft_for_1m"].null_count() == 3
    assert rejected["home_buying_power_percentile"].null_count() == 3
    assert rejected["home_costs_coverage_status"].to_list() == [
        "source_quality_flag",
        "invalid_price_per_square_foot",
        "invalid_price_per_square_foot",
    ]
    assert not any(path.read_bytes() == content for path in directory.iterdir() if path.is_file())
    status = source_status(paths, release_lock_path=lock, today=date(2026, 10, 1))
    assert status["cached"] is True
    assert status["stale"] is False
    assert status["usage_notice"] == "Personal local use only."
    private_paths = [
        paths.data / "home-market",
        paths.data / "home-market" / "releases",
        directory,
        *directory.iterdir(),
        paths.data / "home-market" / "current.json",
    ]
    assert all(path.stat().st_mode & 0o077 == 0 for path in private_paths)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: [*rows, {**rows[0], "county_name": "Duplicate"}], "not unique"),
        (
            lambda rows: [
                rows[0],
                {**rows[1], "month_date_yyyymm": 202607},
                *rows[2:],
            ],
            "approved release month",
        ),
        (lambda rows: [{**rows[0], "quality_flag": 2}, *rows[1:]], "must be 0 or 1"),
        (lambda rows: [{**rows[0], "county_fips": ""}, *rows[1:]], "invalid county FIPS"),
        (
            lambda rows: [{**rows[0], "county_fips": "99999"}, *rows[1:]],
            "unknown or out-of-scope county FIPS",
        ),
        (
            lambda rows: [{**rows[0], "median_listing_price": float("inf")}, *rows[1:]],
            "nonfinite",
        ),
    ],
)
def test_import_rejects_malformed_rows(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    rows = mutate(_rows())  # type: ignore[operator]
    source, lock, _, _ = _fixture_release(tmp_path, rows=rows)
    with pytest.raises(SourceContractError, match=message):
        import_home_market(
            RuntimePaths.from_root(tmp_path / "runtime"),
            source,
            acknowledge_personal_use=True,
            release_lock_path=lock,
        )


@pytest.mark.parametrize("county_fips", ["01999", "72001"])
def test_import_rejects_valid_looking_nonexistent_or_out_of_scope_county_before_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    county_fips: str,
) -> None:
    rows = [{**_rows()[0], "county_fips": county_fips}, *_rows()[1:]]

    def ranking_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("national percentile calibration ran before county validation")

    monkeypatch.setattr(pl.Expr, "rank", ranking_must_not_run)
    source, lock, _, _ = _fixture_release(tmp_path, rows=rows)
    paths = RuntimePaths.from_root(tmp_path / "runtime")

    with pytest.raises(
        SourceContractError,
        match="unknown or out-of-scope county FIPS",
    ):
        import_home_market(
            paths,
            source,
            acknowledge_personal_use=True,
            release_lock_path=lock,
        )

    assert not (paths.data / "home-market" / "current.json").exists()


def test_import_rejects_filename_size_and_checksum_mismatch(tmp_path: Path) -> None:
    source, lock, content, entry = _fixture_release(tmp_path)
    wrong_name = source.with_name("wrong.csv")
    wrong_name.write_bytes(content)
    with pytest.raises(SourceContractError, match="filename and size"):
        import_home_market(
            RuntimePaths.from_root(tmp_path / "one"),
            wrong_name,
            acknowledge_personal_use=True,
            release_lock_path=lock,
        )

    source.write_bytes(content + b"x")
    with pytest.raises(SourceContractError, match="filename and size"):
        import_home_market(
            RuntimePaths.from_root(tmp_path / "two"),
            source,
            acknowledge_personal_use=True,
            release_lock_path=lock,
        )

    source.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    altered_entry = {**entry, "byte_size": len(content)}
    altered_lock = _write_lock(tmp_path, [altered_entry])
    with pytest.raises(SourceContractError, match="checksum"):
        import_home_market(
            RuntimePaths.from_root(tmp_path / "three"),
            source,
            acknowledge_personal_use=True,
            release_lock_path=altered_lock,
        )


def test_release_lock_rejects_oversized_approval(tmp_path: Path) -> None:
    _, _, content, entry = _fixture_release(tmp_path)
    lock = _write_lock(tmp_path, [{**entry, "byte_size": MAX_IMPORT_BYTES + 1}])
    with pytest.raises(SourceContractError, match="invalid metadata"):
        load_release_lock(lock)
    assert len(content) < MAX_IMPORT_BYTES


def test_cancelled_import_does_not_replace_current_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_content = _csv(_rows(202607))
    second_content = _csv(_rows(202608))
    first = tmp_path / "july.csv"
    second = tmp_path / "august.csv"
    first.write_bytes(first_content)
    second.write_bytes(second_content)
    entries = [
        _entry(first_content, first.name, "2026-07", 202607),
        _entry(second_content, second.name, "2026-08", 202608),
    ]
    lock = _write_lock(tmp_path, entries)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    import_home_market(
        paths, first, acknowledge_personal_use=True, release_lock_path=lock
    )
    pointer = paths.data / "home-market" / "current.json"
    previous = pointer.read_bytes()
    real_write_release = home_market_module._write_release
    published = False

    def publish_then_cancel(*args: object, **kwargs: object) -> Path:
        nonlocal published
        output = real_write_release(*args, **kwargs)
        published = True
        return output

    monkeypatch.setattr(home_market_module, "_write_release", publish_then_cancel)
    with pytest.raises(InterruptedError, match="cancelled"):
        import_home_market(
            paths,
            second,
            acknowledge_personal_use=True,
            release_lock_path=lock,
            cancelled=lambda: published,
        )
    assert pointer.read_bytes() == previous


def test_failed_pointer_swap_preserves_previous_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_content = _csv(_rows(202607))
    second_content = _csv(_rows(202608))
    first = tmp_path / "july.csv"
    second = tmp_path / "august.csv"
    first.write_bytes(first_content)
    second.write_bytes(second_content)
    entries = [
        _entry(first_content, first.name, "2026-07", 202607),
        _entry(second_content, second.name, "2026-08", 202608),
    ]
    lock = _write_lock(tmp_path, entries)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    import_home_market(
        paths, first, acknowledge_personal_use=True, release_lock_path=lock
    )
    pointer = paths.data / "home-market" / "current.json"
    previous = pointer.read_bytes()
    real_atomic_write = home_market_module.atomic_write_json

    def fail_pointer(path: Path, payload: object) -> None:
        if path.name == "current.json":
            raise OSError("disk full")
        real_atomic_write(path, payload)

    monkeypatch.setattr(home_market_module, "atomic_write_json", fail_pointer)
    with pytest.raises(OSError, match="disk full"):
        import_home_market(
            paths, second, acknowledge_personal_use=True, release_lock_path=lock
        )
    assert pointer.read_bytes() == previous
    current = load_current_release(paths, release_lock_path=lock)
    assert current is not None
    assert current.manifest["month"] == "2026-07"


def test_release_validation_rejects_flagged_row_with_forged_ranking_values(
    tmp_path: Path,
) -> None:
    source, lock, _, _ = _fixture_release(tmp_path)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    directory = import_home_market(
        paths, source, acknowledge_personal_use=True, release_lock_path=lock
    )
    table_path = directory / "home_market.parquet"
    frame = pl.read_parquet(table_path).with_columns(
        pl.when(pl.col("county_fips") == "01007")
        .then(pl.lit("complete"))
        .otherwise(pl.col("home_costs_coverage_status"))
        .alias("home_costs_coverage_status"),
        pl.when(pl.col("county_fips") == "01007")
        .then(20_000.0)
        .otherwise(pl.col("home_sqft_for_1m_unrounded"))
        .alias("home_sqft_for_1m_unrounded"),
        pl.when(pl.col("county_fips") == "01007")
        .then(20_000)
        .otherwise(pl.col("home_sqft_for_1m"))
        .alias("home_sqft_for_1m"),
        pl.when(pl.col("county_fips") == "01007")
        .then(100.0)
        .otherwise(pl.col("home_buying_power_percentile"))
        .alias("home_buying_power_percentile"),
    )
    frame.write_parquet(table_path, compression="zstd", statistics=True)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["eligible_row_count"] = 4
    manifest["logical_sha256"] = logical_checksum(frame)
    manifest["normalized_sha256"] = sha256_file(table_path)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(SourceContractError, match="normalized checksum"):
        home_market_module.validate_release_directory(directory, load_release_lock(lock))


def test_existing_release_fast_path_rejects_self_consistent_value_forgery(
    tmp_path: Path,
) -> None:
    source, lock, _, _ = _fixture_release(tmp_path)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    directory = import_home_market(
        paths, source, acknowledge_personal_use=True, release_lock_path=lock
    )
    table_path = directory / "home_market.parquet"
    frame = pl.read_parquet(table_path).with_columns(
        pl.when(pl.col("county_fips") == "01001")
        .then(pl.col("home_median_listing_price") + 1)
        .otherwise(pl.col("home_median_listing_price"))
        .alias("home_median_listing_price")
    )
    frame.write_parquet(table_path, compression="zstd", statistics=True)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["logical_sha256"] = logical_checksum(frame)
    manifest["normalized_sha256"] = sha256_file(table_path)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(SourceContractError, match="normalized checksum"):
        import_home_market(
            paths, source, acknowledge_personal_use=True, release_lock_path=lock
        )


@pytest.mark.parametrize("county_fips", ["01999", "72001"])
def test_release_revalidation_rejects_valid_looking_nonexistent_or_out_of_scope_county(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    county_fips: str,
) -> None:
    source, lock, _, _ = _fixture_release(tmp_path)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    directory = import_home_market(
        paths, source, acknowledge_personal_use=True, release_lock_path=lock
    )
    table_path = directory / "home_market.parquet"
    frame = pl.read_parquet(table_path).with_columns(
        pl.when(pl.col("county_fips") == "01001")
        .then(pl.lit(county_fips))
        .otherwise(pl.col("county_fips"))
        .alias("county_fips")
    )
    frame.write_parquet(table_path, compression="zstd", statistics=True)

    reviewed = load_release_lock(lock)
    reviewed_release = reviewed["releases"][0]
    reviewed_release["normalized_logical_sha256"] = logical_checksum(frame)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["logical_sha256"] = logical_checksum(frame)
    manifest["normalized_sha256"] = sha256_file(table_path)
    manifest["release_contract_sha256"] = home_market_module._release_contract_sha(
        reviewed, reviewed_release
    )
    manifest_path.write_text(json.dumps(manifest))

    def ranking_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("national percentile calibration ran before county validation")

    monkeypatch.setattr(pl.Expr, "rank", ranking_must_not_run)

    with pytest.raises(
        SourceContractError,
        match="normalized row count or uniqueness is invalid",
    ):
        home_market_module.validate_release_directory(directory, reviewed)


def test_release_validation_enforces_locked_quality_flag_count(tmp_path: Path) -> None:
    source, lock, _, _ = _fixture_release(tmp_path)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    directory = import_home_market(
        paths, source, acknowledge_personal_use=True, release_lock_path=lock
    )
    table_path = directory / "home_market.parquet"
    target = pl.col("county_fips") == "01001"
    frame = (
        pl.read_parquet(table_path)
        .with_columns(
            pl.when(target)
            .then(pl.lit(1, dtype=pl.Int8))
            .otherwise(pl.col("source_quality_flag"))
            .alias("source_quality_flag"),
            pl.when(target)
            .then(pl.lit("source_quality_flag"))
            .otherwise(pl.col("home_costs_coverage_status"))
            .alias("home_costs_coverage_status"),
            pl.when(target)
            .then(pl.lit(None, dtype=pl.Float64))
            .otherwise(pl.col("home_sqft_for_1m_unrounded"))
            .alias("home_sqft_for_1m_unrounded"),
            pl.when(target)
            .then(pl.lit(None, dtype=pl.Int64))
            .otherwise(pl.col("home_sqft_for_1m"))
            .alias("home_sqft_for_1m"),
        )
        .with_columns(
            pl.when(pl.col("home_sqft_for_1m_unrounded").is_not_null())
            .then(100.0)
            .otherwise(None)
            .alias("home_buying_power_percentile")
        )
    )
    frame.write_parquet(table_path, compression="zstd", statistics=True)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["quality_flagged_row_count"] = 2
    manifest["eligible_row_count"] = 2
    manifest["logical_sha256"] = logical_checksum(frame)
    manifest["normalized_sha256"] = sha256_file(table_path)
    reviewed = load_release_lock(lock)
    reviewed_release = reviewed["releases"][0]
    reviewed_release["normalized_logical_sha256"] = logical_checksum(frame)
    manifest["release_contract_sha256"] = home_market_module._release_contract_sha(
        reviewed, reviewed_release
    )
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(SourceContractError, match="quality-flag count"):
        home_market_module.validate_release_directory(directory, reviewed)


def test_release_validation_rejects_forged_rounded_buying_power(tmp_path: Path) -> None:
    source, lock, _, _ = _fixture_release(tmp_path)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    directory = import_home_market(
        paths, source, acknowledge_personal_use=True, release_lock_path=lock
    )
    table_path = directory / "home_market.parquet"
    frame = pl.read_parquet(table_path).with_columns(
        pl.when(pl.col("county_fips") == "01001")
        .then(99_999)
        .otherwise(pl.col("home_sqft_for_1m"))
        .alias("home_sqft_for_1m")
    )
    frame.write_parquet(table_path, compression="zstd", statistics=True)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["logical_sha256"] = logical_checksum(frame)
    manifest["normalized_sha256"] = sha256_file(table_path)
    reviewed = load_release_lock(lock)
    reviewed_release = reviewed["releases"][0]
    reviewed_release["normalized_logical_sha256"] = logical_checksum(frame)
    manifest["release_contract_sha256"] = home_market_module._release_contract_sha(
        reviewed, reviewed_release
    )
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(SourceContractError, match="derived values"):
        home_market_module.validate_release_directory(directory, reviewed)


def test_older_import_never_demotes_newer_current_release(tmp_path: Path) -> None:
    first_content = _csv(_rows(202607))
    second_content = _csv(_rows(202608))
    first = tmp_path / "july.csv"
    second = tmp_path / "august.csv"
    first.write_bytes(first_content)
    second.write_bytes(second_content)
    entries = [
        _entry(first_content, first.name, "2026-07", 202607),
        _entry(second_content, second.name, "2026-08", 202608),
    ]
    lock = _write_lock(tmp_path, entries)
    paths = RuntimePaths.from_root(tmp_path / "runtime")
    import_home_market(paths, second, acknowledge_personal_use=True, release_lock_path=lock)
    import_home_market(paths, first, acknowledge_personal_use=True, release_lock_path=lock)
    current = load_current_release(paths, release_lock_path=lock)
    assert current is not None
    assert current.manifest["month"] == "2026-08"


def test_stale_boundary_is_month_end_plus_62_days() -> None:
    assert stale_after("2026-08") == date(2026, 11, 1)
    assert is_stale("2026-08", today=date(2026, 11, 1)) is False
    assert is_stale("2026-08", today=date(2026, 11, 2)) is True
