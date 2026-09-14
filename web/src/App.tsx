import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import RiskMap, { type FocusTarget, type MapPreview } from "./RiskMap";
import { metricColorScale, readHash, scoreBand, scoreColor, STATE_ABBREVIATIONS, STATE_FIPS, type CameraState, type ScoreBand } from "./map";
import type { AddressConfirmation, AddressLookup, Geography, HazardPercentile, JobStatus, LookupResult, Meta, Metric, PlaceDetail, PlaceSummary } from "./types";

export { scoreBand } from "./map";
export type { ScoreBand } from "./map";

const number = new Intl.NumberFormat("en-US");
const initial = readHash(window.location.hash);
const COMMUNITY_EXPLANATION = "National CHR&R Community Conditions Health Group. Group 1 represents the healthiest community conditions and Group 10 the least healthy. Groups are data-driven clusters, not percentiles.";
const geographyPlural = (level: Geography) => level === "tract" ? "tracts" : "counties";
const mountainExplanation = (level: Geography) => `Mountain Magnitude ranks resident-weighted mountain terrain and access exposure among U.S. ${geographyPlural(level)}. Each +1 means ten times fewer ${geographyPlural(level)} have an equal-or-higher base exposure; it does not mean ten times more mountainous terrain. Tract and county magnitudes use separate peer groups and are not comparable. It does not measure property-specific views, trail quality, drive time, or guaranteed access.`;

export { STATE_ABBREVIATIONS } from "./map";

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

export function communityLabel(place: PlaceSummary): string {
  return place.community_conditions_group === null
    ? "Not grouped"
    : `Group ${place.community_conditions_group} of 10`;
}

export function mountainLabel(place: PlaceSummary): string {
  return place.mountain_magnitude === null ? "Unavailable" : `M${place.mountain_magnitude.toFixed(2)}`;
}

function percentileLabel(value: number | null): string {
  return value === null ? "percentile unavailable" : `${value.toFixed(1)} pct`;
}

function metricLabel(place: PlaceSummary, metric: Metric): string {
  return metric === "fema" ? scoreLabel(place) : metric === "mountain" ? mountainLabel(place) : communityLabel(place);
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
        const latest = await json<JobStatus>(`/api/v2/jobs/${job.job_id}`);
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
      setJob(await json<JobStatus>("/api/v2/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-HouseHunter-Token": token },
        body: JSON.stringify({ kind: "prepare" }),
      }));
    } catch (caught) { setError((caught as Error).message); }
  }
  async function cancel() {
    if (!job) return;
    try {
      setJob(await json<JobStatus>(`/api/v2/jobs/${job.job_id}`, {
        method: "DELETE", headers: { "X-HouseHunter-Token": token },
      }));
    } catch (caught) { setError((caught as Error).message); }
  }
  return <section className="setup-card" aria-labelledby="prepare-title">
    <p className="eyebrow">Private local snapshot</p><h1 id="prepare-title">Prepare the national risk map</h1>
    <p>HouseHunter downloads pinned FEMA and CHR&R county attributes and builds a read-only snapshot on this Mac.</p>
    {job && <div className="progress-card" aria-live="polite"><div><strong>{job.message}</strong><span>{job.progress}%</span></div><progress max="100" value={job.progress}>{job.progress}%</progress>{["queued", "running"].includes(job.state) && <button className="secondary" onClick={cancel}>Cancel</button>}</div>}
    {error && <p role="alert" className="error">{error}</p>}
    {(!job || ["failed", "cancelled"].includes(job.state)) && <button className="primary" onClick={prepare}>{job ? "Try preparation again" : "Prepare national data"}</button>}
    <p className="fine">FEMA NRI December 2025 v1.20 · CHR&R 2025 · no Census download required</p>
  </section>;
}

function MetricLegend({ metric, level }: { metric: Metric; level: Geography }) {
  const { minimum, maximum, ticks, gradient } = metricColorScale(metric, level);
  const peers = geographyPlural(level);
  const missing = metric === "fema" ? "Unranked" : metric === "mountain" ? "Unavailable" : "Not grouped";
  const label = metric === "fema"
    ? "Continuous score color scale, lower is better"
    : metric === "mountain"
      ? `Continuous Mountain Magnitude color scale for U.S. ${peers}, higher means fewer equal-or-higher peers`
      : "Continuous Community Conditions color scale, Group 1 is healthiest";
  const rampLabel = metric === "fema"
    ? "FEMA Risk continuous color ramp from 0 to 100"
    : metric === "mountain"
      ? `Mountain Magnitude continuous color ramp from M0 to M${maximum} for U.S. ${peers}`
      : "Community Conditions color ramp from Group 1 to Group 10";
  return <div className="legend" role="group" aria-label={label}>
    <div className="legend-keys">
      <div className="continuous-key">
        <i className="legend-gradient" style={{ backgroundImage: gradient }} role="img" aria-label={rampLabel} />
        <div className="legend-ticks">{ticks.map((tick) => <span
          key={tick}
          style={{ left: `${(tick - minimum) * 100 / (maximum - minimum)}%` }}
        >{tick}</span>)}</div>
      </div>
      <span className="missing-key"><i className="hatched" aria-hidden="true" />{missing}</span>
    </div>
    <p>{metric === "fema" ? <><strong>FEMA ALR_NPCTL</strong> · lower is better · national percentile · not property-level risk</> : metric === "mountain" ? <><strong>Mountain Magnitude</strong> · U.S. {peers} · +1 means 10× fewer equal-or-higher peers · not comparable across grains · not property-specific</> : <><strong>CHR&amp;R Community Conditions</strong> · 1 healthiest · 10 least healthy · county-level clusters, not percentiles</>}</p>
  </div>;
}

