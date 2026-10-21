# Vendored Ortho4XP

Upstream: https://github.com/oscarpilote/Ortho4XP
Commit:   26ec00acce4a2b42b79d2b618e491c1310f5d0b0 (2026-03-14)
License:  GNU GPL v3 (see Licence/)

Only the parts needed for headless mesh/DSF generation are included:
`src/`, the Triangle4XP / DSFTool / 7-Zip binaries for Windows and Linux,
the Bing layer definition and the colour filters. The Ortho4XP sources are
unmodified; XP12 DEM Generator drives them through `xp12dem/o4x_worker.py`.
