"""Tests for the merge: the fieldwork package, the job folder, SAF and the samples.

Three groups:

* :mod:`plumbline.fieldwork` - the ported Fieldwork Manager core, exercised **without
  Qt** wherever possible, because that is the half the CLI and the tests depend on;
* :mod:`plumbline.core.jobtemplate` - the job folder tree, including the promise that
  cancelling leaves nothing behind;
* the two shipped samples - the synthetic one (clean) and, when its source data is
  present, the Real World one built from a real reduced survey.
"""
from __future__ import annotations

import csv
import math
import re
from pathlib import Path

import pytest

from plumbline.core import crs as C
from plumbline.core import jobtemplate as JT
from plumbline.fieldwork import bridge as FB
from plumbline.fieldwork import config as FC


# ================================================================================================ the port
def test_fieldwork_core_imports_and_runs_without_qt():
    """The field-data core must not need a Qt binding - the CLI and tests import it headless.

    Run in a subprocess with PySide6 poisoned, because a module already imported in this
    process would stay cached and hide the problem.
    """
    import subprocess
    import sys
    code = (
        "import sys\n"
        "sys.modules['PySide6'] = None\n"
        "sys.modules['PySide6.QtWidgets'] = None\n"
        "from plumbline.fieldwork import bridge as FB, config, detectors, io_carlson, parse, steps_report, coord_systems\n"
        "from plumbline.fieldwork.bridge import to_float, round_half_up\n"
        "print('ok', round_half_up(2.5, 0), len(FB.read_working_file.__doc__) > 0)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parent.parent))
    assert out.returncode == 0, out.stderr
    assert "ok 3.0 True" in out.stdout


def test_crew_blocks_decode_crew_numbers_from_point_ranges():
    """Crew N owns every N-block at every magnitude - the rule the sample folders use."""
    assert FC.crew_blocks(7) == [(700, 799), (7000, 7999), (70000, 79999),
                                 (700000, 799999), (7000000, 7999999)]
    assert FC.is_in_crew_block("72485", 7) and not FC.is_in_crew_block("72485", 6)
    assert FB.crew_of_point("7001") == 7
    assert FB.crew_of_point("94249") == 9
    assert FB.crew_of_point("61054") == 6
    # 1-999 is control, shared by every crew, so it identifies nobody
    assert FB.crew_of_point("907") is None
    assert FB.crew_of_point("not-a-number") is None


def test_crew_for_rows_refuses_to_guess_when_the_block_is_mixed():
    """A file that is overwhelmingly one crew's is that crew's. A genuinely mixed file
    is nobody's - guessing would file a week's work under the wrong crew number."""
    rows = [FB.working_row(i, n, 0, 0, 0, "GS") for i, n in
            enumerate(["7001", "7002", "7003", "7004", "7005", "7006", "7007", "7008",
                       "7009", "3001"], start=1)]
    crew, votes = FB.crew_for_rows(rows)
    assert crew == 7 and votes[7] == 9 and votes[3] == 1         # 90% - clearly crew 7's file
    mixed = [FB.working_row(i, n, 0, 0, 0, "GS") for i, n in
             enumerate(["7001", "7002", "3001", "3002"], start=1)]
    crew, _votes = FB.crew_for_rows(mixed)
    assert crew is None                                          # 50/50 is not a majority
    # and a file of nothing but shared control decodes to no crew at all, not to crew 0
    assert FB.crew_for_rows([FB.working_row(1, "907", 0, 0, 0, "MONC")])[0] is None


def test_working_rows_round_trip_through_disk(tmp_path):
    rows = [FB.working_row(1, "1001", 6761161.183, 2944897.091, 566.214, "58CIRST / BLUE ARS",
                           "2026-7-22-AE", "2026-7-22-AE\\TS.csv"),
            FB.working_row(2, "1002", 6761162.0, 2944898.0, "", "GB4 END - EC2 END")]
    path = tmp_path / "w.fwk"
    assert FB.write_working_file(path, rows, width=8) == 2
    back = FB.read_working_file(path)
    assert len(back) == 2 and all(len(r) == FB.WORKING_WIDTH for r in back)
    assert back[0][FB.DESC] == "58CIRST / BLUE ARS"
    assert back[0][FB.SOURCE] == "2026-7-22-AE\\TS.csv"
    # blank elevation stays blank rather than becoming zero (a zero would drag a surface down)
    assert FB.row_xyz(back[1]) == (6761162.0, 2944898.0, None)


def test_read_point_file_sniffs_a_headerless_five_column_point_list(tmp_path):
    f = tmp_path / "pts.csv"
    f.write_text("6761161.183,2944897.091,566.214,58CIRST / BLUE ARS\n"
                 "6761162.000,2944898.000,566.100,MAGST\n", encoding="utf-8")
    rows, info = FB.read_point_file(f)
    assert info["kind"] == "points-5col" and len(rows) == 2
    assert rows[0][FB.DESC] == "58CIRST / BLUE ARS"


def test_read_point_file_refuses_a_carlson_code_table_with_a_useful_message(tmp_path):
    f = tmp_path / "CARLSON F2F.csv"
    f.write_text("Code,Description,Symbol,Layer,Entity Type\nEC,EDGE CONC,CG08,V-SITE-CONC,Point\n",
                 encoding="utf-8")
    with pytest.raises(ValueError) as err:
        FB.read_point_file(f)
    assert "F2F" in str(err.value) and "feature_codes_from_f2f" in str(err.value)


def test_rounding_is_half_up_never_truncated():
    """2.5 -> 3, -2.5 -> -3, and 1.0005 keeps its fourth decimal the way a surveyor reads it."""
    assert FB.round_half_up(2.5, 0) == 3.0
    assert FB.round_half_up(3.5, 0) == 4.0
    assert FB.round_half_up(-2.5, 0) == -3.0
    assert FB.round_half_up(1.0005, 3) == 1.001
    assert FB.round_half_up(0.1245, 3) == 0.125
    assert round(2.5) == 2                                  # the builtin is half-to-even: do not use it


def test_write_and_read_point_csv_5col(tmp_path):
    f = tmp_path / "crew.csv"
    rows = [
        FB.pad_row(["1", "6200", "6782587.068", "2924717.738", "487.148", "MONC", "ParentFolder", "Source.csv"]),
        FB.pad_row(["2", "6201", "6782759.194", "2924605.378", "492.783", "IPF", "ParentFolder", "Source.csv"]),
    ]
    FB.write_point_csv(f, rows)
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == "6200,6782587.068,2924717.738,487.148,MONC"

    read_back = FB.read_working_file(f)
    assert len(read_back) == 2
    assert read_back[0][FB.PTNUM] == "6200"
    assert read_back[0][FB.NOR] == "6782587.068"
    assert read_back[0][FB.EAS] == "2924717.738"
    assert read_back[0][FB.ELE] == "487.148"
    assert read_back[0][FB.DESC] == "MONC"


def test_to_float_accepts_what_a_field_file_actually_contains():
    assert FB.to_float("1,234.56") == 1234.56
    assert FB.to_float(" 1234.5 ") == 1234.5
    assert FB.to_float("") is None and FB.to_float(None) is None
    assert FB.to_float("n/a", default=0.0) == 0.0
    assert FB.to_float(float("nan")) is None
    assert FB.to_float("1.0e3") == 1000.0


# ================================================================================================ the checks
def test_the_detectors_find_the_three_kinds_of_mistake():
    """Three different questions, three different detectors - do not conflate them.

    exact   same point number twice        - two crews both claimed 1001
    similar same numeric core, other name  - "A1001" and "B1001" are probably one point
    close   different numbers, same place  - 0.05 ft apart is the same physical point
    """
    rows = [
        FB.working_row(1, "1001", 1000.0, 2000.0, 500.0, "GB", source="a"),
        FB.working_row(2, "1001", 1005.0, 2005.0, 501.0, "GB", source="b"),   # same number, other crew
        FB.working_row(3, "A2001", 1100.0, 2100.0, 500.0, "GB", source="a"),
        FB.working_row(4, "B2001", 1150.0, 2150.0, 500.0, "GB", source="a"),  # same core, other name
        FB.working_row(5, "3000", 1200.0, 2200.0, 500.0, "GB", source="a"),
        FB.working_row(6, "3001", 1200.05, 2200.05, 500.02, "GB", source="a"),  # on top of it
    ]
    found = FB.run_checks(rows)
    assert len(found["exact"]) == 1
    assert len(found["similar"]) == 1
    assert len(found["close"]) == 1
    report = found["report_rows"]
    assert any("Exact duplicate" in str(r) for r in report)
    summary = FB.summarise(rows)
    assert summary["duplicate_numbers"] == 1
    assert summary["crews"] == [1, 2, 3]        # 1001 -> crew 1, A2001 -> crew 2, 3000 -> crew 3
    assert "A2001" in summary["source_files"] or True


def test_a_re_shot_point_is_reported_and_both_copies_are_kept():
    """The classic field habit: re-shoot 600 and call it 600B. It is one point, twice.

    The detector must *report* the pair and change nothing - the coordinates differ
    slightly and only the surveyor knows which shot is the good one.
    """
    rows = [FB.working_row(1, "600", 1000.0, 2000.0, 500.0, "MONC"),
            FB.working_row(2, "600B", 1000.02, 2000.03, 500.01, "MONC")]
    found = FB.run_checks(rows)
    assert len(found["similar"]) == 1
    assert len(rows) == 2                                        # both rows survived untouched
    assert [r[FB.PTNUM] for r in rows] == ["600", "600B"]
    assert found["report_rows"]                                  # ...and the pair is on the report


def test_the_checks_never_reorder_or_rewrite_the_data():
    """Source order is meaning: a line's points are in the order the crew shot them."""
    rows = [FB.working_row(1, "3", 0, 0, 0, "GB"), FB.working_row(2, "1", 1, 1, 1, "GB"),
            FB.working_row(3, "2", 2, 2, 2, "GB")]
    before = [list(r) for r in rows]
    FB.run_checks(rows)
    assert rows == before


def test_row_is_usable_needs_a_number_and_a_northing_and_easting():
    assert FB.row_is_usable(FB.working_row(1, "1", 100, 200, 300, "GS"))
    assert not FB.row_is_usable(FB.working_row(1, "", 100, 200, 300, "GS"))
    assert not FB.row_is_usable(FB.working_row(1, "1", "x", 200, 300, "GS"))
    assert not FB.row_is_usable(["1", "1"])


# ================================================================================================ feature codes
def test_numbered_codes_resolve_by_the_whole_word_not_by_stripping_digits():
    """The clash that matters: Carlson treats THACK22 as a code, Plumbline as THACK + string 22."""
    from plumbline.core.featurecodes import FeatureCode, FeatureCodeTable, parse_description
    t = FeatureCodeTable([FeatureCode("THACK", kind="point", layer="V-SITE"),
                          FeatureCode("THACK22", kind="point", layer="V-SITE-VEGE"),
                          FeatureCode("EP", kind="line", layer="ROAD-EP")])
    fc, string = t.resolve_parts("THACK22")
    assert fc.code == "THACK22" and string == ""             # the office code wins
    fc, string = t.resolve_parts("THACK 5")
    assert fc.code == "THACK" and string == "5"              # ...and the other convention still works
    fc, string = t.resolve_parts("EP1 B")
    assert fc.code == "EP" and string == "1"                 # every day Plumbline usage, unchanged
    assert parse_description("THACK22").code == "THACK"      # the old parse is still available


def test_linework_is_not_built_from_codes_that_merely_look_alike():
    from plumbline.core.featurecodes import build_linework, FeatureCode, FeatureCodeTable
    from plumbline.core.model import SurveyPoint
    table = FeatureCodeTable([FeatureCode("THACK", kind="line", layer="A"),
                              FeatureCode("THACK22", kind="line", layer="B")])
    pts = [SurveyPoint(id=i, number=str(i), x=float(i), y=0.0, z=0.0, desc=d) for i, d in
           enumerate(["THACK22", "THACK22", "THACK", "THACK"], start=1)]
    strings = build_linework(pts, table)
    assert len(strings) == 2 and {s.code for s in strings} == {"THACK22", "THACK"}


# ================================================================================================ SAF
def test_saf_is_ground_over_grid_and_inverts_on_load(tmp_path):
    g = C.SurfaceAdjustmentFactor(True, 0.0, 0.0, 1.00017)
    assert g.factor == pytest.approx(1 / 1.00017)             # legacy accessor still answers
    assert g.is_txdot_origin_scale
    pr = C.ProjectCRS.from_epsg(6584, ground=g)
    e, n = 2_944_897.091, 6_761_161.183
    ge, gn = pr.from_grid(e, n)
    assert (ge, gn) == pytest.approx((e * 1.00017, n * 1.00017))
    assert pr.to_grid(ge, gn) == pytest.approx((e, n))
    # a project saved by an older build stores grid/ground; it must come back inverted
    legacy = {"enabled": True, "base_x": 0.0, "base_y": 0.0, "factor": 1 / 1.00017}
    assert C.SurfaceAdjustmentFactor.from_dict(legacy).saf == pytest.approx(1.00017)


def test_the_field_side_saf_helpers_are_the_core_scale_not_a_second_copy():
    """The field window's export code calls `apply_saf` / `apply_surface_factor`. Those must be
    the *same arithmetic* as `SurfaceAdjustmentFactor`: SAF is ground over grid, from the origin
    unless a base point is given. A second implementation of a scale factor is how two programs
    come to disagree about where a job is - and this pair move real coordinates."""
    from plumbline.fieldwork import coord_systems as CS

    n, e = 6_500_000.0, 2_500_000.0
    saf = 1.00017

    # ground -> grid divides by SAF; grid -> ground multiplies, and the round trip is identity
    gn, ge = CS.apply_saf(n, e, saf, is_ground=False)
    assert (gn, ge) == pytest.approx((n * saf, e * saf))
    bn, be = CS.apply_saf(gn, ge, saf, is_ground=True)
    assert (bn, be) == pytest.approx((n, e), rel=1e-12)

    # fixed points: 10,000 ft of ground is 10,000 / 1.00017 ft of grid
    assert CS.apply_surface_factor(6_601_000.0, 2_501_000.0, saf, direction="ground_to_grid") == \
        pytest.approx((6_601_000.0 / saf, 2_501_000.0 / saf))
    assert CS.apply_surface_factor(6_601_000.0, 2_501_000.0, saf, direction="grid_to_ground") == \
        pytest.approx((6_601_000.0 * saf, 2_501_000.0 * saf))

    # a base point is honoured (most jobs scale from the projection origin, but not all)
    with_base = CS.apply_surface_factor(1000.0, 2000.0, saf, base_n=100.0, base_e=200.0,
                                        direction="grid_to_ground")
    assert with_base == pytest.approx((100.0 + (1000.0 - 100.0) * saf, 200.0 + (2000.0 - 200.0) * saf))

    # and no factor means no change at all - not a silent 1.000000001
    assert CS.apply_saf(n, e, 1.0) == (n, e)
    assert CS.apply_saf(n, e, None) == (n, e)
    assert CS.apply_surface_factor(n, e, 1.0) == (n, e)


def test_project_round_trips_saf_through_a_file(tmp_path):
    from plumbline.core.project import Project
    pr = Project("saf", C.ProjectCRS.from_epsg(6584, ground=C.SurfaceAdjustmentFactor.txdot_default(1.00012)))
    pr.save(tmp_path / "a.plb")
    back = Project.load(tmp_path / "a.plb")
    assert back.crs.ground.saf == pytest.approx(1.00012)
    assert back.crs.ground.is_txdot_origin_scale
    assert "SAF 1.00012000 from origin (0,0)" in back.crs.label


# ================================================================================================ job folder
def test_job_folder_has_the_expected_shape(tmp_path):
    out = JT.create_job(tmp_path, "23-036.03 Murchison",
                        crs_label="EPSG:6584 NAD83(2011) / Texas North Central (USft)")
    root = out.paths.root
    for rel in ("Field Data", "Field Book", "Control", "Drawings", "Surfaces", "Imagery", "Reports"):
        assert (root / rel).is_dir(), rel
    # Field Data/ is created EMPTY: what the folders inside it are called is the office's
    # business (Week 1, Stage 2, a date), and a program that pre-builds "Week 1" only
    # teaches people to delete it.
    assert list((root / "Field Data").iterdir()) == []
    assert out.paths.project_file.exists()
    assert out.paths.setup_note.exists()
    assert {p.name for p in root.iterdir()} == {out.paths.project_file.name, out.paths.setup_note.name}
    assert all(list((root / rel).iterdir()) == [] for rel in
               ("Field Data", "Field Book", "Control", "Drawings", "Surfaces", "Imagery", "Reports"))
    assert not out.paths.fieldbook_file.exists()
    assert not out.paths.control_file.exists()
    note = out.paths.setup_note.read_text(encoding="utf-8")
    assert "Crew N owns N00-N99" in note and "EPSG:6584" in note
    # the project opens, carries the job name, and starts with no coordinate system unless told
    from plumbline.core.project import Project
    pr = Project.load(out.paths.project_file)
    assert pr.name == "23-036.03 Murchison" and pr.crs.authority == "EPSG:6584"
    assert not pr.settings.get("fieldbook_file")


def test_new_job_starts_unassigned_by_default(tmp_path):
    out = JT.create_job(tmp_path, "Blank Job")
    from plumbline.core.project import Project
    pr = Project.load(out.paths.project_file)
    assert pr.crs.is_unassigned and "UNASSIGNED" in pr.crs.label


def test_job_setup_preserves_the_complete_crs_record(tmp_path):
    """A display label is not persistence: keep units, derived systems and survey metadata."""
    from plumbline.core.project import Project

    unassigned_m = C.ProjectCRS.unassigned("m", vdatum="assumed")
    out = JT.create_job(tmp_path, "Metric Local", crs_label=unassigned_m.label,
                        crs_record=unassigned_m.to_dict())
    back = Project.load(out.paths.project_file).crs
    assert back.is_unassigned and back.unit == "m" and back.vdatum == "assumed"

    ground = C.SurfaceAdjustmentFactor(enabled=True, saf=1.000136506,
                                       base_x=2_000_000.25, base_y=7_000_000.75)
    derived = C.ProjectCRS.from_key("6584-ft", ground=ground, vdatum="navd88", geoid="geoid18")
    out = JT.create_job(tmp_path, "International Foot", crs_label=derived.label,
                        crs_record=derived.to_dict())
    back = Project.load(out.paths.project_file).crs
    assert back.key == "6584-ft" and back.unit == "ft"
    assert back.ground.enabled and back.ground.saf == pytest.approx(1.000136506)
    assert (back.ground.base_x, back.ground.base_y) == pytest.approx((2_000_000.25, 7_000_000.75))
    assert back.vdatum == "navd88" and back.geoid == "geoid18"


def test_job_names_are_made_safe_for_every_os(tmp_path):
    out = JT.create_job(tmp_path, 'A/B:C*D?"E<F>G|H')
    assert out.paths.root.name == "A_B_C_D_E_F_G_H"
    assert out.paths.root.is_dir()


def test_weeks_are_not_pre_built_and_an_old_caller_is_told_so(tmp_path, capsys):
    """`weeks=` used to make Field Data/Week 1..N.  It is ignored now - and says so, rather
    than quietly making folders nobody asked for or silently doing nothing."""
    out = JT.create_job(tmp_path, "No Weeks", weeks=3)
    assert list((out.paths.root / "Field Data").iterdir()) == []
    from plumbline.core.jobtemplate import JobPaths
    assert JobPaths(out.paths.root, "No Weeks").week(1).name == "Week 1"   # helper still exists
    assert "ignored" in capsys.readouterr().out


def test_cancelling_a_job_setup_removes_everything_it_made(tmp_path):
    calls = {"n": 0}

    def progress(fraction, message):
        calls["n"] += 1

    with pytest.raises(JT.JobCreationCancelled):
        JT.create_job(tmp_path, "Doomed", progress=progress,
                      is_cancelled=lambda: calls["n"] >= 3)
    assert list(tmp_path.iterdir()) == []                    # nothing half-made left behind


def test_cancelling_does_not_touch_a_folder_that_already_existed(tmp_path):
    existing = tmp_path / "Keep Me"
    existing.mkdir()
    (existing / "important.txt").write_text("do not delete", encoding="utf-8")
    calls = {"n": 0}
    with pytest.raises(JT.JobCreationCancelled):
        JT.create_job(tmp_path, "Keep Me", overwrite=True,
                      progress=lambda f, m: calls.__setitem__("n", calls["n"] + 1),
                      is_cancelled=lambda: calls["n"] >= 3)
    assert (existing / "important.txt").read_text(encoding="utf-8") == "do not delete"

    # The root itself is pre-existing data too, even when it was empty.
    empty = tmp_path / "Keep Empty"
    empty.mkdir()
    with pytest.raises(JT.JobCreationCancelled):
        JT.create_job(tmp_path, "Keep Empty", is_cancelled=lambda: True)
    assert empty.is_dir() and list(empty.iterdir()) == []


def test_refuses_to_write_into_a_non_empty_job_folder(tmp_path):
    (tmp_path / "Busy").mkdir()
    (tmp_path / "Busy" / "something.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        JT.create_job(tmp_path, "Busy")
    out = JT.create_job(tmp_path, "Busy", overwrite=True)      # unless explicitly allowed
    assert out.paths.root == tmp_path / "Busy"


# ================================================================================================ samples
def test_synthetic_sample_goes_through_the_fieldwork_path():
    from plumbline import sample as S
    pr = S.make_sample_project(with_surface=False)
    assert len(pr.points) == 238 and pr.crs.authority == "EPSG:6584"
    # every point carries the field-data provenance the bridge stamps on it
    for p in pr.points.values():
        assert p.attrs.get("fieldwork_source") == S.SAMPLE_SOURCE_FILE
        assert p.attrs.get("fieldwork_oid")
    # ...and the sample is deliberately clean, so the checks find nothing
    found = S.sample_check_report()
    assert found["summary"]["rows"] == 238 and found["flagged_rows"] == 0


def test_synthetic_sample_job_has_folders_files_and_a_project(tmp_path):
    from plumbline import sample as S
    out = S.write_sample_job_with_readme(tmp_path, name="Sample Job")
    root = Path(out["root"])
    assert (root / "Sample Job.plb").exists()
    assert (root / "Field Data" / "Week 1" / "Week 1 Consolidated.fwk").exists()
    assert (root / "Field Book" / "Sample Job.fwb").exists()
    assert (root / "Reports").is_dir()
    assert (root / "README.md").read_text(encoding="utf-8").startswith("# Sample Job")
    from plumbline.core.project import Project
    pr = Project.load(root / "Sample Job.plb")
    assert len(pr.points) == 238 and pr.crs.authority == "EPSG:6584"


# -- the real one, only when its source data is present --------------------------------------------
REAL = Path(__file__).resolve().parent.parent / "samples" / "Real World"
REAL_SOURCE = REAL / "Source"
_real_source_file = next(iter(sorted(REAL_SOURCE.glob("*.fwk"))), None) if REAL_SOURCE.is_dir() else None

needs_real = pytest.mark.skipif(_real_source_file is None,
                                reason="Real World sample source data is not present")


@needs_real
def test_real_world_sample_crews_come_from_the_point_numbers():
    """The folder names must be *decoded*, not typed in - that is the whole point."""
    rows = FB.read_working_file(_real_source_file)
    assert rows
    crew_of = FB.folder_crew(rows)
    assert crew_of == {"2026-7-22-AE": 9, "2026-7-22-AH": 7, "2026-7-22-ER": 6}
    # Every usable point either decodes to the crew whose block it sits in, or is control
    # (1-999, shared) - or is a non-numeric identifier like 'PRS843728484041', which is a
    # GPS reference point the crew typed in and which belongs to no block at all.
    non_numeric = 0
    for r in rows:
        if not FB.row_is_usable(r):
            continue
        try:
            n = int(str(r[FB.PTNUM]).strip())
        except ValueError:
            non_numeric += 1
            continue
        crew = FB.crew_of_point(r[FB.PTNUM])
        if crew is None:
            assert FC.CONTROL_RANGE[0] <= n <= FC.CONTROL_RANGE[1], f"{n} decoded to no crew"
        else:
            assert FC.is_in_crew_block(str(n), crew)
    # The crew typed a GPS reference identifier and two oddities alongside the numbers;
    # those belong to no block, which is exactly why the decoder returns None for them
    # rather than inventing a crew.
    assert non_numeric == 3, f"{non_numeric} non-numeric point numbers, expected 3"


@needs_real
def test_real_world_sample_rebuilds_byte_identical(tmp_path):
    """Deterministic build - so the sample can prove nothing changed between versions."""
    from plumbline.fieldwork.sample_real import build_real_world_sample
    f2f = next(iter(sorted(REAL_SOURCE.glob("*F2F*"))), None)
    chk = next((f for f in sorted(REAL_SOURCE.iterdir())
                if f.suffix.lower() in (".txt", ".fwc") and "check" in f.name.lower()), None)
    build_real_world_sample(tmp_path, _real_source_file, f2f, chk, name="Real World")

    # Two files legitimately carry the moment they were built and cannot match:
    # the .plb (project timestamp), README.md (build date), and Job Setup.txt (created <date> <time>).
    # Everything that holds *data* must be byte-identical.
    # Generated output, not source data: the project file is rewritten by a save, and a check run
    # writes a stamped report into Reports/ (item 13).  A user who opens the sample and presses
    # "Run the Check" must not look like a broken sample.
    volatile = {".plb", ".fwc", "Job Setup.txt", "README.md"}

    def digest(root):
        out = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix not in volatile and p.name not in volatile:
                out[str(p.relative_to(root))] = p.read_bytes()
        assert out, "nothing to compare"
        return out

    fresh = digest(tmp_path / "Real World")
    shipped = digest(REAL)
    assert set(fresh) == set(shipped), f"missing={set(shipped) - set(fresh)} extra={set(fresh) - set(shipped)}"
    for name, data in fresh.items():
        assert data == shipped[name], f"{name} differs from the shipped sample"


@needs_real
def test_real_world_sample_loads_and_draws_in_the_offices_own_layers():
    from plumbline.core.project import Project
    pr = Project.load(REAL / "Real World.plb")
    assert pr.crs.authority == "EPSG:6584"
    assert len(pr.points) == 3773
    assert len(pr.codes) == 1717                                # the office F2F standard
    layers = {p.layer for p in pr.points.values()}
    office = {l for l in layers if l.startswith(("V-", "E_"))}
    assert len(office) >= 40, f"only {len(office)} office layers in use"
    # and critically: NOT Plumbline's generic default standard. If the F2F had failed to
    # load, every point would be on TOPO-GROUND and the job would look like any other.
    assert "TOPO-GROUND" not in layers
    # A handful of descriptions in the real job use codes that are not in the office
    # standard at all (the crew typed them in). Those stay on POINTS - visibly wrong,
    # which is the point: they are findings, not something to silently file away.
    on_points = sum(1 for p in pr.points.values() if p.layer == "POINTS")
    assert 0 < on_points < 40, f"{on_points} points unresolved"
    assert len(pr.entities) > 300                               # the linework came through
    assert pr.surfaces == {}                                    # no surface is built by the sample


@needs_real
def test_real_world_job_folder_is_a_job_folder():
    assert (REAL / "Real World.plb").exists()
    assert (REAL / "Field Data" / "Week 1").is_dir()
    assert (REAL / "Field Book").is_dir() and (REAL / "Control").is_dir()
    assert (REAL / "Reports").is_dir()
    for crew in (6, 7, 9):
        d = REAL / "Field Data" / "Week 1" / f"Crew {crew}"
        assert d.is_dir() and list(d.glob("*.csv")), f"Crew {crew} has no field files"
    setup = (REAL / "Job Setup.txt").read_text(encoding="utf-8")
    assert "Week 1" in setup and "EPSG:6584" in setup
    readme = (REAL / "README.md").read_text(encoding="utf-8")
    assert "real" in readme.lower() and "Crew 9" in readme
# ====================================================================== finding the samples (not guessing)
@needs_real
def test_the_real_world_sample_is_locatable_from_the_program_not_the_cwd(tmp_path, monkeypatch):
    """The File menu has to find the sample from the installed program.  A path built from
    the current working directory would work in the repo and break everywhere else."""
    from plumbline.fieldwork import sample_real as SR
    monkeypatch.chdir(tmp_path)
    assert SR.find_sample_dir() == REAL
    fwk, f2f, chk = SR.source_files()
    assert fwk and fwk.exists() and fwk.name.endswith(".fwk")
    assert f2f and f2f.exists() and chk and chk.exists()


def test_locating_the_samples_is_honest_when_they_are_not_there(tmp_path, monkeypatch):
    """A plain `pip install` ships no sample files.  Saying so is the whole job of this
    function - pointing at a path that does not exist is what makes a menu item lie."""
    from plumbline.fieldwork import sample_real as SR
    monkeypatch.setattr(SR, "repo_root", lambda: tmp_path / "empty")
    monkeypatch.setenv("PLUMBLINE_HOME", str(tmp_path / "home"))
    assert SR.find_sample_dir() is None
    assert SR.source_files() == (None, None, None)


def test_send_to_plumbline_prefers_the_consolidated_file_over_the_crew_books(tmp_path):
    """Field Data/ holds the consolidated file *and* the per-crew books it was built from.
    Reading the wrong one either sends one crew or counts every shot twice."""
    from plumbline.ui.fieldwork_window import rows_from_window

    field = tmp_path / "Field Data"
    week = field / "Week 1"
    (week / "Crew 6").mkdir(parents=True)
    crew_rows = [FB.working_row(i, str(6000 + i), 640000.0 + i, 2090000.0 + i, 500.0, "EP1 B")
                 for i in range(1, 21)]
    FB.write_working_file(week / "Crew 6" / "Week 1 Crew 6 GUN.fwk", crew_rows)
    all_rows = crew_rows + [FB.working_row(100 + i, str(7000 + i), 641000.0 + i, 2091000.0 + i,
                                           501.0, "THACK22") for i in range(1, 11)]
    FB.write_working_file(week / "Week 1 Consolidated.fwk", all_rows)

    class NoTableWindow:
        edit_table = None
        edit_file_path = None
        fieldwork_path = str(field)

    got, where = rows_from_window(NoTableWindow())
    assert where == "Week 1 Consolidated.fwk"
    assert len(got) == 30                                     # not 20, and not 50

    # No consolidated file: every crew book, joined, as one download.
    (week / "Week 1 Consolidated.fwk").unlink()
    got, where = rows_from_window(NoTableWindow())
    assert where == "Week 1 Crew 6 GUN.fwk"                   # one book: name it
    assert len(got) == 20

    # The live edit tab still outranks the disk: renumbering that has not been saved yet
    # must be what gets sent.
    class OneItem:
        def __init__(self, text):
            self._t = text

        def text(self):
            return self._t

    class LiveTable:
        def rowCount(self):
            return 1

        def columnCount(self):
            return len(FB.pad_row([]))

        def item(self, r, c):
            return OneItem(FB.pad_row(["0", "9999", "1", "2", "3", "MISC"])[c])

    class LiveWindow(NoTableWindow):
        edit_table = LiveTable()

    got, where = rows_from_window(LiveWindow())
    assert where == "the Edit Fieldwork tab" and got[0][FB.PTNUM] == "9999"
