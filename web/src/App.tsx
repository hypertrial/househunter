import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import type { HazardPercentile, JobStatus, PlaceDetail, PlaceSummary } from "./types";

interface Meta {
  app_version: string;
  mutation_token: string;
  reference_assets_ready: boolean;
  reference_assets_error: string | null;
  build: null | {
    build_id: string;
    place_count: number;
    ranked_place_count: number;
    source_vintages: Record<string, string>;
    scope: { kind: string; state: string | null };
  };
}

type Geography = "tract" | "county";

const number = new Intl.NumberFormat("en-US");

export const STATE_ABBREVIATIONS = [
  "AK", "AL", "AR", "AS", "AZ", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "GU",
  "HI", "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
  "MO", "MP", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH",
  "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VI", "VT",
  "WA", "WI", "WV", "WY",
] as const;

async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Request failed");
  return body as T;
}

export function scoreLabel(place: PlaceSummary): string {
  return place.risk_score === null ? "Not ranked" : place.risk_score.toFixed(1);
}

export function sortedHazardPercentiles(hazards: HazardPercentile[]): HazardPercentile[] {
  return [...hazards].sort((left, right) => {
    if (left.percentile === null && right.percentile === null) {
      return left.label.localeCompare(right.label);
    }
    if (left.percentile === null) return 1;
    if (right.percentile === null) return -1;
    if (right.percentile !== left.percentile) return right.percentile - left.percentile;
    return left.label.localeCompare(right.label);
  });
}

