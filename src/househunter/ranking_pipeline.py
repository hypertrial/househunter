from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import tarfile
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import duckdb
import polars as pl

from .build import snapshot_artifacts_are_valid
from .config import canonical_json, sha256_bytes, sha256_file
from .errors import HouseHunterError
from .geography import STATE_BY_FIPS
from .ranking_reference import (
    COUNTY_COLUMNS,
    CRIME_COVERAGE_FLOOR,
    HOMESCHOOL_NOTICE,
    PILLAR_INTERNALS,
    average_tie_percentile,
    load_homeschool_policy,
    load_source_lock,
    validate_fbi_response_manifest,
)

PIPELINE_VERSION = "ranking-maintainer-etl-v2"
CRIME_STAGE_SCHEMA = 3
EMPLOYMENT_STAGE_SCHEMA = 2
IN_SCOPE_STATE_FIPS = frozenset(
    code for code, state in STATE_BY_FIPS.items() if state not in {"AS", "GU", "MP", "PR", "VI"}
)
ALL_CITATION_IDS = (
    "population",
    "hazard",
    "crime",
    "water",
    "healthcare",
    "community_context",
    "rpp",
    "property_tax",
    "employment",
    "broadband",
    "mountain",
    "climate",
    "homeschool",
)
METHODOLOGY_LIMITATIONS = (
    "Provider supply measures availability, not access, quality, or utilization.",
    "EPA public-water coverage is a conservative max-intersection proxy; service-area "
    "boundaries may be supplied or modeled, and the value is not evidence of private-well use.",
    "FCC broadband is the share of broadband-serviceable locations, not population coverage.",
    "Climate is missing without an in-county station containing all required NOAA normals; "
    "there is no nearest-station fallback.",
    HOMESCHOOL_NOTICE,
)


def _artifact(data_root: Path, source: str, filename: str) -> Path:
    path = data_root / "raw" / source / filename
    if path.is_symlink() or not path.is_file():
        raise HouseHunterError(f"Ranking source {source} is not staged: {filename}")
    return path


def _validate_locked_raw_artifacts(data_root: Path, source_lock: Mapping[str, Any]) -> str:
    """Validate every staged artifact before any normalized checkpoint can be reused."""
    records: list[dict[str, object]] = []
    for source in source_lock.get("sources", []):
        source_name = str(source.get("name") or "")
        for artifact in source.get("artifacts", []):
            filename = str(artifact.get("filename") or "")
            path = _artifact(data_root, source_name, filename)
            expected_bytes = artifact.get("bytes")
            expected_sha256 = artifact.get("sha256")
            actual_bytes = path.stat().st_size
            if actual_bytes != expected_bytes:
                raise HouseHunterError(
                    f"Ranking source {source_name} byte count differs: {filename}"
                )
            actual_sha256 = sha256_file(path)
            if actual_sha256 != expected_sha256:
                raise HouseHunterError(f"Ranking source {source_name} checksum differs: {filename}")
            records.append(
                {
                    "source": source_name,
                    "filename": filename,
                    "bytes": actual_bytes,
                    "sha256": actual_sha256,
                }
            )
    return sha256_bytes(canonical_json(records))


def _validate_packaged_inputs(source_lock: Mapping[str, Any]) -> str:
    sources = {str(source.get("name")): source for source in source_lock.get("sources", [])}
    assets = Path(__file__).with_name("assets")
    contracts = (
        (
            "omb_county_cbsa_2023",
            assets / "housing_stock" / "county_msa.parquet",
            "artifact_sha256",
        ),
        (
            "househunter_mountain_v2",
            assets / "mountain" / "counties.parquet",
            "county_artifact_sha256",
        ),
    )
    records: list[dict[str, str]] = []
    for source_name, path, identity_key in contracts:
        expected = (sources.get(source_name) or {}).get("identity", {}).get(identity_key)
        if path.is_symlink() or not path.is_file() or not isinstance(expected, str):
            raise HouseHunterError(f"Ranking packaged input is missing: {source_name}")
        actual = sha256_file(path)
        if actual != expected:
            raise HouseHunterError(f"Ranking packaged input checksum differs: {source_name}")
        records.append({"source": source_name, "sha256": actual})
    return sha256_bytes(canonical_json(records))


def _sources_contract(source_lock: Mapping[str, Any], *names: str) -> str:
    sources = {str(source.get("name")): source for source in source_lock.get("sources", [])}
    try:
        selected = [sources[name] for name in names]
    except KeyError as exc:
        raise HouseHunterError(f"Ranking source lock is missing {exc.args[0]}") from exc
    return sha256_bytes(canonical_json(selected))


def _validate_snapshot_input(snapshot: Path, source_lock: Mapping[str, Any]) -> dict[str, object]:
    metadata_path = snapshot / "build.json"
    counties_path = snapshot / "counties.parquet"
    database_path = snapshot / "househunter.duckdb"
    if (
        snapshot.is_symlink()
        or not snapshot.is_dir()
        or metadata_path.is_symlink()
        or counties_path.is_symlink()
        or database_path.is_symlink()
        or not metadata_path.is_file()
        or not counties_path.is_file()
        or not database_path.is_file()
    ):
        raise HouseHunterError("Ranking ETL requires a complete immutable snapshot input")
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HouseHunterError("Ranking snapshot metadata is invalid") from exc
    scope = metadata.get("scope")
    build_id = metadata.get("build_id")
    if (
        metadata.get("schema_version") != 13
        or not isinstance(build_id, str)
        or not build_id
        or snapshot.name != build_id
        or not isinstance(scope, dict)
        or scope.get("kind") != "national"
    ):
        raise HouseHunterError("Ranking ETL requires a schema-13 national snapshot input")
    if not snapshot_artifacts_are_valid(snapshot):
        raise HouseHunterError("Ranking snapshot failed immutable artifact validation")
    try:
        counties = pl.read_parquet(counties_path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise HouseHunterError("Ranking snapshot county table is invalid") from exc
    if "place_id" not in counties.columns:
        raise HouseHunterError("Ranking snapshot county table lacks place_id")
    all_county_fips = counties["place_id"].cast(pl.String).sort().to_list()
    if len(set(all_county_fips)) != len(all_county_fips) or any(
        len(fips) != 5 or not fips.isdigit() or fips[:2] not in STATE_BY_FIPS
        for fips in all_county_fips
    ):
        raise HouseHunterError("Ranking snapshot county identifiers are invalid")
    county_fips = [fips for fips in all_county_fips if fips[:2] in IN_SCOPE_STATE_FIPS]
    universe = source_lock.get("county_universe") or {}
    if len(county_fips) != universe.get("row_count") or sha256_bytes(
        canonical_json(county_fips)
    ) != universe.get("sorted_fips_sha256"):
        raise HouseHunterError("Ranking snapshot differs from the locked county universe")
    logical_checksum = (metadata.get("logical_checksums") or {}).get("counties")
    if not isinstance(logical_checksum, str):
        raise HouseHunterError("Ranking snapshot county logical checksum is missing")

    sources = {str(source.get("name")): source for source in source_lock.get("sources", [])}
    expected_inputs = {
        "fema_counties": (sources.get("fema_residential_hazard") or {})
        .get("identity", {})
        .get("canonical_sha256"),
        "chrr": (sources.get("chrr_community_context_2025") or {})
        .get("identity", {})
        .get("canonical_sha256"),
    }
    input_checksums = metadata.get("input_checksums")
    if not isinstance(input_checksums, dict) or any(
        not isinstance(expected, str) or input_checksums.get(key) != expected
        for key, expected in expected_inputs.items()
    ):
        raise HouseHunterError("Ranking snapshot source identities differ from the source lock")

    return {
        "schema_version": metadata["schema_version"],
        "build_id": build_id,
        "counties_sha256": sha256_file(counties_path),
        "counties_logical_checksum": logical_checksum,
    }


def _read_frame_checkpoint(path: Path, *, contract: Mapping[str, Any]) -> pl.DataFrame | None:
    checkpoint_path = path.with_suffix(path.suffix + ".checkpoint.json")
    if (
        path.is_symlink()
        or checkpoint_path.is_symlink()
        or not path.is_file()
        or not checkpoint_path.is_file()
    ):
        return None
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        checkpoint.get("schema") != 1
        or checkpoint.get("pipeline") != PIPELINE_VERSION
        or checkpoint.get("contract") != contract
        or checkpoint.get("bytes") != path.stat().st_size
        or checkpoint.get("sha256") != sha256_file(path)
    ):
        return None
    frame = pl.read_parquet(path)
    if checkpoint.get("rows") != frame.height or checkpoint.get("columns") != frame.columns:
        return None
    return frame


