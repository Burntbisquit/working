# Geometry Segments & Linework Tools — Game Plan

Status: **v4 — decisions baked in from discussion**, 2026-10-15.
Baseline: PR #4 ("Add Field Book safeguards and improve QA workbenches",
merge commit `46731aa`, merged 2026-10-15 21:24 UTC — verified).

---

## 1. What exists today (verified in code)

| Capability | Where | Notes |
|---|---|---|
| `SurveyPoint` / `Polyline` model | `core/model.py` | Polylines: `(N,3)` verts, DXF bulges, `kind` (`line`/`breakline`/…), `attrs`. Points carry original-state audit. |
| Point-driven linework generation | `core/project.py` (`process_linework`) | Strings coded points → polylines with `attrs["point_ids"]`, `derived="linework"`, `kind="breakline"` when the feature code says so. Rebuilt from points on reprocess. |
| Breakline definition | `core/featurecodes.py` (`ENTITY_TO_KIND`) | Field Book entity types **Line / 2D Polyline / 3D Polyline / Arc / Circle / Polygon** map to `breakline=True`. |
| Code instances (`CODE`, `CODE1`, `CODE2`…) | `fieldwork/parse.py`, `linecheck.py` | Parser classifies bare codes as `exact` and number-suffixed codes as `line_instance` (trailing and leading digits). Processing walks points in **ascending point-number order** (1, 2, 3, 4, 5…). A label may be closed and deliberately re-opened later; each `ST`→`END` occurrence is its own segment. |
| Transactional line editing | `core/linework_editor.py` | `LineEditDraft`: insert/delete/move/nudge/swap/reverse/curve/straighten/close + undo. `commit_line_draft` writes vertex moves **back to the linked survey points** (point mode). |
| Multi-line join drafts | `core/linework_editor.py` (`JoinLinesDraft`) | Reorder/reverse/auto-orient/average/condense/renumber machinery — **kept, but reorder/renumber flows are de-scoped below (future task).** |
| Semantic description edits | `core/point_linework_coder.py` | Adds/removes ST/PC/PT/END/X in descriptions using active Field Book tokens; multicode-aware. |
| Whole-line sequence checks | `fieldwork/linecheck.py` | Missing ST/END, Missing PC/PT, Line Order. **Already groups coded linework into segments** (ST→END/CLOSE runs keyed by code+occurrence). |
| Geometry primitives | `core/geometry.py` | `segment_intersection`, bulge-aware `flatten_polyline`, lengths, areas, Chaikin. |
| All-pairs spatial machinery | `core/surface.py` (`_split_crossings`) + shapely `STRtree` | Pattern exists for TIN noding; the new detector reuses it for *reporting*. |
| QA workbench UI pattern | `ui/qa_workspace.py`, `ui/check_dock.py` | Findings table → zoom → inline fix → apply/undo → CSV report. |
| Canvas grip editing | `ui/tools.py` (`SelectTool`) | Currently edits any polyline vertex directly — no lock, no point propagation. This is the gap. |
| Tests + CI | `tests/`, `.github/workflows/python-tests.yml` | pytest on push/PR, Python 3.13 + 3.14, offscreen Qt. Gates every merge. |

## 2. Decisions from discussion

1. **Flip past / untwist definitions confirmed:** untwist = reorder/swap a couple
   of vertices to remove a self-crossing "bowtie"; flip past = flip a run of the
   string to the other side of an edge (shots taken in the wrong order past a
   corner).
2. **Crossing-check corpus = every polyline flagged as a breakline**, from any
   source (generated linework and flagged imports). **2D polylines sitting at
   elevation 0 are treated as elevation-less**: they still break, and their
   elevation is taken from the points — exactly as with painted stripes shot at
   z=0 that should still break along real point elevations. Implications:
   - Crossing detector: a plan crossing is always reported; a *Z-conflict* is
     only raised when both lines carry meaningful Z at the crossing (z=0/D-shaped
     lines never fake a conflict).
   - Related surface fix (flagged for Phase 2 scope check): TIN build should
     drape elevation-less breakline vertices onto point elevations instead of
     cutting 0.00 cliffs.
3. **"Line too long" = one global threshold**, configurable in project settings.
   Default proposal: **300 ft** (≈ longest routine topo shot in us-ft; metric
   jobs default 100 m). Tunable per project.
