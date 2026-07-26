# Cuts land in the wrong place: `at` is geodesic, `ST_LineSubstring` is not

**Status:** proposed, not implemented. Found 2026-07-25 while validating `flat.connector`.

## Problem

Overture's linear references — `connectors[].at`, and every `between` — are fractions of the
segment's **geodesic length**. `ST_LineSubstring` and `ST_LineLocatePoint` measure fraction of
**2D Cartesian length in degree space**. At 59°N one degree of longitude is ~0.51 of a degree of
latitude in metres, so the two parameterisations diverge on any segment that is not a straight
N–S or E–W run.

Every cut we make passes a geodesic fraction to a degree-space function.

### Evidence

Connector `fc01da5c-8507-4aed-9213-f70d2621d199`, which lies **exactly on** both segments that
reference it (0.0 m off the line):

| segment | Overture `at` | 2D-degree fraction | geodesic fraction |
|---|---|---|---|
| `b8aa3834` | 0.795222 | 0.870819 | **0.795222** |
| `91be6848` | 0.458030 | 0.383560 | **0.458030** |

`at` matches the geodesic fraction to six decimal places on both. Cutting at `at` in degree space
puts the boundary 88.7 m and 33.2 m from where it belongs.

Across the Södermalm extract, comparing each connector's stated position against
`ST_LineInterpolatePoint(geometry, at)`:

| gap | connector refs |
|---|---:|
| < 0.5 m | 10,402 |
| 0.5–5 m | 1,415 |
| 5–25 m | 694 |
| **> 25 m** | **63** (worst 88.7 m) |

So ~6% of junctions are materially misplaced, and the tail is bad enough to matter for routing —
88 m puts a junction on the wrong side of a block.

### What is and is not affected

- **Positions are wrong.** Piece boundaries, and therefore `flat.connector` node coordinates, are
  off by up to 88 m.
- **Totals are fine.** Summing a segment's pieces reconstructs the whole segment wherever the cuts
  land, which is why the length-conservation check reads 0.0000% and never caught this.
- **Per-piece `length_m` is self-consistent.** It is measured from the emitted geometry, so all
  8,980 pieces agree with their own line to within 0.5 m. The lengths honestly describe the pieces
  we emit; the pieces are in the wrong places.
- **Splitter parity is not at risk.** The comparison against
  `OvertureMaps/transportation-splitter` was made on `start_lr` values and row counts, which are
  the same numbers under either parameterisation. Fixing this moves us *towards* the reference,
  not away.

### The reference implementation gets this right

`transportation_splitter/geometry.py`:

```python
def get_length(line_geometry: LineString) -> float:
    """Calculate the geodesic length of a LineString in meters using WGS84."""
    geod = pyproj.Geod(ellps="WGS84")
    return geod.geometry_length(line_geometry)
```

and its split points carry `lr_meters` — geodesic metres along the line. So this is our bug, not a
disagreement about semantics.

## Approach

Three ways to cut at a geodesic fraction in DuckDB.

**A. Cumulative geodesic length per vertex, in SQL (recommended).** Unnest the line's points,
measure each consecutive pair geodesically, cumulative-sum to get each vertex's geodesic distance
along the line, then locate the target fraction between the bracketing vertices and interpolate.
Exact, no new dependency, correct at any extract size and latitude. More SQL than the current
one-liner, and it has to run per cut rather than per segment.

**B. Substring in a local metric CRS.** `ST_Transform` to a projected CRS, `ST_LineSubstring`
there, transform back. A few lines, and planar-metre fractions match geodesic ones to well under
a centimetre at city scale. The wrinkle is picking the CRS: it would have to be derived from the
boundary (UTM zone from the centroid), and distortion grows for country-sized extracts, so the
error becomes a function of how much area was asked for.

**C. A pyproj UDF.** Matches the reference exactly, but adds a dependency to a core that is
currently `duckdb` + `click` only, and a row-wise Python UDF over ~20k cuts is slow.

Recommend **A**, with **B** as a fallback if A proves too slow — it is the honest fix and it holds
regardless of where on Earth the extract is.

## Rejected

- **Leave it and document.** 6% of junctions misplaced by 5–88 m is not a caveat, it is a wrong
  answer for the routing use case the `routing` mode exists to serve.
- **Reinterpret `at` as degree-space.** It is not, on the evidence above, and the reference
  implementation agrees.

## Impact

- All four modes re-cut; `segment` mode is unaffected (no cuts).
- `flat.connector` node positions converge on Overture's own connector geometry — that check is
  already wired in `flatten()` and currently reports 88.1 m, which becomes the regression test.
- Row counts should not move: cut *positions* change, cut *count* does not.
- `length_m` per piece changes for the ~6% that were misplaced. Total stays 290,590 m.
