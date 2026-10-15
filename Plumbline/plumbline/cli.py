"""Command line entry points:  python -m plumbline [file]   |   run / info / export-dxf / sample / doctor / --version"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def _gui(path: str | None) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Plumbline")
    app.setApplicationVersion(__version__)
    # The licence comes before anything else: a user who declines should not reach a window that
    # has already opened their files.  Accepted once per agreement version (core/licence.py).
    from .ui.licence_dialog import ensure_accepted
    if not ensure_accepted():
        return 0
    win = MainWindow(open_path=path if (path and path.lower().endswith(".plb")) else None,
                     welcome=path is None)
    win.show()
    # External data (grids, geoid models, datum shifts) ships as flags, not files, and the first
    # run offers to fetch it - ticked by default, asked once (core/firstrun.py).
    from .ui.first_run import offer_on_first_run
    if offer_on_first_run(win):
        win.state.log("External data: the first-run list was offered (Settings > External data "
                      "sources lists every source and can fetch any of them later).", "info")
    if path and not path.lower().endswith(".plb"):
        win.importer.import_path(path)
    return app.exec()


def _load(path: str):
    from .core.project import Project
    return Project.load(path)


COMMANDS = ("run", "info", "export-dxf", "sample", "sample-job", "sample-real",
            "new", "fieldwork", "doctor", "licence", "sites", "external")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `plumbline job.plb` opens that file. (argparse cannot mix an optional positional with sub-commands, so a leading
    # argument that is neither a command nor an option is taken as the file.)
    file_arg = None
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        file_arg, argv = argv[0], argv[1:]
    ap = argparse.ArgumentParser(
        prog="plumbline", usage="plumbline [-h] [--version] [file | command ...]",
        description="Plumbline - planimetric & topographic CAD for survey data",
        epilog="With no command the program opens. Give it a .plb project to open that project, or any CSV / DXF / "
               "LandXML / GIS file to import it.")
    ap.add_argument("--version", action="version", version=f"Plumbline {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    r = sub.add_parser("run", help="run a Python script against a project without the GUI")
    r.add_argument("script")
    r.add_argument("project", nargs="?")
    r.add_argument("--save", help="save the project here afterwards")
    i = sub.add_parser("info", help="print a summary of a project")
    i.add_argument("project")
    d = sub.add_parser("export-dxf", help="export a project to DXF")
    d.add_argument("project")
    d.add_argument("out")
    d.add_argument("--text-height", type=float, default=None)
    s = sub.add_parser("sample", help="write the synthetic sample data (CSV, DXF, LandXML, GeoPackage, KMZ, project) to a folder")
    s.add_argument("folder", nargs="?", default="sample_data")
    sj = sub.add_parser("sample-job", help="write the synthetic sample as a full job folder (Field Data / Field Book / Control / Reports)")
    sj.add_argument("folder", nargs="?", default=".")
    sj.add_argument("--name", default="Sample Job")
    sr = sub.add_parser("sample-real", help="rebuild the Real World sample job from real field data")
    sr.add_argument("folder", nargs="?", help="where to put it (default: ./samples)")
    sr.add_argument("--source", help="the consolidated field file (.fwk) to build from")
    sr.add_argument("--f2f", help="the Carlson F2F code table")
    sr.add_argument("--check-report", help="the job's check report, if you have one")
    sr.add_argument("--name", default="Real World")
    sr.add_argument("--epsg", type=int, default=6584, help="coordinate system EPSG (0 = leave unassigned)")
    ex = sub.add_parser("external", help="the external data flags: --list, or --download what is missing")
    ex.add_argument("--list", action="store_true", help="what would be fetched, with sizes (the default)")
    ex.add_argument("--download", action="store_true", help="fetch the missing grids through PROJ")
    ex.add_argument("--yes", action="store_true", help="no questions (for a scripted install)")
    si = sub.add_parser("sites", help="list every external source this program pulls data from")
    si.add_argument("--json", action="store_true", help="as JSON, to diff between machines")
    lic = sub.add_parser("licence", help="the licence and end-user agreement: --show, or --write the shipped files")
    lic.add_argument("--write", metavar="FOLDER", nargs="?", const=".",
                     help="write LICENSE.md and EULA.md from the one copy in the program")
    lic.add_argument("--show", action="store_true", help="print the agreement")
    nw = sub.add_parser("new", help="create an empty job folder (folder tree, field book, control list, project)")
    nw.add_argument("name", help="job name, e.g. 23-036.03 Murchison")
    nw.add_argument("--folder", default=".", help="parent folder (default: current directory)")
    nw.add_argument("--weeks", type=int, default=None,
                    help="ignored (Field Data/ is created empty - name the folders inside it your way)")
    nw.add_argument("--epsg", type=int, default=0, help="coordinate system EPSG (0 = unassigned, the default)")
    fw = sub.add_parser("fieldwork", help="run the field-data checks over a .fwk and print the findings")
    fw.add_argument("file", help="consolidated field data (.fwk) or a point CSV")
    fw.add_argument("--f2f", help="Carlson F2F code table, to resolve descriptions")
    sub.add_parser("doctor", help="check that Python and every library Plumbline needs is installed and working")
    a = ap.parse_args(argv)
    if file_arg and a.cmd:
        ap.error(f"unexpected file {file_arg!r} before the {a.cmd!r} command")

    if a.cmd == "info":
        pr = _load(a.project)
        print(f"{pr.name}\n  CRS: {pr.crs.label}")
        for k, v in pr.summary().items():
            print(f"  {k}: {v}")
        ext = pr.extents()
        if ext:
            print(f"  extents: E {ext[0]:,.3f} .. {ext[2]:,.3f}   N {ext[1]:,.3f} .. {ext[3]:,.3f}")
        return 0
    if a.cmd == "run":
        from .core.project import Project
        from . import plugins
        pr = _load(a.project) if a.project else Project("Script")
        err = plugins.run_script(a.script, pr, None, log=print)
        if err:
            print(err, file=sys.stderr)
            return 1
        if a.save:
            pr.save(a.save)
            print(f"saved {a.save}")
        return 0
    if a.cmd == "export-dxf":
        from .io import dxf_io
        pr = _load(a.project)
        o = dxf_io.DxfOptions(text_height=a.text_height or float(pr.settings.get("text_height", 2.0)))
        st = dxf_io.write_dxf(a.out, pr, o)
        print(f"wrote {a.out}: {st}")
        return 0
    if a.cmd == "doctor":
        from . import doctor
        return doctor.run()
    if a.cmd == "external":
        from .core import firstrun as FR
        items = FR.plan()
        missing = [i for i in items if i.mb > 0 and not FR.grid_present(i.key)]
        if a.download:
            if missing and not a.yes:
                ans = input(f"Fetch {len(missing)} item(s), about {FR.total_mb(missing):.0f} MB? [y/N] ")
                if ans.strip().lower() not in ("y", "yes"):
                    print("nothing fetched")
                    return 1
            out = FR.download(missing, progress=lambda f, m: print(f"  [{f:4.0%}] {m}"))
            print(f"downloaded {len(out['done'])}, already present {len(out['skipped'])}, "
                  f"failed {len(out['failed'])}")
            for key, why in out["failed"]:
                print(f"  {key}: {why}")
            FR.mark_asked("offered")
            return 0
        print("External data (nothing ships inside the program - these are fetched on demand)")
        for i in items:
            state = "present" if FR.grid_present(i.key) else ("no download needed" if i.mb <= 0 else "to fetch")
            print(f"  [{state:>16}] {i.title}  ({i.size})")
            print(f"                     {i.why}")
        return 0
    if a.cmd == "sites":
        import json as _json
        from .core import registry as REG
        if a.json:
            print(_json.dumps(REG.as_rows(), indent=2))
        else:
            print("\n".join(REG.report_lines()))
        return 0
    if a.cmd == "licence":
        from .core import licence as LIC
        if a.show or a.write is None:
            print(LIC.document())
        if a.write is not None:
            for f in LIC.write_files(a.write):
                print(f"wrote {f}")
        return 0
    if a.cmd == "sample":
        from .sample import write_sample_files
        out = write_sample_files(Path(a.folder))
        for k, v in out.items():
            print(f"  {k:8} {v}")
        return 0
    if a.cmd == "sample-job":
        from .sample import write_sample_job_with_readme
        out = write_sample_job_with_readme(a.folder, name=a.name,
                                           progress=lambda f, m: print(f"  [{f:4.0%}] {m}"))
        print(f"\njob folder: {out['root']}")
        print(f"  project      {out['project']}")
        print(f"  field data   {out['field_data']}")
        print(f"  check report {out['check_report']}")
        return 0
    if a.cmd == "sample-real":
        return _sample_real(a)
    if a.cmd == "new":
        return _new_job(a)
    if a.cmd == "fieldwork":
        return _fieldwork_report(a)
    return _gui(file_arg)


# ----------------------------------------------------------------------------- fieldwork commands
def _default_source() -> tuple[str | None, str | None, str | None]:
    """Where the Real World sample's own source files live, relative to this checkout."""
    from .fieldwork.sample_real import source_files
    fwk, f2f, chk = source_files()
    return ((str(fwk) if fwk else None),
            (str(f2f) if f2f else None),
            (str(chk) if chk else None))


