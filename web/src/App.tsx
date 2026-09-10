import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import type { JobStatus, PlaceDetail, PlaceSummary } from "./types";

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

const number = new Intl.NumberFormat("en-US");
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Request failed");
  return body as T;
}

export function scoreLabel(place: PlaceSummary): string {
  return place.risk_score === null ? "Not ranked" : place.risk_score.toFixed(1);
}

function Setup({ token, referencesReady, referencesError, onReady }: { token: string; referencesReady: boolean; referencesError: string | null; onReady: () => void }) {
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
    <h1>Rank places with one clear risk signal.</h1>
    <p className="lead">HouseHunter downloads the pinned FEMA source, joins it to packaged Census housing weights, and builds a private snapshot on this Mac.</p>
    {job && <section aria-live="polite" className="progress-card">
      <div className="progress-copy"><strong>{job.message}</strong><span>{job.progress}%</span></div>
      <progress max="100" value={job.progress}>{job.progress}%</progress>
      {["queued", "running"].includes(job.state) && <button className="secondary" onClick={cancel}>Cancel</button>}
    </section>}
    {error && <p role="alert" className="error">{error}</p>}
    {!referencesReady && <p role="alert" className="error">{referencesError ? `Census reference validation failed: ${referencesError}` : "This checkout is missing its release-generated Census reference assets. See DATA_SOURCES.md before preparing data."}</p>}
    {referencesReady && (!job || ["failed", "cancelled"].includes(job.state)) && <button className="primary" onClick={prepare}>{job ? "Try preparation again" : "Prepare national data"}</button>}
    <p className="fine">FEMA NRI December 2025 v1.20 · Census 2020 · ACS 2024 5-year</p>
  </main>;
}

