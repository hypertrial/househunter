import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import RiskMap, { type FocusTarget, type MapPreview } from "./RiskMap";
import {
  COUNTY_FIT_PILLARS,
  COUNTY_FIT_POPULATION_FLOOR,
  COUNTY_FIT_PRESETS,
  COUNTY_FIT_VIEWS,
  EMPTY_COUNTY_FIT_FILTERS,
  countyFitFiltersValid,
  countyFitParams,
  countyFitRows,
  countyFitWeightsValid,
  readCountyFitHash,
  type CountyFitFilters,
} from "./countyFit";
import { COUNTY_FIT_GRADIENT, STATE_ABBREVIATIONS, type CameraState } from "./map";
import type {
  CountyFitDetail, CountyFitPillar, CountyFitSummary, CountyFitView, MapFilters, Meta,
} from "./types";

const FIT_DISCLAIMER = "Approximate, project-authored policy preference rubric. It is not legal advice, a legal-compliance determination, school-quality evidence, or a recommendation. Laws and interpretations may change; verify current requirements with official state sources or qualified counsel.";
const HISTORY_COMMANDS = [
  "househunter import-home-market FILE --acknowledge-personal-use --history",
  "househunter build",
];
const MAP_FILTERS: MapFilters = {
  state: "", county: "", showUnavailable: true, mountainMagnitudeMin: null,
  communityConditionsGroupMax: null, costOfLivingIndexMax: null,
  homeSqftFor1mMin: null, housingBuilt2000PlusPctMin: null,
};
const PILLAR_LABELS: Record<CountyFitPillar, string> = {
  safety: "Safety Factors", health: "Health", affordability: "Affordability",
  opportunity: "Opportunity", lifestyle: "Mountain Landscape", family: "Homeschool Policy Fit",
};
const VIEW_DESCRIPTIONS: Record<CountyFitView, string> = {
  safety: "50% residential hazard, 35% reported crime, and 15% public-water violation share. Public-water coverage is context only; it excludes private wells and is not countywide water quality.",
  health: "Provider availability plus County Health Rankings & Roadmaps community context.",
  affordability: "Housing buying power, regional price parity, and property-tax inputs.",
  opportunity: "Employment growth, wages, commute time, and broadband availability.",
  lifestyle: "Mountain Magnitude only. Climate remains a filter and is not scored.",
  family: "Approximate state-level homeschool-policy rubric. Counties within a state tie.",
  custom: "A weighted combination of all six pillars using the selected preset or exact custom weights.",
};
const PRESET_LABELS: Record<string, string> = {
  balanced: "Balanced",
  "safety-health": "Safety + health",
  affordability: "Affordability",
  "mountain-lifestyle": "Mountain lifestyle",
};

function displayPercent(value: number | null): string {
  return value === null ? "Unavailable" : `${(value * 100).toFixed(1)}`;
}

function displayMeasure(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Unavailable";
  if (typeof value === "number") return Number.isInteger(value)
    ? value.toLocaleString("en-US") : value.toLocaleString("en-US", { maximumFractionDigits: 3 });
  return String(value).replaceAll("_", " ");
}

function exclusionLabel(reason: string | null | undefined, referenceOnly = false): string {
  if (reason === "population") {
    return referenceOnly ? "Population below 25,000" : "Below selected population minimum";
  }
  return displayMeasure(reason);
}

function apiFilters(filters: CountyFitFilters): Record<string, string | number | boolean | null> {
  const result: Record<string, string | number | boolean | null> = {
    state: filters.state,
    exclude_appalachia: filters.exclude_appalachia,
  };
  for (const [key, raw] of Object.entries(filters)) {
    if (key === "state" || key === "exclude_appalachia" || raw === "") continue;
    const value = Number(raw);
    result[key] = key.startsWith("min_") && COUNTY_FIT_PILLARS.includes(key.slice(4) as CountyFitPillar)
      ? value * 0.01 : value;
  }
  return result;
}

