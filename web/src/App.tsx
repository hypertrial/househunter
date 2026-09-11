import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import RiskMap, { type FocusTarget, type MapPreview } from "./RiskMap";
import { MAP_COLORS, readHash, scoreBand, STATE_ABBREVIATIONS, STATE_FIPS, type CameraState, type ScoreBand } from "./map";
import type { AddressConfirmation, AddressLookup, Geography, HazardPercentile, JobStatus, LookupResult, MapScore, MapScores, Meta, PlaceDetail, PlaceSummary } from "./types";

export { scoreBand } from "./map";
export type { ScoreBand } from "./map";

const number = new Intl.NumberFormat("en-US");
const initial = readHash(window.location.hash);
const EMPTY_MAP_ROWS: MapScore[] = [];

export { STATE_ABBREVIATIONS } from "./map";

export const SCORE_BANDS = [
  { id: "low", label: "0–20" }, { id: "below", label: "20–40" },
  { id: "typical", label: "40–60" }, { id: "high", label: "60–80" },
  { id: "highest", label: "80–100" },
] as const satisfies ReadonlyArray<{ id: ScoreBand; label: string }>;

export const SCORE_BAND_LABELS: Record<ScoreBand, string> = {
  low: "low among peers", below: "below typical", typical: "typical",
  high: "high among peers", highest: "highest among peers",
};

export function mapFocusTarget(place: string, state: string): Omit<FocusTarget, "nonce"> | null {
  return place ? { kind: "place", id: place } : state ? { kind: "state", id: state } : null;
}

export function mapTooltipClass(x: number, y: number, width: number, height: number): string {
  return `map-tooltip${x > width / 2 ? " tooltip-left" : ""}${y > height / 2 ? " tooltip-up" : ""}`;
}

async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Request failed");
  return body as T;
}

export function scoreLabel(place: PlaceSummary): string {
  return place.risk_score === null ? "Not ranked" : place.risk_score.toFixed(1);
}

export function scoreToneClass(value: number | null): string {
  const band = scoreBand(value);
  return band ? `score-${band}` : "missing";
}

export function scorePillLabel(place: PlaceSummary): string | undefined {
  const band = scoreBand(place.risk_score);
  return band ? `${scoreLabel(place)}, ${SCORE_BAND_LABELS[band]}` : undefined;
}

export function sortedHazardPercentiles(hazards: HazardPercentile[]): HazardPercentile[] {
  return [...hazards].sort((left, right) => {
    if (left.percentile === null && right.percentile === null) return left.label.localeCompare(right.label);
    if (left.percentile === null) return 1;
    if (right.percentile === null) return -1;
    return right.percentile - left.percentile || left.label.localeCompare(right.label);
  });
}

function Setup({ token, onReady }: { token: string; onReady: () => void }) {
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const latest = await json<JobStatus>(`/api/v1/jobs/${job.job_id}`);
        setJob(latest);
        if (latest.state === "succeeded") onReady();
        if (latest.state === "failed") setError(latest.error || "Preparation failed");
      } catch (caught) { setError((caught as Error).message); }
    }, 600);
    return () => window.clearInterval(timer);
  }, [job, onReady]);
  async function prepare() {
    setError("");
    try {
      setJob(await json<JobStatus>("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-HouseHunter-Token": token },
        body: JSON.stringify({ kind: "prepare" }),
      }));
    } catch (caught) { setError((caught as Error).message); }
  }
  async function cancel() {
    if (!job) return;
    try {
      setJob(await json<JobStatus>(`/api/v1/jobs/${job.job_id}`, {
        method: "DELETE", headers: { "X-HouseHunter-Token": token },
      }));
    } catch (caught) { setError((caught as Error).message); }
  }
  return <section className="setup-card" aria-labelledby="prepare-title">
    <p className="eyebrow">Private local snapshot</p><h1 id="prepare-title">Prepare the national risk map</h1>
    <p>HouseHunter downloads pinned FEMA tract and county attributes and builds a read-only snapshot on this Mac.</p>
    {job && <div className="progress-card" aria-live="polite"><div><strong>{job.message}</strong><span>{job.progress}%</span></div><progress max="100" value={job.progress}>{job.progress}%</progress>{["queued", "running"].includes(job.state) && <button className="secondary" onClick={cancel}>Cancel</button>}</div>}
    {error && <p role="alert" className="error">{error}</p>}
    {(!job || ["failed", "cancelled"].includes(job.state)) && <button className="primary" onClick={prepare}>{job ? "Try preparation again" : "Prepare national data"}</button>}
    <p className="fine">FEMA NRI December 2025 v1.20 · no Census download required</p>
  </section>;
}