def _sample_real(a) -> int:
    from .fieldwork.sample_real import build_real_world_sample
    fwk, f2f, chk = _default_source()
    fwk = a.source or fwk
    f2f = a.f2f or f2f
    chk = a.check_report or chk
    if not fwk or not Path(fwk).exists():
        print("No source field file. Pass --source <consolidated .fwk> "
              "(and --f2f <CARLSON F2F.csv> for the code table).", file=sys.stderr)
        return 2
    folder = Path(a.folder) if a.folder else Path.cwd() / "samples"
    if folder.name.lower() not in ("samples", "real world") and (folder / a.name).exists():
        pass
    out = build_real_world_sample(folder, fwk, f2f, chk, name=a.name,
                                  epsg=a.epsg or None,
                                  progress=lambda f, m: print(f"  [{f:4.0%}] {m}"))
    print(f"\njob folder: {out['root']}")
    for c in out["crews"]:
        print(f"  Crew {c['crew']:>2}  {'/'.join(c['initials']) or '?':4}  "
              f"{c['points']:>5,} pts  {', '.join(c['sensors']):12}  {c['folder']}")
    im = out.get("import", {})
    print(f"\n  {im.get('points', 0):,} points imported, "
          f"{im.get('coded', 0):,} coded, {im.get('duplicates', 0)} duplicates renumbered")
    return 0


