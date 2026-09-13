/// <reference lib="webworker" />

import { geoPath, type GeoProjection } from "d3-geo";
import {
  boundsIntersect,
  buildSpatialGrid,
  detailEvictions,
  featureId,
  gridCandidates,
  mergeBounds,
  projectionFor,
  projectors,
  stateCode,
  TERRITORY_BOXES,
  type Bounds,
  type MapFeature,
  type ProjectedFeature,
  type Projectors,
  type SpatialGrid,
} from "./mapGeometry";
import { metricValueColor } from "./map";
import type {
  LoaderCommand,
  LoaderEvent,
  MapPickPreview,
  MapSemantics,
  MapTransform,
  ProfileEntry,
  RendererCommand,
  RendererEvent,
} from "./mapWorkerProtocol";
import type { Geography, MapScores, Metric } from "./types";

declare const self: DedicatedWorkerGlobalScope;

const DETAIL_DISPLAY_ZOOM = 4;
const DETAIL_PREFETCH_ZOOM = 3;
const MAX_NON_VISIBLE_DETAIL_STATES = 24;
const SLICE_MS = 1;
const RENDER_SLICE_MS = 1;

interface PaintStyle {
  fill: string;
  alpha: number;
  stroke: string;
}

interface PaintGroup extends PaintStyle {
  path: Path2D;
  features: number;
}

interface StylePlan {
  key: string;
  groups: PaintGroup[];
}

interface ViewportPlan {
  key: string;
  groups: PaintGroup[];
  outlines: PaintGroup[];
}

interface FeatureRecord extends ProjectedFeature {
  path: Path2D;
  scoreIndex: number | null;
}

interface Partition {
  state: string;
  detail: boolean;
  revision: number;
  projection: GeoProjection;
  records: FeatureRecord[];
  grid: SpatialGrid;
  bounds: Bounds;
  stylePlans: Map<string, StylePlan>;
  lastUsed: number;
}

interface StateMeta {
  code: string;
  sourceIndex: number;
  feature: MapFeature;
  projection: GeoProjection;
  path: Path2D;
  bounds: Bounds;
  label: [number, number] | null;
}

interface Snapshot {
  id: number;
  datasetGeneration: number;
  viewportGeneration: number;
  cameraGeneration: number;
  semanticGeneration: number;
  camera: MapTransform;
  semantics: MapSemantics;
  originX: number;
  originY: number;
  partitions: Partition[];
  featureCount: number;
  interactive: boolean;
}

class Cancelled extends Error {}

class TrackingPath {
  private minX = Infinity;
  private minY = Infinity;
  private maxX = -Infinity;
  private maxY = -Infinity;

  constructor(private readonly path: Path2D) {}

  private point(x: number, y: number) {
    this.minX = Math.min(this.minX, x);
    this.minY = Math.min(this.minY, y);
    this.maxX = Math.max(this.maxX, x);
    this.maxY = Math.max(this.maxY, y);
  }

  moveTo(x: number, y: number) { this.point(x, y); this.path.moveTo(x, y); }
  lineTo(x: number, y: number) { this.point(x, y); this.path.lineTo(x, y); }
  closePath() { this.path.closePath(); }
  arc(x: number, y: number, radius: number, startAngle: number, endAngle: number) {
    this.point(x - radius, y - radius);
    this.point(x + radius, y + radius);
    this.path.arc(x, y, radius, startAngle, endAngle);
  }
  rect(x: number, y: number, width: number, height: number) {
    this.point(x, y);
    this.point(x + width, y + height);
    this.path.rect(x, y, width, height);
  }
  result(): Bounds {
    return Number.isFinite(this.minX)
      ? [[this.minX, this.minY], [this.maxX, this.maxY]]
      : [[0, 0], [0, 0]];
  }
}

let loaderPort: MessagePort | null = null;
let datasetGeneration = 0;
let viewportGeneration = 0;
let cameraGeneration = 0;
let semanticGeneration = 0;
let level: Geography = "tract";
let width = 1;
let height = 1;
let ratio = 1;
let camera: MapTransform = { k: 1, x: 0, y: 0 };
let semantics: MapSemantics = {
  metric: "fema", state: "", county: "", showUnranked: false, mountainMin: null, neutralOnly: false,
};
let selected = "";
let scores: MapScores | null = null;
let scoreIndexes = new Map<string, number>();
let rawStates: MapFeature[] = [];
let rawFeatures: MapFeature[] = [];
let rawDetails = new Map<string, MapFeature[]>();
let stateMetadata: StateMeta[] = [];
let nationalPartitions = new Map<string, Partition>();
let detailPartitions = new Map<string, Partition>();
let viewportPlans = new Map<string, ViewportPlan>();
let requestedDetails = new Map<string, number>();
let failedDetails = new Set<string>();
let evictedDetails = new Set<string>();
let projectionSet: Projectors | null = null;
let detailRevision = 0;
let buildEpoch = 0;
let renderEpoch = 0;
let rendering = false;
let renderQueued = false;
let readySent = false;
let dataReady = false;
let snapshotSequence = 0;
let presentedSnapshotId = 0;
let snapshots = new Map<number, Snapshot>();
let canvas: OffscreenCanvas | null = null;
let context: OffscreenCanvasRenderingContext2D | null = null;
const hitContext = new OffscreenCanvas(1, 1).getContext("2d");
let prefetchTimer: ReturnType<typeof setTimeout> | null = null;
let detailFrameTimer: ReturnType<typeof setTimeout> | null = null;
const detailBuildQueue: Array<{ state: string; features: MapFeature[]; generation: number }> = [];
let detailBuilding = false;
let detailStatesSinceFrame = 0;
let detailFramesRequested = 0;