function DetailDrawer({ detail, loading, error, level, metric, onClose, onRetry, onViewTracts, onViewCounty }: {
  detail: PlaceDetail | null; loading: boolean; error: string; level: Geography; metric: Metric;
  onClose: () => void; onRetry: () => void;
  onViewTracts: (countyFips: string, state: string) => void; onViewCounty: (countyFips: string) => void;
}) {
  return <aside className="detail-drawer" role="dialog" aria-label={`${level === "tract" ? "Tract" : "County"} detail`} aria-modal="false">
    <button autoFocus className="close" onClick={onClose} aria-label={`Close ${level} detail`}>×</button>
    {loading && <p className="loading-copy">Loading detail…</p>}
    {error && <div className="error" role="alert"><p>{error}</p><button className="secondary" onClick={onRetry}>Retry detail</button></div>}
    {detail && <><p className="eyebrow">{detail.summary.place_type} · {detail.summary.place_id}</p>
      <h2>{detail.summary.name}, {detail.summary.state}</h2>
      <div className="metric-cards">
        <section className={`metric-card ${metric === "fema" ? "active" : ""}`} aria-label="FEMA risk"><strong>Natural Hazard Risk</strong><span style={{ color: scoreColor(detail.summary.risk_score) ?? undefined }}>{scoreLabel(detail.summary)}</span><small>FEMA {level === "county" ? "county " : ""}ALR_NPCTL<br />Lower is better</small></section>
        <section className={`metric-card ${metric === "community-conditions" ? "active" : ""}`} aria-label="Community Conditions"><strong>Community Conditions</strong><span>{communityLabel(detail.summary)}</span><small>CHR&R Community Conditions<br />Better conditions ↑ · County-level <button className="metric-help" title={COMMUNITY_EXPLANATION} aria-label={COMMUNITY_EXPLANATION}>ⓘ</button></small></section>
        <section className={`metric-card ${metric === "mountain" ? "active" : ""}`} aria-label="Mountain Magnitude"><strong>Mountain Magnitude</strong><span>{mountainLabel(detail.summary)}</span><small>U.S. {geographyPlural(level)} resident exposure<br />Fewer equal-or-higher peers ↑ <button className="metric-help" title={mountainExplanation(level)} aria-label={mountainExplanation(level)}>ⓘ</button></small></section>
      </div>
      <details className="mountain-breakdown"><summary>Mountain Magnitude breakdown</summary>{detail.summary.mountain_magnitude === null ? <p>Mountain data is {detail.summary.mountain_coverage_status.replaceAll("_", " ")} for this geography.</p> : <dl className="facts"><div><dt>Relief within 20 km</dt><dd>{detail.summary.relief_20km_m === null ? "Unavailable" : `${number.format(detail.summary.relief_20km_m)} m`} · {percentileLabel(detail.summary.relief_20km_pct)}</dd></div><div><dt>Rugged terrain</dt><dd>{detail.summary.rugged_fraction_20km === null ? "Unavailable" : `${(detail.summary.rugged_fraction_20km * 100).toFixed(1)}%`} · {percentileLabel(detail.summary.rugged_pct)}</dd></div><div><dt>Weighted public access</dt><dd>{detail.summary.public_mountain_access_raw === null ? "Unavailable" : `${detail.summary.public_mountain_access_raw.toFixed(1)} km²`} · {percentileLabel(detail.summary.public_mountain_access_pct)}</dd></div><div><dt>Hiking access</dt><dd>{detail.summary.nearest_mountain_trail_km === null ? "No mapped trail nearby" : `${detail.summary.nearest_mountain_trail_km.toFixed(1)} km nearest`} · {percentileLabel(detail.summary.trail_access_pct)}</dd></div></dl>}<p className="notice">{mountainExplanation(level)}</p></details>
      <dl className="facts"><div><dt>{level === "county" ? "County" : "Tract"} FIPS</dt><dd>{detail.summary.place_id}</dd></div><div><dt>State</dt><dd>{detail.summary.state}</dd></div>{level === "tract" && <div><dt>County</dt><dd>{detail.summary.county_name}</dd></div>}<div><dt>FEMA vintage</dt><dd>{detail.summary.fema_vintage}</dd></div></dl>
      {level === "county" && <button className="secondary" onClick={() => onViewTracts(detail.summary.place_id, detail.summary.state)}>View {number.format(detail.member_tract_count ?? 0)} tracts</button>}
      {level === "tract" && /^\d{5}$/.test(detail.summary.county_fips) && <button className="secondary" onClick={() => onViewCounty(detail.summary.county_fips)}>View {detail.summary.county_name} county</button>}
      <h3>Published hazard percentiles</h3><div className="contributions">{sortedHazardPercentiles(detail.hazard_percentiles).map((hazard) => <div key={hazard.code} className="contribution"><div><span>{hazard.label}</span><span>{hazard.percentile === null ? "No rating" : hazard.percentile.toFixed(1)}</span></div><div className="bar"><i style={{ width: hazard.percentile === null ? "0%" : `${hazard.percentile}%`, backgroundColor: scoreColor(hazard.percentile) ?? undefined }} /></div></div>)}</div>
      <p className="notice">{detail.methodology_notice}</p></>}
  </aside>;
}

