import { geoContains } from "d3-geo";
import { describe, expect, it } from "vitest";
import { boundsIntersect, buildSpatialGrid, detailEvictions, featuresFrom, gridCandidates, type ProjectedFeature } from "./mapGeometry";

const item = (id: string, bounds: [[number, number], [number, number]]): ProjectedFeature => ({
  feature: { type: "Feature", id, properties: {}, geometry: { type: "Polygon", coordinates: [] } },
  id, state: "CO", countyFips: "08013", name: id, bounds, sourceIndex: Number(id),
});

describe("map spatial grid", () => {
  it("returns local candidates in source order", () => {
    const grid = buildSpatialGrid([item("1", [[0, 0], [10, 10]]), item("2", [[8, 8], [20, 20]])], 100, 100);
    expect(gridCandidates(grid, 9, 9)).toEqual([0, 1]);
    expect(gridCandidates(grid, 90, 90)).toEqual([]);
  });

  it("keeps exceptionally broad features in the overflow list", () => {
    const grid = buildSpatialGrid([item("1", [[-1000, -1000], [1000, 1000]])], 100, 100);
    expect(grid.overflow).toEqual([0]);
    expect(gridCandidates(grid, 50, 50)).toEqual([0]);
  });

  it("clamps cells at both sizing boundaries", () => {
    const crowded = Array.from({ length: 100 }, (_, index) => item(String(index), [[0, 0], [1, 1]]));
    expect(buildSpatialGrid(crowded, 10, 10).cellSize).toBe(8);
    expect(buildSpatialGrid([item("1", [[0, 0], [1, 1]])], 1_000, 1_000).cellSize).toBe(64);
  });

  it("indexes a feature occupying exactly 256 cells and overflows the next cell", () => {
    const indexed = buildSpatialGrid([item("1", [[0, 0], [1_023, 1_023]])], 64, 64);
    expect(indexed.cellSize).toBe(64);
    expect(indexed.overflow).toEqual([]);
    expect(gridCandidates(indexed, 1_023, 1_023)).toEqual([0]);

    const overflow = buildSpatialGrid([item("1", [[0, 0], [1_087, 1_023]])], 64, 64);
    expect(overflow.overflow).toEqual([0]);
  });

  it("keeps negative projected coordinates queryable", () => {
    const grid = buildSpatialGrid([item("1", [[-20, -20], [-1, -1]])], 100, 100);
    expect(gridCandidates(grid, -10, -10)).toEqual([0]);
  });

  it("normalizes polygon winding without filling interior holes", () => {
    const [feature] = featuresFrom({
      type: "Topology",
      objects: { geography: { type: "GeometryCollection", geometries: [{ type: "Polygon", id: "1", properties: {}, arcs: [[0], [1]] }] } },
      arcs: [
        [[-109, 41], [-102, 41], [-102, 37], [-109, 37], [-109, 41]],
        [[-106.4, 39.6], [-106.4, 38.4], [-104.6, 38.4], [-104.6, 39.6], [-106.4, 39.6]],
      ],
    });
    expect(geoContains(feature, [-108, 39])).toBe(true);
    expect(geoContains(feature, [-105.5, 39])).toBe(false);
  });
});

describe("map viewport and detail-cache policy", () => {
  it("uses inclusive viewport intersections", () => {
    expect(boundsIntersect([[0, 0], [10, 10]], [[10, 4], [20, 8]])).toBe(true);
    expect(boundsIntersect([[0, 0], [9, 10]], [[10, 4], [20, 8]])).toBe(false);
  });

  it("exempts visible states and retains the 24 most-recent non-visible states", () => {
    const items = Array.from({ length: 28 }, (_, index) => ({
      state: `S${index}`,
      lastUsed: index,
      featureCount: 2_000,
    }));
    expect(detailEvictions(items, new Set(["S0"]))).toEqual(["S1", "S2", "S3"]);
    expect(detailEvictions(items, new Set(["S27"]))).toEqual(["S0", "S1", "S2"]);
  });

  it("does not evict at either exact cache ceiling", () => {
    const items = Array.from({ length: 24 }, (_, index) => ({
      state: `S${index}`, lastUsed: index, featureCount: 2_500,
    }));
    expect(detailEvictions(items, new Set())).toEqual([]);
  });

  it("never evicts visible states when the non-visible cache is within its ceiling", () => {
    const items = [
      { state: "VISIBLE", lastUsed: 0, featureCount: 30_000 },
      { state: "CACHED", lastUsed: 1, featureCount: 1 },
    ];
    expect(detailEvictions(items, new Set(["VISIBLE"]))).toEqual([]);
  });

  it("evicts least-recently-used non-visible states first", () => {
    const items = Array.from({ length: 25 }, (_, index) => ({
      state: index === 0 ? "old" : `S${index}`, lastUsed: index, featureCount: 1_000,
    }));
    expect(detailEvictions(items, new Set())).toEqual(["old"]);
  });
});
