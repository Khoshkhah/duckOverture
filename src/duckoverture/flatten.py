"""Stage 2 — derive flat, scalar tables from the raw nested layer.

Overture attaches attributes to **ranges** along a segment rather than splitting the road:

    level_rules: [{"value": 1, "between": [0.430, 0.969]}]

means *"elevated for the middle 54% of this segment"*. Reading such a column with `[1]` — take the
first rule — is wrong whenever a segment carries more than one, and silently so: a 4,097 m road
with a 324 m tunnel in it comes out labelled "tunnel" end to end.

**Default: one row per segment, no splitting.** Each ranged column collapses to the value of its
**dominant** rule — the one covering most of the segment. That fixes the first-rule bug without
fragmenting the network: the 4,097 m road above comes out at ground level, which is true of 92%
of it.

``split=True`` cuts instead, at every point where a *resolved attribute changes* (not at every
`between`, which over-fragments). Measured on Södermalm: 4,203 segments → 4,863 pieces, of which
3,827 stay whole and only 376 split. Correct, but it invents 1,672 sub-20 m rows and cuts roads at
crosswalk and surface boundaries — rarely what a renderer or router wants. Opt in when per-stretch
accuracy matters more than a stable one-row-per-segment join.

The raw layer is untouched either way, so this can be re-derived after any change without
re-downloading.

Invariant: **total length is conserved** — 448,588 m before and after, to the metre, in both modes.
"""
from __future__ import annotations

import time
from pathlib import Path

import duckdb

from .roundabouts import have_roundabouts

# Rule columns that carry a `between` range. Their breakpoints define where a segment is cut.
# `sources` is easy to overlook and matters most: it carries provenance ranges on EVERY road, and
# omitting it left us 1,117 pieces short of Overture's own splitter, which considers all columns.
RANGED = ["level_rules", "speed_limits", "access_restrictions", "road_flags",
          "road_surface", "subclass_rules", "width_rules", "sources"]

# Two split points closer than this along the segment are the same point. Matches
# lr_split_point_min_dist_meters in OvertureMaps/transportation-splitter.
MIN_SPLIT_M = 0.01


def _len(geom: str = "geometry") -> str:
    """Geodesic length in metres.

    ST_Length_Spheroid reads coordinates as (LAT, LON) — DuckDB's spheroid functions all do, and
    Overture geometry is (lon, lat), so calling it directly overstated Södermalm's road network by
    54% (448,588 m against a true 290,590 m). ST_FlipCoordinates puts them in the order it wants;
    the result then agrees with a projected EPSG:3006 measurement to 8 m over 290 km.

    The length-conservation check could never catch this: both sides of it used the same function,
    so a consistent error cancelled exactly.
    """
    return f"ST_Length_Spheroid(ST_FlipCoordinates({geom}))"

# modes whose access denial constitutes a one-way street (a foot/bicycle rule does not)
_MOTOR_SQL = "['motor_vehicle','car','truck','hgv','bus','motorcycle','taxi','hov']" 

# `between IS NULL` means the rule covers the whole segment.
def _at_mid(col: str) -> str:
    return (f"list_filter({col}, x -> x.between IS NULL OR "
            f"(x.between[1] <= mid AND x.between[2] >= mid))")


def _dominant(col: str) -> str:
    """The rule covering the most of the segment — the representative value when not splitting.

    ``[1]`` (first rule) is the obvious shortcut and it is wrong: a 4,097 m road whose first rule
    is a 324 m tunnel comes out labelled "tunnel" end to end. Ordering by covered fraction picks
    the value true of most of the road. Whole-segment rules (``between IS NULL``) cover 1.0 and so
    win outright, which is correct.
    """
    return (f"list_sort(list_transform({col}, x -> {{"
            f"'cov': CASE WHEN x.between IS NULL THEN 1.0 "
            f"ELSE x.between[2] - x.between[1] END, 'v': x}}), 'DESC')[1].v")


