import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { select } from "d3-selection";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import { cameraFromTransform, relativeTransform, transformFromCamera, type CameraState } from "./map";
import type {
  LoaderBootstrap,
  MapFocusTarget,
  MapSemantics,
  MapTransform,
  RendererCommand,
  RendererEvent,
} from "./mapWorkerProtocol";
import type { Geography, MapScore, Metric } from "./types";

export type FocusTarget = MapFocusTarget;

export interface MapPreview {
  placeId: string;
  name: string;
  state: string;
  score: MapScore | null;
  x: number;
  y: number;
}

interface Props {
  manifestUrl: string;
  scoreUrl: string;
  expectedBuildId: string;
  level: Geography;
  metric?: Metric;
  displayMetric?: Metric;
  busy?: boolean;
  selected: string;
  state: string;
  county: string;
  showUnranked: boolean;
  mountainMin?: number | null;
  neutralOnly?: boolean;
  retryGeneration?: number;
  focusTarget: FocusTarget | null;
  cameraTarget?: CameraState & { nonce: number };
  initialCamera: CameraState;
  onSelect: (placeId: string) => void;
  onPreview: (preview: MapPreview | null) => void;
  onCamera: (camera: CameraState) => void;
  onStatus: (message: string) => void;
  onScoresReady?: (count: number) => void;
  onScoreError?: (message: string) => void;
  onVisibleCommit?: (metric: Metric) => void;
  onInteractiveCommit?: (metric: Metric) => void;
}

interface PresentedFrame {
  snapshotId: number;
  datasetGeneration: number;
  viewportGeneration: number;
  semanticGeneration: number;
  cameraGeneration: number;
  camera: MapTransform;
  originX: number;
  originY: number;
  interactive: boolean;
}

interface PointerPick {
  x: number;
  y: number;
  clientX: number;
  clientY: number;
  retries?: number;
}

declare global {
  interface Window {
    __HOUSEHUNTER_MAP_PROFILE__?: Array<Record<string, unknown>>;
  }
}

const PROFILE_MAP = new URLSearchParams(window.location.search).has("profile-map");

function recordProfile(value: Record<string, unknown>) {
  if (!PROFILE_MAP) return;
  const entries = window.__HOUSEHUNTER_MAP_PROFILE__ ||= [];
  const recorded = { recordedAt: performance.now(), ...value };
  entries.push(recorded);
  if (entries.length > 2_000) entries.splice(0, entries.length - 2_000);
  if (value.name !== "gesture-frame") {
    performance.mark(`househunter-map:${String(value.name || "event")}`, { detail: recorded });
  }
}

function assertSameOrigin(url: string): string {
  const target = new URL(url, window.location.href);
  if (target.origin !== window.location.origin) throw new Error("Cross-origin map assets are not allowed");
  return target.href;
}