const scheduler = new MessageChannel();
const scheduled: Array<() => void> = [];
scheduler.port1.onmessage = () => scheduled.shift()?.();

function yieldWorker(): Promise<void> {
  return new Promise((resolve) => {
    scheduled.push(resolve);
    scheduler.port2.postMessage(null);
  });
}

function post(event: RendererEvent, transfer: Transferable[] = []) {
  self.postMessage(event, transfer);
}

function profile(entry: ProfileEntry) {
  post({ type: "PROFILE", datasetGeneration, entry });
}

function semanticKey(value: MapSemantics): string {
  return [
    scores?.build_id || "neutral",
    value.metric,
    value.state,
    value.county,
    value.showUnranked ? 1 : 0,
    value.mountainMin ?? "",
    value.neutralOnly ? 1 : 0,
  ].join("|");
}

function scoreValues(index: number | null): [number | null, number | null, number | null] {
  if (index === null || !scores) return [null, null, null];
  return [
    scores.columns.risk_score[index],
    scores.columns.community_conditions_group[index],
    scores.columns.mountain_score[index],
  ];
}

function scoreIncluded(index: number | null, value: MapSemantics): boolean {
  if (index === null || !scores || value.neutralOnly) return false;
  const mountain = scores.columns.mountain_score[index];
  return value.mountainMin === null || (mountain !== null && mountain >= value.mountainMin);
}

function featureStyle(
  id: string,
  featureState: string,
  countyFips: string,
  value: MapSemantics,
): PaintStyle {
  const index = scoreIndexes.get(id) ?? null;
  const included = scoreIncluded(index, value);
  const [risk, community, mountain] = included ? scoreValues(index) : [null, null, null];
  const color = included ? metricValueColor(risk, community, mountain, value.metric) : null;
  const filtered = Boolean(
    (value.state && featureState !== value.state)
    || (value.county && countyFips !== value.county),
  );
  const showMissing = value.metric === "community-conditions" || value.showUnranked;
  return {
    fill: color || (included && showMissing ? "hatch" : "#344149"),
    alpha: filtered ? 0.12 : included ? 1 : 0.34,
    stroke: filtered ? "#233038" : "rgba(9,15,18,.54)",
  };
}

function eligible(record: FeatureRecord, value: MapSemantics): boolean {
  if (!scoreIncluded(record.scoreIndex, value)) return false;
  if ((value.state && record.state !== value.state) || (value.county && record.countyFips !== value.county)) {
    return false;
  }
  const [risk, community, mountain] = scoreValues(record.scoreIndex);
  const present = value.metric === "fema" ? risk !== null
    : value.metric === "mountain" ? mountain !== null : community !== null;
  return present || value.metric === "community-conditions" || value.showUnranked;
}

function groupFor(groups: Map<string, PaintGroup[]>, order: PaintGroup[], style: PaintStyle): PaintGroup {
  const key = `${style.fill}|${style.alpha}|${style.stroke}`;
  const existing = groups.get(key);
  let group = existing?.at(-1);
  if (!group || group.features >= 32) {
    group = { ...style, path: new Path2D(), features: 0 };
    if (existing) existing.push(group);
    else groups.set(key, [group]);
    order.push(group);
  }
  return group;
}

async function viewportPlan(
  partitions: Partition[],
  plans: StylePlan[],
  value: MapSemantics,
  epoch: number,
): Promise<ViewportPlan> {
  const key = `${semanticKey(value)}|${partitions.map((partition) =>
    `${partition.state}:${partition.detail ? "d" : "n"}:${partition.revision}`
  ).join(",")}`;
  const cached = viewportPlans.get(key);
  if (cached) return cached;
  const groups: PaintGroup[] = [];
  const outlines: PaintGroup[] = [];
  let sliceStarted = performance.now();
  for (const plan of plans) {
    for (const source of plan.groups) {
      if (epoch !== renderEpoch) throw new Cancelled();
      groups.push(source);
      outlines.push({
        fill: "", alpha: source.alpha, stroke: source.stroke,
        path: source.path, features: source.features,
      });
      if (performance.now() - sliceStarted >= SLICE_MS) {
        await yieldWorker();
        sliceStarted = performance.now();
      }
    }
  }
  const result = { key, groups, outlines };
  viewportPlans.set(key, result);
  while (viewportPlans.size > 6) {
    const oldest = viewportPlans.keys().next().value as string | undefined;
    if (!oldest || oldest === key) break;
    viewportPlans.delete(oldest);
  }
  return result;
}