def _flag_coverage(flag: str) -> str:
    """Fraction of the segment on which `flag` is set, summing every range that carries it.

    Flags cannot be read with `list_contains(...)` over the whole column: that answers "does this
    appear anywhere", so Söderledstunneln — a 1,470 m tunnel ending in a 106 m bridge — came back
    is_bridge AND is_tunnel, which is incoherent and feeds contradictory inputs into roadstyle's
    grade-separation sandwich. Summing coverage lets the flags be resolved by dominance, the same
    rule the other ranged columns use.
    """
    return (f"coalesce(list_sum(list_transform(list_filter(road_flags, "
            f"x -> list_contains(x.values, '{flag}')), "
            f"x -> CASE WHEN x.between IS NULL THEN 1.0 "
            f"ELSE x.between[2] - x.between[1] END)), 0.0)")


def _denial(col: str, conditional: bool) -> str:
    """A denial a driver actually has to obey.

    Three things must be excluded or this reads far more closures than exist:

    - ``heading IS NOT NULL`` — that is a one-way street, already handled by `oneway`.
    - ``vehicle IS NOT NULL`` — "no vehicles over 3 m" is a lorry sign, not a closed road. 66 of
      the 118 non-directional motor denials on Södermalm are these, and counting them as closures
      would have removed 62 perfectly drivable segments.
    - a foot/bicycle-scoped mode — a footway rule says nothing about cars.

    ``conditional`` splits what remains: an unqualified denial closes the road, one scoped to
    ``using`` (at_destination, as_customer) or ``during`` only restricts it.
    """
    cond = ('(x."when"."using" IS NOT NULL OR x."when".during IS NOT NULL)' if conditional
            else '(x."when"."using" IS NULL AND x."when".during IS NULL)')
    return (f"""len(list_filter({col}, x -> x.access_type = 'denied'
            AND x."when".heading IS NULL AND x."when".vehicle IS NULL
            AND (x."when"."mode" IS NULL OR list_has_any(x."when"."mode", {_MOTOR_SQL}))
            AND {cond})) > 0""")


def _motor_allowed(col: str) -> str:
    """Whether an explicit allowance exists for motor traffic.

    44 of the denied segments carry one — the OSM `access=no` + `motor_vehicle=destination`
    pattern. Without this they would come out flatly closed when they are reachable by residents.
    """
    return (f"""len(list_filter({col}, x -> x.access_type IN ('allowed', 'designated')
            AND (x."when"."mode" IS NULL OR list_has_any(x."when"."mode", {_MOTOR_SQL})))) > 0""")


# Classes that are not motor infrastructure. OSM lets the class IMPLY the access rule —
# highway=footway means no cars without anyone writing it down — and Overture inherits that
# silence: of 5,968 Södermalm edges on these classes, it explicitly denies motor traffic on 18.
# Reading access_restrictions alone therefore called 156 km of footway, steps and cycleway
# drivable. `track` stays motorised (agricultural access) and `unknown` is left alone rather than
# guessed at.
_NON_MOTOR = "('footway', 'steps', 'path', 'pedestrian', 'cycleway', 'bridleway')"


def _motor_access(col: str) -> str:
    """Can a car use this stretch: 'yes' | 'restricted' | 'no'.

    Order matters. An explicit unqualified denial beats everything; otherwise a non-motor class is
    closed unless Overture explicitly lets motor traffic in, which is how a pedestrian street with
    delivery access comes out `restricted` rather than `no`.
    """
    return (f"CASE "
            f"WHEN {_denial(col, False)} AND NOT {_motor_allowed(col)} THEN 'no' "
            f"WHEN class IN {_NON_MOTOR} THEN "
            f"  CASE WHEN {_motor_allowed(col)} THEN 'restricted' ELSE 'no' END "
            f"WHEN {_denial(col, False)} OR {_denial(col, True)} THEN 'restricted' "
            f"ELSE 'yes' END")


