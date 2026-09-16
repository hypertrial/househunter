from __future__ import annotations

import csv
import io
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .config import RuntimePaths, sha256_bytes
from .contracts import (
    AddressConfirmation,
    AddressLookup,
    AddressLookupRequest,
    CostOfLivingMapScores,
    HomeCostsMapScores,
    JobStatus,
    MapScores,
    MapScoresCore,
    PlaceDetail,
    PlacePage,
    SourceStatus,
)
from .download import source_statuses
from .errors import AmbiguousPlaceError, BuildNotFoundError, HouseHunterError
from .geocode import lookup_address
from .home_market import is_stale as home_market_is_stale
from .home_market import source_status as home_market_source_status
from .jobs import JobKind, JobManager
from .map_assets import (
    MANIFEST_NAME,
    asset_directory,
    load_manifest,
    map_asset_status,
)
from .store import Store, _export_select_sql, current_build
from .top_counties import PILLARS, PREFERENCE_NOTICE, evaluate_counties


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
        if request.url.path in {"/api/v1", "/api/v2"} or request.url.path.startswith(
            ("/api/v1/", "/api/v2/")
        ):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
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


def _public_build_metadata(metadata: dict[str, object]) -> dict[str, object]:
    public = dict(metadata)
    public.pop("ranking", None)
    return public


def _county_fit_readiness(metadata: dict[str, object]) -> dict[str, object]:
    scope = metadata.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") != "national":
        return {
            "readiness": "unavailable",
            "reason_code": "national_snapshot_required",
            "methodology_id": "top-counties-v2",
            "available_pillars": [],
        }
    ranking = metadata.get("ranking")
    if not isinstance(ranking, dict):
        return {
            "readiness": "unavailable",
            "reason_code": "ranking_metadata_missing",
            "methodology_id": "top-counties-v2",
            "available_pillars": [],
        }
    return {
        key: ranking.get(key)
        for key in (
            "readiness",
            "reason_code",
            "methodology_id",
            "calibration_id",
            "bundle_schema_version",
            "bundle_release",
            "vintages",
            "row_count",
            "available_pillars",
            "local_history",
            "notices",
        )
    }


def _county_fit_query(
    build_id: Annotated[str, Query(min_length=1, max_length=128)],
    view: Literal[
        "safety",
        "health",
        "affordability",
        "opportunity",
        "lifestyle",
        "family",
        "family-autonomy",
        "custom",
        "custom-fit",
    ] = "custom",
    preset: str = "balanced",
    weight_safety: float | None = Query(None, ge=0),
    weight_health: float | None = Query(None, ge=0),
    weight_affordability: float | None = Query(None, ge=0),
    weight_opportunity: float | None = Query(None, ge=0),
    weight_lifestyle: float | None = Query(None, ge=0),
    weight_family: float | None = Query(None, ge=0),
    state: Annotated[list[str] | None, Query()] = None,
    exclude_state: Annotated[list[str] | None, Query()] = None,
    exclude_appalachia: bool = False,
    min_population: int | None = Query(None, ge=0),
    min_active_listings: int | None = Query(None, ge=0),
    min_valid_months: int | None = Query(None, ge=1, le=12),
    min_jan_temp_f: float | None = None,
    max_jan_temp_f: float | None = None,
    min_jul_temp_f: float | None = None,
    max_jul_temp_f: float | None = None,
    max_extreme_heat_days: float | None = Query(None, ge=0),
    max_extreme_cold_days: float | None = Query(None, ge=0),
    min_safety: float | None = Query(None, ge=0, le=1),
    min_health: float | None = Query(None, ge=0, le=1),
    min_affordability: float | None = Query(None, ge=0, le=1),
    min_opportunity: float | None = Query(None, ge=0, le=1),
    min_lifestyle: float | None = Query(None, ge=0, le=1),
    min_family: float | None = Query(None, ge=0, le=1),
) -> dict[str, Any]:
    supplied_weights = {
        "safety": weight_safety,
        "health": weight_health,
        "affordability": weight_affordability,
        "opportunity": weight_opportunity,
        "lifestyle": weight_lifestyle,
        "family": weight_family,
    }
    custom_weights = (
        None if all(value is None for value in supplied_weights.values()) else supplied_weights
    )
    return {
        "build_id": build_id,
        "view": view,
        "preset": preset,
        "custom_weights": custom_weights,
        "states": state or (),
        "exclude_states": exclude_state or (),
        "exclude_region": "appalachia" if exclude_appalachia else None,
        "min_population": min_population,
        "min_active_listings": min_active_listings,
        "min_valid_months": min_valid_months,
        "min_jan_temp_f": min_jan_temp_f,
        "max_jan_temp_f": max_jan_temp_f,
        "min_jul_temp_f": min_jul_temp_f,
        "max_jul_temp_f": max_jul_temp_f,
        "max_extreme_heat_days": max_extreme_heat_days,
        "max_extreme_cold_days": max_extreme_cold_days,
        "min_pillars": {
            pillar: value
            for pillar, value in {
                "safety": min_safety,
                "health": min_health,
                "affordability": min_affordability,
                "opportunity": min_opportunity,
                "lifestyle": min_lifestyle,
                "family": min_family,
            }.items()
            if value is not None
        },
    }