function featureDrawer(projection: GeoProjection) {
  const path = geoPath(projection);
  return (feature: MapFeature, target: Path2D | TrackingPath) => {
    path.context(target as unknown as CanvasRenderingContext2D)(feature);
  };
}

function checkBuild(epoch: number) {
  if (epoch !== buildEpoch) throw new Cancelled();
}

async function buildPartition(
  code: string,
  features: MapFeature[],
  detail: boolean,
  projection: GeoProjection,
  sourceIndexes: number[],
  value: MapSemantics,
  epoch: number,
): Promise<Partition> {
  const records: FeatureRecord[] = [];
  const groupMap = new Map<string, PaintGroup[]>();
  const groups: PaintGroup[] = [];
  const drawFeature = featureDrawer(projection);
  let combinedBounds: Bounds | undefined;
  let sliceStarted = performance.now();
  for (let index = 0; index < features.length; index += 1) {
    checkBuild(epoch);
    const feature = features[index];
    const id = featureId(feature);
    const featureState = stateCode(feature) || code;
    const countyFips = String(feature.properties?.county_fips || (level === "county" ? id : ""));
    const style = featureStyle(id, featureState, countyFips, value);
    const group = groupFor(groupMap, groups, style);
    const featurePath = new Path2D();
    const tracker = new TrackingPath(featurePath);
    drawFeature(feature, tracker);
    group.path.addPath(featurePath);
    group.features += 1;
    const bounds = tracker.result();
    combinedBounds = mergeBounds(combinedBounds, bounds);
    records.push({
      feature,
      path: featurePath,
      id,
      state: featureState,
      countyFips,
      name: String(feature.properties?.name || id),
      bounds,
      sourceIndex: sourceIndexes[index],
      scoreIndex: scoreIndexes.get(id) ?? null,
    });
    if (performance.now() - sliceStarted >= SLICE_MS) {
      await yieldWorker();
      sliceStarted = performance.now();
    }
  }
  const key = semanticKey(value);
  return {
    state: code,
    detail,
    revision: detail ? ++detailRevision : 0,
    projection,
    records,
    grid: buildSpatialGrid(records, width, height),
    bounds: combinedBounds || [[0, 0], [0, 0]],
    stylePlans: new Map([[key, { key, groups }]]),
    lastUsed: performance.now(),
  };
}

async function stylePlan(partition: Partition, value: MapSemantics, epoch: number): Promise<StylePlan> {
  const key = semanticKey(value);
  const cached = partition.stylePlans.get(key);
  if (cached) return cached;
  const groupMap = new Map<string, PaintGroup[]>();
  const groups: PaintGroup[] = [];
  let sliceStarted = performance.now();
  for (const record of partition.records) {
    if (epoch !== renderEpoch) throw new Cancelled();
    const group = groupFor(groupMap, groups, featureStyle(record.id, record.state, record.countyFips, value));
    group.path.addPath(record.path);
    group.features += 1;
    if (performance.now() - sliceStarted >= SLICE_MS) {
      await yieldWorker();
      sliceStarted = performance.now();
    }
  }
  const plan = { key, groups };
  partition.stylePlans.set(key, plan);
  while (partition.stylePlans.size > 3) {
    const oldest = partition.stylePlans.keys().next().value as string | undefined;
    if (oldest && oldest !== key) partition.stylePlans.delete(oldest);
    else break;
  }
  return plan;
}

function mapBounds(value: MapTransform, expansion: number): Bounds {
  const xPad = width * expansion;
  const yPad = height * expansion;
  return [
    [(-xPad - value.x) / value.k, (-yPad - value.y) / value.k],
    [(width + xPad - value.x) / value.k, (height + yPad - value.y) / value.k],
  ];
}

function rasterPadding(): number {
  return Math.min(128, Math.max(48, Math.round(Math.min(width, height) * 0.1)));
}

function rasterBounds(value: MapTransform, padding: number): Bounds {
  return [
    [(-padding - value.x) / value.k, (-padding - value.y) / value.k],
    [(width + padding - value.x) / value.k, (height + padding - value.y) / value.k],
  ];
}

function interactionPartitions(value: MapTransform): Partition[] {
  return stateMetadata
    .sort((a, b) => a.sourceIndex - b.sourceIndex)
    .map((state) => level === "tract" && value.k >= DETAIL_DISPLAY_ZOOM
      ? detailPartitions.get(state.code) || nationalPartitions.get(state.code)
      : nationalPartitions.get(state.code))
    .filter((partition): partition is Partition => Boolean(partition));
}

function activePartitions(value: MapTransform, expanded = true): Partition[] {
  const view = expanded ? rasterBounds(value, rasterPadding()) : mapBounds(value, 0);
  return interactionPartitions(value).filter((partition) => boundsIntersect(partition.bounds, view));
}

function hatchPattern(target: OffscreenCanvasRenderingContext2D, scale: number): CanvasPattern | string {
  const tile = new OffscreenCanvas(8, 8);
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
  const pattern = target.createPattern(tile, "repeat");
  if (pattern && typeof pattern.setTransform === "function") {
    pattern.setTransform({ a: 1 / scale, d: 1 / scale });
  }
  return pattern || "#566169";
}

