from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb

from .config import RuntimePaths
from .contracts import METHODOLOGY_NOTICE
from .errors import AmbiguousPlaceError, BuildNotFoundError, HouseHunterError

SUMMARY_COLUMNS = """
place_id, name, state, place_type, population_2020, housing_units_2020,
population_2024, housing_units_2024, median_home_value_2024, risk_score,
coverage_status, fema_vintage, census_vintage, acs_vintage
"""
SUMMARY_KEYS = [
    "place_id",
    "name",
    "state",
    "place_type",
    "population_2020",
    "housing_units_2020",
    "population_2024",
    "housing_units_2024",
    "median_home_value_2024",
    "risk_score",
    "coverage_status",
    "fema_vintage",
    "census_vintage",
    "acs_vintage",
]


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
    required = [
        build / "househunter.duckdb",
        build / "build.json",
        build / "places.parquet",
        build / "tract_contributions.parquet",
    ]
    if not build.is_dir() or any(not path.is_file() for path in required):
        raise BuildNotFoundError(f"Published build is incomplete: {build}")
    try:
        metadata = json.loads((build / "build.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildNotFoundError(f"Build metadata is invalid: {exc}") from exc
    if metadata.get("build_id") != pointer.get("build_id"):
        raise BuildNotFoundError("Current pointer and build metadata disagree")
    return build, metadata


class Store:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self.build, self.metadata = current_build(paths)
        self.connection = duckdb.connect(str(self.build / "househunter.duckdb"), read_only=True)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_places(
        self,
        *,
        search: str | None = None,
        state: str | None = None,
        min_population: int | None = None,
        max_population: int | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        include_unranked: bool = False,
        sort: str = "risk_score",
        direction: str = "asc",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        sort_columns = {
            "risk_score": "risk_score",
            "name": "name",
            "state": "state",
            "population": "coalesce(population_2024, population_2020)",
            "home_value": "median_home_value_2024",
        }
        if sort not in sort_columns:
            raise HouseHunterError(f"Unsupported sort column: {sort}")
        if direction not in {"asc", "desc"}:
            raise HouseHunterError("Sort direction must be asc or desc")
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        clauses: list[str] = []
        parameters: list[Any] = []
        if not include_unranked:
            clauses.append("coverage_status = 'complete'")
        if search:
            clauses.append("(name ILIKE ? OR place_id = ?)")
            parameters.extend([f"%{search}%", search])
        if state:
            clauses.append("state = ?")
            parameters.append(state.upper())
        for column, operator, value in (
            ("coalesce(population_2024, population_2020)", ">=", min_population),
            ("coalesce(population_2024, population_2020)", "<=", max_population),
            ("risk_score", ">=", min_score),
            ("risk_score", "<=", max_score),
        ):
            if value is not None:
                clauses.append(f"{column} {operator} ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.connection.execute(
            f"SELECT count(*) FROM places{where}", parameters
        ).fetchone()[0]
        null_order = "NULLS LAST"
        query = (
            f"SELECT {SUMMARY_COLUMNS} FROM places{where} "
            f"ORDER BY {sort_columns[sort]} {direction.upper()} {null_order}, "
            "place_id ASC LIMIT ? OFFSET ?"
        )
        cursor = self.connection.execute(query, [*parameters, limit, offset])
        columns = [item[0] for item in cursor.description]
        items = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def resolve_place(self, query: str) -> str:
        if len(query) == 7 and query.isdigit():
            exists = self.connection.execute(
                "SELECT 1 FROM places WHERE place_id = ?", [query]
            ).fetchone()
            if not exists:
                raise HouseHunterError(f"Place not found: {query}")
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
            raise HouseHunterError(f"Place not found: {query}")
        if len(rows) > 1:
            raise AmbiguousPlaceError(
                query,
                [{"place_id": row[0], "name": row[1], "state": row[2]} for row in rows],
            )
        return rows[0][0]

    def place_detail(self, place_id: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            f"SELECT {SUMMARY_COLUMNS}, total_weighted_housing, coverage_ratio "
            "FROM places WHERE place_id = ?",
            [place_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise HouseHunterError(f"Place not found: {place_id}")
        columns = [item[0] for item in cursor.description]
        record = dict(zip(columns, row, strict=True))
        summary = {key: record[key] for key in SUMMARY_KEYS}
        contribution_cursor = self.connection.execute(
            """
            SELECT tract_id, housing_units, housing_weight, alr_npctl AS fema_percentile,
                   weighted_contribution
            FROM tract_contributions WHERE place_id = ?
            ORDER BY weighted_contribution DESC NULLS LAST, tract_id ASC
            """,
            [place_id],
        )
        contribution_columns = [item[0] for item in contribution_cursor.description]
        contributions = [
            dict(zip(contribution_columns, item, strict=True))
            for item in contribution_cursor.fetchall()
        ]
        return {
            "summary": summary,
            "total_weighted_housing": record["total_weighted_housing"],
            "coverage_ratio": record["coverage_ratio"],
            "methodology_notice": METHODOLOGY_NOTICE,
            "tract_contributions": contributions,
        }

    def export(self, format: str, output: Path) -> Path:
        destination = output.expanduser().resolve()
        protected_directories = [self.paths.cache.resolve(), self.paths.builds.resolve()]
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
        if format == "parquet":
            shutil.copyfile(self.build / "places.parquet", destination)
        elif format == "csv":
            self.connection.execute(
                "COPY (SELECT * FROM places ORDER BY place_id) TO ? (HEADER, DELIMITER ',')",
                [str(destination)],
            )
        else:
            raise HouseHunterError("Export format must be csv or parquet")
        return destination
