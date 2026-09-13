import { geoAlbersUsa, geoArea, geoMercator, type GeoProjection } from "d3-geo";
import { feature as topoFeature } from "topojson-client";
import type { Feature, FeatureCollection, Geometry, GeoJsonProperties } from "geojson";
import type { GeometryCollection, Topology } from "topojson-specification";

export type Bounds = [[number, number], [number, number]];

export type MapFeature = Feature<Geometry, GeoJsonProperties & {
  place_id?: string;
  state?: string;
  county_fips?: string;
  name?: string;
  label?: [number, number];
}>;

export interface ProjectedFeature {
  feature: MapFeature;
  id: string;
  state: string;
  countyFips: string;
  name: string;
  bounds: Bounds;
  sourceIndex: number;
}

export interface SpatialGrid {
  cellSize: number;
  cells: Map<string, number[]>;
  overflow: number[];
}

export const TERRITORY_BOXES: Record<string, [number, number, number, number]> = {
  GU: [0.04, 0.76, 0.10, 0.89],
  MP: [0.16, 0.76, 0.22, 0.89],
  AS: [0.25, 0.76, 0.34, 0.89],
  PR: [0.72, 0.76, 0.86, 0.89],
  VI: [0.88, 0.76, 0.95, 0.89],
};

export const collection = (features: MapFeature[]): FeatureCollection<Geometry> => ({
  type: "FeatureCollection",
  features,
});

function geometryCollection(topology: Topology): GeometryCollection<Record<string, unknown>> {
  const object = topology.objects.geography;
  if (!object || object.type !== "GeometryCollection") throw new Error("Topology has no geography collection");
  return object as GeometryCollection<Record<string, unknown>>;
}

export function featuresFrom(topology: Topology): MapFeature[] {
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

export function projectors(width: number, height: number, stateFeatures: MapFeature[]) {
  const territories = new Set(Object.keys(TERRITORY_BOXES));
  const base = stateFeatures.filter((item) => !territories.has(stateCode(item)));
  const main = geoAlbersUsa().fitExtent(
    [[24, 64], [Math.max(25, width - 24), Math.max(65, height - 112)]],
    collection(base),
  );
  const byState = new Map<string, GeoProjection>();
  for (const [state, box] of Object.entries(TERRITORY_BOXES)) {
    const geography = stateFeatures.filter((item) => stateCode(item) === state);
    if (!geography.length) continue;
    byState.set(state, geoMercator().fitExtent(
      [[width * box[0], height * box[1]], [width * box[2], height * box[3]]],
      collection(geography),
    ));
  }
  return { main, byState };
}

export type Projectors = ReturnType<typeof projectors>;

export function stateCode(item: MapFeature): string {
  return String(item.properties?.state || item.id || "");
}

export function featureId(item: MapFeature): string {
  return String(item.id || item.properties?.place_id || "");
}

export function projectionFor(item: MapFeature, value: Projectors): GeoProjection {
  return value.byState.get(stateCode(item)) || value.main;
}

export function boundsIntersect(a: Bounds, b: Bounds): boolean {
  return a[1][0] >= b[0][0] && a[0][0] <= b[1][0]
    && a[1][1] >= b[0][1] && a[0][1] <= b[1][1];
}

export function mergeBounds(a: Bounds | undefined, b: Bounds): Bounds {
  if (!a) return [[b[0][0], b[0][1]], [b[1][0], b[1][1]]];
  return [
    [Math.min(a[0][0], b[0][0]), Math.min(a[0][1], b[0][1])],
    [Math.max(a[1][0], b[1][0]), Math.max(a[1][1], b[1][1])],
  ];
}

const cellKey = (x: number, y: number) => `${x}:${y}`;

export function buildSpatialGrid(features: ProjectedFeature[], width: number, height: number): SpatialGrid {
  const cellSize = Math.max(8, Math.min(64, Math.sqrt(width * height * 32 / Math.max(1, features.length))));
  const cells = new Map<string, number[]>();
  const overflow: number[] = [];
  features.forEach((item, index) => {
    const x0 = Math.floor(item.bounds[0][0] / cellSize);
    const y0 = Math.floor(item.bounds[0][1] / cellSize);
    const x1 = Math.floor(item.bounds[1][0] / cellSize);
    const y1 = Math.floor(item.bounds[1][1] / cellSize);
    if ((x1 - x0 + 1) * (y1 - y0 + 1) > 256) {
      overflow.push(index);
      return;
    }
    for (let y = y0; y <= y1; y += 1) {
      for (let x = x0; x <= x1; x += 1) {
        const key = cellKey(x, y);
        const bucket = cells.get(key);
        if (bucket) bucket.push(index);
        else cells.set(key, [index]);
      }
    }
  });
  return { cellSize, cells, overflow };
}

export function gridCandidates(grid: SpatialGrid, x: number, y: number): number[] {
  return [...(grid.cells.get(cellKey(Math.floor(x / grid.cellSize), Math.floor(y / grid.cellSize))) || []), ...grid.overflow];
}

export interface DetailCacheItem {
  state: string;
  lastUsed: number;
  featureCount: number;
}

export function detailEvictions(
  items: DetailCacheItem[],
  visible: ReadonlySet<string>,
  maxStates = 24,
  maxFeatures = Infinity,
): string[] {
  const candidates = items.filter((item) => !visible.has(item.state)).sort((a, b) => a.lastUsed - b.lastUsed);
  let states = candidates.length;
  let features = candidates.reduce((total, item) => total + item.featureCount, 0);
  const evicted: string[] = [];
  for (const item of candidates) {
    if (states <= maxStates && features <= maxFeatures) break;
    evicted.push(item.state);
    states -= 1;
    features -= item.featureCount;
  }
  return evicted;
}
