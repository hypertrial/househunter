import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { geoAlbersUsa, geoArea, geoMercator, geoPath, type GeoProjection } from "d3-geo";
import { select } from "d3-selection";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import { feature as topoFeature } from "topojson-client";
import type { Feature, FeatureCollection, Geometry, GeoJsonProperties } from "geojson";
import type { GeometryCollection, Topology } from "topojson-specification";
import { cameraFromTransform, detailAsset, nationalAsset, relativeTransform, scoreColor, transformFromCamera, type CameraState } from "./map";
import type { MapManifest, MapScore } from "./types";

type MapFeature = Feature<Geometry, GeoJsonProperties & {
  place_id?: string;
  state?: string;
  county_fips?: string;
  name?: string;
  label?: [number, number];
}>;

export interface MapPreview {
  placeId: string;
  name: string;
  state: string;
  score: MapScore | null;
  x: number;
  y: number;
}

export interface FocusTarget {
  kind: "place" | "state" | "county";
  id: string;
  nonce: number;
}

interface Props {
  manifestUrl: string;
  level: "tract" | "county";
  rows: MapScore[];
  selected: string;
  state: string;
  county: string;
  showUnranked: boolean;
  neutralOnly?: boolean;
  focusTarget: FocusTarget | null;
  cameraTarget?: CameraState & { nonce: number };
  initialCamera: CameraState;
  onSelect: (placeId: string) => void;
  onPreview: (preview: MapPreview | null) => void;
  onCamera: (camera: CameraState) => void;
  onStatus: (message: string) => void;
}

interface RenderFrame {
  canvas: HTMLCanvasElement;
  transform: ZoomTransform;
  width: number;
  height: number;
  ratio: number;
}

const DETAIL_ZOOM = 4;

const TERRITORY_BOXES: Record<string, [number, number, number, number]> = {
  GU: [0.04, 0.76, 0.10, 0.89],
  MP: [0.16, 0.76, 0.22, 0.89],
  AS: [0.25, 0.76, 0.34, 0.89],
  PR: [0.72, 0.76, 0.86, 0.89],
  VI: [0.88, 0.76, 0.95, 0.89],
};

const collection = (features: MapFeature[]): FeatureCollection<Geometry> => ({
  type: "FeatureCollection",
  features,
});

const profile = (label: string) => {
  if (new URLSearchParams(window.location.search).has("profile-map")) console.debug(`[map] ${label}`, performance.now());
};

function geometryCollection(topology: Topology): GeometryCollection<Record<string, unknown>> {
  const object = topology.objects.geography;
  if (!object || object.type !== "GeometryCollection") throw new Error("Topology has no geography collection");
  return object as GeometryCollection<Record<string, unknown>>;
}

function featuresFrom(topology: Topology): MapFeature[] {
  const converted = topoFeature(topology, geometryCollection(topology));
  return ((converted as FeatureCollection<Geometry>).features as MapFeature[]).map((item) => {
    if (item.geometry.type !== "Polygon" && item.geometry.type !== "MultiPolygon") return item;
    const normalize = (rings: number[][][]) => geoArea({ type: "Polygon", coordinates: rings }) > 2 * Math.PI
      ? rings.map((ring) => [...ring].reverse())
      : rings;
    return {
      ...item,
      geometry: item.geometry.type === "Polygon"
        ? { ...item.geometry, coordinates: normalize(item.geometry.coordinates) }
        : { ...item.geometry, coordinates: item.geometry.coordinates.map(normalize) },
    } as MapFeature;
  });
}

function projectors(width: number, height: number, stateFeatures: MapFeature[]) {
  const territories = new Set(Object.keys(TERRITORY_BOXES));
  const base = stateFeatures.filter((item) => !territories.has(String(item.properties?.state)));
  const main = geoAlbersUsa().fitExtent(
    [[24, 64], [Math.max(25, width - 24), Math.max(65, height - 112)]],
    collection(base),
  );
  const byState = new Map<string, GeoProjection>();
  for (const [state, box] of Object.entries(TERRITORY_BOXES)) {
    const geography = stateFeatures.filter((item) => item.properties?.state === state);
    if (!geography.length) continue;
    byState.set(state, geoMercator().fitExtent(
      [[width * box[0], height * box[1]], [width * box[2], height * box[3]]],
      collection(geography),
    ));
  }
  return { main, byState };
}