def _vehicle_limit(col: str, dim: str, unit: str) -> str:
    """Tightest "denied above X" limit on the segment, as the largest vehicle still allowed."""
    return (f"""list_min(list_transform(list_filter(flatten(list_transform(
            list_filter({col}, x -> x.access_type = 'denied' AND x."when".vehicle IS NOT NULL),
            x -> x."when".vehicle)),
            v -> v.dimension = '{dim}' AND v.comparison = 'greater_than' AND v.unit = '{unit}'),
            v -> v.value))""")


# The roundabout way ids fetched by `duckoverture roundabouts`. A scalar subquery, so DuckDB
# evaluates it once rather than inlining thousands of literals into the SQL.
_RB_IDS = "(SELECT coalesce(list(osm_way_id), []) FROM osm.roundabout_way)"
_OSM_WAY = "try_cast(regexp_extract({0}.record_id, '^w(\\d+)', 1) AS BIGINT)"


def _is_roundabout(col: str, have: bool) -> str:
    """Whether this stretch sits on an OSM way tagged `junction=roundabout`.

    Resolved through `sources` ranges rather than the whole column, because Overture MERGES OSM
    ways: 1,384 of 14,594 Nacka road segments (9.5%) reference more than one, so a segment can be
    a roundabout for part of its length and an ordinary road for the rest.
    """
    if not have:
        return "FALSE"
    ways = f"list_transform({col}, x -> {_OSM_WAY.format('x')})"
    return f"coalesce(list_has_any({ways}, {_RB_IDS}), FALSE)"


def _is_roundabout_dominant(have: bool) -> str:
    """Whole-segment mode: the roundabout must cover MOST of the segment, not merely touch it.

    Dominance rather than presence, and deliberately the opposite choice to `motor_access`. The
    costs are reversed here: wrongly closing a road only makes a router avoid it, but wrongly
    marking one one-way makes it untraversable in the other direction — so over-reporting is the
    dangerous side and we require the roundabout to be the dominant source.
    """
    if not have:
        return "FALSE"
    return (f"coalesce(list_contains({_RB_IDS}, "
            f"{_OSM_WAY.format(_dominant('sources'))}), FALSE)")


def _breakpoints(connectors: bool = False) -> str:
    """Every fraction at which the segment must be cut.

    Attribute ranges always contribute. With ``connectors=True`` the junction positions
    (``connectors[].at``) are added too — that is what turns a segment into routable edges, since
    44% of Södermalm road segments carry more than two connectors and so pass *through* junctions
    without being split.
    """
    parts = ", ".join(
        f"coalesce(flatten(list_transform({c}, x -> coalesce(x.between, []))), [])"
        for c in RANGED)
    if not connectors:
        return f"list_sort(list_distinct(list_concat([0.0, 1.0], {parts})))"
    ats = "list_transform(conns, c -> c.at)"
    # Attribute boundaries and connector positions can name the same place while differing in the
    # source floats — 0.379873 against a connector at 0.379872, 1.7 mm apart on a 1,694 m segment.
    # Left alone that emits a 1.7 mm piece AND, worse, the following piece then starts at a value
    # that matches no connector, so the junction is merged away entirely (55 swallowed connectors,
    # one 110 m inside an edge).
    #
    # So SNAP each breakpoint onto a connector within MIN_SPLIT_M before de-duplicating. This is
    # what Overture's own lr_split_point_min_dist_meters does: "how far ... do these LRs need to be
    # for us to create a new connector for them instead of using the existing connector".
    tol = f"({MIN_SPLIT_M} / {_len()})"
    raw = f"list_concat([0.0, 1.0], {parts}, coalesce({ats}, []))"
    snapped = (f"list_transform({raw}, x -> coalesce("
               f"list_filter({ats}, a -> abs(a - x) <= {tol})[1], x))")
    return f"list_sort(list_distinct({snapped}))"


