/// <reference lib="webworker" />

import type { Topology } from "topojson-specification";
import { featuresFrom } from "./mapGeometry";
import {
  decodeMapScoreAddon, decodeMapScores, loadedMapAddons, mergeMapScoreAddon,
} from "./mapScores";
import type { LoaderBootstrap, LoaderCommand, LoaderEvent, ProfileEntry } from "./mapWorkerProtocol";
import type { MapAssetEntry, MapManifest, MapScoreAddon, MapScoreAddonKind, MapScores } from "./types";

declare const self: DedicatedWorkerGlobalScope;

let rendererPort: MessagePort | null = null;
let manifest: MapManifest | null = null;
let activeGeneration = 0;
let sequence = 0;
const pending = new Map<string, { state: string; priority: number; order: number; generation: number }>();
const inFlight = new Set<string>();
let loadedScores: MapScores | null = null;
let loadedAddOns = new Set<MapScoreAddonKind>();
const addOnTasks = new Map<MapScoreAddonKind, Promise<void>>();
const pendingAddOns = new Set<MapScoreAddonKind>();

function post(event: LoaderEvent) {
  rendererPort?.postMessage(event);
}

function profile(generation: number, entry: ProfileEntry) {
  post({ type: "PROFILE", datasetGeneration: generation, entry });
}

async function json<T>(url: string): Promise<T> {
  const target = new URL(url, self.location.href);
  if (target.origin !== self.location.origin) throw new Error("Cross-origin map assets are not allowed");
  const response = await fetch(target);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json() as Promise<T>;
}

function assetUrl(asset: MapAssetEntry): string {
  return `/map-assets/${asset.filename}`;
}

function nationalAsset(value: MapManifest, level: string): MapAssetEntry | undefined {
  return value.files.find((asset) => asset.level === level && asset.lod === "national");
}

function addOnUrl(scores: MapScores, kind: MapScoreAddonKind): string {
  const url = kind === "cost-of-living"
    ? scores.add_ons?.cost_of_living
    : scores.add_ons?.home_costs;
  if (!url) throw new Error(`Map score ${kind} add-on is unavailable`);
  return url;
}

async function fetchAddOn(scores: MapScores, kind: MapScoreAddonKind): Promise<MapScoreAddon> {
  const started = performance.now();
  const addon = decodeMapScoreAddon(await json<unknown>(addOnUrl(scores, kind)), scores, kind);
  profile(activeGeneration, {
    name: "score-addon-ready", start: started, duration: performance.now() - started,
    details: { kind },
  });
  return addon;
}

async function load(command: Extract<LoaderCommand, { type: "LOAD" }>) {
  activeGeneration = command.datasetGeneration;
  manifest = null;
  pending.clear();
  loadedScores = null;
  loadedAddOns.clear();
  addOnTasks.clear();
  pendingAddOns.clear();
  const started = performance.now();
  try {
    const loadedManifest = await json<MapManifest>(command.manifestUrl);
    if (command.datasetGeneration !== activeGeneration) return;
    const scoreTask = (async (): Promise<MapScores | null> => {
      if (command.neutralOnly) return null;
      try {
        const scoreStarted = performance.now();
        const loaded = decodeMapScores(
          await json<unknown>(command.scoreUrl),
          command.expectedBuildId,
          command.level,
        );
        loadedAddOns = new Set(loadedMapAddons(loaded));
        loadedScores = loaded;
        profile(command.datasetGeneration, {
          name: "scores-parsed", start: scoreStarted, duration: performance.now() - scoreStarted,
          details: { count: loaded.columns.place_id.length },
        });
        return loaded;
      } catch (caught) {
        if (command.datasetGeneration === activeGeneration) {
          post({
            type: "ERROR", datasetGeneration: command.datasetGeneration, kind: "score",
            message: caught instanceof Error ? caught.message : "Map scores failed to load",
          });
        }
        return null;
      }
    })();
    const statesAsset = nationalAsset(loadedManifest, "state");
    const geographyAsset = command.neutralOnly ? null : nationalAsset(loadedManifest, command.level);
    if (!statesAsset || (!command.neutralOnly && !geographyAsset)) throw new Error("Required map asset is missing");
    const geometryStarted = performance.now();
    const geographyTask = geographyAsset
      ? json<Topology>(assetUrl(geographyAsset))
      : Promise.resolve(null);
    const statesTopology = await json<Topology>(assetUrl(statesAsset));
    if (command.datasetGeneration !== activeGeneration) return;
    const states = featuresFrom(statesTopology);
    post({ type: "STATES", datasetGeneration: command.datasetGeneration, manifest: loadedManifest, states });
    const [scores, geographyTopology] = await Promise.all([scoreTask, geographyTask]);
    if (command.datasetGeneration !== activeGeneration) return;
    manifest = loadedManifest;
    const features = geographyTopology ? featuresFrom(geographyTopology) : [];
    profile(command.datasetGeneration, {
      name: "topology-parsed", start: geometryStarted, duration: performance.now() - geometryStarted,
      details: { states: states.length, features: features.length },
    });
    if (scores) profile(command.datasetGeneration, {
      name: "scores-ready", start: started, duration: performance.now() - started,
      details: { count: scores.columns.place_id.length },
    });
    profile(command.datasetGeneration, {
      name: "topology-loaded", start: geometryStarted, duration: performance.now() - geometryStarted,
      details: { states: states.length, features: features.length },
    });
    post({ type: "DATASET", datasetGeneration: command.datasetGeneration, manifest: loadedManifest, scores, loadedAddOns: [...loadedAddOns], states, features });
    for (const kind of pendingAddOns) {
      pendingAddOns.delete(kind);
      requestAddOn({ type: "ADDON", datasetGeneration: command.datasetGeneration, kind });
    }
  } catch (caught) {
    if (command.datasetGeneration !== activeGeneration) return;
    const message = caught instanceof Error ? caught.message : "Map loading failed";
    post({ type: "ERROR", datasetGeneration: command.datasetGeneration, kind: "geometry", message });
  }
}

