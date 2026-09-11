from pathlib import Path

from househunter.config import canonical_json, load_config, sha256_bytes
from househunter.hazards import (
    FEMA_HAZARD_FIELDS,
    HAZARDS,
    hazard_percentiles_from_record,
    optional_percentile,
    tract_out_fields,
)


def test_catalog_covers_eighteen_published_alr_percentiles() -> None:
    assert len(HAZARDS) == 18
    assert len(set(hazard.code for hazard in HAZARDS)) == 18
    assert HAZARDS[-2].column == "alr_npctl_wfir"
    assert HAZARDS[-2].fema_field == "WFIR_ALR_NPCTL"
    assert FEMA_HAZARD_FIELDS[HAZARDS[-2].fema_field] == "esriFieldTypeDouble"
    assert tract_out_fields().startswith("TRACTFIPS,ALR_NPCTL,NRI_VER,AVLN_ALR_NPCTL")
    assert tract_out_fields().endswith("WNTW_ALR_NPCTL")


def test_null_hazard_percentile_is_not_zero() -> None:
    assert optional_percentile(None) is None
    assert optional_percentile(float("nan")) is None
    assert optional_percentile(12.5) == 12.5
    hazards = hazard_percentiles_from_record({"alr_npctl_wfir": None, "alr_npctl_tsun": 0.0})
    by_code = {item["code"]: item["percentile"] for item in hazards}
    assert by_code["WFIR"] is None
    assert by_code["TSUN"] == 0.0


def test_pinned_source_contracts_include_eighteen_hazards() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config" / "sources.yml")
    for key in ("fema", "fema_counties"):
        fields = config[key]["fields"]
        for fema_field in FEMA_HAZARD_FIELDS:
            assert fields[fema_field] == "esriFieldTypeDouble"
        assert sha256_bytes(canonical_json(fields)) == config[key]["schema_fingerprint"]
