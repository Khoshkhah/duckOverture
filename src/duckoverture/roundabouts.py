"""Fetch `junction=roundabout` way ids from Overpass into ``osm.roundabout_way``.

Overture drops OSM's `junction=roundabout` tag and never materialises the `oneway=yes` it implies,
so roundabouts come out two-way and a router sends vehicles the wrong way round them. What Overture
*does* keep is the provenance: ``sources[].record_id`` is the OSM way id verbatim
(``w288602249@7``), on 98.1% of road segments. So the fact can be looked up rather than guessed
from shape — see ``docs/design/roundabout_oneway_recovery.md`` for the measurement against a
geometric detector (exact vs 22-35% precision).

This is a separate step from ``flatten`` on purpose. ``flatten`` is run repeatedly, once per mode;
if the lookup lived inside it every re-run would need the network. Fetch once, then flatten offline
as many times as you like.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import duckdb

# The main endpoint 504s often enough that a first run hits it; fall through to mirrors rather
# than making the user discover --endpoint from a stack trace.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MIRRORS = (OVERPASS_URL,
           "https://overpass.kumi.systems/api/interpreter",
           "https://overpass.private.coffee/api/interpreter")

# `out ids` returns nothing but the way ids — a few hundred for a city, not a PBF.
_QUERY = '[out:json][timeout:{timeout}];way["junction"="roundabout"]({s},{w},{n},{e});out ids;'


def fetch(db: str | Path, endpoint: str | None = None, timeout: int = 180,
          log=print) -> int:
    """Look up the area's roundabout way ids and store them. Idempotent — re-run to refresh."""
    con = duckdb.connect(str(db))
    con.execute("INSTALL spatial; LOAD spatial;")
    try:
        s, w, n, e = con.execute(
            "SELECT ST_YMin(geom), ST_XMin(geom), ST_YMax(geom), ST_XMax(geom) "
            "FROM main.boundary").fetchone()
        query = _QUERY.format(timeout=timeout, s=s, w=w, n=n, e=e)
        log(f"overpass  bbox {w:.4f},{s:.4f} -> {e:.4f},{n:.4f}")

        endpoints = list(MIRRORS) if endpoint is None else [endpoint]
        payload, last = None, ""
        for i, url in enumerate(endpoints):
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode({"data": query}).encode(),
                headers={"User-Agent": "duckoverture (github.com/kaveh/duckoverture)"})
            try:
                with urllib.request.urlopen(req, timeout=timeout + 30) as r:
                    payload = json.loads(r.read().decode())
                break
            except (urllib.error.HTTPError, urllib.error.URLError) as err:
                # 429/504 mean Overpass is busy, not that the query is wrong
                last = f"{getattr(err, 'code', '')} {getattr(err, 'reason', err)}".strip()
                if i + 1 < len(endpoints):
                    log(f"  {urllib.parse.urlparse(url).netloc} -> {last}, trying a mirror")
        if payload is None:
            raise SystemExit(
                f"every Overpass endpoint failed (last: {last}). They rate-limit heavily; wait a "
                f"minute and retry, or pass --endpoint.")

        ids = sorted({el["id"] for el in payload.get("elements", []) if el.get("type") == "way"})
        con.execute("CREATE SCHEMA IF NOT EXISTS osm")
        # now() is computed IN SQL and never fetched into Python — reading a timestamp back would
        # pull in a pytz dependency the core does not otherwise need.
        con.execute(
            "CREATE OR REPLACE TABLE osm.roundabout_way AS "
            "SELECT unnest(?::BIGINT[]) AS osm_way_id, now()::VARCHAR AS fetched_at, "
            "       'overpass' AS source, ? AS query", [ids, query])
        log(f"  osm.roundabout_way  {len(ids):,} roundabout ways")

        # How many actually land on roads we hold — the number that will change anything.
        hit = con.execute("""
            SELECT count(DISTINCT s.id) FROM transportation.segment s, unnest(s.sources) t(x)
            WHERE s.subtype = 'road' AND try_cast(regexp_extract(x.record_id, '^w(\\d+)', 1)
                                                  AS BIGINT) IN (SELECT osm_way_id FROM osm.roundabout_way)
            """).fetchone()[0]
        log(f"  {hit:,} segments in this extract sit on one")
        if ids and not hit:
            log("  none matched — is the boundary the same area the extract was built from?")
        log("  run `duckoverture flatten` to apply it to oneway")
        return len(ids)
    finally:
        con.close()


def have_roundabouts(con) -> bool:
    """Whether a roundabout source has been fetched for this database."""
    return bool(con.execute(
        "SELECT count(*) FROM duckdb_tables() "
        "WHERE schema_name = 'osm' AND table_name = 'roundabout_way'").fetchone()[0])
