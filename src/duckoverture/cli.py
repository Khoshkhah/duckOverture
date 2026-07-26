"""duckOverture CLI."""
from __future__ import annotations

from pathlib import Path

import click

from .build import DEFAULT_RELEASE, build
from .schema import THEMES, TYPES


@click.group()
@click.version_option()
def cli() -> None:
    """Extract Overture Maps data inside a boundary into a single DuckDB file."""


@cli.command("build")
@click.option("-b", "--boundary", required=True, type=click.Path(exists=True, path_type=Path),
              help="GeoJSON boundary (Feature, FeatureCollection or bare geometry).")
@click.option("-o", "--output", required=True, type=click.Path(path_type=Path),
              help="Output .duckdb path.")
@click.option("-r", "--release", default=DEFAULT_RELEASE, show_default=True,
              help="Overture release to pin.")
@click.option("-t", "--types", multiple=True,
              help="Type or theme to include (repeatable). Default: all 15 types.")
@click.option("--clip/--no-clip", default=True, show_default=True,
              help="Exact ST_Intersects clip; --no-clip keeps the bbox prefilter only (faster).")
def build_cmd(boundary: Path, output: Path, release: str, types, clip: bool) -> None:
    """Extract Overture data inside BOUNDARY into a DuckDB file.

    Tables land as <theme>.<type>, holding rows exactly as Overture publishes them — nested
    structs intact. Run `duckoverture flatten` afterwards for scalar convenience columns.
    """
    build(boundary, output, release=release, types=list(types) or None, clip=clip)


@cli.command("flatten")
@click.argument("db", type=click.Path(exists=True, path_type=Path))
@click.option("-m", "--mode", type=click.Choice(["segment", "attrs", "edges", "routing"]),
              default="segment", show_default=True,
              help="segment: one row each · attrs: cut on attribute change · "
                   "routing: cut at junctions too · edges: routing without merging "
                   "(matches Overture's splitter).")
def flatten_cmd(db: Path, mode: str) -> None:
    """Derive the flat.* schema from the raw Overture tables.

    Overture attaches attributes to RANGES along a segment (`between: [0.43, 0.97]`). By default
    each segment stays one row, with every ranged column taking its DOMINANT rule — the one
    covering most of the segment.

    Run `duckoverture roundabouts` first to repair the one-way that Overture drops on
    roundabouts; without it `is_roundabout` is false everywhere.

    -m routing additionally cuts at every connector, giving edges with exactly two junctions and
    from_connector_id / to_connector_id — the routable form. It also builds flat.turn_restriction,
    renumbering Overture's prohibited_transitions onto the split pieces at both ends.

    -m edges is the same but never merges adjacent identical pieces, so it matches
    OvertureMaps/transportation-splitter row for row. Use it to validate, not to route: `sources`
    carries a range per contributing OSM way, so a 237 m street stitched from 13 of them becomes
    16 identical pieces.

    Total length is conserved in all modes.
    """
    from .flatten import flatten
    flatten(db, mode=mode)


@cli.command("roundabouts")
@click.argument("db", type=click.Path(exists=True, path_type=Path))
@click.option("--endpoint", default=None,
              help="Overpass endpoint. Default: overpass-api.de, falling back to mirrors.")
@click.option("--timeout", default=180, show_default=True, help="Overpass query timeout, seconds.")
def roundabouts_cmd(db: Path, endpoint: str | None, timeout: int) -> None:
    """Fetch junction=roundabout way ids from Overpass, for the extract's boundary.

    Overture drops OSM's roundabout tag and the `oneway=yes` it implies, so roundabouts come out
    two-way and a router sends vehicles the wrong way round them. `sources[].record_id` is the OSM
    way id, so the fact can be looked up instead of guessed from shape.

    Run this once per area, then `duckoverture flatten` applies it offline. Without it,
    `is_roundabout` is false everywhere and nothing changes.
    """
    from .roundabouts import fetch
    fetch(db, endpoint=endpoint, timeout=timeout)


@cli.command("viz")
@click.argument("db", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Output HTML (default: viz/<db stem>.html).")
@click.option("-p", "--palette", default="highsat", show_default=True,
              help="roadstyle palette: highsat | carto | mono.")
@click.option("--backend", default="web", show_default=True,
              help="roadstyle backend: web | folium | lonboard.")
@click.option("--report/--no-report", default=True, show_default=True,
              help="Inspection map: flat, resolved fields in a click-to-open side panel. "
                   "--no-report for the plain styled map.")
@click.option("--raw", is_flag=True,
              help="With report mode, also append the untouched nested columns as JSON.")
@click.option("--drivable", is_flag=True,
              help="Only roads a car can use (motor_access <> 'no'). Needs flatten first.")
@click.option("--tiles/--no-tiles", default=True, show_default=True,
              help="Embed PMTiles vector tiles instead of inline GeoJSON — much smoother on "
                   "large networks. --no-tiles for plain inline GeoJSON.")
def viz_cmd(db: Path, output: Path | None, palette: str, backend: str,
            report: bool, raw: bool, drivable: bool, tiles: bool) -> None:
    """Render an extract's road network to a self-contained HTML map.

    Overture's `class` values are OSM-derived, so roadstyle styles them with no palette changes —
    bridge / tunnel / layer are unpacked from `road_flags` and `level_rules`.

    By default this is the full inspection map — report side panel, embedded PMTiles, per-class
    zoom thinning. In report mode every Overture column travels with the map and clicking a road
    docks a side panel showing all of them; nested structs are rendered as JSON so they stay
    readable. `--no-report` for the plain styled map.
    """
    from .viz import default_output, render
    out = output or default_output(db)
    if report and output is None:
        out = out.with_name(f"{out.stem}_report.html")
    render(db, out, palette=palette, backend=backend, report=report, raw=raw,
           drivable=drivable, tiles=tiles)


@cli.command("types")
def types_cmd() -> None:
    """List the Overture themes and types this can extract."""
    for theme in THEMES:
        ts = [t.type for t in TYPES if t.theme == theme]
        click.echo(f"{theme:16s} {', '.join(ts)}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
