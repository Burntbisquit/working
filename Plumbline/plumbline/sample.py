"""Synthetic sample site (a small commercial lot with a street) used by the tutorial, screenshots and tests.

Everything here is computer-generated - it is NOT a real survey.  Coordinates sit in NAD83(2011) / Texas
North Central (US survey feet, EPSG:6584) near Mesquite, TX so the CRS tools and imagery have a plausible
place to land; change the project CRS (Survey > Project Coordinate System > Assign) to move it anywhere.

The sample is built the long way round on purpose: the points are written out as a field download, then run
through the same Fieldwork Manager code path a real job takes (read -> check -> import -> code -> linework).
So `python -m plumbline sample` is an end-to-end test of the merged program, not just a pretty picture.

The synthetic site uses Carlson Field-to-Finish command meanings and representative codes from the
shipped office F2F reference: ``EA ST``, ``FNC X``, ``T14 / OAK``, and ``NG``. This keeps the tutorial
project synthetic while exercising the same start/end/close and description conventions as a field book.
The untouched real-world source remains a separate, real reduced survey in ``samples/Real World``.

For real field data, see :func:`plumbline.fieldwork.sample_real.build_real_world_sample`.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from .core.crs import ProjectCRS
from .core.model import ImportBatch, SurveyPoint
from .core.project import Project

E0, N0 = 2552600.0, 6967000.0          # south-west corner of the site

#: The coordinate system the sample is published in.  NAD83(2011) Texas North Central in US survey
#: feet - the current realisation of the zone, not the superseded 2276.  The two are the same
#: projection to within a datum shift, so every number in this file is valid in either.
SAMPLE_EPSG = 6584
SAMPLE_CRS_LABEL = "EPSG:6584 NAD83(2011) / Texas North Central (USft)"

#: What the sample's download would have been called if a crew had shot it.
SAMPLE_CREW_FOLDER = "2026-07-19-S1"
SAMPLE_SOURCE_FILE = SAMPLE_CREW_FOLDER + "\\2026-07-19GPS S1.csv"


def swale_y(x):
    """North coordinate of the drainage-swale flowline at local easting x."""
    return 300.0 - 0.12 * np.asarray(x, float)


def terrain(x, y):
    """Ground elevation (ftUS-ish) for local coordinates x east, y north of the SW corner."""
    base = 512.0 - 0.012 * x + 0.020 * y
    hill = 5.0 * np.exp(-(((x - 430) / 130.0) ** 2 + ((y - 140) / 130.0) ** 2))
    swale = -1.5 * np.exp(-(((y - swale_y(x)) / 75.0) ** 2))
    return base + hill + swale


def _arc(cx, cy, r, a0, a1, n=7):
    """Points along an arc (degrees, CCW positive), excluding the first point."""
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in np.linspace(a0, a1, n)[1:]]


def sample_points(seed: int = 11) -> list[tuple]:
    """Synthetic PNEZD points encoded with Carlson-style Field-to-Finish descriptions."""
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    T = terrain

    def add(x, y, dz, desc):
        rows.append((float(x), float(y), float(T(x, y) + dz), desc))

    R = 30.0
    # Main street edges, centerline, and side street.  Command semantics follow the
    # Carlson Field Book defaults: Start Line, End Line, with each segment explicit.
    xs = list(range(0, 621, 40))
    for i, x in enumerate(xs):
        add(x, 28, -0.15, "EA ST" if i == 0 else "EA END" if i == len(xs) - 1 else "EA")
    north = [(x, 52) for x in range(0, 519, 40)] + [(518, 52)]
    north = [pt for k, pt in enumerate(north) if k == 0 or pt != north[k - 1]]
    for i, (x, y) in enumerate(north):
        add(x, y, -0.15, "EA ST" if i == 0 else "EA")
    for x, y in _arc(518, 82, R, -90, 0):
        add(x, y, -0.15, "EA")
    for y in range(120, 341, 40):
        add(548, y, -0.15, "EA")
    rows[-1] = (*rows[-1][:3], "EA END")
    for i, y in enumerate(range(340, 81, -40)):
        add(572, y, -0.15, "EA ST" if i == 0 else "EA")
    add(572, 82, -0.15, "EA")
    for x, y in _arc(602, 82, R, 180, 270):
        add(x, y, -0.15, "EA")
    add(620, 52, -0.15, "EA END")

    for i, x in enumerate(range(0, 621, 40)):
        add(x, 40, 0.05, "CL ST" if i == 0 else "CL END" if x == 620 else "CL")
    for i, y in enumerate(range(80, 341, 52)):
        add(560, y, 0.05, "CL ST" if i == 0 else "CL")
    rows[-1] = (*rows[-1][:3], "CL END")

    # Sidewalk and closed site features.
    for i, x in enumerate(range(10, 539, 66)):
        add(x, 60, -0.05, "SW ST" if i == 0 else "SW")
    rows[-1] = (*rows[-1][:3], "SW END")

    for i, (x, y) in enumerate(((0, 62), (540, 62), (540, 345), (0, 345))):
        add(x, y, 0.0, "FNC ST" if i == 0 else "FNC X" if i == 3 else "FNC")
    for i, x in enumerate(range(0, 541, 90)):
        add(x, 343, 0.0, "FNC ST" if i == 0 else "FNC END" if x == 540 else "FNC")

    # Building and parking geometry.
    bx, by, bw, bh = 150.0, 170.0, 90.0, 54.0
    ff = float(T(bx + bw / 2, by + bh / 2)) + 2.0
    building_corners = ((bx, by), (bx + bw, by), (bx + bw, by + bh), (bx, by + bh))
    for i, (x, y) in enumerate(building_corners):
        desc = "BLDG ST" if i == 0 else "BLDG X" if i == len(building_corners) - 1 else "BLDG"
        rows.append((x, y, round(ff - 0.35, 2), desc))
    rows.append((bx + bw / 2, by + bh / 2, round(ff, 2), "FFE"))
    for i, (x, y) in enumerate(((120, 100), (280, 100), (280, 150), (120, 150))):
        add(x, y, -0.05, "EA ST" if i == 0 else "EA X" if i == 3 else "EA")
    add(195, 152, 0.1, "SW ST")
    add(195, 100, 0.05, "SW END")

    # Point features use office-standard codes; a slash introduces a note, not another code.
    add(95, 46, -0.12, "MH / WATER")
    add(430, 46, -0.10, "SSMH")
    add(300, 57, 0.10, "FH")
    add(60, 58, 0.05, "LP")
    add(350, 58, 0.05, "LP")
    add(255, 66, 0.0, "SN")
    for x, y, diameter in ((330, 300, 14), (360, 120, 18), (90, 240, 22), (470, 230, 16), (40, 320, 12)):
        add(x, y, 0.0, f"T{diameter} / OAK")

    # Drainage flowline (a surface breakline).
    for i, x in enumerate(range(30, 531, 50)):
        add(x, float(swale_y(x)), 0.0, "FL ST" if i == 0 else "FL END" if x == 530 else "FL")

    # Natural-ground shots on a loose grid, skipping the building, parking, and street.
    for x in np.arange(14, 540, 38):
        for y in np.arange(74, 336, 36):
            if bx - 14 < x < bx + bw + 14 and by - 14 < y < by + bh + 14:
                continue
            if 108 < x < 292 and 88 < y < 162:
                continue
            xx, yy = x + rng.uniform(-9, 9), y + rng.uniform(-9, 9)
            rows.append((float(xx), float(yy), float(T(xx, yy) + rng.normal(0, 0.04)), "NG"))
    for x in range(20, 600, 60):
        add(x + rng.uniform(-8, 8), 14 + rng.uniform(-3, 3), rng.normal(0, 0.04), "NG")
    return rows


def sample_fieldbook_codes():
    """A small synthetic code table based on the Carlson F2F office reference."""
    from .core.featurecodes import FeatureCode, FeatureCodeTable

    definitions = [
        ("EA", "Edge of asphalt", "line", "V-SITE-ASPHALT", True, True),
        ("CL", "Centerline", "line", "V-SITE-CENTERLINE", True, True),
        ("SW", "Sidewalk", "line", "V-SITE-WALK", True, True),
        ("FNC", "Fence", "line", "V-SITE-FENCE", False, False),
        ("BLDG", "Building corner", "polygon", "V-BLDG", False, False),
        ("FFE", "Finished floor elevation", "point", "V-BLDG-FFE", False, False),
        ("MH", "Manhole", "point", "E-UTILITY-MH", False, False),
        ("SSMH", "Sanitary sewer manhole", "point", "E-UTILITY-SSMH", False, False),
        ("FH", "Fire hydrant", "point", "E-UTILITY-FH", False, False),
        ("LP", "Light pole", "point", "E-UTILITY-LP", False, False),
        ("SN", "Sign", "point", "V-ROAD-SIGN", False, False),
        ("FL", "Flowline", "line", "V-SITE-FLOWLINE", True, True),
        ("NG", "Natural ground shot", "point", "V-SITE-GROUND", False, True),
        ("T12", 'Tree 12"', "point", "V-SITE-TREES", False, False),
        ("T14", 'Tree 14"', "point", "V-SITE-TREES", False, False),
        ("T16", 'Tree 16"', "point", "V-SITE-TREES", False, False),
        ("T18", 'Tree 18"', "point", "V-SITE-TREES", False, False),
        ("T22", 'Tree 22"', "point", "V-SITE-TREES", False, False),
    ]
    return FeatureCodeTable([
        FeatureCode(code, name, kind, layer, breakline=breakline, ground=ground)
        for code, name, kind, layer, breakline, ground in definitions
    ])


def sample_field_rows(seed: int = 11) -> list[list[str]]:
    """The synthetic site as a field download - 15-wide working rows.

    This is the sample's own data wearing the field-data program's clothes: point
    number, N, E, Z, description, and the file it "came from".  Going through this
    representation (rather than adding points to the project directly) is what makes
    `plumbline sample` exercise the fieldwork reader, the duplicate detectors and the
    feature-code pass - if any of them breaks, the sample breaks.
    """
    from .fieldwork.bridge import working_row
    rows = []
    for i, (x, y, z, d) in enumerate(sample_points(seed), start=1):
        rows.append(working_row(i, str(i), f"{N0 + y:.3f}", f"{E0 + x:.3f}", f"{z:.2f}",
                                d, SAMPLE_CREW_FOLDER, SAMPLE_SOURCE_FILE))
    return rows


def sample_bust_row(rows) -> int | None:
    """Index of the one natural-ground shot given a deliberate +4.9 ft elevation error.

    The Data Quality Check tool needs something to find, and a surveyor's eye goes
    straight to a shot that is 4.9 ft above its neighbours.
    """
    ground_shots = [i for i, r in enumerate(rows) if r[5] == "NG"]
    return ground_shots[len(ground_shots) // 2] if ground_shots else None


def make_sample_project(with_surface: bool = True, plant_bust: bool = True,
                        through_fieldwork: bool = True) -> Project:
    """Build the sample project.

    With ``through_fieldwork`` (the default) the points arrive through the merged
    Fieldwork Manager path, so the sample is proof the two halves fit together.
    Pass False to bypass it - useful when debugging the drawing half on its own.
    """
    from .fieldwork import bridge as fb

    pr = Project("Sample site (synthetic)",
                 ProjectCRS.from_epsg(SAMPLE_EPSG, vunit="ftUS", vdatum="NAVD88 (assumed)"))
    pr.codes = sample_fieldbook_codes()
    rows = sample_field_rows()
    if plant_bust:
        i = sample_bust_row(rows)
        if i is not None:
            rows[i][4] = f"{float(rows[i][4]) + 4.9:.2f}"      # a fat-finger elevation

    if through_fieldwork:
        fb.apply_rows_to_project(pr, rows, dup_policy="renumber")
    else:
        for r in rows:
            x, y, z = fb.row_xyz(r)
            pr.add_point(x, y, round(z, 2), number=r[fb.PTNUM], desc=r[fb.DESC])
        pr.apply_codes_to_points()
    pr.process_linework()

    if with_surface:
        from .core.surface import build_surface_from_project, generate_contours
        sf, rep = build_surface_from_project(pr, "Existing ground", {"ground_only": True, "use_breakline_kind": True,
                                                                     "max_edge": 160.0})
        pr.add_surface(sf)
    pr.notes = ("Synthetic sample: terrain = plane + hill + swale. Point numbers near the middle of the NG shots "
                "include one deliberate +4.9 ft bust for the Data QA tool. Carlson-style Field Book commands and code meanings are used.\n"
                f"Coordinate system {SAMPLE_CRS_LABEL}. The points went in through the Fieldwork Manager "
                "import path (Survey > Fieldwork Manager), so this project also demonstrates the field-data side.")
    return pr


def sample_check_report(rows=None) -> dict:
    """Run the field-data checks over the synthetic sample and report what they find.

    The same three detectors the Fieldwork Manager window runs: identical point
    numbers, numbers that look like each other, and points that sit on top of each
    other.  The deliberate +4.9 ft bust should show up under the elevation check.
    """
    from .fieldwork import bridge as fb
    rows = rows if rows is not None else sample_field_rows()
    found = fb.run_checks(rows)
    return {"summary": fb.summarise(rows), "exact": found["exact"], "similar": found["similar"],
            "close": found["close"], "flagged_rows": found["flagged_rows"],
            "report_rows": found["report_rows"]}


def write_sample_files(folder) -> dict:
    """Write the sample as importable files (CSV points, DXF, LandXML, GeoJSON, KML) plus a project file."""
    from .io import dxf_io, gis_io, kml_io, landxml
    from .core.crs import make_transform

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    pr = make_sample_project()
    out = {}
    # PNEZD text, the format every data collector can make
    f = folder / "sample_points_PNEZD.csv"
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for p in pr.points.values():
            w.writerow([p.number, f"{p.y:.3f}", f"{p.x:.3f}", f"{p.z:.2f}", p.desc])
    out["csv"] = f
    # a second, header-ful, tab-separated file in X,Y,Z order (to exercise column mapping)
    f = folder / "sample_points_XYZ_tab.txt"
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["PointID", "X", "Y", "Elev", "Code"])
        for p in pr.points.values():
            w.writerow([p.number, f"{p.x:.3f}", f"{p.y:.3f}", f"{p.z:.2f}", p.desc])
    out["tsv"] = f
    f = folder / "sample_site.dxf"
    dxf_io.write_dxf(f, pr, dxf_io.DxfOptions(text_height=3.0))
    out["dxf"] = f
    f = folder / "sample_site.xml"
    landxml.write_landxml(f, pr)
    out["landxml"] = f
    f = folder / "sample_site.gpkg"
    gis_io.write_gis(f, pr, driver="GPKG", crs=f"EPSG:{SAMPLE_EPSG}")
    out["gpkg"] = f
    tf = pr.crs.transform_to(4326)
    f = folder / "sample_site.kmz"
    kml_io.write_kml(f, pr, lambda x, y: tf(x, y))
    out["kmz"] = f
    f = folder / "sample_site.plb"
    pr.save(f)
    out["project"] = f
    return out


def write_sample_job(root, name: str = "Sample Job", overwrite: bool = True,
                     progress=None, is_cancelled=None) -> dict:
    """Write the synthetic sample as a complete job folder (the New Project layout).

    Same data as :func:`write_sample_files`, arranged the way :mod:`plumbline.core.jobtemplate`
    says a job should be, with the points supplied as a field download so the
    fieldwork half of the program has something real to read:

        Sample Job/
            Sample Job.plb
            Field Data/Week 1/
                Week 1 Consolidated.fwk        the download, as it arrived
                2026-07-19-S1/                 one folder per crew (S1)
                    Week 1 S1 GPS.csv
            Field Book/Sample Job.fwb          the code table
            Reports/                           the check report the detectors produced
    """
    from .core import jobtemplate as JT
    from .fieldwork import bridge as fb
    from .fieldwork import io_carlson as IC

    def step(f, m):
        if is_cancelled is not None and is_cancelled():
            raise JT.JobCreationCancelled(m)
        if progress is not None:
            progress(f, m)

    step(0.05, "creating the job folder")
    creation = JT.create_job(root, name, template=JT.JOB_TEMPLATE,
                             crs_label=SAMPLE_CRS_LABEL, weeks=1, overwrite=overwrite,
                             starter_files=False, is_cancelled=is_cancelled)
    paths = creation.paths

    step(0.25, "writing the field download")
    rows = sample_field_rows()
    cons = paths.week(1) / "Week 1 Consolidated.fwk"
    fb.write_working_file(cons, rows, width=8)

    step(0.45, "splitting the crew folder")
    crew_dir = paths.week(1) / SAMPLE_CREW_FOLDER
    crew_dir.mkdir(parents=True, exist_ok=True)
    crew_file = crew_dir / "Week 1 S1 GPS.csv"
    fb.write_point_csv(crew_file, rows)

    step(0.60, "writing the field book")
    import csv as _csv
    with open(paths.fieldbook_file, "w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["Code", "Description", "Symbol", "Layer", "Entity Type", "Category"])
        for fc in make_sample_project(with_surface=False).codes:
            w.writerow([fc.code, fc.name, fc.symbol, fc.layer,
                        "Point" if fc.kind == "point" else "3D Polyline", "Site"])

    step(0.72, "running the checks")
    found = fb.run_checks(rows)
    # The synthetic site is deliberately *clean*: unique point numbers, no two shots on top
    # of each other.  So the interesting result is that the checks find nothing, and the
    # report says so rather than the file being mysteriously absent.
    if found["report_rows"]:
        report_path = paths.reports / "Sample Check Report.fwc"
        try:
            IC.write_unified_report(report_path, IC.build_unified_report_rows(
                rows, found["exact"], found["similar"], found["close"], {}))
        except Exception:
            report_path = paths.reports / "Sample Check Report.csv"
            with open(report_path, "w", encoding="utf-8-sig", newline="") as fh:
                w = _csv.writer(fh)
                w.writerow(["GroupID", "IssueType", "OID", "PointNumber", "Detail"])
                for r in found["report_rows"]:
                    w.writerow(r[:5])
    else:
        report_path = paths.reports / "Sample Check Report.txt"
        report_path.write_text(
            "Sample Check Report\n"
            "===================\n\n"
            f"{len(rows)} rows checked - 0 findings.\n\n"
            "This is correct. The synthetic sample site was generated with unique point\n"
            "numbers and no two shots within a foot of each other, so the duplicate checks\n"
            "have nothing to report. It is here to show a *passing* run.\n\n"
            "For a report with real findings in it, open the Real World sample - that job\n"
            "came off three crews on the same day and has genuine duplicate point numbers,\n"
            "which is what the Checks tab is for.\n", encoding="utf-8")

    step(0.86, "saving the project")
    pr = make_sample_project()
    pr.path = str(paths.project_file)
    pr.save(paths.project_file)

    step(1.0, "done")
    summary = fb.summarise(rows)
    return {"root": str(paths.root), "project": str(paths.project_file),
            "field_data": str(cons), "crew_folder": str(crew_dir),
            "check_report": str(report_path), "summary": summary,
            "duplicate_numbers": summary["duplicate_numbers"],
            "flagged_rows": found["flagged_rows"]}


def _sample_job_readme(result, rows) -> str:
    s = result["summary"]
    return f"""# Sample Job (synthetic)