function transformCanvas(canvas: HTMLCanvasElement, current: ZoomTransform, frame: PresentedFrame | null) {
  if (!frame) return;
  const relative = relativeTransform(current, frame.camera);
  const x = relative.x + relative.k * frame.originX - frame.originX;
  const y = relative.y + relative.k * frame.originY - frame.originY;
  canvas.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${relative.k})`;
}

export default function RiskMap({
  manifestUrl,
  scoreUrl,
  expectedBuildId,
  level,
  metric = "fema",
  displayMetric = metric,
  busy = displayMetric !== metric,
  selected,
  state,
  county,
  showUnranked,
  mountainMin = null,
  neutralOnly = false,
  retryGeneration = 0,
  focusTarget,
  cameraTarget,
  initialCamera,
  onSelect,
  onPreview,
  onCamera,
  onStatus,
  onScoresReady,
  onScoreError,
  onVisibleCommit,
  onInteractiveCommit,
}: Props) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<Worker | null>(null);
  const transformRef = useRef<ZoomTransform>(zoomIdentity);
  const zoomRef = useRef<ZoomBehavior<HTMLDivElement, unknown> | null>(null);
  const dimensionsRef = useRef({ width: 1, height: 1, ratio: 1 });
  const presentedRef = useRef<PresentedFrame | null>(null);
  const datasetGenerationRef = useRef(0);
  const viewportGenerationRef = useRef(0);
  const cameraGenerationRef = useRef(0);
  const semanticGenerationRef = useRef(0);
  const requestRef = useRef(0);
  const focusedNonceRef = useRef<number | null>(null);
  const focusTargetRef = useRef<FocusTarget | null>(focusTarget);
  const focusPendingRef = useRef<{ nonce: number; requestId: number; snapshotId: number } | null>(null);
  const gestureRef = useRef(false);
  const pointerStartsRef = useRef(new Map<number, [number, number]>());
  const transformFrameRef = useRef(0);
  const gestureFrameRef = useRef(0);
  const hoverInFlightRef = useRef(false);
  const hoverPendingRef = useRef<PointerPick | null>(null);
  const activatePendingRef = useRef<PointerPick | null>(null);
  const lastHoverRef = useRef<PointerPick | null>(null);
  const pickPointersRef = useRef(new Map<number, PointerPick>());
  const pickStartedRef = useRef(new Map<number, number>());
  const settleStartedRef = useRef(new Map<number, number>());
  const callbackRef = useRef({
    onSelect, onPreview, onCamera, onStatus, onScoresReady, onScoreError,
    onVisibleCommit, onInteractiveCommit,
  });
  callbackRef.current = {
    onSelect, onPreview, onCamera, onStatus, onScoresReady, onScoreError,
    onVisibleCommit, onInteractiveCommit,
  };
  focusTargetRef.current = focusTarget;
  const [cursor, setCursor] = useState({ x: 0.5, y: 0.5 });
  const [workerError, setWorkerError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [restartGeneration, setRestartGeneration] = useState(0);
  const [presentedVersion, setPresentedVersion] = useState(0);

  const semanticValue = useMemo<MapSemantics>(() => ({
    metric, state, county, showUnranked, mountainMin, neutralOnly,
  }), [county, metric, mountainMin, neutralOnly, showUnranked, state]);

  const post = useCallback((command: RendererCommand, transfer: Transferable[] = []) => {
    rendererRef.current?.postMessage(command, transfer);
  }, []);

  const scheduleTransform = useCallback(() => {
    if (transformFrameRef.current) return;
    transformFrameRef.current = window.requestAnimationFrame((timestamp) => {
      transformFrameRef.current = 0;
      const canvas = canvasRef.current;
      if (canvas) transformCanvas(canvas, transformRef.current, presentedRef.current);
      if (gestureRef.current) {
        if (gestureFrameRef.current) recordProfile({
          name: "gesture-frame",
          start: gestureFrameRef.current,
          duration: timestamp - gestureFrameRef.current,
          cameraGeneration: cameraGenerationRef.current,
        });
        gestureFrameRef.current = timestamp;
      }
    });
  }, []);

  const sendPick = useCallback((mode: "hover" | "activate", pointer: PointerPick) => {
    const frame = presentedRef.current;
    if (!frame?.interactive || workerError) return;
    const requestId = ++requestRef.current;
    pickPointersRef.current.set(requestId, pointer);
    pickStartedRef.current.set(requestId, performance.now());
    recordProfile({ name: "pick-request", start: performance.now(), requestId, mode, snapshotId: frame.snapshotId });
    if (mode === "hover") hoverInFlightRef.current = true;
    post({
      type: "PICK",
      datasetGeneration: frame.datasetGeneration,
      requestId,
      mode,
      snapshotId: frame.snapshotId,
      x: pointer.x,
      y: pointer.y,
      camera: { k: transformRef.current.k, x: transformRef.current.x, y: transformRef.current.y },
    });
  }, [post, workerError]);

  useEffect(() => {
    setWorkerError("");
    setDetailError("");
    let renderer: Worker;
    let loader: Worker;
    try {
      assertSameOrigin(manifestUrl);
      assertSameOrigin(scoreUrl);
      const workerOrigin = new URL(import.meta.url).origin;
      if (workerOrigin !== "null" && workerOrigin !== window.location.origin) {
        throw new Error("Map workers must be same-origin");
      }
      renderer = new Worker(new URL("./mapRenderer.worker.ts", import.meta.url), { type: "module", name: "househunter-map-renderer" });
      loader = new Worker(new URL("./mapLoader.worker.ts", import.meta.url), { type: "module", name: "househunter-map-loader" });
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Map workers are unavailable";
      setWorkerError(message);
      callbackRef.current.onStatus("Map worker failed");
      return;
    }
    const datasetGeneration = ++datasetGenerationRef.current;
    const channel = new MessageChannel();
    const bootstrap: LoaderBootstrap = { type: "CONNECT", port: channel.port1 };
    loader.postMessage(bootstrap, [channel.port1]);
    rendererRef.current = renderer;
    presentedRef.current = null;
    hoverInFlightRef.current = false;
    hoverPendingRef.current = null;
    activatePendingRef.current = null;
    focusedNonceRef.current = null;
    focusPendingRef.current = null;
    pickPointersRef.current.clear();
    pickStartedRef.current.clear();
    settleStartedRef.current.clear();
    const dimensions = dimensionsRef.current;
    const init: RendererCommand = {
      type: "INIT",
      loaderPort: channel.port2,
      datasetGeneration,
      viewportGeneration: viewportGenerationRef.current,
      cameraGeneration: cameraGenerationRef.current,
      semanticGeneration: semanticGenerationRef.current,
      manifestUrl: assertSameOrigin(manifestUrl),
      scoreUrl: assertSameOrigin(scoreUrl),
      expectedBuildId,
      level,
      width: dimensions.width,
      height: dimensions.height,
      ratio: dimensions.ratio,
      camera: { k: transformRef.current.k, x: transformRef.current.x, y: transformRef.current.y },
      semantics: semanticValue,
      selected,
    };
    renderer.postMessage(init, [channel.port2]);

    const failed = (message: string) => {
      setWorkerError(message);
      callbackRef.current.onPreview(null);
      callbackRef.current.onStatus("Map worker failed");
    };
    renderer.onerror = (event) => failed(event.message || "Map renderer stopped unexpectedly");
    loader.onerror = (event) => failed(event.message || "Map loader stopped unexpectedly");
    renderer.onmessage = (event: MessageEvent<RendererEvent>) => {
      const value = event.data;
      if (value.datasetGeneration !== datasetGenerationRef.current) {
        if (value.type === "FRAME") value.bitmap.close();
        return;
      }
      if (value.type === "PROFILE") {
        recordProfile({ ...value.entry, datasetGeneration: value.datasetGeneration });
      } else if (value.type === "STATUS") {
        callbackRef.current.onStatus(value.message);
      } else if (value.type === "SCORES_READY") {
        callbackRef.current.onScoresReady?.(value.count);
      } else if (value.type === "ERROR") {
        if (value.kind === "score") callbackRef.current.onScoreError?.(value.message);
        else if (value.kind === "detail") {
          setDetailError(value.message);
          callbackRef.current.onStatus("Detailed tract boundaries failed to load");
        } else failed(value.message);
      } else if (value.type === "FRAME" || value.type === "FRAME_REUSED") {
        const stale = value.viewportGeneration !== viewportGenerationRef.current
          || value.semanticGeneration !== semanticGenerationRef.current
          || value.cameraGeneration !== cameraGenerationRef.current;
        const canvas = canvasRef.current;
        const bitmapRenderer = value.type === "FRAME"
          ? canvas?.getContext("bitmaprenderer") as ImageBitmapRenderingContext | null
          : null;
        if (stale || !canvas || (value.type === "FRAME" && !bitmapRenderer)) {
          if (value.type === "FRAME") value.bitmap.close();
          renderer.postMessage({
            type: "FRAME_COMMITTED", datasetGeneration, snapshotId: value.snapshotId, presented: false,
          } satisfies RendererCommand);
          if (value.type === "FRAME" && !bitmapRenderer && canvas) failed("Bitmap presentation is unavailable");
          return;
        }
        if (value.type === "FRAME") {
          canvas.width = Math.max(1, Math.round(value.width * value.ratio));
          canvas.height = Math.max(1, Math.round(value.height * value.ratio));
          canvas.style.width = `${value.width}px`;
          canvas.style.height = `${value.height}px`;
          bitmapRenderer!.transferFromImageBitmap(value.bitmap);
        }
        canvas.style.left = `${value.originX}px`;
        canvas.style.top = `${value.originY}px`;
        canvas.dataset.snapshotId = String(value.snapshotId);
        canvas.dataset.cameraGeneration = String(value.cameraGeneration);
        const frame: PresentedFrame = {
          snapshotId: value.snapshotId,
          datasetGeneration: value.datasetGeneration,
          viewportGeneration: value.viewportGeneration,
          semanticGeneration: value.semanticGeneration,
          cameraGeneration: value.cameraGeneration,
          camera: value.camera,
          originX: value.originX,
          originY: value.originY,
          interactive: value.interactive,
        };
        presentedRef.current = frame;
        transformCanvas(canvas, transformRef.current, frame);
        renderer.postMessage({
          type: "FRAME_COMMITTED", datasetGeneration, snapshotId: value.snapshotId, presented: true,
        } satisfies RendererCommand);
        setPresentedVersion((current) => current + 1);
        if (value.interactive) {
          callbackRef.current.onVisibleCommit?.(value.metric);
          callbackRef.current.onInteractiveCommit?.(value.metric);
          callbackRef.current.onStatus(`${value.featureCount.toLocaleString()} ${level}s interactive`);
        }
        recordProfile({
          name: value.type === "FRAME" ? "bitmap-commit" : "bitmap-reuse-commit",
          start: performance.now(), snapshotId: value.snapshotId,
          featureCount: value.featureCount, metric: value.metric,
        });
        const settleStarted = settleStartedRef.current.get(value.cameraGeneration);
        if (settleStarted !== undefined) {
          recordProfile({
            name: "camera-settle", start: settleStarted,
            duration: performance.now() - settleStarted,
            cameraGeneration: value.cameraGeneration,
          });
          settleStartedRef.current.delete(value.cameraGeneration);
        }
        if (lastHoverRef.current && !gestureRef.current) {
          if (hoverInFlightRef.current) hoverPendingRef.current = lastHoverRef.current;
          else sendPick("hover", lastHoverRef.current);
        }
        if (activatePendingRef.current) {
          const pending = activatePendingRef.current;
          activatePendingRef.current = null;
          sendPick("activate", pending);
        }
      } else if (value.type === "PICK_RESULT") {
        const pointer = pickPointersRef.current.get(value.requestId);
        const pickStarted = pickStartedRef.current.get(value.requestId);
        pickPointersRef.current.delete(value.requestId);
        pickStartedRef.current.delete(value.requestId);
        if (pickStarted !== undefined) recordProfile({
          name: "pick-roundtrip", start: pickStarted, duration: performance.now() - pickStarted,
          requestId: value.requestId, mode: value.mode, hit: Boolean(value.preview),
        });
        if (value.mode === "hover") hoverInFlightRef.current = false;
        const presented = presentedRef.current;
        if (pointer && value.snapshotId === presented?.snapshotId) {
          if (value.mode === "activate") {
            if (value.preview) callbackRef.current.onSelect(value.preview.placeId);
            else if ((pointer.retries || 0) < 2 && (
              presented.cameraGeneration !== cameraGenerationRef.current
              || presented.semanticGeneration !== semanticGenerationRef.current
            )) activatePendingRef.current = { ...pointer, retries: (pointer.retries || 0) + 1 };
          } else {
            callbackRef.current.onPreview(value.preview ? {
              ...value.preview, x: pointer.clientX, y: pointer.clientY,
            } : null);
          }
        } else if (pointer && value.mode === "activate" && (pointer.retries || 0) < 2) {
          sendPick("activate", { ...pointer, retries: (pointer.retries || 0) + 1 });
        } else if (pointer && value.mode === "hover") hoverPendingRef.current = pointer;
        if (value.mode === "hover" && hoverPendingRef.current) {
          const pending = hoverPendingRef.current;
          hoverPendingRef.current = null;
          sendPick("hover", pending);
        }
      } else if (value.type === "FOCUS_RESULT") {
        const pending = focusPendingRef.current;
        if (!pending || value.requestId !== pending.requestId) return;
        focusPendingRef.current = null;
        const currentTarget = focusTargetRef.current;
        if (!currentTarget || currentTarget.nonce !== pending.nonce) return;
        if (value.snapshotId !== presentedRef.current?.snapshotId) {
          setPresentedVersion((current) => current + 1);
          return;
        }
        if (!value.bounds) {
          if (presentedRef.current?.interactive) focusedNonceRef.current = pending.nonce;
          return;
        }
        focusedNonceRef.current = pending.nonce;
        const viewport = viewportRef.current;
        const behavior = zoomRef.current;
        if (!viewport || !behavior) return;
        const [[x0, y0], [x1, y1]] = value.bounds;
        const dimensions = dimensionsRef.current;
        const scale = Math.max(1, Math.min(10, 0.76 / Math.max(
          (x1 - x0) / dimensions.width,
          (y1 - y0) / dimensions.height,
          0.01,
        )));
        const target = zoomIdentity
          .translate(dimensions.width / 2 - scale * (x0 + x1) / 2, dimensions.height / 2 - scale * (y0 + y1) / 2)
          .scale(scale);
        select(viewport).call(behavior.transform, target);
      }
    };
    return () => {
      window.cancelAnimationFrame(transformFrameRef.current);
      transformFrameRef.current = 0;
      renderer.postMessage({ type: "DISPOSE" } satisfies RendererCommand);
      renderer.terminate();
      loader.terminate();
      if (rendererRef.current === renderer) rendererRef.current = null;
    };
  }, [expectedBuildId, level, manifestUrl, restartGeneration, retryGeneration, scoreUrl]);

  useEffect(() => {
    if (!rendererRef.current) return;
    const nextGeneration = ++semanticGenerationRef.current;
    post({
      type: "SET_SEMANTICS",
      datasetGeneration: datasetGenerationRef.current,
      semanticGeneration: nextGeneration,
      semantics: semanticValue,
    });
  }, [post, semanticValue]);

  useEffect(() => {
    if (!rendererRef.current) return;
    const nextGeneration = ++semanticGenerationRef.current;
    post({
      type: "SET_SELECTION",
      datasetGeneration: datasetGenerationRef.current,
      semanticGeneration: nextGeneration,
      selected,
    });
  }, [post, selected]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const resize = () => {
      const bounds = viewport.getBoundingClientRect();
      const next = {
        width: Math.max(1, bounds.width),
        height: Math.max(1, bounds.height),
        ratio: Math.min(window.devicePixelRatio || 1, 2),
      };
      const previous = dimensionsRef.current;
      if (next.width === previous.width && next.height === previous.height && next.ratio === previous.ratio) return;
      const normalized = cameraFromTransform(transformRef.current, previous.width, previous.height);
      const nextTransform = previous.width <= 1 && previous.height <= 1
        ? transformFromCamera(initialCamera, next.width, next.height)
        : transformFromCamera(normalized, next.width, next.height);
      dimensionsRef.current = next;
      transformRef.current = nextTransform;
      const viewportGeneration = ++viewportGenerationRef.current;
      const cameraGeneration = ++cameraGenerationRef.current;
      if (zoomRef.current) select(viewport).call(zoomRef.current.transform, nextTransform);
      if (rendererRef.current) post({
        type: "RESIZE",
        datasetGeneration: datasetGenerationRef.current,
        viewportGeneration,
        cameraGeneration,
        ...next,
        camera: { k: nextTransform.k, x: nextTransform.x, y: nextTransform.y },
      });
      scheduleTransform();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(viewport);
    resize();
    return () => observer.disconnect();
  }, [initialCamera, post, scheduleTransform]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const behavior = zoom<HTMLDivElement, unknown>()
      .scaleExtent([1, 12])
      .filter((event) => !event.button && (!event.ctrlKey || event.type === "wheel"));
    zoomRef.current = behavior;
    select(viewport).call(behavior).on("dblclick.zoom", null);
    behavior
      .on("start", () => {
        gestureRef.current = true;
        gestureFrameRef.current = 0;
        callbackRef.current.onPreview(null);
      })
      .on("zoom", (event) => {
        transformRef.current = event.transform;
        cameraGenerationRef.current += 1;
        scheduleTransform();
      })
      .on("end", () => {
        gestureRef.current = false;
        gestureFrameRef.current = 0;
        const dimensions = dimensionsRef.current;
        const live = transformRef.current;
        settleStartedRef.current.set(cameraGenerationRef.current, performance.now());
        recordProfile({
          name: "camera-settle-start", start: performance.now(),
          cameraGeneration: cameraGenerationRef.current,
        });
        post({
          type: "SET_CAMERA",
          datasetGeneration: datasetGenerationRef.current,
          cameraGeneration: cameraGenerationRef.current,
          camera: { k: live.k, x: live.x, y: live.y },
        });
        callbackRef.current.onCamera(cameraFromTransform(live, dimensions.width, dimensions.height));
      });
    const initial = transformFromCamera(initialCamera, dimensionsRef.current.width, dimensionsRef.current.height);
    transformRef.current = initial;
    select(viewport).call(behavior.transform, initial);
    return () => {
      select(viewport).on(".zoom", null);
      zoomRef.current = null;
    };
  }, [initialCamera, post, scheduleTransform]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const down = (event: PointerEvent) => {
      pointerStartsRef.current.set(event.pointerId, [event.clientX, event.clientY]);
    };
    const move = (event: PointerEvent) => {
      const start = pointerStartsRef.current.get(event.pointerId);
      if (event.buttons || start || gestureRef.current) return;
      const bounds = viewport.getBoundingClientRect();
      const pointer = {
        x: event.clientX - bounds.left,
        y: event.clientY - bounds.top,
        clientX: event.clientX,
        clientY: event.clientY,
      };
      lastHoverRef.current = pointer;
      if (hoverInFlightRef.current) hoverPendingRef.current = pointer;
      else sendPick("hover", pointer);
    };
    const up = (event: PointerEvent) => {
      const start = pointerStartsRef.current.get(event.pointerId);
      pointerStartsRef.current.delete(event.pointerId);
      const moved = !start || Math.hypot(event.clientX - start[0], event.clientY - start[1]) > 5;
      if (!moved) {
        const bounds = viewport.getBoundingClientRect();
        sendPick("activate", {
          x: event.clientX - bounds.left,
          y: event.clientY - bounds.top,
          clientX: event.clientX,
          clientY: event.clientY,
        });
      }
    };
    const cancel = (event: PointerEvent) => {
      pointerStartsRef.current.delete(event.pointerId);
    };
    const leave = () => {
      hoverPendingRef.current = null;
      lastHoverRef.current = null;
      callbackRef.current.onPreview(null);
    };
    viewport.addEventListener("pointerdown", down);
    viewport.addEventListener("pointermove", move);
    viewport.addEventListener("pointerup", up);
    viewport.addEventListener("pointercancel", cancel);
    viewport.addEventListener("pointerleave", leave);
    return () => {
      viewport.removeEventListener("pointerdown", down);
      viewport.removeEventListener("pointermove", move);
      viewport.removeEventListener("pointerup", up);
      viewport.removeEventListener("pointercancel", cancel);
      viewport.removeEventListener("pointerleave", leave);
    };
  }, [sendPick]);

  useEffect(() => {
    const frame = presentedRef.current;
    if (!focusTarget) {
      focusedNonceRef.current = null;
      focusPendingRef.current = null;
      return;
    }
    if (!frame || focusedNonceRef.current === focusTarget.nonce) return;
    const pending = focusPendingRef.current;
    if (pending?.nonce === focusTarget.nonce && pending.snapshotId === frame.snapshotId) return;
    const requestId = ++requestRef.current;
    focusPendingRef.current = { nonce: focusTarget.nonce, requestId, snapshotId: frame.snapshotId };
    post({
      type: "FOCUS",
      datasetGeneration: frame.datasetGeneration,
      requestId,
      snapshotId: frame.snapshotId,
      target: focusTarget,
    });
  }, [focusTarget, post, presentedVersion]);

  useEffect(() => {
    const viewport = viewportRef.current;
    const behavior = zoomRef.current;
    if (!cameraTarget || !viewport || !behavior) return;
    const dimensions = dimensionsRef.current;
    select(viewport).call(behavior.transform, transformFromCamera(cameraTarget, dimensions.width, dimensions.height));
  }, [cameraTarget]);

  function keyboard(event: React.KeyboardEvent<HTMLDivElement>) {
    const step = event.shiftKey ? 0.08 : 0.035;
    if (event.key.startsWith("Arrow")) {
      event.preventDefault();
      setCursor((current) => ({
        x: Math.max(0, Math.min(1, current.x + (event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0))),
        y: Math.max(0, Math.min(1, current.y + (event.key === "ArrowDown" ? step : event.key === "ArrowUp" ? -step : 0))),
      }));
    } else if ((event.key === "+" || event.key === "=" || event.code === "Equal" || event.code === "NumpadAdd") && zoomRef.current) {
      event.preventDefault();
      select(event.currentTarget).call(zoomRef.current.scaleBy, 1.5);
    } else if ((event.key === "-" || event.code === "Minus" || event.code === "NumpadSubtract") && zoomRef.current) {
      event.preventDefault();
      select(event.currentTarget).call(zoomRef.current.scaleBy, 1 / 1.5);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const dimensions = dimensionsRef.current;
      sendPick("activate", {
        x: cursor.x * dimensions.width,
        y: cursor.y * dimensions.height,
        clientX: cursor.x * dimensions.width,
        clientY: cursor.y * dimensions.height,
      });
    }
  }

  const zoomBy = (factor: number) => {
    const viewport = viewportRef.current;
    if (viewport && zoomRef.current) select(viewport).call(zoomRef.current.scaleBy, factor);
  };
  const reset = () => {
    const viewport = viewportRef.current;
    if (viewport && zoomRef.current) select(viewport).call(zoomRef.current.transform, zoomIdentity);
  };

  const metricLabel = displayMetric === "fema" ? "risk"
    : displayMetric === "mountain" ? "Mountain Score" : "Community Conditions";
  return <div className="map-stage" data-level={level}>
    <div
      ref={viewportRef}
      className="map-viewport risk-canvas"
      role="img"
      tabIndex={0}
      aria-busy={busy}
      aria-label={`Focusable USA ${level} ${metricLabel} map. ${displayMetric === "fema" ? "Lower FEMA ALR_NPCTL is better." : displayMetric === "mountain" ? "Higher Mountain Score means greater nearby mountain and access characteristics." : "Group 1 is healthiest and Group 10 least healthy; tract values are county-level."} Use arrow keys to move the focus cursor, plus and minus to zoom, and Enter to select.`}
      onKeyDown={keyboard}
    >
      <canvas ref={canvasRef} className="map-presentation" aria-hidden="true" />
      <span
        className="map-keyboard-cursor"
        aria-hidden="true"
        style={{ left: `${cursor.x * 100}%`, top: `${cursor.y * 100}%` }}
      />
    </div>
    <div className="map-zoom" aria-label="Map controls">
      <button type="button" onClick={() => zoomBy(1.5)} aria-label="Zoom in" title="Zoom in">+</button>
      <button type="button" onClick={() => zoomBy(1 / 1.5)} aria-label="Zoom out" title="Zoom out">−</button>
      <button type="button" onClick={reset} aria-label="Reset map" title="Reset map">⌂</button>
    </div>
    {workerError && <div className="map-error" role="alert"><strong>Map interaction stopped</strong><span>{workerError}</span><button className="secondary" onClick={() => setRestartGeneration((value) => value + 1)}>Restart map</button></div>}
    {!workerError && detailError && <div className="map-detail-error" role="alert"><span>{detailError}</span><button className="secondary" onClick={() => { setDetailError(""); post({ type: "RETRY", datasetGeneration: datasetGenerationRef.current }); }}>Retry detail</button></div>}
  </div>;
}
