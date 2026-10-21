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

## OpenStreetMap — ODbL

Water, coastline, airport and road geometry used while meshing is downloaded
at run time from the Overpass API. © OpenStreetMap contributors,
https://www.openstreetmap.org/copyright

## Base map tiles — CARTO / OpenStreetMap

The region map loads "Dark Matter" raster tiles from CARTO
(https://carto.com/attributions), rendered from OpenStreetMap data.
© OpenStreetMap contributors © CARTO. Tiles are cached locally and only loaded
while the map is visible.

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
