# Third-party notices

XP12 DEM Generator's own source code (`xp12dem/`, `packaging/`, `tests/`) is
released under the MIT license (see `LICENSE`). It builds on the following
works:

## Ortho4XP — GNU GPL v3

* Copyright © Oscar Pilote and contributors
* https://github.com/oscarpilote/Ortho4XP
* Vendored, unmodified, in `third_party/Ortho4XP` (see `VENDORED.md` there for
  the exact commit). License text: `third_party/Ortho4XP/Licence/gpl.txt`.
* Includes the Triangle4XP mesher (based on J. R. Shewchuk's Triangle) and
  Laminar Research's DSFTool binaries, plus 7-Zip (LGPL) for Windows.

Because the Windows/Linux builds bundle Ortho4XP, **the distributed binaries
are covered by the GNU GPL v3**. The corresponding source is this repository.

## Sonny's LiDAR Digital Terrain Models of Europe — CC BY 4.0

* Elevation data by Sonny, https://sonny.4lima.de
* Downloaded at run time (not redistributed in this repository) from the
  per-tile mirror https://github.com/mg4gh/hgtdata.
* Generated scenery packs contain this attribution in their `README.txt`.

## USGS 3D Elevation Program (3DEP) — public domain

1 and 1/3 arc-second elevation tiles for the USA, downloaded at run time from
https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/ .

## FABDEM V1-2 — University of Bristol, non-commercial licence

Hawker, Uhe, Paulo, Sosa, Savage, Sampson & Neal (2022): "A 30 m global map of
elevation with forests and buildings removed", Environ. Res. Lett. 17 024016.
https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn — based on
Copernicus GLO-30 (© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH
2014-2018, provided under COPERNICUS by the European Union and ESA). Free for
non-commercial use; downloaded at run time, not redistributed.

## Copernicus DEM GLO-30

© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided
under COPERNICUS by the European Union and ESA; all rights reserved. Read at
run time from the AWS Open Data bucket `copernicus-dem-30m` (used to fill gaps
and to convert ArcticDEM heights to sea level); not redistributed.

## LINZ elevation data (New Zealand) — CC BY 4.0

Toitū Te Whenua Land Information New Zealand, LiDAR 1 m DEMs and the national
8 m DEM, read at run time from the AWS Open Data bucket `nz-elevation`.

## NRCan HRDEM / MRDEM (Canada) — Open Government Licence – Canada

Contains information licensed under the Open Government Licence – Canada.
Natural Resources Canada High Resolution DEM (LiDAR mosaic) and Medium
Resolution DEM (30 m), read at run time from `canelevation-dem`.

## ArcticDEM — CC BY 4.0

DEMs provided by the Polar Geospatial Center under NSF-OPP awards 1043681,
1559691, 1542736, 1810976 and 2129685. Read at run time from the AWS Open Data
bucket `pgc-opendata-dems`.

## GSI elevation tiles (Japan)

Geospatial Information Authority of Japan (国土地理院) elevation tiles
(https://maps.gsi.go.jp/development/ichiran.html), read at run time with
attribution as required by the GSI terms of use.

## tifffile / imagecodecs — BSD

https://github.com/cgohlke/tifffile , https://github.com/cgohlke/imagecodecs

## OpenStreetMap — ODbL

Water, coastline, airport and road geometry used while meshing is downloaded
at run time from the Overpass API. © OpenStreetMap contributors,
https://www.openstreetmap.org/copyright

## Base map tiles — OpenTopoMap / OpenStreetMap

The region map loads raster tiles from OpenTopoMap (https://opentopomap.org,
map style CC-BY-SA, data © OpenStreetMap contributors, SRTM) and falls back to
the standard OpenStreetMap tiles (https://tile.openstreetmap.org). Both are
free and need no API key; the app identifies itself with its User-Agent,
caches tiles locally for 30 days and only loads what is visible.

## Natural Earth — public domain

`xp12dem/resources/europe_land.json` and `europe_borders.json` (offline map)
are derived from Natural Earth 1:50m land, admin-0 boundary lines and country
label points, https://www.naturalearthdata.com

## Qt for Python (PySide6) — LGPL v3

https://www.qt.io/qt-for-python — shipped as unmodified shared libraries in
the binary distribution.

## Python packages

numpy (BSD), requests (Apache 2.0), Pillow (MIT-CMU), shapely (BSD),
pyproj (MIT), Rtree (MIT) / libspatialindex (MIT), scikit-fmm (BSD).
