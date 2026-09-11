import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { geoAlbersUsa, geoArea, geoMercator, geoPath, type GeoProjection } from "d3-geo";
import { select } from "d3-selection";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import { feature as topoFeature } from "topojson-client";
import type { Feature, FeatureCollection, Geometry, GeoJsonProperties } from "geojson";
import type { GeometryCollection, Topology } from "topojson-specification";
import { cameraFromTransform, detailAsset, nationalAsset, scoreColor, type CameraState } from "./map";
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

function hatch(context: CanvasRenderingContext2D): CanvasPattern | string {
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
  return context.createPattern(tile, "repeat") || "#566169";
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
  const baseRef = useRef<HTMLCanvasElement>(document.createElement("canvas"));
  const hitRef = useRef<HTMLCanvasElement>(document.createElement("canvas"));
  const transformRef = useRef<ZoomTransform>(zoomIdentity);
  const zoomRef = useRef<ZoomBehavior<HTMLCanvasElement, unknown> | null>(null);
  const featuresRef = useRef<MapFeature[]>([]);
  const statesRef = useRef<MapFeature[]>([]);
  const projectorsRef = useRef<ReturnType<typeof projectors> | null>(null);
  const dimensionsRef = useRef({ width: 1, height: 1, ratio: 1 });
  const colorIdsRef = useRef<string[]>([]);
  const featureByIdRef = useRef(new Map<string, MapFeature>());
  const detailRef = useRef(new Map<string, MapFeature[]>());
  const manifestRef = useRef<MapManifest | null>(null);
  const loadedLevelRef = useRef<"tract" | "county" | "">("");
  const hitBuildRef = useRef(0);
  const drawFrameRef = useRef(0);
  const renderKeyRef = useRef("");
  const gestureRef = useRef(false);
  const rowsIdentityRef = useRef(rows);
  const rowsVersionRef = useRef(0);
  if (rowsIdentityRef.current !== rows) {
    rowsIdentityRef.current = rows;
    rowsVersionRef.current += 1;
  }
  const [geometryVersion, setGeometryVersion] = useState(0);
  const [error, setError] = useState("");
  const [reloadNonce, setReloadNonce] = useState(0);
  const [cursor, setCursor] = useState({ x: 0.5, y: 0.5 });
  const scores = useMemo(() => new Map(rows.map((row) => [row.place_id, row])), [rows]);
  const scoresRef = useRef(scores);
  scoresRef.current = scores;

  const effectiveFeatures = useCallback(() => {
    if (level !== "tract" || detailRef.current.size === 0) return featuresRef.current;
    const loaded = new Set(detailRef.current.keys());
    return [
      ...featuresRef.current.filter((item) => !loaded.has(String(item.properties?.state))),
      ...[...detailRef.current.values()].flat(),
    ];
  }, [level]);

  const projectionFor = useCallback((item: MapFeature) => {
    const stateCode = String(item.properties?.state || item.id || "");
    return projectorsRef.current?.byState.get(stateCode) || projectorsRef.current?.main || null;
  }, []);

  const drawTransformed = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { width, height, ratio } = dimensionsRef.current;
    const context = canvas.getContext("2d");
    if (!context) return;
    const transform = transformRef.current;
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.setTransform(
      ratio * transform.k, 0, 0, ratio * transform.k,
      ratio * transform.x, ratio * transform.y,
    );
    context.drawImage(baseRef.current, 0, 0, width, height);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.strokeStyle = "rgba(255,255,255,.9)";
    context.lineWidth = 1.5;
    context.beginPath();
    context.moveTo(cursor.x * width - 7, cursor.y * height);
    context.lineTo(cursor.x * width + 7, cursor.y * height);
    context.moveTo(cursor.x * width, cursor.y * height - 7);
    context.lineTo(cursor.x * width, cursor.y * height + 7);
    context.stroke();
  }, [cursor]);

  const drawBase = useCallback(() => {
    const { width, height, ratio } = dimensionsRef.current;
    const renderKey = [width, height, ratio, geometryVersion, rowsVersionRef.current, level, state, county, showUnranked, selected, neutralOnly].join("|");
    if (renderKeyRef.current === renderKey) return;
    renderKeyRef.current = renderKey;
    profile(`draw start ${renderKey}`);
    const base = baseRef.current;
    const hit = hitRef.current;
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
    const missingPattern = hatch(context);
    const geographies = neutralOnly || rowsIdentityRef.current.length === 0 || loadedLevelRef.current !== level
      ? []
      : effectiveFeatures();
    colorIdsRef.current = geographies.map((item) => String(item.id || item.properties?.place_id || ""));
    featureByIdRef.current = new Map(geographies.map((item, index) => [colorIdsRef.current[index], item]));
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
      const placeId = colorIdsRef.current[index];
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
    let paintIndex = 0;
    const finishVisibleLayer = () => {
      if (buildId !== hitBuildRef.current) return;
      profile("groups painted");
      context.globalAlpha = 1;
      const selectedFeature = selected ? featureByIdRef.current.get(selected) : null;
      if (selectedFeature) {
        const projection = projectionFor(selectedFeature);
        if (projection) {
          context.beginPath();
          pathFor(projection)(selectedFeature);
          context.strokeStyle = "#f7f4e8";
          context.lineWidth = 2.2;
          context.stroke();
        }
      }
      drawTransformed();
      profile("visible canvas painted");
      if (geographies.length) onStatus(`${geographies.length.toLocaleString()} ${level}s mapped`);
      let index = 0;
      const buildHitLayer = () => {
        if (buildId !== hitBuildRef.current) return;
        if (gestureRef.current) {
          window.requestAnimationFrame(buildHitLayer);
          return;
        }
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
        else if (geographies.length) onStatus(`${geographies.length.toLocaleString()} ${level}s interactive`);
      };
      if (!neutralOnly && !geographies.length) return;
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
          context.beginPath();
          path(item);
          context.strokeStyle = "rgba(223,231,230,.52)";
          context.lineWidth = 0.8;
          context.stroke();
          const stateCode = String(item.properties?.state || item.id || "");
          const label = item.properties?.label;
          const projectedLabel = label ? projection(label) : null;
          const [x, y] = projectedLabel || path.centroid(item);
          const [[x0, y0], [x1, y1]] = path.bounds(item);
          if (!Number.isFinite(x) || !Number.isFinite(y) || ((x1 - x0 < 24 || y1 - y0 < 16) && !TERRITORY_BOXES[stateCode])) continue;
          context.font = "600 10px ui-sans-serif, system-ui";
          context.textAlign = "center";
          context.fillStyle = "rgba(231,238,236,.72)";
          context.fillText(stateCode, x, y);
        }
        drawTransformed();
        if (stateIndex < statesRef.current.length) window.requestAnimationFrame(paintStateLayer);
        else window.requestAnimationFrame(buildHitLayer);
      };
      window.requestAnimationFrame(paintStateLayer);
    };
    const paintVisibleLayer = () => {
      if (buildId !== hitBuildRef.current) return;
      const started = performance.now();
      while (paintIndex < visibleGroups.length && performance.now() - started < 38) {
        const group = visibleGroups[paintIndex];
        context.beginPath();
        pathFor(group.projection)(collection(group.items));
        context.globalAlpha = group.alpha;
        context.fillStyle = group.fill;
        context.fill();
        if (level === "county" || detailRef.current.size > 0) {
          context.strokeStyle = group.stroke;
          context.lineWidth = level === "county" ? 0.5 : 0.18;
          context.stroke();
        }
        paintIndex += 1;
      }
      drawTransformed();
      if (paintIndex < visibleGroups.length) window.requestAnimationFrame(paintVisibleLayer);
      else finishVisibleLayer();
    };
    if (geographies.length) onStatus(`Rendering ${geographies.length.toLocaleString()} ${level}s`);
    window.requestAnimationFrame(paintVisibleLayer);
  }, [county, drawTransformed, effectiveFeatures, geometryVersion, level, neutralOnly, onStatus, projectionFor, selected, showUnranked, state]);

  const scheduleDraw = useCallback(() => {
    window.cancelAnimationFrame(drawFrameRef.current);
    drawFrameRef.current = window.requestAnimationFrame(drawBase);
  }, [drawBase]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setError("");
      featuresRef.current = [];
      detailRef.current.clear();
      loadedLevelRef.current = "";
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
      dimensionsRef.current = { width, height, ratio };
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      projectorsRef.current = projectors(width, height, statesRef.current);
      if (transformRef.current === zoomIdentity) {
        transformRef.current = zoomIdentity
          .translate(width / 2 - initialCamera.cx * width * initialCamera.z, height / 2 - initialCamera.cy * height * initialCamera.z)
          .scale(initialCamera.z);
      }
      scheduleDraw();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();
    return () => observer.disconnect();
  }, [geometryVersion, initialCamera, scheduleDraw]);

  const pick = useCallback((x: number, y: number) => {
    if (gestureRef.current) return "";
    const { ratio } = dimensionsRef.current;
    const point = transformRef.current.invert([x, y]);
    const context = hitRef.current.getContext("2d", { willReadFrequently: true });
    if (!context || point[0] < 0 || point[1] < 0) return "";
    const pixel = context.getImageData(Math.round(point[0] * ratio), Math.round(point[1] * ratio), 1, 1).data;
    const value = pixel[0] + (pixel[1] << 8) + (pixel[2] << 16);
    const placeId = value ? colorIdsRef.current[value - 1] || "" : "";
    const item = featureByIdRef.current.get(placeId);
    const featureState = String(item?.properties?.state || "");
    const countyFips = String(item?.properties?.county_fips || (level === "county" ? placeId : ""));
    if ((state && featureState !== state) || (county && countyFips !== county)) return "";
    return placeId;
  }, [county, level, state]);

  const loadVisibleDetails = useCallback(async () => {
    if (level !== "tract" || transformRef.current.k < 4 || !manifestRef.current) return;
    const { width, height } = dimensionsRef.current;
    const visible = statesRef.current.filter((item) => {
      const projection = projectionFor(item);
      if (!projection) return false;
      const [[x0, y0], [x1, y1]] = geoPath(projection).bounds(item);
      const transform = transformRef.current;
      return x1 * transform.k + transform.x >= 0 && x0 * transform.k + transform.x <= width
        && y1 * transform.k + transform.y >= 0 && y0 * transform.k + transform.y <= height;
    }).map((item) => String(item.properties?.state || item.id));
    const pending = visible.filter((code) => !detailRef.current.has(code));
    try {
      await Promise.all(pending.map(async (code) => {
        const asset = detailAsset(manifestRef.current!, code);
        if (!asset) throw new Error(`Detailed tract asset is missing for ${code}`);
        const response = await fetch(`/map-assets/${asset.filename}`);
        if (!response.ok) throw new Error(`Detailed tract request failed for ${code} (${response.status})`);
        detailRef.current.set(code, featuresFrom(await response.json() as Topology));
      }));
      if (pending.length) {
        setGeometryVersion((value) => value + 1);
        onStatus(`Detailed tract boundaries loaded for ${pending.join(", ")}`);
      }
    } catch (caught) {
      setError((caught as Error).message);
      onStatus("Detailed tract boundaries failed to load");
    }
  }, [level, onStatus, projectionFor]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let dragged = false;
    const starts = new Map<number, [number, number]>();
    const behavior = zoom<HTMLCanvasElement, unknown>()
      .scaleExtent([1, 12])
      .filter((event) => !event.button && (!event.ctrlKey || event.type === "wheel"))
      .on("start", () => { gestureRef.current = true; onPreview(null); })
      .on("zoom", (event) => { transformRef.current = event.transform; drawTransformed(); })
      .on("end", () => {
        gestureRef.current = false;
        const { width, height } = dimensionsRef.current;
        onCamera(cameraFromTransform(transformRef.current, width, height));
        void loadVisibleDetails();
      });
    zoomRef.current = behavior;
    select(canvas).call(behavior).on("dblclick.zoom", null);
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
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", cancel);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("click", clicked);
    canvas.addEventListener("pointerleave", () => onPreview(null));
    return () => {
      select(canvas).on(".zoom", null);
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("pointercancel", cancel);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("click", clicked);
    };
  }, [drawTransformed, effectiveFeatures, loadVisibleDetails, onCamera, onPreview, onSelect, pick]);

  useEffect(() => {
    if (geometryVersion && transformRef.current.k >= 4) void loadVisibleDetails();
  }, [geometryVersion, loadVisibleDetails]);

  useEffect(() => {
    scheduleDraw();
    return () => window.cancelAnimationFrame(drawFrameRef.current);
  }, [geometryVersion, rows, scheduleDraw]);

  useEffect(() => {
    if (!focusTarget || !featuresRef.current.length || !canvasRef.current || !zoomRef.current) return;
    let targets: MapFeature[] = [];
    if (focusTarget.kind === "place") {
      targets = effectiveFeatures().filter((item) => String(item.id) === focusTarget.id);
    } else if (focusTarget.kind === "state") {
      targets = effectiveFeatures().filter((item) => item.properties?.state === focusTarget.id);
    } else {
      targets = effectiveFeatures().filter((item) => (
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
  }, [effectiveFeatures, focusTarget, geometryVersion, level, projectionFor]);

  useEffect(() => {
    if (!cameraTarget || !canvasRef.current || !zoomRef.current) return;
    const { width, height } = dimensionsRef.current;
    const target = zoomIdentity
      .translate(width / 2 - cameraTarget.cx * width * cameraTarget.z, height / 2 - cameraTarget.cy * height * cameraTarget.z)
      .scale(cameraTarget.z);
    select(canvasRef.current).call(zoomRef.current.transform, target);
  }, [cameraTarget]);

  function keyboard(event: React.KeyboardEvent<HTMLCanvasElement>) {
    const step = event.shiftKey ? 0.08 : 0.035;
    if (event.key.startsWith("Arrow")) {
      event.preventDefault();
      setCursor((value) => ({
        x: Math.max(0, Math.min(1, value.x + (event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0))),
        y: Math.max(0, Math.min(1, value.y + (event.key === "ArrowDown" ? step : event.key === "ArrowUp" ? -step : 0))),
      }));
    } else if ((event.key === "+" || event.key === "=") && zoomRef.current) {
      event.preventDefault();
      select(event.currentTarget).call(zoomRef.current.scaleBy, 1.5);
    } else if (event.key === "-" && zoomRef.current) {
      event.preventDefault();
      select(event.currentTarget).call(zoomRef.current.scaleBy, 1 / 1.5);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const { width, height } = dimensionsRef.current;
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
      <button type="button" onClick={() => zoomBy(1.5)} aria-label="Zoom in">+</button>
      <button type="button" onClick={() => zoomBy(1 / 1.5)} aria-label="Zoom out">−</button>
      <button type="button" onClick={reset} aria-label="Reset map">⌂</button>
    </div>
    {error && <div className="map-error" role="alert"><strong>Map unavailable</strong><span>{error}</span><button className="secondary" onClick={() => setReloadNonce((value) => value + 1)}>Retry map</button></div>}
  </div>;
}