function ensureCanvas(renderWidth: number, renderHeight: number) {
  const pixelWidth = Math.max(1, Math.round(renderWidth * ratio));
  const pixelHeight = Math.max(1, Math.round(renderHeight * ratio));
  if (!canvas) canvas = new OffscreenCanvas(pixelWidth, pixelHeight);
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  context = canvas.getContext("2d", { alpha: false });
  if (!context) throw new Error("Offscreen map canvas is unavailable");
}

async function render(epoch: number) {
  if (!projectionSet || !stateMetadata.length) return;
  const started = performance.now();
  const renderCamera = { ...camera };
  const renderSemantics = { ...semantics };
  const renderDataset = datasetGeneration;
  const renderViewport = viewportGeneration;
  const renderCameraGeneration = cameraGeneration;
  const renderSemantic = semanticGeneration;
  const partitions = activePartitions(renderCamera);
  const plans: StylePlan[] = [];
  for (const partition of partitions) {
    if (epoch !== renderEpoch) throw new Cancelled();
    partition.lastUsed = performance.now();
    plans.push(await stylePlan(partition, renderSemantics, epoch));
  }
  const composite = await viewportPlan(partitions, plans, renderSemantics, epoch);
  if (epoch !== renderEpoch) throw new Cancelled();
  const padding = rasterPadding();
  const originX = -padding;
  const originY = -padding;
  const renderWidth = width + padding * 2;
  const renderHeight = height + padding * 2;
  ensureCanvas(renderWidth, renderHeight);
  const target = context;
  if (!target || !canvas) return;
  target.setTransform(1, 0, 0, 1, 0, 0);
  target.globalAlpha = 1;
  target.fillStyle = "#10191e";
  target.fillRect(0, 0, canvas.width, canvas.height);
  target.setTransform(
    ratio * renderCamera.k, 0, 0, ratio * renderCamera.k,
    ratio * (renderCamera.x - originX), ratio * (renderCamera.y - originY),
  );
  const missing = hatchPattern(target, renderCamera.k);
  let sliceStarted = performance.now();
  for (const group of composite.groups) {
    if (epoch !== renderEpoch) throw new Cancelled();
    target.globalAlpha = group.alpha;
    target.fillStyle = group.fill === "hatch" ? missing : group.fill;
    target.fill(group.path);
    if (performance.now() - sliceStarted >= RENDER_SLICE_MS) {
      await yieldWorker();
      sliceStarted = performance.now();
    }
  }
  if (level === "county" || renderCamera.k >= DETAIL_DISPLAY_ZOOM) {
    for (const outline of composite.outlines) {
      if (epoch !== renderEpoch) throw new Cancelled();
      target.globalAlpha = outline.alpha;
      target.strokeStyle = outline.stroke;
      target.lineWidth = (level === "county" ? 0.5 : 0.18) / renderCamera.k;
      target.stroke(outline.path);
      if (performance.now() - sliceStarted >= RENDER_SLICE_MS) {
        await yieldWorker();
        sliceStarted = performance.now();
      }
    }
  }
  target.globalAlpha = 1;
  const visible = rasterBounds(renderCamera, padding);
  for (const state of stateMetadata) {
    if (!boundsIntersect(state.bounds, visible)) continue;
    if (epoch !== renderEpoch) throw new Cancelled();
    target.strokeStyle = "rgba(223,231,230,.52)";
    target.lineWidth = 0.8 / renderCamera.k;
    target.stroke(state.path);
    const boundsWidth = (state.bounds[1][0] - state.bounds[0][0]) * renderCamera.k;
    const boundsHeight = (state.bounds[1][1] - state.bounds[0][1]) * renderCamera.k;
    if ((width >= 480 || renderCamera.k >= 2 || TERRITORY_BOXES[state.code])
      && (boundsWidth >= 24 && boundsHeight >= 16 || TERRITORY_BOXES[state.code])
      && state.label) {
      const screenX = state.label[0] * renderCamera.k + renderCamera.x - originX;
      const screenY = state.label[1] * renderCamera.k + renderCamera.y - originY;
      target.setTransform(ratio, 0, 0, ratio, 0, 0);
      target.font = "600 10px ui-sans-serif, system-ui";
      target.textAlign = "center";
      target.fillStyle = "rgba(231,238,236,.72)";
      target.fillText(state.code, screenX, screenY);
      target.setTransform(
        ratio * renderCamera.k, 0, 0, ratio * renderCamera.k,
        ratio * (renderCamera.x - originX), ratio * (renderCamera.y - originY),
      );
    }
    if (performance.now() - sliceStarted >= RENDER_SLICE_MS) {
      await yieldWorker();
      sliceStarted = performance.now();
    }
  }
  if (selected) {
    const selectedRecord = partitions.flatMap((partition) => partition.records)
      .find((record) => record.id === selected);
    if (selectedRecord) {
      target.globalAlpha = 1;
      target.strokeStyle = "#f7f4e8";
      target.lineWidth = 2.2 / renderCamera.k;
      target.stroke(selectedRecord.path);
    }
  }
  if (epoch !== renderEpoch || renderDataset !== datasetGeneration || renderViewport !== viewportGeneration) {
    throw new Cancelled();
  }
  const snapshot: Snapshot = {
    id: ++snapshotSequence,
    datasetGeneration: renderDataset,
    viewportGeneration: renderViewport,
    cameraGeneration: renderCameraGeneration,
    semanticGeneration: renderSemantic,
    camera: renderCamera,
    semantics: renderSemantics,
    originX,
    originY,
    // Keep every atomic state partition in the snapshot so the exact picker can
    // answer against areas exposed by the compositor before the next raster is
    // ready. Raster work remains bounded to the visible partitions above.
    partitions: interactionPartitions(renderCamera),
    featureCount: partitions.reduce((count, partition) => count + partition.records.length, 0),
    interactive: dataReady,
  };
  snapshots.set(snapshot.id, snapshot);
  const bitmap = canvas.transferToImageBitmap();
  profile({
    name: "bitmap-ready", start: started, duration: performance.now() - started,
    details: { snapshot: snapshot.id, features: snapshot.featureCount, groups: composite.groups.length, outlines: composite.outlines.length },
  });
  post({
    type: "FRAME",
    datasetGeneration: snapshot.datasetGeneration,
    viewportGeneration: snapshot.viewportGeneration,
    cameraGeneration: snapshot.cameraGeneration,
    snapshotId: snapshot.id,
    semanticGeneration: snapshot.semanticGeneration,
    metric: snapshot.semantics.metric,
    camera: snapshot.camera,
    originX,
    originY,
    width: renderWidth,
    height: renderHeight,
    ratio,
    featureCount: snapshot.featureCount,
    interactive: snapshot.interactive,
    bitmap,
  }, [bitmap]);
  if (snapshot.interactive && !readySent) {
    readySent = true;
    post({
      type: "READY", datasetGeneration: snapshot.datasetGeneration,
      viewportGeneration: snapshot.viewportGeneration, snapshotId: snapshot.id,
      featureCount: snapshot.featureCount, level,
    });
  }
}

