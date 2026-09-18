import { FormEvent, type KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import RiskMap, { type FocusTarget, type MapPreview } from "./RiskMap";
import CountyFit, { writeCountyFitHash } from "./CountyFitWorkspace";
import { COUNTY_FIT_PRESETS, EMPTY_COUNTY_FIT_FILTERS, readCountyFitHash } from "./countyFit";
import { METRIC_UI, metricColorScale, mountainColor, readHash, scoreBand, scoreColor, STATE_ABBREVIATIONS, STATE_FIPS, type CameraState, type ScoreBand } from "./map";
import { requestedMapAddons } from "./mapScores";
import type { AddressConfirmation, AddressLookup, Geography, HazardPercentile, JobStatus, LayerDescriptor, LookupResult, MapFilters, MapMetric, MapScoreAddonKind, Meta, Metric, PlaceDetail, PlaceSummary, SourceDescriptor } from "./types";

export { scoreBand } from "./map";
export type { ScoreBand } from "./map";

const number = new Intl.NumberFormat("en-US");
const rarityPercent = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 2 });
const COMMUNITY_EXPLANATION = "National CHR&R Community Conditions Health Group. Group 1 represents the healthiest community conditions and Group 10 the least healthy. Groups are data-driven clusters, not percentiles.";
const geographyPlural = (level: Geography) => level === "tract" ? "tracts" : "counties";
const mountainExplanation = (level: Geography) => `Mountain Magnitude ranks resident-weighted mountain terrain and access exposure among U.S. ${geographyPlural(level)}. Each +1 means ten times fewer ${geographyPlural(level)} have an equal-or-higher base exposure; it does not mean ten times more mountainous terrain. Tract and county magnitudes use separate peer groups and are not comparable. It does not measure property-specific views, trail quality, drive time, or guaranteed access.`;
const METRICS = Object.keys(METRIC_UI) as Metric[];
const EMPTY_FILTERS: MapFilters = {
  state: "", county: "", showUnavailable: false, mountainMagnitudeMin: null,
  communityConditionsGroupMax: null, costOfLivingIndexMax: null,
  homeSqftFor1mMin: null, housingBuilt2000PlusPctMin: null,
};

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
  return place.res_hazard_npctl === null ? "Not ranked" : place.res_hazard_npctl.toFixed(1);
}

export function communityLabel(place: PlaceSummary): string {
  return place.community_conditions_group === null
    ? "Not grouped"
    : `Group ${place.community_conditions_group} of 10`;
}

export function mountainLabel(place: PlaceSummary): string {
  return place.mountain_magnitude === null ? "Unavailable" : `M${place.mountain_magnitude.toFixed(2)}`;
}

export function costOfLivingLabel(place: PlaceSummary): string {
  return place.cost_of_living_index === null
    ? "Unavailable" : `${place.cost_of_living_index.toFixed(1)} RPP`;
}

export function homeCostsLabel(place: PlaceSummary): string {
  return place.home_sqft_for_1m === null
    ? "Unavailable" : `${number.format(place.home_sqft_for_1m)} sq ft / $1M`;
}

export function mountainRarityLabel(magnitude: number | null, level: Geography): string {
  if (magnitude === null || !Number.isFinite(magnitude) || magnitude < 0) return "Rarity unavailable";
  return `≈ top ${rarityPercent.format(100 * 10 ** -magnitude)}% of U.S. ${geographyPlural(level)} by base exposure`;
}

function percentileLabel(value: number | null): string {
  return value === null ? "percentile unavailable" : `${value.toFixed(1)} pct`;
}

function metricLabel(place: PlaceSummary, metric: Metric): string {
  if (metric === "residential-hazard") return scoreLabel(place);
  if (metric === "community-conditions") return communityLabel(place);
  if (metric === "mountain") return mountainLabel(place);
  return metric === "cost-of-living" ? costOfLivingLabel(place) : homeCostsLabel(place);
}

function displayValue(value: number | null, suffix = ""): string {
  return value === null ? "Unavailable" : `${number.format(value)}${suffix}`;
}

function appendFilterParams(params: URLSearchParams, filters: MapFilters, level: Geography) {
  if (filters.state) params.set("state", filters.state);
  if (level === "tract" && filters.county) params.set("county", filters.county);
  if (filters.showUnavailable) params.set("include_unranked", "true");
  if (filters.mountainMagnitudeMin !== null) {
    params.set("mountain_magnitude_min", String(filters.mountainMagnitudeMin));
  }
  if (filters.communityConditionsGroupMax !== null) {
    params.set("max_community_conditions_group", String(filters.communityConditionsGroupMax));
  }
  if (filters.costOfLivingIndexMax !== null) {
    params.set("cost_of_living_index_max", String(filters.costOfLivingIndexMax));
  }
  if (filters.homeSqftFor1mMin !== null) {
    params.set("home_sqft_for_1m_min", String(filters.homeSqftFor1mMin));
  }
  if (filters.housingBuilt2000PlusPctMin !== null) {
    params.set("housing_built_2000_plus_pct_min", String(filters.housingBuilt2000PlusPctMin));
  }
}

export function scorePillLabel(place: PlaceSummary): string | undefined {
  const band = scoreBand(place.res_hazard_npctl);
  return band ? `${scoreLabel(place)}, ${SCORE_BAND_LABELS[band]}` : undefined;
}

export function sensitivityLabel(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "Unavailable";
  if (value < 10) return "Low";
  if (value <= 20) return "Moderate";
  return "High";
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
        const latest = await json<JobStatus>(`/api/v3/jobs/${job.job_id}`);
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
      setJob(await json<JobStatus>("/api/v3/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-HouseHunter-Token": token },
        body: JSON.stringify({ kind: "prepare" }),
      }));
    } catch (caught) { setError((caught as Error).message); }
  }
  async function cancel() {
    if (!job) return;
    try {
      setJob(await json<JobStatus>(`/api/v3/jobs/${job.job_id}`, {
        method: "DELETE", headers: { "X-HouseHunter-Token": token },
      }));
    } catch (caught) { setError((caught as Error).message); }
  }
  return <section className="setup-card" aria-labelledby="prepare-title">
    <p className="eyebrow">Private local snapshot</p><h1 id="prepare-title">Prepare the national hazard map</h1>
    <p>HouseHunter downloads pinned FEMA and CHR&R county attributes and builds a read-only snapshot on this Mac.</p>
    {job && <div className="progress-card" aria-live="polite"><div><strong>{job.message}</strong><span>{job.progress}%</span></div><progress max="100" value={job.progress}>{job.progress}%</progress>{["queued", "running"].includes(job.state) && <button className="secondary" onClick={cancel}>Cancel</button>}</div>}
    {error && <p role="alert" className="error">{error}</p>}
    {(!job || ["failed", "cancelled"].includes(job.state)) && <button className="primary" onClick={prepare}>{job ? "Try preparation again" : "Prepare national data"}</button>}
    <p className="fine">FEMA NRI December 2025 v1.20 · CHR&R 2025 · BEA RPP optional · bundled ACS housing stock · no live Census download required</p>
  </section>;
}

