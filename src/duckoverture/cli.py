"""duckoverture CLI."""
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
@click.option("--split/--no-split", default=False, show_default=True,
              help="Cut segments where a resolved attribute changes, instead of one row each.")
def flatten_cmd(db: Path, split: bool) -> None:
    """Derive the flat.* schema from the raw Overture tables.

    Overture attaches attributes to RANGES along a segment (`between: [0.43, 0.97]`). By default
    each segment stays one row, with every ranged column taking its DOMINANT rule — the one
    covering most of the segment. --split cuts instead, wherever a resolved attribute changes.
    Total length is conserved either way.
    """
    from .flatten import flatten
    flatten(db, split=split)


@cli.command("viz")
@click.argument("db", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Output HTML (default: viz/<db stem>.html).")
@click.option("-p", "--palette", default="highsat", show_default=True,
              help="roadstyle palette: highsat | carto | mono.")
@click.option("--backend", default="web", show_default=True,
              help="roadstyle backend: web | folium | lonboard.")
@click.option("--report", is_flag=True,
              help="Inspection map: flat, resolved fields in a click-to-open side panel.")
@click.option("--raw", is_flag=True,
              help="With --report, also append the untouched nested columns as JSON.")
def viz_cmd(db: Path, output: Path | None, palette: str, backend: str,
            report: bool, raw: bool) -> None:
    """Render an extract's road network to a self-contained HTML map.

    Overture's `class` values are OSM-derived, so roadstyle styles them with no palette changes —
    bridge / tunnel / layer are unpacked from `road_flags` and `level_rules`.

    With --report, every Overture column travels with the map and clicking a road docks a side
    panel showing all of them; nested structs are rendered as JSON so they stay readable.
    """
    from .viz import default_output, render
    out = output or default_output(db)
    if report and output is None:
        out = out.with_name(f"{out.stem}_report.html")
    render(db, out, palette=palette, backend=backend, report=report, raw=raw)


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