type Overlay = "filters" | "extremes" | "exports" | "info" | "more" | "search" | null;
type PlacePage = { items: PlaceSummary[]; total: number };
type DetailTarget = { level: Geography; id: string };

function ResultGroup({ title, items, metric, onChoose, onBrowse }: { title: string; items: PlaceSummary[]; metric: Metric; onChoose: (item: PlaceSummary) => void; onBrowse?: () => void }) {
  return <section><h3>{title}</h3>{items.length
    ? <><ol className="result-list">{items.map((item) => <li key={item.place_id}><button onClick={() => onChoose(item)}><span><strong>{item.name}</strong><small>{item.state} · {item.place_id}</small></span><b>{metricLabel(item, metric)}</b></button></li>)}</ol>{onBrowse && <button className="secondary browse-all" onClick={onBrowse}>Browse all</button>}</>
    : <p className="empty-copy">No ranked geographies</p>}</section>;
}

function Workspace({ meta }: { meta: Meta }) {
  const builtState = meta.build?.scope.kind === "state" ? meta.build.scope.state || "" : "";
  const initialState = builtState || initial.state;
  const initialPlace = !builtState || initial.place.startsWith(STATE_FIPS[builtState as keyof typeof STATE_FIPS] || "-")
    ? initial.place : "";
  const [level, setLevel] = useState<Geography>(initial.level);
  const [metric, setMetric] = useState<Metric>(initial.metric);
  const [renderedMetric, setRenderedMetric] = useState<Metric>(initial.metric);
  const [interactiveMetric, setInteractiveMetric] = useState<Metric | null>(null);
  const [state, setState] = useState(initialState);
  const [county, setCounty] = useState(builtState && initial.state !== builtState ? "" : initial.county);
  const [showUnranked, setShowUnranked] = useState(initial.unranked);
  const [mountainMagnitudeMin, setMountainMagnitudeMin] = useState<number | null>(initial.mountainMagnitudeMin);
  const [selected, setSelected] = useState(initialPlace);
  const [detailTarget, setDetailTarget] = useState<DetailTarget | null>(
    initialPlace ? { level: initial.level, id: initialPlace } : null,
  );
  const [detailRetry, setDetailRetry] = useState(0);
  const [scoreError, setScoreError] = useState("");
  const [scoreReloadNonce, setScoreReloadNonce] = useState(0);
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
  const [draftMountainMagnitudeMin, setDraftMountainMagnitudeMin] = useState<number | null>(mountainMagnitudeMin);
  const [lowest, setLowest] = useState<PlaceSummary[]>([]);
  const [highest, setHighest] = useState<PlaceSummary[]>([]);
  const [lowestTotal, setLowestTotal] = useState(0);
  const [highestTotal, setHighestTotal] = useState(0);
  const [browseGroup, setBrowseGroup] = useState<number | null>(null);
  const [browseItems, setBrowseItems] = useState<PlaceSummary[]>([]);
  const [browseTotal, setBrowseTotal] = useState(0);
  const [browseOffset, setBrowseOffset] = useState(0);
  const [extremesLoading, setExtremesLoading] = useState(false);
  const camera = useRef<CameraState>(initial.camera);
  const [cameraTarget, setCameraTarget] = useState<(CameraState & { nonce: number }) | undefined>();
  const firstSemantic = useRef(true);
  const scopeHashNormalized = useRef(false);
  const restoringHistory = useRef(false);
  const overlayTrigger = useRef<HTMLElement | null>(null);
  const overlayEntry = useRef<HTMLElement | null>(null);
  const restoreOverlayFocus = useRef(true);
  const previousOverlay = useRef<Overlay>(null);
  const detailTrigger = useRef<HTMLElement | null>(null);
  const extremesGeneration = useRef(0);
  const countyGeneration = useRef(0);
  const addressGeneration = useRef(0);
  const selectedMetric = useRef(metric);
  selectedMetric.current = metric;
  const mapVisibleCommit = useCallback((committedMetric: Metric) => {
    if (committedMetric === selectedMetric.current) setRenderedMetric(committedMetric);
  }, []);
  const mapInteractiveCommit = useCallback((committedMetric: Metric) => {
    if (committedMetric === selectedMetric.current) setInteractiveMetric(committedMetric);
  }, []);
  const mapScoresReady = useCallback((count: number) => {
    setScoreError("");
    setStatus(`${number.format(count)} ${geographyPlural(level)} ready`);
  }, [level]);
  const mapScoreFailed = useCallback((message: string) => {
    setScoreError(message);
    setStatus("Map scores failed to load");
  }, []);
  const retryScores = useCallback(() => {
    setScoreError("");
    setInteractiveMetric(null);
    setStatus(`Loading ${level} scores`);
    setScoreReloadNonce((value) => value + 1);
  }, [level]);

  function invalidateAddressLookup() {
    addressGeneration.current += 1;
    setSearching(false);
    setConfirmation(null);
  }

  function toggleOverlay(next: Exclude<Overlay, null>, trigger: HTMLElement) {
    if (overlay === "search") invalidateAddressLookup();
    overlayTrigger.current = trigger;
    restoreOverlayFocus.current = true;
    setOverlay((current) => current === next ? null : next);
  }

  useEffect(() => {
    if (previousOverlay.current === "more" && (overlay === "exports" || overlay === "info")) {
      window.requestAnimationFrame(() => overlayEntry.current?.focus());
    } else if (previousOverlay.current && !overlay && restoreOverlayFocus.current) {
      window.requestAnimationFrame(() => overlayTrigger.current?.focus());
    }
    previousOverlay.current = overlay;
    restoreOverlayFocus.current = true;
  }, [overlay]);

  const closeDetail = useCallback(() => {
    setSelected("");
    setDetailTarget(null);
    window.requestAnimationFrame(() => {
      (detailTrigger.current || document.querySelector<HTMLElement>(".risk-canvas"))?.focus();
    });
  }, []);

  const writeHash = useCallback((mode: "push" | "replace") => {
    const params = new URLSearchParams({ level, metric });
    if (state) params.set("state", state);
    if (level === "tract" && county) params.set("county", county);
    if (selected) params.set("place", selected);
    if (showUnranked) params.set("unranked", "1");
    if (mountainMagnitudeMin !== null) params.set("mountain_magnitude_min", String(mountainMagnitudeMin));
    params.set("cx", camera.current.cx.toFixed(4)); params.set("cy", camera.current.cy.toFixed(4)); params.set("z", camera.current.z.toFixed(3));
    window.history[mode === "push" ? "pushState" : "replaceState"](null, "", `#${params}`);
  }, [county, level, metric, mountainMagnitudeMin, selected, showUnranked, state]);
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
    setDetailTarget({ level, id: placeId });
    setFocusTarget({ kind: "place", id: placeId, nonce: Date.now() });
  }, [level]);

  useEffect(() => {
    const restore = () => {
      invalidateAddressLookup();
      const parsed = readHash(window.location.hash);
      const nextState = builtState || parsed.state;
      const nextPlace = !builtState || parsed.place.startsWith(STATE_FIPS[builtState as keyof typeof STATE_FIPS] || "-")
        ? parsed.place : "";
      restoringHistory.current = true;
      setLevel(parsed.level); setMetric(parsed.metric); setState(nextState); setDraftState(nextState);
      setCounty(builtState && parsed.state !== builtState ? "" : parsed.county);
      setDraftCounty(builtState && parsed.state !== builtState ? "" : parsed.county);
      setSelected(nextPlace);
      setDetailTarget(nextPlace ? { level: parsed.level, id: nextPlace } : null);
      setShowUnranked(parsed.unranked); setDraftUnranked(parsed.unranked);
      setMountainMagnitudeMin(parsed.mountainMagnitudeMin);
      setDraftMountainMagnitudeMin(parsed.mountainMagnitudeMin);
      camera.current = parsed.camera;
      setCameraTarget({ ...parsed.camera, nonce: Date.now() });
      setOverlay(null); setPreview(null);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [builtState]);

  useEffect(() => {
    setDetail(null); setDetailError("");
    if (!detailTarget) return;
    let cancelled = false; setDetailLoading(true);
    const path = detailTarget.level === "county"
      ? `/api/v2/counties/${detailTarget.id}`
      : `/api/v2/places/${detailTarget.id}`;
    json<PlaceDetail>(path).then((value) => { if (!cancelled) { setDetail(value); setStatus(`${value.summary.name} selected`); } }).catch((caught) => { if (!cancelled) setDetailError((caught as Error).message); }).finally(() => { if (!cancelled) setDetailLoading(false); });
    return () => { cancelled = true; };
  }, [detailRetry, detailTarget]);

  useEffect(() => {
    const generation = ++countyGeneration.current;
    setCountyOptions([]);
    if (!draftState) return;
    const params = new URLSearchParams({ state: draftState, limit: "500", sort: "name", direction: "asc", include_unranked: "true" });
    void json<{ items: PlaceSummary[] }>(`/api/v2/counties?${params}`)
      .then((value) => {
        if (generation === countyGeneration.current) {
          setCountyOptions(value.items.filter((item) => item.state === draftState));
        }
      })
      .catch(() => {
        if (generation === countyGeneration.current) setCountyOptions([]);
      });
  }, [draftState]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { if (overlay) { if (overlay === "search") invalidateAddressLookup(); setOverlay(null); } else if (detailTarget) closeDetail(); } };
    window.addEventListener("keydown", escape); return () => window.removeEventListener("keydown", escape);
  }, [closeDetail, detailTarget, overlay]);

  async function openExtremes(trigger: HTMLElement) {
    if (overlay === "extremes") { extremesGeneration.current += 1; setExtremesLoading(false); setOverlay(null); return; }
    const generation = ++extremesGeneration.current;
    toggleOverlay("extremes", trigger); setSearchError(""); setLowest([]); setHighest([]); setLowestTotal(0); setHighestTotal(0); setBrowseGroup(null); setExtremesLoading(true);
    const community = metric === "community-conditions";
    const sort = community ? "community_conditions_group" : metric === "mountain" ? "mountain_magnitude" : "risk_score";
    const base = new URLSearchParams({ limit: community ? "1" : "5", sort });
    if (state) base.set("state", state); if (!community && level === "tract" && county) base.set("county", county);
    if (mountainMagnitudeMin !== null) {
      base.set("mountain_magnitude_min", String(mountainMagnitudeMin));
    }
    if (community) base.set("include_unranked", "true");
    const path = community ? "/api/v2/counties" : level === "tract" ? "/api/v2/places" : "/api/v2/counties";
    try {
      const [low, high] = await Promise.all([json<PlacePage>(`${path}?${base}&direction=asc`), json<PlacePage>(`${path}?${base}&direction=desc`)]);
      if (!community) {
        if (generation === extremesGeneration.current) { setLowest(low.items); setHighest(high.items); setLowestTotal(low.total); setHighestTotal(high.total); }
      } else {
        const lowGroup = low.items[0]?.community_conditions_group;
        const highGroup = high.items[0]?.community_conditions_group;
        const samples = async (group: number | null | undefined) => {
          if (group === null || group === undefined) return { items: [], total: 0 };
          const params = new URLSearchParams({ limit: "5", sort: "name", direction: "asc", include_unranked: "true", community_conditions_group: String(group) });
          if (state) params.set("state", state);
          return json<PlacePage>(`/api/v2/counties?${params}`);
        };
        const [lowSample, highSample] = await Promise.all([samples(lowGroup), samples(highGroup)]);
        if (generation === extremesGeneration.current) { setLowest(lowSample.items); setHighest(highSample.items); setLowestTotal(lowSample.total); setHighestTotal(highSample.total); }
      }
    } catch (caught) {
      if (generation === extremesGeneration.current) setSearchError((caught as Error).message);
    } finally {
      if (generation === extremesGeneration.current) setExtremesLoading(false);
    }
  }
  function choose(place: PlaceSummary) { detailTrigger.current = overlayTrigger.current; restoreOverlayFocus.current = false; const targetLevel = metric === "community-conditions" ? "county" : level; setSelected(targetLevel === level ? place.place_id : ""); setDetailTarget({ level: targetLevel, id: place.place_id }); setFocusTarget(targetLevel === level ? { kind: "place", id: place.place_id, nonce: Date.now() } : null); setOverlay(null); setQuery(""); }
  function switchLevel(next: Geography) { invalidateAddressLookup(); setLevel(next); setCounty(""); setDraftCounty(""); setSelected(""); setDetailTarget(null); setDetail(null); setFocusTarget(null); setOverlay(null); setPreview(null); setScoreError(""); setInteractiveMetric(null); }
  function switchMetric(next: Metric) { setMetric(next); setPreview(null); setOverlay(null); }

  async function browseCommunity(group: number, offset = 0) {
    const generation = ++extremesGeneration.current;
    const params = new URLSearchParams({ limit: "50", offset: String(offset), sort: "name", direction: "asc", include_unranked: "true", community_conditions_group: String(group) });
    if (state) params.set("state", state);
    setExtremesLoading(true); setSearchError("");
    try {
      const page = await json<PlacePage>(`/api/v2/counties?${params}`);
      if (generation !== extremesGeneration.current) return;
      setBrowseGroup(group); setBrowseItems(page.items); setBrowseTotal(page.total); setBrowseOffset(offset);
    } catch (caught) {
      if (generation === extremesGeneration.current) setSearchError((caught as Error).message);
    } finally {
      if (generation === extremesGeneration.current) setExtremesLoading(false);
    }
  }

  function closeCommunityBrowse() {
    extremesGeneration.current += 1;
    setExtremesLoading(false);
    setBrowseGroup(null);
  }

  async function lookupAddress(event: FormEvent) {
    event.preventDefault();
    const generation = ++addressGeneration.current;
    const address = query;
    setSearching(true); setSearchError(""); setConfirmation(null);
    try {
      const result = await json<LookupResult>("/api/v2/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address }) });
      if (generation !== addressGeneration.current) return;
      if (result.status === "confirmation_required") setConfirmation(result); else navigateToLookup(result);
    } catch (caught) {
      if (generation === addressGeneration.current) setSearchError((caught as Error).message);
    } finally {
      if (generation === addressGeneration.current) setSearching(false);
    }
  }
  async function confirmAddress(candidateId: string) {
    if (!confirmation) return;
    const generation = ++addressGeneration.current;
    const address = confirmation.query;
    setSearching(true);
    try {
      const result = await json<LookupResult>("/api/v2/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address, candidate_id: candidateId }) });
      if (generation !== addressGeneration.current) return;
      if (result.status === "resolved") navigateToLookup(result);
    } catch (caught) {
      if (generation === addressGeneration.current) setSearchError((caught as Error).message);
    } finally {
      if (generation === addressGeneration.current) setSearching(false);
    }
  }
  function navigateToLookup(result: AddressLookup) {
    const summary = result.detail.summary;
    detailTrigger.current = overlayTrigger.current;
    restoreOverlayFocus.current = false;
    setLevel("tract"); setState(summary.state); setDraftState(summary.state);
    setCounty(summary.county_fips); setDraftCounty(summary.county_fips);
    setDetail(result.detail); setSelected(result.tract_id); setDetailTarget({ level: "tract", id: result.tract_id });
    setFocusTarget({ kind: "place", id: result.tract_id, nonce: Date.now() });
    setConfirmation(null); setOverlay(null); setQuery("");
    setStatus(`Matched ${summary.name}; showing tract risk only`);
  }
  function applyFilters() {
    const nextState = builtState || draftState;
    setState(nextState); setCounty(level === "tract" ? draftCounty : ""); setShowUnranked(draftUnranked); setMountainMagnitudeMin(draftMountainMagnitudeMin); setSelected(""); setDetailTarget(null); setOverlay(null);
    if (draftCounty) setFocusTarget({ kind: "county", id: draftCounty, nonce: Date.now() }); else if (nextState) setFocusTarget({ kind: "state", id: nextState, nonce: Date.now() }); else setFocusTarget(null);
    setStatus(nextState ? `Filter applied: ${nextState}${draftCounty ? ` · ${draftCounty}` : ""}` : "National filter applied");
  }
  function clearFilters() { setDraftState(builtState); setDraftCounty(""); setDraftUnranked(false); setDraftMountainMagnitudeMin(null); setState(builtState); setCounty(""); setShowUnranked(false); setMountainMagnitudeMin(null); setFocusTarget(builtState ? { kind: "state", id: builtState, nonce: Date.now() } : null); setSelected(""); setDetailTarget(null); setOverlay(null); }

  const tooltipScore = metric === "mountain" ? preview?.score?.mountain_magnitude : preview?.score?.risk_score;
  const tooltipBand = scoreBand(tooltipScore ?? null);
  const tooltipClass = preview ? mapTooltipClass(preview.x, preview.y, window.innerWidth, window.innerHeight) : "";
  const activeFilter = Boolean(state || county || mountainMagnitudeMin !== null || (metric !== "community-conditions" && showUnranked));
  const moreOpen = overlay === "more" || overlay === "exports" || overlay === "info";
  const moreControls = overlay === "exports" ? "exports-panel" : overlay === "info" ? "info-panel" : "more-panel";
  const metricName = metric === "fema" ? "FEMA Risk" : metric === "mountain" ? "Mountain Magnitude" : "Community Conditions";
  const mapUpdating = !scoreError && (renderedMetric !== metric || interactiveMetric !== metric);
  const lowestGroup = lowest[0]?.community_conditions_group ?? null;
  const highestGroup = highest[0]?.community_conditions_group ?? null;
  const noCommunityGroups = metric === "community-conditions" && lowestGroup === null && highestGroup === null;
  return <main className="map-shell">
    <RiskMap manifestUrl={meta.map_assets.manifest_url} scoreUrl={`/api/v2/map/scores?level=${level}`} expectedBuildId={meta.build?.build_id || ""} level={level} metric={metric} displayMetric={renderedMetric} busy={mapUpdating} selected={selected} state={state} county={county} showUnranked={showUnranked} mountainMagnitudeMin={mountainMagnitudeMin} neutralOnly={Boolean(scoreError)} retryGeneration={scoreReloadNonce} focusTarget={focusTarget} cameraTarget={cameraTarget} initialCamera={initial.camera} onSelect={selectFromMap} onPreview={setPreview} onCamera={cameraChanged} onStatus={setStatus} onScoresReady={mapScoresReady} onScoreError={mapScoreFailed} onVisibleCommit={mapVisibleCommit} onInteractiveCommit={mapInteractiveCommit} />
    <header className={`top-dock${overlay ? " overlay-open" : ""}`}><div className="brand"><strong>HouseHunter</strong><span>{metric === "fema" ? "FEMA ALR_NPCTL" : metric === "mountain" ? "Mountain Magnitude" : "Community Conditions"}</span></div><div className="level-toggle" aria-label="Geography level"><button aria-pressed={level === "tract"} onClick={() => switchLevel("tract")}>Tracts</button><button aria-pressed={level === "county"} onClick={() => switchLevel("county")}>Counties</button></div><div className="metric-toggle" aria-label="Map metric"><button aria-pressed={metric === "fema"} onClick={() => switchMetric("fema")}>FEMA Risk</button><button aria-pressed={metric === "community-conditions"} onClick={() => switchMetric("community-conditions")}>Community Conditions</button><button aria-pressed={metric === "mountain"} onClick={() => switchMetric("mountain")}>Mountain Magnitude</button></div><div className="dock-actions"><button aria-expanded={overlay === "search"} aria-controls="search-panel" onClick={(event) => toggleOverlay("search", event.currentTarget)}>Search</button><button aria-expanded={overlay === "extremes"} aria-controls="extremes-panel" onClick={(event) => void openExtremes(event.currentTarget)}>{metric === "community-conditions" ? "Best / Worst" : "Lowest / Highest"}</button><button aria-expanded={overlay === "filters"} aria-controls="filters-panel" className={activeFilter ? "active" : ""} onClick={(event) => toggleOverlay("filters", event.currentTarget)}>Filters{activeFilter ? " · On" : ""}</button><button className="desktop-dock-action" aria-expanded={overlay === "exports"} aria-controls="exports-panel" onClick={(event) => toggleOverlay("exports", event.currentTarget)}>Exports</button><button className="desktop-dock-action" aria-expanded={overlay === "info"} aria-controls="info-panel" aria-label="Information" onClick={(event) => toggleOverlay("info", event.currentTarget)}>ⓘ</button><button className={`mobile-more${moreOpen ? " active" : ""}`} aria-expanded={moreOpen} aria-controls={moreControls} onClick={(event) => { if (moreOpen) { overlayTrigger.current = event.currentTarget; setOverlay(null); } else toggleOverlay("more", event.currentTarget); }}>More</button></div><div className={`build-pill ${scoreError ? "failed" : ""}`} title={meta.build?.build_id}>{scoreError ? "Score error" : status}</div></header>
    {overlay === "search" && <section id="search-panel" className="floating-panel search-panel" aria-label="Search"><form onSubmit={lookupAddress} aria-busy={searching}><label>Street address<input autoFocus autoComplete="street-address" autoCapitalize="words" enterKeyHint="search" value={query} onChange={(event) => { invalidateAddressLookup(); setSearchError(""); setQuery(event.target.value); }} placeholder="Street, city, state, ZIP" /></label><p className="privacy-note">Find tract contacts the US Census geocoder through this loopback server and may send it to OpenStreetMap only after a valid Census no-match. Addresses are not written to disk. Do not submit confidential addresses. The map shows tract risk, never a property marker or score.</p><button className="primary" disabled={searching || !query.trim()}>{searching ? "Looking…" : "Find tract"}</button>{confirmation && <div className="confirm-card" role="region" aria-label="Approximate street match"><strong>Confirm approximate street match</strong><p>{confirmation.message}</p>{confirmation.candidates.map((candidate) => <button type="button" className="secondary" key={candidate.candidate_id} onClick={() => void confirmAddress(candidate.candidate_id)}>Use approximate street location: {candidate.matched_address}</button>)}<small>{confirmation.attribution}</small></div>}</form>{searchError && <p role="alert" className="error">{searchError}</p>}</section>}
    {overlay === "filters" && <section id="filters-panel" className="floating-panel filters-panel" aria-label="Map filters"><h2>Filters</h2><label>State<select value={draftState} disabled={Boolean(builtState)} onChange={(event) => { setDraftState(event.target.value); setDraftCounty(""); }}><option value="">All states & territories</option>{STATE_ABBREVIATIONS.map((item) => <option key={item}>{item}</option>)}</select></label>{level === "tract" && <label>County<select value={draftCounty} disabled={!draftState} onChange={(event) => setDraftCounty(event.target.value)}><option value="">All counties</option>{countyOptions.map((item) => <option value={item.place_id} key={item.place_id}>{item.name}</option>)}</select></label>}<label>Minimum Mountain Magnitude<input type="number" min="0" step="0.01" value={draftMountainMagnitudeMin ?? ""} onChange={(event) => { const value = Number(event.target.value); setDraftMountainMagnitudeMin(event.target.value === "" ? null : Number.isFinite(value) && value >= 0 ? value : null); }} placeholder="Any" /></label>{metric === "community-conditions" ? <p className="filter-note">Not-grouped geographies always remain visible on this layer.</p> : <label className="check"><input type="checkbox" checked={draftUnranked} onChange={(event) => setDraftUnranked(event.target.checked)} />Show {metric === "mountain" ? "Mountain-unavailable" : "FEMA-unranked"} geographies</label>}<div className="panel-buttons"><button className="primary" onClick={applyFilters}>Apply</button><button className="secondary" onClick={clearFilters}>Clear</button></div></section>}
    {overlay === "extremes" && <section id="extremes-panel" className="floating-panel extremes-panel" aria-label={metric === "fema" ? "Lowest and highest risk" : metric === "mountain" ? "Lowest and highest Mountain Magnitude" : "Best and worst Community Conditions"} aria-busy={extremesLoading}><h2>Explore the range</h2>{browseGroup !== null ? <><button className="secondary" onClick={closeCommunityBrowse}>← Back to groups</button><ResultGroup title={`Group ${browseGroup} · ${number.format(browseTotal)} counties`} items={browseItems} metric={metric} onChoose={choose} /><div className="pager"><button className="secondary" disabled={browseOffset === 0} onClick={() => void browseCommunity(browseGroup, Math.max(0, browseOffset - 50))}>Previous</button><span>{number.format(browseOffset + 1)}–{number.format(Math.min(browseOffset + browseItems.length, browseTotal))} of {number.format(browseTotal)}</span><button className="secondary" disabled={browseOffset + 50 >= browseTotal} onClick={() => void browseCommunity(browseGroup, browseOffset + 50)}>Next</button></div></> : extremesLoading ? <p className="loading-copy" role="status">{metric === "community-conditions" ? "Loading best and worst groups…" : "Loading lowest and highest…"}</p> : noCommunityGroups ? <p>No grouped counties in this scope.</p> : <div><ResultGroup title={metric === "community-conditions" ? `Best present · Group ${lowestGroup} · ${number.format(lowestTotal)} counties` : "Lowest"} items={lowest} metric={metric} onChoose={choose} onBrowse={metric === "community-conditions" && lowestGroup !== null ? () => void browseCommunity(lowestGroup) : undefined} /><ResultGroup title={metric === "community-conditions" ? `Worst present · Group ${highestGroup} · ${number.format(highestTotal)} counties` : "Highest"} items={highest} metric={metric} onChoose={choose} onBrowse={metric === "community-conditions" && highestGroup !== null ? () => void browseCommunity(highestGroup) : undefined} /></div>}{searchError && <p role="alert" className="error">{searchError}</p>}</section>}
    {overlay === "more" && <section id="more-panel" className="floating-panel more-panel" aria-label="More actions"><h2>More</h2><button className="secondary" onClick={() => setOverlay("exports")}>Export snapshot</button><button className="secondary" onClick={() => setOverlay("info")}>About this map</button></section>}
    {overlay === "exports" && <nav id="exports-panel" className="floating-panel export-panel" aria-label="Exports"><h2>Export snapshot</h2><a ref={(node) => { overlayEntry.current = node; }} href="/api/v2/exports/places.csv" download>Tracts CSV</a><a href="/api/v2/exports/places.parquet" download>Tracts Parquet</a><a href="/api/v2/exports/counties.csv" download>Counties CSV</a><a href="/api/v2/exports/counties.parquet" download>Counties Parquet</a></nav>}
    {overlay === "info" && <section id="info-panel" className="floating-panel info-panel" aria-label="About this map"><h2 ref={(node) => { overlayEntry.current = node; }} tabIndex={-1}>About this map</h2><p>Every geography is drawn from the pinned FEMA National Risk Index December 2025 release. Dense tracts become distinguishable as you zoom; none are aggregated or enlarged.</p><p>Tracts and counties use separate FEMA percentile universes. County values are never tract averages. Hazard percentiles appear only in details.</p><p>{COMMUNITY_EXPLANATION} Tract colors inherit their county group and never imply tract-level resolution.</p><p>{mountainExplanation(level)}</p><p>HouseHunter is local-only and uses no basemap, telemetry, account, or hosted database.</p></section>}
    {preview && <div className={tooltipClass} style={{ left: preview.x, top: preview.y }}><strong>{preview.name}</strong><span>{preview.state} · {preview.placeId}</span><b>{metric === "fema" ? preview.score?.risk_score === null || !preview.score ? "Not ranked / unavailable" : `${preview.score.risk_score.toFixed(1)} · ${tooltipBand ? SCORE_BAND_LABELS[tooltipBand] : ""}` : metric === "mountain" ? preview.score?.mountain_magnitude === null || !preview.score ? "Mountain Magnitude unavailable" : `M${preview.score.mountain_magnitude.toFixed(2)}` : preview.score?.community_conditions_group === null || !preview.score ? "Not grouped · County-level" : `Group ${preview.score.community_conditions_group} of 10 · County-level`}</b></div>}
    {scoreError && <section className="recovery-card" role="alert"><strong>Scores could not be loaded</strong><span>{scoreError}</span><button className="secondary" onClick={retryScores}>Retry scores</button></section>}
    {mapUpdating && <div className="map-updating" aria-hidden="true">{renderedMetric !== metric ? `Updating ${metricName} map…` : `Preparing ${metricName} interaction…`}</div>}
    <MetricLegend metric={renderedMetric} level={level} />
    {detailTarget && <DetailDrawer detail={detail} loading={detailLoading} error={detailError} level={detailTarget.level} metric={metric} onClose={closeDetail} onRetry={() => setDetailRetry((value) => value + 1)} onViewTracts={(countyFips, nextState) => { setLevel("tract"); setState(nextState); setDraftState(nextState); setCounty(countyFips); setDraftCounty(countyFips); setSelected(""); setDetailTarget(null); setFocusTarget({ kind: "county", id: countyFips, nonce: Date.now() }); }} onViewCounty={(countyFips) => { setLevel("county"); setCounty(""); setDraftCounty(""); setSelected(countyFips); setDetailTarget({ level: "county", id: countyFips }); setFocusTarget({ kind: "place", id: countyFips, nonce: Date.now() }); }} />}
    <p className="sr-only" aria-live="polite">{status}</p>
  </main>;
}

function App() {
  const [meta, setMeta] = useState<Meta | null>(null); const [error, setError] = useState("");
  const load = useCallback(() => { setError(""); void json<Meta>("/api/v2/meta").then(setMeta).catch((caught) => setError((caught as Error).message)); }, []);
  useEffect(load, [load]);
  if (!meta) return <main className="map-shell boot"><div className="boot-copy" role={error ? "alert" : "status"}>{error || "Opening HouseHunter map…"}{error && <button onClick={load}>Retry</button>}</div></main>;
  const mapAssets = meta.map_assets || { ready: false, error: "Map asset status is missing", schema_version: null, release: null, manifest_url: "/map-assets/manifest.json" };
  if (!mapAssets.ready) return <main className="map-shell"><RiskMap manifestUrl={mapAssets.manifest_url} scoreUrl="/api/v2/map/scores?level=tract" expectedBuildId="" level="tract" selected="" state="" county="" showUnranked={false} neutralOnly focusTarget={null} initialCamera={initial.camera} onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined} /><div className="asset-repair" role="alert"><h1>Map boundaries need repair</h1><p>{mapAssets.error}</p><p>Run <code>uv run python scripts/generate_map_assets.py</code> from the HouseHunter checkout, then restart the app.</p></div></main>;
  const readyMeta = { ...meta, map_assets: mapAssets };
  if (!meta.build) return <main className="map-shell"><RiskMap manifestUrl={mapAssets.manifest_url} scoreUrl="/api/v2/map/scores?level=tract" expectedBuildId="" level="tract" selected="" state="" county="" showUnranked={false} neutralOnly focusTarget={null} initialCamera={initial.camera} onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined} /><header className="top-dock"><div className="brand"><strong>HouseHunter</strong><span>FEMA ALR_NPCTL</span></div><div className="build-pill">Setup required</div></header><Setup token={meta.mutation_token} onReady={load} /><div className="legend"><p><strong>FEMA ALR_NPCTL</strong> · lower is better · not property-level risk</p></div></main>;
  return <Workspace meta={readyMeta} />;
}

export default App;