function scheduleRender(delay = 0) {
  renderEpoch += 1;
  renderQueued = true;
  if (rendering) return;
  const run = async () => {
    rendering = true;
    while (renderQueued) {
      renderQueued = false;
      const epoch = renderEpoch;
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      try {
        await render(epoch);
      } catch (caught) {
        if (caught instanceof Cancelled) {
          profile({ name: "render-cancelled", start: performance.now(), details: { epoch } });
        } else {
          post({
            type: "ERROR", datasetGeneration, kind: "worker",
            message: caught instanceof Error ? caught.message : "Map rendering failed",
          });
        }
      }
    }
    rendering = false;
  };
  void run();
}

function reuseTranslatedFrame(nextCamera: MapTransform, nextCameraGeneration: number): boolean {
  const previous = snapshots.get(presentedSnapshotId);
  if (!previous
    || previous.datasetGeneration !== datasetGeneration
    || previous.viewportGeneration !== viewportGeneration
    || previous.semanticGeneration !== semanticGeneration
    || Math.abs(previous.camera.k - nextCamera.k) > 1e-9) return false;
  const deltaX = nextCamera.x - previous.camera.x;
  const deltaY = nextCamera.y - previous.camera.y;
  const originX = previous.originX + deltaX;
  const originY = previous.originY + deltaY;
  const padding = rasterPadding();
  if (originX < -padding * 2 || originX > 0 || originY < -padding * 2 || originY > 0) return false;
  const snapshot: Snapshot = {
    ...previous,
    id: ++snapshotSequence,
    cameraGeneration: nextCameraGeneration,
    camera: { ...nextCamera },
    originX,
    originY,
  };
  snapshots.set(snapshot.id, snapshot);
  post({
    type: "FRAME_REUSED",
    datasetGeneration: snapshot.datasetGeneration,
    viewportGeneration: snapshot.viewportGeneration,
    cameraGeneration: snapshot.cameraGeneration,
    snapshotId: snapshot.id,
    semanticGeneration: snapshot.semanticGeneration,
    metric: snapshot.semantics.metric,
    camera: snapshot.camera,
    originX,
    originY,
    featureCount: snapshot.featureCount,
    interactive: snapshot.interactive,
  });
  profile({
    name: "bitmap-reused", start: performance.now(), duration: 0,
    details: { snapshot: snapshot.id, features: snapshot.featureCount },
  });
  return true;
}

