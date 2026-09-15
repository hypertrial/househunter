import { describe, expect, it } from "vitest";
import {
  decodeMapScoreAddon, decodeMapScores, mergeMapScoreAddon, requestedMapAddons,
} from "./mapScores";

const payload = {
  schema_version: 4,
  build_id: "fixture",
  level: "tract",
  scope: { kind: "national", state: null },
  columns: {
    place_id: ["01001000100", "01001000200"],
    risk_score: [10, null],
    community_conditions_group: [2, null],
    mountain_magnitude: [2.5, null],
    cost_of_living_index: [91.2, null],
    home_buying_power_percentile: [75, null],
    home_sqft_for_1m: [4200, null],
    housing_built_2000_plus_pct: [32.5, null],
  },
};

describe("decodeMapScores", () => {
  it("accepts the compact ordered score contract without hydrating rows", () => {
    const decoded = decodeMapScores(payload, "fixture", "tract");
    expect(decoded.columns).toEqual(payload.columns);
    expect(decoded.columns.place_id).toHaveLength(2);
    expect(decoded).not.toHaveProperty("rows");
  });

  it.each([
    [{ ...payload, schema_version: 1 }, "schema"],
    [{ ...payload, schema_version: 2 }, "schema"],
    [{ ...payload, build_id: "stale" }, "build"],
    [{ ...payload, level: "county" }, "level"],
    [{ ...payload, scope: null }, "scope"],
    [{ ...payload, scope: { kind: "national", state: "CO" } }, "scope"],
    [{ ...payload, scope: { kind: "state", state: null } }, "scope"],
    [{ ...payload, scope: { kind: "state", state: "co" } }, "scope"],
    [{ ...payload, scope: { kind: "national", state: null, extra: true } }, "scope"],
    [{ ...payload, columns: null }, "schema"],
    [{ ...payload, columns: { ...payload.columns, place_id: "not-an-array" } }, "columns"],
    [{ ...payload, columns: { ...payload.columns, risk_score: [10] } }, "length"],
    [{ ...payload, columns: { ...payload.columns, place_id: ["2", "1"] } }, "ordered"],
    [{ ...payload, columns: { ...payload.columns, place_id: ["1", "1"] } }, "unique"],
    [{ ...payload, columns: { ...payload.columns, place_id: ["1", 2] } }, "place ID"],
    [{ ...payload, columns: { ...payload.columns, risk_score: [Number.NaN, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, risk_score: [Number.POSITIVE_INFINITY, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, mountain_magnitude: [Number.NEGATIVE_INFINITY, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, mountain_magnitude: [-0.1, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, cost_of_living_index: [0, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, home_buying_power_percentile: [101, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, home_sqft_for_1m: [-1, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, housing_built_2000_plus_pct: [101, null] } }, "value"],
    [{ ...payload, columns: { ...payload.columns, community_conditions_group: [0, null] } }, "Community"],
    [{ ...payload, columns: { ...payload.columns, community_conditions_group: [11, null] } }, "Community"],
    [{ ...payload, columns: { ...payload.columns, community_conditions_group: [1.5, null] } }, "Community"],
  ])("rejects an invalid %s payload", (invalid, message) => {
    expect(() => decodeMapScores(invalid, "fixture", "tract")).toThrow(message);
  });

  it("preserves nulls in every nullable column", () => {
    const decoded = decodeMapScores({
      ...payload,
      columns: {
        ...payload.columns,
        risk_score: [null, null],
        community_conditions_group: [null, null],
        mountain_magnitude: [null, null],
        cost_of_living_index: [null, null],
        home_buying_power_percentile: [null, null],
        home_sqft_for_1m: [null, null],
        housing_built_2000_plus_pct: [null, null],
      },
    }, "fixture", "tract");
    expect(decoded.columns.risk_score).toEqual([null, null]);
    expect(decoded.columns.community_conditions_group).toEqual([null, null]);
    expect(decoded.columns.mountain_magnitude).toEqual([null, null]);
  });

  it("accepts the lazy core contract and merges independently validated add-ons", () => {
    const core = decodeMapScores({
      ...payload,
      add_ons: {
        cost_of_living: "/api/v2/map/scores/addons/cost-of-living?level=tract&build_id=fixture",
        home_costs: "/api/v2/map/scores/addons/home-costs?level=tract&build_id=fixture",
      },
      columns: {
        place_id: payload.columns.place_id,
        risk_score: payload.columns.risk_score,
        community_conditions_group: payload.columns.community_conditions_group,
        mountain_magnitude: payload.columns.mountain_magnitude,
      },
    }, "fixture", "tract");
    expect(core.columns.cost_of_living_index).toEqual([null, null]);
    const cost = decodeMapScoreAddon({
      schema_version: 1, kind: "cost-of-living", build_id: "fixture", level: "tract",
      scope: payload.scope,
      columns: {
        place_id: payload.columns.place_id,
        cost_of_living_index: payload.columns.cost_of_living_index,
      },
    }, core, "cost-of-living");
    const home = decodeMapScoreAddon({
      schema_version: 1, kind: "home-costs", build_id: "fixture", level: "tract",
      scope: payload.scope,
      columns: {
        place_id: payload.columns.place_id,
        home_buying_power_percentile: payload.columns.home_buying_power_percentile,
        home_sqft_for_1m: payload.columns.home_sqft_for_1m,
        housing_built_2000_plus_pct: payload.columns.housing_built_2000_plus_pct,
      },
    }, mergeMapScoreAddon(core, cost), "home-costs");
    expect(mergeMapScoreAddon(mergeMapScoreAddon(core, cost), home).columns)
      .toEqual(payload.columns);
  });

  it.each([
    [{ schema_version: 2, kind: "cost-of-living" }, "schema"],
    [{ schema_version: 1, kind: "home-costs" }, "schema"],
    [{ schema_version: 1, kind: "cost-of-living", build_id: "stale" }, "build"],
    [{ schema_version: 1, kind: "cost-of-living", build_id: "fixture", level: "county" }, "level"],
  ])("rejects mismatched lazy add-on identity: %s", (overrides, message) => {
    const full = decodeMapScores(payload, "fixture", "tract");
    expect(() => decodeMapScoreAddon(Object.assign({
      schema_version: 1, kind: "cost-of-living", build_id: "fixture", level: "tract",
      scope: payload.scope,
      columns: {
        place_id: payload.columns.place_id,
        cost_of_living_index: payload.columns.cost_of_living_index,
      },
    }, overrides), full, "cost-of-living")).toThrow(message);
  });

  it("requests only add-ons needed by the active layer or an explicit filter", () => {
    const filters = {
      state: "", county: "", showUnavailable: false, mountainMagnitudeMin: null,
      communityConditionsGroupMax: null, costOfLivingIndexMax: null,
      homeSqftFor1mMin: null, housingBuilt2000PlusPctMin: null,
    };
    expect(requestedMapAddons("fema", filters)).toEqual([]);
    expect(requestedMapAddons("cost-of-living", filters)).toEqual(["cost-of-living"]);
    expect(requestedMapAddons("home-costs", filters)).toEqual(["home-costs"]);
    expect(requestedMapAddons("fema", {
      ...filters, costOfLivingIndexMax: 100, housingBuilt2000PlusPctMin: 25,
    })).toEqual(["cost-of-living", "home-costs"]);
  });
});