4. **All edit tools migrate into the main window as openable/closable panels
   docked on the right** — including existing workbenches like Fix Point Errors.
   Modal popups that are inherently one-shot (CRS picker, job setup, licence)
   stay dialogs. New tools are built panel-first; existing ones migrate
   progressively (see Phases).
5. **Connect-lines = active-occurrence strategy (details in §4):** a suffix is a
   reusable field instruction label, not a globally unique segment ID. A label
   such as `SIDEWALK1` may be closed and deliberately started again later.
   Segment identity is therefore `(code instance, occurrence)`, with an
   auto-generated display ID. A connector only needs an instance that is **not
   live through its proposed point-number interval**; it must never attach to a
   live string by accident. Prefer the lowest eligible **previously closed**
   label before minting a new suffix. **Point numbers are protected absolutely**;
   tools reuse existing points wherever one lies within snap tolerance and its
   number preserves the intended ascending traversal.
6. **Shrink line commands / command repair is a planned Geometry-panel tool.**
   It will compact a jumped-to instance such as `SIDEWALK10` to the lowest safe
   reusable label while preserving segment breaks, point numbers, geometry, and
   all unrelated multicode sections. It also repairs missing `ST`/`END` pairs
   using the explicit break rules in §3.5.
7. **Line join reorder = future task, discontinued for this effort.** The
   `JoinLinesDraft` code stays, but none of these edits ship reorder/renumber UI.

## 3. Requirement → design mapping

### 3.1 Geometry listed by segments (auto-named IDs)
- New Qt-free module **`core/geometry_segments.py`**: one uniform `Segment`
  model built from (a) Field Book strings (same parse `linecheck` uses) and
  (b) existing `Polyline` entities (each polyline = one segment, or split where
  a vertex carries multiple line codes).
- `Segment`: display-only `seg_id` (`{CODE}-{occurrence}`), source, ordered
  point/vertex list, closed flag, code/instance, layer/kind, 2D/3D length,
  issues. The occurrence is determined by an ascending-number sweep, not by
  spatial adjacency or by the literal suffix alone.
- Single source consumed by the Geometry panel, the crossing detector, and all
  tools.

### 3.2 Point-driven, linked, locked unless editing
- `SurveyPoint` is the single truth. Derived linework (has `point_ids`) is
  **locked by default**:
  - `SelectTool` grip-drag refuses derived linework with a status hint
    ("Enter edit mode"); free/imported polylines stay directly editable.
  - **Edit mode** toggle (per line or per session). Vertex edits route through
    `LineEditDraft` point mode + `commit_line_draft` → moving a vertex **moves
    its point** → every line through that point follows on reprocess. One undo
    transaction per gesture. Locked state is visually distinguishable.

### 3.3 Autodetect: crossing breaklines — ALL of them
- New **`core/crossings.py`** (Qt-free): build breakline corpus (§2.2) →
  bulge-aware `flatten_polyline` → `STRtree` all-pairs → exact
  `segment_intersection` verification → classify (plan crossing / Z-conflict /
  shared-endpoint OK) → **one finding per crossing pair, never early-exit.**
  Unit test plants N crossings and demands N findings (the Carlson single-stop
  failure mode is the pinned regression test).

### 3.4 Geometry properties (clickable point list)
- Segment selected ⇒ per-point table: number, code, **line command**
  (ST/PC/PT/END/X), full descriptor, E/N/Z, length-to-next.
- Edits via `point_linework_coder` inside one AppState transaction → reprocess →
  panel refresh. Fieldbook segments edit descriptions; imported segments edit
  entity attrs/verts.

### 3.5 Shrink line commands / repair command sequence

This is a Geometry-panel tool for cleaning field coding **without changing point
numbers, coordinates, vertex order, or unrelated multicode descriptions**. It
works from the same ascending-number state sweep as the segment builder and
always shows a per-point preview before one undoable apply.

**Break rules.** Both commands deliberately create a segment break, but on
opposite sides of their command point:

| Observed field sequence for one exact instance | Finding | Proposed repair |
|---|---|---|
| First point carrying `CODE`/`CODE1` has no `ST` | **Missing initial ST** | Add `ST` to that first matching code section. |
| An open occurrence is followed by a new `ST` for that instance, and its last matching vertex has no `END`/`X` | **Missing END before ST** | Add `END` to the last vertex of the preceding occurrence. The new `ST` remains the first vertex of the next segment. |
| `END`/`X` is followed later by another matching code section that lacks `ST` | **Missing ST after END** | Add `ST` to that next matching code section. |

