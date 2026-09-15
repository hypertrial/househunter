from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import duckdb

from .build import BUILD_SCHEMA_VERSION, SNAPSHOT_FILES, snapshot_artifacts_are_valid
from .config import RuntimePaths
from .contracts import COUNTY_METHODOLOGY_NOTICE, METHODOLOGY_NOTICE
from .dimensions import SUMMARY_DIMENSION_COLUMNS
from .errors import AmbiguousPlaceError, BuildNotFoundError, HouseHunterError
from .hazards import HAZARDS, hazard_percentiles_from_record, hazard_select_sql

SUMMARY_COLUMNS = """
place_id, name, state, place_type, population_2020, housing_units_2020,
res_hazard_npctl, res_hazard_spread, res_hazard_spectral, res_hazard_tail,
res_hazard_power4, property_loss_npctl, res_hazard_data_quality,
res_hazard_available_count, res_hazard_coverage_ratio, alr_npctl, alr_valb,
fema_vintage, census_vintage, county_fips, county_name,
community_conditions_group, community_conditions_geography, chrr_release_year,
mountain_magnitude, mountain_magnitude_version, mountain_pipeline_version,
relief_5km_m, relief_10km_m, relief_20km_m, relief_40km_m, relief_20km_pct,
rugged_fraction_20km, rugged_pct, public_mountain_access_raw, public_mountain_access_pct,
open_mountain_km2_5, open_mountain_km2_15, open_mountain_km2_30,
restricted_mountain_km2_30, closed_mountain_km2_30, unknown_mountain_km2_30,
nearest_mountain_trail_km, mountain_trail_km_10, mountain_trail_km_25,
trail_access_raw, trail_access_pct, mountain_population_coverage, mountain_coverage_status
"""
SUMMARY_KEYS = [
    "place_id",
    "name",
    "state",
    "place_type",
    "population_2020",
    "housing_units_2020",
    "res_hazard_npctl",
    "res_hazard_spread",
    "res_hazard_spectral",
    "res_hazard_tail",
    "res_hazard_power4",
    "property_loss_npctl",
    "res_hazard_data_quality",
    "res_hazard_available_count",
    "res_hazard_coverage_ratio",
    "alr_npctl",
    "alr_valb",
    "fema_vintage",
    "census_vintage",
    "county_fips",
    "county_name",
    "community_conditions_group",
    "community_conditions_geography",
    "chrr_release_year",
    "mountain_magnitude",
    "mountain_magnitude_version",
    "mountain_pipeline_version",
    "relief_5km_m",
    "relief_10km_m",
    "relief_20km_m",
    "relief_40km_m",
    "relief_20km_pct",
    "rugged_fraction_20km",
    "rugged_pct",
    "public_mountain_access_raw",
    "public_mountain_access_pct",
    "open_mountain_km2_5",
    "open_mountain_km2_15",
    "open_mountain_km2_30",
    "restricted_mountain_km2_30",
    "closed_mountain_km2_30",
    "unknown_mountain_km2_30",
    "nearest_mountain_trail_km",
    "mountain_trail_km_10",
    "mountain_trail_km_25",
    "trail_access_raw",
    "trail_access_pct",
    "mountain_population_coverage",
    "mountain_coverage_status",
    *SUMMARY_DIMENSION_COLUMNS,
]
SUMMARY_COLUMNS = ", ".join(SUMMARY_KEYS)
BUILD_ID_PATTERN = re.compile(r"(?:national|[a-z]{2})-[0-9a-f]{16}")

