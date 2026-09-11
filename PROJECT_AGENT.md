# HouseHunter project notes

HouseHunter is a local-only macOS application that ranks FEMA National Risk Index
tracts by the published tract-level `ALR_NPCTL`. Lower is better.

A HouseHunter score is FEMA's published tract percentile. It is **not** a
property-level assessment, a loss probability, an insurance quote, or a prediction.

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
- Runtime ranking uses FEMA only. Do not contact census.gov from `run-app`,
  prepare, or build.

## Verification

- Fast: `uv run ruff check .`, `uv run pytest`, `npm test` in `web/`.
- Completion: also `npm ci`, `npm run build`, `git diff --exit-code -- web/dist`,
  Playwright Chromium e2e in `web/`.

These commands are wrapped by `scripts/verify-fast` and `scripts/verify`.
Native GitHub CI is the independent verification source.