def _resolve_sql(connectors: bool = False, have_rb: bool = False) -> str:
    """Cut the segment at every breakpoint and resolve each rule at the piece midpoint."""
    return f"""
WITH src AS (SELECT * FROM transportation.segment WHERE subtype = 'road'),
-- Only junctions we actually hold a connector record for become cut points. Overture's splitter
-- does the same, and it matters in a clipped extract: 0.8% of connector references on Södermalm
-- point outside the boundary, and splitting there would mint nodes we cannot identify.
conns AS (
    SELECT s.id, coalesce(list({{'connector_id': x.connector_id, 'at': x.at}}), []) AS conns
    FROM src s, unnest(s.connectors) t(x)
    JOIN transportation.connector k ON k.id = x.connector_id
    GROUP BY 1),
resolved_conns AS (
    SELECT s.*, coalesce(c.conns, []) AS conns FROM src s LEFT JOIN conns c ON c.id = s.id),
breaks AS (SELECT *, {_breakpoints(connectors)} AS bp FROM resolved_conns),
pieces AS (
    SELECT b.*, u.i AS piece_no, b.bp[u.i] AS start_lr, b.bp[u.i + 1] AS end_lr,
           (b.bp[u.i] + b.bp[u.i + 1]) / 2 AS mid
    FROM breaks b, UNNEST(range(1, len(b.bp))) AS u(i)
)
SELECT
    id                                          AS segment_id,
    piece_no, start_lr, end_lr, geometry, conns,
    (end_lr - start_lr) * {_len()} AS piece_m,
    class                                       AS "class",
    -- from subclass_rules, NOT Overture's scalar `subclass`: that one is only set when
    -- the subclass covers the whole segment, so it is NULL on all 36 ranged segments.
    {_at_mid('subclass_rules')}[1].value        AS subclass,
    names.primary                               AS name,
    -- one value per piece, resolved at its midpoint
    {_at_mid('level_rules')}[1].value           AS level,
    {_at_mid('road_surface')}[1].value          AS surface,
    {_at_mid('width_rules')}[1].value           AS width_m,
    -- speed, normalised to km/h (Overture also emits mph)
    CASE {_at_mid('speed_limits')}[1].max_speed.unit
         WHEN 'mph' THEN round({_at_mid('speed_limits')}[1].max_speed.value * 1.609344)
         ELSE {_at_mid('speed_limits')}[1].max_speed.value END::INTEGER AS maxspeed_kmh,
    -- flags that apply to THIS piece, not to the segment as a whole
    coalesce(list_contains(flatten(list_transform({_at_mid('road_flags')}, x -> x.values)),
                  'is_bridge'), FALSE)          AS is_bridge,
    coalesce(list_contains(flatten(list_transform({_at_mid('road_flags')}, x -> x.values)),
                  'is_tunnel'), FALSE)          AS is_tunnel,
    list_contains(flatten(list_transform({_at_mid('road_flags')}, x -> x.values)),
                  'is_link')                    AS is_link,
    -- one-way = a heading-scoped denial for motor traffic covering this piece
    coalesce(len(list_filter({_at_mid('access_restrictions')},
        x -> x.access_type = 'denied' AND x."when".heading IS NOT NULL
             AND (x."when".mode IS NULL OR list_has_any(x."when".mode, {_MOTOR_SQL})))) > 0, FALSE)
             OR {_is_roundabout(_at_mid('sources'), have_rb)}
                                                AS oneway,
    -- Overture drops junction=roundabout and the oneway it implies; recovered by way-id lookup.
    {_is_roundabout(_at_mid('sources'), have_rb)}   AS is_roundabout,
    -- travel direction: 'backward' denied -> runs forward, and vice versa
    CASE WHEN len(list_filter({_at_mid('access_restrictions')},
              x -> x.access_type = 'denied' AND x."when".heading = 'forward'
                   AND (x."when".mode IS NULL OR list_has_any(x."when".mode, {_MOTOR_SQL})))) > 0
         THEN 'backward' ELSE 'forward' END     AS direction,
    -- can a car use this at all? 132 edges on Södermalm are hard-closed and used to read as
    -- ordinary two-way streets, because access_restrictions was only ever mined for `oneway`.
    {_motor_access(_at_mid('access_restrictions'))}  AS motor_access,
    {_vehicle_limit(_at_mid('access_restrictions'), 'height', 'm')}::DOUBLE AS max_height_m,
    {_vehicle_limit(_at_mid('access_restrictions'), 'weight', 't')}::DOUBLE AS max_weight_t
FROM pieces
"""


