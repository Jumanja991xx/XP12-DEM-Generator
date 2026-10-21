# XP12 DEM Generator

**High-resolution LiDAR terrain for X-Plane 12 in a few clicks — for XPME, AutoOrtho and
default-scenery users alike.**

XP12 DEM Generator downloads accurate elevation data — [Sonny's LiDAR Digital Terrain Models of
Europe](https://sonny.4lima.de), USGS 3DEP for the USA and FABDEM for the rest of the world — and
puts it into the X-Plane 12 terrain you already fly with:
X-Plane Map Enhancement (XPME), AutoOrtho, Ortho4XP tiles or X-Plane's default scenery.
No Ortho4XP knowledge, no copying folders, no editing `scenery_packs.ini`.

| 1. Choose a region | 2. Choose method and DEM resolution |
|---|---|
| ![Region](docs/img/1_region.png) | ![Method](docs/img/2_resolution.png) |
| **3. Create & install** | **4. Done** |
| ![Create](docs/img/3_create.png) | ![Done](docs/img/4_done.png) |

## Why?

X-Plane's default mesh, XPME's base packages and the stock AutoOrtho packs are all built
from Viewfinder Panoramas / SRTM elevation data — 30 m in the Alps, 90 m in most other
places, with radar artefacts in steep terrain. Sonny's models are compiled from official
airborne-laser-scan (LiDAR) surveys and are far more accurate, especially on sharp ridges,
in narrow valleys and in forests. For VFR flying through the mountains the difference is
very visible.

## Two methods

| | **Improve my current terrain** (default) | **Build a new, denser mesh** |
|---|---|---|
| Works with | **XPME**, AutoOrtho, Ortho4XP packs, **default X-Plane scenery (no ortho)** | AutoOrtho only |
| What it does | Takes the base mesh X-Plane currently uses for each tile and replaces the vertex heights and normals with LiDAR values | Ortho4XP builds a completely new mesh from the LiDAR data |
| Textures, water, roads, forests | unchanged — still delivered by XPME / AutoOrtho / X-Plane | new Ortho4XP mesh, photos from AutoOrtho |
| Detail | as dense as your current mesh, but with correct heights | densest (selectable mesh detail) |
| Needs internet for | DEM download | DEM download + OpenStreetMap (Overpass) |
| Time per 1°×1° tile | ~2 min | 2–6 min |

**Improve** is the right choice for almost everyone. How it works:

1. For every tile the app reads `Custom Scenery/scenery_packs.ini` and finds the base mesh
   X-Plane really uses — the highest-priority pack with a DSF for that tile (overlays such as
   simHeaven are skipped), or the X-Plane 12 Global Scenery if no pack covers it.
2. It converts that DSF to text with Laminar's DSFTool, writes LiDAR elevations and matching
   vertex normals (for correct hill shading) and updates the XP12 elevation raster.
   Triangulation, terrain types, water, roads, forests and objects are left exactly as they were.
3. Terrain definitions of photo packs (XPME/AutoOrtho) are copied with their texture paths
   pointing back to the original pack (`../../XPME_Europe/textures/…`), so XPME or AutoOrtho
   keeps streaming the photos.
4. Heights are **not** changed on water (lakes and sea stay flat), **around runways** (airport
   flattening by X-Plane, XPME or Ortho4XP is preserved — runways come from `apt.dat`) and
   towards tile edges whose neighbour is not processed (no steps at the seams). Transitions
   are smooth.
5. The result is installed as `Custom Scenery/z_dem_<region>` and listed in
   `scenery_packs.ini` **directly above** the pack it improves.

## Quick start (Windows 10/11)

1. Download **`XP12DEMGenerator-Setup.exe`** from the [latest release](../../releases/latest)
   (or from the newest successful [build run](../../actions) → *Artifacts*) and run it. It installs
   per user (no administrator rights), adds a Start-menu entry and an uninstaller. Windows
   SmartScreen may warn because the installer is not code-signed: *More info → Run anyway*.
   A portable ZIP (`XP12DEMGenerator-windows.zip`) is available as well.
2. **XPME / AutoOrtho users:** start XPME or AutoOrtho first, so its scenery folders can be
   read. The app warns you if a pack in `scenery_packs.ini` is empty (= not running).
3. **Region** — pick a preset (Alps, Tyrol, Dolomites, Pyrenees, Norway, Budapest, Colorado,
   Sierra Nevada, Cascades, Grand Canyon, Alaska, Hawaii…) or click
   your own 1°×1° tiles on the map (OpenTopoMap / OpenStreetMap with borders, places and contour lines —
   free, no API key; offline a built-in Natural Earth map with country borders is shown).
   The map works like any web map: drag to move, mouse wheel or **+/−** to zoom, click a tile
   to (de)select it, **Shift+drag** selects many tiles, right-drag removes them.
   Tile colours show the data quality: **green** = LiDAR (best), **blue** = national model (good),
   **red** = satellite only (basic); *Highlight data quality* makes them stand out.
   **Orange** tiles are already installed (by any pack of this app, also next to XPME),
   **dashed** tiles have their elevation data downloaded but are not built yet. The
   background map is shown in grey so the colours are easy to tell apart. Below the map the app
   counts how many selected tiles are installed and not built yet, and which data quality they get — *Select only missing tiles*
   keeps just the ones you still need. If you build into a pack that already contains some
   of the selected tiles, the app asks whether to build only the new tiles or replace them.
4. **Method & resolution** — keep *Improve my current terrain*, choose 1″ (≈30 m) or 3″ (≈90 m).
5. **Create** — the summary shows which terrain each tile currently uses; click
   *Create & Install*.
   The remaining time is estimated in seconds per tile; the app remembers how long tiles take
   on your PC, so from the second build on the estimate matches your hardware.
   **Delete data…** (bottom left) removes downloaded data, caches, settings and — only if you
   tick it — the installed packs of this app. It asks twice; other scenery is never touched.
6. If something fails (e.g. XPME was not running), fix the cause and click **Retry failed tiles** —
   only the failed tiles are built again.
7. Start X-Plane 12 (with XPME/AutoOrtho as usual) and fly. The new terrain is stored locally;
   the app does not need to run while flying.

## Notes for XPME users

* XPME's base packages are Ortho4XP-style meshes served through its own virtual drive. The
  improved pack re-uses XPME's textures, so **XPME must stay installed and running**.
* If XPME keeps its packages outside X-Plane (absolute paths such as `D:/XPME/XPME_Europe/` in
  `scenery_packs.ini`), the improved pack is created **in the same folder** and registered with
  its full path, so the texture references keep working.
* XPME can re-order `scenery_packs.ini` when it starts. The `z_dem_…` pack must stay **above**
  the XPME packages; if XPME moves it, disable XPME's automatic ordering or move the line up.
* After XPME updates its base packages, or after you flatten an airport in XPME, build the
  region again (the pack's `xp12dem_pack.json` records which source each tile came from).

## What gets installed where

* **Improve:** `X-Plane 12/Custom Scenery/z_dem_<region>` with `Earth nav data/*.dsf` and, for
  photo packs, small copies of the `.ter` files. Nothing of the original scenery is modified.
* **New mesh:** `z_ao_dem_<region>` with terrain only (`Earth nav data`, `terrain/*.ter`, water
  masks). The photo textures it references (`textures/<row>_<col>_BI16.dds`, ZL16 with ZL18
  around airports) are exactly the names AutoOrtho generates on the fly. In AutoOrtho mode the
  pack goes to `<AutoOrtho scenery path>/z_autoortho/scenery/`, AutoOrtho mounts it on start-up.
* `scenery_packs.ini` is updated so the new pack wins over the mesh it replaces. A backup is
  written to `scenery_packs.ini.xp12dem.bak`.
* Building the same pack name again adds new tiles and replaces rebuilt ones.
* Work files (DEM cache, OSM cache, logs) live in `%LOCALAPPDATA%\XP12DEMGenerator`.
  Delete that folder to reclaim disk space; set the `XP12DEM_DATA` environment variable
  to move it.

## Options explained

| Option | Values | Notes |
|---|---|---|
| Method | **Improve** · **New mesh** | See above. |
| DEM resolution | **1″** (≈30 m) · **1/3″** (≈10 m) · **3″** (≈90 m) | 1/3″ where 1–10 m data exists, otherwise 1″; 3″ is resampled from 1″ (3×3 mean). |
| Mesh detail (new mesh only) | **Balanced** · **High** · **Maximum** | Ortho4XP `curvature_tol` 2.0 / 1.2 / 0.8 and `limit_tris` 3 / 4 / 5 M triangles per tile. |
| Tiles at the same time | Automatic / 1 – 8 | *Automatic* picks the best value from CPU threads and RAM (about 2 GB per tile for *improve*, 3 GB for *new mesh*, 4 GB kept free). |
| Own elevation files | optional | Folder with your own GeoTIFFs (any projection), `.hgt`, Sonny `.zip` or FABDEM files. Used first; missing tiles are downloaded automatically. |

## Elevation data sources

Every 1°×1° tile automatically gets the best free source for its area — no account, no API key.
Weaker sources only fill gaps (sea, the edge of a survey) and are blended in over ~300 m, so no
step is visible. The map colours show the result before you build (hover a tile for details,
*Where does the data come from?* on step 1 shows the overview):

| Colour | Source | Where | Finest |
|---|---|---|---|
| 🟩 Best | **Sonny's LiDAR DTM** (CC BY 4.0) | most of Europe (Alps, Germany, France, Italy, Spain, UK, Poland, …) | 1″ (30 m) |
| 🟩 Best | **USGS 3DEP** (public domain) | USA incl. Alaska, Hawaii, Puerto Rico | 1/3″ (10 m) |
| 🟩 Best | **LINZ LiDAR 1 m** + national 8 m DEM (CC BY 4.0) | New Zealand | 1/3″ |
| 🟩 Best / 🟦 Good | **NRCan HRDEM LiDAR** + **MRDEM 30 m** (Open Government Licence – Canada) | Canada (LiDAR where surveyed, ~2 million km²) | 1/3″ |
| 🟦 Good | **Sonny's DTM — national models** | Norway, Sweden, Finland, Baltics, Iceland | 1″ |
| 🟦 Good | **GSI elevation tiles** 5–10 m | Japan | 1/3″ |
| 🟦 Good | **ArcticDEM 10 m** (CC BY 4.0) | Greenland, Svalbard and land above ~67° N | 1/3″ |
| 🟥 Basic | **FABDEM V1-2** (non-commercial licence), **Copernicus GLO-30** as gap filler | everywhere else | 1″ |

* All reading is partial: the national mosaics are Cloud Optimized GeoTIFFs of up to 80 GB, the
  app downloads only the part and the detail level it needs (a few MB to ~150 MB per tile).
* ArcticDEM heights refer to the ellipsoid; the app converts them to heights above sea level
  by calibrating against Copernicus GLO-30 (30–70 m difference in Greenland/Iceland).
* Sonny's tiles come from the per-tile mirror [mg4gh/hgtdata](https://github.com/mg4gh/hgtdata)
  (Europe 1″ v24).
* FABDEM is published as 10°×10° zip files; the app reads just the needed tile out of the remote
  zip. If the server does not allow that, the log names the zip to download from
  [data.bris.ac.uk](https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn) into the
  *Own elevation files* folder; meanwhile Copernicus GLO-30 is used.
* Australia: FABDEM; free LiDAR (1–5 m) from ELVIS can be added as own files.
* **Own elevation files** (step 2, Advanced): any GeoTIFF in any projection (found by its
  coordinates, not its name), SRTM `.hgt`, Sonny `.zip`, FABDEM zip/`.tif`. These files win over
  everything else. Free 1–10 m data you can add this way: swissALTI3D (swisstopo), Austria's
  ALS DTM (data.gv.at), Bavaria's DGM1 (geodaten.bayern.de), South Tyrol's DTM, Australian
  LiDAR from ELVIS (elevation.fsdf.org.au), Mexico's CEM (INEGI).
* **Resolution:** 1″ (≈30 m) is the default. 1/3″ (≈10 m) uses the full detail where a 1–10 m
  source covers the tile (others fall back to 1″) — files are 9× bigger (233 MB per tile) and
  builds slower. 3″ is resampled from 1″.
* The pack's `xp12dem_pack.json` and `README.txt` record the sources of every tile and the
  required attributions.
* **New mesh mode only:** water, coastline, airport and road geometry from OpenStreetMap via Overpass.

## Command line

The same pipeline is available without the GUI:

```
XP12DEMGenerator.exe build --region tyrol --resolution 1                  # improve current terrain
XP12DEMGenerator.exe build --tiles +46+007,+46+008 --name "Valais" --parallel 2
XP12DEMGenerator.exe build --region alps --method rebuild --mesh high      # new mesh for AutoOrtho
```

Run `XP12DEMGenerator.exe build --help` for all options (`--method`, `--xplane`, `--target`,
`--dem-folder`, `--no-install`).

## Building from source

```
python -m venv .venv
.venv\Scripts\activate           # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m xp12dem                # start the GUI
python -m pytest                 # run the tests
pyinstaller --noconfirm packaging/xp12dem.spec   # -> dist/XP12DEMGenerator/
iscc /DMyAppVersion=0.1.0 packaging/installer.iss  # -> dist/XP12DEMGenerator-Setup-0.1.0.exe
```

The app also runs on Linux (Ortho4XP's Linux binaries are included). Every push builds
and smoke-tests the Windows executable with GitHub Actions; tags `v*` publish a release.

### How it works

```
xp12dem/
  gui/            PySide6 wizard (map widget, 4 steps)
  regions.py      presets and 1°×1° tile helpers
  dem.py          per-tile cache, source chain with soft gap filling, 3″ resampling
  sources.py      elevation sources (Sonny, USGS, LINZ, NRCan, GSI, ArcticDEM, FABDEM, GLO-30, own files)
  raster.py       partial COG reading over HTTP, reprojection onto the tile grid
  pipeline.py     orchestration: DEM → per-tile worker → pack → install
  eta.py          remaining-time estimate (remembers build times per PC)
  inventory.py    installed packs and downloaded DEM tiles shown on the map
  cleanup.py      "Delete data" (caches, downloads, installed packs)
  reheight.py     "improve" mode: find the active mesh, apply LiDAR heights via DSFTool
  o4x_worker.py   worker process (one per tile): re-height job or headless Ortho4XP
  installer.py    pack assembly, AutoOrtho / Custom Scenery install, scenery_packs.ini
  xplane.py       X-Plane 12 and AutoOrtho detection
third_party/Ortho4XP   unmodified Ortho4XP sources + Triangle4XP / DSFTool / 7-Zip
```

`xp12dem-worker.exe` is an internal helper that the app starts itself — never run it manually.
Each tile is processed by a separate `xp12dem-worker` process, which keeps the window responsive,
lets several tiles run in parallel and isolates memory use and crashes of the heavy mesh tools. In *new mesh* mode it configures Ortho4XP
(Sonny DEM as `custom_dem`, imagery download disabled, XP12 water, masks as separate
PNGs for AutoOrtho), then runs Ortho4XP's normal steps: vector data → Triangle4XP
mesh → masks → DSF.

## Troubleshooting

* **"Not readable: XPME_…"** — the pack's folder is empty because XPME (or AutoOrtho) is not
  running. Start it and build again; otherwise the new heights stay hidden below that pack.
* **"AutoOrtho (not found)"** (new mesh mode) — start AutoOrtho once and set its X-Plane /
  scenery paths; the app reads them from `%USERPROFILE%\.autoortho`.
* **Tiles show no photos** — AutoOrtho is not running or was not restarted after the install.
* **Old terrain still visible** — check that `z_dem_…` / `z_ao_dem_…` is listed above the
  pack it replaces (`XPME_…`, `z_ao_eur`, …) in `Custom Scenery\scenery_packs.ini`.
* **OSM download is slow / fails** (new mesh mode only) — the public Overpass servers are sometimes busy; the
  app rotates through six servers and gives up after 10 minutes per query with a clear
  message. Just run the build again later — finished downloads are cached.
* Detailed per-tile logs: `%LOCALAPPDATA%\XP12DEMGenerator\logs`.

## Credits and licenses

* Elevation data © Sonny, https://sonny.4lima.de — CC BY 4.0
* USGS 3D Elevation Program — public domain; FABDEM V1-2 © University of Bristol (non-commercial licence)
* Ortho4XP © Oscar Pilote and contributors — GPL v3 (vendored in `third_party/Ortho4XP`)
* DSFTool © Laminar Research (bundled with Ortho4XP)
* Map data © OpenStreetMap contributors — ODbL; base map © OpenTopoMap (CC-BY-SA); offline map: Natural Earth
* XP12 DEM Generator source code — MIT (see `LICENSE`). Binary releases bundle Ortho4XP and
  are therefore distributed under the GPL v3. Details in `THIRD_PARTY_NOTICES.md`.

Not affiliated with Laminar Research, Sonny, XPME, AutoOrtho or Ortho4XP.
