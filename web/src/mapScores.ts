import type {
  Geography, MapFilters, MapScoreAddon, MapScoreAddonKind, MapScores, Metric,
} from "./types";

const numberOrNull = (value: unknown) => value === null
  || (typeof value === "number" && Number.isFinite(value));

function validScope(scope: unknown): boolean {
  if (!scope || typeof scope !== "object") return false;
  const value = scope as Record<string, unknown>;
  return Object.keys(value).sort().join(",") === "kind,state" && (
    (value.kind === "national" && value.state === null)
    || (value.kind === "state" && typeof value.state === "string"
      && /^[A-Z]{2}$/.test(value.state))
  );
}

function validAddonUrls(value: unknown): value is NonNullable<MapScores["add_ons"]> {
  if (!value || typeof value !== "object") return false;
  const urls = value as Record<string, unknown>;
  return Object.keys(urls).sort().join(",") === "cost_of_living,home_costs"
    && typeof urls.cost_of_living === "string"
    && urls.cost_of_living.startsWith("/api/v3/map/scores/addons/cost-of-living?")
    && typeof urls.home_costs === "string"
    && urls.home_costs.startsWith("/api/v3/map/scores/addons/home-costs?");
}

export function decodeMapScores(
  value: unknown,
  expectedBuildId: string,
  expectedLevel: Geography,
): MapScores {
  if (!value || typeof value !== "object") throw new Error("Map score schema is invalid");
  const payload = value as Partial<MapScores>;
  if (payload.schema_version !== 5 || !payload.columns) {
    throw new Error("Map score schema is unsupported");
  }
  if (payload.build_id !== expectedBuildId) throw new Error("Map score build is stale");
  if (payload.level !== expectedLevel) throw new Error("Map score level is stale");
  if (!validScope(payload.scope)) throw new Error("Map score scope is invalid");
  const {
    place_id,
    res_hazard_npctl,
    community_conditions_group,
    mountain_magnitude,
    cost_of_living_index,
    home_buying_power_percentile,
    home_sqft_for_1m,
    housing_built_2000_plus_pct,
  } = payload.columns;
  const coreColumns = [
    place_id,
    res_hazard_npctl,
    community_conditions_group,
    mountain_magnitude,
  ];
  if (!coreColumns.every(Array.isArray)) {
    throw new Error("Map score columns are invalid");
  }
  const optionalColumns = [
    cost_of_living_index,
    home_buying_power_percentile,
    home_sqft_for_1m,
    housing_built_2000_plus_pct,
  ];
  const full = optionalColumns.every(Array.isArray);
  const core = optionalColumns.every((column) => column === undefined)
    && validAddonUrls(payload.add_ons);
  if (!full && !core) throw new Error("Map score columns are invalid");
  const nulls = () => Array(place_id.length).fill(null) as Array<number | null>;
  const normalized: MapScores = {
    ...(payload as MapScores),
    columns: {
      place_id,
      res_hazard_npctl,
      community_conditions_group,
      mountain_magnitude,
      cost_of_living_index: full ? cost_of_living_index : nulls(),
      home_buying_power_percentile: full ? home_buying_power_percentile : nulls(),
      home_sqft_for_1m: full ? home_sqft_for_1m : nulls(),
      housing_built_2000_plus_pct: full ? housing_built_2000_plus_pct : nulls(),
    },
  };
  const columns = Object.values(normalized.columns);
  if (new Set(columns.map((column) => column.length)).size !== 1) {
    throw new Error("Map score column length mismatch");
  }
  for (let index = 0; index < place_id.length; index += 1) {
    if (typeof place_id[index] !== "string") throw new Error("Map score place ID is invalid");
    if (index && place_id[index - 1] >= place_id[index]) {
      throw new Error(place_id[index - 1] === place_id[index]
        ? "Map score place IDs must be unique"
        : "Map score place IDs must be ordered");
    }
    const magnitude = normalized.columns.mountain_magnitude[index];
    const cost = normalized.columns.cost_of_living_index[index];
    const percentile = normalized.columns.home_buying_power_percentile[index];
    const squareFeet = normalized.columns.home_sqft_for_1m[index];
    const built2000 = normalized.columns.housing_built_2000_plus_pct[index];
    if (![res_hazard_npctl[index], magnitude, cost, percentile, squareFeet, built2000]
      .every(numberOrNull)
      || (res_hazard_npctl[index] !== null && (res_hazard_npctl[index]! < 0 || res_hazard_npctl[index]! > 100))
      || (magnitude !== null && magnitude < 0)
      || (cost !== null && cost <= 0)
      || (percentile !== null && (percentile < 0 || percentile > 100))
      || (squareFeet !== null && squareFeet <= 0)
      || (built2000 !== null && (built2000 < 0 || built2000 > 100))) {
      throw new Error("Map score value is invalid");
    }
    const group = normalized.columns.community_conditions_group[index];
    if (group !== null && (!Number.isInteger(group) || group < 1 || group > 10)) {
      throw new Error("Map score Community Conditions group is invalid");
    }
  }
  return normalized;
}

