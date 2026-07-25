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

# Rule columns that carry a `between` range. Their breakpoints define where a segment is cut.
RANGED = ["level_rules", "speed_limits", "access_restrictions", "road_flags",
          "road_surface", "subclass_rules", "width_rules"]

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


def _breakpoints() -> str:
    parts = ", ".join(
        f"coalesce(flatten(list_transform({c}, x -> coalesce(x.between, []))), [])"
        for c in RANGED)
    return f"list_sort(list_distinct(list_concat([0.0, 1.0], {parts})))"


_RESOLVE_SQL = f"""
WITH src AS (SELECT * FROM transportation.segment WHERE subtype = 'road'),
breaks AS (SELECT *, {_breakpoints()} AS bp FROM src),
pieces AS (
    SELECT b.*, u.i AS piece_no, b.bp[u.i] AS s, b.bp[u.i + 1] AS e,
           (b.bp[u.i] + b.bp[u.i + 1]) / 2 AS mid
    FROM breaks b, UNNEST(range(1, len(b.bp))) AS u(i)
)
SELECT
    id                                          AS segment_id,
    piece_no, s, e, geometry,
    class                                       AS highway,
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
    list_contains(flatten(list_transform({_at_mid('road_flags')}, x -> x.values)),
                  'is_bridge')                  AS is_bridge,
    list_contains(flatten(list_transform({_at_mid('road_flags')}, x -> x.values)),
                  'is_tunnel')                  AS is_tunnel,
    list_contains(flatten(list_transform({_at_mid('road_flags')}, x -> x.values)),
                  'is_link')                    AS is_link,
    -- one-way = a heading-scoped denial for motor traffic covering this piece
    len(list_filter({_at_mid('access_restrictions')},
        x -> x.access_type = 'denied' AND x."when".heading IS NOT NULL
             AND (x."when".mode IS NULL OR list_has_any(x."when".mode,
                  ['motor_vehicle','car','truck','hgv','bus','motorcycle','taxi','hov'])))) > 0
                                                AS oneway,
    -- travel direction: 'backward' denied -> runs forward, and vice versa
    CASE WHEN len(list_filter({_at_mid('access_restrictions')},
              x -> x.access_type = 'denied' AND x."when".heading = 'forward'
                   AND (x."when".mode IS NULL OR list_has_any(x."when".mode,
                        ['motor_vehicle','car','truck','hgv','bus','motorcycle','taxi','hov'])))) > 0
         THEN 'backward' ELSE 'forward' END     AS direction
FROM pieces
"""

_WHOLE_SQL = f"""
SELECT
    id                                          AS piece_id,
    id                                          AS segment_id,
    1                                           AS piece_no,
    0.0 AS s, 1.0 AS e,
    class                                       AS highway,
    {_dominant('subclass_rules')}.value         AS subclass,
    names.primary                               AS name,
    {_dominant('level_rules')}.value            AS level,
    {_dominant('road_surface')}.value           AS surface,
    {_dominant('width_rules')}.value            AS width_m,
    CASE {_dominant('speed_limits')}.max_speed.unit
         WHEN 'mph' THEN round({_dominant('speed_limits')}.max_speed.value * 1.609344)
         ELSE {_dominant('speed_limits')}.max_speed.value END::INTEGER AS maxspeed_kmh,
    list_contains(flatten(list_transform(road_flags, x -> x.values)), 'is_bridge') AS is_bridge,
    list_contains(flatten(list_transform(road_flags, x -> x.values)), 'is_tunnel') AS is_tunnel,
    list_contains(flatten(list_transform(road_flags, x -> x.values)), 'is_link')   AS is_link,
    len(list_filter(access_restrictions,
        x -> x.access_type = 'denied' AND x."when".heading IS NOT NULL
             AND (x."when".mode IS NULL OR list_has_any(x."when".mode, {_MOTOR_SQL})))) > 0
                                                AS oneway,
    CASE WHEN len(list_filter(access_restrictions,
              x -> x.access_type = 'denied' AND x."when".heading = 'forward'
                   AND (x."when".mode IS NULL OR list_has_any(x."when".mode, {_MOTOR_SQL})))) > 0
         THEN 'backward' ELSE 'forward' END     AS direction,
    ST_Length_Spheroid(geometry)                AS length_m,
    geometry                                    AS geometry
FROM transportation.segment
WHERE subtype = 'road'
"""


