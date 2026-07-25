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
SELECT round(sum(ST_Length_Spheroid(ST_FlipCoordinates(geometry))))
FROM transportation.segment WHERE subtype = 'road';   -- 290,590 m
```

**`ST_FlipCoordinates` is not optional.** DuckDB's spheroid functions read coordinates as
**(lat, lon)**; Overture geometry is (lon, lat). Calling `ST_Length_Spheroid` directly overstates
Södermalm's road network by 54% — 448,588 m against a true 290,590 m, which a projected EPSG:3006
measurement independently confirms to within 8 m over 290 km.

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
(`residential`, `footway`, `secondary`, …), so roadstyle resolves them directly — the column keeps
Overture's own name and roadstyle is pointed at it with `highway_col="class"`.

`subclass` is resolved per piece from `subclass_rules`, **not** from Overture's scalar `subclass`
column: that one is only populated when the subclass covers the whole segment, so it is NULL on
every one of the 36 segments whose subclass is ranged. The report shows `subclass_rules` beside it
as the evidence, with the range each rule applies to.
The only work is unpacking three nested columns into the flat ones roadstyle reads for the
grade-separation sandwich:

| Overture | → | roadstyle |
|---|---|---|
| `class` | → | `class` *(kept — roadstyle is told via `highway_col`)* |
| `names.primary` | → | `name` |
| `road_flags` contains `is_bridge` / `is_tunnel` | → | `is_bridge` / `is_tunnel` |
| `level_rules[1].value` | → | `layer` |
| `speed_limits[1].max_speed.value` | → | `maxspeed_kmh` |
| `access_restrictions` heading denial | → | **`oneway`** |

**Overture has no `oneway` column.** A one-way street is a *heading-scoped denial*:
`{"access_type": "denied", "when": {"heading": "backward"}}` means traffic runs forward only.
Mode matters — a denial scoped to `[foot, bicycle]` is a footway rule, not a one-way street, so
only all-mode or motorised denials count. Södermalm: **644 of 4,203**.

Segments whose *forward* travel is denied get `ST_Reverse`d, because roadstyle draws the direction
chevrons **along the geometry** — without the flip they would point into oncoming traffic.

Sub-range rules (`between`) collapse to the segment's first rule — roadstyle styles whole features,
so a segment that is a bridge for part of its length is drawn as a bridge. Splitting at sub-range
boundaries belongs in the flatten stage.

### Report mode — click a road, see everything

```bash
duckoverture viz data/sodermalm.duckdb --report
```

```
4,203 road segments  |  bridges 84 · tunnels 243 · levelled 303 · speed limits 965
  report mode: 28 fields per road in the click panel
wrote viz/sodermalm_report.html
```

Clicking a road docks a **side panel** of 23 fields — **every one a scalar**:

```
segment_id           9ca6940d-4e06-43e0-bcdd-b8968b8b2fc4
class                secondary          name       Stadsgårdsleden
maxspeed_kmh         30                 oneway     True
surface              paved              length_m   1516.0
layer                -1                 is_tunnel  true
n_connectors         5                  routes     222
all_speed_limits     30                 flags      is_tunnel
access               denied / denied / denied backward
source_dataset       OpenStreetMap      overture_version  5
```

Values come from `flat.segment`, so the panel and the styling agree — both use the dominant rule.
Overture's nested columns are **summarised** rather than dumped: `connectors` → a count,
`speed_limits` → the values joined (`"40 / 60"`), `road_flags` → the flag names. Bridge and
tunnel keep their `is_` prefix and stay BOOLEAN, matching `flat.segment` rather than roadstyle's
default `'yes'`/null strings — `bridge_col` / `tunnel_col` are passed through so the
grade-separation sandwich still works. A wall of
`[{"connector_id":"a0c1c021-…"}]` is not a report.

`--report` requires `duckoverture flatten` first; without it the panel would show `[1]`-derived
values that contradict the map. Add `--raw` to append the untouched nested columns as JSON when
the evidence behind a resolved value is what you want.

Needs the extra: `pip install "duckoverture[viz]"`.

## Flatten

```bash
duckoverture flatten data/sodermalm.duckdb
```

```
  flat.segment  4,203 segments -> 4,203 rows [one row per segment (dominant rule)]  0.0s
  length 290,590 m vs raw 290,590 m  (drift 0.0000%)
```

Overture attaches attributes to **ranges** along a segment instead of splitting the road:

```json
speed_limits: [{"max_speed": {"value": 40}, "between": [0.000, 0.070]},
               {"max_speed": {"value": 60}, "between": [0.070, 0.927]},
               {"max_speed": {"value": 40}, "between": [0.927, 1.000]}]
