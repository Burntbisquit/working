"""Repeatable, non-mutating performance benchmarks for a Plumbline project.

The benchmark is intentionally a project workload report, not a synthetic CPU score.
That lets a survey office run the same job on a lightweight field laptop and a
high-end workstation, then compare the Markdown/JSON reports operation by
operation.  It never saves, edits, rebuilds, or otherwise changes the project.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter_ns
from typing import Callable, TYPE_CHECKING

from .crossings import run_geometry_checks
from .featurecodes import _parse_description_cached, build_linework
from .geometry_segments import build_segments, segment_point_rows
from .qa import run_checks
from .spatial import SpatialIndex

if TYPE_CHECKING:  # pragma: no cover
    from .project import Project

SUITE_VERSION = "2"
PROFILES = {
    "Quick": {"repeats": 1, "qa": False},
    "Standard": {"repeats": 3, "qa": True},
    "Deep": {"repeats": 7, "qa": True},
}


@dataclass(frozen=True, slots=True)
class Measurement:
    operation: str
    iterations: int
    total_ms: float
    mean_ms: float
    best_ms: float
    worst_ms: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    created_utc: str
    suite_version: str
    profile: str
    machine: dict
    project: dict
    measurements: tuple[Measurement, ...]

    def as_dict(self) -> dict:
        return {
            "created_utc": self.created_utc,
            "suite_version": self.suite_version,
            "profile": self.profile,
            "machine": self.machine,
            "project": self.project,
            "measurements": [asdict(item) for item in self.measurements],
        }

    def to_markdown(self) -> str:
        project = self.project
        lines = [
            "# Plumbline Performance Report",
            "",
            f"- **UTC:** {self.created_utc}",
            f"- **Benchmark suite:** {self.suite_version}",
            f"- **Profile:** {self.profile}",
            f"- **Project:** {project.get('name', 'Untitled')}",
            "",
            "## Machine",
            "",
            f"- OS: {self.machine.get('os', 'Unknown')}",
            f"- CPU: {self.machine.get('cpu', 'Unknown')}",
            f"- Logical CPUs: {self.machine.get('logical_cpus', 'Unknown')}",
            f"- RAM: {self.machine.get('memory_gb', 'Unknown')}",
            f"- Python: {self.machine.get('python', 'Unknown')}",
            f"- NumPy: {self.machine.get('numpy', 'Unknown')}",
            "",
            "## Project workload",
            "",
            f"- Points: {project.get('points', 0):,}",
            f"- Polylines: {project.get('polylines', 0):,}",
            f"- Text: {project.get('texts', 0):,}",
            f"- Surfaces: {project.get('surfaces', 0):,}",
            f"- Layers: {project.get('layers', 0):,}",
            f"- Feature codes: {project.get('codes', 0):,}",
            f"- Linework processed: {'Yes' if project.get('linework_processed') else 'No'}",
            "",
            "## Timings",
            "",
            "| Operation | Iterations | Mean (ms) | Best (ms) | Worst (ms) | Detail |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for metric in self.measurements:
            lines.append(
                f"| {metric.operation} | {metric.iterations} | {metric.mean_ms:.3f} | "
                f"{metric.best_ms:.3f} | {metric.worst_ms:.3f} | {metric.detail} |"
            )
        lines += [
            "",
            "## Reading this report",
            "",
            "Compare reports from the same project and profile. The slowest row identifies "
            "the subsystem to inspect first; compare mean timing rather than a single best run. "
            "The JSON file beside this report preserves the exact values for deeper comparison.",
            "",
        ]
        return "\n".join(lines)


def _physical_memory_gb() -> str:
    try:
        if os.name == "nt":
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            total = int(status.ullTotalPhys)
        else:
            total = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
        return f"{total / (1024 ** 3):.1f} GB"
    except (AttributeError, OSError, ValueError):
        return "Unavailable"


def machine_fingerprint() -> dict:
    """Portable, dependency-free machine facts useful when comparing two reports."""
    try:
        import numpy as np
        numpy_version = np.__version__
    except Exception:  # pragma: no cover - NumPy is an application dependency
        numpy_version = "Unavailable"
    return {
        "os": platform.platform(aliased=True),
        "cpu": platform.processor() or platform.machine() or "Unavailable",
        "logical_cpus": os.cpu_count() or "Unavailable",
        "memory_gb": _physical_memory_gb(),
        "python": sys.version.split()[0],
        "numpy": numpy_version,
    }


def _measure(
    operation: str,
    function: Callable[[], object],
    iterations: int,
    detail: Callable[[object], str] | None = None,
    before_each: Callable[[], None] | None = None,
) -> Measurement:
    elapsed = []
    result = None
    for _ in range(iterations):
        if before_each is not None:
            before_each()
        started = perf_counter_ns()
        result = function()
        elapsed.append((perf_counter_ns() - started) / 1_000_000.0)
    return Measurement(
        operation=operation, iterations=iterations, total_ms=sum(elapsed), mean_ms=sum(elapsed) / len(elapsed),
        best_ms=min(elapsed), worst_ms=max(elapsed), detail=detail(result) if detail else "",
    )


def _segment_inventory_detail(project: Project, result: list) -> str:
    if not result and project.points:
        return "0 segment(s) (run Process Linework to generate polylines)"
    linked = sum(1 for seg in result if seg.linked)
    return f"{len(result):,} segment(s) ({linked:,} linked)"


def benchmark_project(project: Project, profile: str = "Standard") -> PerformanceReport:
    """Measure stable, useful project operations without altering the project.

    ``Quick`` is suitable on a field laptop while work is waiting. ``Standard`` is
    the normal comparison run. ``Deep`` uses additional repetitions to reduce
    timing noise on a quiet workstation.
    """
    profile = str(profile or "Standard").title()
    if profile not in PROFILES:
        raise ValueError(f"Unknown benchmark profile '{profile}'. Choose: {', '.join(PROFILES)}.")
    spec = PROFILES[profile]
    repeats = int(spec["repeats"])
    # Project.extents() uses a legitimate performance cache. Benchmarking must not
    # leave even that cache changed, so a user can run it while reviewing a job.
    point_array_cache = getattr(project, "_pa_cache", None)
    commands = (project.settings or {}).get("f2f_commands")

    def clear_pa_cache() -> None:
        project._pa_cache = None

    try:
        segments_snapshot = build_segments(project)
        metrics = [
            _measure("Project extents", project.extents, repeats, before_each=clear_pa_cache),
            _measure("Linework input fingerprint", lambda: project.linework_input_signature(order="number"), repeats),
            _measure(
                "Linework build (number order)",
                lambda: build_linework(project.points.values(), project.codes, order="number", commands=commands),
                repeats,
                lambda result: f"{len(result):,} string(s) from {len(project.points):,} points; {len(project.codes):,} code(s)",
            ),
            _measure(
                "Geometry segment inventory",
                lambda: build_segments(project),
                repeats,
                lambda result: _segment_inventory_detail(project, result),
            ),
            _measure(
                "Geometry segment point rows",
                lambda: [segment_point_rows(project, seg) for seg in segments_snapshot],
                repeats,
                lambda result: f"{sum(len(rows) for rows in result):,} vertex row(s) across {len(segments_snapshot):,} segment(s)",
            ),
            _measure(
                "Spatial index build (cold)",
                lambda: SpatialIndex(project),
                repeats,
                lambda result: f"{len(result.pt_ids):,} points; {len(result.segA):,} drawable segment(s)",
                before_each=clear_pa_cache,
            ),
        ]
        if spec["qa"]:
            if hasattr(project.codes.codes, "_invalidate"):
                project.codes.codes._invalidate()
            _parse_description_cached.cache_clear()
            metrics.append(_measure(
                "Point QA checks",
                lambda: run_checks(project),
                repeats,
                lambda result: f"{len(result):,} finding(s); {len(project.codes):,} code(s)",
            ))
            from ..fieldwork import bridge as FB
            f2f_codes = project.codes.casefolded_set() if hasattr(project.codes, "casefolded_set") else {
                c.code.casefold() for c in project.codes
            }
            fb_path = (project.settings or {}).get("fieldbook_file") or None
            metrics.append(_measure(
                "Fieldwork line-sequence checks",
                lambda: FB.check_linework_project(project, f2f=f2f_codes, fieldbook_path=fb_path),
                repeats,
                lambda result: f"{len(result.get('line_issues', ())):,} line issue(s)",
            ))
            metrics.append(_measure(
                "Breakline crossing & length QA",
                lambda: run_geometry_checks(project, segments=segments_snapshot, include_command_issues=False),
                repeats,
                lambda result: (
                    f"{len(result.crossings):,} crossing(s) across {result.breakline_count:,} breakline(s); "
                    f"{len(result.long_segments):,} long span(s) (>{result.max_segment_length:.0f})"
                ),
            ))
        return PerformanceReport(
            created_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            suite_version=SUITE_VERSION, profile=profile, machine=machine_fingerprint(),
            project={
                "name": project.name,
                **project.summary(),
                "codes": len(project.codes),
                "linework_processed": project.linework_is_processed(),
            },
            measurements=tuple(metrics),
        )
    finally:
        project._pa_cache = point_array_cache


def report_directory(project: Project, base: str | os.PathLike | None = None) -> Path:
    """Choose a portable report folder without requiring a project save."""
    if base:
        root = Path(base).expanduser()
    elif getattr(project, "path", None):
        root = Path(project.path).expanduser().parent / "Performance Reports"
    else:
        root = Path.home() / "Documents" / "Plumbline Performance Reports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_report(report: PerformanceReport, directory: str | os.PathLike) -> tuple[Path, Path]:
    """Write human-readable Markdown plus machine-readable JSON, returning both paths."""
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = report.created_utc.replace(":", "").replace("+00:00", "Z").replace("-", "")
    safe_name = "".join(character if character.isalnum() or character in "-_" else "_"
                        for character in str(report.project.get("name", "Untitled"))).strip("_") or "Untitled"
    stem = f"plumbline-performance-{safe_name}-{stamp}"
    markdown_path = folder / f"{stem}.md"
    json_path = folder / f"{stem}.json"
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    json_path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return markdown_path, json_path


def run_project_benchmark(project: Project, profile: str = "Standard", directory: str | os.PathLike | None = None) -> tuple[PerformanceReport, Path, Path]:
    """Run the suite and persist both reports; convenient for the GUI button and scripts."""
    report = benchmark_project(project, profile)
    markdown_path, json_path = write_report(report, report_directory(project, directory))
    return report, markdown_path, json_path
