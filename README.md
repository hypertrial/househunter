# HouseHunter

HouseHunter is a local-only macOS application that ranks every 2020 U.S. Census Place in
the 50 states and District of Columbia by one transparent measure: the 2020-housing-weighted
mean of FEMA tract-level `ALR_NPCTL`. Lower is better.

`ALR_NPCTL` is FEMA's national percentile for composite Expected Annual Loss Rate, distinct
from FEMA's broader Risk Index. A HouseHunter score is an aggregation created by this
project. It is **not** a FEMA-published Place percentile, a property-level assessment, a loss
probability, an insurance quote, or a prediction.

## Quick start

HouseHunter requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). The compiled web
interface is committed, so Node is not required to use the app. No API keys are required.

```console
./scripts/run-app
```

That installs Python dependencies, downloads the pinned FEMA source if needed,
generates a local Census reference copy under `data/` when packaged assets are
missing, publishes a snapshot, and starts the loopback app. Flags: `--port`,
`--no-open`, `--state`, `--skip-prepare`. `--state` only scopes the snapshot;
missing Census assets are still generated nationally and the first run can take
a long time.

The same steps can be run individually:

```console
uv sync
uv run househunter sources
uv run househunter download --source fema
uv run househunter build
uv run househunter rank --state CO --limit 20 --min-population 1000
uv run househunter inspect "Boulder, CO"
uv run househunter app
```

Runtime data is written beneath `data/` by default. Set `HOUSEHUNTER_DATA_DIR` to use a
different local directory. The server listens only on `127.0.0.1`; it has no telemetry,
accounts, hosted database, or external browser requests.

Release archives include the generated Census reference assets. `./scripts/run-app`
can generate a local ignored copy when those files are missing. For release
regeneration, follow [DATA_SOURCES.md](DATA_SOURCES.md).

## Commands

```text
./scripts/run-app [--port PORT] [--no-open] [--state CO] [--skip-prepare]
househunter sources [--json]
househunter download [--source fema]
househunter build [--state CO]
househunter rank [--state CO] [--limit N] [--min-population N] [--include-unranked]
househunter inspect "Place, ST" | PLACE_ID
househunter export --format parquet|csv [--output PATH]
househunter app [--port PORT] [--no-open]
```

`build --state` publishes a snapshot explicitly marked as state-scoped. Re-running a build
with identical inputs reuses the same content-addressed directory. A failed or cancelled
build never replaces the current snapshot. Export destinations must be outside HouseHunter's
managed cache and build paths so exports cannot overwrite immutable runtime data.

## Method

For Place `p`, HouseHunter calculates:

```text
risk_score[p] = sum(housing_units[p,t] * ALR_NPCTL[t])
                / sum(housing_units[p,t])
```

Every positive-housing Place/tract intersection must have a valid FEMA value. HouseHunter
does not impute, re-percentile, winsorize, or renormalize around missing tracts. Unrankable
Places remain visible as `zero_housing`, `missing_fema`, or `unmatched_geography`.

Read [DATA_SOURCES.md](DATA_SOURCES.md) for source provenance, release maintenance, the
Connecticut reconciliation, and limitations.

## Development

```console
uv sync
uv run pytest
uv run ruff check .

cd web
npm ci
npm test
npm run build
```

The committed `web/dist/` must match `npm run build`. The Python wheel packages that bundle.
Raw third-party files and generated runtime databases belong under ignored `data/`, never in
Git.

## Scope

V1 intentionally contains no mountain classifier, trails, maps, insurance data, additional
hazards, composite weights, hosted service, or native installer. Those ideas remain deferred
until the sole-metric workflow is demonstrably correct and useful.

## License

MIT. Public source data retain their respective attribution and terms.