```

Reading that with `[1]` — the obvious shortcut — labels a **1,639 m road 40 km/h** on the strength
of its first 115 m. `flat.segment` takes the **dominant** rule instead, the one covering most of
the segment: **60 km/h**, true of 86% of it.

One row per segment, so it joins 1:1 on `segment_id`:

```
piece_id · segment_id · class · subclass · name · level · surface · width_m
maxspeed_kmh · is_bridge · is_tunnel · is_link · oneway · direction · motor_access
max_height_m · max_weight_t · is_roundabout · length_m · geometry
```

### Three modes

```bash
duckoverture flatten data/sodermalm.duckdb -m edges
```

| `-m` | Södermalm rows | cuts where | use it for |
|---|---:|---|---|
| `segment` *(default)* | 4,203 | nowhere — dominant rule per segment | joining 1:1 on `segment_id` |
| `attrs` | 4,863 | a **resolved attribute changes** | per-stretch accuracy |
| `edges` | 10,167 | that, **plus every junction** | routing |

Cutting at every `between` boundary instead would over-fragment — 1,672 sub-20 m rows on
Södermalm, mostly crosswalk edges of a footway. `attrs` cuts on the *resolved* value, so a
segment whose three speed-limit ranges all flatten to 60 stays one row: 3,827 of 4,203 segments
never split at all.

`edges` gives every row exactly two junctions, with `from_connector_id` / `to_connector_id`
(synthetic `<segment_id>@<lr>` where a cut is an attribute change rather than a junction).

**Total length is conserved in all three modes**, to the metre — that is the invariant the build
checks.

`duckoverture viz` prefers `flat.segment` when it exists, so the map uses the corrected values.

### Validated against Overture's own splitter

Overture publishes [`transportation-splitter`](https://github.com/OvertureMaps/transportation-splitter),
a PySpark + Sedona reference implementation of exactly this. Run on the same Södermalm extract with
`split_at_connectors=True`, against `-m edges`:

```
ours 10,167   official 10,167   diff +0
identical cut-point sets: 4,198 / 4,203  (99.88%)
```

Three things had to match to get there, and each was a real bug on our side:

- **`sources` is a ranged column.** It is easy to overlook — it carries provenance, not routing
  attributes — but it has a `between` on every road. Omitting it left us **1,117 pieces short**.
- **Only split at *resolvable* connectors.** 98 of 12,672 connector references on Södermalm point
  outside the clip boundary. Splitting there mints nodes we hold no record for; the official
  splitter skips them, so we do too.
- **`edges` is deliberately unmerged.** Adjacent pieces with identical attributes stay separate
  rows, which is what makes the comparison row-for-row. Use `-m attrs` if you want them merged.

### Turn restrictions get renumbered too

A turn restriction reads *"from segment A, via connector c, you may not enter segment B"*.
Splitting breaks **both** halves of that sentence: it belongs to only one piece of A — the one
that touches `c` — and B is now several pieces, of which exactly one is the manoeuvre. Leave it
unresolved and a router either bans the whole road, hundreds of metres of it, or cannot apply the
ban at all.

`-m edges` renumbers both ends into `flat.turn_restriction`:

```
  flat.turn_restriction  54 transitions -> 54 renumbered onto pieces (58 steps)  0.0s
```

```
from_piece_id     1227298b-…-d26b0a59a403#5     -- the edge you are on
tr_no  seq_no     2  1                          -- 2-step (via-way) restriction, first step
via_connector_id  ef18293a-…-d5853615801a       -- the junction crossed
to_piece_id       aabe0b25-…-ffd69e2d1e9f#1     -- the edge banned  ← renumbered
to_segment_id     aabe0b25-…-ffd69e2d1e9f       -- what Overture said, kept for traceability
final_heading · when_heading · during · mode
```

One row per step, so the common case is `seq_no = 1` and via-way restrictions carry the whole
chain. `during` and `mode` travel with it — a restriction that only applies at rush hour must not
read as absolute.

Against the official splitter: **58 steps vs 58, an exact multiset match** — same owner piece,
same junction, same referenced piece, on every one. 26 of the 46 referenced segments are split
into pieces (one into 15), so this is doing real work rather than passing ids through.

Two restrictions resolve to `to_piece_id = from_piece_id`. Those are **U-turn bans** — reverse at
the junction you arrived from — not a bug.

Chains that only partly resolve are **dropped**, as the official splitter drops them; on a clipped
extract those are manoeuvres leaving the boundary. A partly-resolved chain is a guess, not a
restriction.

## Known limits of the source data

These are Overture's, not this tool's — `duckoverture` reports faithfully what the release
contains. They matter because they are invisible unless you look.

### Roundabouts lose their one-way ⚠️

In OSM, `junction=roundabout` **implies** `oneway=yes` with no explicit tag. **Overture has no
roundabout concept** — nothing in `subclass` or `road_flags` marks one — and the implication is
never materialised into `access_restrictions`. Measured on Nacka (210 roundabouts):

| | roundabout segments | marked one-way |
|---|---:|---:|
| OSM / duckOSM | 210 | **210 (100%)** |
| Overture | 208 | **52 (25%)** |

**75% come out two-way.** Fixed — one extra command:

```bash
duckoverture roundabouts area.duckdb    # then flatten as usual
```

```
overpass  bbox 18.1111,59.2484 -> 18.3113,59.3265
  osm.roundabout_way  193 roundabout ways
  217 segments in this extract sit on one