def _whole_sql(have_rb: bool = False) -> str:
    return f"""
SELECT
    id                                          AS piece_id,
    id                                          AS segment_id,
    1                                           AS piece_no,
    0.0 AS start_lr, 1.0 AS end_lr,
    class                                       AS "class",
    {_dominant('subclass_rules')}.value         AS subclass,
    names.primary                               AS name,
    {_dominant('level_rules')}.value            AS level,
    {_dominant('road_surface')}.value           AS surface,
    {_dominant('width_rules')}.value            AS width_m,
    CASE {_dominant('speed_limits')}.max_speed.unit
         WHEN 'mph' THEN round({_dominant('speed_limits')}.max_speed.value * 1.609344)
         ELSE {_dominant('speed_limits')}.max_speed.value END::INTEGER AS maxspeed_kmh,
    -- resolved by dominance: a flag wins only if it covers more of the segment than its rival
    ({_flag_coverage('is_bridge')} > 0.5
     AND {_flag_coverage('is_bridge')} >= {_flag_coverage('is_tunnel')})  AS is_bridge,
    ({_flag_coverage('is_tunnel')} > 0.5
     AND {_flag_coverage('is_tunnel')} >  {_flag_coverage('is_bridge')})  AS is_tunnel,
    ({_flag_coverage('is_link')} > 0.5)                                   AS is_link,
    coalesce(len(list_filter(access_restrictions,
        x -> x.access_type = 'denied' AND x."when".heading IS NOT NULL
             AND (x."when".mode IS NULL OR list_has_any(x."when".mode, {_MOTOR_SQL})))) > 0, FALSE)
             OR {_is_roundabout_dominant(have_rb)}
                                                AS oneway,
    {_is_roundabout_dominant(have_rb)}          AS is_roundabout,
    CASE WHEN len(list_filter(access_restrictions,
              x -> x.access_type = 'denied' AND x."when".heading = 'forward'
                   AND (x."when".mode IS NULL OR list_has_any(x."when".mode, {_MOTOR_SQL})))) > 0
         THEN 'backward' ELSE 'forward' END     AS direction,
    -- Deliberately presence-based, not dominant like the columns above. The costs are asymmetric:
    -- missing a closure routes a car down a closed road, over-reporting one only avoids it. So a
    -- closure anywhere on the segment closes the whole row in this mode. Use -m attrs or -m edges
    -- to get the closure on just the stretch it applies to.
    {_motor_access('access_restrictions')}      AS motor_access,
    {_vehicle_limit('access_restrictions', 'height', 'm')}::DOUBLE AS max_height_m,
    {_vehicle_limit('access_restrictions', 'weight', 't')}::DOUBLE AS max_weight_t,
    {_len()}                                    AS length_m,
    geometry                                    AS geometry
FROM transportation.segment
WHERE subtype = 'road'
"""


# The attributes that define a distinct stretch of road. Two adjacent pieces agreeing on all of
# these are the same road and must not be separate rows.
_ATTRS = ["class", "subclass", "name", "level", "surface", "width_m", "maxspeed_kmh",
          "is_bridge", "is_tunnel", "is_link", "oneway", "direction",
          "motor_access", "max_height_m", "max_weight_t", "is_roundabout"]

# Splitting at every `between` over-fragments badly: breakpoints come from rules that often resolve
# to the same value, so a 932 m footpath came out as 16 pieces of which 15 were identical. Cut only
# where the resolved attributes actually CHANGE — merge adjacent runs that agree on all of them.
# (Gaps-and-islands: flag a change against the previous piece, cumulative-sum into a run id.)
_CHANGED = " OR ".join(
    f"{c} IS DISTINCT FROM lag({c}) OVER w" for c in _ATTRS)

