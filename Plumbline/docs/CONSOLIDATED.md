
=== docs/CHANGE_LIST_2026-10.md ===
# The 2026-10 change order, item by item

The user's second list, kept in their words, with what was done, where it lives and what pins it.
Where a choice was open the choice and its reason are here, so it is not re-argued later.  This is
the record for the items that arrived *after* `CHANGE_LIST.md` (whose 18 items all stand).

**State of the suite when this phase finished: 395 passed** (offscreen Qt, ~8.5 min; 358 when the
phase began).  The tree-level audit `verification/check_change_order_2026_10.py` reads 13/13, and the
first order's `verification/check_change_list.py` still reads 18/18.

**How this list was checked:** every item was looked for in the tree, not in a status column -
`tests/test_change_order_2026_10.py` is one test (or more) per item, written against the acceptance
criterion the user gave, and it runs with the rest of the suite:

```
cd Plumbline && QT_QPA_PLATFORM=offscreen python3 -m pytest -q tests/test_change_order_2026_10.py
```

| # | Item (the user's words, shortened) | State |
|---|---|---|
| 1 | Licence + EULA, accepted on first run, readable from Help | **done** |
| 2 | Object groups: any selectable object, hidden everywhere | **done** |
| 3 | Settings save/load; every pull-site listed and editable | **done** |
| 4 | Open an existing project folder, or overwrite it (two warnings) | **done** |
| 5 | New project makes a folder in the current directory and names it | **done** |
| 6 | Field data by default; a CSV assumes the project CRS; change a file's CRS afterwards | **done** |
| 7 | CRS selection covers coordinate, vertical and SAF, in import and new-project startup | **done** |
| 8 | Pan on Esc; pan is the default tool | **done** |
| 9 | CRS/geoids/vertical datums by download flag, not shipped | **done** |
| 10 | Base N/E and SAF unenterable unless "use ground"; N above E; no "Convert a Height" | **done** |
| 11 | The whole SAF can be typed: 1.000136506 | **done** |
| 12 | One folder choice with subfolders, not a location plus a project | **done** |
| 13 | Run Checks: works, and leaves an audit trail across close/reopen | **done** |

---

## 1. Licence and EULA

**"licence first eula second, testing/private use only, files + accept on first run, and readable
from Help; any change to the words bumps the version so it is shown again."**

* `plumbline/core/licence.py` holds the only copy of the words - `COMMERCIAL_TERMS` (Part 1, the
  no-commercial-use licence) then `EULA` (Part 2, the general agreement) - and `write_files()` emits
  `LICENSE.md` and `EULA.md` from it.  A test compares the tree's two files against the module, so
  the words on disk cannot drift from the words on screen.
* `plumbline/ui/licence_dialog.py`: two tabs in that order, "I Agree" disabled until the box is
  ticked, declining closes the program.  `Help > Licence Agreement...` reopens the same dialog
  read-only.  `ensure_accepted()` runs before the main window is built, so a user who declines never
  reaches a window that has already opened their files.  Acceptance is stored as a **version**
  (`licence_accepted_version`); bumping `licence.VERSION` shows it again.
* Pinned by `test_item_1_*` (order, gate, Help door, files match the module, re-shown on a version
  bump).

## 2. Object groups

**"any selectable object, hidden everywhere - drawing, exports, surfaces."**

* `plumbline/core/groups.py` (`GroupSet`: create / rename / remove / add / discard / set_visible),
  on `Project.groups`, saved with the job.  Membership is by **object id**, so a group can hold
  points, polylines, text, surfaces - anything with an id.
* `Project.hidden_ids()` is the single question the rest of the program asks: the drawing
  (`ui/render.py`), DXF, LandXML, GIS, KML, the reports, the surface builder (`core/surface.py`,
  which also drops a hidden breakline) and the 3D scene all consult it.  `visible_points()`,
  `visible_entities()`, `hidden_count()` for the rest.
* `plumbline/ui/groups_dock.py` ticks, renames and fills groups through `state.edit(...)`, so a
  group change is one undo step.
* Deliberate limits, recorded here: a surface **already built** is not rebuilt because an object
  was hidden afterwards, and a hidden object is still pickable (it is not deleted).  Both are the
  conservative reading of "hidden everywhere": nothing is silently recomputed, nothing is lost.
* Pinned by `test_item_2_*` - including a surface with a hidden breakline and a save/load/undo
  round trip.

## 3. Settings save/load, and the registry of every pull-site

**"every reference the program pulls from listed and editable, with the formats shown; settings
saved and loaded as a file."**

* `plumbline/core/registry.py` is one table of **15 sites in 4 kinds** (imagery, coordinate systems,
  geoids/vertical datums, software update).  Each row says what it is, what it accepts (the
  `{z}/{x}/{y}` placeholders, the grid file names) and its current value.
* `RegistryDialog` (Settings > **External data sources...** or the CLI `python -m plumbline sites`)
  edits, switches off and **restores the built-in** value; a replaced imagery source also lands in
  `settings["replaced_tile_sources"]` so the imagery menu reflects it.
* `settings().export_to(path)` writes what differs from the defaults; `load_from(path)` returns the
  keys it changed and saves.  UI: Settings > **Save settings... / Load settings...**.
* Pinned by `test_item_3_*`.

## 4 / 5 / 12. One folder: open it or overwrite it, named after the job

**"open an existing project folder or overwrite; overwrite asks twice - 'are you sure all prior
data will be deleted', then 'last chance to cancel'." / "a new project creates a folder in the
current directory and names it." / "too many folder options - location and project must be one
folder with subfolders, not two entries."**

* `plumbline/ui/project_folder.py` is the single door (**File > Project Folder...**): pick a folder,
  open the project in it, make a new project in it, or start fresh - and starting fresh is the only
  path that deletes anything.  The old separate "Select Job Folder" / "New Job Folder" entries are
  gone.
* `plumbline/ui/widgets.destructive_confirm(parent, title, steps, final="Continue")` is the
  two-question shape the user asked for, with the words pinned:
  * "Start fresh - delete what is here"
  * "Are you sure?  All prior data in this folder will be deleted."
  * "Last chance to cancel, continue?"
  and step 2 lists exactly what is there (projects, field books, drawings - from
  `jobtemplate.job_folder_contents`) before it is deleted.  Deletions go to the recycle bin when
  Qt offers one.
* A new project's folder is created **in the current directory** by default and named after the
  job; the folder contains the `.plb` and the subfolders (`Field Data/ Field Book/ Control/
  Reports/ Drawings/ Surfaces/ Imagery/`).
* Pinned by `test_item_4_*`, `test_item_5_*`, `test_item_12_*`.

## 6 / 7. Import: this job's field data, the project CRS, and the vertical + SAF

**"field data from a file or folder defaults to job field data; any CSV import assumes the project
CRS; reprojection only happens when it is a different CRS; a way to change a file's CRS after the
fact (assumed TXNC grid, was TXC ground)." / "the CRS choice covers coordinate, vertical and SAF,
and is part of import and of new-project startup."**

* Every import dialog now opens on **"This job's field data"** (role `None`); reference layers and
  other roles are a deliberate choice below it.  The folder import behaves the same way.