function MetricLegend({ metric, level, descriptor }: {
  metric: Metric; level: Geography; descriptor?: LayerDescriptor;
}) {
  const { minimum, maximum, ticks, gradient } = metricColorScale(metric, level);
  const peers = geographyPlural(level);
  const missing = metric === "residential-hazard" ? "Unranked"
    : metric === "community-conditions" ? "Not grouped" : "Unavailable";
  const label = metric === "residential-hazard"
    ? "Continuous Residential Hazard Exposure color scale, higher is worse"
    : metric === "mountain"
      ? `Stepped Mountain Magnitude color scale for U.S. ${peers}, higher means fewer equal-or-higher peers`
      : metric === "community-conditions"
        ? "Continuous Community Conditions color scale, Group 1 is healthiest"
        : metric === "cost-of-living"
          ? "Cost of Living color scale from 80 to 120, lower is better, U.S. equals 100"
          : "Home Costs national buying power percentile color scale, higher is better";
  const rampLabel = metric === "residential-hazard"
    ? "Residential Hazard Exposure continuous color ramp from 0 to 100"
    : metric === "mountain"
      ? `Mountain Magnitude half-step color bands from M0 up to M${maximum} for U.S. ${peers}; M${maximum} and above use the separate cap color`
      : metric === "community-conditions"
        ? "Community Conditions color ramp from Group 1 to Group 10"
        : metric === "cost-of-living"
          ? "BEA Regional Price Parity color ramp from 80 to 120 with U.S. 100 marked"
          : "Home buying power national percentile color ramp from 0 to 100";
  const explanation = metric === "residential-hazard"
    ? <><strong>Residential Hazard Exposure</strong> · HouseHunter composite of FEMA building-specific loss rates · higher is worse · national percentile within {peers} · not property-level risk</>
    : metric === "mountain"
      ? <><strong>Mountain Magnitude</strong> · ½-step colors ≈ 3.2× fewer peers · M1 ≈ top 10% · M2 ≈ top 1% · M3 ≈ top 0.1% · U.S. {peers} · not comparable across grains · not property-specific</>
      : metric === "community-conditions"
        ? <><strong>CHR&amp;R Community Conditions</strong> · 1 healthiest · 10 least healthy · county-level clusters, not percentiles</>
        : metric === "cost-of-living"
          ? <><strong>BEA all-items RPP</strong> · U.S. = 100 · lower is better · values outside 80–120 use the endpoint color</>
          : <><strong>Square feet purchasable for $1M</strong> · national county percentile · higher is better · asking-market indicator</>;
  return <div className="legend" role="group" aria-label={label}>
    <div className="legend-keys">
      <div className="continuous-key">
        <i className="legend-gradient" style={{ backgroundImage: gradient }} role="img" aria-label={rampLabel} />
        <div className="legend-ticks">{ticks.map((tick) => <span
          key={tick}
          style={{ left: `${(tick - minimum) * 100 / (maximum - minimum)}%` }}
        >{metric === "cost-of-living" && tick === 100 ? "100 · U.S." : tick}</span>)}</div>
      </div>
      {metric === "mountain" && <span className="missing-key mountain-cap-key"><i aria-hidden="true" style={{ backgroundColor: mountainColor(maximum, level) ?? undefined }} />M{maximum}+</span>}
      <span className="missing-key"><i className="hatched" aria-hidden="true" />{missing}</span>
    </div>
    <p>{explanation}</p>
    {descriptor && <p className={`layer-source ${descriptor.availability}`}><strong>{descriptor.source}</strong> · {descriptor.vintage} · {descriptor.availability === "available" ? descriptor.geography : `Unavailable in this snapshot. ${descriptor.notice}`}</p>}
  </div>;
}