export function writeCountyFitHash(
  mode: "push" | "replace",
  state: {
    view: CountyFitView;
    preset: string;
    weights: Record<CountyFitPillar, number>;
    filters: CountyFitFilters;
    selected: string;
    camera: CameraState;
  },
) {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  for (const key of [...params.keys()]) {
    if (key === "workspace" || key.startsWith("fit_")) params.delete(key);
  }
  params.set("workspace", "county-fit");
  params.set("fit_view", state.view);
  params.set("fit_preset", state.preset);
  if (state.preset === "custom") {
    for (const pillar of COUNTY_FIT_PILLARS) params.set(`fit_weight_${pillar}`, String(state.weights[pillar]));
  }
  if (state.filters.state) params.set("fit_state", state.filters.state);
  if (state.filters.exclude_appalachia) params.set("fit_exclude_appalachia", "1");
  for (const [key, value] of Object.entries(state.filters)) {
    if (key === "state" || key === "exclude_appalachia" || value === "") continue;
    params.set(`fit_${key}`, String(value));
  }
  if (state.selected) params.set("fit_place", state.selected);
  params.set("fit_cx", state.camera.cx.toFixed(4));
  params.set("fit_cy", state.camera.cy.toFixed(4));
  params.set("fit_z", state.camera.z.toFixed(3));
  window.history[mode === "push" ? "pushState" : "replaceState"](null, "", `#${params}`);
}

function DetailDrawer({ detail, loading, error, onClose, onRetry }: {
  detail: CountyFitDetail | null;
  loading: boolean;
  error: string;
  onClose: () => void;
  onRetry: () => void;
}) {
  return <aside className="detail-drawer county-fit-detail" role="dialog" aria-modal="false" aria-label="County Fit details" aria-busy={loading}>
    <button autoFocus type="button" className="close" aria-label="Close County Fit details" onClick={onClose}>×</button>
    {loading && <p role="status">Loading county measures…</p>}
    {error && <div className="error" role="alert"><p>{error}</p><button className="secondary" onClick={onRetry}>Retry</button></div>}
    {detail && <>
      <p className="eyebrow">County Fit · {detail.county.fips}</p>
      <h2>{detail.county.name}, {detail.county.state}</h2>
      {detail.reference_only && <p className="notice"><strong>Reference only.</strong> This county is below the 25,000 population floor, so it has no County Fit score or rank. Component utilities remain visible for context.</p>}
      <dl className="facts">
        <div><dt>Population</dt><dd>{detail.population === null ? "—" : detail.population.toLocaleString()}</dd></div>
        <div><dt>Active value</dt><dd>{detail.reference_only ? "Not ranked" : displayPercent(detail.active_value)}</dd></div>
        <div><dt>Status</dt><dd>{detail.eligible ? "Included" : exclusionLabel(detail.exclusion_reason, detail.reference_only)}</dd></div>
        <div><dt>National rank</dt><dd>{detail.national_rank ?? "—"}</dd></div>
        <div><dt>Filtered rank</dt><dd>{detail.filtered_rank ?? "—"}</dd></div>
      </dl>
      {COUNTY_FIT_PILLARS.map((pillar) => {
        const item = detail.pillars[pillar];
        return <section className="fit-pillar-detail" key={pillar}>
          <div className="fit-pillar-heading"><h3>{PILLAR_LABELS[pillar]}</h3><strong>{displayPercent(item.utility)}</strong></div>
          <p>Weight {(item.weight * 100).toFixed(0)}% · contribution {item.contribution === null ? "—" : (item.contribution * 100).toFixed(1)}</p>
          <dl className="card-facts">{Object.entries(item.measures).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{displayMeasure(value)}</dd></div>)}</dl>
          {pillar === "safety" && typeof item.measures.public_water_coverage === "number" && <p className="notice">Public-system coverage proxy: {(item.measures.public_water_coverage * 100).toFixed(1)}%. It excludes private wells and is not countywide water quality.</p>}
          {pillar === "family" && <p className="notice">{FIT_DISCLAIMER}</p>}
        </section>;
      })}
      <details className="fit-technical"><summary>Coverage, vintages, citations, and methodology</summary>
        <h3>Coverage and quality</h3><dl className="card-facts">{Object.entries(detail.coverage).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{displayMeasure(value)}</dd></div>)}</dl>
        <h3>Vintages</h3><pre>{JSON.stringify(detail.vintages, null, 2)}</pre>
        <h3>Provider sources</h3><pre>{JSON.stringify(detail.sources, null, 2)}</pre>
        <h3>Citations</h3><pre>{JSON.stringify(detail.citations, null, 2)}</pre>
        {detail.rubric_components && <><h3>Homeschool Policy Fit rubric</h3><pre>{JSON.stringify(detail.rubric_components, null, 2)}</pre></>}
      </details>
      {detail.limitations.map((notice) => <p className="notice" key={notice}>{notice}</p>)}
    </>}
  </aside>;
}

