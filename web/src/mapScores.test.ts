import { describe, expect, it } from "vitest";
import { decodeMapScores } from "./mapScores";

const payload = {
  schema_version: 2,
  build_id: "fixture",
  level: "tract",
  scope: { kind: "national", state: null },
  columns: {
    place_id: ["01001000100", "01001000200"],
    risk_score: [10, null],
    community_conditions_group: [2, null],
    mountain_score: [75, null],
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
    [{ ...payload, columns: { ...payload.columns, mountain_score: [Number.NEGATIVE_INFINITY, null] } }, "value"],
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
        mountain_score: [null, null],
      },
    }, "fixture", "tract");
    expect(decoded.columns.risk_score).toEqual([null, null]);
    expect(decoded.columns.community_conditions_group).toEqual([null, null]);
    expect(decoded.columns.mountain_score).toEqual([null, null]);
  });
});