* `plumbline/core/filecrs.py` records, per imported file, **what system its numbers were taken as**;
  when a file says nothing it gets the project's own system, method `project`, and **no
  reprojection** - which is the user's rule.  The record travels with the project and is listed by
  `Files > Coordinate Systems of Imported Files...` (`ui/filecrs_dialog.py`), where a file can be:
  * **relabelled** (the numbers are right, the system was not), nothing moves;
  * **reprojected** (the numbers really are in another system), every point audited;
  * **ground/grid scaled** (the same system, scaled) - the TXNC-grid / TXC-ground case: a
    `GroundScale` with the base point and the full SAF.
* `plumbline/ui/crs_extra.py:VerticalAndGroundPanel` carries coordinate system + vertical datum +
  geoid + ground tick + Base Northing/Base Easting/SAF, and both the import panel and the
  new-project screen embed it, so a new job starts on NAVD88/GEOID18 and the same choices appear at
  import time.
* Pinned by `test_item_6_*` and `test_item_7_*`.

## 8. Pan

**"pan releases on Esc; the pan tool is the one the program starts in."**

* The pan tool is the startup tool and the tool a replaced project comes back in; `Esc` releases
  whatever pick tool is active and returns to pan (canvas `escape_pressed`).
* Pinned by `test_item_8_pan_is_the_default_tool_and_escape_returns_to_it`.

## 9. External data: a flag to download, not a file that ships

**"flags to download shipped when installed / first run, so not packaged, but default download on
first run - for all external references, to keep the ship small."**

* Nothing large ships inside the package: the package carries **definitions** (PROJ's EPSG
  parameters, the datum and geoid tables in `core/vdatum.py`) and **flags** saying where the data
  comes from.  A test asserts no file over 2 MB exists anywhere under `plumbline/`.
* `plumbline/core/firstrun.py` is the plan: every item (geoid models, VERTCON, the PROJ grid CDN,
  imagery) with what it is for, what stops working without it and its size; `grid_present()`,
  `plan()`, `total_mb()`, `download()` (through PROJ, so the file lands where the conversion will
  look for it).
* `plumbline/ui/first_run.py` is the first run: every item **ticked**, with sizes and consequences,
  offered **once** and remembered whatever the answer - "Later" is an answer, and the list lives in
  Settings > External data sources from then on.  `Nothing is fetched behind the user's back`; a
  missing grid is reported when a conversion needs it rather than guessed.
* Imagery is deliberately **not** pre-downloaded: it is a cache fetched tile by tile as you look at
  the map, and its sources are registry rows.
* CLI: `python -m plumbline external [--list|--download]`.
* Pinned by `test_item_9_*`.

## 10 / 11. The ground scale

**"Base Northing / Base Easting / SAF unenterable unless 'use ground' is ticked; Northing above
Easting; 'Convert a Height' removed." / "1.000136506 must not be typable as 1.00013650."**

* `ui/crs_extra.VerticalAndGroundPanel` and `ui/crs_dialog.py`: the three boxes are locked until
  the ground tick is set, Base Northing sits above Base Easting, and the SAF box keeps **11
  decimals** - `1.000136506` round-trips exactly, in the CRS dialog, in the import panel and in the
  field window.
* "Convert a Height" is gone from the CRS dialog; the NGVD29 note now points at the tool that does
  the job properly (**Survey > Project Coordinate System > Reproject**), and a geoid note says what a missing model means.
* SAF is ground/grid (`ground = base + (grid - base) * saf`), which is the TXDOT convention the
  user's numbers came from.
* Pinned by `test_item_10_*` and `test_item_11_*`.

## 13. Run Checks, and the audit trail

**"Run Checks with the field book converted made no noticeable difference; I do not necessarily
care about the files as long as we can audit and track even after the project is closed and
reopened."**

What was wrong, in one line: the dock looked for the job's field book only in `Field Book/`, and a
fresh job's template book there has an **empty** code table - so the description check ran against
nothing and reported **132 "Unknown code"** alarms against a job whose real book has 1,717 codes.
That is how a check gets switched off and stays off.

* One resolver, `fieldwork/bridge.vocabulary_for(project, job_root, fieldbook)`, answers what the
  check will run against, and says so before it runs:
  * `field book` - the job's book (remembered, else any `*.fwb` under the job folder, the job's own
    name first: an office book in `Source/` counts);
  * `field book + job codes` - a book with no code table merged with the job's own converted codes;
  * `job codes` - only when they came from the office's Carlson export (`f2f_path`), because the
    program's built-in default codes are a starting set, not this job's vocabulary;
  * `none` - number/position and blank-description checks still run; vocabulary-dependent checks
    pause and the banner says which inputs are missing. An empty vocabulary is never run as if it were one.
* The Check Fieldwork dock and Fix Point Errors use the point-error run/cache. Whole-line findings
  use a separate Fix Linework run/cache; `Line command order` remains in Fix Point Errors by direction.
* **Auditable across close/reopen**: each point-check run is stored on the project
  (`settings["check_report"]`: when, how many points, crews, every finding with the point ids it
  named *and* their numbers, the vocabulary used and its size), written beside the job as
  `Reports/Check Fieldwork <stamp>.fwc` (the same unified 9-column report the field window writes,
  so it opens in Excel and in the report viewer), and shown again when the project is reopened -
  even if the `.fwc` is deleted, because the record lives with the project.  "Save Report..." writes
  any of them out again.
* Pinned by `test_item_13_*`, including a real close-and-reopen of the job.


=== docs/WORKING_PROFILE.md ===
> **2026-10-03 — this file moved.** Fieldwork Manager is now part of Plumbline
> (`plumbline/fieldwork/`, opened from *Survey > Fieldwork Manager* — it was under Tools until the menus were tidied). Any path in this
> document that points at `program/` or `run.py` is history; the rules, the domain notes and
> the way this workspace is run are still current. See `docs/MERGE.md` for what moved where.

# Fieldwork Manager — Working Profile & Study Recap

**DURABLE FILE — changes rarely, but retired single-file era 2026-09-23.** See `README.md` for current modular layout. The original `fieldwork benchmark.pdf` was **retired and deleted on 2026-09-18**; `error_catalog.md` and `test_fieldwork_manager.py` retired 2026-09-23.

| File | Role |
| :--- | :--- |
| `fieldwork-manager-profile.md` | This file: who you are, how to work with you, durable domain rules, study recaps (still current for domain/sorts) |
| `program/run.py` + `program/fieldwork_manager/*` | The app itself — modular workspace copy of record (see README.md) |
| `FieldworkManager_v1.0001.zip` | Ship — `program/` zipped |

**Versioning policy (retired single-file, kept for history):** Old policy was major milestones iterate `fieldwork_manager_v1.py → _v2.py` and archived. Current: modular `program/fieldwork_manager/` edited in place; ship re-zipped as `FieldworkManager_v1.0001.zip`. No `APP_MODULE` dial anymore.

**Candidate workflow (retired 2026-09-23):** Previously unreviewed changes went into `fieldwork_manager_v1-1.py` candidate file. Now deprecated — agent edits `program/fieldwork_manager/*.py` directly.