def _split_sql(connectors: bool = False, merge: bool = True,
               have_rb: bool = False) -> str:
    """Merge adjacent pieces that agree on every attribute, then re-cut the geometry.

    With ``connectors=True`` a piece boundary that sits on a connector is never merged away — the
    junction has to survive or the result is not routable. That is the difference between "flat
    attributes" and "edges you can route on": 44% of Södermalm road segments carry more than two
    connectors, i.e. they pass straight through junctions.

    Boundary connector ids come out as ``from_connector_id`` / ``to_connector_id``. Where a
    boundary is an attribute change rather than a junction there is no Overture connector, so a
    synthetic ``<segment_id>@<lr>`` id is minted — the same approach as
    OvertureMaps/transportation-splitter, which calls them artificial connectors.
    """
    # a boundary that coincides with a connector must force a cut, even if attributes match
    # c.at must be snapped the same way the breakpoints were, or the equality never matches
    on_conn = ('len(list_filter(conns, c -> abs(c.at - start_lr) < 1e-6)) > 0'
               if connectors else "FALSE")
    # merge=False keeps every breakpoint as its own row, which is what Overture's splitter emits.
    # merge=True collapses adjacent runs whose resolved attributes are identical — fewer rows, but
    # no longer row-for-row comparable with the reference implementation.
    changed = (" OR ".join(f"{c} IS DISTINCT FROM lag({c}) OVER w" for c in _ATTRS)
               if merge else "TRUE")
    conn_at = (f"list_filter(any_value(conns), "
               f"c -> abs(c.at - {{lr}}) < 1e-6)[1].connector_id"
               if connectors else "NULL")
    return f"""
WITH resolved AS ({_resolve_sql(connectors, have_rb)}),
marked AS (
    -- A piece shorter than MIN_SPLIT_M is not a real split: two source values naming the same
    -- place a fraction of a millimetre apart. Absorb it into the previous run rather than emit a
    -- degenerate edge. Same intent as lr_split_point_min_dist_meters in Overture's own splitter.
    SELECT *, CASE WHEN piece_no = 1 THEN 1
                   WHEN piece_m < {MIN_SPLIT_M} THEN 0
                   WHEN ({changed}) OR ({on_conn}) THEN 1
                   ELSE 0 END AS is_new
    FROM resolved
    WINDOW w AS (PARTITION BY segment_id ORDER BY piece_no)
),
runs AS (
    SELECT *, sum(is_new) OVER (PARTITION BY segment_id ORDER BY piece_no
                                ROWS UNBOUNDED PRECEDING) AS run_no
    FROM runs_src
)
SELECT
    segment_id || '#' || run_no                 AS piece_id,
    segment_id, run_no                          AS piece_no,
    min(start_lr)                               AS start_lr,
    max(end_lr)                                 AS end_lr,
    coalesce({conn_at.format(lr='min(start_lr)')},
             segment_id || '@' || min(start_lr)) AS from_connector_id,
    coalesce({conn_at.format(lr='max(end_lr)')},
             segment_id || '@' || max(end_lr))   AS to_connector_id,
    {", ".join(f"any_value({c}) AS {c}" for c in _ATTRS)},
    {_len('ST_LineSubstring(any_value(geometry), min(start_lr), max(end_lr))')}
                                                AS length_m,
    ST_LineSubstring(any_value(geometry), min(start_lr), max(end_lr))
                                                AS geometry
FROM runs
GROUP BY segment_id, run_no
""".replace("FROM runs_src", "FROM marked")