export default function CountyFit({ meta, active, onMap }: {
  meta: Meta;
  active: boolean;
  onMap: () => void;
}) {
  const parsed = readCountyFitHash(window.location.hash);
  const initialView = parsed?.view ?? (meta.county_fit.readiness === "ready" ? "custom" : "safety");
  const viewAllowed = (value: CountyFitView) => value === "custom"
    ? meta.county_fit.readiness === "ready"
    : meta.county_fit.available_pillars.includes(value as CountyFitPillar);
  const [view, setView] = useState<CountyFitView>(viewAllowed(initialView) ? initialView : "safety");
  const [preset, setPreset] = useState(parsed?.preset ?? "balanced");
  const [weights, setWeights] = useState<Record<CountyFitPillar, number>>(parsed?.weights ?? { ...COUNTY_FIT_PRESETS.balanced });
  const [draftWeights, setDraftWeights] = useState(weights);
  const [filters, setFilters] = useState<CountyFitFilters>(parsed?.filters ?? { ...EMPTY_COUNTY_FIT_FILTERS });
  const [draftFilters, setDraftFilters] = useState(filters);
  const [summary, setSummary] = useState<CountyFitSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState(parsed?.selected ?? "");
  const [detail, setDetail] = useState<CountyFitDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailRetry, setDetailRetry] = useState(0);
  const [preview, setPreview] = useState<MapPreview | null>(null);
  const [focusTarget, setFocusTarget] = useState<FocusTarget | null>(() => parsed?.selected
    ? { kind: "place", id: parsed.selected, nonce: Date.now() } : null);
  const [visibleRows, setVisibleRows] = useState(100);
  const [panel, setPanel] = useState<"filters" | "weights" | "readiness" | null>(null);
  const [status, setStatus] = useState("County Fit is loading");
  const initialCamera = useRef<CameraState>(parsed?.camera ?? { cx: 0.5, cy: 0.5, z: 1 });
  const camera = useRef<CameraState>(initialCamera.current);
  const [cameraTarget, setCameraTarget] = useState<(CameraState & { nonce: number }) | undefined>();
  const detailTrigger = useRef<HTMLElement | null>(null);
  const restoring = useRef(false);
  const firstHash = useRef(true);
  const wasActive = useRef(active);
  const totalWeight = Object.values(draftWeights).reduce((total, value) => total + value, 0);
  const weightsValid = countyFitWeightsValid(draftWeights);
  const filtersValid = countyFitFiltersValid(draftFilters);
  const buildId = meta.build?.build_id || "";
  const params = useMemo(() => countyFitParams(
    buildId, view, preset, weights, apiFilters(filters),
  ), [buildId, filters, preset, view, weights]);
  const scoreUrl = `/api/v3/county-fit?${params}`;

  const updateHash = useCallback((mode: "push" | "replace") => {
    if (!active) return;
    writeCountyFitHash(mode, { view, preset, weights, filters, selected, camera: camera.current });
  }, [active, filters, preset, selected, view, weights]);

  useEffect(() => {
    if (!active) {
      wasActive.current = false;
      return;
    }
    if (!wasActive.current) {
      wasActive.current = true;
      return;
    }
    if (firstHash.current) firstHash.current = false;
    else if (restoring.current) restoring.current = false;
    else updateHash("push");
  }, [active, updateHash]);

  useEffect(() => {
    const restore = () => {
      const next = readCountyFitHash(window.location.hash);
      if (!next) return;
      restoring.current = true;
      setView(viewAllowed(next.view) ? next.view : "safety");
      setPreset(next.preset); setWeights(next.weights); setDraftWeights(next.weights);
      setFilters(next.filters); setDraftFilters(next.filters); setSelected(next.selected);
      camera.current = next.camera;
      setCameraTarget({ ...next.camera, nonce: Date.now() });
      setFocusTarget(next.selected ? { kind: "place", id: next.selected, nonce: Date.now() } : null);
      setPanel(null);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [meta.county_fit.available_pillars, meta.county_fit.readiness]);

  useEffect(() => {
    if (!active || meta.county_fit.readiness === "unavailable" || !viewAllowed(view)) return;
    setLoading(true); setError(""); setStatus(`Loading ${COUNTY_FIT_VIEWS.find((item) => item.key === view)?.label}`);
  }, [active, meta.county_fit.readiness, retry, scoreUrl, view]);

  useEffect(() => {
    setDetail(null); setDetailError("");
    if (!active || !selected || !summary) return;
    const controller = new AbortController();
    setDetailLoading(true);
    void fetch(`/api/v3/county-fit/counties/${selected}?${params}`, { signal: controller.signal })
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "County detail failed");
        return body as CountyFitDetail;
      }).then((value) => { setDetail(value); setStatus(`${value.county.name} selected`); })
      .catch((caught: Error) => { if (caught.name !== "AbortError") setDetailError(caught.message); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [active, detailRetry, params, selected, summary]);

  const orderedRows = useMemo(() => summary ? countyFitRows(summary).sort((left, right) => {
    if (left.filteredRank !== null && right.filteredRank !== null) {
      return left.filteredRank - right.filteredRank || left.fips.localeCompare(right.fips);
    }
    if (left.filteredRank !== null) return -1;
    if (right.filteredRank !== null) return 1;
    return left.name.localeCompare(right.name) || left.fips.localeCompare(right.fips);
  }) : [], [summary]);

  const selectCounty = useCallback((fips: string, trigger?: HTMLElement) => {
    detailTrigger.current = trigger || document.activeElement as HTMLElement;
    setSelected(fips); setFocusTarget({ kind: "place", id: fips, nonce: Date.now() });
  }, []);

  const closeDetail = useCallback(() => {
    setSelected(""); setDetail(null);
    window.requestAnimationFrame(() => detailTrigger.current?.focus());
  }, []);

  const chooseView = (next: CountyFitView) => {
    if (!viewAllowed(next)) return;
    setView(next); setSelected(""); setDetail(null); setPreview(null); setPanel(null);
  };

  const choosePreset = (next: string) => {
    const exact = { ...COUNTY_FIT_PRESETS[next] };
    setPreset(next); setWeights(exact); setDraftWeights(exact);
  };

  const applyWeights = () => {
    if (!weightsValid) return;
    setPreset("custom"); setWeights({ ...draftWeights }); setPanel(null);
  };

  const applyFilters = () => {
    if (!filtersValid) return;
    setFilters({ ...draftFilters }); setSelected(""); setPanel(null);
  };
  const clearFilters = () => {
    const cleared = { ...EMPTY_COUNTY_FIT_FILTERS };
    setDraftFilters(cleared); setFilters(cleared); setSelected(""); setPanel(null);
  };
  const readiness = meta.county_fit;
  const historyCommands = readiness.local_history?.commands || HISTORY_COMMANDS;
  const activeViewLabel = COUNTY_FIT_VIEWS.find((item) => item.key === view)?.label || "County Fit";
  const tooltipClass = preview ? `map-tooltip${preview.x > window.innerWidth / 2 ? " tooltip-left" : ""}${preview.y > window.innerHeight / 2 ? " tooltip-up" : ""}` : "";

  if (readiness.readiness === "unavailable") return <main className="map-shell county-fit-shell">
    <header className="top-dock"><div className="brand"><strong>HouseHunter</strong><span>County Fit</span></div><div className="workspace-toggle" aria-label="Workspace"><button onClick={onMap}>Map</button><button aria-pressed="true">County Fit</button></div></header>
    <section className="asset-repair" role="alert"><p className="eyebrow">County Fit unavailable</p><h1>The ranking bundle is not ready</h1><p>{(readiness.reason_code || "unavailable").replaceAll("_", " ")}</p><p>The original five-layer map is still available.</p><button className="primary" onClick={onMap}>Return to Map</button></section>
  </main>;

  return <main className="map-shell county-fit-shell">
    <RiskMap
      active={active}
      manifestUrl={meta.map_assets.manifest_url}
      scoreUrl={scoreUrl}
      expectedBuildId={buildId}
      level="county"
      datasetKind="county-fit"
      metric="county-fit"
      selected={selected}
      filters={MAP_FILTERS}
      neutralOnly={Boolean(error)}
      retryGeneration={retry}
      focusTarget={focusTarget}
      cameraTarget={cameraTarget}
      initialCamera={initialCamera.current}
      onSelect={selectCounty}
      onPreview={setPreview}
      onCamera={(next) => { camera.current = next; updateHash("replace"); }}
      onStatus={setStatus}
      onCountyFitReady={(value) => {
        setSummary(value); setVisibleRows(100); setLoading(false); setError("");
        setStatus(`${value.cohort_count.toLocaleString("en-US")} counties in the filtered cohort`);
      }}
      onScoreError={(message) => {
        setError(message); setLoading(false); setStatus("County Fit could not be loaded");
      }}
    />
    <header className={`top-dock county-fit-dock${panel ? " overlay-open" : ""}`}>
      <div className="brand"><strong>HouseHunter</strong><span>County Fit · county-only</span></div>
      <div className="workspace-toggle" aria-label="Workspace"><button onClick={onMap}>Map</button><button aria-pressed="true">County Fit</button></div>
      <label className="fit-view-select">View<select value={view} onChange={(event) => chooseView(event.target.value as CountyFitView)}>{COUNTY_FIT_VIEWS.map((item) => <option key={item.key} value={item.key} disabled={!viewAllowed(item.key)}>{item.label}{!viewAllowed(item.key) ? " · unavailable" : ""}</option>)}</select></label>
      <div className="dock-actions"><button aria-expanded={panel === "filters"} onClick={() => setPanel((current) => current === "filters" ? null : "filters")}>Filters</button>{view === "custom" && <button aria-expanded={panel === "weights"} onClick={() => setPanel((current) => current === "weights" ? null : "weights")}>Weights · {Object.values(weights).reduce((a, b) => a + b, 0)}%</button>}<a className="dock-link" href={`/api/v3/exports/county-fit.csv?${params}`} download>Export CSV</a><button aria-expanded={panel === "readiness"} onClick={() => setPanel((current) => current === "readiness" ? null : "readiness")}>About</button></div>
      <div className={`build-pill ${error ? "failed" : ""}`}>{loading ? "Loading…" : error ? "Fit error" : `${summary?.cohort_count ?? 0} in cohort`}</div>
    </header>

    {panel === "filters" && <section className="floating-panel fit-filter-panel" aria-label="County Fit filters"><h2>County Fit filters</h2><p>Filters remove counties; they never recalibrate utilities or renormalize weights.</p><label>State<select value={draftFilters.state} onChange={(event) => setDraftFilters((current) => ({ ...current, state: event.target.value }))}><option value="">All states + DC</option>{STATE_ABBREVIATIONS.filter((state) => !["AS", "GU", "MP", "PR", "VI"].includes(state)).map((state) => <option key={state}>{state}</option>)}</select></label><div className="fit-filter-grid">
      {([
        ["min_population", "Minimum population (25,000 floor)", "1"], ["min_valid_months", "Minimum housing months", "1"], ["min_active_listings", "Minimum active listings", "1"],
        ["min_jan_temp_f", "Minimum January mean °F", "0.1"], ["max_jan_temp_f", "Maximum January mean °F", "0.1"], ["min_jul_temp_f", "Minimum July mean °F", "0.1"], ["max_jul_temp_f", "Maximum July mean °F", "0.1"],
        ["max_extreme_heat_days", "Maximum ≥90°F days", "0.1"], ["max_extreme_cold_days", "Maximum ≤32°F days", "0.1"],
      ] as const).map(([key, label, step]) => <label key={key}>{label}<input type="number" min={key === "min_population" ? COUNTY_FIT_POPULATION_FLOOR : undefined} step={step} value={draftFilters[key]} onChange={(event) => setDraftFilters((current) => ({ ...current, [key]: event.target.value }))} placeholder={key === "min_population" ? "25,000" : "Any"} /></label>)}
    </div><h3>Minimum pillar utility (%)</h3><div className="fit-filter-grid">{COUNTY_FIT_PILLARS.map((pillar) => <label key={pillar}>{PILLAR_LABELS[pillar]}<input type="number" min="0" max="100" step="1" value={draftFilters[`min_${pillar}`]} onChange={(event) => setDraftFilters((current) => ({ ...current, [`min_${pillar}`]: event.target.value }))} placeholder="Any" /></label>)}</div><label className="check"><input type="checkbox" checked={draftFilters.exclude_appalachia} onChange={(event) => setDraftFilters((current) => ({ ...current, exclude_appalachia: event.target.checked }))} />Exclude Appalachian Regional Commission counties</label>{!filtersValid && <p className="weight-total invalid" role="status">Population must be a whole number of at least 25,000; listing, month, and percentage filters must also be valid.</p>}<div className="panel-buttons"><button className="primary" disabled={!filtersValid} onClick={applyFilters}>Apply</button><button className="secondary" onClick={clearFilters}>Clear</button></div></section>}

    {panel === "weights" && <section className="floating-panel fit-weights-panel" aria-label="Custom Fit weights"><h2>Custom Fit weights</h2><div className="fit-presets">{Object.entries(PRESET_LABELS).map(([key, label]) => <button className="secondary" key={key} onClick={() => choosePreset(key)}>{label}</button>)}</div>{COUNTY_FIT_PILLARS.map((pillar) => <label className="weight-control" key={pillar}><span>{PILLAR_LABELS[pillar]}</span><input type="range" min="0" max="100" step="1" value={draftWeights[pillar]} onChange={(event) => { const value = Number(event.target.value); setDraftWeights((current) => ({ ...current, [pillar]: value })); }} /><input aria-label={`${PILLAR_LABELS[pillar]} weight percent`} type="number" min="0" max="100" step="1" value={draftWeights[pillar]} onChange={(event) => { const value = Math.max(0, Math.min(100, Number(event.target.value) || 0)); setDraftWeights((current) => ({ ...current, [pillar]: value })); }} /></label>)}<p className={`weight-total ${weightsValid ? "valid" : "invalid"}`} role="status">Total: {totalWeight}%</p>{!weightsValid && <p className="muted">{Object.values(draftWeights).every(Number.isInteger) ? "Weights must total 100%." : "Use whole percentages totaling 100%."}</p>}<div className="panel-buttons"><button className="primary" disabled={!weightsValid} onClick={applyWeights}>Apply weights</button><button className="secondary" onClick={() => { setDraftWeights({ ...COUNTY_FIT_PRESETS.balanced }); }}>Reset balanced</button></div></section>}

    {panel === "readiness" && <section className="floating-panel info-panel" aria-label="About County Fit"><h2>About County Fit</h2><p><strong>{readiness.readiness === "ready" ? "All seven views are ready." : "Five public-data views are ready."}</strong> County Fit is loaded only when this workspace opens and never requests agency data at runtime.</p>{readiness.readiness === "partial" && <div className="notice"><strong>Affordability and Custom Fit need approved history.</strong>{historyCommands.map((command) => <code key={command}>{command}</code>)}</div>}<p>Utilities are calibrated against the full source-valid national universe. Every view ranks only counties with population ≥25,000; higher population filters apply afterward. Equal values share competition ranks.</p><p>Custom Fit also requires complete six-pillar data, ≥90% crime coverage, ≥9 valid housing months, and median active listings ≥100.</p><p>Provider values are availability proxies. FCC broadband is the share of broadband-serviceable locations, not population. Public-water coverage is context only, excludes private wells, and is not countywide water quality; boundaries may be supplied or EPA-modeled. Climate is missing without a qualifying in-county station.</p><p className="notice">{FIT_DISCLAIMER}</p><p>Ranking state RPP is an official state all-items value for non-MSA counties. It is distinct from the existing map’s nonmetropolitan <code>00999</code> assignment.</p></section>}

    {readiness.readiness === "partial" && <aside className="fit-readiness-banner"><strong>Partial readiness</strong><span>Affordability and Custom Fit need ≥9 approved Realtor.com history months.</span><button onClick={() => setPanel("readiness")}>Show commands</button></aside>}

    <aside className="county-fit-ranking" aria-label="County Fit ranked counties" aria-busy={loading}>
      <div className="fit-ranking-heading"><div><p className="eyebrow">{activeViewLabel}</p><h2>Ranked counties</h2></div><span>{summary?.cohort_count.toLocaleString("en-US") ?? "—"} included</span></div>
      <p className="fit-view-methodology">{VIEW_DESCRIPTIONS[view]}</p>
      {view === "family" && <p className="notice fit-family-disclaimer" role="note" aria-label="Homeschool Policy Fit limitation">{FIT_DISCLAIMER}</p>}
      {error && <div className="error" role="alert"><p>{error}</p><button className="secondary" onClick={() => setRetry((value) => value + 1)}>Retry County Fit</button></div>}
      {!error && <ol className="fit-ranking-list">{orderedRows.slice(0, visibleRows).map((row) => { const populationExcluded = row.exclusionReason === "population" && row.activeValue === null && row.nationalRank === null; return <li key={row.fips} className={!row.eligible ? "excluded" : row.fips === selected ? "selected" : ""}><button onClick={(event) => selectCounty(row.fips, event.currentTarget)}><span className="fit-rank">{row.filteredRank ?? "—"}</span><span className="fit-county"><strong>{row.name}</strong><small>{row.state} · {row.fips}{row.paretoOptimal ? " · Pareto" : ""}</small>{!row.eligible && <em>{exclusionLabel(row.exclusionReason, populationExcluded)}</em>}</span><span className="fit-score">{populationExcluded ? "Not ranked" : displayPercent(row.activeValue)}<small>{populationExcluded ? "25,000 floor" : `Nat. ${row.nationalRank ?? "—"}`}</small></span></button></li>; })}</ol>}
      {!error && visibleRows < orderedRows.length && <button className="secondary load-more" onClick={() => setVisibleRows((value) => value + 100)}>Load 100 more</button>}
    </aside>

    <div className="legend county-fit-legend"><div className="legend-keys"><div className="continuous-key"><i className="legend-gradient" style={{ background: COUNTY_FIT_GRADIENT }} /><div className="legend-ticks"><span style={{ left: "0%" }}>0</span><span style={{ left: "25%" }}>25</span><span style={{ left: "50%" }}>50</span><span style={{ left: "75%" }}>75</span><span style={{ left: "100%" }}>100</span></div></div><span className="missing-key"><i className="hatched" />Excluded / unavailable</span></div><p><strong>{activeViewLabel}</strong> · higher is better · fixed national calibration · cohort {summary?.cohort_count ?? 0}</p></div>
    {preview && <div className={tooltipClass} style={{ left: preview.x, top: preview.y }}><strong>{preview.name}</strong><span>{preview.state} · {preview.placeId}</span><b>{preview.countyFit?.eligible ? displayPercent(preview.countyFit.activeValue) : exclusionLabel(preview.countyFit?.exclusionReason, preview.countyFit?.activeValue === null && preview.countyFit?.nationalRank === null)}</b></div>}
    {selected && <DetailDrawer detail={detail} loading={detailLoading} error={detailError} onClose={closeDetail} onRetry={() => setDetailRetry((value) => value + 1)} />}
    <p className="sr-only" aria-live="polite">{status}</p>
  </main>;
}