“Previous” and “next” here mean the prior/next **point carrying that exact
multicode line section**, not simply the adjacent survey-record number. This is
what makes it safe for interleaved cross-section, curb, and natural-ground
shots. A terminal `END` with no later matching point is already complete and is
not flagged. A second `ST` while the same instance is live remains an explicit
QA finding; the tool can insert the missing prior `END`, but never silently
rearranges vertices.

**Shrink/compact instance labels.** When a crew jumps from, say, `SIDEWALK2` to
`SIDEWALK10` because the currently open labels are unclear, the tool can map the
whole closed `SIDEWALK10` occurrence to the lowest label that is not live over
that occurrence's point-number interval. It changes only that occurrence's
matching code sections, preserves its `ST`/`END` break points, and never merges
the geometry with an adjacent occurrence merely because the label is reused.
Open, overlapping, malformed, or ambiguous ranges are left unchanged and shown
for review. This is a preview-and-accept operation, not an opaque cleanup.

### 3.6 Geometry tools (new `core/geometry_tools.py` unless noted)
| Tool | Basis |
|---|---|
| Nudge points | `LineEditDraft.nudge_vertex` + point commit; arrow-key nudge by step + drag. Attached linework follows via the point link. |
| Auto-nudge | **New solver:** candidates = untwist, flip-past, per-point micro-nudge along crossing normal; score by *crossings removed → fewest points moved → least displacement*; iterate to zero or report the unfixable residue. |
| Square shape | **New dialog:** dominant orientation from edge azimuths mod 90°; snap to 90s; modes *average* (mean basis) / *small* (hold smaller dims) / *large* (hold larger dims) / *lock points* (anchor vertices fixed, 90s solved around them). |
| Flip past | Reflect a selected run of vertices to the other side of the edge; descriptions unchanged (geometry op). |
| Untwist | Local vertex swap removing a bowtie; geometric sibling of the existing code-swap bowtie repair. |
| Curve / recurve | `LineEditDraft.set_curve`/`straighten`; recurve refits an arc through PC/mid/PT and rewrites curve commands. |
| Connect lines | **Active-occurrence algorithm — §4.** |
| Join lines, average | Geometry-only join: move both endpoints to their mean (**both numbers survive** — numbers protected); unify instances via description edits, not renumbers. Reorder/renumber UI: future. |
| Move end to end | Pick mover line + target line; translate (rotate optional) the mover's endpoint onto the target endpoint, via the point link. |
| Add node | `insert_vertex` **node mode** (new): pure vertex, no survey point. |
| Inject point | `insert_vertex` point mode: real numbered point, range-preserving (`_allocate_number`). |
| COGO from lines | **New:** from imported (e.g., LiDAR) polylines, create survey points at starts/ends (dedupe shared), record mapping in attrs. |

### 3.7 Geometry errors
- **Line too long** — per segment, 2D and 3D length vs global threshold (§2.3).
- **Segments lack start/end** — existing `linecheck` findings are expanded to the
  explicit missing-initial-ST / missing-END-before-ST / missing-ST-after-END
  rules above and surfaced project-side so drawing and fieldbook views agree.

## 4. Point-number sweep and connect-lines: active-occurrence spec

### 4.1 The field sequence is not a drawing sequence

Crews regularly interleave shots from several strings. For example, in ascending
point number, a crew can start `SIDEWALK1`, start `SIDEWALK2`, shoot natural
ground and curb shots, start `SIDEWALK3`, later end `SIDEWALK1`, continue
shooting points on `SIDEWALK2` and `SIDEWALK3`, then start `SIDEWALK1` again.
Those unrelated shots never become sidewalk vertices: the exact line-code
section selects the vertices. The repeat `SIDEWALK1 ST` starts a **new
occurrence**, not a continuation inferred from proximity or the shared suffix.

Accordingly, the segment builder makes one ascending-number sweep of the parsed,
multicode-aware descriptions:

- `ST` opens the next occurrence for that exact code instance.
- A matching code section while it is open contributes that survey point to that
  occurrence, even if many unrelated points occur between it and the prior
  vertex.