# A turn restriction says "from THIS segment, via connector c, you may not enter segment b".
# Splitting invalidates both ends of that sentence: the restriction belongs to only ONE piece of
# this segment (the one that actually touches c), and `b` is now several pieces of which exactly
# one is the manoeuvre. Left unresolved, a router either applies the ban to the whole road — miles
# of it — or cannot apply it at all. This renumbers both ends onto pieces.
#
# Mirrors get_trs() + resolve_tr_references() in OvertureMaps/transportation-splitter.
_TURN_SQL = """
WITH tr AS (
    SELECT s.id AS from_segment_id, t.i AS tr_no,
           s.prohibited_transitions[t.i].sequence       AS seq,
           s.prohibited_transitions[t.i].final_heading  AS final_heading,
           s.prohibited_transitions[t.i]."when".heading AS when_heading,
           s.prohibited_transitions[t.i]."when".during  AS during,
           s.prohibited_transitions[t.i]."when"."mode"  AS mode
    FROM transportation.segment s,
         UNNEST(range(1, len(s.prohibited_transitions) + 1)) t(i)
    WHERE s.subtype = 'road' AND len(s.prohibited_transitions[t.i].sequence) > 0
),
-- Which piece owns the restriction? The one whose END is the first junction of the sequence if
-- the ban applies to forward travel, whose START is if backward. An unscoped ban takes either.
owner AS (
    SELECT f.piece_id AS from_piece_id, tr.*
    FROM tr JOIN flat.segment f ON f.segment_id = tr.from_segment_id
    WHERE CASE tr.when_heading
            WHEN 'forward'  THEN f.to_connector_id   = tr.seq[1].connector_id
            WHEN 'backward' THEN f.from_connector_id = tr.seq[1].connector_id
            ELSE tr.seq[1].connector_id IN (f.from_connector_id, f.to_connector_id) END
),
steps AS (
    SELECT o.*, u.i AS seq_no,
           o.seq[u.i].connector_id AS via_connector_id,
           o.seq[u.i].segment_id   AS to_segment_id,
           CASE WHEN u.i < len(o.seq) THEN o.seq[u.i + 1].connector_id END AS next_connector_id
    FROM owner o, UNNEST(range(1, len(o.seq) + 1)) u(i)
),
-- The referenced piece must span BOTH junctions of this step — enter at one, leave at the other.
-- On the last step there is no next junction, so direction of travel picks the end instead.
resolved AS (
    SELECT st.*, f.piece_id AS to_piece_id
    FROM steps st JOIN flat.segment f ON f.segment_id = st.to_segment_id
    WHERE CASE WHEN st.next_connector_id IS NULL
               THEN CASE st.final_heading
                      WHEN 'forward' THEN f.from_connector_id = st.via_connector_id
                      ELSE                f.to_connector_id   = st.via_connector_id END
               ELSE st.via_connector_id  IN (f.from_connector_id, f.to_connector_id)
                AND st.next_connector_id IN (f.from_connector_id, f.to_connector_id) END
),
-- A partly-resolved chain is not a restriction, it is a guess. Overture's splitter drops these
-- too; on a clipped extract most of them are manoeuvres leaving the boundary.
complete AS (
    SELECT from_piece_id, tr_no FROM resolved
    GROUP BY 1, 2 HAVING count(DISTINCT seq_no) = any_value(len(seq))
)
SELECT r.from_piece_id, r.from_segment_id, r.tr_no, r.seq_no,
       r.via_connector_id, r.to_piece_id, r.to_segment_id,
       r.final_heading, r.when_heading, r.during, r.mode
FROM resolved r JOIN complete c USING (from_piece_id, tr_no)
ORDER BY r.from_piece_id, r.tr_no, r.seq_no
"""


MODES = ("segment", "attrs", "edges", "routing")


