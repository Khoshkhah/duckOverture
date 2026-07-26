# Recovering the one-way that Overture drops on roundabouts

**Status:** **IMPLEMENTED** 2026-07-25 via Overpass (`duckoverture roundabouts`). Measured on
Nacka, Overture release `2026-07-22.0`.

## Result

```
duckoverture roundabouts nacka.duckdb
  osm.roundabout_way  193 roundabout ways
  217 segments in this extract sit on one
duckoverture flatten nacka.duckdb -m edges
```

| | before | after |
|---|---:|---:|
| roundabout edges marked one-way | 33 / 190 (**17%**) | **233 / 233 (100%)** |

**Precision 100%, verified twice.** 190 of the 233 are confirmed by duckOSM's `junction` tag. The
other 43 looked like false positives until checked: their 25 underlying OSM ways were queried
directly against Overpass and **all 25 are `junction=roundabout`**. duckOSM's Nacka snapshot is
simply older than live OSM. Nothing was wrongly marked one-way — which was the failure mode that
ruled out the geometric approach.

Row counts are unchanged (30,865 edges), so splitter parity is unaffected.

## Problem

In OSM, `junction=roundabout` **implies** `oneway=yes`; no explicit tag is needed and every
consumer is expected to apply the implication. Overture has no roundabout concept — nothing in
`class`, `subclass` or `road_flags` marks one — and the implication is never materialised into
`access_restrictions`. So the one-way-ness silently disappears.

Measured on Nacka, joining Overture segments to their OSM source ways (see below):

| | edges | one-way |
|---|---:|---:|
| duckOSM (`junction='roundabout'`) | 210 | **210 (100%)** |
| duckOverture `flat.segment`, same roads | 190 | **33 (17%)** |

**3,511 m of roundabout comes out two-way.** A router on `flat.segment` sends vehicles the wrong
way round. This is the last known driving-correctness gap now that direction, turn restrictions
and access closures are handled.