export function requestedMapAddons(metric: Metric, filters: MapFilters): MapScoreAddonKind[] {
  const requested: MapScoreAddonKind[] = [];
  if (metric === "cost-of-living" || filters.costOfLivingIndexMax !== null) {
    requested.push("cost-of-living");
  }
  if (metric === "home-costs" || filters.homeSqftFor1mMin !== null
    || filters.housingBuilt2000PlusPctMin !== null) {
    requested.push("home-costs");
  }
  return requested;
}

export function loadedMapAddons(scores: MapScores): MapScoreAddonKind[] {
  return scores.add_ons ? [] : ["cost-of-living", "home-costs"];
}

export function decodeMapScoreAddon(
  value: unknown,
  expected: MapScores,
  kind: MapScoreAddonKind,
): MapScoreAddon {
  if (!value || typeof value !== "object") throw new Error("Map score add-on schema is invalid");
  const payload = value as Partial<MapScoreAddon>;
  if (payload.schema_version !== 1 || payload.kind !== kind || !payload.columns) {
    throw new Error("Map score add-on schema is unsupported");
  }
  if (payload.build_id !== expected.build_id) throw new Error("Map score add-on build is stale");
  if (payload.level !== expected.level) throw new Error("Map score add-on level is stale");
  if (!validScope(payload.scope)
    || JSON.stringify(payload.scope) !== JSON.stringify(expected.scope)) {
    throw new Error("Map score add-on scope is invalid");
  }
  const ids = payload.columns.place_id;
  const expectedIds = expected.columns.place_id;
  if (!Array.isArray(ids) || ids.length !== expectedIds.length
    || ids.some((id, index) => id !== expectedIds[index])) {
    throw new Error("Map score add-on place IDs do not align");
  }
  const cost = payload.columns.cost_of_living_index;
  const percentile = payload.columns.home_buying_power_percentile;
  const squareFeet = payload.columns.home_sqft_for_1m;
  const built2000 = payload.columns.housing_built_2000_plus_pct;
  const columns = kind === "cost-of-living"
    ? [cost]
    : [percentile, squareFeet, built2000];
  if (!columns.every((column) => Array.isArray(column) && column.length === ids.length)) {
    throw new Error("Map score add-on column length mismatch");
  }
  for (let index = 0; index < ids.length; index += 1) {
    if (kind === "cost-of-living") {
      const valueAtIndex = cost![index];
      if (!numberOrNull(valueAtIndex) || (valueAtIndex !== null && valueAtIndex <= 0)) {
        throw new Error("Map score add-on value is invalid");
      }
    } else {
      const percentileAtIndex = percentile![index];
      const squareFeetAtIndex = squareFeet![index];
      const builtAtIndex = built2000![index];
      if (![percentileAtIndex, squareFeetAtIndex, builtAtIndex].every(numberOrNull)
        || (percentileAtIndex !== null && (percentileAtIndex < 0 || percentileAtIndex > 100))
        || (squareFeetAtIndex !== null && squareFeetAtIndex <= 0)
        || (builtAtIndex !== null && (builtAtIndex < 0 || builtAtIndex > 100))) {
        throw new Error("Map score add-on value is invalid");
      }
    }
  }
  return payload as MapScoreAddon;
}

export function mergeMapScoreAddon(scores: MapScores, addon: MapScoreAddon): MapScores {
  return {
    ...scores,
    columns: { ...scores.columns, ...addon.columns, place_id: scores.columns.place_id },
  };
}