def _new_job(a) -> int:
    from .core import jobtemplate as JT
    from .core.crs import ProjectCRS
    crs = ProjectCRS.from_epsg(a.epsg) if a.epsg else ProjectCRS.unassigned("ftUS")
    try:
        out = JT.create_job(a.folder, a.name, template=JT.JOB_TEMPLATE,
                            crs_label=crs.label, crs_record=crs.to_dict(),
                            progress=lambda f, m: print(f"  [{f:4.0%}] {m}"))
    except FileExistsError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"\n{out.paths.root}")
    for d in out.folders[1:]:
        print(f"  {d.relative_to(out.paths.root)}/")
    for f in out.files:
        print(f"  {f.relative_to(out.paths.root)}")
    print(f"\ncoordinate system: {crs.label}")
    return 0


def _fieldwork_report(a) -> int:
    from .fieldwork import bridge as FB
    try:
        rows, info = FB.read_point_file(a.file)
    except (IOError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    s = FB.summarise(rows)
    print(f"{a.file}  ({info['kind']})")
    print(f"  rows {s['rows']:,}   usable {s['usable']:,}   duplicate numbers {s['duplicate_numbers']}")
    if s["crews"]:
        print(f"  crews by point range: {', '.join(str(c) for c in s['crews'])}")
    print(f"  point numbers {s['min_number']} .. {s['max_number']}")
    if s["files"] > 1:
        print(f"  {s['files']} source files")
    f2f = FB.f2f_code_set(a.f2f) if a.f2f else set()
    found = FB.run_checks(rows)
    print(f"\nfindings")
    print(f"  exact duplicate numbers  {len(found['exact'])} groups")
    print(f"  look-alike numbers       {len(found['similar'])} groups")
    print(f"  points on top of each other {len(found['close'])} groups")
    if a.f2f:
        # A code counts as known if the *whole* leading word is in the F2F (Carlson
        # convention, "THACK22") or if the base code before any string number is
        # (Plumbline convention, "EP1"). Reporting the first word only would call
        # 1,314 perfectly good numbered codes unknown.
        import re as _re
        from .core.featurecodes import parse_description
        from .fieldwork.config import get_command_map
        commands = get_command_map(a.f2f)
        unknown = {}
        for r in rows:
            if not FB.row_is_usable(r):
                continue
            desc = (r[FB.DESC] or "").strip()
            if not desc:
                continue
            lead = _re.match(r"^[A-Za-z][A-Za-z0-9_\-]*", desc)
            word = lead.group(0) if lead else ""
            base = parse_description(desc, commands=commands, known_codes=f2f).code
            if word.casefold() in f2f or (base and base.casefold() in f2f):
                continue
            key = word or base or desc[:12]
            unknown[key] = unknown.get(key, 0) + 1
        print(f"  descriptions not in the F2F {len(unknown)} codes / {sum(unknown.values())} points")
        for code, n in sorted(unknown.items(), key=lambda kv: -kv[1])[:10]:
            print(f"      {code:<16} {n}")
    print("\nreport")
    for r in found["report_rows"][:15]:
        detail = r[10] if len(r) > 10 else ""
        print(f"    {r[1]:<18} {detail}")
    return 0
