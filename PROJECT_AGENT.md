# HouseHunter project notes

HouseHunter is a local-only macOS application that independently ranks FEMA National
Risk Index tracts and counties by HouseHunter's `RES_HAZARD_NPCTL`. The metric derives
same-grain national percentiles from 17 building-specific `*_ALRB` fields and combines
spectral, worst-quartile-tail, and fourth-order power aggregations. Higher is worse.

Never rank counties against tracts or average tract scores into counties. Preserve the
three source states: valid rates rank nationally, not-applicable hazards contribute
zero, and missing/invalid hazards remain null and lower the explicit data-quality count.
FEMA `ALR_NPCTL` and `ALR_VALB` remain supporting detail/export provenance only;
`PROPERTY_LOSS_NPCTL` is derived from `ALR_VALB` but never blended into the primary
metric. No score is a property-level assessment, loss probability, insurance quote,
or prediction.

CHR&R Community Conditions is a separate county-level thematic metric. Preserve the
official 2025 integer group (1 healthiest, 10 least healthy, or null), join only on
five-digit county FIPS, label tract inheritance as county-level, and never convert it
to a percentile or blend it with FEMA.

Mountain Magnitude is a third, independent contextual layer. Runtime snapshots may join
only a validated schema-2 national tract/county artifact using
`mountain_magnitude_v2`; `mountain_score_v1` remains internal full-release validation
evidence and is never public. Keep the native GIS stack optional and maintainer-only;
ordinary setup must remain lightweight. Missing Mountain data is null with explicit
`unavailable` status, never zero. Preserve the pinned source lock, content-addressed
release identity, national population-weighted component percentiles, exact integer
45/20/20/15 base weighting, six-decimal geography bases, four-decimal uncapped
logarithmic magnitude, and inclusive equal-or-higher same-grain peer rule. A +1
magnitude means ten times fewer equal-or-higher peers, not ten times more terrain;
tracts and counties are not cross-grain comparable. Do not describe it as
property-specific views, trail quality, drive time, or guaranteed access.

Cost of Living and Home Costs are fourth and fifth independent dimensions. Cost of
Living uses the pinned BEA all-items RPP (U.S. = 100, lower is better), assigned at
MSA or U.S. nonmetropolitan geography and inherited through counties by tracts. Home
Costs uses a manually imported, approved county asking-market release; square feet for
$1M and its national county percentile are higher-is-better and inherited by tracts.
ACS 2024 housing-stock estimates are direct at tract/county grain. Never blend these
dimensions, imply tract-level BEA/market precision, substitute ACS home value, or
describe asking-market indicators as sales, valuations, or total ownership costs.

Use the `househunter-engineering` workspace from `.pad.toml`. Follow `AGENTS.md`
and the local `pad-engineering` skill. Keep ticket bodies, exports, credentials,
and local Pad state out of this public repository.

## Invariants

- Runtime data stays under `data/` (gitignored) or `HOUSEHUNTER_DATA_DIR`.
- The server listens only on `127.0.0.1`. There is no telemetry, accounts, hosted
  database, or external browser request from the app.
- The compiled web interface in `web/dist` is committed; keep it in sync with
  `web/` source. Node is not required to *use* the app, but it is required to
  change the UI.
- Never commit credentials, fetched FEMA/BEA payloads, Realtor.com source rows, or
  derived home-market snapshots. Real home-market data is loopback/local-export only.
- Residential Hazard Exposure uses only FEMA building-loss inputs; Community Conditions
  sorting uses only the official CHR&R group. Mountain Magnitude filtering does not alter
  either metric. Do not
  contact census.gov from the normal prepare, download, or build flow. Normal download
  also never contacts Realtor.com. FEMA tract/county and CHR&R county layers are
  required; BEA RPP and the local home-market import are optional and fail open with
  explicit statuses.
- Runtime snapshots are schema 11, map-score payloads are schema 5, and public HTTP
  routes are `/api/v3` only. `/api/v1` and `/api/v2` remain unsupported. Public
  artifacts use `res_hazard_npctl` and `mountain_magnitude`; do not add legacy score
  aliases or cap stored magnitudes.
- Keep the compatible full map-score endpoint. The browser uses the build-bound core
  plus lazy Cost and Home/ACS add-ons to preserve the initial payload envelope. Never
  weaken the 5.7 MB decoded, 1.3 MB gzip, or existing interaction thresholds.
- Cross-major Mountain migration uses only `househunter mountain rescore-v1`, stages
  and validates full/compact/snapshot artifacts before pointer publication, and resumes
  an interrupted commit forward from its atomic journal. Never load or advertise a v1
  Mountain release as a v2 rollback; rollback restores the v1 app and v1 pointers.
- Address lookup may contact `geocoding.geo.census.gov` on explicit user action,
  and may contact Nominatim (`HOUSEHUNTER_NOMINATIM_URL`, default
  `nominatim.openstreetmap.org`) only after a valid Census empty match list.
  The browser never does; the loopback API proxies both. Nominatim is not a
  Census-outage backup. Street-level OSM matches require confirmation.
  Addresses are not persisted.

## Verification

- Fast: `uv run ruff check .`, `uv run pytest`, `npm test` in `web/`.
- Completion: also `npm ci`, `npm run build`, `git diff --exit-code -- web/dist`,
  and Playwright Chromium and WebKit e2e in `web/`.

These commands are wrapped by `scripts/verify-fast` and `scripts/verify`.
