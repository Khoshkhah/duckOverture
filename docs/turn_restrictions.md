# `flat.turn_restriction` — where every column comes from

One row per turn restriction: *"from this piece, via something, you may not enter that piece."*
Built by `duckoverture flatten -m routing|edges`. This page separates what Overture ships from
what this tool derives, because the two fail differently: raw columns are only as good as the
release; derived columns are only as good as the resolution rules documented here.

All measurements: Södermalm, release 2026-07-22.0 — 54 restrictions (50 via a connector, 4 via a
way, 2 U-turn bans).

## The raw material

Overture attaches restrictions to the **from** segment, as anonymous entries in
`transportation.segment.prohibited_transitions`:

```json
{"sequence":      [{"connector_id": "b876a718-…", "segment_id": "c6db78b3-…"},
                   {"connector_id": "63056bf8-…", "segment_id": "74008e74-…"}],
 "final_heading": "forward",
 "when":          {"heading": "forward", "during": null, "using": null,
                   "recognized": null, "mode": null, "vehicle": null},
 "between":       null}
```

Each `sequence` step is *"cross this connector, enter this segment"* — and that is ALL the
format contains: an ordered path of hops. There is no `via` field; **"via" is positional**, our
name (and OSM's) for every hop before the last. The **last** step's segment is the banned
target; earlier steps (rare — 4 of 54 here) are the legal prefix the manoeuvre drives first.
The one thing that is semantics rather than naming: the hops are **not independent bans** —
each hop in the path is individually legal, only *completing the whole path* is forbidden
(see the decomposition proof below). This mirrors OSM's restriction relation, but two things are already lost upstream,
before this tool runs:

- **The OSM relation id.** `sources[].record_id` names contributing ways, never the restriction
  relation. (We verified the four via-way rules against live OSM by Overpass lookup —
  e.g. [r8407604](https://osm.org/relation/8407604) — precisely because no join exists.)
- **The restriction type.** `no_left_turn` vs `no_u_turn` is OSM's `restriction=*` tag; Overture
  keeps only the effect (the banned sequence), not the name.

## Column provenance

| column | source | how |
|---|---|---|
| `tr_id` | **ours** | A small integer (`row_number()` over the group keys) — deliberately meaningless, because its job is to be *quoted* ("ban 12 looks wrong"; the viz cards show it as `tr 12`). Unique, and deterministic for a given db+mode. Identity and the raw trace live in the `(from_piece_id, tr_no)` columns instead. Rejected: uuid-composites (unquotable, twice) and a content hash (not unique — two raw rules can carry an identical path and conditions). Overture itself gives restrictions no identifier at all. |
| `from_segment_id` | raw | The segment whose `prohibited_transitions` carries the rule. Verbatim. |
| `tr_no` | raw (position) | 1-based index of the rule in that segment's list. Not a stored value — it names a position, so `prohibited_transitions[tr_no]` on `from_segment_id` **is** the raw rule. The pair is the permanent trace back. |
| `from_piece_id` | **derived** | Which piece of the from-segment owns the ban. Resolution: `when.heading = 'forward'` → the piece whose **end** is the first junction of the sequence; `'backward'` → whose **start** is; unscoped → either. |
| `via_type` | **derived** | `'connector'` when the sequence has one step (the classic node-pivot ban), `'piece'` when it has more (via-way form). Overture never states this; it is implied by sequence length, and matches OSM's via-node / via-way distinction. |
| `via_id` | **derived** | Convenience: the via connector for `'connector'` rows, the (first) via piece for `'piece'` rows. Duplicates the columns below; exists so the common case is one scalar. |
| `via_piece_ids` | **derived** | The via way *segments* are raw (below); their resolution onto **pieces** is ours: a via leg must span both of its junctions — enter at one, leave at the other. NULL for connector vias. |
| `via_segment_ids` | raw | `sequence[i].segment_id` for every step except the last, in order. Verbatim. |
| `via_connector_ids` | raw | `sequence[i].connector_id` for every step, in order — every junction the manoeuvre crosses. Verbatim. |
| `to_piece_id` | **derived** | Which piece of the banned segment is the manoeuvre. Middle steps must span both junctions; the final step has no next junction, so `final_heading` picks the side: `'forward'` → the piece **starting** at the last via connector, else the piece ending there. |
| `to_segment_id` | raw | The last step's `segment_id`, verbatim — kept so every derived resolution can be audited against what Overture said. |
| `final_heading` | raw | Verbatim. |
| `when_heading` | raw | `when.heading`, verbatim. |
| `during` | raw | `when.during`, verbatim (0 non-null here — but a rush-hour-only ban must not read as absolute, so it always travels along). |
| `mode` | raw | `when.mode`, verbatim (0 non-null here). |

Rule of thumb: **every `*_segment_id` and `*_connector_id` column is Overture's, verbatim; every
`*_piece_id` column and both id/type columns are ours.** The whole point of the table is the
piece resolution — segment-level restrictions are unusable after splitting, when "segment B" has
become several pieces of which exactly one is the banned manoeuvre.

## What is deliberately NOT read

- **`between`** — a restriction can in principle be range-scoped to part of the from-segment.
  Not read: 0 of 54 rules here carry it, and the owner-piece rule (must touch the first junction)
  already enforces the only geometrically sensible scope. If a release ever populates it, the
  owner resolution is where it would apply.
- **`when.using`, `when.vehicle`, `when.recognized`** — 0 non-null here. Unread until real data
  shows what they look like; adding untested columns is how silent misreadings ship.

## Worked example — resolving the JSON above onto pieces

The enabling fact: after splitting, **every piece's two endpoints are connectors**
(`from_connector_id` / `to_connector_id`), and pieces keep the parent segment's geometry
direction. So "which piece?" always reduces to "which piece has the right junction at the right
end?", and headings translate to end-vs-start.

The rule above sits on segment `d70ee40d…` (Londonviadukten), which flatten cut into pieces #1
and #2. Three resolutions:

1. **`from_piece` — who owns the ban.** `when.heading = 'forward'` means the ban applies to
   traffic travelling the from-segment in geometry direction; such traffic arrives at the
   sequence's first junction (`b876a718`) at a piece's **end**:

   ```sql
   CASE when_heading
     WHEN 'forward'  THEN f.to_connector_id   = seq[1].connector_id  -- arrive at the END
     WHEN 'backward' THEN f.from_connector_id = seq[1].connector_id  -- arrive at the START
     ELSE seq[1].connector_id IN (f.from_connector_id, f.to_connector_id)  -- unscoped
   END
   ```

   Only `d70ee40d…#2` ends at `b876a718` → it owns the ban. Piece #1 carries nothing.

2. **Via legs — must span both junctions.** A middle step knows where it is entered (its own
   connector) *and* left (the next step's connector), so no heading is needed — both endpoints
   are pinned:

   ```sql
   via_connector_id      IN (f.from_connector_id, f.to_connector_id)
   AND next_connector_id IN (f.from_connector_id, f.to_connector_id)
   ```

   `c6db78b3…#1` runs `b876a718 → 63056bf8` → the via piece.

3. **`to_piece` — the last step has no next junction, `final_heading` picks the side.**
   `'forward'` means the banned segment would be traversed in geometry direction, i.e. entered
   at its **start**:

   ```sql
   CASE final_heading
     WHEN 'forward' THEN f.from_connector_id = via_connector_id  -- entered at its START
     ELSE                f.to_connector_id   = via_connector_id  -- entered at its END
   END
   ```

   `74008e74…#1` starts at `63056bf8` → the banned edge.

Every step must resolve to exactly one piece or the whole chain is dropped (see below); the
middles then fold into `via_piece_ids`, the last into `to_piece_id`.

## Chains fold to one row

A via-way restriction arrives as a multi-step sequence. Earlier versions of this table kept one
row per step, and it read as several bans — Londonviadukten's 2-step `no_left_turn` showed up as
"Londonviadukten → Londonviadukten" at its first junction, as if continuing along the street were
banned. It is not: **only completing the whole chain is prohibited**; the via legs are legally
driven by every other movement, and a via-way ban is *not* decomposable into via-connector bans
(the Londonviadukten stub has 2 entries × 2 exits = 4 paths, of which OSM bans exactly 1 — any
pairwise ban kills 2). So the table folds each restriction to one row, and routers should ban the
path `from_piece_id → via_piece_ids… → to_piece_id`, nothing less, nothing more.

Chains that only partly resolve are **dropped**, as Overture's own splitter drops them (0 here;
on clipped extracts they are manoeuvres leaving the boundary). A partly-resolved chain is a
guess, not a restriction.

## For routers: the piece-only reading

A router's graph is pieces and connectors — segments do not exist in it. The routable content of
this table is exactly:

```
from_piece_id → via_piece_ids… → to_piece_id      -- the forbidden path (edges)
via_connector_ids                                 -- the nodes between its edges
during · mode                                     -- conditions on the ban
```

Two consequences:

- **The `*_segment_id` columns and `tr_no` are provenance, not routing data** — they exist so
  every resolution can be audited against the raw layer, and a router never reads them.
- **Neither are the heading columns.** `when_heading` / `final_heading` were consumed during
  piece resolution — their whole job was picking *which piece*, and once picked, direction is
  implicit in the path: the ban applies when traversing `from_piece` **toward**
  `via_connector_ids[1]` and entering `to_piece` **away from** the last connector. A piece plus
  the junction you exit through fully determines direction, so the piece path is
  self-directional. The headings stay as the audit trail of how resolution decided.

## Validation

- Renumbering matches `OvertureMaps/transportation-splitter` exactly: same owner piece, same
  junction, same referenced piece on all 58 resolved steps (an exact multiset match, measured on
  the step-level form this table folds).
- Tested invariants (`tests/test_integration.py`): the owner piece touches the first junction;
  the banned piece touches the last; every via leg spans its two junctions; one row per
  restriction; `via_type`/`via_id`/`via_piece_ids`/`via_connector_ids` mutually consistent.
- The 2 rows with `to_piece_id = from_piece_id` are U-turn bans, not bugs.