```

Nacka roundabout edges go from **17% one-way to 100%**, and `flat.segment` gains `is_roundabout`.

**It is a lookup, not a guess.** `sources[].record_id` is the OSM way id verbatim
(`w288602249@7`), present on **98.1% of road segments** — so the roundabout tag Overture dropped
can be fetched back from OSM by id. Precision is 100%: of 233 flagged edges, 190 are confirmed by
duckOSM and the other 43 were checked against live OSM, where all 25 underlying ways really are
`junction=roundabout`.

Detecting circles geometrically was tried first and **rejected** — 22–35% precision, because small
city blocks, parking loops and courtyards are also small circles. It would have marked ~55 ordinary
streets one-way, which is worse than the bug. `ST_IsClosed` finds zero, since Overture splits every
ring. See [docs/design/roundabout_oneway_recovery.md](docs/design/roundabout_oneway_recovery.md).

Without running `roundabouts`, `is_roundabout` is false everywhere and behaviour is unchanged — the
column makes the caveat visible in the data rather than only here.

### "Two-way" is inferred from silence

Overture only ever states direction *negatively* — `access_type: denied` with a `heading`. There is
no positive "this is two-way". So `oneway = false` conflates:

| | Södermalm |
|---|---:|
| no `access_restrictions` at all | 3,195 |
| has restrictions, none directional | 364 |
| **has a heading-scoped denial** → `oneway = true` | **644** |

3,559 roads are two-way *by assumption*. That is the OSM convention and it is usually right, but
the data cannot distinguish "known two-way" from "nobody said".

### Access is more than direction — now read

`access_restrictions` carries closures and vehicle limits alongside the heading denials that make
a road one-way. `flat.segment` used to mine it **only** for `oneway`, so 132 edges (4.9 km) that
are flatly closed read as ordinary two-way streets. Three columns now carry the rest:

| column | Södermalm (`-m edges`) |
|---|---|
| `motor_access` — `yes` / `restricted` / `no` | 10,022 · 86 · **59** |
| `max_height_m` | 100 edges, 1.8–14.7 m |
| `max_weight_t` | 22 edges, 3.5–14.0 t |

The trap is the opposite error. **66 of the 118 non-directional motor denials are vehicle size
limits** — "no vehicles over 3 m" is a lorry sign, not a closed road. Counting those as closures
would have removed 62 perfectly drivable segments, so `motor_access` ignores any denial scoped to
`when.vehicle` and reports the limit separately.

`restricted` is the OSM `access=no` + `motor_vehicle=destination` pattern: a denial paired with an
`allowed` rule, or one scoped to `using` (`at_destination`, `as_customer`) or `during`. Reachable,
but not through-routable — 44 segments carry it.

Closures respect `between` like everything else, so a segment closed for its first 98% keeps its
open tail as a separate `yes` edge. In `-m segment` the resolution is deliberately **presence**,
not dominance: missing a closure routes a car down a closed road, over-reporting one only avoids
it, so a closure anywhere closes the whole row.

### No lane counts

There is no `lanes` column anywhere. `width_rules` gives metres, not a lane count.

### Attributes apply to ranges, not whole roads

See [Flatten](#flatten) — this is handled, but it is why `flat.segment` exists at all.

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

v0.2.0 — raw extraction, flattening and rendering all work. `-m edges` matches Overture's own
splitter exactly on Södermalm, geometry and turn restrictions both.

Next: a `flat.connector` node table, so the extract can feed a router directly. Note the
[roundabout caveat](#roundabouts-lose-their-one-way-) before routing on this.