function Setup({ token, onReady }: { token: string; onReady: () => void }) {
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const latest = await json<JobStatus>(`/api/v1/jobs/${job.job_id}`);
        setJob(latest);
        if (latest.state === "succeeded") onReady();
        if (latest.state === "failed") setError(latest.error || "Preparation failed");
      } catch (caught) {
        setError((caught as Error).message);
      }
    }, 600);
    return () => window.clearInterval(timer);
  }, [job, onReady]);

  async function prepare() {
    setError(null);
    try {
      setJob(await json<JobStatus>("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-HouseHunter-Token": token },
        body: JSON.stringify({ kind: "prepare" }),
      }));
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  async function cancel() {
    if (!job) return;
    try {
      setJob(await json<JobStatus>(`/api/v1/jobs/${job.job_id}`, {
        method: "DELETE", headers: { "X-HouseHunter-Token": token },
      }));
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  return <main className="setup">
    <p className="eyebrow">Local data preparation</p>
    <h1>Rank FEMA tracts with one clear risk signal.</h1>
    <p className="lead">HouseHunter downloads the pinned FEMA National Risk Index tract and county layers and builds a private snapshot on this Mac. No Census download is required.</p>
    {job && <section aria-live="polite" className="progress-card">
      <div className="progress-copy"><strong>{job.message}</strong><span>{job.progress}%</span></div>
      <progress max="100" value={job.progress}>{job.progress}%</progress>
      {["queued", "running"].includes(job.state) && <button className="secondary" onClick={cancel}>Cancel</button>}
    </section>}
    {error && <p role="alert" className="error">{error}</p>}
    {(!job || ["failed", "cancelled"].includes(job.state)) && <button className="primary" onClick={prepare}>{job ? "Try preparation again" : "Prepare national data"}</button>}
    <p className="fine">FEMA NRI December 2025 v1.20</p>
  </main>;
}

function Detail({
  placeId,
  level,
  onClose,
  onViewTracts,
  onViewCounty,
}: {
  placeId: string;
  level: Geography;
  onClose: () => void;
  onViewTracts?: (countyFips: string, state: string) => void;
  onViewCounty?: (countyFips: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [detail, setDetail] = useState<PlaceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    const path = level === "county" ? `/api/v1/counties/${placeId}` : `/api/v1/places/${placeId}`;
    json<PlaceDetail>(path)
      .then((result) => {
        if (!cancelled) setDetail(result);
      })
      .catch((caught) => {
        if (!cancelled) setError((caught as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [placeId, level]);
  useEffect(() => {
    const node = dialog.current;
    if (node && !node.open) node.showModal();
    return () => {
      if (node?.open) node.close();
    };
  }, []);
  const closeLabel = level === "county" ? "Close county detail" : "Close tract detail";
  const knownCounty = Boolean(detail && /^\d{5}$/.test(detail.summary.county_fips));
  return <dialog ref={dialog} className="detail" aria-label={level === "county" ? "County detail" : "Tract detail"} onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <button autoFocus className="close" onClick={onClose} aria-label={closeLabel}>×</button>
    {error && <p role="alert" className="error">{error}</p>}
    {!detail ? <p>Loading…</p> : <>
      <p className="eyebrow">{detail.summary.place_type} · {detail.summary.place_id}</p>
      <h2>{detail.summary.name}, {detail.summary.state}</h2>
      <div className="score"><span>{scoreLabel(detail.summary)}</span><small>{level === "county" ? "FEMA county ALR_NPCTL" : "FEMA ALR_NPCTL"}<br />Lower is better</small></div>
      <dl className="facts">
        <div><dt>{level === "county" ? "County FIPS" : "Tract FIPS"}</dt><dd>{detail.summary.place_id}</dd></div>
        <div><dt>State</dt><dd>{detail.summary.state}</dd></div>
        {level === "tract" && <div><dt>County</dt><dd>{detail.summary.county_name}</dd></div>}
        <div><dt>FEMA vintage</dt><dd>{detail.summary.fema_vintage}</dd></div>
      </dl>
      {level === "county" && onViewTracts && <button className="secondary" type="button" onClick={() => onViewTracts(detail.summary.place_id, detail.summary.state)}>View {number.format(detail.member_tract_count ?? 0)} tracts</button>}
      {level === "tract" && knownCounty && onViewCounty && <button className="secondary" type="button" onClick={() => onViewCounty(detail.summary.county_fips)}>View {detail.summary.county_name} county</button>}
      <h3>Published hazard percentiles</h3>
      <div className="contributions">
        {sortedHazardPercentiles(detail.hazard_percentiles).map((hazard) => <div key={hazard.code} className="contribution">
          <div><span>{hazard.label}</span><span>{hazard.percentile === null ? "No rating" : hazard.percentile.toFixed(1)}</span></div>
          <div className="bar"><i style={{ width: `${hazard.percentile ?? 0}%` }} /></div>
        </div>)}
      </div>
      <p className="notice">{detail.methodology_notice}</p>
    </>}
  </dialog>;
}

function Rankings({ meta }: { meta: Meta }) {
  const [places, setPlaces] = useState<PlaceSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [state, setState] = useState("");
  const [county, setCounty] = useState("");
  const [countyOptions, setCountyOptions] = useState<PlaceSummary[]>([]);
  const [level, setLevel] = useState<Geography>("tract");
  const [includeUnranked, setIncludeUnranked] = useState(false);
  const [sort, setSort] = useState("risk_score");
  const [direction, setDirection] = useState("asc");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setCountyOptions([]);
    if (!state) return;
    const params = new URLSearchParams({ state, limit: "500", sort: "name", direction: "asc" });
    json<{ items: PlaceSummary[] }>(`/api/v1/counties?${params}`)
      .then((result) => setCountyOptions(result.items))
      .catch((caught) => setError((caught as Error).message));
  }, [state]);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: "50", offset: String(offset), sort, direction });
    if (search) params.set("search", search);
    if (state) params.set("state", state);
    if (includeUnranked) params.set("include_unranked", "true");
    if (level === "tract" && county) params.set("county", county);
    const path = level === "county" ? "/api/v1/counties" : "/api/v1/places";
    try {
      const result = await json<{ items: PlaceSummary[]; total: number }>(`${path}?${params}`);
      setPlaces(result.items); setTotal(result.total); setError(null);
    } catch (caught) { setError((caught as Error).message); }
  }, [county, direction, includeUnranked, level, offset, search, sort, state]);

  useEffect(() => { void load(); }, [load]);
  function submit(event: FormEvent) { event.preventDefault(); setOffset(0); void load(); }
  function changeSort(value: string) {
    if (sort === value) setDirection(direction === "asc" ? "desc" : "asc");
    else { setSort(value); setDirection("asc"); }
    setOffset(0);
  }
  function changeLevel(next: Geography) {
    setLevel(next);
    setSelected(null);
    setOffset(0);
    if (next === "county") setCounty("");
  }
  function changeState(next: string) {
    setState(next);
    setCounty("");
    setOffset(0);
  }
  function viewTracts(countyFips: string, stateAbbr: string) {
    setLevel("tract");
    setState(stateAbbr);
    setCounty(countyFips);
    setSelected(null);
    setOffset(0);
  }
  function viewCounty(countyFips: string) {
    setLevel("county");
    setCounty("");
    setSelected(countyFips);
    setOffset(0);
  }
  const scopeKind = meta.build?.scope.kind === "state" ? `${meta.build.scope.state} ` : "";
  const noun = level === "county" ? "counties" : "tracts";
  return <>
    <header className="topbar"><div><strong>HouseHunter</strong><span>FEMA ALR_NPCTL</span></div><nav aria-label="Exports">
      <a href="/api/v1/exports/places.csv">Tracts CSV</a>
      <a href="/api/v1/exports/places.parquet">Tracts Parquet</a>
      <a href="/api/v1/exports/counties.csv">Counties CSV</a>
      <a href="/api/v1/exports/counties.parquet">Counties Parquet</a>
    </nav></header>
    <main className="layout">
      <section className="rankings">
        <div className="intro">
          <p className="eyebrow">{level === "county" ? `${scopeKind}FEMA National Risk Index counties` : `${scopeKind}FEMA National Risk Index tracts`}</p>
          <h1>Lower risk, plainly ranked.</h1>
          <p>{level === "county"
            ? "One score: each county's published FEMA Expected Annual Loss Rate national percentile, ranked among counties. This is not an average of tract scores."
            : "One score: each tract's published FEMA Expected Annual Loss Rate national percentile."}</p>
        </div>
        <div className="view-toggle" role="group" aria-label="Geography">
          <button type="button" aria-pressed={level === "tract"} onClick={() => changeLevel("tract")}>Tracts</button>
          <button type="button" aria-pressed={level === "county"} onClick={() => changeLevel("county")}>Counties</button>
        </div>
        <form className="filters" onSubmit={submit}>
          <label>Search<input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={level === "county" ? "County name or FIPS" : "Tract FIPS or county name"} /></label>
          <label>State<select value={state} onChange={(e) => changeState(e.target.value)}>
            <option value="">All states</option>
            {STATE_ABBREVIATIONS.map((abbreviation) => <option key={abbreviation} value={abbreviation}>{abbreviation}</option>)}
          </select></label>
          {level === "tract" && <label>County<select value={county} disabled={!state} onChange={(e) => { setCounty(e.target.value); setOffset(0); }}>
            <option value="">All counties</option>
            {countyOptions.map((option) => <option key={option.place_id} value={option.place_id}>{option.name}</option>)}
          </select></label>}
          <label className="check"><input type="checkbox" checked={includeUnranked} onChange={(e) => setIncludeUnranked(e.target.checked)} /> Include incomplete</label>
          <button className="primary" type="submit">Apply</button>
        </form>
        {error && <p role="alert" className="error">{error}</p>}
        <p className="result-count">{number.format(total)} {noun} · click a row for details</p>
        <div className="table-wrap"><table><thead><tr>
          <th scope="col">#</th>
          <th scope="col" aria-sort={sort === "name" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("name")}>{level === "county" ? "County" : "Tract"}</button></th>
          {level === "tract" && <th scope="col">County</th>}
          <th scope="col" aria-sort={sort === "state" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("state")}>State</button></th>
          <th scope="col" aria-sort={sort === "risk_score" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("risk_score")}>Risk score {sort === "risk_score" && direction === "desc" ? "↑" : "↓"}</button></th>
        </tr></thead><tbody>{places.map((place, index) => <tr key={place.place_id} onClick={() => setSelected(place.place_id)}>
          <td>{offset + index + 1}</td><td><button className="place-link" onClick={() => setSelected(place.place_id)}><strong>{place.name}</strong><small>{place.place_type}</small></button></td>
          {level === "tract" && <td>{place.county_name}</td>}
          <td>{place.state}</td>
          <td><span className={place.risk_score === null ? "pill missing" : "pill"}>{scoreLabel(place)}</span>{place.risk_score === null && <small>{place.coverage_status.replaceAll("_", " ")}</small>}</td>
        </tr>)}</tbody></table></div>
        <div className="pager"><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button><span>{offset + 1}–{Math.min(offset + 50, total)}</span><button disabled={offset + 50 >= total} onClick={() => setOffset(offset + 50)}>Next</button></div>
        <footer><strong>Provenance</strong><span>{meta.build?.source_vintages.fema}</span><span>Build {meta.build?.build_id} · {meta.build?.scope.kind}</span></footer>
      </section>
      {selected && <Detail
        placeId={selected}
        level={level}
        onClose={() => setSelected(null)}
        onViewTracts={viewTracts}
        onViewCounty={viewCounty}
      />}
    </main>
  </>;
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(() => { json<Meta>("/api/v1/meta").then(setMeta).catch((caught) => setError(caught.message)); }, []);
  useEffect(refresh, [refresh]);
  if (error) return <main className="setup"><h1>HouseHunter could not start</h1><p role="alert" className="error">{error}</p></main>;
  if (!meta) return <main className="setup"><p>Opening HouseHunter…</p></main>;
  return meta.build ? <Rankings meta={meta} /> : <Setup token={meta.mutation_token} onReady={refresh} />;
}
