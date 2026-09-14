# HouseHunter project notes

HouseHunter is a local-only macOS application that ranks FEMA National Risk Index
tracts by the published tract-level `ALR_NPCTL`, and separately ranks FEMA
counties by the published county-level `ALR_NPCTL`. Lower is better.

A HouseHunter tract score is FEMA's published tract percentile. A county score is
FEMA's published county percentile, ranked among counties — not the mean of tract
scores. Detail and export surfaces also pass through FEMA's 18 published
`{CODE}_ALR_NPCTL` values at that grain; they are not a HouseHunter blend and do
not change list ranking. Neither score is a property-level assessment, a loss
probability, an insurance quote, or a prediction.

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
- Never commit credentials or fetched FEMA payloads.
- FEMA ranking semantics use FEMA only; Community Conditions sorting uses only the
  official CHR&R group. Mountain Magnitude filtering does not alter either metric. Do not
  contact census.gov from the normal prepare, download, or build flow.
  FEMA tract/county and CHR&R county layers are all required.
- Runtime snapshots are schema 9, map-score payloads are schema 3, and public HTTP
  routes are `/api/v2` only. Public artifacts, filters, and types use
  `mountain_magnitude`; do not add score aliases or cap stored magnitudes.
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