function requestAddOn(command: Extract<LoaderCommand, { type: "ADDON" }>) {
  if (command.datasetGeneration !== activeGeneration || loadedAddOns.has(command.kind)) return;
  if (!loadedScores) {
    pendingAddOns.add(command.kind);
    return;
  }
  if (addOnTasks.has(command.kind)) return;
  const task = fetchAddOn(loadedScores, command.kind)
    .then((addon) => {
      if (command.datasetGeneration !== activeGeneration) return;
      loadedScores = mergeMapScoreAddon(loadedScores!, addon);
      loadedAddOns.add(command.kind);
      post({ type: "ADDON", datasetGeneration: command.datasetGeneration, kind: command.kind, scores: loadedScores });
    })
    .catch((caught) => {
      if (command.datasetGeneration !== activeGeneration) return;
      post({
        type: "ADDON_ERROR", datasetGeneration: command.datasetGeneration, kind: command.kind,
        message: caught instanceof Error ? caught.message : "Map score add-on failed to load",
      });
    })
    .finally(() => addOnTasks.delete(command.kind));
  addOnTasks.set(command.kind, task);
}

function detailAsset(state: string): MapAssetEntry | undefined {
  return manifest?.files.find((asset) => asset.level === "tract" && asset.lod === "detail" && asset.jurisdiction === state);
}

async function loadDetail(item: { state: string; generation: number }) {
  const key = `${item.generation}:${item.state}`;
  inFlight.add(key);
  try {
    const asset = detailAsset(item.state);
    if (!asset) throw new Error(`Detailed tract asset is missing for ${item.state}`);
    const started = performance.now();
    const features = featuresFrom(await json<Topology>(assetUrl(asset)));
    profile(item.generation, {
      name: "detail-loaded", start: started, duration: performance.now() - started,
      details: { state: item.state, features: features.length },
    });
    post({ type: "DETAIL", datasetGeneration: item.generation, state: item.state, features });
  } catch (caught) {
    const reason = caught instanceof Error ? caught.message : "request failed";
    post({
      type: "ERROR", datasetGeneration: item.generation, kind: "detail", state: item.state,
      message: `Detailed tract request failed for ${item.state}: ${reason}`,
    });
  } finally {
    inFlight.delete(key);
    pump();
  }
}

function pump() {
  while (inFlight.size < 4 && pending.size) {
    const next = [...pending.values()].sort((a, b) => b.priority - a.priority || a.order - b.order)[0];
    pending.delete(`${next.generation}:${next.state}`);
    void loadDetail(next);
  }
}

function queueDetails(command: Extract<LoaderCommand, { type: "DETAIL" }>) {
  for (const request of command.requests) {
    const key = `${command.datasetGeneration}:${request.state}`;
    if (inFlight.has(key)) continue;
    const existing = pending.get(key);
    if (existing) existing.priority = Math.max(existing.priority, request.priority);
    else pending.set(key, { ...request, generation: command.datasetGeneration, order: sequence++ });
  }
  pump();
}

function connected(event: MessageEvent<LoaderCommand>) {
  const command = event.data;
  if (command.type === "LOAD") void load(command);
  else if (command.type === "ADDON") requestAddOn(command);
  else if (command.type === "DETAIL") queueDetails(command);
  else {
    pending.clear();
    rendererPort?.close();
    self.close();
  }
}

self.onmessage = (event: MessageEvent<LoaderBootstrap>) => {
  if (event.data.type !== "CONNECT") return;
  rendererPort = event.data.port;
  rendererPort.onmessage = connected;
  rendererPort.start();
};
