from pathlib import Path

import polars as pl

from househunter.reference_generation import reconcile_connecticut, remove_obsolete_assets


def test_reference_generation_removes_obsolete_assets(tmp_path: Path) -> None:
    obsolete = tmp_path / "obsolete.parquet"
    unrelated = tmp_path / "unrelated.parquet"
    obsolete.write_bytes(b"stale")
    unrelated.write_bytes(b"keep")
    (tmp_path / "reference_metadata.json").write_text(
        '{"row_counts":{"places_2020":1,"place_tract_weights_2020":1,"obsolete":1}}'
    )
    remove_obsolete_assets(tmp_path)
    assert not obsolete.exists()
    assert unrelated.read_bytes() == b"keep"


def test_connecticut_reconciliation_keeps_positive_housing_and_audits_water_split(
    tmp_path: Path,
) -> None:
    fema = tmp_path / "fema.parquet"
    pl.DataFrame(
        {
            "tract_id": ["09110000100"],
            "alr_npctl": [25.0],
            "nri_version": ["December 2025"],
        }
    ).write_parquet(fema)
    result, audit = reconcile_connecticut(
        {
            ("0900001", "09001000100"): 5,
            ("0900001", "09001990000"): 0,
        },
        {"09001000100": 5, "09001990000": 0},
        fema,
    )
    assert result == {("0900001", "09110000100"): 5}
    assert audit["splits"]["09001990000"]["housing_units"] == 0
    assert audit["splits"]["09001990000"]["fema_rows"] == []
