import type { MapFeature, Bounds } from "./mapGeometry";
import type { CameraState } from "./map";
import type { Geography, MapFilters, MapManifest, MapScore, MapScoreAddonKind, MapScores, Metric } from "./types";

export interface MapTransform { k: number; x: number; y: number }

export interface MapFocusTarget {
  kind: "place" | "state" | "county";
  id: string;
  nonce: number;
}

export interface MapPickPreview {
  placeId: string;
  name: string;
  state: string;
  score: MapScore | null;
}

export interface MapSemantics extends MapFilters {
  metric: Metric;
  neutralOnly: boolean;
}

export interface ProfileEntry {
  name: string;
  start: number;
  duration?: number;
  details?: Record<string, string | number | boolean>;
}

export type RendererCommand =
  | { type: "INIT"; loaderPort: MessagePort; datasetGeneration: number; viewportGeneration: number; cameraGeneration: number; semanticGeneration: number; manifestUrl: string; scoreUrl: string; expectedBuildId: string; level: Geography; width: number; height: number; ratio: number; camera: MapTransform; semantics: MapSemantics; selected: string }
  | { type: "RESIZE"; datasetGeneration: number; viewportGeneration: number; cameraGeneration: number; width: number; height: number; ratio: number; camera: MapTransform }
  | { type: "SET_CAMERA"; datasetGeneration: number; cameraGeneration: number; camera: MapTransform }
  | { type: "SET_SEMANTICS"; datasetGeneration: number; semanticGeneration: number; semantics: MapSemantics }
  | { type: "SET_SELECTION"; datasetGeneration: number; semanticGeneration: number; selected: string }
  | { type: "FOCUS"; datasetGeneration: number; requestId: number; snapshotId: number; target: MapFocusTarget }
  | { type: "PICK"; datasetGeneration: number; requestId: number; mode: "hover" | "activate"; snapshotId: number; x: number; y: number; camera: MapTransform }
  | { type: "RETRY"; datasetGeneration: number }
  | { type: "RETRY_ADDON"; datasetGeneration: number; kind: MapScoreAddonKind }
  | { type: "FRAME_COMMITTED"; datasetGeneration: number; snapshotId: number; presented: boolean }
  | { type: "DISPOSE" };

export type RendererEvent =
  | { type: "SCORES_READY"; datasetGeneration: number; count: number }
  | { type: "FRAME"; datasetGeneration: number; viewportGeneration: number; cameraGeneration: number; snapshotId: number; semanticGeneration: number; metric: Metric; camera: MapTransform; originX: number; originY: number; width: number; height: number; ratio: number; featureCount: number; interactive: boolean; bitmap: ImageBitmap }
  | { type: "FRAME_REUSED"; datasetGeneration: number; viewportGeneration: number; cameraGeneration: number; snapshotId: number; semanticGeneration: number; metric: Metric; camera: MapTransform; originX: number; originY: number; featureCount: number; interactive: boolean }
  | { type: "READY"; datasetGeneration: number; viewportGeneration: number; snapshotId: number; featureCount: number; level: Geography }
  | { type: "PICK_RESULT"; datasetGeneration: number; requestId: number; mode: "hover" | "activate"; snapshotId: number; preview: MapPickPreview | null }
  | { type: "FOCUS_RESULT"; datasetGeneration: number; requestId: number; snapshotId: number; bounds: Bounds | null }
  | { type: "STATUS"; datasetGeneration: number; message: string }
  | { type: "ERROR"; datasetGeneration: number; kind: "score" | "geometry" | "detail" | "worker"; message: string }
  | { type: "ADDON_ERROR"; datasetGeneration: number; kind: MapScoreAddonKind; message: string }
  | { type: "ADDON_READY"; datasetGeneration: number; kind: MapScoreAddonKind }
  | { type: "PROFILE"; datasetGeneration: number; entry: ProfileEntry };

export type LoaderCommand =
  | { type: "LOAD"; datasetGeneration: number; manifestUrl: string; scoreUrl: string; expectedBuildId: string; level: Geography; neutralOnly: boolean }
  | { type: "ADDON"; datasetGeneration: number; kind: MapScoreAddonKind }
  | { type: "DETAIL"; datasetGeneration: number; requests: Array<{ state: string; priority: number }> }
  | { type: "DISPOSE" };

export type LoaderEvent =
  | { type: "STATES"; datasetGeneration: number; manifest: MapManifest; states: MapFeature[] }
  | { type: "DATASET"; datasetGeneration: number; manifest: MapManifest; scores: MapScores | null; loadedAddOns: MapScoreAddonKind[]; states: MapFeature[]; features: MapFeature[] }
  | { type: "ADDON"; datasetGeneration: number; kind: MapScoreAddonKind; scores: MapScores }
  | { type: "DETAIL"; datasetGeneration: number; state: string; features: MapFeature[] }
  | { type: "ERROR"; datasetGeneration: number; kind: "score" | "geometry" | "detail"; state?: string; message: string }
  | { type: "ADDON_ERROR"; datasetGeneration: number; kind: MapScoreAddonKind; message: string }
  | { type: "PROFILE"; datasetGeneration: number; entry: ProfileEntry };

export type LoaderBootstrap = { type: "CONNECT"; port: MessagePort };

export const cameraTransform = (camera: CameraState, width: number, height: number): MapTransform => ({
  k: camera.z,
  x: width / 2 - camera.cx * width * camera.z,
  y: height / 2 - camera.cy * height * camera.z,
});