**Workflow — agent edits workspace files directly:** the agent changes the shared-workspace copies in `program/`, verifies with `python -m py_compile` + offscreen smoke, then the user copies the whole `program/` or zip. No chat copy-paste of code blocks. Every batch of changes ships with a **layman explanation** — WHAT changed and WHY, in plain language with jargon defined — plus verification steps.

---

## 1. Who You Are

- Prefer to stay anonymous; if a name must be used: **Ann Anya Mist**.
- Learning **Python + PyQt6 at the same time**. Beginner-to-early-intermediate in Python, new to Qt, but very hands-on and self-directed.
- **Domain expert: Land Surveying.** You know exactly what the tool must do in the real world.
- Building a real tool, not a tutorial toy: **Fieldwork Manager**.

## 2. Your Learning Style

1. **Notes-first, then build.** You started with handwritten notes: `variable = ClassName(args)`, `app = QApplication([])`, `window.show()`, `app.exec()`. Understand *why* before building.
2. **Fundamentals through analogies.** `super().__init__()` = "build the foundation before adding the bedroom"; `sys.argv` = Argument Vector; Parent-Child = memory ownership.
3. **Learn by breaking and fixing.** Paste the full traceback; learn to *read* it bottom-up, not just get a fix.
4. **Incremental, not waterfall.** "lets wait on filtering until we get to this part but we can implement sorting now" — finish one feature cleanly before adding the next.
5. **Try it yourself first.** You split Settings into `ProjectPathsDialog` + `PrecisionDialog` on your own, then asked for help crossing the finish line.

## 3. How You Want to Communicate

1. **Workspace-first edits, layman explanations.** The agent edits the workspace `.py` files directly and re-runs the test suite; the user copies whole files. In chat: WHAT changed and WHY in plain language (2–3 lines per change, jargon defined), plus verification steps. No 600-line regenerations in chat, no line-by-line pasting unless asked.
2. **Explain the why in 2–3 lines.**
3. **Show verification.** ("Click Point Number header, you should see 1,3,6,6a,6b,15,a2,a4,a20")
4. **Tables, summaries, checklists.**
5. **End with a mission.**
6. **Respect the code.** Don't rename variables unless needed. Keep Qt conventions: `parent=None`, `if __name__ == "__main__":`, `sys.exit(app.exec())`.

## 4. What Works Best With You

- Small fix first, then the concept behind it.
- When a full file is pasted, scan for **duplicate blocks** (the blank-Description bug was a duplicated Point Number block overwriting column 4).
- Define Qt jargon on first use: `QStackedWidget` = "deck of cards", `lambda` = "tiny anonymous function".
- Windows quirks: spaces in paths (`ai learning 3.py` → needs quotes), terminal holding open with "Press any key", needing the `.py` extension.
- When a feature split has a cost difference, call it out: **rescan disk** (slow, do rarely) vs **redraw table** (cheap).

## 5. What To Avoid

- Regenerating 600 lines when 5 lines changed.
- **String-sorting numbers.** Correct survey sorting is non-negotiable.
- Truncating instead of rounding.
- Adding parked features. If told "hold off on field book", don't add field book code.
- QSS / advanced patterns before the core works.

## 6. Starting a New Chat (current modular system)

1. Paste or link: this profile + `README.md` + `program/run.py` + `program/fieldwork_manager/ui_main.py` (or whole `program/` zip).
2. The AI must acknowledge it understands the architecture — `NaturalSortItem` + `natural_key()` with `letters_first`, `NumericSortItem` (stores the float separately from display text), split dialogs (`ProjectPathsDialog` → rescan, `PrecisionDialog` → redraw, `CorrectionRulesDialog` / `LineCommandsDialog` (fillable Command → locked Meaning) → stored in `.fwb` extra), **two dirty flags** (`is_dirty` for the project JSON, `edit_dirty` for the working file + `check_dirty`), persistent hide/show tabs with a View menu, canonical `.fwk` export (OID assigned after folder→point sort) — then **ask what the next incremental step is**.
3. Changes land in `program/fieldwork_manager/*.py`, verified with `py_compile` + offscreen smoke, the user copies the `program/` folder or `FieldworkManager_v1.0001.zip`: done.

## 7. Environment

| Item | Value |
| :--- | :--- |
| OS | Windows |
| Python | *(fill in from mission below)* |
| PyQt6 | *(fill in from mission below)* |
| Filenames | Spaces in paths — quote them in terminal |

**Mission (run once):**
```
python -c "import sys, PyQt6.QtCore; print(sys.version.split()[0], PyQt6.QtCore.PYQT_VERSION_STR)"
```

---

## 8. Domain Reference (durable rules — do not change without asking)

### File format
- **5-column CSV, no header:** `Point Number,Northing,Easting,Elevation,Description`
- Nested deep: `project > survey > fieldwork > crew date folder (maybe deeper)`
- Descriptions may contain commas — extra columns get re-joined.
- Scanner: `.csv` only, case-insensitive, recursive (`Path.rglob("*")`).

### Sort conventions
| Column | Rule | Ascending example |
| :--- | :--- | :--- |
| Point Number (0) | Natural sort, **letters-first** | `a2, a4, a20, c43, 1, 3, 6, 6a, 6b, 15` |
| N / E / Z (1–3) | True numeric sort via `NumericSortItem` | smallest → largest |
| Description (4) | Natural sort, **numbers-first** | `1, 2, 3, a, b, c` |

### Rounding convention (decided 2026-09-17)
- **Half-up** (`decimal.ROUND_HALF_UP`) — standard survey practice: `2.5 → 3`, `102.335 → 102.34`.
- **Not** Python's default half-even: `round(2.5)` → 2, `round(102.335, 2)` → 102.33.
- Parse via `Decimal(str(num))` to dodge binary-float artifacts (`round(2.675, 2)` → 2.67, should be 2.68).
- Round, never truncate. Implemented in `_format_decimal` only.

### Sample test data
Save as a CSV under the fieldwork folder; use for sort + rounding verification.

```csv
a2,100002.675,500000.125,102.335,AXLE
a4,100033.21,500044.4455,101.9,BOLT
a20,100050.0,500060.75,100.425,12 OAK
1,100001.5,500005.25,103.15,3 PINE
3,100003.75,500006.0,104.0,AXLE 2
6a,100006.1,500007.3,105.55,1 IRON ROD
6b,100006.2,500007.35,105.6,2 IPF
15,100015.99,500016.125,106.035,BENCHMARK
```

**Expected results:**

| Test | Expected |
| :--- | :--- |
| Sort Point Number asc | `a2, a4, a20, 1, 3, 6a, 6b, 15` |
| Sort Description asc | `1 IRON ROD, 2 IPF, 3 PINE, 12 OAK, AXLE, AXLE 2, BENCHMARK, BOLT` |
| Precision N/E = 2, row a2 | N `100002.68`, E `500000.13` |
| Precision Elev = 2 | a2 Z `102.34`, a20 Z `100.43`, 15 Z `106.04` |

### Working file schema (v2 — 2026-09-18)

The export is a **working file** (start of the editing pipeline), not a deliverable. Headerless by design — in-house structure is always the same. Content is plain CSV, saved with the extension **`.fwk`** (constant `WORKING_FILE_EXT` in code; avoids `.fld`/`.fbk` collisions with other tools).