function DetailDrawer({ detail, loading, error, level, onClose, onRetry, onViewTracts, onViewCounty }: {
  detail: PlaceDetail | null; loading: boolean; error: string; level: Geography;
  onClose: () => void; onRetry: () => void;
  onViewTracts: (countyFips: string, state: string) => void; onViewCounty: (countyFips: string) => void;
}) {
  return <aside className="detail-drawer" role="dialog" aria-label={`${level === "tract" ? "Tract" : "County"} detail`} aria-modal="false">
    <button autoFocus className="close" onClick={onClose} aria-label={`Close ${level} detail`}>×</button>
    {loading && <p className="loading-copy">Loading detail…</p>}
    {error && <div className="error" role="alert"><p>{error}</p><button className="secondary" onClick={onRetry}>Retry detail</button></div>}
    {detail && <><p className="eyebrow">{detail.summary.place_type} · {detail.summary.place_id}</p>
      <h2>{detail.summary.name}, {detail.summary.state}</h2>
      <div className={`score ${scoreToneClass(detail.summary.risk_score)}`}><span>{scoreLabel(detail.summary)}</span><small>FEMA {level === "county" ? "county " : ""}ALR_NPCTL<br />Lower is better</small></div>
      <dl className="facts"><div><dt>{level === "county" ? "County" : "Tract"} FIPS</dt><dd>{detail.summary.place_id}</dd></div><div><dt>State</dt><dd>{detail.summary.state}</dd></div>{level === "tract" && <div><dt>County</dt><dd>{detail.summary.county_name}</dd></div>}<div><dt>FEMA vintage</dt><dd>{detail.summary.fema_vintage}</dd></div></dl>
      {level === "county" && <button className="secondary" onClick={() => onViewTracts(detail.summary.place_id, detail.summary.state)}>View {number.format(detail.member_tract_count ?? 0)} tracts</button>}
      {level === "tract" && /^\d{5}$/.test(detail.summary.county_fips) && <button className="secondary" onClick={() => onViewCounty(detail.summary.county_fips)}>View {detail.summary.county_name} county</button>}
      <h3>Published hazard percentiles</h3><div className="contributions">{sortedHazardPercentiles(detail.hazard_percentiles).map((hazard) => <div key={hazard.code} className="contribution"><div><span>{hazard.label}</span><span>{hazard.percentile === null ? "No rating" : hazard.percentile.toFixed(1)}</span></div><div className={`bar ${scoreToneClass(hazard.percentile)}`}><i style={{ width: hazard.percentile === null ? "0%" : `${hazard.percentile}%` }} /></div></div>)}</div>
      <p className="notice">{detail.methodology_notice}</p></>}
  </aside>;
}

type Overlay = "filters" | "extremes" | "exports" | "info" | "more" | "search" | null;

function ResultGroup({ title, items, onChoose }: { title: string; items: PlaceSummary[]; onChoose: (item: PlaceSummary) => void }) {
  return <section><h3>{title}</h3>{items.length
    ? <ol className="result-list">{items.map((item) => <li key={item.place_id}><button onClick={() => onChoose(item)}><span><strong>{item.name}</strong><small>{item.state} · {item.place_id}</small></span><b>{scoreLabel(item)}</b></button></li>)}</ol>
    : <p className="empty-copy">No ranked geographies</p>}</section>;
}