def _write_frame_checkpoint(
    path: Path, frame: pl.DataFrame, *, contract: Mapping[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    checkpoint_path = path.with_suffix(path.suffix + ".checkpoint.json")
    checkpoint_temporary = checkpoint_path.with_name(
        f".{checkpoint_path.name}.{uuid.uuid4().hex}.part"
    )
    try:
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        os.replace(temporary, path)
        checkpoint = {
            "schema": 1,
            "pipeline": PIPELINE_VERSION,
            "contract": contract,
            "rows": frame.height,
            "columns": frame.columns,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        checkpoint_temporary.write_bytes(
            json.dumps(checkpoint, indent=2, sort_keys=True).encode() + b"\n"
        )
        os.replace(checkpoint_temporary, checkpoint_path)
    finally:
        temporary.unlink(missing_ok=True)
        checkpoint_temporary.unlink(missing_ok=True)


def _normalized_stage(
    data_root: Path,
    name: str,
    *,
    contract: Mapping[str, Any],
    build: Callable[[], pl.DataFrame],
) -> pl.DataFrame:
    path = data_root / "normalized" / f"{name}.parquet"
    frame = _read_frame_checkpoint(path, contract=contract)
    if frame is not None:
        return frame
    frame = build()
    _write_frame_checkpoint(path, frame, contract=contract)
    return frame


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: object) -> float | None:
    if isinstance(value, str) and value.strip() in {"", "(D)", "(L)", "null", "None"}:
        return None
    return _finite(value)


def _unique(frame: pl.DataFrame, source: str) -> pl.DataFrame:
    if "county_fips" not in frame.columns:
        raise HouseHunterError(f"Ranking source {source} is missing county_fips")
    if frame.filter(~pl.col("county_fips").str.contains(r"^\d{5}$")).height:
        raise HouseHunterError(f"Ranking source {source} contains malformed county FIPS")
    if frame["county_fips"].n_unique() != frame.height:
        raise HouseHunterError(f"Ranking source {source} duplicates county FIPS")
    return frame.sort("county_fips")


def _join(base: pl.DataFrame, source: pl.DataFrame, label: str) -> pl.DataFrame:
    source = _unique(source, label)
    overlap = (set(base.columns) & set(source.columns)) - {"county_fips"}
    if overlap:
        raise HouseHunterError(
            f"Ranking source {label} would overwrite columns: {', '.join(sorted(overlap))}"
        )
    joined = base.join(source, on="county_fips", how="left", validate="1:1")
    if joined.height != base.height:
        raise HouseHunterError(f"Ranking source {label} multiplied the county join")
    return joined


def _ecdf_column(
    frame: pl.DataFrame, raw: str, utility: str, *, invert: bool = False
) -> pl.DataFrame:
    values = [_finite(value) for value in frame[raw].to_list()]
    valid = [(index, value) for index, value in enumerate(values) if value is not None]
    utilities: list[float | None] = [None] * len(values)
    scored = average_tie_percentile([value for _, value in valid], invert=invert)
    for (index, _), value in zip(valid, scored, strict=True):
        utilities[index] = value
    return frame.with_columns(pl.Series(utility, utilities, dtype=pl.Float64))


def _population(data_root: Path) -> pl.DataFrame:
    path = _artifact(data_root, "census_pep_county_2025", "co-est2025-alldata.csv")
    frame = pl.read_csv(
        path,
        encoding="windows-1252",
        schema_overrides={"STATE": pl.String, "COUNTY": pl.String, "SUMLEV": pl.String},
    ).filter(pl.col("SUMLEV") == "050")
    frame = frame.with_columns(
        (pl.col("STATE").str.zfill(2) + pl.col("COUNTY").str.zfill(3)).alias("county_fips")
    ).filter(pl.col("STATE").is_in(sorted(IN_SCOPE_STATE_FIPS)))
    return _unique(
        frame.select(
            "county_fips",
            pl.col("POPESTIMATE2025").cast(pl.Int64).alias("population"),
            pl.lit("2025").alias("population_vintage"),
            pl.lit("complete").alias("population_status"),
        ),
        "Census PEP",
    )


def _runtime_context(snapshot: Path) -> pl.DataFrame:
    path = snapshot / "counties.parquet"
    if path.is_symlink() or not path.is_file():
        raise HouseHunterError("Ranking ETL requires a validated national snapshot county table")
    frame = pl.read_parquet(path)
    required = {
        "place_id",
        "state",
        "res_hazard_npctl",
        "community_conditions_group",
    }
    if not required <= set(frame.columns):
        raise HouseHunterError("National snapshot lacks ranking hazard/community inputs")
    groups = frame["community_conditions_group"].cast(pl.Float64)
    return _unique(
        frame.select(
            pl.col("place_id").alias("county_fips"),
            "state",
            pl.col("res_hazard_npctl").cast(pl.Float64),
            (1.0 - pl.col("res_hazard_npctl").cast(pl.Float64) / 100.0).alias("u_hazard"),
            pl.when(pl.col("res_hazard_npctl").is_not_null())
            .then(pl.lit("complete"))
            .otherwise(pl.lit("missing"))
            .alias("hazard_status"),
            groups.alias("community_context"),
            pl.when(groups.is_between(1, 10, closed="both"))
            .then((10.0 - groups) / 9.0)
            .otherwise(None)
            .alias("u_community_context"),
            pl.when(groups.is_between(1, 10, closed="both"))
            .then(pl.lit("published_group_reconstructed"))
            .otherwise(pl.lit("missing"))
            .alias("community_context_kind"),
            pl.when(groups.is_between(1, 10, closed="both"))
            .then(pl.lit("complete"))
            .otherwise(pl.lit("missing"))
            .alias("community_context_status"),
        ),
        "runtime county context",
    )


def _agency_series(container: object, suffix: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(container, dict):
        raise HouseHunterError("FBI summary series is malformed")
    matches = [
        (str(key)[: -len(suffix)], value)
        for key, value in container.items()
        if str(key).endswith(suffix)
    ]
    if len(matches) != 1 or not isinstance(matches[0][1], dict):
        raise HouseHunterError("FBI summary series identity is ambiguous")
    return matches[0]


def _validate_fbi_summary_inputs(data_root: Path, source_lock_path: Path | None) -> dict[str, Any]:
    manifest = validate_fbi_response_manifest(source_lock_path=source_lock_path)
    contract = str(manifest["contract_sha256"])
    summary_root = data_root / "raw" / "fbi" / contract / "summaries"
    if summary_root.is_symlink() or not summary_root.is_dir():
        raise HouseHunterError("FBI normalized summary directory is missing")
    expected_paths: set[Path] = set()
    for response in manifest["responses"]:
        if response.get("kind") != "summary":
            continue
        key = str(response.get("key") or "")
        if ":" not in key:
            raise HouseHunterError("FBI response manifest summary key is malformed")
        ori, family = key.rsplit(":", 1)
        path = summary_root / family / f"{ori}.json"
        expected_paths.add(path)
        if path.is_symlink() or not path.is_file():
            raise HouseHunterError(f"FBI normalized input is missing for {ori} {family}")
        if path.stat().st_size != response.get("bytes") or sha256_file(path) != response.get(
            "sha256"
        ):
            raise HouseHunterError(f"FBI normalized input differs for {ori} {family}")
    actual_paths = set(summary_root.glob("*/*.json"))
    if actual_paths != expected_paths or any(path.is_symlink() for path in actual_paths):
        raise HouseHunterError("FBI normalized summary inventory differs from the manifest")
    return manifest


def _crime(data_root: Path, manifest: Mapping[str, Any]) -> pl.DataFrame:
    agencies = {row["ori"]: row["county_fips"] for row in manifest["agencies"]}
    contract = str(manifest["contract_sha256"])
    root = data_root / "raw" / "fbi" / contract / "summaries"
    aggregate: dict[tuple[str, str, int], dict[str, float | bool]] = defaultdict(
        lambda: {"offenses": 0.0, "population": 0.0, "participated": 0.0, "valid": True}
    )
    for ori, county_fips in sorted(agencies.items()):
        for family in ("violent-crime", "property-crime"):
            path = root / family / f"{ori}.json"
            if path.is_symlink() or not path.is_file():
                raise HouseHunterError(f"FBI normalized input is missing for {ori} {family}")
            payload = json.loads(path.read_text())
            name, actuals = _agency_series(payload["offenses"]["actuals"], " Offenses")
            populations = payload["populations"]["population"][name]
            participated = payload["populations"]["participated_population"][name]
            for year in (2023, 2024, 2025):
                expected = {f"{month:02d}-{year}" for month in range(1, 13)}
                if any(
                    {month for month in series if month.endswith(str(year))} != expected
                    for series in (actuals, populations, participated)
                ):
                    aggregate[(county_fips, family, year)]["valid"] = False
            for month in sorted(actuals):
                year = int(month[-4:])
                target = aggregate[(county_fips, family, year)]
                values = (
                    _number(actuals[month]),
                    _number(populations.get(month)),
                    _number(participated.get(month)),
                )
                if any(value is None for value in values):
                    target["valid"] = False
                    continue
                offenses, population, reported = values
                if population < 0 or reported < 0 or offenses < 0:
                    raise HouseHunterError("FBI summary contains a negative measure")
                target["offenses"] += offenses
                target["population"] += population
                target["participated"] += reported
    rows: list[dict[str, Any]] = []
    for county_fips in sorted(set(agencies.values())):
        row: dict[str, Any] = {"county_fips": county_fips}
        rates: dict[str, float | None] = {}
        all_coverages: list[float] = []
        complete = True
        for family, short in (("violent-crime", "violent"), ("property-crime", "property")):
            offenses = 0.0
            participated = 0.0
            for year in (2023, 2024, 2025):
                value = aggregate.get((county_fips, family, year))
                coverage = None
                if value and value["valid"] and value["population"] > 0:
                    coverage = float(value["participated"]) / float(value["population"])
                    offenses += float(value["offenses"])
                    participated += float(value["participated"])
                row[f"crime_{short}_coverage_{year}"] = coverage
                if coverage is None or coverage < CRIME_COVERAGE_FLOOR:
                    complete = False
                else:
                    all_coverages.append(coverage)
            rates[short] = (
                offenses / (participated / 12.0) * 100_000.0
                if complete and participated > 0
                else None
            )
        if not complete:
            rates = {"violent": None, "property": None}
        row.update(
            {
                "crime_violent_rate": rates["violent"],
                "crime_property_rate": rates["property"],
                "crime_coverage": min(all_coverages) if complete and all_coverages else None,
                "crime_status": "complete" if complete else "below_coverage_or_missing",
            }
        )
        rows.append(row)
    frame = pl.DataFrame(rows)
    frame = _ecdf_column(frame, "crime_violent_rate", "u_crime_violent", invert=True)
    frame = _ecdf_column(frame, "crime_property_rate", "u_crime_property", invert=True)
    frame = frame.with_columns(
        pl.when(pl.col("u_crime_violent").is_not_null() & pl.col("u_crime_property").is_not_null())
        .then(0.60 * pl.col("u_crime_violent") + 0.40 * pl.col("u_crime_property"))
        .otherwise(None)
        .alias("u_crime")
    )
    return _unique(frame, "FBI crime")


def _read_zip_csv(path: Path, member_suffix: str, **kwargs: Any) -> pl.DataFrame:
    with zipfile.ZipFile(path) as archive:
        matches = [name for name in archive.namelist() if name.endswith(member_suffix)]
        if len(matches) != 1:
            raise HouseHunterError(f"Archive member {member_suffix} is missing or ambiguous")
        return pl.read_csv(io.BytesIO(archive.read(matches[0])), **kwargs)


def _health(data_root: Path) -> pl.DataFrame:
    ahrf = _read_zip_csv(
        _artifact(data_root, "hrsa_ahrf_2024_2025", "AHRF_2024-2025_CSV.zip"),
        "/AHRF2025.csv",
        schema_overrides={"fips_st_cnty": pl.String},
        infer_schema_length=0,
    ).select(
        pl.col("fips_st_cnty").str.zfill(5).alias("county_fips"),
        pl.col("phys_nf_prim_care_pc_exc_rsdt_23")
        .cast(pl.Float64, strict=False)
        .alias("provider_primary_care_count"),
        pl.col("dent_nf_fed_proflly_activ_23")
        .cast(pl.Float64, strict=False)
        .alias("provider_dental_count"),
    )
    supplement = pl.read_csv(
        _artifact(
            data_root,
            "chrr_nppes_mental_health_2025",
            "analytic_supplement_20260325.csv",
        ),
        infer_schema_length=0,
        null_values=["", "NA", "N/A"],
    )
    lower = {column.lower(): column for column in supplement.columns}
    fips_column = next(
        (lower[name] for name in ("fipscode", "fips", "county_fips") if name in lower),
        None,
    )
    mental_column = lower.get("v062_numerator")
    if fips_column is None or mental_column is None:
        raise HouseHunterError("CHR&R mental-health supplement schema differs")
    mental = supplement.select(
        pl.col(fips_column).cast(pl.String).str.zfill(5).alias("county_fips"),
        pl.col(mental_column).cast(pl.Float64, strict=False).alias("provider_mental_health_count"),
    )
    return _join(_unique(ahrf, "AHRF"), mental, "CHR&R mental health")


def _acs_table(path: Path, estimate: str) -> pl.DataFrame:
    frame = pl.read_csv(
        path,
        separator="|",
        schema_overrides={"GEO_ID": pl.String},
        infer_schema_length=0,
        null_values=["", "null", "-666666666", "-999999999"],
    ).filter(pl.col("GEO_ID").str.starts_with("0500000US"))
    return _unique(
        frame.select(
            pl.col("GEO_ID").str.slice(-5).alias("county_fips"),
            pl.col(estimate).cast(pl.Float64, strict=False).alias(estimate),
        ),
        estimate,
    )


def _property_tax(data_root: Path) -> pl.DataFrame:
    tax = _acs_table(
        _artifact(data_root, "acs_property_tax_2024", "acsdt5y2024-b25103.dat"),
        "B25103_E001",
    )
    value = _acs_table(
        _artifact(data_root, "acs_property_tax_2024", "acsdt5y2024-b25077.dat"),
        "B25077_E001",
    )
    return (
        _join(tax, value, "ACS home value")
        .with_columns(
            pl.when((pl.col("B25103_E001") > 0) & (pl.col("B25077_E001") > 0))
            .then(pl.col("B25103_E001") / pl.col("B25077_E001"))
            .otherwise(None)
            .alias("property_tax_rate"),
            pl.when((pl.col("B25103_E001") > 0) & (pl.col("B25077_E001") > 0))
            .then(pl.lit("complete"))
            .otherwise(pl.lit("missing_or_invalid_denominator"))
            .alias("property_tax_status"),
        )
        .drop("B25103_E001", "B25077_E001")
    )


def _rpp(data_root: Path) -> pl.DataFrame:
    counties = _population(data_root).select("county_fips")
    county_cbsa = pl.read_parquet(
        Path(__file__).with_name("assets") / "housing_stock" / "county_msa.parquet"
    )
    marpp = (
        _read_zip_csv(
            _artifact(data_root, "bea_marpp_2024", "MARPP.zip"),
            "MARPP_MSA_2008_2024.csv",
            schema_overrides={"GeoFIPS": pl.String},
            null_values=["(NA)"],
        )
        .filter(pl.col("LineCode") == 1)
        .select(
            pl.col("GeoFIPS").str.strip_chars(' "').str.zfill(5).alias("cbsa_id"),
            pl.col("2024").cast(pl.Float64).alias("metro_rpp"),
        )
    )
    sarpp = (
        _read_zip_csv(
            _artifact(data_root, "bea_sarpp_2024", "SARPP.zip"),
            "SARPP_STATE_2008_2024.csv",
            schema_overrides={"GeoFIPS": pl.String},
            null_values=["(NA)"],
        )
        .filter(pl.col("LineCode") == 1)
        .select(
            pl.col("GeoFIPS").str.strip_chars(' "').str.slice(0, 2).alias("state_fips"),
            pl.col("2024").cast(pl.Float64).alias("state_rpp"),
        )
    )
    if (
        marpp["cbsa_id"].n_unique() != marpp.height
        or sarpp["state_fips"].n_unique() != sarpp.height
    ):
        raise HouseHunterError("BEA RPP source contains duplicate geography")
    result = (
        counties.join(county_cbsa, on="county_fips", how="left", validate="1:1")
        .join(marpp, on="cbsa_id", how="left", validate="m:1")
        .with_columns(pl.col("county_fips").str.slice(0, 2).alias("state_fips"))
        .join(sarpp, on="state_fips", how="left", validate="m:1")
    )
    return _unique(
        result.select(
            "county_fips",
            pl.coalesce("metro_rpp", "state_rpp").alias("rpp_index"),
            pl.when(pl.col("metro_rpp").is_not_null())
            .then(pl.lit("metropolitan"))
            .otherwise(pl.lit("state"))
            .alias("rpp_geography_type"),
            pl.when(pl.coalesce("metro_rpp", "state_rpp").is_not_null())
            .then(pl.lit("complete"))
            .otherwise(pl.lit("missing"))
            .alias("rpp_status"),
        ),
        "BEA RPP",
    )


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    index = 0
    for character in letters:
        index = index * 26 + ord(character.upper()) - ord("A") + 1
    return index - 1


def _xlsx_rows(payload: bytes) -> Iterable[list[str]]:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(io.BytesIO(payload)) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in value.iter(f"{{{namespace}}}t"))
                for value in root.findall(f"{{{namespace}}}si")
            ]
        with workbook.open("xl/worksheets/sheet1.xml") as worksheet:
            for _, element in ElementTree.iterparse(worksheet, events=("end",)):
                if element.tag != f"{{{namespace}}}row":
                    continue
                values: dict[int, str] = {}
                for cell in element.findall(f"{{{namespace}}}c"):
                    value = cell.find(f"{{{namespace}}}v")
                    text = "" if value is None or value.text is None else value.text
                    if cell.get("t") == "s" and text:
                        text = shared[int(text)]
                    values[_column_index(str(cell.get("r")))] = text
                width = max(values, default=-1) + 1
                yield [values.get(index, "") for index in range(width)]
                element.clear()


def _xlsx_value(values: list[str], index: Mapping[str, int], name: str) -> str:
    position = index[name]
    return values[position] if position < len(values) else ""


def _qcew(path: Path, annual_member: str) -> pl.DataFrame:
    with zipfile.ZipFile(path) as archive:
        rows = iter(_xlsx_rows(archive.read(annual_member)))
        header = [value.replace("\n", " ").strip() for value in next(rows)]
        required = {
            "St",
            "Cnty",
            "Own",
            "NAICS",
            "Qtr",
            "Area Type",
            "Annual Average Employment",
            "Annual Average Weekly Wage",
        }
        if not required <= set(header):
            raise HouseHunterError("QCEW annual workbook schema differs")
        index = {name: header.index(name) for name in required}
        output: list[dict[str, object]] = []
        for values in rows:
            if (
                _xlsx_value(values, index, "Area Type") != "County"
                or _xlsx_value(values, index, "Own") != "0"
                or _xlsx_value(values, index, "NAICS") != "10"
                or _xlsx_value(values, index, "Qtr") != "A"
            ):
                continue
            output.append(
                {
                    "county_fips": _xlsx_value(values, index, "St").zfill(2)
                    + _xlsx_value(values, index, "Cnty").zfill(3),
                    "employment": _number(_xlsx_value(values, index, "Annual Average Employment")),
                    "average_weekly_wage": _number(
                        _xlsx_value(values, index, "Annual Average Weekly Wage")
                    ),
                }
            )
    return _unique(pl.DataFrame(output), f"QCEW {annual_member}")


def _employment(data_root: Path) -> pl.DataFrame:
    prior = _qcew(
        _artifact(data_root, "bls_qcew_2024", "2024_all_county_high_level.zip"),
        "allhlcn24.xlsx",
    ).rename({"employment": "employment_2024", "average_weekly_wage": "wage_2024"})
    current = _qcew(
        _artifact(data_root, "bls_qcew_2025", "2025_all_county_high_level.zip"),
        "allhlcn25.xlsx",
    ).rename({"employment": "employment_2025"})
    raw = pl.read_csv(
        _artifact(data_root, "acs_commute_2024", "acsdt5y2024-b08303.dat"),
        separator="|",
        schema_overrides={"GEO_ID": pl.String},
        infer_schema_length=0,
        null_values=["", "null", "-666666666", "-999999999"],
    ).filter(pl.col("GEO_ID").str.starts_with("0500000US"))
    commute = _unique(
        raw.select(
            pl.col("GEO_ID").str.slice(-5).alias("county_fips"),
            pl.sum_horizontal(
                *[
                    pl.col(f"B08303_E00{index}").cast(pl.Float64, strict=False)
                    for index in range(2, 8)
                ],
                ignore_nulls=False,
            ).alias("under_30"),
            pl.col("B08303_E001").cast(pl.Float64, strict=False).alias("commute_total"),
        )
        .with_columns(
            pl.when(pl.col("commute_total") > 0)
            .then(pl.col("under_30") / pl.col("commute_total"))
            .otherwise(None)
            .alias("commute_under_30_share")
        )
        .select("county_fips", "commute_under_30_share"),
        "ACS commute",
    )
    result = _join(_join(prior, current, "QCEW 2025"), commute, "ACS commute")
    return result.with_columns(
        pl.when(pl.col("employment_2024") > 0)
        .then((pl.col("employment_2025") - pl.col("employment_2024")) / pl.col("employment_2024"))
        .otherwise(None)
        .alias("employment_growth"),
        pl.when(
            (pl.col("employment_2024") > 0)
            & pl.col("employment_2025").is_not_null()
            & pl.col("average_weekly_wage").is_not_null()
            & pl.col("commute_under_30_share").is_not_null()
        )
        .then(pl.lit("complete"))
        .otherwise(pl.lit("missing_component"))
        .alias("employment_status"),
    ).select(
        "county_fips",
        "employment_growth",
        "average_weekly_wage",
        "commute_under_30_share",
        "employment_status",
    )


def _broadband(data_root: Path) -> pl.DataFrame:
    frame = _read_zip_csv(
        _artifact(
            data_root,
            "fcc_fixed_summary_2025_12",
            "bdc_us_fixed_broadband_summary_by_geography_D25_03sep2026.zip",
        ),
        "bdc_us_fixed_broadband_summary_by_geography_D25_03sep2026.csv",
        infer_schema_length=0,
    )
    lower = {column.lower(): column for column in frame.columns}
    required = {
        "area_data_type",
        "geography_type",
        "geography_id",
        "technology",
        "biz_res",
        "speed_100_20",
    }
    if not required <= set(lower):
        raise HouseHunterError("FCC county aggregate schema differs")
    filtered = frame.filter(
        (pl.col(lower["area_data_type"]) == "Total")
        & (pl.col(lower["geography_type"]) == "County")
        & (pl.col(lower["technology"]) == "Any Terrestrial")
        & (pl.col(lower["biz_res"]) == "R")
    )
    return _unique(
        filtered.select(
            pl.col(lower["geography_id"]).cast(pl.String).str.zfill(5).alias("county_fips"),
            pl.col(lower["speed_100_20"]).cast(pl.Float64, strict=False).alias("broadband_100_20"),
            pl.lit("broadband-serviceable locations").alias("broadband_denominator_label"),
            pl.when(pl.col(lower["speed_100_20"]).cast(pl.Float64, strict=False).is_between(0, 1))
            .then(pl.lit("complete"))
            .otherwise(pl.lit("missing_or_invalid"))
            .alias("broadband_status"),
        ),
        "FCC broadband",
    )


def _sdwis_csv(archive_path: Path, member: str) -> pl.DataFrame:
    with zipfile.ZipFile(archive_path) as archive:
        if member not in archive.namelist():
            raise HouseHunterError(f"SDWIS archive is missing {member}")
        return pl.read_csv(
            io.BytesIO(archive.read(member)),
            infer_schema_length=0,
            schema_overrides={"PWSID": pl.String},
            null_values=[""],
        )


def _duckdb_frame(relation: duckdb.DuckDBPyRelation) -> pl.DataFrame:
    return pl.DataFrame(relation.fetchall(), schema=relation.columns, orient="row")


def _materialize_sdwis_member(
    data_root: Path,
    member: str,
    source_lock_path: Path | None,
) -> Path:
    lock = load_source_lock(source_lock_path)
    source = next(item for item in lock["sources"] if item["name"] == "epa_sdwis_2026_q2")
    archive_contract = source["artifacts"][0]
    contract = next(item for item in archive_contract["archive_members"] if item["name"] == member)
    archive_path = _artifact(data_root, "epa_sdwis_2026_q2", "SDWA_latest_downloads_2026-09-14.zip")
    destination = data_root / "work" / "epa_sdwis_2026_q2" / member
    checkpoint = destination.with_suffix(destination.suffix + ".checkpoint.json")
    expected_checkpoint = {
        "schema": 1,
        "archive_sha256": archive_contract["sha256"],
        "member": member,
        "bytes": contract["bytes"],
        "sha256": contract["sha256"],
    }
    if destination.is_file() and checkpoint.is_file() and not destination.is_symlink():
        try:
            actual_checkpoint = json.loads(checkpoint.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            actual_checkpoint = None
        if (
            actual_checkpoint == expected_checkpoint
            and destination.stat().st_size == contract["bytes"]
            and sha256_file(destination) == contract["sha256"]
        ):
            return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    received = 0
    try:
        with (
            zipfile.ZipFile(archive_path) as archive,
            archive.open(member) as source_handle,
            temporary.open("xb") as output,
        ):
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                received += len(chunk)
                if received > contract["bytes"]:
                    raise HouseHunterError(f"SDWIS {member} exceeds its locked size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if received != contract["bytes"] or digest.hexdigest() != contract["sha256"]:
            raise HouseHunterError(f"SDWIS {member} differs from its archive contract")
        os.replace(temporary, destination)
        checkpoint.write_bytes(
            json.dumps(expected_checkpoint, indent=2, sort_keys=True).encode() + b"\n"
        )
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _water(
    data_root: Path,
    source_lock_path: Path | None,
) -> pl.DataFrame:
    archive = _artifact(data_root, "epa_sdwis_2026_q2", "SDWA_latest_downloads_2026-09-14.zip")
    systems = _sdwis_csv(archive, "SDWA_PUB_WATER_SYSTEMS.csv").filter(
        pl.col("SUBMISSIONYEARQUARTER") == "2026Q2"
    )
    system_fields = [
        "PWSID",
        "PRIMACY_AGENCY_CODE",
        "PWS_ACTIVITY_CODE",
        "PWS_TYPE_CODE",
        "POPULATION_SERVED_COUNT",
        "IS_WHOLESALER_IND",
        "SUBMISSION_STATUS_CODE",
    ]
    systems = systems.select(system_fields).unique()
    if systems["PWSID"].n_unique() != systems.height:
        raise HouseHunterError("SDWIS current-quarter systems conflict by PWSID")
    system_rows = {row["PWSID"]: row for row in systems.iter_rows(named=True)}

    service = (
        _sdwis_csv(archive, "SDWA_SERVICE_AREAS.csv")
        .filter(pl.col("SUBMISSIONYEARQUARTER") == "2026Q2")
        .select("PWSID", "SERVICE_AREA_TYPE_CODE")
        .unique()
    )
    reference = _sdwis_csv(archive, "SDWA_REF_CODE_VALUES.csv")
    reviewed_codes = {
        row["VALUE_CODE"]: row["VALUE_DESCRIPTION"]
        for row in reference.filter(pl.col("VALUE_TYPE") == "SERVICE_AREA_TYPE_CODE").iter_rows(
            named=True
        )
    }
    if reviewed_codes.get("WH") != "Wholesaler of Water":
        raise HouseHunterError("SDWIS wholesale service-area code differs")
    unknown_codes = set(service["SERVICE_AREA_TYPE_CODE"].drop_nulls().to_list()) - set(
        reviewed_codes
    )
    if unknown_codes:
        raise HouseHunterError("SDWIS contains unknown service-area codes")
    service_codes: dict[str, set[str]] = defaultdict(set)
    for row in service.iter_rows(named=True):
        if row["SERVICE_AREA_TYPE_CODE"]:
            service_codes[row["PWSID"]].add(row["SERVICE_AREA_TYPE_CODE"])

    retail: dict[str, float] = {}
    unknown_classification: dict[str, float] = {}
    for pwsid, row in system_rows.items():
        population = _number(row["POPULATION_SERVED_COUNT"])
        if (
            row["PWS_TYPE_CODE"] != "CWS"
            or row["PWS_ACTIVITY_CODE"] != "A"
            or row["SUBMISSION_STATUS_CODE"] != "Y"
            or population is None
            or population <= 0
        ):
            continue
        wholesaler = row["IS_WHOLESALER_IND"]
        if wholesaler == "N":
            retail[pwsid] = population
        elif wholesaler == "Y" and service_codes.get(pwsid):
            if service_codes[pwsid] != {"WH"}:
                retail[pwsid] = population
        else:
            unknown_classification[pwsid] = population

    violation_path = _materialize_sdwis_member(
        data_root, "SDWA_VIOLATIONS_ENFORCEMENT.csv", source_lock_path
    )
    lock = load_source_lock(source_lock_path)
    sdwis = next(source for source in lock["sources"] if source["name"] == "epa_sdwis_2026_q2")
    violation_contract = next(
        member
        for member in sdwis["artifacts"][0]["archive_members"]
        if member["name"] == "SDWA_VIOLATIONS_ENFORCEMENT.csv"
    )
    violation_cache = data_root / "normalized" / "epa_health_violations_2025.parquet"
    violation_rows = _read_frame_checkpoint(
        violation_cache,
        contract={
            "source_sha256": violation_contract["sha256"],
            "period": "calendar-2025-overlap",
            "health_based": True,
            "stage_schema": 2,
        },
    )
    if violation_rows is None:
        escaped_violation = str(violation_path).replace("'", "''")
        connection = duckdb.connect()
        connection.execute("PRAGMA threads=1")
        try:
            violation_rows = _duckdb_frame(
                connection.sql(
                    f"""
                SELECT DISTINCT PWSID, VIOLATION_ID, NON_COMPL_PER_BEGIN_DATE,
                       NON_COMPL_PER_END_DATE, IS_HEALTH_BASED_IND
                FROM read_csv('{escaped_violation}', header=true, all_varchar=true)
                WHERE IS_HEALTH_BASED_IND = 'Y'
                  AND try_strptime(NON_COMPL_PER_BEGIN_DATE, '%m/%d/%Y') <= DATE '2025-12-31'
                  AND (
                    NON_COMPL_PER_END_DATE IS NULL
                    OR NON_COMPL_PER_END_DATE = ''
                    OR try_strptime(NON_COMPL_PER_END_DATE, '%m/%d/%Y') >= DATE '2025-01-01'
                  )
                """
                )
            ).sort("PWSID", "VIOLATION_ID")
        finally:
            connection.close()
        _write_frame_checkpoint(
            violation_cache,
            violation_rows,
            contract={
                "source_sha256": violation_contract["sha256"],
                "period": "calendar-2025-overlap",
                "health_based": True,
                "stage_schema": 2,
            },
        )
    duplicate_violations = (
        violation_rows.group_by("PWSID", "VIOLATION_ID").len().filter(pl.col("len") > 1)
    )
    if duplicate_violations.height:
        raise HouseHunterError("SDWIS violations conflict by PWSID and VIOLATION_ID")
    violating = set(violation_rows["PWSID"].to_list())

    from pyogrio.raw import read as read_vector

    _, _, _, fields = read_vector(
        _artifact(data_root, "epa_cws_service_areas_v2_1", "CWS_2_1.gpkg"),
        layer="Boundaries",
        columns=["PWSID", "Symbology_Field"],
        read_geometry=False,
    )
    provenance: dict[str, str] = {}
    for raw_pwsid, value in zip(fields[0], fields[1], strict=True):
        mapped = {"System Sourced": "supplied", "Modeled": "modeled"}.get(str(value))
        if mapped is None:
            raise HouseHunterError("EPA water boundary provenance differs")
        pwsids = [part.strip() for part in str(raw_pwsid).split(";") if part.strip()]
        if not pwsids:
            raise HouseHunterError("EPA water boundary has an empty PWSID")
        for pwsid in pwsids:
            previous = provenance.setdefault(pwsid, mapped)
            if previous != mapped:
                provenance[pwsid] = "mixed"

    block_path = _artifact(data_root, "epa_cws_service_areas_v2_1", "Blocks_V_2_1.csv")
    boundary_source = next(
        source for source in lock["sources"] if source["name"] == "epa_cws_service_areas_v2_1"
    )
    boundary_artifacts = {
        artifact["filename"]: artifact["sha256"] for artifact in boundary_source["artifacts"]
    }
    active_pwsids = sorted(set(retail) & set(provenance))
    block_contract = {
        "stage_schema": 2,
        "blocks_sha256": boundary_artifacts["Blocks_V_2_1.csv"],
        "boundaries_sha256": boundary_artifacts["CWS_2_1.gpkg"],
        "active_pws_sha256": sha256_bytes(canonical_json(active_pwsids)),
        "overlap_method": "max-intersection-per-pws-and-block",
    }
    allocation_cache = data_root / "normalized" / "epa_water_block_allocations.parquet"
    proxy_cache = data_root / "normalized" / "epa_water_coverage_proxy.parquet"
    allocations = _read_frame_checkpoint(allocation_cache, contract=block_contract)
    proxy = _read_frame_checkpoint(proxy_cache, contract=block_contract)
    if allocations is None or proxy is None:
        escaped_blocks = str(block_path).replace("'", "''")
        connection = duckdb.connect()
        connection.execute("PRAGMA threads=1")
        connection.execute("CREATE TEMP TABLE active_pws (PWSID VARCHAR PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO active_pws VALUES (?)", [(pwsid,) for pwsid in active_pwsids]
        )
        block_source = f"""(
            WITH raw AS (
              SELECT row_number() OVER () AS source_row_id,
                     GEOID20,
                     regexp_replace(PWSID, '\\s+', '', 'g') AS PWSID_SET,
                     Pop20_AW
              FROM read_csv(
                '{escaped_blocks}', header=true, all_varchar=true,
                delim=',', quote='"', escape='"'
              )
            )
            SELECT source_row_id, GEOID20, PWSID_SET,
                   unnest(string_split(PWSID_SET, ';')) AS PWSID, Pop20_AW
            FROM raw
        )"""
        try:
            connection.execute(
                f"""
                CREATE TEMP TABLE selected_blocks AS
                SELECT b.source_row_id, b.PWSID_SET, b.PWSID, b.GEOID20, b.Pop20_AW
                FROM {block_source} b
                INNER JOIN active_pws a ON a.PWSID = b.PWSID
                """
            )
            deduplicated = """
                SELECT PWSID, GEOID20, max(try_cast(Pop20_AW AS DOUBLE)) AS Pop20_AW
                FROM selected_blocks
                GROUP BY PWSID, GEOID20
            """
            invalid_blocks = connection.sql(
                """
                SELECT count(*) AS n
                FROM selected_blocks
                WHERE try_cast(Pop20_AW AS DOUBLE) IS NULL
                   OR try_cast(Pop20_AW AS DOUBLE) < 0
                """
            ).fetchone()[0]
            if invalid_blocks:
                raise HouseHunterError(
                    f"EPA block allocation contains {invalid_blocks} invalid rows"
                )
            allocations = _duckdb_frame(
                connection.sql(
                    f"""
                SELECT PWSID, substr(GEOID20, 1, 5) AS county_fips,
                       sum(Pop20_AW) AS block_population
                FROM ({deduplicated})
                GROUP BY PWSID, county_fips
                """
                )
            ).sort("PWSID", "county_fips")
            proxy = _duckdb_frame(
                connection.sql(
                    f"""
                WITH per_block AS (
                  SELECT GEOID20, max(Pop20_AW) AS proxy_population,
                         sum(Pop20_AW) - max(Pop20_AW) AS duplicate_population,
                         sum(Pop20_AW) AS total_population
                  FROM ({deduplicated})
                  GROUP BY GEOID20
                )
                SELECT substr(GEOID20, 1, 5) AS county_fips,
                       sum(proxy_population) AS proxy_population,
                       sum(duplicate_population) / nullif(sum(total_population), 0)
                         AS overlap_duplicate_share_proxy
                FROM per_block GROUP BY county_fips
                """
                )
            ).sort("county_fips")
        finally:
            connection.close()
        _write_frame_checkpoint(allocation_cache, allocations, contract=block_contract)
        _write_frame_checkpoint(proxy_cache, proxy, contract=block_contract)

    current_fips = set(_population(data_root)["county_fips"].to_list())
    allocations = allocations.filter(
        pl.col("county_fips").is_in(sorted(current_fips))
        & ~pl.col("county_fips").str.starts_with("09")
    )
    allocation_by_system: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in allocations.sort("PWSID", "county_fips").iter_rows(named=True):
        allocation_by_system[row["PWSID"]].append(
            (row["county_fips"], float(row["block_population"]))
        )

    geographic = (
        _sdwis_csv(archive, "SDWA_GEOGRAPHIC_AREAS.csv")
        .filter((pl.col("SUBMISSIONYEARQUARTER") == "2026Q2") & (pl.col("AREA_TYPE_CODE") == "CN"))
        .select("PWSID", "ANSI_ENTITY_CODE")
        .unique()
    )
    state_fips = {state: code for code, state in STATE_BY_FIPS.items()}
    served: dict[str, set[str]] = defaultdict(set)
    for row in geographic.iter_rows(named=True):
        pwsid = row["PWSID"]
        system = system_rows.get(pwsid)
        if system is None:
            continue
        primacy = system["PRIMACY_AGENCY_CODE"]
        prefix = str(pwsid)[:2]
        if prefix.isalpha() and prefix != primacy:
            raise HouseHunterError(f"SDWIS primacy and PWSID prefix conflict for {pwsid}")
        ansi = row["ANSI_ENTITY_CODE"]
        if (
            not prefix.isalpha()
            or primacy not in state_fips
            or not ansi
            or not ansi.isdigit()
            or len(ansi) != 3
        ):
            continue
        candidate = state_fips[primacy] + ansi
        if candidate in current_fips:
            served[pwsid].add(candidate)

    allocated_contributions: dict[str, list[float]] = defaultdict(list)
    violating_contributions: dict[str, list[float]] = defaultdict(list)
    county_provenance: dict[str, set[str]] = defaultdict(set)
    usable_systems: set[str] = set()
    for pwsid, population in sorted(retail.items()):
        weights = allocation_by_system.get(pwsid, [])
        total = math.fsum(weight for _, weight in weights)
        if total <= 0 or pwsid not in provenance:
            continue
        usable_systems.add(pwsid)
        reconciled: list[float] = []
        for county_fips, weight in weights:
            allocated = population * weight / total
            allocated_contributions[county_fips].append(allocated)
            if pwsid in violating:
                violating_contributions[county_fips].append(allocated)
            county_provenance[county_fips].add(provenance[pwsid])
            reconciled.append(allocated)
        if abs(math.fsum(reconciled) - population) > max(1e-9, population * 1e-9):
            raise HouseHunterError(f"EPA water allocation does not reconcile for {pwsid}")

    allocated_population = {
        county_fips: math.fsum(values)
        for county_fips, values in sorted(allocated_contributions.items())
    }
    violating_population = {
        county_fips: math.fsum(values)
        for county_fips, values in sorted(violating_contributions.items())
    }

    uncovered_population: dict[str, float] = defaultdict(float)
    ambiguous_counties: set[str] = set()
    for pwsid, population in sorted(
        {
            **{key: value for key, value in retail.items() if key not in usable_systems},
            **unknown_classification,
        }.items()
    ):
        counties = served.get(pwsid, set())
        if len(counties) == 1:
            uncovered_population[next(iter(counties))] += population
        elif len(counties) > 1:
            ambiguous_counties.update(counties)

    base = _population(data_root)
    estimates_base = (
        pl.read_csv(
            _artifact(data_root, "census_pep_county_2025", "co-est2025-alldata.csv"),
            encoding="windows-1252",
            schema_overrides={"STATE": pl.String, "COUNTY": pl.String, "SUMLEV": pl.String},
        )
        .filter(pl.col("SUMLEV") == "050")
        .select(
            (pl.col("STATE").str.zfill(2) + pl.col("COUNTY").str.zfill(3)).alias("county_fips"),
            pl.col("ESTIMATESBASE2020").cast(pl.Float64).alias("population_base_2020"),
        )
    )
    proxy_by_county = {row["county_fips"]: row for row in proxy.iter_rows(named=True)}
    estimates = {
        row["county_fips"]: float(row["population_base_2020"])
        for row in estimates_base.iter_rows(named=True)
    }
    rows: list[dict[str, Any]] = []
    for county_fips in base["county_fips"].to_list():
        allocated = allocated_population.get(county_fips, 0.0)
        uncovered = uncovered_population[county_fips]
        denominator = allocated + uncovered
        coverage = allocated / denominator if denominator > 0 else None
        violation_share = (
            violating_population.get(county_fips, 0.0) / allocated if allocated > 0 else None
        )
        proxy_row = proxy_by_county.get(county_fips)
        public_coverage = None
        overlap = None
        overlap_quality = "missing_proxy"
        if proxy_row and estimates.get(county_fips, 0) > 0:
            raw_public_coverage = float(proxy_row["proxy_population"]) / estimates[county_fips]
            if raw_public_coverage < 0:
                raise HouseHunterError(f"EPA public-water coverage is negative for {county_fips}")
            if raw_public_coverage > 1.005:
                raise HouseHunterError(
                    f"EPA public-water coverage exceeds its reconciliation tolerance for "
                    f"{county_fips}"
                )
            public_coverage = min(1.0, raw_public_coverage)
            overlap = _finite(proxy_row["overlap_duplicate_share_proxy"])
            overlap_quality = "max_intersection_proxy"
        if county_fips.startswith("09"):
            status = "missing_legacy_geography"
        elif county_fips in ambiguous_counties:
            status = "ambiguous_served_counties"
        elif coverage is None:
            status = "missing_no_allocatable_systems"
        elif coverage < 0.90:
            status = "below_allocation_coverage"
        else:
            status = "complete"
        provenances = county_provenance[county_fips]
        boundary = (
            next(iter(provenances)) if len(provenances) == 1 else "mixed" if provenances else None
        )
        rows.append(
            {
                "county_fips": county_fips,
                "water_violation_share": violation_share,
                "public_water_coverage": public_coverage,
                "public_water_coverage_kind": "max_intersection_proxy",
                "water_allocation_coverage": coverage,
                "water_boundary_provenance": boundary,
                "water_overlap_duplicate_share_proxy": overlap,
                "water_overlap_quality_status": overlap_quality,
                "u_water": 1.0 - violation_share if status == "complete" else None,
                "water_status": status,
            }
        )
    return _unique(pl.DataFrame(rows), "EPA drinking water")


def _station_rows(archive: tarfile.TarFile) -> Iterable[dict[str, str]]:
    for member in archive:
        if not member.isfile() or not member.name.endswith(".csv"):
            continue
        handle = archive.extractfile(member)
        if handle is None:
            raise HouseHunterError(f"NOAA normals member cannot be read: {member.name}")
        with io.TextIOWrapper(handle, encoding="utf-8-sig", newline="") as text:
            yield from csv.DictReader(text)


def _climate(data_root: Path) -> pl.DataFrame:
    annual_path = _artifact(
        data_root,
        "noaa_normals_1991_2020",
        "us-climate-normals_1991-2020_v1.0.1_annualseasonal_multivariate_by-station_c20230404.tar.gz",
    )
    monthly_path = _artifact(
        data_root,
        "noaa_normals_1991_2020",
        "us-climate-normals_1991-2020_v1.0.1_monthly_multivariate_by-station_c20230404.tar.gz",
    )
    annual: dict[str, dict[str, float]] = {}
    with tarfile.open(annual_path, "r:gz") as archive:
        for row in _station_rows(archive):
            station = row.get("STATION", "").strip()
            latitude = _number(row.get("LATITUDE"))
            longitude = _number(row.get("LONGITUDE"))
            heat = _number(row.get("ANN-TMAX-AVGNDS-GRTH090"))
            cold = _number(row.get("ANN-TMIN-AVGNDS-LSTH032"))
            if not station or None in {latitude, longitude, heat, cold}:
                continue
            value = {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "extreme_heat_days": float(heat),
                "extreme_cold_days": float(cold),
            }
            if station in annual and annual[station] != value:
                raise HouseHunterError(f"NOAA annual normals conflict for station {station}")
            annual[station] = value

    monthly: dict[str, dict[str, float]] = defaultdict(dict)
    with tarfile.open(monthly_path, "r:gz") as archive:
        for row in _station_rows(archive):
            station = row.get("STATION", "").strip()
            month = row.get("month", "").strip().zfill(2)
            if station not in annual or month not in {"01", "07"}:
                continue
            normal = _number(row.get("MLY-TAVG-NORMAL"))
            if normal is None:
                continue
            key = "jan_avg_temp_f" if month == "01" else "jul_avg_temp_f"
            if key in monthly[station] and monthly[station][key] != normal:
                raise HouseHunterError(f"NOAA monthly normals conflict for station {station}")
            monthly[station][key] = float(normal)

    qualifying = [
        {"station": station, **values, **monthly[station]}
        for station, values in sorted(annual.items())
        if {"jan_avg_temp_f", "jul_avg_temp_f"} <= set(monthly[station])
    ]
    if not qualifying:
        raise HouseHunterError("NOAA normals contain no stations with all four required measures")

    from pyogrio.raw import read as read_vector
    from shapely import STRtree, from_wkb, points

    tiger = _artifact(data_root, "census_tiger_county_2025", "tl_2025_us_county.zip")
    _, _, geometries, fields = read_vector(f"zip://{tiger}", columns=["GEOID", "STATEFP"])
    county_fips = [str(value) for value in fields[0]]
    state_fips = [str(value) for value in fields[1]]
    keep = [index for index, state in enumerate(state_fips) if state in IN_SCOPE_STATE_FIPS]
    kept_fips = [county_fips[index] for index in keep]
    county_geometries = from_wkb([geometries[index] for index in keep])
    if len(kept_fips) != len(set(kept_fips)):
        raise HouseHunterError("TIGER county geometry duplicates current county FIPS")
    tree = STRtree(county_geometries)
    station_points = points(
        [row["longitude"] for row in qualifying],
        [row["latitude"] for row in qualifying],
    )
    matches = tree.query(station_points, predicate="within")
    by_station: dict[int, list[int]] = defaultdict(list)
    for station_index, county_index in zip(matches[0], matches[1], strict=True):
        by_station[int(station_index)].append(int(county_index))
    if any(len(indices) > 1 for indices in by_station.values()):
        raise HouseHunterError("A NOAA station falls within multiple current counties")

    county_stations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for station_index, row in enumerate(qualifying):
        matches_for_station = by_station.get(station_index, [])
        if matches_for_station:
            county_stations[kept_fips[matches_for_station[0]]].append(row)

    rows: list[dict[str, Any]] = []
    for county in _population(data_root)["county_fips"].to_list():
        stations = county_stations[county]
        if stations:
            rows.append(
                {
                    "county_fips": county,
                    "jan_avg_temp_f": sum(row["jan_avg_temp_f"] for row in stations)
                    / len(stations),
                    "jul_avg_temp_f": sum(row["jul_avg_temp_f"] for row in stations)
                    / len(stations),
                    "extreme_heat_days": sum(row["extreme_heat_days"] for row in stations)
                    / len(stations),
                    "extreme_cold_days": sum(row["extreme_cold_days"] for row in stations)
                    / len(stations),
                    "climate_station_count": len(stations),
                    "climate_coverage_status": "complete_in_county_station_mean",
                }
            )
        else:
            rows.append(
                {
                    "county_fips": county,
                    "jan_avg_temp_f": None,
                    "jul_avg_temp_f": None,
                    "extreme_heat_days": None,
                    "extreme_cold_days": None,
                    "climate_station_count": 0,
                    "climate_coverage_status": "missing_no_qualifying_in_county_station",
                }
            )
    return _unique(pl.DataFrame(rows), "NOAA climate normals")


def _mountain() -> pl.DataFrame:
    frame = pl.read_parquet(Path(__file__).with_name("assets") / "mountain" / "counties.parquet")
    return _unique(
        frame.select(
            pl.col("place_id").alias("county_fips"),
            pl.col("mountain_magnitude").cast(pl.Float64),
            pl.col("mountain_coverage_status").alias("mountain_status"),
        ),
        "Mountain Magnitude",
    )


def _homeschool() -> pl.DataFrame:
    policy = load_homeschool_policy()
    state_fips = {state: code for code, state in STATE_BY_FIPS.items()}
    rows = []
    for state, rubric in sorted(policy["jurisdictions"].items()):
        detail = {**rubric, "official_source": policy["official_sources"][state]}
        rows.append(
            {
                "state_fips": state_fips[state],
                "homeschool_utility": float(rubric["utility"]),
                "homeschool_rubric_json": canonical_json(detail).decode(),
                "family_status": "approximate_project_authored",
            }
        )
    return pl.DataFrame(rows)


def _health_rates(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        (pl.col("provider_primary_care_count") / pl.col("population") * 100_000).alias(
            "provider_primary_care"
        ),
        (pl.col("provider_mental_health_count") / pl.col("population") * 100_000).alias(
            "provider_mental_health"
        ),
        (pl.col("provider_dental_count") / pl.col("population") * 100_000).alias("provider_dental"),
    ).drop(
        "provider_primary_care_count",
        "provider_mental_health_count",
        "provider_dental_count",
    )


def _calibrate(frame: pl.DataFrame) -> pl.DataFrame:
    for raw, utility, invert in (
        ("provider_primary_care", "u_primary_care", False),
        ("provider_mental_health", "u_mental_health", False),
        ("provider_dental", "u_dental", False),
        ("rpp_index", "u_rpp", True),
        ("property_tax_rate", "u_property_tax", True),
        ("employment_growth", "u_employment_growth", False),
        ("average_weekly_wage", "u_average_weekly_wage", False),
        ("commute_under_30_share", "u_commute_under_30", False),
        ("mountain_magnitude", "u_mountain", False),
    ):
        frame = _ecdf_column(frame, raw, utility, invert=invert)
    all_provider_components = pl.all_horizontal(
        pl.col("u_primary_care").is_not_null(),
        pl.col("u_mental_health").is_not_null(),
        pl.col("u_dental").is_not_null(),
    )
    return frame.with_columns(
        pl.when(all_provider_components)
        .then((pl.col("u_primary_care") + pl.col("u_mental_health") + pl.col("u_dental")) / 3.0)
        .otherwise(None)
        .alias("u_healthcare"),
        (
            0.50 * pl.col("u_employment_growth")
            + 0.25 * pl.col("u_average_weekly_wage")
            + 0.25 * pl.col("u_commute_under_30")
        ).alias("u_employment"),
        pl.col("broadband_100_20").alias("u_broadband"),
        pl.col("homeschool_utility").alias("u_family"),
        pl.col("u_mountain").alias("u_lifestyle"),
        pl.when(all_provider_components)
        .then(pl.lit("complete"))
        .otherwise(pl.lit("missing_component"))
        .alias("healthcare_status"),
    )


def _finish(frame: pl.DataFrame) -> pl.DataFrame:
    pillars = [
        (
            "u_safety",
            ("u_hazard", "u_crime", "u_water"),
            tuple(PILLAR_INTERNALS["safety"].values()),
        ),
        (
            "u_health",
            ("u_healthcare", "u_community_context"),
            tuple(PILLAR_INTERNALS["health"].values()),
        ),
        (
            "u_opportunity",
            ("u_employment", "u_broadband"),
            tuple(PILLAR_INTERNALS["opportunity"].values()),
        ),
    ]
    expressions = []
    for output, columns, weights in pillars:
        present = pl.all_horizontal(*[pl.col(column).is_not_null() for column in columns])
        score = sum(
            (pl.col(column) * weight for column, weight in zip(columns, weights, strict=True)),
            start=pl.lit(0.0),
        )
        expressions.append(pl.when(present).then(score).otherwise(None).alias(output))
    frame = frame.with_columns(*expressions)
    frame = frame.with_columns(
        pl.lit("HRSA AHRF").alias("provider_primary_care_source"),
        pl.lit("CHR&R NPPES").alias("provider_mental_health_source"),
        pl.lit("HRSA AHRF").alias("provider_dental_source"),
        pl.lit("2023").alias("provider_primary_care_vintage"),
        pl.lit("2025").alias("provider_mental_health_vintage"),
        pl.lit("2023").alias("provider_dental_vintage"),
        pl.lit(json.dumps(ALL_CITATION_IDS, separators=(",", ":"))).alias("citation_ids_json"),
        pl.lit(json.dumps(METHODOLOGY_LIMITATIONS, separators=(",", ":"))).alias(
            "limitations_json"
        ),
    )
    for column in COUNTY_COLUMNS:
        if column not in frame.columns:
            dtype = (
                pl.String
                if column.endswith(("_status", "_source", "_vintage", "_json", "_kind"))
                or column
                in {
                    "state",
                    "rpp_geography_type",
                    "water_boundary_provenance",
                    "broadband_denominator_label",
                }
                else pl.Int64
                if column in {"population", "climate_station_count"}
                else pl.Float64
            )
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
    status_columns = [column for column in COUNTY_COLUMNS if column.endswith("_status")]
    frame = frame.with_columns(
        *[
            pl.col(column).fill_null("missing").alias(column)
            for column in status_columns
            if column != "coverage_status"
        ],
        pl.when(
            pl.all_horizontal(
                *[
                    pl.col(column).is_not_null()
                    for column in (
                        "u_safety",
                        "u_health",
                        "u_opportunity",
                        "u_lifestyle",
                        "u_family",
                    )
                ]
            )
        )
        .then(pl.lit("complete_public_core"))
        .otherwise(pl.lit("partial_public_core"))
        .alias("coverage_status"),
    )
    return frame.select(COUNTY_COLUMNS).sort("county_fips")


def build_ranking_counties(
    *,
    data_root: Path,
    snapshot: Path,
    source_lock_path: Path | None = None,
    water: pl.DataFrame | None = None,
    climate: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Build the compact public county table from the locked maintainer inputs."""
    lock = load_source_lock(source_lock_path)
    _validate_locked_raw_artifacts(data_root, lock)
    _validate_packaged_inputs(lock)
    snapshot_identity = _validate_snapshot_input(snapshot, lock)
    fbi_manifest = _validate_fbi_summary_inputs(data_root, source_lock_path)
    frame = _normalized_stage(
        data_root,
        "population",
        contract={
            "stage": "population",
            "sources_sha256": _sources_contract(lock, "census_pep_county_2025"),
        },
        build=lambda: _population(data_root),
    )
    if (
        frame.height != lock["county_universe"]["row_count"]
        or sha256_bytes(canonical_json(frame["county_fips"].to_list()))
        != lock["county_universe"]["sorted_fips_sha256"]
    ):
        raise HouseHunterError("Ranking population source differs from the locked county universe")
    stages: tuple[tuple[str, str, Mapping[str, Any], Callable[[], pl.DataFrame]], ...] = (
        (
            "runtime_context",
            "runtime county context",
            {
                "stage": "runtime_context",
                "snapshot": snapshot_identity,
                "sources_sha256": _sources_contract(
                    lock, "fema_residential_hazard", "chrr_community_context_2025"
                ),
            },
            lambda: _runtime_context(snapshot),
        ),
        (
            "crime",
            "FBI crime",
            {
                "stage": "crime",
                "schema": CRIME_STAGE_SCHEMA,
                "sources_sha256": _sources_contract(lock, "fbi_ucr_agency_2023_2025"),
                "fbi_responses_sha256": fbi_manifest["responses_sha256"],
            },
            lambda: _crime(data_root, fbi_manifest),
        ),
        (
            "provider_supply",
            "provider supply",
            {
                "stage": "provider_supply",
                "sources_sha256": _sources_contract(
                    lock, "hrsa_ahrf_2024_2025", "chrr_nppes_mental_health_2025"
                ),
            },
            lambda: _health(data_root),
        ),
        (
            "rpp",
            "BEA RPP",
            {
                "stage": "rpp",
                "sources_sha256": _sources_contract(
                    lock,
                    "census_pep_county_2025",
                    "bea_marpp_2024",
                    "bea_sarpp_2024",
                    "omb_county_cbsa_2023",
                ),
            },
            lambda: _rpp(data_root),
        ),
        (
            "property_tax",
            "ACS property tax",
            {
                "stage": "property_tax",
                "sources_sha256": _sources_contract(lock, "acs_property_tax_2024"),
            },
            lambda: _property_tax(data_root),
        ),
        (
            "employment",
            "employment opportunity",
            {
                "stage": "employment",
                "schema": EMPLOYMENT_STAGE_SCHEMA,
                "sources_sha256": _sources_contract(
                    lock, "bls_qcew_2024", "bls_qcew_2025", "acs_commute_2024"
                ),
            },
            lambda: _employment(data_root),
        ),
        (
            "broadband",
            "FCC broadband",
            {
                "stage": "broadband",
                "sources_sha256": _sources_contract(lock, "fcc_fixed_summary_2025_12"),
            },
            lambda: _broadband(data_root),
        ),
        (
            "mountain",
            "Mountain Magnitude",
            {
                "stage": "mountain",
                "sources_sha256": _sources_contract(lock, "househunter_mountain_v2"),
            },
            _mountain,
        ),
    )
    for name, label, contract, build in stages:
        frame = _join(
            frame,
            _normalized_stage(data_root, name, contract=contract, build=build),
            label,
        )
    homeschool = _homeschool()
    frame = (
        frame.with_columns(pl.col("county_fips").str.slice(0, 2).alias("state_fips"))
        .join(homeschool, on="state_fips", how="left", validate="m:1")
        .drop("state_fips")
    )
    water_frame = (
        water
        if water is not None
        else _normalized_stage(
            data_root,
            "water",
            contract={
                "stage": "water",
                "stage_schema": 3,
                "sources_sha256": _sources_contract(
                    lock,
                    "census_pep_county_2025",
                    "epa_sdwis_2026_q2",
                    "epa_cws_service_areas_v2_1",
                ),
            },
            build=lambda: _water(data_root, source_lock_path),
        )
    )
    climate_frame = (
        climate
        if climate is not None
        else _normalized_stage(
            data_root,
            "climate",
            contract={
                "stage": "climate",
                "sources_sha256": _sources_contract(
                    lock,
                    "census_pep_county_2025",
                    "noaa_normals_1991_2020",
                    "census_tiger_county_2025",
                ),
            },
            build=lambda: _climate(data_root),
        )
    )
    frame = _join(frame, water_frame, "EPA drinking water")
    frame = _join(frame, climate_frame, "NOAA climate")
    frame = _health_rates(frame)
    frame = _calibrate(frame)
    return _finish(frame)