**File classes:**

| Extension | Class | Home |
| :--- | :--- | :--- |
| `.csv` | Raw field data | Fieldwork tree (scanned recursively, `.csv` suffix only) |
| `.fmp` | Project file (paths, precision, notes) — custom ext, JSON inside | Project folder |
| `.fwk` | Working/edit file | Project folder |
| `.fwb` | Field-book library (distilled Carlson F2F, headered, 6 cols) | Project folder |
| *(future)* | Clean points (fixed numbers/descriptions), geometry fixes | Project folder |

The scanner's `.csv`-only filter means `.fwk` files are ignored by raw scans **by mechanics, not discipline** — file type is differentiated by extension. Convention stays: fieldwork tree = raw data only; project folder = everything the pipeline produces.

| Col | Content | Notes |
| :--- | :--- | :--- |
| 1 | **OID** | Position 1..N at export time, assigned AFTER the canonical sort (Source File path, then Point Number natural letters-first). Arbitrary tracking only — NOT a durable ID. Changes with every export; does not necessarily match on-screen row numbers. |
| 2–6 | RawPoint, RawNorthing, RawEasting, RawElevation, RawDescription | Values as displayed (precision locked at export) |
| 7–8 | Parent Folder, Source File | Provenance metadata |

**Rules:**
- **Append-only schema.** Future metadata (NewPoint, duplicate resolution, fieldbook codes, corrections) is added as NEW columns at the END. Never reorder, insert, or delete columns — position IS the schema.
- **Home = project folder.** The `.fwk` extension makes working files invisible to raw scans anywhere on disk, but project folder remains the intended home for all pipeline files.
- **Project linkage:** the project JSON stores `working_file` (path to the `.fwk`). Opening a project reopens its working file into the edit tab automatically; if the file was moved, reattach via **File > Load Edit File (Ctrl+E)**. Changing the link marks the project dirty so it must be saved.
- Metadata grows over time; stripped-down data views are built FROM the full working file, never by deleting from it.

**Rules:**
- **Append-only schema.** Future metadata (NewPoint, duplicate resolution, fieldbook codes, corrections) is added as NEW columns at the END. Never reorder, insert, or delete columns — position IS the schema.
- **Home = project folder.** The `.fwk` extension makes working files invisible to raw scans anywhere on disk, but project folder remains the intended home for all pipeline files.
- **Project linkage:** the project JSON stores `working_file` (path to the `.fwk`). Opening a project reopens its working file into the edit tab automatically; if the file was moved, reattach via **File > Load Edit File (Ctrl+E)**. Changing the link marks the project dirty so it must be saved.
- Metadata grows over time; stripped-down data views are built FROM the full working file, never by deleting from it.

### Field-book library schema (v1 — 2026-09-19)

Headered, never sorted, Model A artifact — generated from Carlson F2F CSV, reconverted when Carlson changes. Extension `.fwb` (`FIELDBOOK_EXT`).

| Col | Content | Notes |
| :--- | :--- | :--- |
| 1 | Code | From Carlson `Code` |
| 2 | Description | From `Description` — quoted commas preserved (`"1/2"" cirf"` → `1/2" cirf`) |
| 3 | Symbol | From `Symbol` (e.g. `CG08`, `Iron_Pin_Found`) |
| 4 | Layer | From `Layer` (e.g. `V-SITE-DEFAULT`) |
| 5 | Entity Type | From `Entity Type` mapped 0→Point,1→Line,2→2D Polyline,3→3D Polyline; unknown codes kept as-is with warning |
| 6 | Category | From `Category,<name>` group rows; rows before first group get `Default`; blank row 2 is skipped; source order preserved (never sorted) |

Rules: **headered** (field data CSVs stay headerless); **append-only** (future `Symbol Size` etc add at END); home = project folder; JSON stores `fieldbook_file`; `Tools > Convert Field Book (F2F CSV → .fwb)...` picks source CSV then save `.fwb` (defaults to project folder), auto-wires path and opens **Field Book** tab (read-only viewer). Viewer is **View → Field Book** (hide/show). `Check File` if no book prompts `Pick an existing file now?` and loads viewer.


---

## Project Status Log (rolling, newest first)

- **2026-09-19 (merge)** — Field-book converter + viewer shipped and merged: `Tools > Convert Field Book (F2F CSV → .fwb)...` (Model A, headered 6 cols `Code,Desc,Symbol,Layer,Entity Type,Category`, `Default` before first `Category`, blank row 2 handled, quoted descs kept, never sorted, lean ~92 KB from 250+ col source), **Field Book** viewer tab (read-only, 4 tabs at startup, `View → Field Book`), `Settings > Project Paths` 3rd field `Field Book (.fwb)`, JSON `fieldbook_file`, `Check File` guard prompts to pick, Edit tab gets `Open Edit File...` button. `fieldwork_manager_v1-1.py` folded into `fieldwork_manager_v1.py`, candidate deleted, `APP_MODULE` re-pointed, **106-check suite green**.
- **2026-09-19** — App renamed `working_fieldwork_v2.py` → **`fieldwork_manager_v1.py`** ("past testing working sets"); versioning + archive policy adopted (bump suffix on major milestones, archive previous). Test dial (`APP_MODULE`) updated. 56-check suite green.
- **2026-09-18** — Three-file handoff adopted; `fieldwork benchmark.pdf` retired/deleted. GUI: movable + closable hide/show tabs, View menu, edit tab exists at startup with empty-state hint, button renamed "Create Edit Fieldwork...". 56-check suite green.
- **2026-09-17/18** — Edit phase shipped end-to-end: canonical `.fwk` export (OID after folder→point sort), edit tab with its own dirty flag + Save/Discard/Cancel, JSON `working_file` link with auto-reopen + Load Edit File (Ctrl+E) relocate, half-up rounding fix, `_fill_row`/`_row_texts` helpers, signal-echo fix in Close Project.
- **Earlier** — View phase (original handoff): recursive scan, combined/single-file views, natural + numeric sorting, split settings dialogs, JSON project persistence.

**Parked roadmap (original priority order):** ① field book path in settings ② Carlson vs custom field book table structure ③ select field books alongside CSVs ④ description filters built from the field book list. **Nearest doors:** Check File implementation (duplicate Point Numbers first), then clean-points file, then geometry fixes. No filtering until field book is done.

---

## 9. Study Recap — Python Fundamentals