- `END`/`X` closes that occurrence.
- A later `ST` for the same label opens a later occurrence with a new automatic
  segment ID, e.g. `SIDEWALK1-02`.

The literal suffix is thus a linework-routing label; it is **not** a permanent
unique segment identifier. A new `ST` while that exact label is already live is
a sequence error to expose in QA rather than a reason to silently cross-connect
strings.

### 4.2 Safe connector assignment

Goal: connect two selected segments of a code family **without renumbering or
disturbing existing strings.**

Definitions (parser-backed): bare `CODE` is the base instance; `CODE1`, `CODE2`…
(trailing and leading forms) are instance labels. "Free" is evaluated at the
**proposed connector interval in ascending point-number order**, not across the
entire job. A label that ended before the interval (or begins after it) may be
re-used as a fresh occurrence; a label live across any part of the interval may
not, because it would accidentally absorb the connector points.

Algorithm:
1. Sweep every exact instance in the family and construct its open/closed
   occurrence intervals. Preview those intervals to the operator.
2. For the selected endpoint numbers, consider only labels with no live span
   over the proposed `ST`→`END` interval. Prefer the lowest eligible label that
   has a **previously closed** occurrence; then use the lowest otherwise-free
   suffix. Mint a new suffix only when no existing label is safe. This follows
   the normal field practice of re-starting a formerly closed `SIDEWALK1` later
   in the book.
3. Route the connector **through existing points** when a point lies within snap
   tolerance *and* its number preserves the desired ascending trace. Only mint
   a range-continuing point number for a genuine gap. The tool never reorders or
   renumbers the field book to force a geometric route.
4. Append multicode sections only to the connector points: low-number endpoint
   gets `{sep} CODE{N} ST`, eligible interior points get `{sep} CODE{N}`, and
   high-number endpoint gets `{sep} CODE{N} END`. Thus the point-number sweep,
   not visual proximity, produces precisely the intended new occurrence.
5. Re-run `linecheck` + crossing detector against the preview, show the resulting
   occurrence and warnings, then commit as one undoable transaction.

This preserves every original point number and descriptor section while allowing
several open sidewalk strings to coexist naturally.

## 5. UI architecture: right-dock panels

- New panel host in the main window: right-side `QDockWidget` stack, panels
  open/close independently, state persisted.
- **Geometry panel is the first citizen** (segment list + point properties +
  tool buttons + error list).
- Progressive migration of existing workbenches (Fix Point Errors, Line Repair,
  etc.) behind the same host — workbench content re-parented, logic untouched.
  One-shot popups (CRS, job setup) remain modal dialogs.

## 6. Delivery phases (each = compile-green PR with tests)

| Phase | Content |
|---|---|
| **0** | Right-dock panel framework in main window (host only, no behavior change). |
| **1** | `geometry_segments.py` + locked editing + Geometry panel (segments, per-point properties, edit mode, command-sequence findings). Tests: model, lock guard, point-propagation round-trip, interleaved/re-opened occurrence state. |
| **2** | `crossings.py` all-crossings detector (+z=0 semantics), line-too-long, start/end surfacing, check-dock integration, report rows. Scope-check the z=0 surface drape fix here. Tests: N-crossings completeness, bulges, z=0 classification. |
| **3** | Core tools: nudge, untwist, flip past, curve/recurve, **active-occurrence connect**, move end-to-end, add node/inject point, geometry-only join-average, COGO from lines, and **Shrink Line Commands** repair/compaction. Tests per tool incl. undo round-trips, command repairs, safe label reuse, and number-preservation. |
| **4** | Solvers: auto-nudge, square-shape dialog. Progressive migration of existing workbenches into panels. |
| Future | Line join reorder/renumber UI (discontinued for this effort by decision; `JoinLinesDraft` remains available). |

Workflow per push: implement on session branch → `pytest -q` green → PR →
CI (3.13/3.14) → merge → `Update and Run Plumbline.bat` pulls it on your end.

## 7. Still open (small, non-blocking)
- Default too-long threshold: starting at **300 ft / 100 m** unless you say otherwise.
- The working name **Shrink Line Commands** means both sequence repair and safe
  compaction of jumped-to instance suffixes (for example `SIDEWALK10`). Confirm
  the label is what you intended; its conservative, preview-first behavior is
  specified in §3.5.
