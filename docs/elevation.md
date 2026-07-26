# Elevation for an Overture extract

**Overture carries no elevation.** Segments have `road_flags`, `level_rules` and `subtype`, which
say whether something is a bridge or a tunnel and how levels stack *relative to each other* — but
never a height in metres, and never a ground elevation. Neither do buildings, beyond the optional
`height` / `num_floors` attributes on the building itself.

So elevation is always something you pair *with* an extract, from a DEM. This page records where to
get one, the trap that makes the result meaningless, and how the pieces fit together.

## DTM and DSM are different questions

| | what it is | what it answers |
|---|---|---|
| **DTM** (terrain) | bare earth, buildings and vegetation removed | road gradient, slope, drainage |
| **DSM** (surface) | the top of whatever is there — roofs, tree canopy | what blocks light or view |
| **nDSM** = DSM − DTM | height of objects above ground | building heights, canopy height, shadows |

They are not better and worse versions of the same thing. For road gradient you want the **DTM** — a
DSM puts a street node on the tree canopy hanging over it. For anything about what stands on the
ground, you need **both**, because only the difference is meaningful.

## The resolution trap

> Two independent global DEMs cannot be differenced into object heights, no matter how convenient it
> looks.

Measured on Södermalm, differencing EU-DTM (30 m, bare earth) against Copernicus GLO-30 (30 m,
surface) at 2,651 road nodes:

| | |
|---|---|
| mean difference | +1.14 m |
| range | −16.8 m to +15.0 m |
| **DSM below DTM** | **38.3%** of nodes |

A surface model can never sit below bare earth, so nearly two nodes in five were physically
impossible. The difference between those two rasters is disagreement between two separately-produced
datasets, not objects. Two causes, both fatal: they come from different acquisitions with different
vertical references, and at 30 m one pixel spans a whole city block, averaging roof and street into
a single number.

**Rule: use a DTM and DSM from the same acquisition, at a resolution finer than the objects you are
measuring.** For buildings and trees, that means LiDAR at 1–2 m.

## Sources

### Canada — NRCan HRDEM (what the Burnaby/Vancouver extracts use)

The [CanElevation HRDEM series](https://open.canada.ca/data/en/dataset/957782bf-847c-4644-a757-e383c0057995)
publishes a **matched 1 m DTM and DSM pair from one LiDAR flight**, on public S3 with no
credentials, under the Open Government Licence – Canada. Vertical datum CGVD2013.

```
https://canelevation-dem.s3.ca-central-1.amazonaws.com/hrdem-lidar/
    BC-Lower_Mainland_2016-1m-dtm.vrt      # bare earth
    BC-Lower_Mainland_2016-1m-dsm.vrt      # surface
    BC-Lower_Mainland_2016-1m-extent.geojson
```

`BC-Lower_Mainland_2016` covers all of Metro Vancouver (−123.29→−121.34, 49.0→49.51), so both the
Burnaby and Granville Island extracts sit inside one tile. Project names follow
`<province>-<area>-<res>-<dtm|dsm>`; list the bucket to find others.

Sampled at Metrotown, SFU and Brentwood the pair gives object heights of 9.4 m, 32.7 m and 7.3 m —
all positive, all plausible. Over the whole 16 km² Granville Island clip, only **0.92%** of pixels
come out below zero, against 38% for the Södermalm 30 m pair.

### Sweden

No equivalent matched pair is wired up. Lantmäteriet publishes a laser-scanned national height model
at 1 m grid as open data, and laser point clouds from which a surface model can be derived; Stockholm
stad publishes high-resolution city data too. Until one of those is fetched, Stockholm extracts have
only the 30 m products, which are fine for terrain and **useless for object heights** — see above.

### Anywhere

`duckosm elevation --source copernicus` streams Copernicus GLO-30 (a DSM) globally with no auth, and
`--source eudtm` fetches EU-DTM inside Europe with a free OpenTopography key. Both are 30 m: good for
terrain, not for objects.

## Clipping a working raster

Full provincial rasters are hundreds of gigabytes; clip to your extract with a buffer. GDAL reads the
remote VRT directly over `/vsicurl/`, so nothing is downloaded but the window you ask for:

```bash
gdal_translate -of COG -projwin <xmin> <ymax> <xmax> <ymin> \
  -co COMPRESS=DEFLATE -co PREDICTOR=3 -co BIGTIFF=YES -co NUM_THREADS=ALL_CPUS \
  /vsicurl/https://canelevation-dem.s3.ca-central-1.amazonaws.com/hrdem-lidar/BC-Lower_Mainland_2016-1m-dtm.vrt \
  burnaby-1m-dtm.tif
```

`-projwin` is in the raster's own CRS (EPSG:3979 for HRDEM), *not* lon/lat — convert with
`rasterio.warp.transform_bounds`.

**Buffer for what you are computing.** For shadows the margin must hold the shadows of buildings
*outside* your boundary that fall inside it. At 49°N the December sun sits ~17° above the horizon, so
a shadow runs about 3.2× the building's height; downtown Vancouver's ~200 m towers throw ~640 m. The
Burnaby and Granville clips use 1 km.

## Putting elevation on the road network

duckOverture has no elevation command — `duckosm elevation` does the sampling, against a duckOSM
build of the same area. Keep the two extracts on the same boundary and they stay comparable:

```bash
duckosm elevation burnaby.duckdb --dem data/dem/burnaby-1m-dtm.tif
duckosm elevation burnaby.duckdb --dem data/dem/burnaby-1m-dsm.tif --suffix dsm
```

That gives `nodes.ele` / `ele_dsm` and `edges.z_from(_dsm)` / `z_to(_dsm)`, with one provenance row
per column in `main.elevation_metadata`. Height above ground is `ele_dsm - ele`.

For **buildings**, the road-node sampler is the wrong tool — sample the nDSM raster over each
building polygon (zonal statistics: the 75th percentile of nDSM inside the footprint is a more robust
roof height than the mean, which is dragged down by edge pixels). That is worth doing even where
Overture has a `height`: coverage is uneven, and where it exists it is a single number for a whole
massing.

| extract | buildings | with `height` |
|---|---|---|
| Södermalm | 3,346 | 10 (0.3%) |
| Burnaby | 48,737 | 27,535 (56%) |
| Granville Island | 630 | 339 (54%) |

## Where the current rasters live

Kept outside this repo (they are ~1 GB) in `duckOSM/data/dem/`, which is gitignored:

| file | area | size |
|---|---|---|
| `burnaby-1m-dtm.tif` / `-dsm.tif` | 294 km², Burnaby + 1 km | 904 MB / 1.04 GB |
| `granville_island-1m-dtm.tif` / `-dsm.tif` | 16 km², island + 1 km | 48 MB / 56 MB |

All COGs with overviews, 1 m, EPSG:3979, CGVD2013, nodata −32767.

Granville Island is the pilot: at 4 × 4 km the whole nDSM fits in memory as one array, so an
algorithm can be prototyped with no tiling and then scaled to Burnaby unchanged.