_EXPOSURE_EXPORT_COLUMNS = (
    ("res_hazard_npctl", "RES_HAZARD_NPCTL"),
    ("res_hazard_spread", "RES_HAZARD_SPREAD"),
    ("res_hazard_spectral", "RES_HAZARD_SPECTRAL"),
    ("res_hazard_tail", "RES_HAZARD_TAIL"),
    ("res_hazard_power4", "RES_HAZARD_POWER4"),
    ("property_loss_npctl", "PROPERTY_LOSS_NPCTL"),
    ("res_hazard_data_quality", "RES_HAZARD_DATA_QUALITY"),
    ("res_hazard_available_count", "RES_HAZARD_AVAILABLE_COUNT"),
    ("res_hazard_coverage_ratio", "RES_HAZARD_COVERAGE_RATIO"),
    ("alr_npctl", "ALR_NPCTL"),
    ("alr_valb", "ALR_VALB"),
    *(
        column
        for hazard in HAZARDS
        for column in (
            (hazard.raw_column, hazard.alrb_field),
            (hazard.rating_column, hazard.rating_field),
            (hazard.percentile_column, f"{hazard.code}_ALRB_NPCTL"),
        )
    ),
)


def _export_select_sql(table: str) -> str:
    excluded = ", ".join(source for source, _ in _EXPOSURE_EXPORT_COLUMNS)
    aliases = ", ".join(
        f'{source} AS "{destination}"' for source, destination in _EXPOSURE_EXPORT_COLUMNS
    )
    return f"SELECT * EXCLUDE ({excluded}), {aliases} FROM {table} ORDER BY place_id"