async function rebuildGeometry() {
  const epoch = ++buildEpoch;
  const started = performance.now();
  try {
    const nextProjectors = projectors(width, height, rawStates);
    const nextStates: StateMeta[] = [];
    let sliceStarted = performance.now();
    for (let index = 0; index < rawStates.length; index += 1) {
      checkBuild(epoch);
      const feature = rawStates[index];
      const code = stateCode(feature);
      const projection = projectionFor(feature, nextProjectors);
      const path = new Path2D();
      const tracker = new TrackingPath(path);
      featureDrawer(projection)(feature, tracker);
      const specified = feature.properties?.label;
      const projected = Array.isArray(specified) ? projection(specified) : geoPath(projection).centroid(feature);
      nextStates.push({
        code,
        sourceIndex: index,
        feature,
        projection,
        path,
        bounds: tracker.result(),
        label: projected && Number.isFinite(projected[0]) && Number.isFinite(projected[1])
          ? [projected[0], projected[1]] : null,
      });
      if (performance.now() - sliceStarted >= SLICE_MS) {
        await yieldWorker();
        sliceStarted = performance.now();
      }
    }
    const grouped = new Map<string, Array<{ feature: MapFeature; sourceIndex: number }>>();
    rawFeatures.forEach((feature, sourceIndex) => {
      const code = stateCode(feature);
      const items = grouped.get(code);
      if (items) items.push({ feature, sourceIndex });
      else grouped.set(code, [{ feature, sourceIndex }]);
    });
    const nextNational = new Map<string, Partition>();
    for (const [code, items] of grouped) {
      checkBuild(epoch);
      const state = nextStates.find((item) => item.code === code);
      const projection = state?.projection || nextProjectors.main;
      nextNational.set(code, await buildPartition(
        code,
        items.map((item) => item.feature),
        false,
        projection,
        items.map((item) => item.sourceIndex),
        semantics,
        epoch,
      ));
    }
    const nextDetails = new Map<string, Partition>();
    for (const [code, features] of rawDetails) {
      checkBuild(epoch);
      const state = nextStates.find((item) => item.code === code);
      if (!state) continue;
      nextDetails.set(code, await buildPartition(
        code, features, true, state.projection, features.map((_, index) => index), semantics, epoch,
      ));
    }
    checkBuild(epoch);
    projectionSet = nextProjectors;
    stateMetadata = nextStates;
    nationalPartitions = nextNational;
    detailPartitions = nextDetails;
    viewportPlans.clear();
    profile({
      name: "projection-ready", start: started, duration: performance.now() - started,
      details: { states: nextStates.length, features: rawFeatures.length, details: rawDetails.size },
    });
    scheduleRender();
    schedulePrefetch();
  } catch (caught) {
    if (caught instanceof Cancelled) {
      profile({ name: "projection-cancelled", start: performance.now(), details: { epoch } });
    } else {
      post({
        type: "ERROR", datasetGeneration, kind: "worker",
        message: caught instanceof Error ? caught.message : "Map geometry preparation failed",
      });
    }
  }
}

function visibleStateCodes(value = camera): Set<string> {
  const view = mapBounds(value, 0);
  return new Set(stateMetadata.filter((state) => boundsIntersect(state.bounds, view)).map((state) => state.code));
}

function evictDetails() {
  const visible = visibleStateCodes();
  const evictions = detailEvictions(
    [...detailPartitions.values()].map((partition) => ({
      state: partition.state, lastUsed: partition.lastUsed, featureCount: partition.records.length,
    })),
    visible,
    MAX_NON_VISIBLE_DETAIL_STATES,
    Infinity,
  );
  for (const state of evictions) {
    detailPartitions.delete(state);
    rawDetails.delete(state);
    requestedDetails.delete(state);
    evictedDetails.add(state);
    viewportPlans.clear();
    profile({ name: "detail-evicted", start: performance.now(), details: { state } });
  }
}

function requestDetails() {
  if (!dataReady || level !== "tract" || camera.k < DETAIL_PREFETCH_ZOOM || !loaderPort) return;
  const visible = visibleStateCodes();
  const expanded = mapBounds(camera, 0.5);
  const center: [number, number] = [
    (expanded[0][0] + expanded[1][0]) / 2,
    (expanded[0][1] + expanded[1][1]) / 2,
  ];
  const candidates = stateMetadata
    .filter((state) => boundsIntersect(state.bounds, expanded))
    .filter((state) => !detailPartitions.has(state.code) && !failedDetails.has(state.code))
    .filter((state) => !evictedDetails.has(state.code) || visible.has(state.code))
    .sort((a, b) => {
      const visibleDifference = Number(visible.has(b.code)) - Number(visible.has(a.code));
      if (visibleDifference) return visibleDifference;
      const distance = (item: StateMeta) => {
        const x = (item.bounds[0][0] + item.bounds[1][0]) / 2 - center[0];
        const y = (item.bounds[0][1] + item.bounds[1][1]) / 2 - center[1];
        return x * x + y * y;
      };
      return distance(a) - distance(b);
    });
  candidates.filter((state) => visible.has(state.code)).forEach((state) => evictedDetails.delete(state.code));
  const requests = candidates.map((state, index) => ({
    state: state.code,
    priority: visible.has(state.code) ? (camera.k >= DETAIL_DISPLAY_ZOOM ? 3000 : 2000) - index : 1000 - index,
  })).filter((request) => request.priority > (requestedDetails.get(request.state) ?? -Infinity));
  if (!requests.length) return;
  profile({
    name: "detail-prefetch", start: performance.now(),
    details: { requests: requests.length, visible: requests.filter((request) => visible.has(request.state)).length },
  });
  requests.forEach((request) => requestedDetails.set(request.state, request.priority));
  const command: LoaderCommand = {
    type: "DETAIL",
    datasetGeneration,
    requests,
  };
  loaderPort.postMessage(command);
}

