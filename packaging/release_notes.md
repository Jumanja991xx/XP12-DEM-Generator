First public release — sharper, more accurate mountains for X-Plane 12 from laser-scan (LiDAR)
elevation data. Free, for private use. Tested by one person on one PC so far (Windows 11,
XPME, X-Plane 12.4) — feedback and bug reports are very welcome in
[Issues](https://github.com/Jumanja991xx/XP12-DEM-Generator/issues).

**Download:** `XP12DEMGenerator-Setup.exe` below (installs per user, no admin rights; SmartScreen
may warn because it is not code-signed: *More info → Run anyway*). A portable ZIP is attached too.

### What it does

* **Build a new, denser mesh** — Ortho4XP builds a new mesh from LiDAR data, with about twice
  the triangles of XPME's mesh, placed on ridges, cliffs and gullies. The photos come straight
  from **XPME** (or AutoOrtho) — no photo download. Runways keep their real profile, aprons are
  laid level.
* **Improve my current terrain** — keeps XPME's (or X-Plane's) mesh and replaces its heights with
  LiDAR values. Fast (~2 min per tile), keeps XPME's water, roads and airport flattening.
* Best free elevation data per tile, automatically: Sonny's LiDAR DTM (Europe), USGS 3DEP (USA),
  LINZ (New Zealand), NRCan (Canada), GSI (Japan), ArcticDEM, FABDEM elsewhere — or your own files.
* Map with data quality, installed tiles, one-click install into `scenery_packs.ini` above XPME,
  *Fix now* for packs out of order, *Remove selected from X-Plane* to go back to XPME.

See the [README](https://github.com/Jumanja991xx/XP12-DEM-Generator#readme) for the comparison
figures, screenshots and how it works.

### Good to know

* XPME (or AutoOrtho) must be running while you fly — it delivers the photos.
* A new mesh takes ~10–25 min per 1°×1° tile (OpenStreetMap download + triangulation).
* Licences: app MIT, binaries GPL v3 (bundles Ortho4XP). Elevation data © Sonny (CC BY 4.0),
  USGS, LINZ, NRCan, GSI, PGC, University of Bristol (FABDEM, non-commercial). Not affiliated with
  Laminar Research, XPME, AutoOrtho, Ortho4XP or Sonny.
