# Plumbline quick start

Plumbline is a plan-and-topo CAD tool for survey data: bring in points, linework and GIS data, check them against
imagery, build surfaces and contours, and send DXF / LandXML / GIS / Google Earth files and reports back out.

## 1. Start a project

* **File > New** asks for a coordinate system - or choose *local coordinates* and assign one later.
  Nothing is assumed for you. Texas North Central State Plane (US ft) is EPSG:2276; search by name or code.
* **File > Open Sample Project** loads a synthetic site so you can try everything.
* Drag and drop works: drop a CSV, DXF, LandXML, GeoPackage/Shapefile/GeoJSON, KML/KMZ or `.plb` onto the drawing.

## 2. Import

**File > Import** handles each format; every import shows *where the numbers live* (project system, another system,
local) and a plausibility check against that system's area of use. If northing and easting are the wrong way round,
the dialog says so and offers a one-click swap.

* Point files: pick the column roles in the preview (P,N,E,Z,D / P,E,N,Z,D ... presets, header detection, any delimiter).
* LandXML: points, TIN surfaces, parcels, alignments and plan features (lines and arcs).
* DXF: lines, polylines with arcs, circles, arcs, splines, text, points, blocks (as points or exploded), 3D faces (as a surface).
* GIS: pick the layer and which fields hold the point number / description / elevation.

## 3. Feature codes and linework

Descriptions use a Field Book feature code, optional string identifier, configured line/curve commands, and optional notes.
The active Field Book assigns each command its meaning: Start Line begins a string, End Line finishes it, and Close returns a
closed feature to its first point. Intermediate coded points continue the string. **Survey > Process Linework** explicitly turns those
coded strings into polylines (breaklines for codes flagged as such); import and QA never run this automatically. Edit feature codes and command meanings in the Field Book.

## 4. Surfaces, contours, volumes, profiles

* **Surface > Create Surface** builds a TIN from ground points; linework coded as breaklines is honoured exactly.
  Use a maximum edge length or a boundary so the TIN does not bridge gaps. A surface that no longer matches its points is flagged *out of date*.
* **Surface > Contours** creates minor / index contours with labels. They are generated entities; running it again replaces them.
* **Surface > Volumes**: surface to datum, or existing to proposed surface, optionally inside a selected closed polyline.
* **Surface > Profile**: select a polyline first. Cross-sections can be exported to CSV.

## 5. 3D view, depth view, Google Maps

* **View > 3D View** (`Ctrl+3`): drag to orbit, Shift/right-drag to pan, wheel to zoom, double-click to fit. *Top* is the plan; the
  vertical exaggeration is picked for you. It only looks - select in the plan and the points light up in 3D.
* **View > Depth View** (`Ctrl+4`): a side-on window you can rotate to any bearing - the *N E S W* buttons, the dial, *-15 / +15*, *Flip*
  or right-drag. It shows a slab that starts at the *depth line* drawn on the plan and runs *Depth* beyond it. *Follow plan* keeps the
  line through the middle of the plan; **View > Depth Line** lets you click the line yourself (the view looks to its left).
* **Imagery > Open View Center in Google Maps** (`Ctrl+Shift+M`): opens the middle of the plan view in your browser (needs a real
  coordinate system, not local coordinates).

## 6. Aligning imagery

**Imagery > Add Imagery** adds Esri World Imagery, USGS orthos, OpenStreetMap, any XYZ tile URL, or your own georeferenced
GeoTIFF (or PNG/JPG + world file).

* **Look up source accuracy** first. Online imagery can be several metres off; at the sample location Esri states about 8.5 m.
  A visual alignment is only as trustworthy as the image source. For precise work, use a high-accuracy orthophoto.
* **Nudge by points**: select a visible imagery layer and click *Nudge by points...*. In the top view, click a feature in the
  image, then where it should land (often a survey point). The target click follows the current snap setting. Repeat with more
  point pairs if useful, then press Enter or right-click to apply their average shift; Esc cancels. The survey points never move.
* **Google Earth round trip**: export a KMZ, drop pins named with the point numbers, save, and import the pins.

## 7. Output

**File > Export** writes DXF (layers, colours, arcs, contours, labelled points), LandXML, GIS, Google Earth and point text files.
**Reports** builds point lists, surface and volume summaries, line/curve tables, data-quality findings and CRS reports -
viewable, printable to PDF, or saved as HTML / CSV / Excel.

## 8. Make it yours

Put Python files in the plugin folder (**Plugins > Open Plugin Folder**) to add commands, importers and exporters; run scripts
with **Plugins > Run Script**; or use the Python console dock. See `plumbline/plugins/examples`.

## Keys and mouse

| | |
|---|---|
| Wheel / middle-drag | zoom / pan |
| Middle double-click | zoom extents |
| Click, Shift+click, Ctrl+click | select, add, toggle |
| Drag right / drag left | window / crossing selection |
| Delete | delete selection |
| Ctrl+Z / Ctrl+Y | undo / redo |
| F3 | toggle snapping |
| Enter / right-click | finish polyline |
| Esc | cancel |

Typed coordinates in the command box: `N,E` (or `E,N` - see Settings), `@dN,dE`, `@distance<bearing` (e.g. `@100<N45-30E`),
or a point number.