function DetailDrawer({ detail, loading, error, level, metric, sources, onClose, onRetry, onViewTracts, onViewCounty }: {
  detail: PlaceDetail | null; loading: boolean; error: string; level: Geography; metric: Metric;
  sources: SourceDescriptor[];
  onClose: () => void; onRetry: () => void;
  onViewTracts: (countyFips: string, state: string) => void; onViewCounty: (countyFips: string) => void;
}) {
  const homeSource = sources.find((source) => source.source === "home_market");
  const orderedMetrics = [...METRICS].sort((left, right) =>
    Number(right === metric) - Number(left === metric));
  const highestHazards = detail ? sortedHazardPercentiles(detail.hazard_percentiles)
    .filter((hazard) => hazard.availability === "valid" && hazard.percentile !== null)
    .slice(0, 3) : [];
  const card = (item: Metric, summary: PlaceSummary) => {
    if (item === "residential-hazard") return <section key={item} className={`metric-card dimension-card ${metric === item ? "active" : ""}`} aria-label="Residential Hazard Exposure"><strong>Residential Hazard Exposure</strong><span style={{ color: scoreColor(summary.res_hazard_npctl) ?? undefined }}>{scoreLabel(summary)}</span><small>HouseHunter composite of FEMA building-specific loss rates · Higher is worse</small><dl className="card-facts"><div><dt>Expected Property Loss</dt><dd>{percentileLabel(summary.property_loss_npctl)}</dd></div><div><dt>Model Sensitivity</dt><dd>{summary.res_hazard_spread === null ? "Unavailable" : `${summary.res_hazard_spread.toFixed(1)} · ${sensitivityLabel(summary.res_hazard_spread)}`}</dd></div><div><dt>FEMA ALR_NPCTL</dt><dd>{summary.alr_npctl.toFixed(1)}</dd></div><div><dt>FEMA ALR_VALB rate</dt><dd>{summary.alr_valb === null ? "Unavailable" : summary.alr_valb.toPrecision(4)}</dd></div><div><dt>Hazard data</dt><dd>{summary.res_hazard_data_quality} · {summary.res_hazard_available_count}/17 available</dd></div></dl></section>;
    if (item === "community-conditions") return <section key={item} className={`metric-card ${metric === item ? "active" : ""}`} aria-label="Community Conditions"><strong>Community Conditions</strong><span>{communityLabel(summary)}</span><small>CHR&amp;R Community Conditions<br />Group 1 is healthiest{level === "tract" ? ` · Inherited from ${summary.county_name} County` : " · County geography"} <button className="metric-help" title={COMMUNITY_EXPLANATION} aria-label={COMMUNITY_EXPLANATION}>ⓘ</button></small></section>;
    if (item === "mountain") return <section key={item} className={`metric-card ${metric === item ? "active" : ""}`} aria-label="Mountain Magnitude"><strong>Mountain Magnitude</strong><span>{mountainLabel(summary)}</span><small>{summary.mountain_magnitude !== null && <>{mountainRarityLabel(summary.mountain_magnitude, level)}<br /></>}Resident exposure · fewer equal-or-higher peers ↑</small><details className="card-breakdown"><summary>Mountain Magnitude breakdown</summary>{summary.mountain_magnitude === null ? <p>Mountain data is {summary.mountain_coverage_status.replaceAll("_", " ")} for this geography.</p> : <dl className="card-facts"><div><dt>Relief within 20 km</dt><dd>{summary.relief_20km_m === null ? "Unavailable" : `${number.format(summary.relief_20km_m)} m`} · {percentileLabel(summary.relief_20km_pct)}</dd></div><div><dt>Rugged terrain</dt><dd>{summary.rugged_fraction_20km === null ? "Unavailable" : `${(summary.rugged_fraction_20km * 100).toFixed(1)}%`} · {percentileLabel(summary.rugged_pct)}</dd></div><div><dt>Weighted public access</dt><dd>{summary.public_mountain_access_raw === null ? "Unavailable" : `${summary.public_mountain_access_raw.toFixed(1)} km²`} · {percentileLabel(summary.public_mountain_access_pct)}</dd></div><div><dt>Hiking access</dt><dd>{summary.nearest_mountain_trail_km === null ? "No mapped trail nearby" : `${summary.nearest_mountain_trail_km.toFixed(1)} km nearest`} · {percentileLabel(summary.trail_access_pct)}</dd></div></dl>}<p className="card-notice">{mountainExplanation(level)}</p></details></section>;
    if (item === "cost-of-living") return <section key={item} className={`metric-card dimension-card ${metric === item ? "active" : ""}`} aria-label="Cost of Living"><strong>Cost of Living</strong><span>{costOfLivingLabel(summary)}</span><small>BEA all-items Regional Price Parity · U.S. = 100 · Lower is better</small><dl className="card-facts"><div><dt>Goods</dt><dd>{displayValue(summary.cost_of_living_goods_index)}</dd></div><div><dt>Housing rents</dt><dd>{displayValue(summary.cost_of_living_housing_rents_index)}</dd></div><div><dt>Utilities</dt><dd>{displayValue(summary.cost_of_living_utilities_index)}</dd></div><div><dt>Other services</dt><dd>{displayValue(summary.cost_of_living_other_services_index)}</dd></div></dl><p className="card-notice">{level === "tract" && `Inherited from ${summary.county_name} County. `}{summary.cost_of_living_geography_name ? `BEA ${summary.cost_of_living_geography_name} ${summary.cost_of_living_geography_type === "metropolitan" ? "MSA" : "area"}. ` : `Coverage: ${summary.cost_of_living_coverage_status.replaceAll("_", " ")}. `}{summary.cost_of_living_release_year ? `Release ${summary.cost_of_living_release_year}.` : ""}</p></section>;
    const housingVintage = summary.housing_stock_release_year
      ? `ACS ${summary.housing_stock_release_year} five-year estimate`
      : "ACS housing-stock asset";
    const housingNotice = summary.housing_stock_coverage_status === "complete"
      ? `Housing stock uses direct ${level} ${housingVintage}s. `
      : `Housing stock coverage: ${summary.housing_stock_coverage_status.replaceAll("_", " ")} (${housingVintage}). `;
    return <section key={item} className={`metric-card dimension-card ${metric === item ? "active" : ""}`} aria-label="Home Costs"><strong>Home Costs</strong><span>{homeCostsLabel(summary)}</span><small>{summary.home_buying_power_percentile === null ? "National buying-power percentile unavailable" : `${summary.home_buying_power_percentile.toFixed(1)} national county percentile`} · Higher is better</small><dl className="card-facts"><div><dt>Median listing price</dt><dd>{summary.home_median_listing_price === null ? "Unavailable" : `$${number.format(Math.round(summary.home_median_listing_price))}`}</dd></div><div><dt>Median price / sq ft</dt><dd>{summary.home_median_listing_price_per_square_foot === null ? "Unavailable" : `$${number.format(Math.round(summary.home_median_listing_price_per_square_foot))}`}</dd></div><div><dt>Median listed size</dt><dd>{displayValue(summary.home_median_square_feet === null ? null : Math.round(summary.home_median_square_feet), " sq ft")}</dd></div><div><dt>Active listings</dt><dd>{displayValue(summary.home_active_listing_count === null ? null : Math.round(summary.home_active_listing_count))}</dd></div><div><dt>Built 2000+</dt><dd>{summary.housing_built_2000_plus_pct === null ? "Unavailable" : `${Math.round(summary.housing_built_2000_plus_pct)}%`}</dd></div><div><dt>Built 2010+</dt><dd>{summary.housing_built_2010_plus_pct === null ? "Unavailable" : `${Math.round(summary.housing_built_2010_plus_pct)}%`}</dd></div><div><dt>Built 2020+</dt><dd>{summary.housing_built_2020_plus_pct === null ? "Unavailable" : `${Math.round(summary.housing_built_2020_plus_pct)}%`}</dd></div><div><dt>Median year built</dt><dd>{summary.housing_median_year_built === null ? "Unavailable" : String(Math.round(summary.housing_median_year_built))}</dd></div></dl><p className="card-notice">{level === "tract" ? `Market values inherited from ${summary.county_name} County. ` : "County market geography. "}Realtor.com {summary.home_market_month ?? "market source unavailable"}. {housingNotice}{homeSource?.stale ? "Market release is stale. " : ""}{summary.home_costs_coverage_status !== "complete" ? `Market coverage: ${summary.home_costs_coverage_status.replaceAll("_", " ")}. Run househunter import-home-market FILE --acknowledge-personal-use. ` : ""}{summary.home_market_usage_notice}</p></section>;
  };
  return <aside className="detail-drawer" role="dialog" aria-label={`${level === "tract" ? "Tract" : "County"} detail`} aria-modal="false">
    <button autoFocus className="close" onClick={onClose} aria-label={`Close ${level} detail`}>×</button>
    {loading && <p className="loading-copy">Loading detail…</p>}
    {error && <div className="error" role="alert"><p>{error}</p><button className="secondary" onClick={onRetry}>Retry detail</button></div>}
    {detail && <><p className="eyebrow">{detail.summary.place_type} · {detail.summary.place_id}</p>
      <h2>{detail.summary.name}, {detail.summary.state}</h2>
      <div className="metric-cards">
        {orderedMetrics.map((item) => card(item, detail.summary))}
      </div>
      <dl className="facts"><div><dt>{level === "county" ? "County" : "Tract"} FIPS</dt><dd>{detail.summary.place_id}</dd></div><div><dt>State</dt><dd>{detail.summary.state}</dd></div>{level === "tract" && <div><dt>County</dt><dd>{detail.summary.county_name}</dd></div>}<div><dt>FEMA vintage</dt><dd>{detail.summary.fema_vintage}</dd></div></dl>
      {level === "county" && <button className="secondary" onClick={() => onViewTracts(detail.summary.place_id, detail.summary.state)}>View {number.format(detail.member_tract_count ?? 0)} tracts</button>}
      {level === "tract" && /^\d{5}$/.test(detail.summary.county_fips) && <button className="secondary" onClick={() => onViewCounty(detail.summary.county_fips)}>View {detail.summary.county_name} county</button>}
      <h3>Highest Residential Hazards</h3><ol className="result-list">{highestHazards.map((hazard) => <li key={hazard.code}><span><strong>{hazard.label}</strong><b>{hazard.percentile!.toFixed(1)}</b></span></li>)}</ol>
      <h3>All residential hazard percentiles</h3><div className="contributions">{sortedHazardPercentiles(detail.hazard_percentiles).map((hazard) => <div key={hazard.code} className="contribution"><div><span>{hazard.label}<small>FEMA ALRB {hazard.raw_alrb === null ? "unavailable" : hazard.raw_alrb.toPrecision(4)} · {hazard.fema_eal_rating ?? hazard.availability.replaceAll("_", " ")}</small></span><span>{hazard.availability === "not_applicable" ? "0.0 · Not applicable" : hazard.percentile === null ? hazard.availability.replaceAll("_", " ") : hazard.percentile.toFixed(1)}</span></div><div className="bar"><i style={{ width: hazard.percentile === null ? "0%" : `${hazard.percentile}%`, backgroundColor: scoreColor(hazard.percentile) ?? undefined }} /></div></div>)}</div>
      {(detail.source_notices ?? []).map((notice) => <p key={notice} className="notice">{notice}</p>)}
      <p className="notice">{detail.methodology_notice}</p></>}
  </aside>;
}

type Overlay = "filters" | "extremes" | "exports" | "info" | "layers" | "more" | "search" | null;
type PlacePage = { items: PlaceSummary[]; total: number };
type DetailTarget = { level: Geography; id: string };

function ResultGroup({ title, items, metric, onChoose, onBrowse }: { title: string; items: PlaceSummary[]; metric: Metric; onChoose: (item: PlaceSummary) => void; onBrowse?: () => void }) {
  return <section><h3>{title}</h3>{items.length
    ? <><ol className="result-list">{items.map((item) => <li key={item.place_id}><button onClick={() => onChoose(item)}><span><strong>{item.name}</strong><small>{item.state} · {item.place_id}</small></span><b>{metricLabel(item, metric)}</b></button></li>)}</ol>{onBrowse && <button className="secondary browse-all" onClick={onBrowse}>Browse all</button>}</>
    : <p className="empty-copy">No ranked geographies</p>}</section>;
}