function hitColor(index: number): string {
  const value = index + 1;
  return `rgb(${value & 255},${(value >> 8) & 255},${(value >> 16) & 255})`;
}

function hatch(context: CanvasRenderingContext2D, scale: number): CanvasPattern | string {
  const tile = document.createElement("canvas");
  tile.width = tile.height = 8;
  const brush = tile.getContext("2d");
  if (!brush) return "#566169";
  brush.fillStyle = "#3b474e";
  brush.fillRect(0, 0, 8, 8);
  brush.strokeStyle = "#7a8589";
  brush.lineWidth = 1;
  brush.beginPath();
  brush.moveTo(-2, 8);
  brush.lineTo(8, -2);
  brush.stroke();
  const pattern = context.createPattern(tile, "repeat");
  if (pattern && typeof pattern !== "string" && typeof pattern.setTransform === "function") {
    pattern.setTransform({ a: 1 / scale, d: 1 / scale });
  }
  return pattern || "#566169";
}

export default function RiskMap({
  manifestUrl,
  level,
  rows,
  selected,
  state,
  county,
  showUnranked,
  neutralOnly = false,
  focusTarget,
  cameraTarget,
  initialCamera,
  onSelect,
  onPreview,
  onCamera,
  onStatus,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const baseRef = useRef<RenderFrame | null>(null);
  const hitRef = useRef<RenderFrame | null>(null);
  const transformRef = useRef<ZoomTransform>(zoomIdentity);
  const zoomRef = useRef<ZoomBehavior<HTMLCanvasElement, unknown> | null>(null);
  const featuresRef = useRef<MapFeature[]>([]);
  const statesRef = useRef<MapFeature[]>([]);
  const projectorsRef = useRef<ReturnType<typeof projectors> | null>(null);
  const dimensionsRef = useRef({ width: 1, height: 1, ratio: 1 });
  const colorIdsRef = useRef<string[]>([]);
  const featureByIdRef = useRef(new Map<string, MapFeature>());
  const geometryByIdRef = useRef(new Map<string, MapFeature>());
  const detailByIdRef = useRef(new Map<string, MapFeature>());
  const detailRef = useRef(new Map<string, MapFeature[]>());
  const detailPendingRef = useRef(new Map<string, Promise<void>>());
  const detailFailuresRef = useRef(new Map<string, Error>());
  const manifestRef = useRef<MapManifest | null>(null);
  const loadedLevelRef = useRef<"tract" | "county" | "">("");
  const hitBuildRef = useRef(0);
  const drawFrameRef = useRef(0);
  const renderKeyRef = useRef("");
  const hitSemanticKeyRef = useRef("");
  const gestureRef = useRef(false);
  const zoomChangedRef = useRef(false);
  const pickingRef = useRef(false);
  const settleGenerationRef = useRef(0);
  const cameraInitializedRef = useRef(false);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const cursorRef = useRef({ x: 0.5, y: 0.5 });
  const rowsIdentityRef = useRef(rows);
  const rowsVersionRef = useRef(0);
  if (rowsIdentityRef.current !== rows) {
    rowsIdentityRef.current = rows;
    rowsVersionRef.current += 1;
  }
  const [geometryVersion, setGeometryVersion] = useState(0);
  const [error, setError] = useState("");
  const [reloadNonce, setReloadNonce] = useState(0);
  const scores = useMemo(() => new Map(rows.map((row) => [row.place_id, row])), [rows]);
  const scoresRef = useRef(scores);
  scoresRef.current = scores;

  const projectionFor = useCallback((item: MapFeature) => {
    const stateCode = String(item.properties?.state || item.id || "");
    return projectorsRef.current?.byState.get(stateCode) || projectorsRef.current?.main || null;
  }, []);

  const visibleStates = useCallback((transform = transformRef.current) => {
    const { width, height } = dimensionsRef.current;
    return new Set(statesRef.current.filter((item) => {
      const projection = projectionFor(item);
      if (!projection) return false;
      const [[x0, y0], [x1, y1]] = geoPath(projection).bounds(item);
      return x1 * transform.k + transform.x >= 0 && x0 * transform.k + transform.x <= width
        && y1 * transform.k + transform.y >= 0 && y0 * transform.k + transform.y <= height;
    }).map((item) => String(item.properties?.state || item.id)));
  }, [projectionFor]);

  const effectiveFeatures = useCallback((transform = transformRef.current) => {
    if (level !== "tract" || transform.k < DETAIL_ZOOM) return featuresRef.current;
    const visible = visibleStates(transform);
    const detailed = new Set([...visible].filter((code) => detailRef.current.has(code)));
    return [
      ...featuresRef.current.filter((item) => {
        const code = String(item.properties?.state);
        return visible.has(code) && !detailed.has(code);
      }),
      ...[...detailed].flatMap((code) => detailRef.current.get(code) || []),
    ];
  }, [level, visibleStates]);

  const drawTransformed = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { width, height, ratio } = dimensionsRef.current;
    const context = canvas.getContext("2d");
    if (!context) return;
    const transform = transformRef.current;
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    const frame = baseRef.current;
    if (frame) {
      const relative = relativeTransform(transform, frame.transform);
      context.setTransform(
        ratio * relative.k, 0, 0, ratio * relative.k,
        ratio * relative.x, ratio * relative.y,
      );
      context.drawImage(frame.canvas, 0, 0, frame.width, frame.height);
    } else {
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.fillStyle = "#10191e";
      context.fillRect(0, 0, width, height);
    }
    const selectedFeature = (
      level === "tract" && transform.k >= DETAIL_ZOOM
        ? detailByIdRef.current.get(selectedRef.current)
        : null
    ) || geometryByIdRef.current.get(selectedRef.current);
    const selectedProjection = selectedFeature ? projectionFor(selectedFeature) : null;
    if (selectedFeature && selectedProjection) {
      context.setTransform(
        ratio * transform.k, 0, 0, ratio * transform.k,
        ratio * transform.x, ratio * transform.y,
      );
      context.beginPath();
      geoPath(selectedProjection, context)(selectedFeature);
      context.strokeStyle = "#f7f4e8";
      context.lineWidth = 2.2 / transform.k;
      context.stroke();
    }
    const cursor = cursorRef.current;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.strokeStyle = "rgba(255,255,255,.9)";
    context.lineWidth = 1.5;
    context.beginPath();
    context.moveTo(cursor.x * width - 7, cursor.y * height);
    context.lineTo(cursor.x * width + 7, cursor.y * height);
    context.moveTo(cursor.x * width, cursor.y * height - 7);
    context.lineTo(cursor.x * width, cursor.y * height + 7);
    context.stroke();
  }, [level, projectionFor]);

  const drawBase = useCallback(() => {
    const { width, height, ratio } = dimensionsRef.current;
    const renderTransform = transformRef.current;
    const hitSemanticKey = [
      geometryVersion, rowsVersionRef.current, level, state, county, showUnranked, neutralOnly,
    ].join("|");
    const renderKey = [
      width, height, ratio, hitSemanticKey, renderTransform.k, renderTransform.x, renderTransform.y,
    ].join("|");
    if (renderKeyRef.current === renderKey) return;
    profile(`draw start ${renderKey}`);
    const base = document.createElement("canvas");
    const hit = document.createElement("canvas");
    for (const target of [base, hit]) {
      target.width = Math.max(1, Math.round(width * ratio));
      target.height = Math.max(1, Math.round(height * ratio));
    }
    const context = base.getContext("2d", { alpha: false });
    const hitContext = hit.getContext("2d", { willReadFrequently: true });
    if (!context || !hitContext || !projectorsRef.current) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.fillStyle = "#10191e";
    context.fillRect(0, 0, width, height);
    hitContext.setTransform(ratio, 0, 0, ratio, 0, 0);
    hitContext.clearRect(0, 0, width, height);
    const setMapTransform = (target: CanvasRenderingContext2D) => target.setTransform(
      ratio * renderTransform.k, 0, 0, ratio * renderTransform.k,
      ratio * renderTransform.x, ratio * renderTransform.y,
    );
    const missingPattern = hatch(context, renderTransform.k);
    const geographies = neutralOnly || rowsIdentityRef.current.length === 0 || loadedLevelRef.current !== level
      ? []
      : effectiveFeatures(renderTransform);
    const colorIds = geographies.map((item) => String(item.id || item.properties?.place_id || ""));
    const featureById = new Map(geographies.map((item, index) => [colorIds[index], item]));
    type PaintGroup = {
      items: MapFeature[];
      projection: GeoProjection;
      fill: string | CanvasPattern;
      alpha: number;
      stroke: string;
    };
    const visibleGroups: PaintGroup[] = [];
    const currentGroups = new Map<string, PaintGroup>();
    const paths = new Map<GeoProjection, ReturnType<typeof geoPath>>();
    const hitPaths = new Map<GeoProjection, ReturnType<typeof geoPath>>();
    const pathFor = (projection: GeoProjection) => {
      let path = paths.get(projection);
      if (!path) {
        path = geoPath(projection, context);
        paths.set(projection, path);
      }
      return path;
    };
    const hitPathFor = (projection: GeoProjection) => {
      let path = hitPaths.get(projection);
      if (!path) {
        path = geoPath(projection, hitContext);
        hitPaths.set(projection, path);
      }
      return path;
    };
    const hittable: Array<{ item: MapFeature; index: number; projection: GeoProjection }> = [];
    geographies.forEach((item, index) => {
      const placeId = colorIds[index];
      const featureState = String(item.properties?.state || "");
      const countyFips = String(item.properties?.county_fips || (level === "county" ? placeId : ""));
      const filtered = (state && featureState !== state) || (county && countyFips !== county);
      const score = scoresRef.current.get(placeId);
      const color = scoreColor(score?.risk_score ?? null);
      const projection = projectionFor(item);
      if (!projection) return;
      const fill = color || (score && showUnranked ? missingPattern : "#344149");
      const alpha = filtered ? 0.12 : score ? 1 : 0.34;
      const stroke = filtered ? "#233038" : "rgba(9,15,18,.54)";
      const projectionKey = projectorsRef.current?.byState.get(featureState) ? featureState : "main";
      const fillKey = typeof fill === "string" ? fill : "hatch";
      const groupKey = `${projectionKey}|${featureState}|${fillKey}|${alpha}|${stroke}`;
      let group = currentGroups.get(groupKey);
      if (!group || group.items.length >= 100) {
        group = { items: [], projection, fill, alpha, stroke };
        currentGroups.set(groupKey, group);
        visibleGroups.push(group);
      }
      group.items.push(item);
      if (!filtered && score && (score.risk_score !== null || showUnranked)) {
        hittable.push({ item, index, projection });
      }
    });
    profile("groups built");
    const buildId = ++hitBuildRef.current;
    if (hitSemanticKeyRef.current !== hitSemanticKey) pickingRef.current = false;
    let paintIndex = 0;
    const commitVisibleLayer = () => {
      if (buildId !== hitBuildRef.current) return;
      pickingRef.current = false;
      baseRef.current = { canvas: base, transform: renderTransform, width, height, ratio };
      renderKeyRef.current = renderKey;
      drawTransformed();
      profile("visible canvas painted");
      if (geographies.length) onStatus(`${geographies.length.toLocaleString()} ${level}s mapped`);
      if (neutralOnly || !geographies.length) {
        hitRef.current = null;
        colorIdsRef.current = [];
        featureByIdRef.current.clear();
        return;
      }
      let index = 0;
      const buildHitLayer = () => {
        if (buildId !== hitBuildRef.current) return;
        if (gestureRef.current) {
          window.requestAnimationFrame(buildHitLayer);
          return;
        }
        setMapTransform(hitContext);
        const started = performance.now();
        while (index < hittable.length && performance.now() - started < 24) {
          const target = hittable[index];
          hitContext.beginPath();
          hitPathFor(target.projection)(target.item);
          hitContext.fillStyle = hitColor(target.index);
          hitContext.fill();
          index += 1;
        }
        if (index < hittable.length) window.requestAnimationFrame(buildHitLayer);
        else {
          hitRef.current = { canvas: hit, transform: renderTransform, width, height, ratio };
          colorIdsRef.current = colorIds;
          featureByIdRef.current = featureById;
          hitSemanticKeyRef.current = hitSemanticKey;
          pickingRef.current = true;
          onStatus(`${geographies.length.toLocaleString()} ${level}s interactive`);
        }
      };
      window.requestAnimationFrame(buildHitLayer);
    };
    const finishVisibleLayer = () => {
      if (buildId !== hitBuildRef.current) return;
      profile("groups painted");
      context.globalAlpha = 1;
      let stateIndex = 0;
      const paintStateLayer = () => {
        if (buildId !== hitBuildRef.current) return;
        if (gestureRef.current) {
          window.requestAnimationFrame(paintStateLayer);
          return;
        }
        const started = performance.now();
        while (stateIndex < statesRef.current.length && performance.now() - started < 24) {
          const item = statesRef.current[stateIndex++];
          const projection = projectionFor(item);
          if (!projection) continue;
          const path = pathFor(projection);
          setMapTransform(context);
          context.beginPath();
          path(item);
          context.strokeStyle = "rgba(223,231,230,.52)";
          context.lineWidth = 0.8 / renderTransform.k;
          context.stroke();
          const stateCode = String(item.properties?.state || item.id || "");
          const label = item.properties?.label;
          const projectedLabel = label ? projection(label) : null;
          const [x, y] = projectedLabel || path.centroid(item);
          const [[x0, y0], [x1, y1]] = path.bounds(item);
          if (!Number.isFinite(x) || !Number.isFinite(y)
            || (((x1 - x0) * renderTransform.k < 24 || (y1 - y0) * renderTransform.k < 16) && !TERRITORY_BOXES[stateCode])) continue;
          const screenX = x * renderTransform.k + renderTransform.x;
          const screenY = y * renderTransform.k + renderTransform.y;
          if (screenX < -24 || screenX > width + 24 || screenY < -16 || screenY > height + 16) continue;
          context.setTransform(ratio, 0, 0, ratio, 0, 0);
          context.font = "600 10px ui-sans-serif, system-ui";
          context.textAlign = "center";
          context.fillStyle = "rgba(231,238,236,.72)";
          context.fillText(stateCode, screenX, screenY);
        }
        if (stateIndex < statesRef.current.length) window.requestAnimationFrame(paintStateLayer);
        else commitVisibleLayer();
      };
      window.requestAnimationFrame(paintStateLayer);
    };
    const paintVisibleLayer = () => {
      if (buildId !== hitBuildRef.current) return;
      const started = performance.now();
      setMapTransform(context);
      while (paintIndex < visibleGroups.length && performance.now() - started < 38) {
        const group = visibleGroups[paintIndex];
        context.beginPath();
        pathFor(group.projection)(collection(group.items));
        context.globalAlpha = group.alpha;
        context.fillStyle = group.fill;
        context.fill();
        if (level === "county" || renderTransform.k >= DETAIL_ZOOM) {
          context.strokeStyle = group.stroke;
          context.lineWidth = (level === "county" ? 0.5 : 0.18) / renderTransform.k;
          context.stroke();
        }
        paintIndex += 1;
      }
      if (paintIndex < visibleGroups.length) window.requestAnimationFrame(paintVisibleLayer);
      else finishVisibleLayer();
    };
    if (geographies.length) onStatus(`Rendering ${geographies.length.toLocaleString()} ${level}s`);
    window.requestAnimationFrame(paintVisibleLayer);
  }, [county, drawTransformed, effectiveFeatures, geometryVersion, level, neutralOnly, onStatus, projectionFor, showUnranked, state]);

  const scheduleDraw = useCallback(() => {
    window.cancelAnimationFrame(drawFrameRef.current);
    drawFrameRef.current = window.requestAnimationFrame(drawBase);
  }, [drawBase]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setError("");
      ++hitBuildRef.current;
      ++settleGenerationRef.current;
      featuresRef.current = [];
      detailRef.current.clear();
      detailFailuresRef.current.clear();
      geometryByIdRef.current.clear();
      detailByIdRef.current.clear();
      loadedLevelRef.current = "";
      baseRef.current = null;
      hitRef.current = null;
      pickingRef.current = false;
      renderKeyRef.current = "";
      hitSemanticKeyRef.current = "";
      setGeometryVersion((value) => value + 1);
      onStatus("Loading map boundaries");
      try {
        const manifestResponse = await fetch(manifestUrl);
        if (!manifestResponse.ok) throw new Error("Map asset manifest could not be loaded");
        const manifest = await manifestResponse.json() as MapManifest;
        const statesAsset = nationalAsset(manifest, "state");
        const geographyAsset = neutralOnly ? null : nationalAsset(manifest, level);
        if (!statesAsset || (!neutralOnly && !geographyAsset)) throw new Error("Required map asset is missing");
        const urls = [statesAsset, ...(geographyAsset ? [geographyAsset] : [])].map(
          (asset) => `/map-assets/${asset.filename}`,
        );
        const payloads = await Promise.all(urls.map(async (url) => {
          const response = await fetch(url);
          if (!response.ok) throw new Error(`Map boundary request failed (${response.status})`);
          return response.json() as Promise<Topology>;
        }));
        profile("topologies fetched");
        if (cancelled) return;
        manifestRef.current = manifest;
        statesRef.current = featuresFrom(payloads[0]);
        featuresRef.current = payloads[1] ? featuresFrom(payloads[1]) : [];
        geometryByIdRef.current = new Map(featuresRef.current.map((item) => [
          String(item.id || item.properties?.place_id || ""), item,
        ]));
        loadedLevelRef.current = level;
        profile("topologies converted");
        setGeometryVersion((value) => value + 1);
        onStatus(neutralOnly ? "Map outlines ready" : "Map boundaries loaded");
      } catch (caught) {
        if (!cancelled) {
          setError((caught as Error).message);
          onStatus("Map boundaries failed to load");
        }
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [level, manifestUrl, neutralOnly, onStatus, reloadNonce]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      const width = Math.max(1, bounds.width);
      const height = Math.max(1, bounds.height);
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const previous = dimensionsRef.current;
      let nextTransform = transformRef.current;
      if (!cameraInitializedRef.current) {
        nextTransform = transformFromCamera(initialCamera, width, height);
        cameraInitializedRef.current = true;
      } else if (width !== previous.width || height !== previous.height) {
        nextTransform = transformFromCamera(
          cameraFromTransform(transformRef.current, previous.width, previous.height),
          width,
          height,
        );
      }
      dimensionsRef.current = { width, height, ratio };
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      projectorsRef.current = projectors(width, height, statesRef.current);
      if (nextTransform !== transformRef.current) {
        transformRef.current = nextTransform;
        if (zoomRef.current) {
          select(canvas).call(zoomRef.current.transform, nextTransform);
          return;
        }
      }
      drawTransformed();
      scheduleDraw();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();
    return () => observer.disconnect();
  }, [drawTransformed, geometryVersion, initialCamera, scheduleDraw]);

  const pick = useCallback((x: number, y: number) => {
    const frame = hitRef.current;
    if (gestureRef.current || !pickingRef.current || !frame) return "";
    const live = transformRef.current;
    const mapX = (x - live.x) / live.k;
    const mapY = (y - live.y) / live.k;
    const hitX = mapX * frame.transform.k + frame.transform.x;
    const hitY = mapY * frame.transform.k + frame.transform.y;
    if (hitX < 0 || hitY < 0 || hitX >= frame.width || hitY >= frame.height) return "";
    const context = frame.canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return "";
    const pixel = context.getImageData(Math.round(hitX * frame.ratio), Math.round(hitY * frame.ratio), 1, 1).data;
    const value = pixel[0] + (pixel[1] << 8) + (pixel[2] << 16);
    const placeId = value ? colorIdsRef.current[value - 1] || "" : "";
    const item = featureByIdRef.current.get(placeId);
    const featureState = String(item?.properties?.state || "");
    const countyFips = String(item?.properties?.county_fips || (level === "county" ? placeId : ""));
    if ((state && featureState !== state) || (county && countyFips !== county)) return "";
    return placeId;
  }, [county, level, state]);

  const loadVisibleDetails = useCallback(async () => {
    if (level !== "tract" || transformRef.current.k < DETAIL_ZOOM || !manifestRef.current) return;
    await Promise.all([...visibleStates()].map((code) => {
      if (detailRef.current.has(code)) return Promise.resolve();
      const failure = detailFailuresRef.current.get(code);
      if (failure) return Promise.reject(failure);
      const existing = detailPendingRef.current.get(code);
      if (existing) return existing;
      const request = (async () => {
        const asset = detailAsset(manifestRef.current!, code);
        if (!asset) throw new Error(`Detailed tract asset is missing for ${code}`);
        const response = await fetch(`/map-assets/${asset.filename}`);
        if (!response.ok) throw new Error(`Detailed tract request failed for ${code} (${response.status})`);
        const detailed = featuresFrom(await response.json() as Topology);
        detailRef.current.set(code, detailed);
        for (const item of detailed) {
          detailByIdRef.current.set(String(item.id || item.properties?.place_id || ""), item);
        }
      })().catch((caught: unknown) => {
        const failure = caught instanceof Error ? caught : new Error("Detailed tract request failed");
        detailFailuresRef.current.set(code, failure);
        throw failure;
      }).finally(() => detailPendingRef.current.delete(code));
      detailPendingRef.current.set(code, request);
      return request;
    }));
  }, [level, visibleStates]);

  const settleView = useCallback(async () => {
    const generation = ++settleGenerationRef.current;
    try {
      await loadVisibleDetails();
      if (generation !== settleGenerationRef.current) return;
      setError("");
    } catch (caught) {
      if (generation !== settleGenerationRef.current) return;
      setError((caught as Error).message);
      onStatus("Detailed tract boundaries failed to load");
    }
    if (generation === settleGenerationRef.current) scheduleDraw();
  }, [loadVisibleDetails, onStatus, scheduleDraw]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let dragged = false;
    const starts = new Map<number, [number, number]>();
    const behavior = zoom<HTMLCanvasElement, unknown>()
      .scaleExtent([1, 12])
      .filter((event) => !event.button && (!event.ctrlKey || event.type === "wheel"));
    zoomRef.current = behavior;
    select(canvas).call(behavior).call(behavior.transform, transformRef.current).on("dblclick.zoom", null);
    behavior
      .on("start", () => {
        gestureRef.current = true;
        zoomChangedRef.current = false;
        onPreview(null);
      })
      .on("zoom", (event) => {
        if (!zoomChangedRef.current) {
          zoomChangedRef.current = true;
          ++hitBuildRef.current;
          ++settleGenerationRef.current;
          pickingRef.current = false;
          onStatus("Settling map view");
          window.cancelAnimationFrame(drawFrameRef.current);
        }
        transformRef.current = event.transform;
        drawTransformed();
      })
      .on("end", () => {
        gestureRef.current = false;
        if (!zoomChangedRef.current) return;
        const { width, height } = dimensionsRef.current;
        onCamera(cameraFromTransform(transformRef.current, width, height));
        void settleView();
      });
    const down = (event: PointerEvent) => {
      if (!starts.size) dragged = false;
      starts.set(event.pointerId, [event.clientX, event.clientY]);
    };
    const trackDrag = (event: PointerEvent) => {
      const start = starts.get(event.pointerId);
      if (start && Math.hypot(event.clientX - start[0], event.clientY - start[1]) > 5) dragged = true;
    };
    const up = (event: PointerEvent) => {
      const start = starts.get(event.pointerId);
      starts.delete(event.pointerId);
      if (!start || Math.hypot(event.clientX - start[0], event.clientY - start[1]) > 5) dragged = true;
    };
    const clicked = (event: MouseEvent) => {
      if (starts.size || dragged) return;
      const bounds = canvas.getBoundingClientRect();
      const placeId = pick(event.clientX - bounds.left, event.clientY - bounds.top);
      if (placeId) onSelect(placeId);
    };
    const cancel = (event: PointerEvent) => { starts.delete(event.pointerId); dragged = true; };
    const move = (event: PointerEvent) => {
      trackDrag(event);
      if (event.buttons || starts.has(event.pointerId)) return;
      const bounds = canvas.getBoundingClientRect();
      const x = event.clientX - bounds.left;
      const y = event.clientY - bounds.top;
      const placeId = pick(x, y);
      if (!placeId) return onPreview(null);
      const item = featureByIdRef.current.get(placeId);
      onPreview({
        placeId,
        name: String(item?.properties?.name || placeId),
        state: String(item?.properties?.state || ""),
        score: scoresRef.current.get(placeId) || null,
        x,
        y,
      });
    };
    const leave = () => onPreview(null);
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", cancel);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("click", clicked);
    canvas.addEventListener("pointerleave", leave);
    return () => {
      select(canvas).on(".zoom", null);
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("pointercancel", cancel);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("click", clicked);
      canvas.removeEventListener("pointerleave", leave);
    };
  }, [drawTransformed, onCamera, onPreview, onSelect, onStatus, pick, settleView]);

  useEffect(() => {
    if (geometryVersion) void settleView();
  }, [geometryVersion, settleView]);

  useEffect(() => {
    scheduleDraw();
    return () => window.cancelAnimationFrame(drawFrameRef.current);
  }, [geometryVersion, rows, scheduleDraw]);

  useEffect(() => {
    drawTransformed();
  }, [drawTransformed, selected]);

  useEffect(() => {
    if (!focusTarget || !featuresRef.current.length || !canvasRef.current || !zoomRef.current) return;
    let targets: MapFeature[] = [];
    if (focusTarget.kind === "place") {
      const target = geometryByIdRef.current.get(focusTarget.id);
      targets = target ? [target] : [];
    } else if (focusTarget.kind === "state") {
      targets = featuresRef.current.filter((item) => item.properties?.state === focusTarget.id);
    } else {
      targets = featuresRef.current.filter((item) => (
        String(item.properties?.county_fips || (level === "county" ? item.id : "")) === focusTarget.id
      ));
    }
    if (!targets.length) return;
    const projection = projectionFor(targets[0]);
    if (!projection) return;
    const path = geoPath(projection);
    const bounds = targets.map((item) => path.bounds(item));
    const x0 = Math.min(...bounds.map((item) => item[0][0]));
    const y0 = Math.min(...bounds.map((item) => item[0][1]));
    const x1 = Math.max(...bounds.map((item) => item[1][0]));
    const y1 = Math.max(...bounds.map((item) => item[1][1]));
    const { width, height } = dimensionsRef.current;
    const scale = Math.max(1, Math.min(10, 0.76 / Math.max((x1 - x0) / width, (y1 - y0) / height, 0.01)));
    const target = zoomIdentity.translate(width / 2 - scale * (x0 + x1) / 2, height / 2 - scale * (y0 + y1) / 2).scale(scale);
    select(canvasRef.current).call(zoomRef.current.transform, target);
  }, [focusTarget, geometryVersion, level, projectionFor]);

  useEffect(() => {
    if (!cameraTarget || !canvasRef.current || !zoomRef.current) return;
    const { width, height } = dimensionsRef.current;
    const target = transformFromCamera(cameraTarget, width, height);
    select(canvasRef.current).call(zoomRef.current.transform, target);
  }, [cameraTarget]);

  function keyboard(event: React.KeyboardEvent<HTMLCanvasElement>) {
    const step = event.shiftKey ? 0.08 : 0.035;
    if (event.key.startsWith("Arrow")) {
      event.preventDefault();
      const cursor = cursorRef.current;
      cursorRef.current = {
        x: Math.max(0, Math.min(1, cursor.x + (event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0))),
        y: Math.max(0, Math.min(1, cursor.y + (event.key === "ArrowDown" ? step : event.key === "ArrowUp" ? -step : 0))),
      };
      drawTransformed();
    } else if ((event.key === "+" || event.key === "=") && zoomRef.current) {
      event.preventDefault();
      select(event.currentTarget).call(zoomRef.current.scaleBy, 1.5);
    } else if (event.key === "-" && zoomRef.current) {
      event.preventDefault();
      select(event.currentTarget).call(zoomRef.current.scaleBy, 1 / 1.5);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const { width, height } = dimensionsRef.current;
      const cursor = cursorRef.current;
      const placeId = pick(cursor.x * width, cursor.y * height);
      if (placeId) onSelect(placeId);
    }
  }

  const zoomBy = (factor: number) => {
    const canvas = canvasRef.current;
    if (canvas && zoomRef.current) select(canvas).call(zoomRef.current.scaleBy, factor);
  };
  const reset = () => {
    const canvas = canvasRef.current;
    if (canvas && zoomRef.current) select(canvas).call(zoomRef.current.transform, zoomIdentity);
  };

  return <div className="map-stage" data-level={level}>
    <canvas
      ref={canvasRef}
      className="risk-canvas"
      role="img"
      tabIndex={0}
      aria-label={`Focusable USA ${level} risk map. Lower FEMA ALR_NPCTL is better. Use arrow keys to move the focus cursor, plus and minus to zoom, and Enter to select.`}
      onKeyDown={keyboard}
    />
    <div className="map-zoom" aria-label="Map controls">
      <button type="button" onClick={() => zoomBy(1.5)} aria-label="Zoom in" title="Zoom in">+</button>
      <button type="button" onClick={() => zoomBy(1 / 1.5)} aria-label="Zoom out" title="Zoom out">−</button>
      <button type="button" onClick={reset} aria-label="Reset map" title="Reset map">⌂</button>
    </div>
    {error && <div className="map-error" role="alert"><strong>Map unavailable</strong><span>{error}</span><button className="secondary" onClick={() => setReloadNonce((value) => value + 1)}>Retry map</button></div>}
  </div>;
}