def flatten(db: str | Path, mode: str = "segment", log=print) -> dict[str, int]:
    """Build the `flat.*` schema from the raw Overture tables. Idempotent.

    ``mode``:

    - ``segment`` (default) — one row per Overture segment, each ranged attribute from its
      dominant (longest-covering) rule. Joins 1:1 on ``segment_id``.
    - ``attrs`` — cut where a resolved attribute changes.
    - ``routing`` — cut at junctions **and** real attribute changes, with ``from_connector_id`` /
      ``to_connector_id``. The routable form: 44% of Södermalm road segments carry more than two
      connectors, so without it a "segment" runs straight through junctions.
    - ``edges`` — as ``routing`` but never merges adjacent identical pieces, so the output is
      row-for-row comparable with OvertureMaps/transportation-splitter. Costs 1,189 extra rows on
      Södermalm that differ only in which OSM way they came from.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    con = duckdb.connect(str(db))
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("CREATE SCHEMA IF NOT EXISTS flat")
    stats: dict[str, int] = {}

    t0 = time.perf_counter()
    # Roundabout repair only applies if `duckoverture roundabouts` has been run; without it
    # is_roundabout is FALSE everywhere and behaviour is exactly as before.
    have_rb = have_roundabouts(con)
    sql = {"segment": _whole_sql(have_rb),
           "attrs": _split_sql(connectors=False, merge=True, have_rb=have_rb),
           # edges is deliberately UNMERGED: row-for-row comparable with
           # OvertureMaps/transportation-splitter run with split_at_connectors=True
           "edges": _split_sql(connectors=True, merge=False, have_rb=have_rb),
           # routing is edges with adjacent identical-attribute runs merged back. The difference is
           # `sources`: it carries a `between` per contributing OSM way, so a 237 m residential
           # street stitched from 13 ways splits into 16 identical pieces. Those cuts are
           # provenance, not geography, and mean nothing to a router — but they must stay in
           # `edges`, because the reference splitter emits them too.
           "routing": _split_sql(connectors=True, merge=True, have_rb=have_rb)}[mode]
    con.execute(f"CREATE OR REPLACE TABLE flat.segment AS {sql}")
    n = con.execute("SELECT count(*) FROM flat.segment").fetchone()[0]
    src = con.execute(
        "SELECT count(*) FROM transportation.segment WHERE subtype='road'").fetchone()[0]
    stats["flat.segment"] = n
    desc = {"segment": "one row per segment (dominant rule)",
            "attrs": "split at attribute changes",
            "edges": "split at every breakpoint (splitter-parity)",
            "routing": "split at junctions + attribute changes (routable)"}[mode]
    log(f"  flat.segment  {src:,} segments -> {n:,} rows [{desc}]  "
        f"{time.perf_counter()-t0:.1f}s")

    # Turn restrictions reference whole segments, so they only mean anything once the pieces have
    # junctions to hang off — the two connector-aware modes.
    if mode in ("edges", "routing"):
        t0 = time.perf_counter()
        con.execute(f"CREATE OR REPLACE TABLE flat.turn_restriction AS {_TURN_SQL}")
        rows, kept = con.execute(
            """SELECT count(*), count(DISTINCT (from_piece_id, tr_no))
               FROM flat.turn_restriction""").fetchone()
        src_tr = con.execute(
            """SELECT count(*) FROM transportation.segment s,
               UNNEST(s.prohibited_transitions) t(x) WHERE s.subtype='road'""").fetchone()[0]
        stats["flat.turn_restriction"] = rows
        log(f"  flat.turn_restriction  {src_tr} transitions -> {kept} renumbered onto pieces "
            f"({rows} steps)  {time.perf_counter()-t0:.1f}s")
        if kept < src_tr:
            log(f"  {src_tr - kept} dropped: sequence leaves the extract or the piece is unsplit")

    # length conservation is the invariant that says the split was clean
    a, b = con.execute("""SELECT (SELECT sum(length_m) FROM flat.segment),
                                 (SELECT sum(ST_Length_Spheroid(ST_FlipCoordinates(geometry)))
                                  FROM transportation.segment WHERE subtype='road')""").fetchone()
    drift = abs(a - b) / b * 100 if b else 0.0
    log(f"  length {a:,.0f} m vs raw {b:,.0f} m  (drift {drift:.4f}%)")
    if drift > 0.01:
        log("  WARNING: length drift above 0.01% — the split lost geometry")

    con.close()
    return stats
