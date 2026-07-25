# duckoverture

**Give it a boundary, get a DuckDB file with every Overture Maps table for that area.**

```bash
duckoverture build -b sodermalm.geojson -o sodermalm.duckdb
```

```
boundary sodermalm.geojson  bbox 18.0248,59.3027 -> 18.1093,59.3238
release 2026-07-22.0  |  15 type(s)  |  clip=True
  addresses.address                          0 rows     1.5s
  base.bathymetry                           10 rows     0.7s
  base.infrastructure                    4,986 rows     2.3s
  base.land                                432 rows     1.9s
  base.land_cover                           27 rows     2.7s
  base.land_use                            470 rows     1.3s
  base.water                                23 rows     2.2s
  buildings.building                     3,346 rows    12.8s
  buildings.building_part                   69 rows     0.8s
  divisions.division                         8 rows     0.7s
  divisions.division_area                   17 rows     2.8s
  divisions.division_boundary                2 rows     0.6s
  places.place                           6,547 rows     1.3s
  transportation.connector               5,951 rows     1.3s
  transportation.segment                 4,251 rows     6.1s
wrote sodermalm.duckdb  (15/15 types, 26,139 rows)
```

**~39 s for a whole island, no API key, no download of anything you did not ask for.**

## How it works

Overture publishes GeoParquet on S3. DuckDB reads that natively, so there is no client library and
no intermediate download — one query per type does the whole job:

```
read_parquet(s3 url)  →  bbox prefilter  →  exact ST_Intersects clip  →  table
```

The **bbox prefilter** is what makes it affordable. Overture carries a `bbox` struct column, so a
cheap numeric predicate lets Parquet skip whole row groups before any geometry is decoded. On
Södermalm that is 6,165 candidate segments fetched in ~6 s, then clipped to 4,251 in ~0 s. Asking
`ST_Intersects` against the remote dataset directly would pull the planet.

## The output

Schema per Overture theme, table per type — the db mirrors the upstream layout, nothing renamed:

```
addresses.address          divisions.division          transportation.segment
base.bathymetry            divisions.division_area     transportation.connector
base.infrastructure        divisions.division_boundary
base.land                  places.place
base.land_cover            buildings.building
base.land_use              buildings.building_part
base.water
main.boundary              -- the clip polygon, kept
main._meta                 -- release, boundary path, build time, clipped flag
```

**Rows are stored exactly as Overture publishes them.** Nested structs stay nested:

```sql
SELECT id, class, connectors, speed_limits
FROM transportation.segment WHERE speed_limits IS NOT NULL LIMIT 1;

id            = 7330d426-2123-4673-9e39-e1ceb03b3833
class         = secondary
connectors[0] = {'connector_id': 'dac74b95-...', 'at': 0.0}
speed_limits  = [{'max_speed': {'value': 40, 'unit': 'km/h'}, ...}]
```

That is deliberate: the raw layer is a faithful mirror, so flattening can be re-derived without
re-downloading. Turn restrictions (`prohibited_transitions`), access rules with time conditions,
level rules and GERS ids all survive intact.

Geometry arrives as real DuckDB `GEOMETRY` — DuckDB reads the GeoParquet metadata, so no WKB
conversion is needed:

```sql
LOAD spatial;
SELECT round(sum(ST_Length_Spheroid(geometry)))
FROM transportation.segment WHERE subtype = 'road';   -- 448,588 m
```

## Usage

```bash
duckoverture types                      # list themes and types

duckoverture build \
  -b boundary.geojson \                 # Feature, FeatureCollection or bare geometry
  -o area.duckdb \
  -r 2026-07-22.0 \                     # pin a release (default: latest known)
  -t transportation -t buildings \      # themes or types, repeatable; default all 15
  --no-clip                             # bbox prefilter only, skip the exact clip (faster)
```

Multi-feature FeatureCollections are unioned into one boundary.

As a library:

```python
from duckoverture import build
build("boundary.geojson", "area.duckdb", types=["transportation"])
```

## Visualise

```bash
duckoverture viz data/sodermalm.duckdb
```

```
4,203 road segments  |  bridges 84 · tunnels 243 · levelled 303 · speed limits 965
wrote viz/sodermalm.html
```

Maps land in `viz/`, not `data/` — they are outputs, not data, so the two clean and sync
independently. `-o` overrides.

A self-contained [roadstyle](https://pypi.org/project/roadstyle/) map — one file, opens from disk.

**roadstyle styles Overture with no palette changes.** Overture's `class` values are OSM-derived
(`residential`, `footway`, `secondary`, …), so the mapping is a *rename*, not a translation table.
The only work is unpacking three nested columns into the flat ones roadstyle reads for the
grade-separation sandwich:

| Overture | → | roadstyle |
|---|---|---|
| `road_flags` contains `is_bridge` / `is_tunnel` | → | `bridge` / `tunnel` |
| `level_rules[1].value` | → | `layer` |
| `speed_limits[1].max_speed.value` | → | `maxspeed_kmh` |

Sub-range rules (`between`) collapse to the segment's first rule — roadstyle styles whole features,
so a segment that is a bridge for part of its length is drawn as a bridge. Splitting at sub-range
boundaries belongs in the flatten stage.

Needs the extra: `pip install "duckoverture[viz]"`.

## Install

```bash
pip install -e .            # core: duckdb + click only
pip install -e ".[viz]"     # + roadstyle & geopandas for `duckoverture viz`
```

The core needs only `duckdb` and `click`. DuckDB's `spatial` and `httpfs` extensions are installed on first
run.

## Tests

```bash
pytest tests/                                        # unit, offline
DUCKOVERTURE_NETWORK_TESTS=1 pytest tests/           # + live S3 extraction
```

## Status

v0.1.0 — the raw layer works. Next: `duckoverture flatten` to derive scalar convenience columns
(`maxspeed_kmh`, `is_oneway`, `lvl`) from the nested structs, as a second stage on top of the raw
tables.