function Workspace({ meta, active, onCountyFit }: { meta: Meta; active: boolean; onCountyFit: () => void }) {
  const initial = useRef(readHash(window.location.hash)).current;
  const builtState = meta.build?.scope.kind === "state" ? meta.build.scope.state || "" : "";
  const initialState = builtState || initial.state;
  const initialPlace = !builtState || initial.place.startsWith(STATE_FIPS[builtState as keyof typeof STATE_FIPS] || "-")
    ? initial.place : "";
  const [level, setLevel] = useState<Geography>(initial.level);
  const [metric, setMetric] = useState<Metric>(initial.metric);
  const [renderedMetric, setRenderedMetric] = useState<Metric>(initial.metric);
  const [interactiveMetric, setInteractiveMetric] = useState<Metric | null>(null);
  const [filters, setFilters] = useState<MapFilters>({
    ...EMPTY_FILTERS,
    state: initialState,
    county: builtState && initial.state !== builtState ? "" : initial.county,
    showUnavailable: initial.unranked,
    mountainMagnitudeMin: initial.mountainMagnitudeMin,
    communityConditionsGroupMax: initial.communityConditionsGroupMax,
    costOfLivingIndexMax: initial.costOfLivingIndexMax,
    homeSqftFor1mMin: initial.homeSqftFor1mMin,
    housingBuilt2000PlusPctMin: initial.housingBuilt2000PlusPctMin,
  });
  const [draftFilters, setDraftFilters] = useState<MapFilters>(filters);
  const {
    state, county, showUnavailable: showUnranked, mountainMagnitudeMin,
    communityConditionsGroupMax, costOfLivingIndexMax, homeSqftFor1mMin,
    housingBuilt2000PlusPctMin,
  } = filters;
  const {
    state: draftState, county: draftCounty, showUnavailable: draftUnranked,
    mountainMagnitudeMin: draftMountainMagnitudeMin,
    communityConditionsGroupMax: draftCommunityConditionsGroupMax,
    costOfLivingIndexMax: draftCostOfLivingIndexMax,
    homeSqftFor1mMin: draftHomeSqftFor1mMin,
    housingBuilt2000PlusPctMin: draftHousingBuilt2000PlusPctMin,
  } = draftFilters;
  const [selected, setSelected] = useState(initialPlace);
  const [detailTarget, setDetailTarget] = useState<DetailTarget | null>(
    initialPlace ? { level: initial.level, id: initialPlace } : null,
  );
  const [detailRetry, setDetailRetry] = useState(0);
  const [scoreError, setScoreError] = useState("");
  const [scoreReloadNonce, setScoreReloadNonce] = useState(0);
  const [addonErrors, setAddonErrors] = useState<Partial<Record<MapScoreAddonKind, string>>>({});
  const [addonRetry, setAddonRetry] = useState<{ kind: MapScoreAddonKind; nonce: number } | null>(null);
  const [status, setStatus] = useState("Loading map");
  const [overlay, setOverlay] = useState<Overlay>(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [extremesError, setExtremesError] = useState("");
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
  const firstSemantic = useRef(active);
  const wasActive = useRef(active);
  const scopeHashNormalized = useRef(false);
  const restoringHistory = useRef(false);
  const layerMenu = useRef<HTMLDivElement | null>(null);
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
  const setFilter = <Key extends keyof MapFilters>(key: Key, value: MapFilters[Key]) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };
  const setDraftFilter = <Key extends keyof MapFilters>(key: Key, value: MapFilters[Key]) => {
    setDraftFilters((current) => ({ ...current, [key]: value }));
  };
  const mapVisibleCommit = useCallback((committedMetric: MapMetric) => {
    if (committedMetric === selectedMetric.current) setRenderedMetric(committedMetric);
  }, []);
  const mapInteractiveCommit = useCallback((committedMetric: MapMetric) => {
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
  const mapAddonFailed = useCallback((kind: MapScoreAddonKind, message: string) => {
    setAddonErrors((current) => ({ ...current, [kind]: message }));
  }, []);
  const mapAddonReady = useCallback((kind: MapScoreAddonKind) => {
    setAddonErrors((current) => {
      const next = { ...current };
      delete next[kind];
      return next;
    });
  }, []);
  const retryScores = useCallback(() => {
    setScoreError("");
    setInteractiveMetric(null);
    setStatus(`Loading ${level} scores`);
    setScoreReloadNonce((value) => value + 1);
  }, [level]);
  const retryAddon = useCallback((kind: MapScoreAddonKind) => {
    setAddonErrors((current) => {
      const next = { ...current };
      delete next[kind];
      return next;
    });
    setAddonRetry((current) => ({ kind, nonce: (current?.nonce ?? 0) + 1 }));
  }, []);

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
    if (overlay === "layers") {
      window.requestAnimationFrame(() => overlayEntry.current?.focus());
    } else if (previousOverlay.current === "more" && (overlay === "exports" || overlay === "info")) {
      window.requestAnimationFrame(() => overlayEntry.current?.focus());
    } else if (previousOverlay.current && !overlay && restoreOverlayFocus.current) {
      window.requestAnimationFrame(() => overlayTrigger.current?.focus());
    }
    previousOverlay.current = overlay;
    restoreOverlayFocus.current = true;
  }, [overlay]);

  useEffect(() => {
    if (overlay !== "layers") return;
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && layerMenu.current?.contains(event.target)) return;
      restoreOverlayFocus.current = false;
      setOverlay(null);
    };
    window.addEventListener("pointerdown", dismiss);
    return () => window.removeEventListener("pointerdown", dismiss);
  }, [overlay]);

  function dismissLayerMenuOnBlur() {
    if (overlay !== "layers") return;
    window.requestAnimationFrame(() => {
      if (layerMenu.current?.contains(document.activeElement)) return;
      restoreOverlayFocus.current = false;
      setOverlay((current) => current === "layers" ? null : current);
    });
  }

  function moveLayerFocus(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    const offset = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    if (!offset) return;
    event.preventDefault();
    const options = layerMenu.current?.querySelectorAll<HTMLButtonElement>(".layer-option");
    options?.[(index + offset + METRICS.length) % METRICS.length]?.focus();
  }

  const closeDetail = useCallback(() => {
    setSelected("");
    setDetailTarget(null);
    window.requestAnimationFrame(() => {
      (detailTrigger.current || document.querySelector<HTMLElement>(".risk-canvas"))?.focus();
    });
  }, []);

  const writeHash = useCallback((mode: "push" | "replace") => {
    if (!active) return;
    const params = new URLSearchParams({ level, metric });
    if (state) params.set("state", state);
    if (level === "tract" && county) params.set("county", county);
    if (selected) params.set("place", selected);
    if (showUnranked) params.set("unranked", "1");
    if (mountainMagnitudeMin !== null) params.set("mountain_magnitude_min", String(mountainMagnitudeMin));
    if (communityConditionsGroupMax !== null) params.set("max_community_conditions_group", String(communityConditionsGroupMax));
    if (costOfLivingIndexMax !== null) params.set("cost_of_living_index_max", String(costOfLivingIndexMax));
    if (homeSqftFor1mMin !== null) params.set("home_sqft_for_1m_min", String(homeSqftFor1mMin));
    if (housingBuilt2000PlusPctMin !== null) params.set("housing_built_2000_plus_pct_min", String(housingBuilt2000PlusPctMin));
    params.set("cx", camera.current.cx.toFixed(4)); params.set("cy", camera.current.cy.toFixed(4)); params.set("z", camera.current.z.toFixed(3));
    window.history[mode === "push" ? "pushState" : "replaceState"](null, "", `#${params}`);
  }, [active, communityConditionsGroupMax, costOfLivingIndexMax, county, homeSqftFor1mMin, housingBuilt2000PlusPctMin, level, metric, mountainMagnitudeMin, selected, showUnranked, state]);
  useEffect(() => {
    if (!active) {
      wasActive.current = false;
      return;
    }
    if (!wasActive.current) {
      wasActive.current = true;
      return;
    }
    if (firstSemantic.current) firstSemantic.current = false;
    else if (restoringHistory.current) restoringHistory.current = false;
    else writeHash("push");
  }, [active, writeHash]);
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
      if (!active || new URLSearchParams(window.location.hash.replace(/^#/, "")).get("workspace") === "county-fit") return;
      invalidateAddressLookup();
      const parsed = readHash(window.location.hash);
      const nextState = builtState || parsed.state;
      const nextPlace = !builtState || parsed.place.startsWith(STATE_FIPS[builtState as keyof typeof STATE_FIPS] || "-")
        ? parsed.place : "";
      const restoredFilters: MapFilters = {
        state: nextState,
        county: builtState && parsed.state !== builtState ? "" : parsed.county,
        showUnavailable: parsed.unranked,
        mountainMagnitudeMin: parsed.mountainMagnitudeMin,
        communityConditionsGroupMax: parsed.communityConditionsGroupMax,
        costOfLivingIndexMax: parsed.costOfLivingIndexMax,
        homeSqftFor1mMin: parsed.homeSqftFor1mMin,
        housingBuilt2000PlusPctMin: parsed.housingBuilt2000PlusPctMin,
      };
      restoringHistory.current = true;
      setLevel(parsed.level); setMetric(parsed.metric);
      setFilters(restoredFilters); setDraftFilters(restoredFilters);
      setSelected(nextPlace);
      setDetailTarget(nextPlace ? { level: parsed.level, id: nextPlace } : null);
      camera.current = parsed.camera;
      setCameraTarget({ ...parsed.camera, nonce: Date.now() });
      setOverlay(null); setPreview(null);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [active, builtState]);

  useEffect(() => {
    setDetail(null); setDetailError("");
    if (!detailTarget) return;
    let cancelled = false; setDetailLoading(true);
    const path = detailTarget.level === "county"
      ? `/api/v3/counties/${detailTarget.id}`
      : `/api/v3/places/${detailTarget.id}`;
    json<PlaceDetail>(path).then((value) => { if (!cancelled) { setDetail(value); setStatus(`${value.summary.name} selected`); } }).catch((caught) => { if (!cancelled) setDetailError((caught as Error).message); }).finally(() => { if (!cancelled) setDetailLoading(false); });
    return () => { cancelled = true; };
  }, [detailRetry, detailTarget]);

  useEffect(() => {
    const generation = ++countyGeneration.current;
    setCountyOptions([]);
    if (!draftState) return;
    const params = new URLSearchParams({ state: draftState, limit: "500", sort: "name", direction: "asc", include_unranked: "true" });
    void json<{ items: PlaceSummary[] }>(`/api/v3/counties?${params}`)
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
    toggleOverlay("extremes", trigger); setExtremesError(""); setLowest([]); setHighest([]); setLowestTotal(0); setHighestTotal(0); setBrowseGroup(null); setExtremesLoading(true);
    const community = metric === "community-conditions";
    const sort = community ? "community_conditions_group"
      : metric === "mountain" ? "mountain_magnitude"
        : metric === "cost-of-living" ? "cost_of_living_index"
          : metric === "home-costs" ? "home_sqft_for_1m" : "res_hazard_npctl";
    const base = new URLSearchParams({ limit: community ? "1" : "5", sort });
    appendFilterParams(base, filters, community ? "county" : level);
    if (community) base.set("include_unranked", "true");
    if (metric === "mountain" && mountainMagnitudeMin === null) {
      base.set("mountain_magnitude_min", "0");
    }
    if (metric === "cost-of-living") base.set("cost_of_living_index_min", "0");
    if (metric === "home-costs" && homeSqftFor1mMin === null) {
      base.set("home_sqft_for_1m_min", "0");
    }
    const path = community ? "/api/v3/counties" : level === "tract" ? "/api/v3/places" : "/api/v3/counties";
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
          appendFilterParams(params, filters, "county");
          return json<PlacePage>(`/api/v3/counties?${params}`);
        };
        const [lowSample, highSample] = await Promise.all([samples(lowGroup), samples(highGroup)]);
        if (generation === extremesGeneration.current) { setLowest(lowSample.items); setHighest(highSample.items); setLowestTotal(lowSample.total); setHighestTotal(highSample.total); }
      }
    } catch (caught) {
      if (generation === extremesGeneration.current) setExtremesError((caught as Error).message);
    } finally {
      if (generation === extremesGeneration.current) setExtremesLoading(false);
    }
  }
  function choose(place: PlaceSummary) { detailTrigger.current = overlayTrigger.current; restoreOverlayFocus.current = false; const targetLevel = metric === "community-conditions" ? "county" : level; setSelected(targetLevel === level ? place.place_id : ""); setDetailTarget({ level: targetLevel, id: place.place_id }); setFocusTarget(targetLevel === level ? { kind: "place", id: place.place_id, nonce: Date.now() } : null); setOverlay(null); setQuery(""); }
  function switchLevel(next: Geography) { invalidateAddressLookup(); setLevel(next); setFilter("county", ""); setDraftFilter("county", ""); setSelected(""); setDetailTarget(null); setDetail(null); setFocusTarget(null); setOverlay(null); setPreview(null); setScoreError(""); setAddonErrors({}); setAddonRetry(null); setInteractiveMetric(null); }
  function switchMetric(next: Metric) {
    if (next === metric) { setOverlay(null); return; }
    setMetric(next); setFilter("showUnavailable", false); setDraftFilter("showUnavailable", false); setPreview(null); setOverlay(null);
  }

  async function browseCommunity(group: number, offset = 0) {
    const generation = ++extremesGeneration.current;
    const params = new URLSearchParams({ limit: "50", offset: String(offset), sort: "name", direction: "asc", include_unranked: "true", community_conditions_group: String(group) });
    appendFilterParams(params, filters, "county");
    setExtremesLoading(true); setExtremesError("");
    try {
      const page = await json<PlacePage>(`/api/v3/counties?${params}`);
      if (generation !== extremesGeneration.current) return;
      setBrowseGroup(group); setBrowseItems(page.items); setBrowseTotal(page.total); setBrowseOffset(offset);
    } catch (caught) {
      if (generation === extremesGeneration.current) setExtremesError((caught as Error).message);
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
      const result = await json<LookupResult>("/api/v3/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address }) });
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
      const result = await json<LookupResult>("/api/v3/lookup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address, candidate_id: candidateId }) });
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
    setLevel("tract");
    setFilters((current) => ({ ...current, state: summary.state, county: summary.county_fips }));
    setDraftFilters((current) => ({ ...current, state: summary.state, county: summary.county_fips }));
    setDetail(result.detail); setSelected(result.tract_id); setDetailTarget({ level: "tract", id: result.tract_id });
    setFocusTarget({ kind: "place", id: result.tract_id, nonce: Date.now() });
    setConfirmation(null); setOverlay(null); setQuery("");
    setStatus(`Matched ${summary.name}; showing tract detail`);
  }
  function applyFilters() {
    const nextState = builtState || draftState;
    setFilters({ ...draftFilters, state: nextState, county: level === "tract" ? draftCounty : "" }); setSelected(""); setDetailTarget(null); setOverlay(null);
    if (draftCounty) setFocusTarget({ kind: "county", id: draftCounty, nonce: Date.now() }); else if (nextState) setFocusTarget({ kind: "state", id: nextState, nonce: Date.now() }); else setFocusTarget(null);
    setStatus(nextState ? `Filter applied: ${nextState}${draftCounty ? ` · ${draftCounty}` : ""}` : "National filter applied");
  }
  function clearFilters() { const cleared = { ...EMPTY_FILTERS, state: builtState }; setDraftFilters(cleared); setFilters(cleared); setFocusTarget(builtState ? { kind: "state", id: builtState, nonce: Date.now() } : null); setSelected(""); setDetailTarget(null); setOverlay(null); }

  const tooltipScore = metric === "mountain" ? preview?.score?.mountain_magnitude : preview?.score?.res_hazard_npctl;
  const tooltipBand = scoreBand(tooltipScore ?? null);
  const tooltipClass = preview ? mapTooltipClass(preview.x, preview.y, window.innerWidth, window.innerHeight) : "";
  const activeFilter = Boolean(
    state || county || showUnranked || mountainMagnitudeMin !== null
    || communityConditionsGroupMax !== null || costOfLivingIndexMax !== null
    || homeSqftFor1mMin !== null || housingBuilt2000PlusPctMin !== null,
  );
  const moreOpen = overlay === "more" || overlay === "exports" || overlay === "info";
  const moreControls = overlay === "exports" ? "exports-panel" : overlay === "info" ? "info-panel" : "more-panel";
  const metricName = METRIC_UI[metric].name;
  const activeLayer = (meta.layers ?? []).find((layer) =>
    layer.key === METRIC_UI[metric].descriptorKey);
  const renderedLayer = (meta.layers ?? []).find((layer) =>
    layer.key === METRIC_UI[renderedMetric].descriptorKey);
  const sources = meta.build?.sources ?? [];
  const homeSource = sources.find((source) => source.source === "home_market");
  const requiredAddonErrors = requestedMapAddons(metric, filters)
    .filter((kind) => Boolean(addonErrors[kind]));
  const mapUpdating = !scoreError && (renderedMetric !== metric || interactiveMetric !== metric);
  const lowestGroup = lowest[0]?.community_conditions_group ?? null;
  const highestGroup = highest[0]?.community_conditions_group ?? null;
  const noCommunityGroups = metric === "community-conditions" && lowestGroup === null && highestGroup === null;
  const extremeButtonLabel = metric === "community-conditions" ? "Best / Worst"
    : metric === "home-costs" ? "Most / Least" : "Lowest / Highest";
  const extremesLabel = metric === "residential-hazard" ? "Lowest and highest Residential Hazard Exposure"
    : metric === "mountain" ? "Lowest and highest Mountain Magnitude"
      : metric === "community-conditions" ? "Best and worst Community Conditions"
        : metric === "cost-of-living" ? "Lowest and highest Cost of Living RPP"
          : "Most and least square feet for $1M";
  const lowTitle = metric === "cost-of-living" ? "Lowest RPP"
    : metric === "home-costs" ? "Least square feet for $1M" : "Lowest";
  const highTitle = metric === "cost-of-living" ? "Highest RPP"
    : metric === "home-costs" ? "Most square feet for $1M" : "Highest";
  const unavailableLabel = metric === "residential-hazard" ? "hazard-unranked"
    : metric === "community-conditions" ? "Community-ungrouped"
      : metric === "mountain" ? "Mountain-unavailable"
        : metric === "cost-of-living" ? "Cost-of-Living-unavailable"
          : "Home-Costs-unavailable";
  const tooltipValue = !preview?.score ? `${metricName} unavailable`
    : metric === "residential-hazard" ? preview.score.res_hazard_npctl === null ? "Not ranked / unavailable" : `${preview.score.res_hazard_npctl.toFixed(1)} · ${tooltipBand ? SCORE_BAND_LABELS[tooltipBand] : ""}`
      : metric === "mountain" ? preview.score.mountain_magnitude === null ? "Mountain Magnitude unavailable" : `M${preview.score.mountain_magnitude.toFixed(2)} · ${mountainRarityLabel(preview.score.mountain_magnitude, level)}`
        : metric === "community-conditions" ? preview.score.community_conditions_group === null ? "Not grouped · County-level" : `Group ${preview.score.community_conditions_group} of 10 · County-level`
          : metric === "cost-of-living" ? preview.score.cost_of_living_index === null ? "Cost of Living unavailable" : `${preview.score.cost_of_living_index.toFixed(1)} RPP · County assignment`
            : preview.score.home_sqft_for_1m === null ? "Home Costs unavailable" : `${number.format(preview.score.home_sqft_for_1m)} sq ft / $1M · ${preview.score.home_buying_power_percentile?.toFixed(1) ?? "–"}th pct`;
  return <main className="map-shell">
    <RiskMap active={active} manifestUrl={meta.map_assets.manifest_url} scoreUrl={`/api/v3/map/scores/core?level=${level}`} expectedBuildId={meta.build?.build_id || ""} level={level} metric={metric} displayMetric={renderedMetric} busy={mapUpdating} selected={selected} filters={filters} neutralOnly={Boolean(scoreError)} retryGeneration={scoreReloadNonce} addonRetry={addonRetry} focusTarget={focusTarget} cameraTarget={cameraTarget} initialCamera={initial.camera} onSelect={selectFromMap} onPreview={setPreview} onCamera={cameraChanged} onStatus={setStatus} onScoresReady={mapScoresReady} onScoreError={mapScoreFailed} onAddonError={mapAddonFailed} onAddonReady={mapAddonReady} onVisibleCommit={mapVisibleCommit} onInteractiveCommit={mapInteractiveCommit} />
    <header className={`top-dock${overlay ? " overlay-open" : ""}`}>
      <div className="brand"><strong>HouseHunter</strong><span>{METRIC_UI[metric].source}</span></div>
      <div className="workspace-toggle" aria-label="Workspace"><button aria-pressed="true">Map</button><button onClick={onCountyFit}>County Fit</button></div>
      <div className="level-toggle" aria-label="Geography level"><button aria-pressed={level === "tract"} onClick={() => switchLevel("tract")}>Tracts</button><button aria-pressed={level === "county"} onClick={() => switchLevel("county")}>Counties</button></div>
      <div className="layer-menu" ref={layerMenu} onBlur={dismissLayerMenuOnBlur}>
        <span className="layer-label">Layer</span>
        <button type="button" className="layer-trigger" aria-label={`Map layer: ${METRIC_UI[metric].name} — ${METRIC_UI[metric].source}`} aria-expanded={overlay === "layers"} aria-controls="layer-options" onClick={(event) => toggleOverlay("layers", event.currentTarget)}>
          <span className="layer-value">{METRIC_UI[metric].name} — {METRIC_UI[metric].source}</span><span className="layer-chevron" aria-hidden="true">⌄</span>
        </button>
        {overlay === "layers" && <div id="layer-options" className="layer-options" role="group" aria-label="Map layers">{METRICS.map((item, index) => <button type="button" className="layer-option" key={item} aria-label={`${METRIC_UI[item].name} — ${METRIC_UI[item].source}`} aria-pressed={item === metric} ref={item === metric ? (node) => { overlayEntry.current = node; } : undefined} onKeyDown={(event) => moveLayerFocus(event, index)} onClick={() => switchMetric(item)}><strong>{METRIC_UI[item].name}</strong><small>{METRIC_UI[item].source}</small></button>)}</div>}
      </div>
      <div className="dock-actions"><button aria-expanded={overlay === "search"} aria-controls="search-panel" onClick={(event) => toggleOverlay("search", event.currentTarget)}>Search</button><button aria-expanded={overlay === "extremes"} aria-controls="extremes-panel" onClick={(event) => void openExtremes(event.currentTarget)}>{extremeButtonLabel}</button><button aria-expanded={overlay === "filters"} aria-controls="filters-panel" className={activeFilter ? "active" : ""} onClick={(event) => toggleOverlay("filters", event.currentTarget)}>Filters{activeFilter ? " · On" : ""}</button><button className="desktop-dock-action" aria-expanded={overlay === "exports"} aria-controls="exports-panel" onClick={(event) => toggleOverlay("exports", event.currentTarget)}>Exports</button><button className="desktop-dock-action" aria-expanded={overlay === "info"} aria-controls="info-panel" aria-label="Information" onClick={(event) => toggleOverlay("info", event.currentTarget)}>ⓘ</button><button className={`mobile-more${moreOpen ? " active" : ""}`} aria-expanded={moreOpen} aria-controls={moreControls} onClick={(event) => { if (moreOpen) { overlayTrigger.current = event.currentTarget; setOverlay(null); } else toggleOverlay("more", event.currentTarget); }}>More</button></div>
      <div className={`build-pill ${scoreError ? "failed" : ""}`} title={meta.build?.build_id}>{scoreError ? "Score error" : status}</div>
    </header>
    {overlay === "search" && <section id="search-panel" className="floating-panel search-panel" aria-label="Search"><form onSubmit={lookupAddress} aria-busy={searching}><label>Street address<input autoFocus autoComplete="street-address" autoCapitalize="words" enterKeyHint="search" value={query} onChange={(event) => { invalidateAddressLookup(); setSearchError(""); setQuery(event.target.value); }} placeholder="Street, city, state, ZIP" /></label><p className="privacy-note">Find tract contacts the US Census geocoder through this loopback server and may send it to OpenStreetMap only after a valid Census no-match. Addresses are not written to disk. Do not submit confidential addresses. The result shows tract context, never a property marker or property-specific score.</p><button className="primary" disabled={searching || !query.trim()}>{searching ? "Looking…" : "Find tract"}</button>{confirmation && <div className="confirm-card" role="region" aria-label="Approximate street match"><strong>Confirm approximate street match</strong><p>{confirmation.message}</p>{confirmation.candidates.map((candidate) => <button type="button" className="secondary" key={candidate.candidate_id} onClick={() => void confirmAddress(candidate.candidate_id)}>Use approximate street location: {candidate.matched_address}</button>)}<small>{confirmation.attribution}</small></div>}</form>{searchError && <p role="alert" className="error">{searchError}</p>}</section>}
    {overlay === "filters" && <section id="filters-panel" className="floating-panel filters-panel" aria-label="Map filters"><h2>Filters</h2><label>State<select value={draftState} disabled={Boolean(builtState)} onChange={(event) => setDraftFilters((current) => ({ ...current, state: event.target.value, county: "" }))}><option value="">All states & territories</option>{STATE_ABBREVIATIONS.map((item) => <option key={item}>{item}</option>)}</select></label>{level === "tract" && <label>County<select value={draftCounty} disabled={!draftState} onChange={(event) => setDraftFilter("county", event.target.value)}><option value="">All counties</option>{countyOptions.map((item) => <option value={item.place_id} key={item.place_id}>{item.name}</option>)}</select></label>}<label>Minimum Mountain Magnitude<input type="number" min="0" step="0.01" value={draftMountainMagnitudeMin ?? ""} onChange={(event) => { const value = Number(event.target.value); setDraftFilter("mountainMagnitudeMin", event.target.value === "" ? null : Number.isFinite(value) && value >= 0 ? value : null); }} placeholder="Any" /></label><label>Maximum Community Conditions group<input type="number" min="1" max="10" step="1" value={draftCommunityConditionsGroupMax ?? ""} onChange={(event) => { const value = Number(event.target.value); setDraftFilter("communityConditionsGroupMax", event.target.value === "" ? null : Number.isInteger(value) && value >= 1 && value <= 10 ? value : null); }} placeholder="Any" /></label><label>Maximum Cost of Living RPP<input type="number" min="0" step="0.1" value={draftCostOfLivingIndexMax ?? ""} onChange={(event) => { const value = Number(event.target.value); setDraftFilter("costOfLivingIndexMax", event.target.value === "" ? null : Number.isFinite(value) && value >= 0 ? value : null); }} placeholder="Any" /></label><label>Minimum square feet for $1M<input type="number" min="0" step="1" value={draftHomeSqftFor1mMin ?? ""} onChange={(event) => { const value = Number(event.target.value); setDraftFilter("homeSqftFor1mMin", event.target.value === "" ? null : Number.isFinite(value) && value >= 0 ? value : null); }} placeholder="Any" /></label><label>Minimum built 2000+ share (%)<input type="number" min="0" max="100" step="1" value={draftHousingBuilt2000PlusPctMin ?? ""} onChange={(event) => { const value = Number(event.target.value); setDraftFilter("housingBuilt2000PlusPctMin", event.target.value === "" ? null : Number.isFinite(value) && value >= 0 && value <= 100 ? value : null); }} placeholder="Any" /></label><label className="check"><input type="checkbox" checked={draftUnranked} onChange={(event) => setDraftFilter("showUnavailable", event.target.checked)} />Show {unavailableLabel} geographies</label><p className="filter-note">Explicit metric thresholds always exclude unavailable values, even when “show unavailable” is on.</p><div className="panel-buttons"><button className="primary" onClick={applyFilters}>Apply</button><button className="secondary" onClick={clearFilters}>Clear</button></div></section>}
    {overlay === "extremes" && <section id="extremes-panel" className="floating-panel extremes-panel" aria-label={extremesLabel} aria-busy={extremesLoading}><h2>Explore the range</h2>{browseGroup !== null ? <><button className="secondary" onClick={closeCommunityBrowse}>← Back to groups</button><ResultGroup title={`Group ${browseGroup} · ${number.format(browseTotal)} counties`} items={browseItems} metric={metric} onChoose={choose} /><div className="pager"><button className="secondary" disabled={browseOffset === 0} onClick={() => void browseCommunity(browseGroup, Math.max(0, browseOffset - 50))}>Previous</button><span>{number.format(browseOffset + 1)}–{number.format(Math.min(browseOffset + browseItems.length, browseTotal))} of {number.format(browseTotal)}</span><button className="secondary" disabled={browseOffset + 50 >= browseTotal} onClick={() => void browseCommunity(browseGroup, browseOffset + 50)}>Next</button></div></> : extremesLoading ? <p className="loading-copy" role="status">{metric === "community-conditions" ? "Loading best and worst groups…" : `Loading ${extremesLabel.toLowerCase()}…`}</p> : noCommunityGroups ? <p>No grouped counties in this scope.</p> : <div><ResultGroup title={metric === "community-conditions" ? `Best present · Group ${lowestGroup} · ${number.format(lowestTotal)} counties` : metric === "home-costs" ? highTitle : lowTitle} items={metric === "home-costs" ? highest : lowest} metric={metric} onChoose={choose} onBrowse={metric === "community-conditions" && lowestGroup !== null ? () => void browseCommunity(lowestGroup) : undefined} /><ResultGroup title={metric === "community-conditions" ? `Worst present · Group ${highestGroup} · ${number.format(highestTotal)} counties` : metric === "home-costs" ? lowTitle : highTitle} items={metric === "home-costs" ? lowest : highest} metric={metric} onChoose={choose} onBrowse={metric === "community-conditions" && highestGroup !== null ? () => void browseCommunity(highestGroup) : undefined} /></div>}{extremesError && <p role="alert" className="error">{extremesError}</p>}</section>}
    {overlay === "more" && <section id="more-panel" className="floating-panel more-panel" aria-label="More actions"><h2>More</h2><button className="secondary" onClick={() => setOverlay("exports")}>Export snapshot</button><button className="secondary" onClick={() => setOverlay("info")}>About this map</button></section>}
    {overlay === "exports" && <nav id="exports-panel" className="floating-panel export-panel" aria-label="Exports"><h2>Export snapshot</h2><a ref={(node) => { overlayEntry.current = node; }} href="/api/v3/exports/places.csv" download>Tracts CSV</a><a href="/api/v3/exports/places.parquet" download>Tracts Parquet</a><a href="/api/v3/exports/counties.csv" download>Counties CSV</a><a href="/api/v3/exports/counties.parquet" download>Counties Parquet</a></nav>}
    {overlay === "info" && <section id="info-panel" className="floating-panel info-panel" aria-label="About this map"><h2 ref={(node) => { overlayEntry.current = node; }} tabIndex={-1}>About this map</h2><p>HouseHunter keeps five independent dimensions. Filters can be combined, but this map never blends dimensions into one score. The optional <code>househunter top-counties</code> command is a named preference model, not a map score.</p><p>Residential Hazard Exposure is a HouseHunter composite of FEMA building-specific natural-hazard loss rates. Higher values indicate greater exposure to one or more significant hazards, with extra weight on the most elevated hazards. Tracts and counties use separate national percentile universes; county values are never tract averages. FEMA ALR_NPCTL remains available only as supporting detail.</p><p>{COMMUNITY_EXPLANATION} Tract colors inherit their county group and never imply tract-level resolution.</p><p>{mountainExplanation(level)}</p><p>Cost of Living uses BEA all-items Regional Price Parities (U.S. = 100). Tracts inherit their county’s metropolitan or U.S. nonmetropolitan assignment; lower is better.</p><p>Home Costs estimates square feet purchasable for $1M from county asking-market indicators. It is not a sale price, valuation, ownership-cost estimate, or promise that a matching home is listed. Housing-age context is an ACS 2024 five-year estimate.</p>{activeLayer && <p className={`layer-source ${activeLayer.availability}`}><strong>Active layer: {activeLayer.display_name}</strong> · {activeLayer.source} · {activeLayer.vintage} · {activeLayer.geography}. {activeLayer.attribution} {activeLayer.notice}</p>}{(meta.layers ?? []).filter((layer) => layer.availability !== "available").map((layer) => <p className="notice" key={layer.key}><strong>{layer.display_name} unavailable.</strong> {layer.notice}</p>)}{homeSource?.stale && <p className="notice"><strong>Home Costs market release is stale.</strong> The locally imported {homeSource.release ?? homeSource.version} release is older than 62 days.</p>}<p>To enable an approved local Home Costs release, run <code>househunter import-home-market FILE --acknowledge-personal-use</code>. Realtor.com source and derived market data remain local and are for the owner-selected personal/private-use workflow only.</p><p>HouseHunter is local-only and uses no basemap, telemetry, account, or hosted database.</p></section>}
    {preview && <div className={tooltipClass} style={{ left: preview.x, top: preview.y }}><strong>{preview.name}</strong><span>{preview.state} · {preview.placeId}</span><b>{tooltipValue}</b></div>}
    {scoreError && <section className="recovery-card" role="alert"><strong>Scores could not be loaded</strong><span>{scoreError}</span><button className="secondary" onClick={retryScores}>Retry scores</button></section>}
    {!scoreError && requiredAddonErrors.length > 0 && <div className="recovery-stack">{requiredAddonErrors.map((kind) => {
      const name = kind === "cost-of-living" ? "Cost of Living" : "Home Costs";
      return <section className="recovery-card" role="alert" key={kind}><strong>{name} map data could not be loaded</strong><span>{addonErrors[kind]}</span><button className="secondary" onClick={() => retryAddon(kind)}>Retry {name}</button></section>;
    })}</div>}
    {mapUpdating && <div className="map-updating" aria-hidden="true">{renderedMetric !== metric ? `Updating ${metricName} map…` : `Preparing ${metricName} interaction…`}</div>}
    <MetricLegend metric={renderedMetric} level={level} descriptor={renderedLayer} />
    {detailTarget && <DetailDrawer detail={detail} loading={detailLoading} error={detailError} level={detailTarget.level} metric={metric} sources={sources} onClose={closeDetail} onRetry={() => setDetailRetry((value) => value + 1)} onViewTracts={(countyFips, nextState) => { setLevel("tract"); setFilters((current) => ({ ...current, state: nextState, county: countyFips })); setDraftFilters((current) => ({ ...current, state: nextState, county: countyFips })); setSelected(""); setDetailTarget(null); setFocusTarget({ kind: "county", id: countyFips, nonce: Date.now() }); }} onViewCounty={(countyFips) => { setLevel("county"); setFilter("county", ""); setDraftFilter("county", ""); setSelected(countyFips); setDetailTarget({ level: "county", id: countyFips }); setFocusTarget({ kind: "place", id: countyFips, nonce: Date.now() }); }} />}
    <p className="sr-only" aria-live="polite">{status}</p>
  </main>;
}

function App() {
  const [meta, setMeta] = useState<Meta | null>(null); const [error, setError] = useState("");
  const [workspace, setWorkspace] = useState<"map" | "county-fit">(() => readCountyFitHash(window.location.hash) ? "county-fit" : "map");
  const [countyFitOpened, setCountyFitOpened] = useState(workspace === "county-fit");
  const load = useCallback(() => { setError(""); void json<Meta>("/api/v3/meta").then(setMeta).catch((caught) => setError((caught as Error).message)); }, []);
  useEffect(load, [load]);
  useEffect(() => {
    const restoreWorkspace = () => {
      const next = readCountyFitHash(window.location.hash) ? "county-fit" : "map";
      setWorkspace(next);
      if (next === "county-fit") setCountyFitOpened(true);
    };
    window.addEventListener("popstate", restoreWorkspace);
    return () => window.removeEventListener("popstate", restoreWorkspace);
  }, []);
  if (!meta) return <main className="map-shell boot"><div className="boot-copy" role={error ? "alert" : "status"}>{error || "Opening HouseHunter map…"}{error && <button onClick={load}>Retry</button>}</div></main>;
  const mapAssets = meta.map_assets || { ready: false, error: "Map asset status is missing", schema_version: null, release: null, manifest_url: "/map-assets/manifest.json" };
  const currentCamera = readHash(window.location.hash).camera;
  if (!mapAssets.ready) return <main className="map-shell"><RiskMap manifestUrl={mapAssets.manifest_url} scoreUrl="/api/v3/map/scores?level=tract" expectedBuildId="" level="tract" selected="" filters={EMPTY_FILTERS} neutralOnly focusTarget={null} initialCamera={currentCamera} onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined} /><div className="asset-repair" role="alert"><h1>Map boundaries need repair</h1><p>{mapAssets.error}</p><p>Run <code>uv run python scripts/generate_map_assets.py</code> from the HouseHunter checkout, then restart the app.</p></div></main>;
  const readyMeta = { ...meta, map_assets: mapAssets };
  if (!meta.build) return <main className="map-shell"><RiskMap manifestUrl={mapAssets.manifest_url} scoreUrl="/api/v3/map/scores?level=tract" expectedBuildId="" level="tract" selected="" filters={EMPTY_FILTERS} neutralOnly focusTarget={null} initialCamera={currentCamera} onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined} /><header className="top-dock"><div className="brand"><strong>HouseHunter</strong><span>Residential Hazard Exposure</span></div><div className="build-pill">Setup required</div></header><Setup token={meta.mutation_token} onReady={load} /><div className="legend"><p><strong>Residential Hazard Exposure</strong> · higher is worse · not property-level risk</p></div></main>;
  const openCountyFit = () => {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    if (countyFitOpened && params.has("fit_view")) {
      params.set("workspace", "county-fit");
      window.history.pushState(null, "", `#${params}`);
    } else {
      const mapState = readHash(window.location.hash);
      writeCountyFitHash("push", {
        view: meta.county_fit.readiness === "ready" ? "custom" : "safety",
        preset: "balanced",
        weights: { ...COUNTY_FIT_PRESETS.balanced },
        filters: { ...EMPTY_COUNTY_FIT_FILTERS },
        selected: "",
        camera: mapState.camera,
      });
    }
    setCountyFitOpened(true);
    setWorkspace("county-fit");
  };
  const openMap = () => {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    params.delete("workspace");
    window.history.pushState(null, "", `#${params}`);
    setWorkspace("map");
  };
  return <>
    <div className={`workspace-surface${workspace === "map" ? " active" : " inactive"}`} aria-hidden={workspace !== "map"}><Workspace meta={readyMeta} active={workspace === "map"} onCountyFit={openCountyFit} /></div>
    {countyFitOpened && <div className={`workspace-surface${workspace === "county-fit" ? " active" : " inactive"}`} aria-hidden={workspace !== "county-fit"}><CountyFit meta={readyMeta} active={workspace === "county-fit"} onMap={openMap} /></div>}
  </>;
}

export default App;
