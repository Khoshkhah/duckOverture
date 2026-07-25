"""Boundary in, DuckDB out.

One query per Overture type:

    read_parquet(s3 url)  →  bbox prefilter  →  exact ST_Intersects clip  →  table

The bbox prefilter is what makes this affordable. Overture carries a `bbox` struct column, so a
cheap numeric predicate lets Parquet skip whole row groups before any geometry is read. On
Södermalm that is 6,165 candidate segments fetched in ~6 s, then clipped to 4,251 in ~0 s. Doing
`ST_Intersects` alone against the remote dataset would pull the whole planet.

Rows are stored **exactly as Overture publishes them** — nested structs intact, nothing renamed,
nothing dropped. Flattening is a separate stage on top (`duckoverture flatten`), so the raw layer
stays a faithful mirror and can be re-derived from without re-downloading.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb

from .schema import OvertureType, resolve

DEFAULT_RELEASE = "2026-07-22.0"


def _connect(out: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(out))
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")   # Overture's bucket; anonymous read
    return con


def _load_boundary(con: duckdb.DuckDBPyConnection, boundary: Path) -> tuple[float, ...]:
    """Register the boundary as `main.boundary` and return its bbox."""
    gj = json.loads(Path(boundary).read_text())
    if gj.get("type") == "FeatureCollection":
        geoms = [f["geometry"] for f in gj["features"]]
    elif gj.get("type") == "Feature":
        geoms = [gj["geometry"]]
    else:
        geoms = [gj]
    con.execute("CREATE OR REPLACE TABLE boundary AS SELECT ST_GeomFromGeoJSON(?) AS geom",
                [json.dumps(geoms[0])])
    for g in geoms[1:]:      # multiple features -> union them
        con.execute("UPDATE boundary SET geom = ST_Union(geom, ST_GeomFromGeoJSON(?))",
                    [json.dumps(g)])
    return con.execute(
        "SELECT ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom) FROM boundary"
    ).fetchone()


def build_type(con, t: OvertureType, release: str, bbox: tuple[float, ...],
               clip: bool = True) -> tuple[int, float]:
    """Materialise one Overture type into `<theme>.<type>`. Returns (rows, seconds)."""
    xmin, ymin, xmax, ymax = bbox
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {t.theme}")
    t0 = time.perf_counter()
    # bbox overlap, not containment: keep anything whose extent touches the boundary's.
    prefilter = (f"bbox.xmin <= {xmax} AND bbox.xmax >= {xmin} "
                 f"AND bbox.ymin <= {ymax} AND bbox.ymax >= {ymin}")
    src = (f"SELECT * FROM read_parquet('{t.url(release)}', hive_partitioning=1) "
           f"WHERE {prefilter}")
    if clip:
        sql = (f"CREATE OR REPLACE TABLE {t.table} AS "
               f"SELECT s.* FROM ({src}) s, boundary b WHERE ST_Intersects(s.geometry, b.geom)")
    else:
        sql = f"CREATE OR REPLACE TABLE {t.table} AS {src}"
    con.execute(sql)
    n = con.execute(f"SELECT count(*) FROM {t.table}").fetchone()[0]
    return n, time.perf_counter() - t0


def build(boundary: Path, output: Path, release: str = DEFAULT_RELEASE,
          types: list[str] | None = None, clip: bool = True, log=print) -> dict[str, int]:
    """Extract every requested Overture type inside `boundary` into `output`."""
    selected = resolve(types)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(output)
    bbox = _load_boundary(con, boundary)
    log(f"boundary {Path(boundary).name}  bbox "
        f"{bbox[0]:.4f},{bbox[1]:.4f} -> {bbox[2]:.4f},{bbox[3]:.4f}")
    log(f"release {release}  |  {len(selected)} type(s)  |  clip={clip}")

    stats: dict[str, int] = {}
    for t in selected:
        try:
            n, dt = build_type(con, t, release, bbox, clip)
            stats[t.table] = n
            log(f"  {t.table:34s} {n:>9,} rows  {dt:6.1f}s")
        except Exception as e:                   # one bad type must not lose the rest
            stats[t.table] = -1
            log(f"  {t.table:34s} FAILED: {str(e).splitlines()[0][:90]}")

    # now() stays inside SQL: fetching a TIMESTAMPTZ into Python needs pytz, which we do not want
    # as a dependency for a metadata row.
    con.execute("CREATE OR REPLACE TABLE _meta AS SELECT ? AS release, ? AS boundary, "
                "now()::VARCHAR AS built_at, ? AS clipped",
                [release, str(Path(boundary).resolve()), clip])
    con.close()
    ok = sum(1 for v in stats.values() if v >= 0)
    log(f"wrote {output}  ({ok}/{len(selected)} types, {sum(v for v in stats.values() if v>0):,} rows)")
    return stats