A computer-generated commercial lot with a street, written out the way a field crew's
download would arrive. **Not a real survey** - terrain is a plane plus a hill plus a
drainage swale, and the point numbers run 1 to 238 in shooting order.

| | |
|---|---|
| Points | {s['usable']:,} |
| Rows in the download | {s['rows']:,} |
| Coordinate system | {SAMPLE_CRS_LABEL} |
| Duplicate point numbers | {result['duplicate_numbers']} |
| Check findings | {result['flagged_rows']} |

## Why there are no problems in here

The synthetic site is deliberately **clean**: every point number is unique and no two
shots land on top of each other. That makes it the right sample for learning the
drawing tools, and the wrong one for seeing the checks work.

The check report in `Reports/` therefore says "0 findings" and means it.

Descriptions use Carlson Field-to-Finish meanings represented by the shipped F2F reference:
`EA ST` / `EA END` for line boundaries, `FNC X` for a closed feature, `T14 / OAK` for a code plus
note, and `NG` for natural-ground shots. The project and its Field Book are synthetic.

For a job with real problems in it - three crews, one shared control, genuine duplicate
point numbers, descriptions that do not parse - open **Real World**, the other shipped
sample. That one is a real reduced survey.

## What is here

| Path | Contents |
|---|---|
| `Sample Job.plb` | the project - open this |
| `Field Data/Week 1/Week 1 Consolidated.fwk` | the download as one file |
| `Field Data/Week 1/{SAMPLE_CREW_FOLDER}/` | the same points as the crew's own file |
| `Field Book/Sample Job.fwb` | the code table this job was shot against |
| `Reports/` | the check report |

## Things to try

1. **Draw > Polyline**, then **Surface > Create Surface** and **Volumes**.
2. **Survey > Data Quality Check** - nothing to find here, but the tool runs.
3. **Survey > Project Coordinate System** and *Reproject* to EPSG:6583 (the metre
   version of the same zone) - the coordinates convert, the drawing does not move.
4. **Survey > Fieldwork Manager** and open `Field Data/Week 1/` to see the field side
   of the same points.
"""


def write_sample_job_with_readme(root, name: str = "Sample Job", **kw) -> dict:
    """`write_sample_job` plus a README that explains what the reader is looking at."""
    result = write_sample_job(root, name=name, **kw)
    from pathlib import Path as _P
    _P(result["root"], "README.md").write_text(
        _sample_job_readme(result, sample_field_rows()), encoding="utf-8")
    return result
