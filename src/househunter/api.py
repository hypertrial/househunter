from __future__ import annotations

import csv
import io
import json
import secrets
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .config import RuntimePaths, sha256_bytes
from .contracts import (
    AddressConfirmation,
    AddressLookup,
    AddressLookupRequest,
    JobStatus,
    MapScores,
    PlaceDetail,
    PlacePage,
    SourceStatus,
)
from .download import source_statuses
from .errors import AmbiguousPlaceError, BuildNotFoundError, HouseHunterError
from .geocode import lookup_address
from .jobs import JobKind, JobManager
from .map_assets import (
    MANIFEST_NAME,
    asset_directory,
    load_manifest,
    map_asset_status,
)
from .store import Store, current_build


class JobRequest(BaseModel):
    kind: JobKind
    state: str | None = None


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, *, testing: bool = False) -> None:
        super().__init__(app)
        self.testing = testing

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        raw_host = request.headers.get("host", "")
        try:
            parsed_host = urlsplit(f"//{raw_host}")
            host = (parsed_host.hostname or "").lower()
            host_port = parsed_host.port or (443 if request.url.scheme == "https" else 80)
            invalid_host = bool(
                parsed_host.username
                or parsed_host.password
                or parsed_host.path
                or parsed_host.query
                or parsed_host.fragment
            )
        except ValueError:
            host = ""
            host_port = 0
            invalid_host = True
        allowed = {"127.0.0.1", "localhost", "::1"}
        if invalid_host or (not self.testing and host not in allowed):
            return JSONResponse({"detail": "Host is not loopback"}, status_code=400)
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                origin_host = (parsed.hostname or "").lower()
                origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                origin_scheme = parsed.scheme
                invalid_origin = bool(
                    parsed.username
                    or parsed.password
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                )
            except ValueError:
                origin_host = ""
                origin_port = 0
                origin_scheme = ""
                invalid_origin = True
            if (
                invalid_origin
                or origin_host != host
                or origin_host not in allowed
                or origin_port != host_port
                or origin_scheme != request.url.scheme
            ):
                return JSONResponse(
                    {"detail": "Origin is not same-origin loopback"}, status_code=403
                )
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site" and (
            request.url.path.startswith("/api/") or request.url.path.startswith("/map-assets/")
        ):
            return JSONResponse({"detail": "Cross-site loopback request rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def static_directory() -> Path:
    packaged = Path(__file__).with_name("static")
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / "web" / "dist"
    return checkout


def create_app(paths: RuntimePaths | None = None, *, testing: bool = False) -> FastAPI:
    runtime = paths or RuntimePaths.from_root()
    runtime.ensure()
    token = secrets.token_urlsafe(32)
    jobs = JobManager(runtime)
    app = FastAPI(title="HouseHunter", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(LocalOnlyMiddleware, testing=testing)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    asset_root = asset_directory()
    assets = map_asset_status(asset_root)
    verified_manifest = load_manifest(asset_root, verify_files=False) if assets.ready else None
    verified_assets = {
        entry["filename"]: entry for entry in verified_manifest["files"]
    } if verified_manifest else {}

    @app.exception_handler(HouseHunterError)
    async def handle_househunter_error(_: Request, exc: HouseHunterError) -> JSONResponse:
        body: dict[str, object] = {"detail": str(exc)}
        if isinstance(exc, AmbiguousPlaceError):
            body["candidates"] = exc.candidates
        status = 404 if isinstance(exc, BuildNotFoundError) else 400
        return JSONResponse(body, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    def require_token(x_househunter_token: Annotated[str | None, Header()] = None) -> None:
        if x_househunter_token is None or not secrets.compare_digest(x_househunter_token, token):
            raise HTTPException(status_code=403, detail="Invalid or missing mutation token")

    @app.get("/api/v2/meta")
    def meta() -> dict[str, object]:
        result: dict[str, object] = {
            "app_version": __version__,
            "mutation_token": token,
            "methodology": "Separate FEMA tract-level and county-level ALR_NPCTL percentiles",
            "reference_assets_ready": True,
            "reference_assets_error": None,
            "map_assets": assets.as_dict(),
        }
        try:
            _, metadata = current_build(runtime)
            result["build"] = metadata
        except BuildNotFoundError:
            result["build"] = None
        return result

    @app.get("/api/v2/map/scores", response_model=MapScores)
    def map_scores(level: Literal["tract", "county"] = "tract") -> dict[str, object]:
        with Store(runtime) as store:
            return store.map_scores(level)

    @app.get(f"/map-assets/{MANIFEST_NAME}")
    def map_manifest() -> Response:
        if verified_manifest is None:
            raise HTTPException(
                status_code=503, detail=assets.error or "Map assets are unavailable"
            )
        return Response(
            content=json.dumps(verified_manifest, sort_keys=True, separators=(",", ":")) + "\n",
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/map-assets/{filename}")
    def map_asset(filename: str) -> Response:
        if Path(filename).name != filename or filename not in verified_assets:
            raise HTTPException(status_code=404, detail="Map asset is not listed in the manifest")
        entry = verified_assets[filename]
        path = asset_root / filename
        try:
            if path.is_symlink():
                raise ValueError("Map asset checksum mismatch")
            with path.open("rb") as handle:
                content = handle.read(entry["compressed_size"] + 1)
            if len(content) != entry["compressed_size"]:
                raise ValueError("Map asset is missing or has the wrong size")
            if sha256_bytes(content) != entry["sha256"]:
                raise ValueError("Map asset checksum mismatch")
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type="application/topo+json",
            headers={
                "Content-Encoding": "gzip",
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    @app.get("/api/v2/sources", response_model=list[SourceStatus])
    def sources() -> list[dict[str, object]]:
        return [status.model_dump(mode="json") for status in source_statuses(runtime)]

    @app.post(
        "/api/v2/jobs",
        dependencies=[Depends(require_token)],
        status_code=202,
        response_model=JobStatus,
    )
    def create_job(request: JobRequest) -> dict[str, object]:
        return jobs.start(request.kind, state=request.state).model_dump(mode="json")

    @app.get("/api/v2/jobs/{job_id}", response_model=JobStatus)
    def get_job(job_id: str) -> dict[str, object]:
        return jobs.get(job_id).model_dump(mode="json")

    @app.delete(
        "/api/v2/jobs/{job_id}",
        dependencies=[Depends(require_token)],
        response_model=JobStatus,
    )
    def cancel_job(job_id: str) -> dict[str, object]:
        return jobs.cancel(job_id).model_dump(mode="json")

    @app.get("/api/v2/places", response_model=PlacePage)
    def places(
        search: str | None = None,
        state: str | None = None,
        county: str | None = None,
        min_population: int | None = Query(None, ge=0),
        max_population: int | None = Query(None, ge=0),
        min_score: float | None = Query(None, ge=0, le=100),
        max_score: float | None = Query(None, ge=0, le=100),
        community_conditions_group: int | None = Query(None, ge=1, le=10),
        mountain_magnitude_min: float | None = Query(None, ge=0),
        mountain_magnitude_max: float | None = Query(None, ge=0),
        include_unranked: bool = False,
        sort: str = "risk_score",
        direction: Literal["asc", "desc"] = "asc",
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, object]:
        with Store(runtime) as store:
            return store.list_places(
                search=search,
                state=state,
                county=county,
                min_population=min_population,
                max_population=max_population,
                min_score=min_score,
                max_score=max_score,
                community_conditions_group=community_conditions_group,
                mountain_magnitude_min=mountain_magnitude_min,
                mountain_magnitude_max=mountain_magnitude_max,
                include_unranked=include_unranked,
                sort=sort,
                direction=direction,
                offset=offset,
                limit=limit,
            )

    @app.get("/api/v2/places/{place_id}", response_model=PlaceDetail)
    def place(place_id: str) -> dict[str, object]:
        with Store(runtime) as store:
            return store.place_detail(store.resolve_place(place_id))

    @app.post("/api/v2/lookup", response_model=AddressLookup | AddressConfirmation)
    def lookup(request: AddressLookupRequest) -> dict[str, object]:
        return lookup_address(runtime, request.address, candidate_id=request.candidate_id)

    @app.get("/api/v2/counties", response_model=PlacePage)
    def counties(
        search: str | None = None,
        state: str | None = None,
        min_score: float | None = Query(None, ge=0, le=100),
        max_score: float | None = Query(None, ge=0, le=100),
        community_conditions_group: int | None = Query(None, ge=1, le=10),
        mountain_magnitude_min: float | None = Query(None, ge=0),
        mountain_magnitude_max: float | None = Query(None, ge=0),
        include_unranked: bool = False,
        sort: str = "risk_score",
        direction: Literal["asc", "desc"] = "asc",
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, object]:
        with Store(runtime) as store:
            return store.list_counties(
                search=search,
                state=state,
                min_score=min_score,
                max_score=max_score,
                community_conditions_group=community_conditions_group,
                mountain_magnitude_min=mountain_magnitude_min,
                mountain_magnitude_max=mountain_magnitude_max,
                include_unranked=include_unranked,
                sort=sort,
                direction=direction,
                offset=offset,
                limit=limit,
            )

    @app.get("/api/v2/counties/{stco_fips}", response_model=PlaceDetail)
    def county(stco_fips: str) -> dict[str, object]:
        with Store(runtime) as store:
            return store.county_detail(store.resolve_county(stco_fips))

    def csv_export_for(table: Literal["places", "counties"], filename: str) -> StreamingResponse:
        def rows():  # type: ignore[no-untyped-def]
            with Store(runtime) as store:
                cursor = store.connection.execute(f"SELECT * FROM {table} ORDER BY place_id")
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([column[0] for column in cursor.description])
                yield buffer.getvalue()
                while batch := cursor.fetchmany(1000):
                    buffer.seek(0)
                    buffer.truncate(0)
                    writer.writerows(batch)
                    yield buffer.getvalue()

        return StreamingResponse(
            rows(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def parquet_export_for(name: Literal["places", "counties"], filename: str) -> FileResponse:
        build, _ = current_build(runtime)
        return FileResponse(
            build / f"{name}.parquet",
            media_type="application/vnd.apache.parquet",
            filename=filename,
        )

    def json_export_for(table: Literal["places", "counties"], filename: str) -> StreamingResponse:
        def rows():  # type: ignore[no-untyped-def]
            yield "["
            first = True
            with Store(runtime) as store:
                cursor = store.connection.execute(f"SELECT * FROM {table} ORDER BY place_id")
                columns = [column[0] for column in cursor.description]
                while batch := cursor.fetchmany(1000):
                    for row in batch:
                        if not first:
                            yield ","
                        yield json.dumps(
                            dict(zip(columns, row, strict=True)), separators=(",", ":")
                        )
                        first = False
            yield "]"

        return StreamingResponse(
            rows(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/v2/exports/places.csv")
    def csv_export() -> StreamingResponse:
        return csv_export_for("places", "househunter-places.csv")

    @app.get("/api/v2/exports/places.parquet")
    def parquet_export() -> FileResponse:
        return parquet_export_for("places", "househunter-places.parquet")

    @app.get("/api/v2/exports/places.json")
    def json_export() -> StreamingResponse:
        return json_export_for("places", "househunter-places.json")

    @app.get("/api/v2/exports/counties.csv")
    def counties_csv_export() -> StreamingResponse:
        return csv_export_for("counties", "househunter-counties.csv")

    @app.get("/api/v2/exports/counties.parquet")
    def counties_parquet_export() -> FileResponse:
        return parquet_export_for("counties", "househunter-counties.parquet")

    @app.get("/api/v2/exports/counties.json")
    def counties_json_export() -> StreamingResponse:
        return json_export_for("counties", "househunter-counties.json")

    legacy_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

    @app.api_route("/api/v1", methods=legacy_methods, include_in_schema=False)
    @app.api_route("/api/v1/{legacy_path:path}", methods=legacy_methods, include_in_schema=False)
    def removed_v1_api(legacy_path: str = "") -> None:
        raise HTTPException(status_code=404, detail="HouseHunter API v1 has been removed")

    static = static_directory()
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app