Reported upstream as [OvertureMaps discussion #358](https://github.com/orgs/OvertureMaps/discussions/358)
— open since April 2025, no maintainer reply. Not fixed in `2026-07-22.0`: an exhaustive search of
the release finds zero occurrences of "roundabout" or "junction".

## The data we actually have

The decisive finding. `sources[].record_id` is the **OSM way id**, verbatim:

```json
{"dataset": "OpenStreetMap", "record_id": "w288602249@7", "license": "ODbL-1.0"}
```

`w<way_id>@<version>`. On Nacka:

- **14,594 of 14,882 road segments (98.1%)** carry one
- 288 segments (1.9%) are TomTom-sourced and carry no OSM id
- the crosswalk references 14,894 distinct OSM ways

So the roundabout question does not have to be *inferred from shape*. It can be **looked up**.

## Approach: exact crosswalk, geometry only as fallback

```
sources[].record_id  ->  OSM way id  ->  junction=roundabout?  ->  force oneway
```

Measured against duckOSM's `junction` tag as ground truth, within the Nacka bbox:

| approach | recall | precision | thresholds to tune |
|---|---:|---:|---|
| **crosswalk join** | **91%** (165/182 ways) | **exact** | none |
| polygonise + circularity | 77% | **22–35%** | 2 |

Precision is what kills the geometric approach. At its best setting (`area < 3000 m²`,
circularity `> 0.7`) it returns 85 faces of which **30 are roundabouts** — the rest are small
circular city blocks, parking loops and service courtyards. Marking 55 ordinary blocks one-way is
a worse failure than the bug being fixed: it makes correct roads unroutable in one direction.

The 9% the crosswalk misses are ways Overture dropped or merged, so no id survives to join on.

### Where the OSM side comes from

Three options, in order of preference. Whichever is used, the result is the same column.

1. **Overpass, one query per area.** `way[junction=roundabout](bbox)` returns just the roundabout
   way ids — a few hundred for a city, not a PBF. Small, no local OSM dependency, but needs
   network at flatten time.
2. **A duckOSM database for the same area**, joined on `edges.osm_id`. Exact and offline; this is
   what the measurements above used. Adds a cross-project dependency, so it should be an optional
   argument, never a required one.
3. **A user-supplied way-id list.** The escape hatch: `--roundabout-ways ids.txt`.

### Proposed shape — which layer gets what

Roundabout-ness is **not derived from the raw data** the way `oneway` or `is_bridge` are. It is
fetched from outside. That makes three layers, not two, and the middle one is easy to miss:

| layer | holds | table |
|---|---|---|
| raw Overture — a faithful mirror, never written to | `sources[].record_id`, the OSM way id | `transportation.segment` *(already there)* |
| fetched input — what OSM told us | the set of roundabout way ids | **`osm.roundabout_way`** *(new)* |
| derived | `is_roundabout`, repaired `oneway` | `flat.segment` |

**The raw table gains nothing.** Its contract is that every column is exactly as Overture
published it; that is what lets `flat.*` be re-derived after any code change without
re-downloading. A computed column there would destroy the ability to tell ours from theirs.

**The fetched ids must be stored, not held in memory.** If the Overpass lookup lived only inside
`flatten`, every re-flatten would need the network — and flatten is run repeatedly, once per mode.
Persisting them means one fetch per area, after which `flatten` stays offline and re-runnable, and
the database records what it was told (with the query and fetch time, so a stale answer is
visible).

So it becomes a separate step, mirroring how `build` and `flatten` already split:

```bash
duckoverture roundabouts area.duckdb        # fetch once, writes osm.roundabout_way
duckoverture flatten     area.duckdb -m edges   # offline, reads it if present
```

`flat.segment` then gains one column, `is_roundabout BOOLEAN`, and `oneway` becomes
`oneway OR is_roundabout`. `direction` stays as-is where already known.

Nothing is inferred without a source. **With no `osm.roundabout_way` table, `is_roundabout` is
`false` everywhere and behaviour is exactly as today** — the column exists so the caveat is
visible in the data rather than only in the README.

## Rejected

- **Geometric detection as the primary path.** 22–35% precision, two thresholds that would need
  retuning per city. Measured, not assumed — the sweep is above. Keep it in the back pocket for
  the 1.9% TomTom-only segments, but it is not good enough to switch on by default.
- **Closed-ring detection (`ST_IsClosed`).** Cheapest possible test, and it finds **zero**
  roundabouts: Overture splits every ring into multiple segments, so no segment self-closes.
- **Cycle enumeration in the connector graph.** More principled than polygonising, but still a
  shape heuristic with the same precision ceiling, and much slower. Not worth it when an exact id
  join is available.
- **Waiting for Overture to fix it.** Discussion #358 has been open 15 months with no reply.
- **Repairing it inside `viz`.** The bug is in the data, not the rendering; a router reading
  `flat.segment` directly would still be wrong.

## Impact

- Nacka: 190 edges / 3,511 m go from 17% one-way to 100%.
- Södermalm: **no change** — it has zero roundabouts. Any test of this needs Nacka or similar.
- `edges`-mode row counts are unaffected: `oneway` is already in `_ATTRS`, so a segment that
  changes roundabout status mid-way would already have been cut there. Splitter parity with
  `OvertureMaps/transportation-splitter` is unaffected, because that tool does not model
  roundabouts either.
- New optional dependency only when the Overpass path is used at flatten time.

## Open questions for review

1. **Which source do we build first** — Overpass (standalone, needs network) or duckOSM (offline,
   cross-project)? Overpass keeps duckOverture self-contained, which has been the point of the
   project so far. Either way the fetched ids land in the same `osm.roundabout_way` table, so the
   second source can be added later without changing `flatten`.
2. **Should `direction` be derived for repaired roundabouts?** Knowing a ring is one-way does not
   say which way. It can be recovered from traversal order around the ring plus drive-on-right,
   but that is a second algorithm and it is only needed if the consumer cares about chevrons or
   turn costs, not for banning wrong-way traversal.
3. **Do `mini_roundabout` and `teardrop` count?** Neither implies `oneway` in OSM the way
   `junction=roundabout` does. **Decided: excluded.** The Overpass query asks for
   `junction=roundabout` only.

## Resolved during implementation

- **Overture merges OSM ways**, so `sources` ranges are load-bearing: 1,384 of 14,594 Nacka road
  segments (9.5%) reference more than one way, and a segment can be a roundabout for part of its
  length. `is_roundabout` therefore resolves through `_at_mid('sources')` in the split modes, not
  a whole-column check.
- **`-m segment` uses dominance, not presence** — deliberately the opposite of `motor_access`.
  Wrongly closing a road only makes a router avoid it; wrongly marking one one-way makes it
  untraversable in the other direction, so over-reporting is the dangerous side here.
- **`direction` is left as `forward`.** `junction=roundabout` implies one-way *in the drawn
  direction*, and Overture preserves OSM's geometry direction in 155 of 169 spot-checks, so
  chevrons point the right way. The residual ~8% is unverified — the comparison is crude because
  duckOSM contracts edges differently.
- **The main Overpass endpoint 504s often**, including on the first run here, so `fetch` falls
  through to mirrors rather than making the user find `--endpoint` in a stack trace. Mirrors lag
  each other slightly (193 ways vs 195 from a mirror minutes apart).