def _with_live_home_market_staleness(metadata: dict[str, object]) -> dict[str, object]:
    """Refresh the time-varying stale flag without changing immutable build metadata."""
    sources = metadata.get("sources")
    if not isinstance(sources, list):
        return metadata
    refreshed_sources: list[object] = []
    for value in sources:
        if not isinstance(value, dict) or value.get("source") != "home_market":
            refreshed_sources.append(value)
            continue
        source = dict(value)
        release = source.get("release")
        if source.get("cached") is True and isinstance(release, str):
            try:
                source["stale"] = home_market_is_stale(release)
            except ValueError:
                source["stale"] = None
        else:
            source["stale"] = None
        refreshed_sources.append(source)
    refreshed = dict(metadata)
    refreshed["sources"] = refreshed_sources
    return refreshed


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
    verified_assets = (
        {entry["filename"]: entry for entry in verified_manifest["files"]}
        if verified_manifest
        else {}
    )

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

    @app.get("/api/v3/meta")
    def meta() -> dict[str, object]:
        result: dict[str, object] = {
            "app_version": __version__,
            "mutation_token": token,
            "methodology": (
                "HouseHunter Residential Hazard Exposure, calibrated separately for the "
                "national FEMA tract and county layers"
            ),
            "reference_assets_ready": True,
            "reference_assets_error": None,
            "map_assets": assets.as_dict(),
        }
        try:
            _, metadata = current_build(runtime)
            metadata = _with_live_home_market_staleness(metadata)
            result["county_fit"] = _county_fit_readiness(metadata)
            public_metadata = _public_build_metadata(metadata)
            result["build"] = public_metadata
            result["layers"] = public_metadata.get("layers", [])
        except BuildNotFoundError:
            result["build"] = None
            result["layers"] = []
            result["county_fit"] = {
                "readiness": "unavailable",
                "reason_code": "snapshot_missing_or_incompatible",
                "methodology_id": "top-counties-v2",
                "available_pillars": [],
            }
        return result

    @app.get("/api/v3/map/scores", response_model=MapScores)
    def map_scores(level: Literal["tract", "county"] = "tract") -> dict[str, object]:
        with Store(runtime) as store:
            return store.map_scores(level)

    @app.get("/api/v3/map/scores/core", response_model=MapScoresCore)
    def map_scores_core(
        level: Literal["tract", "county"] = "tract",
    ) -> dict[str, object]:
        with Store(runtime) as store:
            payload = store.map_scores_core(level)
        query = urlencode({"level": level, "build_id": payload["build_id"]})
        payload["add_ons"] = {
            "cost_of_living": f"/api/v3/map/scores/addons/cost-of-living?{query}",
            "home_costs": f"/api/v3/map/scores/addons/home-costs?{query}",
        }
        return payload

    def map_scores_addon(level: str, kind: str, expected_build_id: str) -> dict[str, object]:
        try:
            with Store(runtime, build_id=expected_build_id) as store:
                return store.map_scores_addon(level, kind)
        except BuildNotFoundError as exc:
            raise HTTPException(
                status_code=409,
                detail="The requested immutable map-score build is no longer available",
            ) from exc

    @app.get(
        "/api/v3/map/scores/addons/cost-of-living",
        response_model=CostOfLivingMapScores,
    )
    def cost_of_living_map_scores(
        build_id: Annotated[str, Query(min_length=1, max_length=128)],
        level: Literal["tract", "county"] = "tract",
    ) -> dict[str, object]:
        return map_scores_addon(level, "cost-of-living", build_id)

    @app.get(
        "/api/v3/map/scores/addons/home-costs",
        response_model=HomeCostsMapScores,
    )
    def home_costs_map_scores(
        build_id: Annotated[str, Query(min_length=1, max_length=128)],
        level: Literal["tract", "county"] = "tract",
    ) -> dict[str, object]:
        return map_scores_addon(level, "home-costs", build_id)

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

    @app.get("/api/v3/sources", response_model=list[SourceStatus])
    def sources() -> list[dict[str, object]]:
        try:
            _, metadata = current_build(runtime)
            metadata = _with_live_home_market_staleness(metadata)
            snapshot_sources = metadata.get("sources")
            if isinstance(snapshot_sources, list):
                return snapshot_sources
        except BuildNotFoundError:
            pass
        live = [status.model_dump(mode="json") for status in source_statuses(runtime)]
        home = home_market_source_status(runtime)
        home["version"] = str(home["release"] or "unavailable")
        live.append(home)
        return live

    @app.post(
        "/api/v3/jobs",
        dependencies=[Depends(require_token)],
        status_code=202,
        response_model=JobStatus,
    )
    def create_job(request: JobRequest) -> dict[str, object]:
        return jobs.start(request.kind, state=request.state).model_dump(mode="json")

    @app.get("/api/v3/jobs/{job_id}", response_model=JobStatus)
    def get_job(job_id: str) -> dict[str, object]:
        return jobs.get(job_id).model_dump(mode="json")

    @app.delete(
        "/api/v3/jobs/{job_id}",
        dependencies=[Depends(require_token)],
        response_model=JobStatus,
    )
    def cancel_job(job_id: str) -> dict[str, object]:
        return jobs.cancel(job_id).model_dump(mode="json")

    @app.get("/api/v3/places", response_model=PlacePage)
    def places(
        search: str | None = None,
        state: str | None = None,
        county: str | None = None,
        min_population: int | None = Query(None, ge=0),
        max_population: int | None = Query(None, ge=0),
        min_res_hazard: float | None = Query(None, ge=0, le=100),
        max_res_hazard: float | None = Query(None, ge=0, le=100),
        community_conditions_group: int | None = Query(None, ge=1, le=10),
        max_community_conditions_group: int | None = Query(None, ge=1, le=10),
        mountain_magnitude_min: float | None = Query(None, ge=0),
        mountain_magnitude_max: float | None = Query(None, ge=0),
        cost_of_living_index_min: float | None = Query(None, ge=0),
        cost_of_living_index_max: float | None = Query(None, ge=0),
        home_sqft_for_1m_min: float | None = Query(None, ge=0),
        home_sqft_for_1m_max: float | None = Query(None, ge=0),
        housing_built_2000_plus_pct_min: float | None = Query(None, ge=0, le=100),
        housing_built_2000_plus_pct_max: float | None = Query(None, ge=0, le=100),
        include_unranked: bool = False,
        sort: str = "res_hazard_npctl",
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

    @app.get("/api/v3/places/{place_id}", response_model=PlaceDetail)
    def place(place_id: str) -> dict[str, object]:
        with Store(runtime) as store:
            return store.place_detail(store.resolve_place(place_id))

    @app.post("/api/v3/lookup", response_model=AddressLookup | AddressConfirmation)
    def lookup(request: AddressLookupRequest) -> dict[str, object]:
        return lookup_address(runtime, request.address, candidate_id=request.candidate_id)

    @app.get("/api/v3/counties", response_model=PlacePage)
    def counties(
        search: str | None = None,
        state: str | None = None,
        min_res_hazard: float | None = Query(None, ge=0, le=100),
        max_res_hazard: float | None = Query(None, ge=0, le=100),
        community_conditions_group: int | None = Query(None, ge=1, le=10),
        max_community_conditions_group: int | None = Query(None, ge=1, le=10),
        mountain_magnitude_min: float | None = Query(None, ge=0),
        mountain_magnitude_max: float | None = Query(None, ge=0),
        cost_of_living_index_min: float | None = Query(None, ge=0),
        cost_of_living_index_max: float | None = Query(None, ge=0),
        home_sqft_for_1m_min: float | None = Query(None, ge=0),
        home_sqft_for_1m_max: float | None = Query(None, ge=0),
        housing_built_2000_plus_pct_min: float | None = Query(None, ge=0, le=100),
        housing_built_2000_plus_pct_max: float | None = Query(None, ge=0, le=100),
        include_unranked: bool = False,
        sort: str = "res_hazard_npctl",
        direction: Literal["asc", "desc"] = "asc",
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, object]:
        with Store(runtime) as store:
            return store.list_counties(
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

    @app.get("/api/v3/counties/{stco_fips}", response_model=PlaceDetail)
    def county(stco_fips: str) -> dict[str, object]:
        with Store(runtime) as store:
            return store.county_detail(store.resolve_county(stco_fips))

    def county_fit_evaluation(query: dict[str, Any]):  # type: ignore[no-untyped-def]
        try:
            _, current_metadata = current_build(runtime)
        except BuildNotFoundError as exc:
            raise HTTPException(
                status_code=503,
                detail="County Fit needs a current schema-13 national snapshot",
            ) from exc
        if query["build_id"] != current_metadata.get("build_id"):
            raise HTTPException(status_code=409, detail="County Fit build_id is stale")
        scope = current_metadata.get("scope")
        if not isinstance(scope, dict) or scope.get("kind") != "national":
            raise HTTPException(status_code=409, detail="County Fit requires a national snapshot")
        ranking = current_metadata.get("ranking")
        if not isinstance(ranking, dict) or not ranking.get("available"):
            raise HTTPException(
                status_code=503,
                detail=(ranking or {}).get("reason_code") or "County Fit bundle is unavailable",
            )
        normalized_view = (
            str(query["view"]).replace("family-autonomy", "family").replace("custom-fit", "custom")
        )
        evaluation_options = {
            "view": normalized_view,
            "preset": str(query["preset"]),
            "custom_weights": query["custom_weights"],
            "min_population": query["min_population"],
            "min_active_listings": query["min_active_listings"],
            "min_valid_months": query["min_valid_months"],
            "states": query["states"],
            "exclude_states": query["exclude_states"],
            "exclude_region": query["exclude_region"],
            "min_jan_temp_f": query["min_jan_temp_f"],
            "max_jan_temp_f": query["max_jan_temp_f"],
            "min_jul_temp_f": query["min_jul_temp_f"],
            "max_jul_temp_f": query["max_jul_temp_f"],
            "max_extreme_heat_days": query["max_extreme_heat_days"],
            "max_extreme_cold_days": query["max_extreme_cold_days"],
            "min_pillars": query["min_pillars"],
            "vintages": ranking.get("vintages") or {},
            "calibration_id": str(ranking.get("calibration_id") or ""),
        }
        try:
            evaluate_counties([], **evaluation_options)
        except HouseHunterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        available = set(ranking.get("available_pillars") or [])
        if normalized_view == "custom" and ranking.get("readiness") != "ready":
            raise HTTPException(
                status_code=503,
                detail="Custom Fit requires at least nine approved housing-history months",
            )
        if normalized_view != "custom" and normalized_view not in available:
            raise HTTPException(
                status_code=503,
                detail=f"County Fit pillar is unavailable: {normalized_view}",
            )
        try:
            with Store(runtime, build_id=str(query["build_id"])) as store:
                candidates = store.list_county_candidates()
            evaluation = evaluate_counties(candidates, **evaluation_options)
        except HouseHunterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return current_metadata, ranking, candidates, evaluation

    @app.get("/api/v3/county-fit")
    def county_fit(
        query: Annotated[dict[str, Any], Depends(_county_fit_query)],
    ) -> dict[str, object]:
        metadata, ranking, _, evaluation = county_fit_evaluation(query)
        columns: dict[str, list[object]] = {
            "county_fips": [],
            "name": [],
            "state": [],
            "active_value": [],
            "eligible": [],
            "exclusion_reason": [],
            "national_rank": [],
            "filtered_rank": [],
            "pareto_optimal": [],
            **{f"u_{pillar}": [] for pillar in PILLARS},
        }
        for row in evaluation.rows:
            columns["county_fips"].append(row.place_id)
            columns["name"].append(row.name)
            columns["state"].append(row.state)
            columns["active_value"].append(row.active_value)
            columns["eligible"].append(row.eligible)
            columns["exclusion_reason"].append(row.exclusion_reason)
            columns["national_rank"].append(row.national_rank)
            columns["filtered_rank"].append(row.filtered_rank)
            columns["pareto_optimal"].append(row.pareto_optimal)
            for pillar in PILLARS:
                columns[f"u_{pillar}"].append(row.pillars[pillar])
        notices = list(dict.fromkeys([PREFERENCE_NOTICE, *(ranking.get("notices") or [])]))
        return {
            "schema_version": 1,
            "build_id": metadata["build_id"],
            "methodology_id": evaluation.methodology_id,
            "calibration_id": evaluation.calibration_id,
            "view": evaluation.view,
            "preset": evaluation.preset,
            "weights": evaluation.weights,
            "gates": evaluation.gates,
            "reference_count": evaluation.reference_count,
            "national_count": evaluation.national_count,
            "cohort_count": evaluation.filtered_count,
            "exclusions": evaluation.exclusions,
            "notices": notices,
            "counties": columns,
        }

    @app.get("/api/v3/county-fit/counties/{fips}")
    def county_fit_detail(
        fips: str,
        query: Annotated[dict[str, Any], Depends(_county_fit_query)],
    ) -> dict[str, object]:
        metadata, ranking, candidates, evaluation = county_fit_evaluation(query)
        if len(fips) != 5 or not fips.isdigit():
            raise HTTPException(status_code=404, detail="County Fit county is invalid")
        source = next((row for row in candidates if row["place_id"] == fips), None)
        result = next((row for row in evaluation.rows if row.place_id == fips), None)
        if source is None or result is None:
            raise HTTPException(status_code=404, detail="County Fit county was not found")
        groups = {
            "safety": (
                "res_hazard_npctl",
                "crime_violent_rate",
                "crime_property_rate",
                "crime_coverage",
                "water_violation_share",
                "public_water_coverage",
                "water_allocation_coverage",
            ),
            "health": (
                "provider_primary_care",
                "provider_mental_health",
                "provider_dental",
                "community_context",
            ),
            "affordability": (
                "sqft_for_1m_t12",
                "rpp_index",
                "rpp_geography_type",
                "property_tax_rate",
                "housing_valid_months",
                "median_active_listings",
            ),
            "opportunity": (
                "employment_growth",
                "average_weekly_wage",
                "commute_under_30_share",
                "broadband_100_20",
                "broadband_denominator_label",
            ),
            "lifestyle": (
                "mountain_magnitude",
                "jan_avg_temp_f",
                "jul_avg_temp_f",
                "extreme_heat_days",
                "extreme_cold_days",
                "climate_station_count",
            ),
            "family": ("homeschool_utility",),
        }
        rubric: object = None
        limitations: list[object] = []
        try:
            rubric = json.loads(source.get("homeschool_rubric_json") or "null")
            limitations = json.loads(source.get("limitations_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            pass
        statuses = {
            key: value
            for key, value in source.items()
            if key.endswith("_status")
            or key.endswith("_coverage")
            or key.startswith("crime_violent_coverage_")
            or key.startswith("crime_property_coverage_")
            or key
            in {
                "water_boundary_provenance",
                "water_overlap_duplicate_share_proxy",
                "water_overlap_quality_status",
                "broadband_denominator_label",
                "climate_station_count",
            }
        }
        return {
            "schema_version": 1,
            "build_id": metadata["build_id"],
            "county": {"fips": fips, "name": result.name, "state": result.state},
            "view": evaluation.view,
            "active_value": result.active_value,
            "eligible": result.eligible,
            "exclusion_reason": result.exclusion_reason,
            "national_rank": result.national_rank,
            "filtered_rank": result.filtered_rank,
            "pareto_optimal": result.pareto_optimal,
            "weights": evaluation.weights,
            "gates": evaluation.gates,
            "pillars": {
                pillar: {
                    "utility": result.pillars[pillar],
                    "weight": evaluation.weights[pillar],
                    "contribution": (
                        None
                        if result.pillars[pillar] is None
                        else result.pillars[pillar] * evaluation.weights[pillar]
                    ),
                    "measures": {key: source.get(key) for key in fields},
                }
                for pillar, fields in groups.items()
            },
            "subutilities": result.utilities,
            "coverage": statuses,
            "vintages": {
                "bundle": ranking.get("vintages") or {},
                "primary_care": source.get("provider_primary_care_vintage"),
                "mental_health": source.get("provider_mental_health_vintage"),
                "dental": source.get("provider_dental_vintage"),
            },
            "sources": {
                "primary_care": source.get("provider_primary_care_source"),
                "mental_health": source.get("provider_mental_health_source"),
                "dental": source.get("provider_dental_source"),
            },
            "citations": ranking.get("citations") or {},
            "rubric_components": rubric,
            "limitations": list(dict.fromkeys([*limitations, *(ranking.get("notices") or [])])),
        }

    def csv_export_for(table: Literal["places", "counties"], filename: str) -> StreamingResponse:
        def rows():  # type: ignore[no-untyped-def]
            with Store(runtime) as store:
                cursor = store.connection.execute(_export_select_sql(table))
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
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="househunter-export-", suffix=".parquet"
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with Store(runtime) as store:
                store.export("parquet", temporary, table=name)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return FileResponse(
            temporary,
            media_type="application/vnd.apache.parquet",
            filename=filename,
            background=BackgroundTask(temporary.unlink, missing_ok=True),
        )

    def json_export_for(table: Literal["places", "counties"], filename: str) -> StreamingResponse:
        def rows():  # type: ignore[no-untyped-def]
            yield "["
            first = True
            with Store(runtime) as store:
                cursor = store.connection.execute(_export_select_sql(table))
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

    @app.get("/api/v3/exports/places.csv")
    def csv_export() -> StreamingResponse:
        return csv_export_for("places", "househunter-places.csv")

    @app.get("/api/v3/exports/places.parquet")
    def parquet_export() -> FileResponse:
        return parquet_export_for("places", "househunter-places.parquet")

    @app.get("/api/v3/exports/places.json")
    def json_export() -> StreamingResponse:
        return json_export_for("places", "househunter-places.json")

    @app.get("/api/v3/exports/counties.csv")
    def counties_csv_export() -> StreamingResponse:
        return csv_export_for("counties", "househunter-counties.csv")

    @app.get("/api/v3/exports/counties.parquet")
    def counties_parquet_export() -> FileResponse:
        return parquet_export_for("counties", "househunter-counties.parquet")

    @app.get("/api/v3/exports/counties.json")
    def counties_json_export() -> StreamingResponse:
        return json_export_for("counties", "househunter-counties.json")

    @app.get("/api/v3/exports/county-fit.csv")
    def county_fit_csv_export(
        query: Annotated[dict[str, Any], Depends(_county_fit_query)],
    ) -> StreamingResponse:
        _, ranking, candidates, evaluation = county_fit_evaluation(query)
        by_id = {str(row["place_id"]): row for row in candidates}
        ordered = sorted(
            (row for row in evaluation.rows if row.eligible),
            key=lambda row: (row.filtered_rank or 0, row.place_id),
        )
        source_columns = (
            [column for column in candidates[0] if column not in {"name", "state", "place_id"}]
            if candidates
            else []
        )
        header = [
            "county_fips",
            "name",
            "state",
            "view",
            "active_value",
            "eligible",
            "exclusion_reason",
            "national_rank",
            "filtered_rank",
            "pareto_optimal",
            *source_columns,
            "weights_json",
            "vintages_json",
            "notices_json",
        ]

        def rows():  # type: ignore[no-untyped-def]
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(header)
            yield buffer.getvalue()
            for result in ordered:
                source = by_id[result.place_id]
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerow(
                    [
                        result.place_id,
                        result.name,
                        result.state,
                        evaluation.view,
                        result.active_value,
                        result.eligible,
                        result.exclusion_reason,
                        result.national_rank,
                        result.filtered_rank,
                        result.pareto_optimal,
                        *(source.get(column) for column in source_columns),
                        json.dumps(evaluation.weights, sort_keys=True),
                        json.dumps(ranking.get("vintages") or {}, sort_keys=True),
                        json.dumps(
                            list(
                                dict.fromkeys([PREFERENCE_NOTICE, *(ranking.get("notices") or [])])
                            )
                        ),
                    ]
                )
                yield buffer.getvalue()

        return StreamingResponse(
            rows(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="househunter-county-fit.csv"'},
        )

    static = static_directory()
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app