function Workspace({ meta }: { meta: Meta }) {
  const builtState = meta.build?.scope.kind === "state" ? meta.build.scope.state || "" : "";
  const initialState = builtState || initial.state;
  const initialPlace = !builtState || initial.place.startsWith(STATE_FIPS[builtState as keyof typeof STATE_FIPS] || "-")
    ? initial.place : "";
  const [level, setLevel] = useState<Geography>(initial.level);
  const [state, setState] = useState(initialState);
  const [county, setCounty] = useState(builtState && initial.state !== builtState ? "" : initial.county);
  const [showUnranked, setShowUnranked] = useState(initial.unranked);
  const [selected, setSelected] = useState(initialPlace);
  const [scores, setScores] = useState<MapScores | null>(null);
  const [scoreError, setScoreError] = useState("");
  const [status, setStatus] = useState("Loading map");
  const [overlay, setOverlay] = useState<Overlay>(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [confirmation, setConfirmation] = useState<AddressConfirmation | null>(null);
  const [preview, setPreview] = useState<MapPreview | null>(null);
  const [focusTarget, setFocusTarget] = useState<FocusTarget | null>(() => {
    const target = mapFocusTarget(initialPlace, initialState);
    return target ? { ...target, nonce: Date.now() } : null;
  });
  const [detail, setDetail] = useState<PlaceDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [countyOptions, setCountyOptions] = useState<PlaceSummary[]>([]);
  const [draftState, setDraftState] = useState(state);
  const [draftCounty, setDraftCounty] = useState(county);
  const [draftUnranked, setDraftUnranked] = useState(showUnranked);
  const [lowest, setLowest] = useState<PlaceSummary[]>([]);
  const [highest, setHighest] = useState<PlaceSummary[]>([]);
  const [extremesLoading, setExtremesLoading] = useState(false);
  const camera = useRef<CameraState>(initial.camera);
  const [cameraTarget, setCameraTarget] = useState<(CameraState & { nonce: number }) | undefined>();
  const firstSemantic = useRef(true);
  const scopeHashNormalized = useRef(false);
  const restoringHistory = useRef(false);
  const overlayTrigger = useRef<HTMLElement | null>(null);
  const restoreOverlayFocus = useRef(true);
  const previousOverlay = useRef<Overlay>(null);
  const detailTrigger = useRef<HTMLElement | null>(null);
  const extremesGeneration = useRef(0);

  function toggleOverlay(next: Exclude<Overlay, null>, trigger: HTMLElement) {
    overlayTrigger.current = trigger;
    restoreOverlayFocus.current = true;
    setOverlay((current) => current === next ? null : next);
  }

  useEffect(() => {
    if (previousOverlay.current && !overlay && restoreOverlayFocus.current) {
      window.requestAnimationFrame(() => overlayTrigger.current?.focus());
    }
    previousOverlay.current = overlay;
    restoreOverlayFocus.current = true;
  }, [overlay]);

  const closeDetail = useCallback(() => {
    setSelected("");
    window.requestAnimationFrame(() => {
      (detailTrigger.current || document.querySelector<HTMLCanvasElement>(".risk-canvas"))?.focus();
    });
  }, []);

  const writeHash = useCallback((mode: "push" | "replace") => {
    const params = new URLSearchParams({ level });
    if (state) params.set("state", state);
    if (level === "tract" && county) params.set("county", county);
    if (selected) params.set("place", selected);
    if (showUnranked) params.set("unranked", "1");
    params.set("cx", camera.current.cx.toFixed(4)); params.set("cy", camera.current.cy.toFixed(4)); params.set("z", camera.current.z.toFixed(3));
    window.history[mode === "push" ? "pushState" : "replaceState"](null, "", `#${params}`);
  }, [county, level, selected, showUnranked, state]);
  useEffect(() => {
    if (firstSemantic.current) firstSemantic.current = false;
    else if (restoringHistory.current) restoringHistory.current = false;
    else writeHash("push");
  }, [writeHash]);
  useEffect(() => {
    if (!builtState || scopeHashNormalized.current) return;
    scopeHashNormalized.current = true;
    writeHash("replace");
  }, [builtState, writeHash]);
  const cameraChanged = useCallback((next: CameraState) => { camera.current = next; writeHash("replace"); }, [writeHash]);
  const selectFromMap = useCallback((placeId: string) => {
    detailTrigger.current = document.activeElement as HTMLElement;
    setSelected(placeId);
    setFocusTarget({ kind: "place", id: placeId, nonce: Date.now() });
  }, []);

  useEffect(() => {
    const restore = () => {
      const parsed = readHash(window.location.hash);
      const nextState = builtState || parsed.state;
      const nextPlace = !builtState || parsed.place.startsWith(STATE_FIPS[builtState as keyof typeof STATE_FIPS] || "-")
        ? parsed.place : "";
      restoringHistory.current = true;
      setLevel(parsed.level); setState(nextState); setDraftState(nextState);
      setCounty(builtState && parsed.state !== builtState ? "" : parsed.county);
      setDraftCounty(builtState && parsed.state !== builtState ? "" : parsed.county);
      setSelected(nextPlace);
      setShowUnranked(parsed.unranked); setDraftUnranked(parsed.unranked);
      camera.current = parsed.camera;
      setCameraTarget({ ...parsed.camera, nonce: Date.now() });
      setOverlay(null); setPreview(null);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [builtState]);

  const loadScores = useCallback(async () => {
    if (!meta.build) return;
    setScoreError(""); setStatus(`Loading ${level} scores`);
    try {
      const payload = await json<MapScores>(`/api/v1/map/scores?level=${level}`);
      if (payload.schema_version !== 1 || payload.build_id !== meta.build.build_id) throw new Error("Map scores do not match the current build");
      setScores(payload); setStatus(`${number.format(payload.rows.length)} ${level}s ready`);
    } catch (caught) { setScoreError((caught as Error).message); }
  }, [level, meta.build]);
  useEffect(() => { void loadScores(); }, [loadScores]);

  useEffect(() => {
    setDetail(null); setDetailError("");
    if (!selected) return;
    let cancelled = false; setDetailLoading(true);
    const path = level === "county" ? `/api/v1/counties/${selected}` : `/api/v1/places/${selected}`;
    json<PlaceDetail>(path).then((value) => { if (!cancelled) { setDetail(value); setStatus(`${value.summary.name} selected`); } }).catch((caught) => { if (!cancelled) setDetailError((caught as Error).message); }).finally(() => { if (!cancelled) setDetailLoading(false); });
    return () => { cancelled = true; };
  }, [level, selected]);

  useEffect(() => {
    setCountyOptions([]);
    if (!draftState) return;
    const params = new URLSearchParams({ state: draftState, limit: "500", sort: "name", direction: "asc", include_unranked: "true" });
    void json<{ items: PlaceSummary[] }>(`/api/v1/counties?${params}`).then((value) => setCountyOptions(value.items)).catch(() => setCountyOptions([]));
  }, [draftState]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { if (overlay) setOverlay(null); else if (selected) closeDetail(); } };
    window.addEventListener("keydown", escape); return () => window.removeEventListener("keydown", escape);
  }, [closeDetail, overlay, selected]);

  async function openExtremes(trigger: HTMLElement) {
    if (overlay === "extremes") { extremesGeneration.current += 1; setExtremesLoading(false); setOverlay(null); return; }
    const generation = ++extremesGeneration.current;
    toggleOverlay("extremes", trigger); setSearchError(""); setLowest([]); setHighest([]); setExtremesLoading(true);
    const base = new URLSearchParams({ limit: "5", sort: "risk_score" });
    if (state) base.set("state", state); if (level === "tract" && county) base.set("county", county);
    const path = level === "tract" ? "/api/v1/places" : "/api/v1/counties";
    try {
      const [low, high] = await Promise.all([json<{ items: PlaceSummary[] }>(`${path}?${base}&direction=asc`), json<{ items: PlaceSummary[] }>(`${path}?${base}&direction=desc`)]);
      if (generation === extremesGeneration.current) { setLowest(low.items); setHighest(high.items); }
    } catch (caught) {
      if (generation === extremesGeneration.current) setSearchError((caught as Error).message);
    } finally {
      if (generation === extremesGeneration.current) setExtremesLoading(false);
    }
  }
  function choose(place: PlaceSummary) { detailTrigger.current = overlayTrigger.current; restoreOverlayFocus.current = false; setSelected(place.place_id); setFocusTarget({ kind: "place", id: place.place_id, nonce: Date.now() }); setOverlay(null); setQuery(""); }
  function switchLevel(next: Geography) { setLevel(next); setCounty(""); setDraftCounty(""); setSelected(""); setDetail(null); setFocusTarget(null); setOverlay(null); setPreview(null); }

  async function lookupAddress(event: FormEvent) {
    event.preventDefault(); setSearching(true); setSearchError(""); setConfirmation(null);
    try {
      const result = await json<LookupResult>("/api/v1/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address: query }) });
      if (result.status === "confirmation_required") setConfirmation(result); else navigateToLookup(result);
    } catch (caught) { setSearchError((caught as Error).message); } finally { setSearching(false); }
  }
  async function confirmAddress(candidateId: string) {
    setSearching(true);
    try {
      const result = await json<LookupResult>("/api/v1/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address: query, candidate_id: candidateId }) });
      if (result.status === "resolved") navigateToLookup(result);
    } catch (caught) { setSearchError((caught as Error).message); } finally { setSearching(false); }
  }
  function navigateToLookup(result: AddressLookup) {
    const summary = result.detail.summary;
    detailTrigger.current = overlayTrigger.current;
    restoreOverlayFocus.current = false;
    setLevel("tract"); setState(summary.state); setDraftState(summary.state);
    setCounty(summary.county_fips); setDraftCounty(summary.county_fips);
    setDetail(result.detail); setSelected(result.tract_id);
    setFocusTarget({ kind: "place", id: result.tract_id, nonce: Date.now() });
    setConfirmation(null); setOverlay(null); setQuery("");
    setStatus(`Matched ${summary.name}; showing tract risk only`);
  }
  function applyFilters() {
    const nextState = builtState || draftState;
    setState(nextState); setCounty(level === "tract" ? draftCounty : ""); setShowUnranked(draftUnranked); setSelected(""); setOverlay(null);
    if (draftCounty) setFocusTarget({ kind: "county", id: draftCounty, nonce: Date.now() }); else if (nextState) setFocusTarget({ kind: "state", id: nextState, nonce: Date.now() }); else setFocusTarget(null);
    setStatus(nextState ? `Filter applied: ${nextState}${draftCounty ? ` · ${draftCounty}` : ""}` : "National filter applied");
  }
  function clearFilters() { setDraftState(builtState); setDraftCounty(""); setDraftUnranked(false); setState(builtState); setCounty(""); setShowUnranked(false); setFocusTarget(builtState ? { kind: "state", id: builtState, nonce: Date.now() } : null); setSelected(""); setOverlay(null); }

  const mapRows = scores?.level === level ? scores.rows : EMPTY_MAP_ROWS;
  const tooltipBand = preview?.score ? scoreBand(preview.score.risk_score) : null;
  const tooltipClass = preview ? mapTooltipClass(preview.x, preview.y, window.innerWidth, window.innerHeight) : "";
  const activeFilter = state || county;
  return <main className="map-shell">
    <RiskMap manifestUrl={meta.map_assets.manifest_url} level={level} rows={mapRows} selected={selected} state={state} county={county} showUnranked={showUnranked} neutralOnly={Boolean(scoreError)} focusTarget={focusTarget} cameraTarget={cameraTarget} initialCamera={initial.camera} onSelect={selectFromMap} onPreview={setPreview} onCamera={cameraChanged} onStatus={setStatus} />
    <header className={`top-dock${overlay ? " overlay-open" : ""}`}><div className="brand"><strong>HouseHunter</strong><span>FEMA ALR_NPCTL</span></div><div className="level-toggle" aria-label="Geography level"><button aria-pressed={level === "tract"} onClick={() => switchLevel("tract")}>Tracts</button><button aria-pressed={level === "county"} onClick={() => switchLevel("county")}>Counties</button></div><div className="dock-actions"><button aria-expanded={overlay === "search"} aria-controls="search-panel" onClick={(event) => toggleOverlay("search", event.currentTarget)}>Search</button><button aria-expanded={overlay === "extremes"} aria-controls="extremes-panel" onClick={(event) => void openExtremes(event.currentTarget)}>Lowest / Highest</button><button aria-expanded={overlay === "filters"} aria-controls="filters-panel" className={activeFilter ? "active" : ""} onClick={(event) => toggleOverlay("filters", event.currentTarget)}>Filters{activeFilter ? " · On" : ""}</button><button className="desktop-dock-action" aria-expanded={overlay === "exports"} aria-controls="exports-panel" onClick={(event) => toggleOverlay("exports", event.currentTarget)}>Exports</button><button className="desktop-dock-action" aria-expanded={overlay === "info"} aria-controls="info-panel" aria-label="Information" onClick={(event) => toggleOverlay("info", event.currentTarget)}>ⓘ</button><button className={`mobile-more${overlay === "exports" || overlay === "info" ? " active" : ""}`} aria-expanded={overlay === "more"} aria-controls="more-panel" onClick={(event) => toggleOverlay("more", event.currentTarget)}>More</button></div><div className={`build-pill ${scoreError ? "failed" : ""}`} title={meta.build?.build_id}>{scoreError ? "Score error" : status}</div></header>
    {overlay === "search" && <section id="search-panel" className="floating-panel search-panel" aria-label="Search"><form onSubmit={lookupAddress} aria-busy={searching}><label>Street address<input autoFocus autoComplete="street-address" autoCapitalize="words" enterKeyHint="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Street, city, state, ZIP" /></label><p className="privacy-note">Find tract contacts the US Census geocoder through this loopback server and may send it to OpenStreetMap only after a valid Census no-match. Addresses are not written to disk. The map shows tract risk, never a property marker or score.</p><button className="primary" disabled={searching || !query.trim()}>{searching ? "Looking…" : "Find tract"}</button>{confirmation && <div className="confirm-card" role="region" aria-label="Approximate street match"><strong>Confirm approximate street match</strong><p>{confirmation.message}</p>{confirmation.candidates.map((candidate) => <button type="button" className="secondary" key={candidate.candidate_id} onClick={() => void confirmAddress(candidate.candidate_id)}>Use approximate street location: {candidate.matched_address}</button>)}<small>{confirmation.attribution}</small></div>}</form>{searchError && <p role="alert" className="error">{searchError}</p>}</section>}
    {overlay === "filters" && <section id="filters-panel" className="floating-panel filters-panel" aria-label="Map filters"><h2>Filters</h2><label>State<select value={draftState} disabled={Boolean(builtState)} onChange={(event) => { setDraftState(event.target.value); setDraftCounty(""); }}><option value="">All states & territories</option>{STATE_ABBREVIATIONS.map((item) => <option key={item}>{item}</option>)}</select></label>{level === "tract" && <label>County<select value={draftCounty} disabled={!draftState} onChange={(event) => setDraftCounty(event.target.value)}><option value="">All counties</option>{countyOptions.map((item) => <option value={item.place_id} key={item.place_id}>{item.name}</option>)}</select></label>}<label className="check"><input type="checkbox" checked={draftUnranked} onChange={(event) => setDraftUnranked(event.target.checked)} />Show FEMA-unranked geographies</label><div className="panel-buttons"><button className="primary" onClick={applyFilters}>Apply</button><button className="secondary" onClick={clearFilters}>Clear</button></div></section>}
    {overlay === "extremes" && <section id="extremes-panel" className="floating-panel extremes-panel" aria-label="Lowest and highest risk" aria-busy={extremesLoading}><h2>Explore the range</h2>{extremesLoading ? <p className="loading-copy" role="status">Loading lowest and highest…</p> : <div><ResultGroup title="Lowest" items={lowest} onChoose={choose} /><ResultGroup title="Highest" items={highest} onChoose={choose} /></div>}{searchError && <p role="alert" className="error">{searchError}</p>}</section>}
    {overlay === "more" && <section id="more-panel" className="floating-panel more-panel" aria-label="More actions"><h2>More</h2><button className="secondary" onClick={() => setOverlay("exports")}>Export snapshot</button><button className="secondary" onClick={() => setOverlay("info")}>About this map</button></section>}
    {overlay === "exports" && <nav id="exports-panel" className="floating-panel export-panel" aria-label="Exports"><h2>Export snapshot</h2><a href="/api/v1/exports/places.csv" download>Tracts CSV</a><a href="/api/v1/exports/places.parquet" download>Tracts Parquet</a><a href="/api/v1/exports/counties.csv" download>Counties CSV</a><a href="/api/v1/exports/counties.parquet" download>Counties Parquet</a></nav>}
    {overlay === "info" && <section id="info-panel" className="floating-panel info-panel" aria-label="About this map"><h2>About this map</h2><p>Every geography is drawn from the pinned FEMA National Risk Index December 2025 release. Dense tracts become distinguishable as you zoom; none are aggregated or enlarged.</p><p>Tracts and counties use separate FEMA percentile universes. County values are never tract averages. Hazard percentiles appear only in details.</p><p>HouseHunter is local-only and uses no basemap, telemetry, account, or hosted database.</p></section>}
    {preview && <div className={tooltipClass} style={{ left: preview.x, top: preview.y }}><strong>{preview.name}</strong><span>{preview.state} · {preview.placeId}</span><b>{preview.score?.risk_score === null || !preview.score ? "Not ranked / unavailable" : `${preview.score.risk_score.toFixed(1)} · ${tooltipBand ? SCORE_BAND_LABELS[tooltipBand] : ""}`}</b></div>}
    {scoreError && <section className="recovery-card" role="alert"><strong>Scores could not be loaded</strong><span>{scoreError}</span><button className="secondary" onClick={() => void loadScores()}>Retry scores</button></section>}
    <div className="legend" aria-label="Score color scale, lower is better"><div>{SCORE_BANDS.map((band) => <span key={band.id}><i style={{ background: MAP_COLORS[band.id] }} />{band.label}</span>)}<span><i className="hatched" />Unranked</span></div><p><strong>FEMA ALR_NPCTL</strong> · lower is better · national percentile · not property-level risk</p></div>
    {selected && <DetailDrawer detail={detail} loading={detailLoading} error={detailError} level={level} onClose={closeDetail} onRetry={() => { const value = selected; setSelected(""); window.setTimeout(() => setSelected(value)); }} onViewTracts={(countyFips, nextState) => { setLevel("tract"); setState(nextState); setDraftState(nextState); setCounty(countyFips); setDraftCounty(countyFips); setSelected(""); setFocusTarget({ kind: "county", id: countyFips, nonce: Date.now() }); }} onViewCounty={(countyFips) => { setLevel("county"); setCounty(""); setDraftCounty(""); setSelected(countyFips); setFocusTarget({ kind: "place", id: countyFips, nonce: Date.now() }); }} />}
    <p className="sr-only" aria-live="polite">{status}</p>
  </main>;
}

function App() {
  const [meta, setMeta] = useState<Meta | null>(null); const [error, setError] = useState("");
  const load = useCallback(() => { setError(""); void json<Meta>("/api/v1/meta").then(setMeta).catch((caught) => setError((caught as Error).message)); }, []);
  useEffect(load, [load]);
  if (!meta) return <main className="map-shell boot"><div className="boot-copy" role={error ? "alert" : "status"}>{error || "Opening HouseHunter map…"}{error && <button onClick={load}>Retry</button>}</div></main>;
  const mapAssets = meta.map_assets || { ready: false, error: "Map asset status is missing", schema_version: null, release: null, manifest_url: "/map-assets/manifest.json" };
  if (!mapAssets.ready) return <main className="map-shell"><RiskMap manifestUrl={mapAssets.manifest_url} level="tract" rows={[]} selected="" state="" county="" showUnranked={false} neutralOnly focusTarget={null} initialCamera={initial.camera} onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined} /><div className="asset-repair" role="alert"><h1>Map boundaries need repair</h1><p>{mapAssets.error}</p><p>Run <code>uv run python scripts/generate_map_assets.py</code> from the HouseHunter checkout, then restart the app.</p></div></main>;
  const readyMeta = { ...meta, map_assets: mapAssets };
  if (!meta.build) return <main className="map-shell"><RiskMap manifestUrl={mapAssets.manifest_url} level="tract" rows={[]} selected="" state="" county="" showUnranked={false} neutralOnly focusTarget={null} initialCamera={initial.camera} onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined} /><header className="top-dock"><div className="brand"><strong>HouseHunter</strong><span>FEMA ALR_NPCTL</span></div><div className="build-pill">Setup required</div></header><Setup token={meta.mutation_token} onReady={load} /><div className="legend"><p><strong>FEMA ALR_NPCTL</strong> · lower is better · not property-level risk</p></div></main>;
  return <Workspace meta={readyMeta} />;
}

export default App;