function schedulePrefetch() {
  if (prefetchTimer) clearTimeout(prefetchTimer);
  prefetchTimer = setTimeout(requestDetails, 150);
}

async function receiveDetail(state: string, features: MapFeature[], generation: number) {
  if (generation !== datasetGeneration || !projectionSet) return;
  const epoch = buildEpoch;
  const meta = stateMetadata.find((item) => item.code === state);
  if (!meta) return;
  rawDetails.set(state, features);
  try {
    const partition = await buildPartition(
      state, features, true, meta.projection, features.map((_, index) => index), semantics, epoch,
    );
    if (generation !== datasetGeneration) return;
    if (epoch !== buildEpoch) {
      detailBuildQueue.push({ state, features, generation });
      return;
    }
    detailPartitions.set(state, partition);
    viewportPlans.clear();
    failedDetails.delete(state);
    profile({
      name: "detail-ready", start: performance.now(),
      details: { state, features: partition.records.length, revision: partition.revision },
    });
    evictDetails();
  } catch (caught) {
    if (caught instanceof Cancelled) {
      if (generation === datasetGeneration) detailBuildQueue.push({ state, features, generation });
    } else {
      rawDetails.delete(state);
      failedDetails.add(state);
      post({
        type: "ERROR", datasetGeneration, kind: "detail",
        message: caught instanceof Error ? caught.message : `Detailed geometry failed for ${state}`,
      });
    }
  }
}

async function pumpDetailBuilds() {
  if (detailBuilding) return;
  detailBuilding = true;
  while (detailBuildQueue.length) {
    const next = detailBuildQueue.shift();
    if (next) {
      await receiveDetail(next.state, next.features, next.generation);
      detailStatesSinceFrame += 1;
      if (detailFrameTimer) clearTimeout(detailFrameTimer);
      const immediate = detailFramesRequested === 0 || detailStatesSinceFrame >= 4;
      detailFrameTimer = setTimeout(() => {
        detailFrameTimer = null;
        detailStatesSinceFrame = 0;
        detailFramesRequested += 1;
        scheduleRender();
      }, immediate ? 16 : 100);
    }
  }
  detailBuilding = false;
}

function receiveLoader(event: LoaderEvent) {
  if (event.datasetGeneration !== datasetGeneration) {
    profile({ name: "stale-loader-event", start: performance.now(), details: { type: event.type } });
    return;
  }
  if (event.type === "PROFILE") {
    post(event);
    return;
  }
  if (event.type === "ERROR") {
    if (event.kind === "detail" && event.state) {
      requestedDetails.delete(event.state);
      failedDetails.add(event.state);
    }
    post({ ...event, datasetGeneration });
    return;
  }
  if (event.type === "DETAIL") {
    detailBuildQueue.push({ state: event.state, features: event.features, generation: event.datasetGeneration });
    void pumpDetailBuilds();
    return;
  }
  if (event.type === "STATES") {
    rawStates = event.states;
    rawFeatures = [];
    dataReady = false;
    post({ type: "STATUS", datasetGeneration, message: "Drawing map outline" });
    void rebuildGeometry();
    return;
  }
  scores = event.scores;
  scoreIndexes = new Map(scores?.columns.place_id.map((id, index) => [id, index]) || []);
  rawStates = event.states;
  rawFeatures = event.features;
  dataReady = true;
  rawDetails.clear();
  detailBuildQueue.length = 0;
  detailStatesSinceFrame = 0;
  detailFramesRequested = 0;
  detailPartitions.clear();
  viewportPlans.clear();
  requestedDetails.clear();
  failedDetails.clear();
  evictedDetails.clear();
  if (scores) post({ type: "SCORES_READY", datasetGeneration, count: scores.columns.place_id.length });
  void rebuildGeometry();
}

function pick(command: Extract<RendererCommand, { type: "PICK" }>) {
  const started = performance.now();
  const snapshot = snapshots.get(command.snapshotId);
  if (!snapshot || snapshot.datasetGeneration !== command.datasetGeneration) {
    post({
      type: "PICK_RESULT", datasetGeneration, requestId: command.requestId,
      mode: command.mode, snapshotId: command.snapshotId, preview: null,
    });
    return;
  }
  const point: [number, number] = [
    (command.x - command.camera.x) / command.camera.k,
    (command.y - command.camera.y) / command.camera.k,
  ];
  const candidates: Array<{ record: FeatureRecord; partitionOrder: number }> = [];
  for (let partitionOrder = 0; partitionOrder < snapshot.partitions.length; partitionOrder += 1) {
    const partition = snapshot.partitions[partitionOrder];
    for (const index of gridCandidates(partition.grid, point[0], point[1])) {
      const record = partition.records[index];
      if (!record || !eligible(record, snapshot.semantics)) continue;
      if (point[0] < record.bounds[0][0] || point[0] > record.bounds[1][0]
        || point[1] < record.bounds[0][1] || point[1] > record.bounds[1][1]) continue;
      candidates.push({ record, partitionOrder });
    }
  }
  candidates.sort((a, b) => b.partitionOrder - a.partitionOrder || b.record.sourceIndex - a.record.sourceIndex);
  const match = candidates.find(({ record }) => hitContext?.isPointInPath(record.path, point[0], point[1]))?.record;
  let preview: MapPickPreview | null = null;
  if (match) {
    const [risk, community, mountain] = scoreValues(match.scoreIndex);
    preview = {
      placeId: match.id,
      name: match.name,
      state: match.state,
      score: match.scoreIndex === null ? null : {
        place_id: match.id,
        risk_score: risk,
        community_conditions_group: community,
        mountain_score: mountain,
      },
    };
  }
  post({
    type: "PICK_RESULT", datasetGeneration, requestId: command.requestId,
    mode: command.mode, snapshotId: command.snapshotId, preview,
  });
  profile({
    name: "pick", start: started, duration: performance.now() - started,
    details: { mode: command.mode, candidates: candidates.length, hit: Boolean(match) },
  });
}

