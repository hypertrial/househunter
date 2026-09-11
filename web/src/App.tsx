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
    <p className="lead">HouseHunter downloads the pinned FEMA National Risk Index and builds a private snapshot on this Mac. No Census download is required.</p>
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
  return <dialog ref={dialog} className="detail" aria-label="Tract detail" onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <button autoFocus className="close" onClick={onClose} aria-label="Close tract detail">×</button>
    {error && <p role="alert" className="error">{error}</p>}
    {!detail ? <p>Loading…</p> : <>
      <p className="eyebrow">{detail.summary.place_type} · {detail.summary.place_id}</p>
      <h2>{detail.summary.name}, {detail.summary.state}</h2>
      <div className="score"><span>{scoreLabel(detail.summary)}</span><small>FEMA ALR_NPCTL<br />Lower is better</small></div>
      <dl className="facts">
        <div><dt>Tract FIPS</dt><dd>{detail.summary.place_id}</dd></div>
        <div><dt>State</dt><dd>{detail.summary.state}</dd></div>
        <div><dt>FEMA vintage</dt><dd>{detail.summary.fema_vintage}</dd></div>
      </dl>
      <h3>FEMA value</h3>
      <div className="contributions">
        {detail.tract_contributions.map((tract, index) => <div key={tract.tract_id ?? `missing-${index}`} className="contribution">
          <div><span>{tract.tract_id ?? "Missing tract"}</span><span>{tract.fema_percentile?.toFixed(1) ?? "Missing"}</span></div>
          <div className="bar"><i style={{ width: `${tract.fema_percentile ?? 0}%` }} /></div>
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
    if (includeUnranked) params.set("include_unranked", "true");
    try {
      const result = await json<{ items: PlaceSummary[]; total: number }>(`/api/v1/places?${params}`);
      setPlaces(result.items); setTotal(result.total); setError(null);
    } catch (caught) { setError((caught as Error).message); }
  }, [direction, includeUnranked, offset, search, sort, state]);

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
        <div className="intro"><p className="eyebrow">{meta.build?.scope.kind === "state" ? `${meta.build.scope.state} FEMA tracts` : "FEMA National Risk Index tracts"}</p><h1>Lower risk, plainly ranked.</h1><p>One score: each tract's published FEMA Expected Annual Loss Rate national percentile.</p></div>
        <form className="filters" onSubmit={submit}>
          <label>Search<input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Tract FIPS" /></label>
          <label>State<select value={state} onChange={(e) => { setState(e.target.value); setOffset(0); }}>
            <option value="">All states</option>
            {STATE_ABBREVIATIONS.map((abbreviation) => <option key={abbreviation} value={abbreviation}>{abbreviation}</option>)}
          </select></label>
          <label className="check"><input type="checkbox" checked={includeUnranked} onChange={(e) => setIncludeUnranked(e.target.checked)} /> Include incomplete</label>
          <button className="primary" type="submit">Apply</button>
        </form>
        {error && <p role="alert" className="error">{error}</p>}
        <p className="result-count">{number.format(total)} tracts · click a row for details</p>
        <div className="table-wrap"><table><thead><tr>
          <th scope="col">#</th>
          <th scope="col" aria-sort={sort === "name" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("name")}>Tract</button></th>
          <th scope="col" aria-sort={sort === "state" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("state")}>State</button></th>
          <th scope="col" aria-sort={sort === "risk_score" ? (direction === "asc" ? "ascending" : "descending") : "none"}><button onClick={() => changeSort("risk_score")}>Risk score {sort === "risk_score" && direction === "desc" ? "↑" : "↓"}</button></th>
        </tr></thead><tbody>{places.map((place, index) => <tr key={place.place_id} onClick={() => setSelected(place.place_id)}>
          <td>{offset + index + 1}</td><td><button className="place-link" onClick={() => setSelected(place.place_id)}><strong>{place.name}</strong><small>{place.place_type}</small></button></td><td>{place.state}</td>
          <td><span className={place.risk_score === null ? "pill missing" : "pill"}>{scoreLabel(place)}</span>{place.risk_score === null && <small>{place.coverage_status.replaceAll("_", " ")}</small>}</td>
        </tr>)}</tbody></table></div>
        <div className="pager"><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button><span>{offset + 1}–{Math.min(offset + 50, total)}</span><button disabled={offset + 50 >= total} onClick={() => setOffset(offset + 50)}>Next</button></div>
        <footer><strong>Provenance</strong><span>{meta.build?.source_vintages.fema}</span><span>Build {meta.build?.build_id} · {meta.build?.scope.kind}</span></footer>
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
  return meta.build ? <Rankings meta={meta} /> : <Setup token={meta.mutation_token} onReady={refresh} />;
}
