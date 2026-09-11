# HouseHunter

HouseHunter is a local-only macOS application that ranks FEMA National Risk Index
tracts by one transparent measure: each tract's published `ALR_NPCTL`. Lower is
better.

`ALR_NPCTL` is FEMA's national percentile for composite Expected Annual Loss Rate,
distinct from FEMA's broader Risk Index. A HouseHunter score is that published
tract percentile. It is **not** a property-level assessment, a loss probability,
an insurance quote, or a prediction.

## Quick start

HouseHunter requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). The compiled web
interface is committed, so Node is not required to use the app. No API keys are required.

```console
./scripts/run-app
```

That installs Python dependencies, downloads the pinned FEMA source if needed,
publishes a snapshot, and starts the loopback app. Flags: `--port`, `--no-open`,
`--state`, `--skip-prepare`. `--state` only scopes the snapshot. The first run
downloads FEMA once and reuses the local cache afterward.

The same steps can be run individually:

```console
uv sync
uv run househunter sources
uv run househunter download --source fema
uv run househunter build
uv run househunter rank --state CO --limit 20
uv run househunter inspect 08013012101
uv run househunter app
```

Runtime data is written beneath `data/` by default. Set `HOUSEHUNTER_DATA_DIR` to use a
different local directory. The server listens only on `127.0.0.1`; it has no telemetry,
accounts, hosted database, or external browser requests.

## Commands

```text
./scripts/run-app [--port PORT] [--no-open] [--state CO] [--skip-prepare]
househunter sources [--json]
househunter download [--source fema]
househunter build [--state CO]
househunter rank [--state CO] [--limit N] [--include-unranked]
househunter inspect TRACT_FIPS
househunter export --format parquet|csv [--output PATH]
househunter app [--port PORT] [--no-open]
```

`build --state` publishes a snapshot explicitly marked as state-scoped. Re-running a build
with identical inputs reuses the same content-addressed directory. A failed or cancelled
build never replaces the current snapshot. Export destinations must be outside HouseHunter's
managed cache and build paths so exports cannot overwrite immutable runtime data.

## Method

For tract `t`, HouseHunter uses:

```text
risk_score[t] = ALR_NPCTL[t]
```

State is derived from the first two digits of `TRACTFIPS` using a bundled FIPS map.
HouseHunter does not impute, re-percentile, winsorize, or renormalize around missing
tracts. Unrankable rows remain visible as `missing_fema`.

Read [DATA_SOURCES.md](DATA_SOURCES.md) for source provenance, release maintenance, and
limitations.

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