function focus(command: Extract<RendererCommand, { type: "FOCUS" }>) {
  const snapshot = snapshots.get(command.snapshotId);
  let bounds: Bounds | undefined;
  if (snapshot) {
    if (command.target.kind === "state") {
      bounds = stateMetadata.find((state) => state.code === command.target.id)?.bounds;
    } else {
      const partitions = [...nationalPartitions.entries()].map(([code, national]) =>
        level === "tract" ? detailPartitions.get(code) || national : national,
      );
      for (const partition of partitions) {
        for (const record of partition.records) {
          const matches = command.target.kind === "place"
            ? record.id === command.target.id
            : record.countyFips === command.target.id;
          if (matches) bounds = mergeBounds(bounds, record.bounds);
        }
      }
    }
  }
  post({
    type: "FOCUS_RESULT", datasetGeneration, requestId: command.requestId,
    snapshotId: command.snapshotId, bounds: bounds || null,
  });
}

function commitSnapshot(snapshotId: number, presented: boolean) {
  const committed = snapshots.get(snapshotId);
  if (presented && committed) presentedSnapshotId = snapshotId;
  if (!presented) snapshots.delete(snapshotId);
  for (const id of snapshots.keys()) {
    if (id !== presentedSnapshotId && id < snapshotId) snapshots.delete(id);
  }
}

function initialize(command: Extract<RendererCommand, { type: "INIT" }>) {
  datasetGeneration = command.datasetGeneration;
  viewportGeneration = command.viewportGeneration;
  cameraGeneration = command.cameraGeneration;
  semanticGeneration = command.semanticGeneration;
  level = command.level;
  width = command.width;
  height = command.height;
  ratio = command.ratio;
  camera = command.camera;
  semantics = command.semantics;
  selected = command.selected;
  loaderPort = command.loaderPort;
  loaderPort.onmessage = (event: MessageEvent<LoaderEvent>) => receiveLoader(event.data);
  loaderPort.start();
  readySent = false;
  dataReady = false;
  snapshots.clear();
  presentedSnapshotId = 0;
  const loaderCommand: LoaderCommand = {
    type: "LOAD",
    datasetGeneration,
    manifestUrl: command.manifestUrl,
    scoreUrl: command.scoreUrl,
    expectedBuildId: command.expectedBuildId,
    level,
    neutralOnly: semantics.neutralOnly,
  };
  loaderPort.postMessage(loaderCommand);
  post({ type: "STATUS", datasetGeneration, message: "Loading map data" });
}

function command(event: MessageEvent<RendererCommand>) {
  const value = event.data;
  if (value.type === "INIT") {
    initialize(value);
    return;
  }
  if (value.type === "DISPOSE") {
    if (prefetchTimer) clearTimeout(prefetchTimer);
    if (detailFrameTimer) clearTimeout(detailFrameTimer);
    loaderPort?.postMessage({ type: "DISPOSE" } satisfies LoaderCommand);
    loaderPort?.close();
    self.close();
    return;
  }
  if (value.datasetGeneration !== datasetGeneration) return;
  if (value.type === "RESIZE") {
    viewportGeneration = value.viewportGeneration;
    cameraGeneration = value.cameraGeneration;
    width = value.width;
    height = value.height;
    ratio = value.ratio;
    camera = value.camera;
    if (rawStates.length) void rebuildGeometry();
  } else if (value.type === "SET_CAMERA") {
    cameraGeneration = value.cameraGeneration;
    camera = value.camera;
    if (!reuseTranslatedFrame(camera, cameraGeneration)) scheduleRender();
    schedulePrefetch();
    evictDetails();
  } else if (value.type === "SET_SEMANTICS") {
    semanticGeneration = value.semanticGeneration;
    semantics = value.semantics;
    post({ type: "STATUS", datasetGeneration, message: `Rendering ${value.semantics.metric} map` });
    scheduleRender();
  } else if (value.type === "SET_SELECTION") {
    semanticGeneration = value.semanticGeneration;
    selected = value.selected;
    scheduleRender();
  } else if (value.type === "PICK") pick(value);
  else if (value.type === "FOCUS") focus(value);
  else if (value.type === "FRAME_COMMITTED") commitSnapshot(value.snapshotId, value.presented);
  else if (value.type === "RETRY") {
    failedDetails.clear();
    requestedDetails.clear();
    schedulePrefetch();
  }
}

self.onmessage = command;