| Concept | What you learned |
| :--- | :--- |
| **OOP basics** | `variable = ClassName(args)` creates an *object* from a *class* (blueprint) |
| **`class Child(Parent)`** | Inheritance — "Is-A" rule |
| **`__init__`** | Constructor; runs automatically on creation |
| **`self`** | "This specific object"; shares data via `self.is_dirty` etc. |
| **`super().__init__()`** | Run the parent's setup first (foundation before bedroom) |
| **Default args** | `def __init__(self, path="", parent=None)` — caller can omit them |
| **`sys.argv` / `sys.exit`** | Command-line word list; report 0 (clean) / 1 (error) to OS |
| **`if __name__ == "__main__":`** | Only run when launched directly, not imported |
| **`lambda:`** | Tiny anonymous function; passes arguments to slots |
| **`pathlib.Path`** | `.rglob("*")`, `.suffix`, `.parent.name`, `.relative_to()` |
| **`csv.reader`** | Read headerless CSV rows as lists |
| **`json.dump` / `json.load`** | Save/restore project state |
| **`re.split(r'(\d+)', s)`** | Split text into digit / non-digit chunks |
| **Tuple sort keys** | `(type_code, value)` so Python never compares `int < str` |
| **`decimal.Decimal`** | Exact base-10 rounding; `quantize(quantum, ROUND_HALF_UP)` |
| **f-string `:.nf`** | Fixed-decimal display of a rounded number |
| **Dunder methods** | Override `__lt__` to control comparison |
| **`try / except`** | Graceful fallback when `float()` or file reads fail |
| **`getattr(obj, name, default)`** | Safe attribute lookup |
| **DRY principle** | One helper (`_fill_row`) instead of copy-pasted blocks |

## 10. Study Recap — PyQt6: The Skeleton

```
App -> Window -> Show -> Loop
```

```python
app = QApplication(sys.argv)
window = MainWindow()
window.show()
sys.exit(app.exec())
```

**The 5 Pillars:** ① Subclassing ② Layouts (never `.move(x,y)`) ③ Signals → Slots ④ `QMainWindow` vs `QWidget` vs `QDialog` ⑤ Parent-child ownership (memory, centering, modality)

## 11. Widgets Used

| Widget | Purpose |
| :--- | :--- |
| `QLabel`, `QPushButton`, `QLineEdit`, `QTextEdit` | Basic text / input |
| `QSpinBox` | Numeric input with range |
| `QGroupBox` | Titled settings group |
| `QTabWidget` | Fieldwork / Notes tabs |
| `QSplitter` | Draggable divider, file list vs table |
| `QListWidget` + `QListWidgetItem` | Checkable file list; full path in `UserRole` |
| `QTableWidget` + `QTableWidgetItem` | Points table |
| `QHeaderView` | Column resize mode (`Interactive` vs `Stretch`) |
| `QFileDialog` | Native file/folder pickers |
| `QMessageBox` | Save / Discard / Cancel |
| `QDialog` | Modal settings (`accept()` / `reject()`) |
| `QMenuBar` / `QAction` | Menus, shortcuts, `.triggered.connect()` |

## 12. Key PyQt Patterns

- **Menus:** `addMenu()` → `addAction()` → `setShortcut()` → `triggered.connect()`
- **Modal dialogs:** `if dialog.exec() == QDialog.DialogCode.Accepted:` then pull data via getters
- **`closeEvent`:** intercept X button; `event.accept()` / `event.ignore()`; `self.close()` on Exit reuses the same unsaved-changes check
- **Dirty-state:** `is_dirty` + `_mark_dirty()` + title/status `*`
- **`blockSignals(True/False)`:** stop `itemChanged` while bulk-updating checkboxes
- **`setSortingEnabled(False)` → fill → `(True)`:** don't let Qt re-sort mid-population
- **Custom sort items:** subclass `QTableWidgetItem`, override `__lt__`
- **Rescan ≠ redraw:** paths changed → `_scan_fieldwork()`; display changed → `_update_combined_points()`
- **One row-builder:** `_fill_row()` serves both combined and single-file views

## 13. Debugging Skills

