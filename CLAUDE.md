# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Boundary GeoJSON in → one DuckDB file with every Overture Maps table for that area. No client
library: DuckDB reads Overture's GeoParquet straight off S3 (`read_parquet` → bbox prefilter →
`ST_Intersects` clip). The README is thorough and current — read it for behaviour and measured
numbers; `docs/design.md` records the approach and the rejected alternatives.

## Commands

```bash
.venv/bin/pip install -e ".[viz,dev]"           # local venv; viz extra = roadstyle + geopandas

.venv/bin/pytest tests/                          # unit tests, offline
DUCKOVERTURE_NETWORK_TESTS=1 .venv/bin/pytest tests/   # + live S3 extraction (slow)
.venv/bin/pytest tests/test_build.py::test_resolve_by_type_and_theme   # single test

# The pipeline, in order:
duckoverture build -b boundary.geojson -o data/area.duckdb   # raw extract (network)
duckoverture roundabouts data/area.duckdb                    # optional Overpass lookup (network)
duckoverture flatten data/area.duckdb -m routing             # derive flat.* (offline, idempotent)
duckoverture viz data/area.duckdb        # HTML map → viz/ (report+tiles+minzoom by default)
```

No linter is configured. `data/` holds `.duckdb` extracts, `viz/` holds HTML outputs — both
gitignored, deliberately separate.

## Architecture

Two layers, strictly separated (mirrors duckOSM's `raw.*` → processed split):

- **Raw mirror** (`build.py` + `schema.py`): tables land as `<theme>.<type>` exactly matching
  Overture's S3 layout, rows exactly as published — nested structs intact, nothing renamed.
  `schema.py` is the whole catalogue (15 types, pinned `DEFAULT_RELEASE`). Never flatten during
  ingest; the raw layer exists so flattening can be re-derived without re-downloading.
- **Derived** (`flatten.py` → `flat.segment`, `flat.connector`, `flat.turn_restriction`): scalar
  columns resolved from Overture's ranged rules. `viz.py` prefers `flat.segment` when present and
  falls back to raw with a warning.

`flatten.py` is SQL-generation: small Python functions (`_dominant`, `_oneway`, `_motor_access`,
`_breakpoints`, …) each return a SQL fragment, composed per mode. Changes here are changes to
generated SQL — test against `data/sodermalm.duckdb` and watch the length-conservation check the
build prints (total metres must survive every mode exactly).

## Domain rules that code must respect

These are the invariants most edits trip over; each is documented at length in the README and in
the module docstrings where it bites:

- **Overture attributes apply to ranges** (`between: [0.43, 0.97]`), not whole segments. Reading a
  rule list with `[1]` is the canonical bug — `flat.segment` resolves the **dominant** rule
  (largest coverage) instead. The four flatten modes (`segment` / `attrs` / `routing` / `edges`)
  differ only in where they cut; `routing` is the routable form, `edges` exists solely to match
  Overture's reference `transportation-splitter` row-for-row (it never merges, and `sources`
  ranges must be included in breakpoints or you come out ~1,100 pieces short).
- **`ST_Length_Spheroid` reads (lat, lon); Overture geometry is (lon, lat).** Every geodesic
  length goes through `_len()` which wraps `ST_FlipCoordinates` — calling it bare overstates by
  ~54%, and the length-conservation check cannot catch it (both sides err identically).
- **There is no `oneway` column.** One-way = heading-scoped `denied` in `access_restrictions`,
  and only all-mode or motorised denials count (a foot/bicycle denial is a footway rule).
  Forward-denied segments get `ST_Reverse`d for viz so direction chevrons point with traffic.
- **Class implies access.** `footway`/`steps`/`path`/`pedestrian`/`cycleway`/`bridleway` are
  closed to motors unless a rule says otherwise; `when.vehicle`-scoped denials are size limits,
  not closures. In `-m segment` a closure anywhere closes the whole row (presence, not dominance).
- **Overture drops roundabout one-ways** (75% come out two-way). `roundabouts.py` recovers the
  fact by OSM way-id lookup via Overpass (`sources[].record_id` is the OSM id) — a lookup, not a
  geometric guess (geometry detection was measured at 22–35% precision and rejected). It is a
  separate network step so `flatten` stays offline and re-runnable.
- **Turn restrictions must be renumbered after splitting** — both the owning piece of the `from`
  segment and the specific piece of the `to` segment. Partly-resolved chains are dropped (matches
  the official splitter). `to_piece_id = from_piece_id` is a U-turn ban, not a bug.

## Conventions

- Core deps are `duckdb` + `click` only; everything heavier (roadstyle, geopandas) lives behind
  the `viz` extra and is imported lazily inside functions.
- Module and function docstrings carry the *measured evidence* for each decision (row counts,
  metres, percentages from Södermalm/Nacka) — keep that style when changing behaviour: state what
  was measured, not just what the code does. README sections quote real command output; update
  them when output changes.
- `docs/design/` holds one file per non-obvious decision (LR parameterisation, roundabout
  recovery). New rejected-alternative analysis goes there, summarized in README.
