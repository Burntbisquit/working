# Plumbline

**Planimetric and topographic CAD for survey data** - a desktop app (Python + Qt) that takes your points, LandXML, DXF and GIS
data in, lets you lay it over imagery, builds surfaces / contours / volumes, and sends DXF, LandXML, GIS, Google Earth
files and reports back out. It is built to be grown: every piece is plain Python, there is a plugin / script / console layer,
and a headless command line.

**It is also the field-data tool now.** Fieldwork Manager - the raw-download front end (duplicate checks, description parsing,
renumbering, the field-book viewer) - lives inside this program as *Survey > Fieldwork Manager*. Same job folder, same code
table, one window per half of the work: reduce the download, then draw it.

> *Plumbline is a working name* - change `APP_NAME` in `plumbline/__init__.py`.
> **This is v0.1, built from scratch against synthetic data. Treat it as a strong foundation, not a finished product** -
> see [What was verified, and what wasn't](#what-was-verified-and-what-wasnt) before trusting it with a deliverable.

![Plumbline main window](docs/img/main_light.png)

## Contents
[Install and run](#install-and-run) · [Licence, first run and downloads](#licence-first-run-and-what-it-downloads) ·
[A 10-minute tour](#a-10-minute-tour) · [Field data](#field-data-the-other-half-of-the-job) ·
[Job folders](#job-folders) · [Samples](#the-two-samples) · [Coordinate systems](#coordinate-systems) ·
[Imagery](#imagery-and-an-honest-word-on-accuracy) · [Import / export](#import-and-export) ·
[Feature codes](#feature-codes-and-linework) · [Surfaces](#surfaces-contours-volumes-profiles) ·
[3D / depth view / Google Maps](#3d-view-depth-view-and-google-maps) · [Reports](#reports) ·
[Make it yours](#make-it-yours) · [Project layout](#project-layout) · [Verified / limits](#what-was-verified-and-what-wasnt) ·
[Next steps](#where-id-take-it-next)

## Install and run

**Windows, and new to the command line?** Follow [`docs/WINDOWS_SETUP.md`](docs/WINDOWS_SETUP.md). It checks your Python (3.14 preferred, 3.13 also works) and PATH, then you
either type five commands in PowerShell or double-click `install_windows.bat`. After that, double-click `Plumbline.bat` whenever you want to work -
or `Update and Run Plumbline.bat` to fetch the latest code with git first, refresh the libraries only when `requirements.txt` changed, and then start the program (see Step 5 of the guide).

**Linux / macOS, or if you like terminals:**

```bash
python3.14 -m venv .venv && source .venv/bin/activate    # (or python3.13; Windows: skip this line - see the Windows guide)
pip install -r requirements.txt                           # or:  pip install -e .
python -m plumbline doctor                                # checks every library is installed and really works
python -m plumbline                                       # opens the welcome window
```

**To run the tests**, install the development extra in that environment and run pytest headlessly (the suite includes Qt UI tests):

```bash
python -m pip install -e ".[dev]"
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

GitHub Actions runs the suite on both supported Python versions, 3.13 and 3.14.

* **Python 3.13 or newer, 64-bit** (`requires-python` is `>=3.13`). **Python 3.14 is the version this release is built and tested against**, and the one `install_windows.bat` picks when it is available; the full test suite passes on 3.14 (on Linux). It also passes unchanged on **3.13** (verified on Linux), so 3.13 is supported too - you just get an advisory note from `doctor`. Anything newer than 3.14 is untested: PySide6 may not have a build for it yet. On Windows every library has a ready-made 64-bit wheel for 3.14 (checked with pip's resolver); everything installs from wheels (PySide6, pyproj, shapely, pyogrio, ezdxf, rasterio ...).
* `rasterio` is optional (GeoTIFF orthophotos); without it PNG/JPG + world file images still work.
* Linux needs the usual Qt system libraries (`libxkbcommon0`, `libegl1`, `libxcb-*` ...) - if Qt complains on start, install them.
* On a headless Linux machine (a server, a cloud sandbox) `tools/live_preview.sh` runs the real window on a virtual screen and serves it to a web browser
  through noVNC (needs `xvfb openbox x11vnc novnc websockify wmctrl`), so you can look at it and click around without a monitor.
* Developed and tested on **Linux (headless/offscreen Qt) only**. PySide6 is cross-platform and I audited the code for OS-specific behaviour
  (explicit UTF-8 for every text file - a test enforces that - fonts that fall back to the system's own, no POSIX-only calls), but I have
  **not run it on Windows or macOS**; the Windows scripts were written from the python.org / PyPI documentation. `python -m plumbline doctor`
  tells you quickly if anything is off.
* `python -m plumbline sample sample_data` regenerates the synthetic sample files, `sample-job` writes it as a job folder, and
`sample-real` rebuilds the Real World sample; `pytest` runs the tests (set `QT_QPA_PLATFORM=offscreen` on a server).

## Licence, first run and what it downloads

**The licence comes first.** This build is licensed for **testing and private use only** - commercial use is not granted. On the
first run the agreement is shown before the main window exists: **Part 1** is the licence (the commercial-use limitation), **Part 2**
is the general end-user agreement, and "I Agree" stays disabled until the box is ticked. Declining closes the program rather than
hiding the question. `Help > Licence Agreement...` shows the same words at any time, and the two files on disk - `LICENSE.md` and
`EULA.md` - are written from the same source the dialog reads, so they cannot drift apart. Change the wording and the version goes
up, and the agreement is put in front of you again.

**Nothing large ships inside the program.** Geoid models, datum-shift grids, vertical-datum tables and imagery
sources are **flags with a URL**, not files in the download - a few hundred megabytes of grids stay off your disk until you ask for
them. The first run offers the list, **ticked**, with what each item is for, what stops working without it and its size; untick what
you don't want, and nothing is fetched behind your back. "Later" is a real answer, and every source stays visible and editable in
**Settings > External data sources...** (`python -m plumbline external` prints the same list, `--download` fetches it). Imagery is
deliberately not pre-downloaded: tiles are cached one at a time as you look at the map. When a grid is genuinely missing, the
conversion says so rather than guessing.

## A 10-minute tour

1. **File > Open Sample Project** (a synthetic lot with a street, building, utilities and ~100 ground shots).
2. **Survey > Data Quality Check** - it finds the planted +4.9 ft elevation bust (point 186). Click the finding to select the point.
3. **Surface > Create Surface** (defaults are fine), then **Surface > Create Contours**. The bust shows up as a tight cluster of contours.
4. Select the building outline, **Reports > Line and Curve Table** (bearings, distances, area).
5. **Surface > Volumes** - surface to a datum, or between two surfaces, optionally inside a selected closed polyline.
6. **File > Export > DXF**, open it in AutoCAD / BricsCAD: real layers and colours, arcs as arcs, contours at elevation.
7. **Imagery > Add Imagery > Esri World Imagery**, then *Look up imagery source* in the Imagery panel (more on that below).
8. **View > 3D View** (drag to orbit), **View > Depth View** (turn the dial to look from any bearing), and **Imagery > Open View Center in Google Maps**.

Typed coordinates work in every drawing tool (box in the toolbar): `N,E` (or `E,N` - see Settings), `@dN,dE`, `@100<N45-30E`,
or just a point number. Drag and drop any supported file onto the drawing.

## Field data (the other half of the job)

**Survey > Fieldwork Manager** (`Ctrl`+nothing - it is on the menu, not a keystroke away) opens the field-data window beside the drawing:

![Fieldwork Manager inside Plumbline](docs/img/fieldwork_manager.png)

It is the same program that used to be Fieldwork Manager, with its five tabs (Raw Field Data, Notes, Consolidate, Check, Field Book),
now pointed at the open job folder and sharing the job's feature code table. It runs in its own window on purpose: you reduce a
download in the morning and draw in the afternoon, and it can go on a second monitor or be closed entirely. If it fails to load,
that menu item is disabled with a reason and **nothing else in the program is affected**.

* **Survey > Fieldwork Manager** - reduce a download: duplicate point numbers, points on top of each other, descriptions that do not
  parse against the office standard, renumbering, the field-book viewer.
* **Survey > Import Cleaned Field Data** - bring a consolidated `.fwk` straight into the open project, choosing what happens to point
  numbers that already exist (renumber / overwrite / skip).
* **Plumbline > Send Cleaned Points to Plumbline** (inside the field window) - the same import, from the other side, with the points
  as they stand on the Edit Fieldwork tab rather than as they stand on disk.
* Everything works headless too: `python -m plumbline fieldwork job.fwk --f2f "CARLSON F2F.csv"` prints the findings for a download
  without opening a window.

**The two description dialects are handled as one language.** A crew writes `EP1 B` (code + string number + begin flag); an office
standard writes `THACK22` and `58CIRST / BLUE ARS` (numbered codes, slash free text). 1,314 of the 1,717 codes in a real Texas office
standard end in a digit, so *"the digits after a code are a string number"* is wrong half the time. A code that is **literally in the
code table wins**; otherwise the digits are read as a string number. On the real job below that is the difference between resolving
3,749 and 3,763 of 3,773 points.

**The point-error checks also run in the drawing window.** *View > Panels > Check Fieldwork* opens a dock beside the point list and checks the points
that are actually in this project: duplicate numbers, look-alike numbers, close points, descriptions and **Line command order**. Point results are
cached independently from line-error results; changed inputs mark the displayed point report stale, and *Update Check* explicitly recomputes it.
Click a finding to select its points in the drawing; double-click to zoom to them. A Field Book (or codes built by *Convert Field to Finish*) enables
the vocabulary-dependent point checks. Without one, number and blank-description checks still run, while code- and command-dependent checks wait. The dock
says which work ran and offers both ways to load a vocabulary. Every point-check run is recorded on the job and `Save Report...` writes a `.fwc` the
field window and Excel can read.

**Whole-line errors use a separate geometry-correction workflow.** Open *Survey > Fix Linework* to run the existing Field Book-driven line-sequence
checks; their results and cache do not share the point-error run. **Line command order** remains in *Fix Point Errors* by design. Fix Linework currently
reports the existing incomplete-line/curve-sequence findings; no additional geometry checks are implied. The active Field Book supplies command meanings,
and its fix tools read the line again to validate a correction. Importing points, running point checks, editing descriptions, and opening a route do not
silently rebuild linework. *Survey > Process Linework* is the explicit action for generating or refreshing derived polylines; Open Route may offer it when
the current point/code inputs have not yet been processed.

![Check Fieldwork](docs/img/check_fieldwork.png)

**And the point list says where each point came from.** *Source File*, *Parent Folder*, *Imported* and *Import* are columns of the point list,
switched on by right-clicking its headings or under *View > Panels > Point Columns*. They are the provenance a working file has always kept
beside the coordinates, written by every import door: the file, the folder relative to the job ("Field Data/Week 1/Crew 6"), the import as a
whole ("Cleaned field data (crew 6)", "Stake-out points") and when it landed. They filter and sort like any other column, so "show me crew 6"
is one line in the filter box; the two the working file has always carried are on to begin with, the other two are one click away, and the
choice is remembered between sessions.

![Point list with source columns](docs/img/point_metadata_columns.png)

## Job folders

*File > New Project* builds the whole job folder, and **File > Project Folder...** is the one place the folder itself is dealt
with - open the project in it, open another one, create a new job folder, or start fresh (*"Start fresh - delete what is here"*, then
*"Are you sure? All prior data in this folder will be deleted."*, then *"Last chance to cancel, continue?"*, with the contents listed
before anything is removed). A location and a project are two entries nobody can keep straight, so there is one entry:

```
23-036.03 Murchison/
    23-036.03 Murchison.plb     opens by double-click
    Job Setup.txt               what this folder is and what the conventions are
    Field Data/
        Week 1/
            Crew 6/             one folder per crew, named from the point blocks
            Crew 9/
    Field Book/                 the F2F / code table, code commands, correction rules
    Control/                    control points and the coordinate-system record
    Drawings/  Surfaces/  Imagery/  Reports/
```

It runs behind a progress dialog with a **real Cancel**: pressing it stops at the next step and **removes everything that run
created**, so a cancelled job leaves no half-made folder for the next attempt to trip over. Nothing that already existed is ever
touched. `python -m plumbline new "23-036.03 Murchison" --weeks 2` does exactly the same thing from a terminal.

Crew folder names are **worked out, not typed in**. A crew owns a point-number block at every magnitude - crew 7 shoots
7,001-7,999 and 70,001-79,999 and nothing else - while points 1-999 are control and are shared by everybody. So a download names
its own crew, which is the only way the folder stays right when a new week arrives.

## The two samples

*File > Open Sample Project* is the synthetic one: a computer-generated commercial lot. It is deliberately **clean** - unique point
numbers, no two shots on top of each other - so it is the right sample for learning the drawing tools and the wrong one for seeing
the checks work. Everything in it arrives through the field-data import path, so it doubles as an end-to-end test.

***samples/Real World*** is the honest one, and it is new. Built from a real reduced survey - three crews, one shared control, a real
Carlson office standard of **1,717 feature codes**, and the check report the office actually produced:

| Crew | Initials | Collector | Points |
|---|---|---|---|
| Crew 6 | ER | total station | 1,053 |
| Crew 7 | AH | total station | 1,051 |
| Crew 9 | AE | GPS + total station | 1,650 |

**3,773 points in five field files, on EPSG:6584 (NAD83(2011) Texas North Central, US survey feet)**, drawn on the office's own
50 layers rather than a generic one, with 390 line strings built from the descriptions. It carries the mistakes real jobs carry:
8 duplicate point numbers, a genuine re-shot pair (`600` / `600B`), four pairs of shots on top of each other, and 12 points keyed
with six codes the office standard does not contain - those stay on the `POINTS` layer, visibly unresolved, because they are
findings rather than something to file away quietly.

`python -m plumbline sample-real` rebuilds it from the files in `samples/Real World/Source/`. The build is deterministic - every file
holding data comes out byte-identical - so it doubles as a regression check between versions.

## Coordinate systems

The coordinate-system manager (**Survey > Project Coordinate System**) is the part of a CAD tool that bites, so it is explicit:

![CRS manager](docs/img/crs_manager.png)

* **Nothing is assumed, and the default is *no coordinate system at all*.** A new project starts **UNASSIGNED** - a flag that reads
  `UNASSIGNED (no CRS)` in the status bar. Drawing, surfaces, volumes, DXF and point handling never ask. The moment a tool genuinely
  needs to know where on the earth the job is - imagery, KML, GIS, reprojection, grid convergence - it says so:
  *"Grid convergence needs a coordinate system - this project's CRS is UNASSIGNED. Select CRS first."* No silent wrong-hemisphere exports.
* **Texas State Plane, NAD83(2011), both units.** The picker opens on the ten Texas zone definitions - five zones, each in **metres
  and US survey feet** - plus WGS 84: North 6581/6582, North Central 6583/6584, Central 6577/6578, South Central 6587/6588,
  South 6585/6586. These are the current realisation; legacy codes (2275-2279, 32138-32141) are **translated forward** on sight
  rather than handed back as a deprecated zone. Anything else is still a search away in the full EPSG register.
* **Assign vs Reproject.** *Assign* = "these numbers already are EPSG:xxxx, just label them" (nothing moves). *Reproject* = convert
  every coordinate (points, polylines, text, surfaces) and the elevations when the unit changes (US ft <-> m).
* The full **EPSG + ESRI catalogue** is searchable (`texas north central`, `2276`, `UTM 14N`), with favourites, projection parameters,
  area of use and datum shown. The dialog warns when your data falls **outside the system's area of use** - the usual symptom of
  the wrong zone, wrong units or swapped northing/easting (importing offers a one-click swap).
* **Datum transformation is your call**: *automatic*, *none* (NAD83 ~ WGS84), or a specific operation. Each operation is listed
  with PROJ's **stated accuracy** and whether its shift grids are installed (a switch lets PROJ download NADCON5 / geoid grids). Without
  grids the available NAD83 -> WGS84 operations are only 2-4 m accurate - that is shown, not hidden.
* **SAF - Surface Adjustment Factor.** Ground scaling is expressed the way Texas survey practice does it:
  **SAF = ground / grid**, always slightly *above* 1 (a Bexar County job might use 1.00017), scaling about the **base point** - the
  projection origin (0,0) per TXDOT SOP, or a monument for a job that scales about one. **Base Northing, Base Easting and the SAF are
  locked until "use ground coordinates" is ticked**, because ground coordinates only exist when that is set; the base point and the
  factor are written into the project file so a job cannot be reopened against the wrong one. The SAF box keeps the **whole** factor -
  type `1.000136506` and it stays `1.000136506`, not rounded to something that is 0.5 ppm off.
  A combined-factor calculator (grid scale factor x elevation factor from latitude/longitude/height) inverts to SAF for you and shows
  both numbers side by side, because 1 / 0.99988 = 1.00012 and typing one into the other's box is a 200 ppm error.
  *Assign* relabels; *Reproject* converts grid <-> ground.
* Projects written before the rename stored the reciprocal (a combined factor, grid/ground). **They are inverted automatically on load** -
  open an old job and the numbers it moves are the same as the day it was saved.
* **Calculator tab**: project coordinates <-> lat/lon (decimal + DMS), grid convergence, scale factor, a Google Maps link and
  *Copy 'lat, lon'* for pasting into Google Earth.
* Z is never pushed through horizontal datum shifts; vertical unit and datum label are kept separately.

![Ground scale](docs/img/crs_ground_scale.png)

## Imagery (and an honest word on accuracy)

![Imagery panel](docs/img/imagery_panel.png)

**Imagery > Add Imagery** adds Esri World Imagery, USGS orthoimagery, OpenStreetMap (context), **any XYZ tile URL** (e.g. a county
orthophoto service), or **your own georeferenced image** (GeoTIFF, or PNG/JPG + world file, or a KMZ ground overlay). Tiles are cached on disk
(works offline afterwards) and drawn with the correct grid-convergence rotation for your projection.

**Read this before you trust a picture.** Online imagery is not survey-grade. Esri publishes the source and stated accuracy of its mosaic, and
Plumbline fetches it (*Source accuracy > Look up*): at the sample location (Mesquite, TX) the service reported
*Vantor Vivid Advanced WV03, captured 10/8/2025, 0.31 m pixels, **stated accuracy 8.47 m (27.8 ft)***. Laid under your drawing that is enough to
catch a blunder - the wrong zone, swapped coordinates, a mis-keyed point, a shift of tens of feet - and **not** enough to verify a survey to the
foot: a gap between a point and the imagery smaller than the imagery's own accuracy says nothing about your points. The panel prints that figure
next to the map; for a comparison you can stand behind, load a high-accuracy orthophoto (state / county / your own) through *Georeferenced
image* or *Custom tile URL*.

**Comparing imagery with your points is a measurement, not a report.** Pan the imagery under the drawing and measure with
**Draw > Distance / Bearing** (two clicks: distance and bearing) or with the *Measure* tool on any pair. *Nudge east / north* shifts the
**displayed** imagery to sit where you want it - the survey is never altered, and the nudge is saved with the layer. For point-based alignment,
select a visible layer and use *Nudge by points...*: click an image feature, then where it should land (the target follows the current snap setting).
Repeat for extra pairs and press Enter or right-click to apply their average shift.

**Google Earth round trip**: *Export KMZ* (datum handling shown and remembered) -> open in Google Earth -> drop a pin on the same feature for each
point, named with the point number -> save the pins as KML/KMZ -> *Import pins*. The pins come in as **reference points** on the `OTHER` layer
(with their own numbering, out of the fieldwork list and out of the data-quality checks), so you can measure between a pin and its point.

Terms: the Esri / USGS / OSM tile services have their own terms of use and attribution (shown on the map). Check them before putting
screenshots in deliverables; Google Maps tiles are deliberately **not** used (their terms forbid it).

## Import and export

| | Import | Export |
|---|---|---|
| **Point text** (CSV, TXT, PNEZD...) | any delimiter, header detection, column-role preview, presets (P,N,E,Z,D / P,E,N,Z,D / lat-lon...), null elevation, bad-row report | same presets, delimiter, decimals, elevation unit, output CRS or lat/lon |
| **LandXML** 1.x | CgPoints, TIN surfaces (invisible faces skipped), parcels, plan features, alignments (lines, arcs, irregular lines; spirals as chords), file CRS + units | points, TIN surfaces, linework with true arcs |
| **DXF** | lines, polylines with bulges, 3D polylines, circles, arcs, splines/ellipses, text/mtext, points, blocks (as attributed points or exploded), 3DFACE/polyface -> surface, layer colours | layers + colours + linetypes, LWPOLYLINE with bulges, 3D polylines where Z varies, contours at elevation, point labels or attributed blocks, optional 3DFACE TIN, `$INSUNITS`, R2000-R2018 |
| **GIS** | Shapefile, GeoPackage, GeoJSON (all layers at once or one), field mapping, polygons with holes | GeoPackage (points/lines/polygons layers), Shapefile, GeoJSON, any output CRS |
| **Google Earth** | KML/KMZ placemarks, lines, polygons, ground overlays | KML / KMZ, WGS84, styled by layer |

Every import states **where the numbers live** (project system / another system / local), checks plausibility against the area of use, and
handles unit conversion (horizontal *and* elevations), duplicate point numbers (renumber / skip / overwrite / keep) and layer assignment.
**A file that says nothing about its coordinate system is taken to be in the project's own system** - no reprojection is invented for it -
and that assumption is written on the file. **Field data from a file or a folder is this job's field data unless you say otherwise**
(reference layers, control and stake-out lists are a deliberate choice).

If a guess turns out wrong, fix it after the fact: **Files > Coordinate Systems of Imported Files...** lists every imported file with
the system its numbers were taken as, and offers the three honest answers - **relabel** it (the numbers were right, the system was not,
nothing moves), **reproject** it (the numbers really are in another system, every point moves and the move is audited), or **ground/grid
scale** it (same system, scaled). That is the "assumed TXNC grid, was TXC ground" case, and it is one dialog.

**A new project and every import choose the whole position** - horizontal coordinate system, vertical datum and geoid model, and the
ground scale - on one panel, so a job cannot start with half a coordinate system.

## Feature codes and linework

A description contains a Field Book feature code, optional string identifier, line/curve commands, and optional notes. The active Field Book maps each
command token to a fixed meaning: Start Line begins a string, End Line finishes it, Close returns a closed feature to its first point, and configured curve
commands mark the curve's endpoints. Intermediate coded points continue a string; string identifiers keep interleaved strings apart. Multi-code and
description separators, including their spacing, follow the active Field Book and Settings. **Survey > Feature Code Table** edits the starter library
(layer, colour, linetype, point/line/polygon, breakline, counts-as-ground). The explicit *Process Linework* action builds polylines
(regenerated, never duplicated); imports and checks do not run it automatically. Breakline codes feed the surface.

**Survey > Convert Field to Finish** reads the office's own code standard straight into those codes - Carlson's Field-to-Finish file, the one every office
here keeps: **1,717 codes over 216 layers, without retyping anything**. Carlson's layout is the default and asks no questions, and the columns are found
**by their names**, not by position (Carlson writes Symbol Size in the fourth column, where a position-reader wants the layer - the difference between 216
layers and a column of symbol sizes). *Custom* is for a table that is not Carlson's: point at the column for each fact by hand (the headers may say
anything, or nothing) and tick the rows to bring in - type `PROP` in the filter, press *Only the matches*, and the property codes come in while the rest of
the standard stays out. *Merge* keeps the codes the job already has, *Replace* takes the file as the whole truth, and the line under the buttons says what
either choice would produce before anything changes. The file is remembered on the job, so next winter's standard opens in the right folder.

![Convert Field to Finish](docs/img/f2f_dialog.png)

## Surfaces, contours, volumes, profiles

* **TIN** (Delaunay) from ground points; **breaklines are honoured exactly** (conforming insertion, crossings noded with elevation averaging and a warning),
  optional boundary / hole polylines and a maximum edge length (with a suggestion) so the TIN doesn't bridge gaps. A surface whose inputs changed is flagged
  *out of date*; Edit / Rebuild fixes it.
* **Contours**: minor/index, base, smoothing, labels placed upright along the line. Generated entities - re-running replaces them.
* **Volumes**: exact per-triangle cut/fill to a datum (with the triangle split exactly where it crosses the datum), clipped to a polygon; surface-to-surface by
  the composite method (or a grid method for comparison with other software). Results in cubic units, cubic yards, cubic metres, with areas.
* **Profile** along any polyline (exact TIN-edge intersections, gaps where the line leaves the surface), vertical exaggeration, hover read-out, CSV / PNG;
  **cross-sections** at an interval and half-width to CSV.
* Display: elevation tint + hill-shade (smooth, Gouraud), hill-shade only, slope classes, TIN edges; big TINs use a cached raster so panning stays fast.

## 3D view, depth view and Google Maps

![3D view](docs/img/view3d.png)

* **3D view** (*View > 3D View*, `Ctrl+3`) - orbit around the surface, points and linework. Drag to orbit, Shift / right / middle drag to pan, wheel to zoom,
  double-click to fit. Presets: *Top* (the plan, north up), *Front*, *Right*, *Back*, *Left*, *Iso*; perspective or orthographic; a **vertical exaggeration**
  that is picked for you (a flat site needs several times to show its shape). It is a *viewer*: no editing or drawing tools - select points in the plan and
  they light up in 3D. Terrain hides what is behind it while anything lying on the surface (survey points, breaklines, contours) stays visible. Big data is
  simplified while you move and sharpens a moment after you let go.
* **Depth view** (*View > Depth View*, `Ctrl+4`) - a side-on window you can **rotate to any bearing**, so it is not stuck looking from the front:

  ![Depth view](docs/img/depthview.png)

  ![The depth line and slab drawn on the plan, with the depth view looking across them](docs/img/main_depth.png)

  You look along a compass direction at a *slab* of the site that starts at the **depth line** and runs a chosen **Depth** beyond it. The ground is drawn as
  sections cut through the surface at the line and at planes further in (fainter = deeper); points and linework inside the slab get smaller and dimmer with
  depth. **Rotate** with *N / E / S / W*, the dial, the bearing box, *-15 / +15*, *Flip*, the arrow keys, or right-drag in the view. The slab is drawn on the plan
  (line, shaded depth, arrow for the viewing direction). *Follow plan* keeps the line through the middle of the plan as you pan it; or aim it yourself with
  **View > Depth Line** / *Pick line on plan...*: click a start and an end (snaps to points) and the view looks across that line to its LEFT - draw it the other
  way to look the other way. *Start* / *Depth* move and thicken the slab, wheel zooms about the cursor, `Ctrl`+wheel changes the vertical exaggeration.
* **Google Maps** (*Imagery > Open View Center in Google Maps*, `Ctrl+Shift+M`) - opens the **center of the plan view** (the middle of what you see) in your browser,
  satellite layer with a pin, at a zoom that matches the plan's scale; a street-map variant and *Copy Google Maps Link* are next to it. It needs a real coordinate
  system (a project in local coordinates has no place on earth); if no browser can be started the link goes to the clipboard.

## Reports

Point list, surface summary (with slope distribution), volume, line & curve table (bearing/distance/radius/area/acres), data-quality findings, a
**point audit** (the job's field points against the state they were imported in: what is missing, what was added, what moved and by how much, what was re-described) and a
coordinate-system report (including which datum shift PROJ applied and its stated accuracy). One viewer: **Save PDF / HTML / CSV / Excel** (the on-screen page is always white, like the print).

## Make it yours

**Plugins** - drop a `.py` in the plugin folder (*Plugins > Open Plugin Folder*; *Install example plugins* in the Plugin Manager):

```python
from plumbline.plugins import command, importer, exporter, Param

@command("Tools/Round elevations", params=[Param("decimals", int, 2, "Decimal places")])
def round_z(api, decimals=2):
    with api.edit("Round elevations"):                      # one undo step, repaint, dirty flag - all handled
        for p in api.selected_points() or api.points():
            if p.z == p.z:
                p.z = round(p.z, decimals)
```

Commands get an auto-built parameter dialog and appear under *Plugins*; importers join *File > Import*, exporters *File > Export*. A plugin that raises is rolled back and
reported, and a plugin file with an error never stops the app from starting.

**Scripts and console** - *Plugins > Run Script* runs any `.py` with `project`, `api`, `np`, `math`, `G` (geometry), `cogo`, `U` (units) in scope; the **Python console** dock
does the same interactively. **Headless**: `python -m plumbline run script.py job.plb --save out.plb`, `python -m plumbline export-dxf job.plb out.dxf`, `python -m plumbline info job.plb`. `python -m plumbline job.plb` opens a project
(or imports a CSV / DXF / LandXML / GIS file); on Windows `Plumbline.bat` does the same, and you can drag a file onto it.

[`docs/EXTENDING.md`](docs/EXTENDING.md) has the data model, the key functions and worked examples (new importer, batch script, custom report).

## Project layout

```
plumbline/
  fieldwork/ the field-data half: config (point blocks, crew rule), detectors, io_carlson, parse, coord_systems (Texas 2011 zones +
             the pure-python LCC fallback), steps_report, bridge (the seam into the project), sample_real, and the field window
             (clean, renumber_tool, ui_main).  All of it except the window is **Qt-free** - a test enforces that.
  core/      Qt-free engine: crs, project/model, jobtemplate (job folders), surface (TIN, contours, volumes, profiles), featurecodes, cogo, spatial (pick/snap),
             imagery (Web-Mercator maths, NSSDA stats, georeferenced images), tiles (disk-cached tile store, Esri metadata), qa, units, settings,
             scene3d (3D / depth-view camera maths + software renderer), maps (Google Maps links), ramps (colour ramps)
  io/        csv_points, landxml, dxf_io, gis_io, kml_io, f2f (Field to Finish), reports (HTML / CSV / XLSX)
  plugins/   the plugin & script API (+ examples/)
  ui/        main_window, canvas + render (scene cache, fast Qt paths, Gouraud surface), tools, docks, dialogs per area,
             view3d (orbit view), depthview (rotatable elevation / depth view), fieldwork_window + job_setup (the merge)
  sample.py  the synthetic sample site          cli.py  command line          doctor.py  `plumbline doctor` health check
Plumbline.bat, install_windows.bat   Windows launcher and one-time setup (see docs/WINDOWS_SETUP.md)
Update and Run Plumbline.bat         one-click update + launch: git pull, reuse .venv, pip only when requirements.txt changed
tests/       228 automated tests (core maths, I/O round trips, plugins, the 3D renderer, an end-to-end Qt suite driving real mouse events,
             portability checks, and the merge: the field-data core importing with Qt sabotaged, crew decoding, the job folder's cancel
             promise, and the two samples)
samples/     Real World - a real reduced job, rebuilt byte-identically by `python -m plumbline sample-real`
sample_data/ sample files in every format       verification/  the independent maths and property harnesses      tools/  screenshots, live preview
docs/        WINDOWS_SETUP, EXTENDING, MERGE (how the field-data half was merged), VERIFICATION + CODE_REVIEW + FIXES_APPLIED,
             WORKING_PROFILE, FUTURE_FIX_TOOLS, examples/, img/
```

Projects are saved as **`.plb`** - plain JSON (numpy arrays zlib+base64 inside), versioned, written atomically; easy to inspect or convert.
Undo is whole-project snapshots (30 deep).

## What was verified, and what wasn't

**Verified (automated):** 395 tests, including analytic checks - exact plane and gable-roof volumes, datum-crossing triangles, breaklines surviving noisy data,
closed contours, profile gaps; CRS round trips, ground-scale algebra, combined factors against known values, unit conversion in every importer (a metric LandXML and DXF landing on
top of the original US-foot data); import/export round trips for every format; imagery tiles, the display nudge and offline mode against a local tile server;
the Google Earth pins arriving as reference points; an office Field-to-Finish table converted both ways - by column name and by hand - and compared
code for code with the door Fieldwork Manager uses; the import provenance surviving a save and a load, the source
columns switching on and off and filtering by crew, and the drawing window's Check Fieldwork dock finding the same duplicates the field
window finds; the 3D renderer (camera conventions, terrain hiding what is behind it while points on the surface stay visible, slab clipping for the depth view) and the Google Maps link; undo/redo; plugin isolation and rollback; and a Qt suite that clicks, drags and types into the real window.
Several real bugs were found and fixed that way. The second change order (the 13 items in [`docs/CHANGE_LIST_2026-10.md`](docs/CHANGE_LIST_2026-10.md))
is pinned one test per item by `tests/test_change_order_2026_10.py`, and two tree-level audits run beside the suite:
`verification/check_change_list.py` (18/18 of the first order) and `verification/check_change_order_2026_10.py` (13/13) - they look at the
artifacts rather than at the tests' opinion of them.
Live checks: real Esri tiles load, orient correctly (north-up with the expected ~1 degree grid rotation) and the accuracy lookup works against Esri's service.

**Performance** (100,000 points, ~200,000-triangle TIN, 2 CPU sandbox): import 0.14 s, TIN build 1.3 s, contours 0.1 s, volume 0.03 s, data-quality scan 1 s, save 0.9 s / load 0.5 s, redraw 0.04-0.35 s,
one undo snapshot 0.6 s, DXF export 6 s as points only - or about 47 s with the default number / elevation / description labels, because ezdxf creates each of the 300,000 text entities one by one (a few thousand points take a second or two).
3D view, same data plus 87,000 line segments, 1100 x 750 window: the sharp picture takes about 1.4 s, while you drag it is simplified to about 0.1 s a frame
(drawn in software, no graphics card needed; a few thousand points redraw in ~10 ms).

**Not verified / limits you should know about**
* **Real field data is now in the tree** (`samples/Real World`) - a real reduced job with real duplicate point numbers, a real 1,717-code
  office standard and the office's own check report, and it round-trips through import, coding and linework in the test suite. What is
  *still* synthetic is any judgement of accuracy: matching the office's expected volumes, layer-by-layer drawing comparison and
  coordinate checks against the previous software are still done by eye. **Run your own jobs through it and compare before relying on it.**
* The field-data window is the one part not rebuilt: it was ported from PyQt6 to PySide6 (4 import lines, verified by constructing and
  showing the real window under PySide6) and opened beside the drawing, rather than reimplemented as docks. Its 7,500 lines of UI are
  as they were - which is the point, but it means the two windows are not yet visually one program.
* Not run on Windows or macOS (see Install above); GUI looked at only through offscreen screenshots.
* Datum shifts without their grids are metre-level (shown in the UI). Vertical datums **are** computed - geoid separation and VERTCON
  (NGVD29 <-> NAVD88) - when the grid is present; the grids are a first-run download rather than part of the package, so an offline machine
  gets a clear "this grid is not here" instead of a guessed height.
* Online imagery accuracy is what it is (see above). No automatic feature matching: comparing imagery with your points is done by eye and by
  measuring, and a project written by an older version keeps its old imagery-check records (shown, not re-run).
* LandXML: no profiles, cross-sections or pipe networks; spirals become chords. DXF: hatches, dimensions and leaders are skipped (reported on import); no paper-space layouts.
* CAD editing is basic (draw, select, grips, move, transform; no trim / extend / offset / fillet). Surfaces are TIN only (no grid surfaces); no end-area / corridor volumes.
* Undo snapshots the whole project: fine to ~100k points, sluggish beyond. Plugins run arbitrary Python - only install ones you trust.
* 3D / depth views: drawn in software and sorted far-to-near by triangle centre, so very long sliver triangles can mis-order at grazing angles; point and line
  visibility is tested to about 1.5 % of the scene depth (something that far *behind* the ground can still show); point numbers in 3D are not hidden by terrain;
  text, imagery and DXF-only entities are not drawn in 3D. Linework without elevations is draped on the surface (or shown at the mean elevation of the points).

## Where I'd take it next
1. Your real data, now that it is in the tree: run a job end to end and compare the drawing, the volumes and the check report against your usual software. The Real World sample is the first one; more weeks and more crews make the crew-block rule earn its keep.
2. Plot sheets: title blocks, scale-aware plan sheets and PDF plotting.
3. Corridor tools (alignments with stationing, cross-section end-area volumes) and profile sheets.
4. Higher-accuracy imagery sources for your area (state / county orthos as tile services) wired in as presets.
5. Raw-data support (RW5 / JobXML) and least-squares adjustment.

## Third-party notes
PySide6 (Qt) is LGPL; pyproj, shapely, ezdxf, pyogrio, rasterio, matplotlib, openpyxl, scipy and numpy have permissive licences. Imagery services belong to their providers (Esri, USGS, OpenStreetMap).
