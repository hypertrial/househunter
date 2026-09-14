import type { Geography, MapScores } from "./types";

const numberOrNull = (value: unknown) => value === null
  || (typeof value === "number" && Number.isFinite(value));

export function decodeMapScores(
  value: unknown,
  expectedBuildId: string,
  expectedLevel: Geography,
): MapScores {
  if (!value || typeof value !== "object") throw new Error("Map score schema is invalid");
  const payload = value as Partial<MapScores>;
  if (payload.schema_version !== 3 || !payload.columns) {
    throw new Error("Map score schema is unsupported");
  }
  if (payload.build_id !== expectedBuildId) throw new Error("Map score build is stale");
  if (payload.level !== expectedLevel) throw new Error("Map score level is stale");
  const scope = payload.scope as Record<string, unknown> | undefined;
  const scopeKeys = scope && Object.keys(scope).sort().join(",");
  const validScope = scopeKeys === "kind,state" && (
    (scope?.kind === "national" && scope.state === null)
    || (scope?.kind === "state" && typeof scope.state === "string"
      && /^[A-Z]{2}$/.test(scope.state))
  );
  if (!validScope) throw new Error("Map score scope is invalid");
  const { place_id, risk_score, community_conditions_group, mountain_magnitude } = payload.columns;
  if (![place_id, risk_score, community_conditions_group, mountain_magnitude].every(Array.isArray)) {
    throw new Error("Map score columns are invalid");
  }
  if (new Set([place_id.length, risk_score.length, community_conditions_group.length, mountain_magnitude.length]).size !== 1) {
    throw new Error("Map score column length mismatch");
  }
  for (let index = 0; index < place_id.length; index += 1) {
    if (typeof place_id[index] !== "string") throw new Error("Map score place ID is invalid");
    if (index && place_id[index - 1] >= place_id[index]) {
      throw new Error(place_id[index - 1] === place_id[index]
        ? "Map score place IDs must be unique"
        : "Map score place IDs must be ordered");
    }
    const magnitude = mountain_magnitude[index];
    if (!numberOrNull(risk_score[index]) || !numberOrNull(magnitude)
      || (magnitude !== null && magnitude < 0)) {
      throw new Error("Map score value is invalid");
    }
    const group = community_conditions_group[index];
    if (group !== null && (!Number.isInteger(group) || group < 1 || group > 10)) {
      throw new Error("Map score Community Conditions group is invalid");
    }
  }
  return payload as MapScores;
}