def _validated_build(build: Path, expected_build_id: str) -> tuple[Path, dict[str, Any]]:
    required = [build / filename for filename in SNAPSHOT_FILES]
    if (
        build.is_symlink()
        or not build.is_dir()
        or any(not path.is_file() or path.is_symlink() for path in required)
    ):
        raise BuildNotFoundError(f"Published build is incomplete: {build}")
    try:
        metadata = json.loads((build / "build.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildNotFoundError(f"Build metadata is invalid: {exc}") from exc
    if (
        metadata.get("schema_version") != BUILD_SCHEMA_VERSION
        or metadata.get("build_id") != expected_build_id
        or build.name != expected_build_id
    ):
        raise BuildNotFoundError("Build directory and metadata disagree")
    if not snapshot_artifacts_are_valid(build):
        raise BuildNotFoundError("Published build artifacts do not match their canonical data")
    return build, metadata


def current_build(paths: RuntimePaths) -> tuple[Path, dict[str, Any]]:
    if not paths.current.is_file():
        raise BuildNotFoundError("No published build; run `househunter build`")
    try:
        pointer = json.loads(paths.current.read_text())
        build = Path(pointer["path"]).resolve()
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise BuildNotFoundError(f"Current build pointer is invalid: {exc}") from exc
    if not build.is_relative_to(paths.builds.resolve()):
        raise BuildNotFoundError("Current build pointer escapes the build directory")
    pointer_schema = pointer.get("schema_version")
    if pointer_schema != BUILD_SCHEMA_VERSION:
        raise BuildNotFoundError(
            f"Snapshot schema {pointer_schema!r} is unsupported; rebuild with "
            f"`househunter build` to create schema {BUILD_SCHEMA_VERSION}"
        )
    return _validated_build(build, str(pointer.get("build_id", "")))


def retained_build(paths: RuntimePaths, build_id: str) -> tuple[Path, dict[str, Any]]:
    """Open a validated immutable build named by a previously advertised payload URL."""
    if paths.builds.is_symlink() or BUILD_ID_PATTERN.fullmatch(build_id) is None:
        raise BuildNotFoundError("Retained build identity is invalid")
    builds_root = paths.builds.resolve()
    build = (paths.builds / build_id).resolve()
    if not build.is_relative_to(builds_root):
        raise BuildNotFoundError("Retained build escapes the build directory")
    return _validated_build(build, build_id)


class Store:
    def __init__(self, paths: RuntimePaths, *, build_id: str | None = None) -> None:
        self.paths = paths
        self.build, self.metadata = (
            retained_build(paths, build_id) if build_id is not None else current_build(paths)
        )
        self.connection = duckdb.connect(str(self.build / "househunter.duckdb"), read_only=True)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _list_rows(
        self,
        table: str,
        *,
        search: str | None = None,
        state: str | None = None,
        county: str | None = None,
        min_population: int | None = None,
        max_population: int | None = None,
        min_res_hazard: float | None = None,
        max_res_hazard: float | None = None,
        community_conditions_group: int | None = None,
        max_community_conditions_group: int | None = None,
        mountain_magnitude_min: float | None = None,
        mountain_magnitude_max: float | None = None,
        cost_of_living_index_min: float | None = None,
        cost_of_living_index_max: float | None = None,
        home_sqft_for_1m_min: float | None = None,
        home_sqft_for_1m_max: float | None = None,
        housing_built_2000_plus_pct_min: float | None = None,
        housing_built_2000_plus_pct_max: float | None = None,
        include_unranked: bool = False,
        sort: str = "res_hazard_npctl",
        direction: str = "asc",
        offset: int = 0,
        limit: int = 100,
        search_county_name: bool = False,
    ) -> dict[str, Any]:
        sort_columns = {
            "res_hazard_npctl": "res_hazard_npctl",
            "name": "name",
            "state": "state",
            "population": "population_2020",
            "community_conditions_group": "community_conditions_group",
            "mountain_magnitude": "mountain_magnitude",
            "cost_of_living_index": "cost_of_living_index",
            "home_sqft_for_1m": "home_sqft_for_1m",
            "home_buying_power_percentile": "home_buying_power_percentile",
            "housing_built_2000_plus_pct": "housing_built_2000_plus_pct",
        }
        if sort not in sort_columns:
            raise HouseHunterError(f"Unsupported sort column: {sort}")
        if direction not in {"asc", "desc"}:
            raise HouseHunterError("Sort direction must be asc or desc")
        if county is not None:
            county = county.strip()
            if county == "":
                county = None
        if county is not None and (len(county) != 5 or not county.isdigit()):
            raise HouseHunterError("County filter must be a 5-digit FIPS code")
        if community_conditions_group is not None and not 1 <= community_conditions_group <= 10:
            raise HouseHunterError("Community Conditions group must be between 1 and 10")
        if max_community_conditions_group is not None and not (
            1 <= max_community_conditions_group <= 10
        ):
            raise HouseHunterError("Maximum Community Conditions group must be between 1 and 10")
        magnitude_bounds = (mountain_magnitude_min, mountain_magnitude_max)
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in magnitude_bounds
        ):
            raise HouseHunterError("Mountain Magnitude bounds must be finite and nonnegative")
        if (
            mountain_magnitude_min is not None
            and mountain_magnitude_max is not None
            and mountain_magnitude_min > mountain_magnitude_max
        ):
            raise HouseHunterError("Mountain Magnitude minimum cannot exceed maximum")
        metric_bounds = (
            (
                "Residential Hazard Exposure",
                min_res_hazard,
                max_res_hazard,
                0.0,
                100.0,
            ),
            (
                "Cost of Living",
                cost_of_living_index_min,
                cost_of_living_index_max,
                0.0,
                None,
            ),
            (
                "Home square feet",
                home_sqft_for_1m_min,
                home_sqft_for_1m_max,
                0.0,
                None,
            ),
            (
                "Built-2000+ share",
                housing_built_2000_plus_pct_min,
                housing_built_2000_plus_pct_max,
                0.0,
                100.0,
            ),
        )
        for label, minimum, maximum, floor, ceiling in metric_bounds:
            for value in (minimum, maximum):
                if value is not None and (
                    not math.isfinite(value)
                    or value < floor
                    or (ceiling is not None and value > ceiling)
                ):
                    range_suffix = f" and at most {ceiling:g}" if ceiling is not None else ""
                    raise HouseHunterError(
                        f"{label} bounds must be finite, nonnegative{range_suffix}"
                    )
            if minimum is not None and maximum is not None and minimum > maximum:
                raise HouseHunterError(f"{label} minimum cannot exceed maximum")
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        clauses: list[str] = []
        parameters: list[Any] = []
        if not include_unranked:
            clauses.append(f"{sort_columns[sort]} IS NOT NULL")
        if search:
            if search_county_name:
                clauses.append("(name ILIKE ? OR place_id = ? OR county_name ILIKE ?)")
                parameters.extend([f"%{search}%", search, f"%{search}%"])
            else:
                clauses.append("(name ILIKE ? OR place_id = ?)")
                parameters.extend([f"%{search}%", search])
        if state:
            clauses.append("state = ?")
            parameters.append(state.upper())
        if county is not None:
            clauses.append("county_fips = ?")
            parameters.append(county)
        if community_conditions_group is not None:
            clauses.append("community_conditions_group = ?")
            parameters.append(community_conditions_group)
        if max_community_conditions_group is not None:
            clauses.append("community_conditions_group <= ?")
            parameters.append(max_community_conditions_group)
        for column, operator, value in (
            ("population_2020", ">=", min_population),
            ("population_2020", "<=", max_population),
            ("res_hazard_npctl", ">=", min_res_hazard),
            ("res_hazard_npctl", "<=", max_res_hazard),
            ("mountain_magnitude", ">=", mountain_magnitude_min),
            ("mountain_magnitude", "<=", mountain_magnitude_max),
            ("cost_of_living_index", ">=", cost_of_living_index_min),
            ("cost_of_living_index", "<=", cost_of_living_index_max),
            ("home_sqft_for_1m", ">=", home_sqft_for_1m_min),
            ("home_sqft_for_1m", "<=", home_sqft_for_1m_max),
            (
                "housing_built_2000_plus_pct",
                ">=",
                housing_built_2000_plus_pct_min,
            ),
            (
                "housing_built_2000_plus_pct",
                "<=",
                housing_built_2000_plus_pct_max,
            ),
        ):
            if value is not None:
                clauses.append(f"{column} {operator} ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.connection.execute(
            f"SELECT count(*) FROM {table}{where}", parameters
        ).fetchone()[0]
        null_order = "NULLS LAST"
        tie_breaker = "place_id ASC"
        query = (
            f"SELECT {SUMMARY_COLUMNS} FROM {table}{where} "
            f"ORDER BY {sort_columns[sort]} {direction.upper()} {null_order}, "
            f"{tie_breaker} LIMIT ? OFFSET ?"
        )
        cursor = self.connection.execute(query, [*parameters, limit, offset])
        columns = [item[0] for item in cursor.description]
        items = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def list_places(
        self,
        *,
        search: str | None = None,
        state: str | None = None,
        county: str | None = None,
        min_population: int | None = None,
        max_population: int | None = None,
        min_res_hazard: float | None = None,
        max_res_hazard: float | None = None,
        community_conditions_group: int | None = None,
        max_community_conditions_group: int | None = None,
        mountain_magnitude_min: float | None = None,
        mountain_magnitude_max: float | None = None,
        cost_of_living_index_min: float | None = None,
        cost_of_living_index_max: float | None = None,
        home_sqft_for_1m_min: float | None = None,
        home_sqft_for_1m_max: float | None = None,
        housing_built_2000_plus_pct_min: float | None = None,
        housing_built_2000_plus_pct_max: float | None = None,
        include_unranked: bool = False,
        sort: str = "res_hazard_npctl",
        direction: str = "asc",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._list_rows(
            "places",
            search=search,
            state=state,
            county=county,
            min_population=min_population,
            max_population=max_population,
            min_res_hazard=min_res_hazard,
            max_res_hazard=max_res_hazard,
            community_conditions_group=community_conditions_group,
            max_community_conditions_group=max_community_conditions_group,
            mountain_magnitude_min=mountain_magnitude_min,
            mountain_magnitude_max=mountain_magnitude_max,
            cost_of_living_index_min=cost_of_living_index_min,
            cost_of_living_index_max=cost_of_living_index_max,
            home_sqft_for_1m_min=home_sqft_for_1m_min,
            home_sqft_for_1m_max=home_sqft_for_1m_max,
            housing_built_2000_plus_pct_min=housing_built_2000_plus_pct_min,
            housing_built_2000_plus_pct_max=housing_built_2000_plus_pct_max,
            include_unranked=include_unranked,
            sort=sort,
            direction=direction,
            offset=offset,
            limit=limit,
            search_county_name=True,
        )

    def list_county_candidates(self) -> list[dict[str, Any]]:
        available = (
            isinstance(self.metadata.get("ranking"), dict)
            and self.metadata["ranking"].get("available")
        )
        if not available:
            return []
        cursor = self.connection.execute(
            "SELECT * FROM ranking_counties ORDER BY place_id"
        )
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def list_counties(
        self,
        *,
        search: str | None = None,
        state: str | None = None,
        min_res_hazard: float | None = None,
        max_res_hazard: float | None = None,
        community_conditions_group: int | None = None,
        max_community_conditions_group: int | None = None,
        mountain_magnitude_min: float | None = None,
        mountain_magnitude_max: float | None = None,
        cost_of_living_index_min: float | None = None,
        cost_of_living_index_max: float | None = None,
        home_sqft_for_1m_min: float | None = None,
        home_sqft_for_1m_max: float | None = None,
        housing_built_2000_plus_pct_min: float | None = None,
        housing_built_2000_plus_pct_max: float | None = None,
        include_unranked: bool = False,
        sort: str = "res_hazard_npctl",
        direction: str = "asc",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._list_rows(
            "counties",
            search=search,
            state=state,
            min_res_hazard=min_res_hazard,
            max_res_hazard=max_res_hazard,
            community_conditions_group=community_conditions_group,
            max_community_conditions_group=max_community_conditions_group,
            mountain_magnitude_min=mountain_magnitude_min,
            mountain_magnitude_max=mountain_magnitude_max,
            cost_of_living_index_min=cost_of_living_index_min,
            cost_of_living_index_max=cost_of_living_index_max,
            home_sqft_for_1m_min=home_sqft_for_1m_min,
            home_sqft_for_1m_max=home_sqft_for_1m_max,
            housing_built_2000_plus_pct_min=housing_built_2000_plus_pct_min,
            housing_built_2000_plus_pct_max=housing_built_2000_plus_pct_max,
            include_unranked=include_unranked,
            sort=sort,
            direction=direction,
            offset=offset,
            limit=limit,
        )

    def map_scores(self, level: str) -> dict[str, Any]:
        return self._map_score_payload(
            level,
            (
                "place_id",
                "res_hazard_npctl",
                "community_conditions_group",
                "mountain_magnitude",
                "cost_of_living_index",
                "home_buying_power_percentile",
                "home_sqft_for_1m",
                "housing_built_2000_plus_pct",
            ),
        )

    def map_scores_core(self, level: str) -> dict[str, Any]:
        return self._map_score_payload(
            level,
            (
                "place_id",
                "res_hazard_npctl",
                "community_conditions_group",
                "mountain_magnitude",
            ),
        )

    def map_scores_addon(self, level: str, kind: str) -> dict[str, Any]:
        columns = {
            "cost-of-living": ("place_id", "cost_of_living_index"),
            "home-costs": (
                "place_id",
                "home_buying_power_percentile",
                "home_sqft_for_1m",
                "housing_built_2000_plus_pct",
            ),
        }.get(kind)
        if columns is None:
            raise HouseHunterError("Map score add-on must be cost-of-living or home-costs")
        payload = self._map_score_payload(level, columns, schema_version=1)
        payload["kind"] = kind
        return payload

    def _map_score_payload(
        self,
        level: str,
        map_columns: tuple[str, ...],
        *,
        schema_version: int = 5,
    ) -> dict[str, Any]:
        table = {"tract": "places", "county": "counties"}.get(level)
        if table is None:
            raise HouseHunterError("Map level must be tract or county")
        rows = self.connection.execute(
            f"SELECT {', '.join(map_columns)} FROM {table} ORDER BY place_id"
        ).fetchall()
        columns: dict[str, list[Any]] = {
            column: [] for column in map_columns
        }
        for row in rows:
            for column, value in zip(map_columns, row, strict=True):
                columns[column].append(value)
        return {
            "schema_version": schema_version,
            "build_id": self.metadata["build_id"],
            "level": level,
            "scope": self.metadata["scope"],
            "columns": columns,
        }

    def resolve_place(self, query: str) -> str:
        if len(query) == 11 and query.isdigit():
            exists = self.connection.execute(
                "SELECT 1 FROM places WHERE place_id = ?", [query]
            ).fetchone()
            if not exists:
                raise HouseHunterError(f"Tract not found: {query}")
            return query
        name, separator, state = query.rpartition(",")
        if separator:
            rows = self.connection.execute(
                "SELECT place_id, name, state FROM places "
                "WHERE lower(name) = lower(?) AND state = ? ORDER BY place_id",
                [name.strip(), state.strip().upper()],
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT place_id, name, state FROM places "
                "WHERE lower(name) = lower(?) ORDER BY place_id",
                [query.strip()],
            ).fetchall()
        if not rows:
            raise HouseHunterError(f"Tract not found: {query}")
        if len(rows) > 1:
            raise AmbiguousPlaceError(
                query,
                [{"place_id": row[0], "name": row[1], "state": row[2]} for row in rows],
            )
        return rows[0][0]

    def resolve_county(self, query: str) -> str:
        if len(query) != 5 or not query.isdigit():
            raise HouseHunterError(f"County not found: {query}")
        exists = self.connection.execute(
            "SELECT 1 FROM counties WHERE place_id = ?", [query]
        ).fetchone()
        if not exists:
            raise HouseHunterError(f"County not found: {query}")
        return query

    def place_detail(self, place_id: str) -> dict[str, Any]:
        return self._geography_detail("places", place_id, METHODOLOGY_NOTICE, "Tract")

    def county_detail(self, county_id: str) -> dict[str, Any]:
        return self._geography_detail("counties", county_id, COUNTY_METHODOLOGY_NOTICE, "County")

    def _geography_detail(
        self, table: str, place_id: str, notice: str, label: str
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            f"SELECT {SUMMARY_COLUMNS}, {hazard_select_sql()} "
            f"FROM {table} WHERE place_id = ?",
            [place_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise HouseHunterError(f"{label} not found: {place_id}")
        columns = [item[0] for item in cursor.description]
        record = dict(zip(columns, row, strict=True))
        summary = {key: record[key] for key in SUMMARY_KEYS}
        member_tract_count = None
        if table == "counties":
            member_tract_count = self.connection.execute(
                "SELECT count(*) FROM places WHERE county_fips = ?",
                [place_id],
            ).fetchone()[0]
        return {
            "summary": summary,
            "methodology_notice": notice,
            "hazard_percentiles": hazard_percentiles_from_record(record),
            "member_tract_count": member_tract_count,
            "source_notices": list(self.metadata.get("detail_notices", [])),
        }

    def export(self, format: str, output: Path, *, table: str = "places") -> Path:
        if table not in {"places", "counties"}:
            raise HouseHunterError("Export table must be places or counties")
        destination = output.expanduser().resolve()
        protected_directories = [
            self.paths.cache.resolve(),
            self.paths.raw.resolve(),
            self.paths.processed.resolve(),
            self.paths.builds.resolve(),
        ]
        protected_files = {
            self.paths.current.resolve(),
            self.paths.source_manifest.resolve(),
            self.paths.job_lock.resolve(),
        }
        if destination in protected_files or any(
            destination.is_relative_to(directory) for directory in protected_directories
        ):
            raise HouseHunterError("Export destination is inside managed HouseHunter data")
        destination.parent.mkdir(parents=True, exist_ok=True)
        query = _export_select_sql(table)
        if format == "parquet":
            self.connection.execute(
                f"COPY ({query}) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(destination)]
            )
        elif format == "csv":
            self.connection.execute(
                f"COPY ({query}) TO ? (HEADER, DELIMITER ',')",
                [str(destination)],
            )
        elif format == "json":
            self.connection.execute(
                f"COPY ({query}) TO ? (FORMAT JSON, ARRAY true)",
                [str(destination)],
            )
        else:
            raise HouseHunterError("Export format must be csv, json, or parquet")
        return destination