function Detail({ placeId, onClose }: { placeId: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [detail, setDetail] = useState<PlaceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    json<PlaceDetail>(`/api/v1/places/${placeId}`).then(setDetail).catch((caught) => setError(caught.message));
  }, [placeId]);
  useEffect(() => {
    const node = dialog.current;
    if (node && !node.open) node.showModal();
    return () => {
      if (node?.open) node.close();
    };
  }, []);
  return <dialog ref={dialog} className="detail" aria-label="Place detail" onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <button autoFocus className="close" onClick={onClose} aria-label="Close place detail">×</button>
    {error && <p role="alert" className="error">{error}</p>}
    {!detail ? <p>Loading…</p> : <>
      <p className="eyebrow">{detail.summary.place_type} · {detail.summary.place_id}</p>
      <h2>{detail.summary.name}, {detail.summary.state}</h2>
      <div className="score"><span>{scoreLabel(detail.summary)}</span><small>HouseHunter Risk Score<br />Lower is better</small></div>
      <dl className="facts">
        <div><dt>2024 population</dt><dd>{detail.summary.population_2024 !== null ? number.format(detail.summary.population_2024) : "Unavailable"}</dd></div>
        <div><dt>2024 median home value</dt><dd>{detail.summary.median_home_value_2024 !== null ? money.format(detail.summary.median_home_value_2024) : "Unavailable"}</dd></div>
        <div><dt>Housing coverage</dt><dd>{(detail.coverage_ratio * 100).toFixed(1)}%</dd></div>
      </dl>
      <h3>Tract contributions</h3>
      <div className="contributions">
        {detail.tract_contributions.map((tract, index) => <div key={tract.tract_id ?? `unmatched-${index}`} className="contribution">
          <div><span>{tract.tract_id ?? "Unmatched tract"}</span><span>{tract.fema_percentile?.toFixed(1) ?? "Missing"}</span></div>
          <div className="bar"><i style={{ width: `${tract.weighted_contribution ?? 0}%` }} /></div>
          <small>{number.format(tract.housing_units)} homes · {(tract.housing_weight * 100).toFixed(1)}% weight · {(tract.weighted_contribution ?? 0).toFixed(2)} points</small>
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
  const [minimum, setMinimum] = useState("");
  const [includeUnranked, setIncludeUnranked] = useState(false);
  const [sort, setSort] = useState("risk_score");
  const [direction, setDirection] = useState("asc");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: "50", offset: String(offset), sort, direction });
    if (search) params.set("search", search);
    if (state) params.set("state", state);
    if (minimum) params.set("min_population", minimum);
    if (includeUnranked) params.set("include_unranked", "true");
    try {
      const result = await json<{ items: PlaceSummary[]; total: number }>(`/api/v1/places?${params}`);
      setPlaces(result.items); setTotal(result.total); setError(null);
    } catch (caught) { setError((caught as Error).message); }
  }, [direction, includeUnranked, minimum, offset, search, sort, state]);

  useEffect(() => { void load(); }, [load]);
  function submit(event: FormEvent) { event.preventDefault(); setOffset(0); void load(); }
  function changeSort(value: string) {
    if (sort === value) setDirection(direction === "asc" ? "desc" : "asc");
    else { setSort(value); setDirection("asc"); }
    setOffset(0);
  }
  return <>
    <header className="topbar"><div><strong>HouseHunter</strong><span>FEMA ALR_NPCTL</span></div><nav aria-label="Exports"><a href="/api/v1/exports/places.csv">CSV</a><a href="/api/v1/exports/places.parquet">Parquet</a></nav></header>
    <main className="layout">
      <section className="rankings">
        <div className="intro"><p className="eyebrow">{meta.build?.scope.kind === "state" ? `${meta.build.scope.state} 2020 Census Places` : "Every 2020 U.S. Census Place"}</p><h1>Lower risk, plainly ranked.</h1><p>One score: the housing-weighted mean of FEMA tract-level Expected Annual Loss Rate national percentiles.</p></div>
        <form className="filters" onSubmit={submit}>
          <label>Search<input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Place or ID" /></label>
          <label>State<input value={state} onChange={(e) => setState(e.target.value.toUpperCase().slice(0, 2))} placeholder="CO" /></label>
          <label>Min. population<input type="number" min="0" value={minimum} onChange={(e) => setMinimum(e.target.value)} /></label>
          <label className="check"><input type="checkbox" checked={includeUnranked} onChange={(e) => setIncludeUnranked(e.target.checked)} /> Include incomplete</label>
          <button className="primary" type="submit">Apply</button>
        </form>
        {error && <p role="alert" className="error">{error}</p>}
        <p className="result-count">{number.format(total)} places · click a row for details</p>
        <div className="table-wrap"><table><thead><tr>
          <th scope="col">#</th>
          <th scope="col" aria-sort={sort === "name" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("name")}>Place</button></th>
          <th scope="col" aria-sort={sort === "state" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("state")}>State</button></th>
          <th scope="col" aria-sort={sort === "population" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("population")}>Population</button></th>
          <th scope="col" aria-sort={sort === "home_value" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("home_value")}>Home value</button></th>
          <th scope="col" aria-sort={sort === "risk_score" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("risk_score")}>Risk score {sort === "risk_score" && direction === "desc" ? "↑" : "↓"}</button></th>
        </tr></thead><tbody>{places.map((place, index) => <tr key={place.place_id} onClick={() => setSelected(place.place_id)}>
          <td>{offset + index + 1}</td><td><button className="place-link" onClick={() => setSelected(place.place_id)}><strong>{place.name}</strong><small>{place.place_type}</small></button></td><td>{place.state}</td>
          <td>{number.format(place.population_2024 ?? place.population_2020)}</td>
          <td>{place.median_home_value_2024 !== null ? money.format(place.median_home_value_2024) : "—"}</td>
          <td><span className={place.risk_score === null ? "pill missing" : "pill"}>{scoreLabel(place)}</span>{place.risk_score === null && <small>{place.coverage_status.replaceAll("_", " ")}</small>}</td>
        </tr>)}</tbody></table></div>
        <div className="pager"><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button><span>{offset + 1}–{Math.min(offset + 50, total)}</span><button disabled={offset + 50 >= total} onClick={() => setOffset(offset + 50)}>Next</button></div>
        <footer><strong>Provenance</strong><span>{meta.build?.source_vintages.fema} · Census {meta.build?.source_vintages.census} · {meta.build?.source_vintages.acs}</span><span>Build {meta.build?.build_id} · {meta.build?.scope.kind}</span></footer>
      </section>
      {selected && <Detail placeId={selected} onClose={() => setSelected(null)} />}
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
  return meta.build ? <Rankings meta={meta} /> : <Setup token={meta.mutation_token} referencesReady={meta.reference_assets_ready} referencesError={meta.reference_assets_error} onReady={refresh} />;
}