# The attributes that define a distinct stretch of road. Two adjacent pieces agreeing on all of
# these are the same road and must not be separate rows.
_ATTRS = ["highway", "subclass", "name", "level", "surface", "width_m", "maxspeed_kmh",
          "is_bridge", "is_tunnel", "is_link", "oneway", "direction"]

# Splitting at every `between` over-fragments badly: breakpoints come from rules that often resolve
# to the same value, so a 932 m footpath came out as 16 pieces of which 15 were identical. Cut only
# where the resolved attributes actually CHANGE — merge adjacent runs that agree on all of them.
# (Gaps-and-islands: flag a change against the previous piece, cumulative-sum into a run id.)
_CHANGED = " OR ".join(
    f"{c} IS DISTINCT FROM lag({c}) OVER w" for c in _ATTRS)

FLAT_SEGMENT_SQL = f"""
WITH resolved AS ({_RESOLVE_SQL}),
marked AS (
    SELECT *, CASE WHEN piece_no = 1 OR ({_CHANGED}) THEN 1 ELSE 0 END AS is_new
    FROM resolved
    WINDOW w AS (PARTITION BY segment_id ORDER BY piece_no)
),
runs AS (
    SELECT *, sum(is_new) OVER (PARTITION BY segment_id ORDER BY piece_no
                                ROWS UNBOUNDED PRECEDING) AS run_no
    FROM marked
)
SELECT
    segment_id || '#' || run_no                 AS piece_id,
    segment_id, run_no                          AS piece_no,
    min(s)                                      AS s,
    max(e)                                      AS e,
    {", ".join(f"any_value({c}) AS {c}" for c in _ATTRS)},
    ST_Length_Spheroid(ST_LineSubstring(any_value(geometry), min(s), max(e))) AS length_m,
    ST_LineSubstring(any_value(geometry), min(s), max(e))                     AS geometry
FROM runs
GROUP BY segment_id, run_no
"""


def flatten(db: str | Path, split: bool = False, log=print) -> dict[str, int]:
    """Build the `flat.*` schema from the raw Overture tables. Idempotent.

    ``split=False`` (default) gives one row per Overture segment, each ranged attribute taken from
    its dominant (longest-covering) rule. ``split=True`` cuts segments where a resolved attribute
    changes.
    """
    con = duckdb.connect(str(db))
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("CREATE SCHEMA IF NOT EXISTS flat")
    stats: dict[str, int] = {}

    t0 = time.perf_counter()
    con.execute(f"CREATE OR REPLACE TABLE flat.segment AS "
                f"{FLAT_SEGMENT_SQL if split else _WHOLE_SQL}")
    n = con.execute("SELECT count(*) FROM flat.segment").fetchone()[0]
    src = con.execute(
        "SELECT count(*) FROM transportation.segment WHERE subtype='road'").fetchone()[0]
    stats["flat.segment"] = n
    mode = "split at attribute changes" if split else "one row per segment (dominant rule)"
    log(f"  flat.segment  {src:,} segments -> {n:,} rows "
        f"[{mode}]  {time.perf_counter()-t0:.1f}s")

    # length conservation is the invariant that says the split was clean
    a, b = con.execute("""SELECT (SELECT sum(length_m) FROM flat.segment),
                                 (SELECT sum(ST_Length_Spheroid(geometry))
                                  FROM transportation.segment WHERE subtype='road')""").fetchone()
    drift = abs(a - b) / b * 100 if b else 0.0
    log(f"  length {a:,.0f} m vs raw {b:,.0f} m  (drift {drift:.4f}%)")
    if drift > 0.01:
        log("  WARNING: length drift above 0.01% — the split lost geometry")

    con.close()
    return stats
