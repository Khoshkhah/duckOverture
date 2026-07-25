# Design

## Problem

Overture Maps publishes ~15 datasets as GeoParquet on S3, partitioned by theme and type. Getting
"everything for this area" means 15 separate fetches, each of which must avoid downloading a
planet-scale dataset to keep a neighbourhood.

## Approach

**DuckDB reads Overture's S3 Parquet directly.** No client library, no staging download. One query
per type:

```
read_parquet(s3://.../theme=X/type=Y/*)  →  bbox prefilter  →  ST_Intersects clip  →  table
```

### Why the bbox prefilter matters

Overture carries a `bbox` STRUCT column alongside the geometry. Filtering on it is a cheap numeric
predicate that Parquet can push into row-group statistics, so most of the dataset is never read.
Measured on Södermalm's `transportation.segment`: 6,165 candidate rows fetched in ~6 s, then
exact-clipped to 4,251 in ~0 s.

The predicate is an **overlap** test, not containment — a segment crossing the boundary must be
kept:

```sql
bbox.xmin <= xmax AND bbox.xmax >= xmin AND bbox.ymin <= ymax AND bbox.ymax >= ymin
```

### Raw first, flatten later

Rows are written **exactly as published** — nested structs intact, nothing renamed or dropped.
Flattening is a separate stage that reads these tables, so:

- the raw layer stays a faithful mirror and is auditable against upstream
- flattening logic can change without re-downloading
- anything Overture adds to a schema survives automatically

This mirrors duckOSM's `raw.*` → processed split.

### Schema per theme

`<theme>.<type>` matches Overture's own `theme=X/type=Y/` layout, so the mapping between our db and
upstream is one-to-one and needs no lookup table.

## Rejected

- **The `overturemaps` Python client.** It works (`record_batch_reader` → Arrow), but it adds a
  dependency to do what DuckDB does natively, and routing bytes through Python discards the
  row-group pruning that makes the bbox prefilter fast.
- **Flattening during ingest.** Would bake today's understanding of Overture's schema into the
  download step, and force a re-fetch whenever that understanding changed.
- **One table per theme (union of types).** Types within a theme have different schemas —
  `division`, `division_area` and `division_boundary` are not union-compatible.
- **Fetching a release listing at runtime.** The default release is pinned in code; a wrong pin
  fails loudly with a clear S3 error, which is better than silently building against a release the
  caller did not choose.

## Known limits

- **`_meta.built_at` is a string.** Fetching DuckDB's `TIMESTAMPTZ` into Python requires `pytz`;
  keeping `now()::VARCHAR` inside SQL avoids a dependency for one metadata row.
- **No resume.** A failed type is logged and skipped, and the run continues; re-running rebuilds
  everything. Fine at neighbourhood scale, wrong for a country.
- **`--no-clip` keeps bbox-overlapping rows**, which extend past the boundary. Intentional: it is
  faster, and sometimes the surrounding context is wanted.
- Only WGS84 in, WGS84 out. Overture is `OGC:CRS84` throughout.
