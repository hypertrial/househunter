# Ranking v2 maintainer qualification

Ranking v2 is built from a coordinated, immutable public-source contract at
`source-lock-v2.json`. The qualification cutoff for this release is September 16,
2026. Ordinary `househunter prepare`, `download`, `build`, and browser requests do
not acquire any of these agency files.

The source lock is fail-closed. Every download has an exact identity, byte count,
SHA-256 digest, vintage, required schema, terms page, and geography. Archives also
have a logical member-tree digest, member count, and total uncompressed size. A
mutable source may be used only after the exact response or staged artifact has
been locked. Credentials are not accepted.

Archive `logical_sha256` values are SHA-256 over canonical JSON for regular-file
records in archive iteration order. Each record has exactly `name`, `bytes`, and
`sha256`; directory entries are excluded. Extraction still rejects unexpected
members, links, devices, encryption, nested archives, traversal, and expansion
outside the reviewed limits.

## Private workspace

Use `data/ranking-v2/raw`, `data/ranking-v2/normalized`, and
`data/ranking-v2/work`. These directories are gitignored and must contain all raw
agency data, EPA geometry and Census-block allocation data, FBI agency responses,
and private Realtor.com history. Only the compact derived county bundle,
calibration, citations, manifest, and checksums may be published under
`src/househunter/assets/ranking_v2`.

The FBI qualification step is resumable and anonymous:

```console
uv run python scripts/acquire_ranking_fbi.py --census-pep data/ranking-v2/raw/census/co-est2025-alldata.csv
```

It checkpoints the exact public CDE catalog and agency-summary responses beneath
the private workspace. It emits a compressed request/response manifest containing
only public request paths, response hashes and sizes, published county attribution,
and agency types. The 2026 qualification locked 51 catalogs, 14,154 agencies that
resolve 1:1 to a current Census PEP county, and 28,308 offense-summary responses.
It explicitly records 653 multiple-county, three unspecified-county, and three
retired Valdez-Cordova exclusions. It never stores an API credential because none
is required. `validate_fbi_response_manifest` verifies both compressed and bounded
uncompressed identities, the exact current-county universe, request coverage,
ordering, counts, and logical digests before the manifest can qualify the source
contract. Maintainers may add `--manifest-only` to revalidate the private response
cache and regenerate the compact manifest without making network requests.

## Locked interpretation notes

- Census PEP population is context and an eligibility gate, never a utility.
- FBI reporting is voluntary. Crime is null unless both offense families meet the
  90% population-served coverage floor in every included year. Multi-county agency
  catalog entries are not assigned to an invented primary county.
- EPA water allocations use active retail CWS boundaries and 2020 block
  population. A system marked as a wholesaler remains included when its service
  areas also contain a non-wholesale code; only an exact `{WH}` service-area set is
  treated as wholesale-only. Connecticut remains null because those block
  identifiers use legacy counties and no reviewed block-to-planning-region overlay
  is locked. Missing multi-county boundaries null every listed county rather than
  inventing a split; a missing single-county boundary contributes its full reported
  population to that county's allocation-coverage denominator. A system whose
  wholesale-only classification is unknown contributes the same denominator
  uncertainty when it can be attributed to one current county. The public-water
  `max_intersection_proxy` uses the maximum `Pop20_AW` intersection per block over
  the locked PEP 2020 estimates-base county population. This conservative proxy
  avoids double counting but can understate disjoint service areas within one
  block; it is context, not an estimate of private-well use or a score input.
- Provider counts are availability proxies, not access, quality, or utilization.
- FCC `speed_100_20` uses the residential (`biz_res=R`) view of
  broadband-serviceable locations, not people. The alternative business view is
  not averaged into it.
- Climate uses equal-weight qualifying in-county stations and has no nearest-station
  fallback.
- The homeschool rubric is approximate and project-authored. Its mandatory notice
  is part of the source contract and is not a legal-review claim.

## Annual refresh checklist

1. Set and record the release cutoff before downloading data.
2. Recheck every terms page and denied-data boundary. Do not add FCC Location
   Fabric, incident/person-level NIBRS, identifiable CMS/tax data, or student data.
3. Acquire the newest complete compatible release available by the cutoff. Never
   combine revisions within a Census vintage.
4. Recompute every byte count, SHA-256, archive logical inventory, required schema,
   source vintage, and retrieval date. Treat drift as a new qualification, not a
   resumable checkpoint.
5. Check every homeschool official-source link and cited effective date. Record the
   check date without describing it as legal review.
6. Re-run the synthetic raw-input golden counties, coverage boundaries, suppression,
   current Connecticut, territory, duplicate-key, and multiplying-join cases.
7. Compare source-valid and complete-core counts, annual crime coverage, water
   allocation coverage/provenance, and climate station coverage to the previous
   release. The published manifest must bind the exact source-status distributions,
   non-null bundle-pillar counts, and complete/partial public-core counts recomputed
   from `counties.parquet`. Investigate material changes before publication.
8. Build twice from clean normalized checkpoints and require identical logical and
   byte checksums.
9. Scan the package inventory for raw files, private Realtor.com rows, credentials,
   geometry, Census blocks, agency responses, and unexpected files.
10. Build and validate the national snapshot, record actual source/core coverage,
    run `scripts/verify`, and complete the independent data, security, test, UI, and
    final reviews.

## Recovery and rollback

An interrupted stage resumes only when its contract hash, input hashes, schema, and
completed-output hash agree. Otherwise build a new content-addressed stage; do not
reinterpret or overwrite the old checkpoint. Publication is atomic.

Rollback restores the previous application/bundle release and the previous snapshot
pointer together. Snapshot schema 12 must never be interpreted as schema 13, and a
schema-13 pointer must not be retained with an older application.