- **Read tracebacks bottom-up:** error type → offending line → call chain
- **"Did you mean…?"** hints catch typos (`setSortcut`)
- **`NameError`** = referenced before defined; **`TypeError: '<' int vs str`** = mixed-type sort key
- **Silent bugs** (blank Description) = duplicate copy-pasted block — no traceback, read the code
- **Exit codes:** `0` clean, `1` Python error, negative = hard crash inside Qt
- **Pre-flight compile check:** `py -m py_compile file.py` — catches stray tokens (a pasted `py` launcher command, missing imports) in 3 seconds, without launching the GUI. Run after every paste.
- **Signal echo:** reset methods must clear flags AFTER clearing widgets that fire signals. `notes.clear()` emits `textChanged` → `_mark_dirty()`, so `is_dirty = False` must come after it, or the project silently re-dirties itself (found by the test suite's Close Project → Open sequence).
- **Verification:** `python -m py_compile program/fieldwork_manager/*.py` + offscreen smoke (`QT_QPA_PLATFORM=offscreen python -c "from PyQt6..."`) after any structural change. Old `test_fieldwork_manager.py` 106-check suite retired 2026-09-23 with the single-file era.


=== docs/FUTURE_FIX_TOOLS.md ===
# Future Fix Tools — Flags → Actions (Not Auto-Fix)

> You noted: we are catching the bulk, but we try too hard to automate. Instead, create flags + tools to solve them. Note for future, move-as-you-go.

## Principle

- **Flags are decision points, not auto-fixes.** System suggests, user decides.
- Keep OID-minimal + live-pull — edits happen in Edit table (source), error file is view.

## Four Tools (per flagged OID/token)

### 1. Fix Error — Best Guess Replace
- System proposes best guess: e.g., `asldf` → closest F2F code `NG` (Levenshtein + F2F frequency), or `ST PT` → insert `PC` between? `deck` → maybe `DECK`? 
- User clicks **Fix** → replaces token, validates (F2F + line-order re-check), updates Edit Desc live, clears flag if valid, may auto-clear second error if it was dependent (e.g., fixing code removes UnknownCode which also removes Orphan).
- Needs: `suggest_fix(flags, raw_desc, f2f_set)` helper in `parse.py`.

### 2. Key-In Fix Error — Replace Error Token (validate to accept)
- User types replacement for the flagged token only: e.g., flagged `UnknownCode:asldf` — user keys `NG` into inline editor.
- Validate: check `NG` in F2F (or `NG1` → `NG` via trailing strip). If valid, replace that token in RawDescription, keep rest, re-parse, update.
- UI: double-click flagged token in Desc Error row → small `QLineEdit` + Validate button.
- Same auto-clear of dependent second error.

### 3. Skip Error — Ignore
- User marks `Status=Skipped` or `Comments=Skip — ...` — row stays in `.chk` but filtered out? Or set `Status=Ignored` and future runs keep it suppressed per OID+flag?
- Needs: persist `Status` in unified `.chk` (already have column) + filter `Status != Skipped` option.
- Does NOT edit Edit table.

### 4. Key-In Entire Code — Full Rewrite
- User completely retypes entire code string: e.g., Raw `asldf` (OID 45) → user types `NG` → updates entire code after validation, removes second error token if it was inside same raw.
- UI: Edit button in Desc Error row → `QDialog` with `QLineEdit` for full RawDescription, Validate (parse + F2F + line-order), Save → updates Edit table's Description cell for that OID, re-runs line validation for that line_id across OIDs (may clear multiple flags).
- Catch if possible: after full rewrite, re-run `parse_desc_field` + `_validate_line_command_order` for that line_id, see if second error for same OID or other OIDs in same line now clears — show toast "also cleared LineOrderError on OID 46".

## Implementation Notes (for when we do it)

- All four tools edit **Edit table / .fwk source**, not the error file directly. Error file is regenerated or patched after edit + re-validate.
- Keep `DisplayTab` + `Flags` in `.chk` — after fix, re-run checks or patch that OID's row out.
- Need `edit_desc_for_oid(oid, new_raw_desc)` helper in `ui_main.py` that updates `edit_table.item(oid_row, 5)` + marks `edit_dirty`.
- Move-as-you-go: don't build all four at once. Start with **#2 Key-In Fix Token** (smallest) + **#3 Skip** — covers 80% of 10-year errors per your note.

## Move-as-you-go Plan

- Now: modules split done, flagged-only + unified done. Next session pick one tool (suggest #2) and wire it to Desc Error table double-click.
- Error catalog was removed 2026-09-23; use real field errors from uploads/ or live .chk as source.



=== docs/EXTENDING.md ===
# Extending Plumbline

Three levels, from least to most work: **scripts** (one-offs), **plugins** (commands / importers / exporters that live in the menus),
and **editing the source** (everything under `plumbline/` is ordinary Python; the engine in `core/` and `io/` has no Qt in it).

## The data model (`plumbline/core/model.py`, `project.py`)

```python
project = Project(name, crs)            # everything for one job
project.crs            # ProjectCRS (pyproj CRS + optional ground scale + elevation unit/datum + datum strategy)
project.h_unit, v_unit # "ftUS" | "ft" | "m"
project.points         # {id: SurveyPoint(id, number:str, x=easting, y=northing, z (NaN = none), desc, layer, attrs)}
project.entities       # {id: Polyline | TextEntity}
   Polyline(id, layer, verts (N,3) x/y/z, closed, bulges (N,) DXF-style or None, kind, color, linetype, attrs, derived)
   TextEntity(id, layer, x, y, text, height, rotation, color, attrs, derived)
project.surfaces       # {id: Surface(name, pts (N,3), tris (M,3), params, style, report)}   .tin() -> TIN
project.layers         # {name: Layer(name, color, linetype, visible, locked)}
project.codes          # FeatureCodeTable
project.imagery / .checks    # imagery layers and recorded checks
project.settings       # dict saved with the project (contour interval, text height ...)
```
Conventions: **x = easting, y = northing** everywhere in code (the UI shows N,E by default); angles are azimuth degrees clockwise from north in `cogo`,
counter-clockwise from +x for text rotation; arcs are DXF bulges (`tan(sweep/4)`, positive = counter-clockwise).
`derived` marks generated entities (`"linework"`, `"contours:<surface>"`) that are replaced when regenerated.
After changing things directly, call `project.touch()` (the UI's `state.edit(...)` does it for you).

Useful functions: `project.add_point / add_polyline / add_text` (adding many points in a loop? pass `number=` yourself - without it every call scans all existing points for the next free number, which gets slow beyond ~10,000 - or build an `ImportBatch` and use `apply_batch`), `project.apply_batch(batch, dup_policy)`, `project.process_linework()`,
`project.apply_similarity(...)`, `project.reproject(new_crs)`, `project.assign_crs(new_crs)`, `project.point_arrays()`;
`core.surface.build_tin / build_surface_from_project / generate_contours / volume_between`, `TIN.z_at / contours / volume_to_datum / profile / sections`;
`core.crs.search_crs / ProjectCRS / make_transform / list_operations / combined_factor`; `core.cogo.inverse / direct / traverse / parse_bearing`;
`core.imagery.compute_check_stats`; `core.qa.run_checks`; `core.scene3d.build_scene / Camera / render_orbit / DepthSpec` (the 3D and depth views);
`core.maps.google_maps_url / maps_link_for_view`; `io.dxf_io / landxml / gis_io / kml_io / csv_points / f2f / reports`.

## Scripts

```python
# run via Plugins > Run Script, the console dock, or:  python -m plumbline run fence_offsets.py job.plb --save out.plb
import numpy as np
fence = [p for p in project.points.values() if p.desc.upper().startswith("FNC")]
print(len(fence), "fence points; mean elevation", np.mean([p.z for p in fence]))
with api.edit("Move fence layer"):            # one undo step (a no-op wrapper when run headless)
    for p in fence:
        p.layer = "SITE-FENCE-REVIEW"
        project.ensure_layer(p.layer, (255, 128, 0))
```

A 3D picture without opening the window (`core/scene3d.py` has no Qt in it):

```python
# python -m plumbline run picture3d.py job.plb
from PIL import Image
from plumbline.core import scene3d as S
sc = S.build_scene(project)                                    # the visible layers + surfaces, flattened to arrays
cam = S.Camera(azimuth=30, elevation=35, vexag=S.auto_vexag(sc.bounds))      # bearing it looks along / height angle
cam.fit(sc.bounds, 1400, 900)
Image.fromarray(S.render_orbit(sc, cam, 1400, 900, S.RenderOptions())).save("view3d.png")      # RGBA, transparent background
```

## Plugins

Files in the plugin folder (`~/.plumbline/plugins` unless `PLUMBLINE_HOME` is set) are imported at start-up and on *Reload*. The decorators register things:

```python
from plumbline.plugins import command, importer, exporter, Param
from plumbline.core.model import ImportBatch, SurveyPoint

@command("Survey/Shift elevations", params=[Param("dz", float, 0.0, "Add to every selected elevation")])
def shift(api, dz=0.0):
    with api.edit("Shift elevations"):
        for p in api.selected_points():
            p.z += dz

@importer("Space-delimited PNEZD (*.txt)", [".txt"])
def read_it(path, api):
    batch = ImportBatch()                                  # coordinates in the FILE's system; Plumbline asks which and converts
    for line in open(path):
        parts = line.split(None, 4)                        # point  northing  easting  elevation  [description...]
        if len(parts) < 4:
            continue
        num, n, e, z = parts[:4]
        batch.points.append(SurveyPoint(0, num, float(e), float(n), float(z), parts[4].strip() if len(parts) > 4 else ""))
    return batch                                           # (x = easting, y = northing)

@exporter("Point count (*.txt)", ".txt")
def write_it(project, path, api):
    open(path, "w").write(f"{len(project.points)} points\n")
```
(`api` offers `project`, `state`, `log()`, `message()`, `ask()`, `edit()`, `selected_points()`, `selected_entities()`, `points()`, `add_point()`, `import_batch()`.)
Commands run inside one undo step and are rolled back if they raise; a broken plugin file shows up in *Plugin Manager* and never blocks start-up.
`plumbline/plugins/examples/` has four small working examples.

## A new importer in the source

1. Write `io/myformat.py` with `read_myformat(path) -> ImportBatch` (points / polylines / texts / surfaces / layers, plus `batch.info` such as `{"epsg": 2276, "units": "ftUS"}`).
2. Add a branch in `ui/import_export.py::Importer.import_path` that calls `Importer._generic(path, batch, title, file_crs=..., unit=...)` - that gives you the CRS / units / duplicate-handling dialog for free.
3. Add a round-trip test in `tests/test_io.py`.

## Threading rule

Heavy work may run on a worker thread via `ui.widgets.run_blocking(...)`, but **only code that reads the project or computes pure results** - never mutate the project there
(the canvas repaints while the busy dialog is up). The pattern used for surfaces and contours: gather inputs on the GUI thread -> compute on the worker -> apply inside `state.edit(...)`
on the GUI thread (`core.surface.compute_contour_data` / `apply_contours` show it).

## Tests

`pytest` runs everything (`QT_QPA_PLATFORM=offscreen` on a machine without a display). `tests/test_ui.py` shows how to drive the real window: dialogs are auto-accepted by patching
`QDialog.exec`, clicks go through `QTest.mouseClick` on the canvas, and a tiny local HTTP server stands in for the tile service.


=== docs/MERGE.md ===
# How Fieldwork Manager became part of Plumbline

**Merged 2026-10-03.** Fieldwork Manager (FM) used to be a separate program: raw Carlson download in,
checked `.fwk` working file out. Plumbline draws the survey. A surveyor's job crosses both, and the step
between them was done by hand.

The merge is deliberately **lopsided**: FM's *core* became part of Plumbline's engine where it can be
tested headless, its *window* was ported and opened beside Plumbline's, and its *standalone program* is
gone. Nothing was rewritten for the sake of it.

```
   RAW FIELD DATA                 the job folder                     THE SURVEY
   Carlson .fwk / F2F          Field Data/Week 1/Crew 6/           drawing, surface,
   duplicate PtNums            Field Book/ (1,717 codes)     -->   volumes, imagery,
   bad descriptions            Control/  Reports/                  DXF / KML / reports
            \_______________________ Field Data _______________________/
                    one job folder, two windows in one program
```

## Where each piece went

| Fieldwork Manager | Now | Notes |
|---|---|---|
| `program/fieldwork_manager/config.py` | `plumbline/fieldwork/config.py` | point blocks, the crew rule, correction rules, file extensions |
| `…/detectors.py` | `plumbline/fieldwork/detectors.py` | exact duplicates, look-alike numbers, points on top of each other |
| `…/io_carlson.py` | `plumbline/fieldwork/io_carlson.py` | `.fwk`, `.fwb`, `.chk`, `.fwc` read and write |
| `…/parse.py` | `plumbline/fieldwork/parse.py` | description parsing, line-order validation |
| `…/coord_systems.py` | `plumbline/fieldwork/coord_systems.py` | **kept as the coordinate-system library** (see below) |
| `…/steps_report.py` | `plumbline/fieldwork/steps_report.py` | the "what is left to fix" report |
| `…/utils_sort.py` | `plumbline/fieldwork/utils_sort.py` | natural sort; the one `QTableWidgetItem` subclass moved to the window |
| `…/clean.py`, `…/renumber_tool.py` | `plumbline/fieldwork/clean.py`, `renumber_tool.py` | the fix dialogs, ported as-is |
| `…/ui_main.py` (7,498 lines) | `plumbline/fieldwork/ui_main.py` | the window, PyQt6 → PySide6 |
| — | `plumbline/fieldwork/bridge.py` | **new**: the seam. Rows → project points, F2F → feature codes, checks |
| — | `plumbline/fieldwork/sample_real.py` | **new**: builds the Real World sample out of a real download |
| `program/run.py`, `Run Fieldwork Manager.bat` | *(retired)* | the window is `Survey > Fieldwork Manager` in the one program |

`plumbline/fieldwork/` is **Qt-free except `ui_main.py`** and a test enforces it: the CLI and the test
suite import the whole field-data core in a subprocess with `PySide6` poisoned, so a stray Qt import in
`detectors.py` fails the suite rather than the user's terminal.

## The four seams, and how each was settled

**A — the office code library.** FM read the office's Carlson Field-to-Finish standard; Plumbline drew
on its own generic starter layers. Now `bridge.feature_codes_from_f2f()` reads the F2F and produces
Plumbline `FeatureCode`s, so a job draws on `V-PROP-CRNR`, not `TOPO-GROUND`. The real job's 1,717-code
standard loads unchanged.

**B — one description engine.** FM validated (`/` starts free text, `PC`/`PT` are curve commands, the
code must exist in the F2F); Plumbline interpreted (`B`/`E`/`CLS` flags, `EP1` line instances). Both
dialects now run in one place. The rule that settled the conflict: **an exact whole-word match against
the code table wins, and only then are trailing digits read as a string number** — which is what a
1,717-code standard where 1,314 codes end in a digit actually needs. On the real job that is the
difference between resolving 3,749 and 3,763 of 3,773 descriptions.

**C — one QA pass, two windows.** FM's detectors (clerical errors) and Plumbline's surface checks
(geometric blunders) stayed where they were and are honest about it: FM cannot spike-test an elevation
without a surface, and Plumbline cannot know that point `600B` is a re-shot of `600`. Each window shows
what it can prove. Merging them into a single `qa.run_checks()` was proposed and **not** done — it would
have made a field-data check depend on a built TIN.

**D — ground scale: SAF, flipped on purpose.** This was the one place the merge could move real numbers.

| | Old FM | Old Plumbline | Now |
|---|---|---|---|
| Name | SAF | `GroundScale.factor` (a combined factor) | `SurfaceAdjustmentFactor.saf` |
| Direction | ground / grid | grid / ground | **ground / grid** |
| Scales from | origin (0, 0) | a base point | either, recorded in the project |
| `ground =` | `grid * saf` | `base + (grid - base) * factor` | `base + (grid - base) * saf` |

`SAF` is always slightly above 1 (a ground distance is longer than the grid distance between the same
two points); the old combined factor was its reciprocal, which is why the two must never be typed into
the same box. There is now exactly **one** implementation of the scaling - `SurfaceAdjustmentFactor` in
`core/crs.py` - and the field window's export code calls it (`fieldwork/coord_systems.py` keeps its old
function names and delegates), so the two halves of the program cannot drift apart by 170 ppm. Projects written before the rename stored the reciprocal and are **inverted automatically
on load** — old jobs move exactly as they did the day they were saved. *Scale from origin (0,0)* is a
one-click button because that is the TXDOT SOP.

## The coordinate system

**The field side's library wins.** `plumbline/fieldwork/coord_systems.py` is now the one list of Texas
systems, and `plumbline/core/crs.py` uses it as its Texas library rather than keeping a second copy.
It is the better of the two: NAD83(2011) codes only, metres and US survey feet side by side, a
`migrate_epsg()` that moves legacy 2275-2279 forward instead of handing back a deprecated zone, and a
pure-Python Lambert fallback so a job still converts on a machine where pyproj will not install. It was
tested on Windows against imagery, which is the other reason not to touch it.

**The default is unassigned, and that is a feature.** A new project starts with **no coordinate system**.
Drawing, surfaces, volumes, DXF and point handling never ask. The first tool that genuinely needs to
know where on the earth the job is — imagery, KML, GIS, reprojection, grid convergence — stops and says
so, by name:

> Grid convergence needs a coordinate system — this project's CRS is UNASSIGNED.
> Select CRS first (Survey > Project Coordinate System…).

## What deliberately did not merge

- **FM's standalone program.** No second launcher, no second settings folder, no divergent copy of the
  code. `python -m plumbline fieldwork job.fwk` does headlessly what the window does.
- **FM's hand-rolled LCC maths.** The numbers were kept; `pyproj`/PROJ does the projection work, with
  FM's pure-Python LCC kept as the fallback when pyproj is missing.
- **The two UIs were not made visually identical.** The field window is 7,500 lines of working UI. It was
  ported (four import lines, PyQt6 → PySide6) and opened beside the drawing rather than rebuilt as
  docks, because "the tools already work" beats "the two windows match".
- **FM's QA into Plumbline's QA** (Seam C above) — see the note there.

## How this was verified

| Check | Result |
|---|---|
| Full test suite | **228 passed** (`QT_QPA_PLATFORM=offscreen python -m pytest -q`) |
| Field-data core without Qt | in a subprocess with `PySide6` poisoned: imports, reads a `.fwk`, rounds half-up |
| Real World sample rebuild | two independent builds differ **only** in the `.plb` `saved` timestamp and the `Root` line of `Job Setup.txt` — every file holding data is byte-identical |
| The real job through the whole path | 3,773 points imported, 3,749 coded against the office standard, 390 polylines, 50 office layers, 14 duplicate numbers renumbered |
| Surface maths | `verification/check_surface_math.py` + three property-test harnesses, all pass |
| Libraries | `python -m plumbline doctor` — every library ok on Python 3.13 (this release targets 3.14) |

## Housekeeping

The workspace was left holding the merged project and nothing else. Removed once the suite was green:
FM's original `program/` tree and its uploads (every module is in the table above; the uploads are
byte-identical to `samples/Real World/Source/` — verified with md5), the Phase-1 review package and its
patch (the fixes are in the code; the review itself is `docs/CODE_REVIEW.md` and `docs/FIXES_APPLIED.md`),
the merge proposal (folded into this file), the standalone launcher, the project handoff snapshots, all
zip containers, and the generated caches (`__pycache__`, `.pytest_cache`, `.plumbline`).

Kept because it is not reproducible: `docs/WORKING_PROFILE.md` (how this workspace is run and what the
domain rules are), `docs/FUTURE_FIX_TOOLS.md` (your note that flags should be decision points, not
auto-fixes), and the Google Earth and steps-report examples under `docs/examples/`.

## Still open

1. **Curves.** FM understands `PC` / `PT` (point of curvature / tangency). Plumbline stores DXF bulges
   but has no code-level curve command. Should `PC`/`PT` become first-class in the parser?
2. **The renumber tool** (482 lines, with protected-number logic) is currently field-window only. Does it
   belong in Plumbline as a drawing-side tool too?
3. **Seam C**, above: one QA pass instead of two, if a field check is allowed to depend on a surface.
4. **Ship shape**: one program is now the answer. `Plumbline.bat` and the console script both open the
   same thing.


=== docs/VERIFICATION.md ===
# Independent verification and the code review

Two things live here, both from the review pass of 2026-10-03:

| If you want to… | Open |
|---|---|
| Know what was wrong and why | `docs/CODE_REVIEW.md` |
| Know what was changed | `docs/FIXES_APPLIED.md` |
| Re-run the independent harnesses | `verification/` (five scripts, run from the repo root) |

The harnesses were written to check the maths and the round trips **from the outside** — cut/fill
integration against analytic formulas, CRS and file round trips, breakline enforcement — rather than to
re-run the program's own assertions. The suite in `tests/` is the author's; these are a second opinion.

## Where the code stands

```
$ cd plumbline
$ QT_QPA_PLATFORM=offscreen python3 -m pytest -q
193 passed
```

- **193 tests passed at the time of the review** (was 181). On Python 3.13 the suite went from *6 failed / 175 passed* to all green.
  The suite is now 228 tests, because the field-data merge added its own.
- Works on **Python 3.13 and 3.14**. 3.14 remains the version it is built and tested against and the
  one the Windows installer prefers; 3.13 is now supported too.
- 12 regression tests were added, one per fix.
- No performance regression: 100k points — TIN 0.99 s, contours 0.13 s, volume 0.04 s, QA 1.09 s,
  save 0.83 s, load 0.37 s.

## Running the verification harnesses

They locate the source relative to themselves (the repo root), so no setup is needed:

```bash
cd verification
QT_QPA_PLATFORM=offscreen python3 check_surface_math.py    # ALL SURFACE MATH CHECKS PASSED
QT_QPA_PLATFORM=offscreen python3 property_tests.py        # ALL ROUND-TRIP / PROPERTY CHECKS PASSED
QT_QPA_PLATFORM=offscreen python3 property_tests2.py       # ALL CHECKS PASSED
QT_QPA_PLATFORM=offscreen python3 property_tests3.py       # ALL CHECKS PASSED
QT_QPA_PLATFORM=offscreen python3 bench.py                 # the performance table
```

These are **independent** of the project's own test suite — they were written to check the maths and
the round trips from the outside, not to re-run the author's assertions.

## What was verified, briefly

- **Cut/fill integration** (`_split_volumes`): worst relative error **4.1e-9** over 20,000 random
  triangles against independent analytic integration; exact to 1e-16 on a plane.
- **Breaklines**: 59/59 sub-segments become real TIN edges; ridge elevation exact; no point moved.
- **CRS**: 5 pairs round-trip within **2.8e-8** (sub-micrometre in feet); Z, text heights and
  contour intervals convert correctly.
- **Save/load is byte-identical**; DXF/CSV/GIS round trips preserve bulges, Z and layers.
- **Contours**: exact on a plane to 5e-8; the deviation on a cone matches the predicted
  inscribed-polygon sagitta and shrinks with mesh refinement (discretisation, not a bug).

Full detail, including the things that are *already* right and should be left alone, is in
`docs/CODE_REVIEW.md`.

## Known open items (deliberately not changed)

- LandXML: no profiles, cross-sections or pipe networks; spirals become chords.
- DXF: hatches, dimensions and leaders are skipped on import; no paper-space layouts.
- Surfaces are TIN only; no grid surfaces, end-area or corridor volumes.
- `search_crs` scans the whole EPSG + ESRI catalogue linearly per keystroke — fine at 400 results,
  but a prefix/trigram index is the next step if the CRS dialog ever feels slow.
- Label placement on closed contours can place two labels near the loop's start/midpoint; needs a
  placement rule rather than a patch.
- Pre-existing, correct-but-surprising behaviour, now documented rather than changed: a multi-layer
  GeoPackage import reads the first layer unless the dialog picks one; `volume_between` reports
  `cut` when the existing surface is above the proposed one.


=== docs/examples/ ===

--- Fieldwork_GoogleEarth_EXAMPLE.kml ---
<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Example SAF 1.00014 Ground clampToGround</name><Placemark><name>PRS843728484041</name><Point><altitudeMode>clampToGroun...

--- Fieldwork_GoogleEarth_EXAMPLE.kmz --- ERROR reading

--- STEPS_REPORT_EXAMPLE.csv ---
﻿Step,Title,Total,Fixed,Open,Pct,Status,Detail
1,1. Fix All Description Errors,36,0,36,0,Not Started,"0/36 fixed, 36 Open"
2,2. Fix Duplicate Errors,26,0,26,0,Not Started,"0/26 fixed, 26 Open"
3,3. Fi...

--- STEPS_REPORT_EXAMPLE.html ---
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Steps Report — Fieldwork Manager</title>
<style>
 body{font-family: Segoe UI, Arial, sans-serif; margin:24px; color:#222}
 h1{font-si...