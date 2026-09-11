# HouseHunter

HouseHunter is a local-only macOS application that ranks FEMA National Risk Index
geographies by published `ALR_NPCTL`. Lower is better. Tract ranking is the
default. County ranking uses FEMA's official county table, not an average of
tract scores.

`ALR_NPCTL` is FEMA's national percentile for composite Expected Annual Loss Rate,
distinct from FEMA's broader Risk Index. Tract and county percentiles are not
comparable. A HouseHunter score is **not** a property-level assessment, a loss
probability, an insurance quote, or a prediction.

## Quick start

HouseHunter requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). The compiled web
interface is committed, so Node is not required to use the app. No API keys are required.

```console
./scripts/run-app
```

That installs Python dependencies, downloads the pinned FEMA tract and county
layers if needed, publishes a snapshot, and starts the loopback app. Flags:
`--port`, `--no-open`, `--state`, `--skip-prepare`. `--state` only scopes the
snapshot. The first run downloads FEMA once and reuses the local cache afterward.

The same steps can be run individually:

```console
uv sync
uv run househunter sources
uv run househunter download --source all
uv run househunter build
uv run househunter rank --state CO --limit 20
uv run househunter rank --level county --state CO
uv run househunter inspect 08013012101
uv run househunter inspect 08013
uv run househunter app
```

Runtime data is written beneath `data/` by default. Set `HOUSEHUNTER_DATA_DIR` to use a
different local directory. The server listens only on `127.0.0.1`; it has no telemetry,
accounts, hosted database, or external browser requests.

## Commands

```text
./scripts/run-app [--port PORT] [--no-open] [--state CO] [--skip-prepare]
househunter sources [--json]
househunter download [--source fema|fema_counties|all]
househunter build [--state CO]
househunter rank [--state CO] [--county STCOFIPS] [--level tract|county] [--limit N] [--include-unranked]
househunter inspect TRACT_FIPS|COUNTY_FIPS
househunter export --format parquet|csv [--level tract|county] [--output PATH]
househunter app [--port PORT] [--no-open]
```

`build --state` publishes a snapshot explicitly marked as state-scoped. Re-running a build
with identical inputs reuses the same content-addressed directory. A failed or cancelled
build never replaces the current snapshot. Export destinations must be outside HouseHunter's
managed cache and build paths so exports cannot overwrite immutable runtime data.

## Method

For tract `t` and county `c`, HouseHunter uses:

```text
risk_score[t] = ALR_NPCTL[t]
risk_score[c] = ALR_NPCTL[c]
```

County names come from the FEMA county layer. Tract rows join `TRACTFIPS[:5]` to
`STCOFIPS`. The county percentile is FEMA's published county value, ranked among
counties; it is not the mean of tract percentiles. State is derived from the first two
digits of `TRACTFIPS` using a bundled FIPS map. HouseHunter does not impute,
re-percentile, winsorize, or renormalize around missing rows. Unrankable rows remain
visible as `missing_fema`.

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
