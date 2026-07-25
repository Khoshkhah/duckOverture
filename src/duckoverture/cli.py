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
