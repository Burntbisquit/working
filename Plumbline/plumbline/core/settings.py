"""User settings stored as JSON in ~/.plumbline (override with PLUMBLINE_HOME)."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path


def user_dir() -> Path:
    base = os.environ.get("PLUMBLINE_HOME")
    p = Path(base) if base else Path.home() / ".plumbline"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_project_folder() -> Path:
    """Return the configured parent folder for new project/job folders.

    The setting is intentionally a *parent* path: each new job is created in its own
    named folder underneath it. Until the user chooses a different location, prefer
    the familiar Documents location and fall back to the home directory when there is
    no Documents folder.
    """
    configured = str(settings().get("default_project_folder", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / "Plumbline Projects"


DEFAULTS = {
    "theme": "dark",
    "coord_order": "NE",            # order used for typed input: NE (Northing,Easting) or XY
    "angle_format": "quadrant",     # quadrant | azimuth
    "angle_dms": True,
    "angle_decimals": 0,
    "snap_px": 12,
    "point_size_px": 7,
    "label_px": 11,
    "crs_favorites": ["EPSG:2276", "EPSG:6584", "EPSG:32138", "EPSG:6583", "EPSG:4326", "EPSG:4269",
                      "EPSG:6318", "EPSG:26914", "EPSG:6343", "EPSG:32614", "EPSG:3857"],
    "recent_files": [],
    "field_data_import_folder": "",  # last field-data folder chooser location; machine-specific
    "default_project_folder": "",  # parent folder for new jobs; machine-specific
    "tile_cache_dir": "",
    "offline_imagery": False,
    "proj_network": False,
    "secondary_readout": "EPSG:4326",
    "custom_tile_sources": [],
    "replaced_tile_sources": [],      # shipped tile names whose URL the user has overridden
    "hidden_tile_sources": [],        # shipped tile names the user has switched off
    "proj_grid_url": "https://cdn.proj.org",   # the one endpoint every datum/geoid grid comes from
    "update_url": "",                 # blank = never look for a new version
    "licence_accepted_version": 0,    # the agreement version the user accepted (item 1)
    "external_prompt_done": [],       # one-time download offers already made and answered
    "plugin_dirs": [],
    "point_columns": ["source_file", "source_folder"],   # extra columns in the point list
    "control_point_tolerance": 0.01,  # project-coordinate units; smaller differences count as rounding
    "require_fieldbook_for_processing": True,  # open the Field Book tool before code/linework QA or processing
    "space_between_commands": True,
    "space_around_multicode_separator": True,
    "space_around_description_separator": True,
}


#: Settings that go into an exported settings file even when they equal the default.  Nothing here
#: is machine-specific noise: these are the keys that answer "where does this install pull its
#: data from, and how is it set up", which is what somebody saving their settings is saving.
#: (The licence acceptance is deliberately **not** carried between machines: each installation
#: accepts the agreement for itself - core/licence.py.)
ALWAYS_SAVED = ("theme", "coord_order", "angle_format", "angle_dms", "angle_decimals",
                "tile_cache_dir", "custom_tile_sources", "replaced_tile_sources",
                "hidden_tile_sources", "proj_grid_url", "update_url", "secondary_readout",
                "plugin_dirs", "crs_favorites", "point_columns", "control_point_tolerance",
                "require_fieldbook_for_processing", "space_between_commands",
                "space_around_multicode_separator", "space_around_description_separator")


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else user_dir() / "settings.json"
        self._lock = threading.Lock()
        self._data: dict = {}
        self.load()

    def load(self):
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
        except Exception:
            self._data = {}

    def get(self, key, default=None):
        """User value, else the built-in default for known keys (a copy), else `default`."""
        if key in self._data:
            return self._data[key]
        if key in DEFAULTS:
            return copy.deepcopy(DEFAULTS[key])
        return default

    def set(self, key, value, save=True):
        self._data[key] = value
        if save:
            self.save()

    def save(self):
        with self._lock:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    # -- whole-file save / load (change order item 3: "settings save/load")
    def to_dict(self) -> dict:
        """Every setting that differs from the built-in default, plus the ones that always go."""
        out = {}
        for k, v in self._data.items():
            if k not in DEFAULTS or self._data[k] != DEFAULTS[k]:
                out[k] = copy.deepcopy(v)
        for k in ALWAYS_SAVED:
            v = self.get(k)                     # the effective value, default or user's
            if v is not None:
                out[k] = copy.deepcopy(v)
        return out

    def from_dict(self, data: dict, merge: bool = True) -> list[str]:
        """Apply a settings file.  Returns the keys that changed, so the caller can say what did.

        Unknown keys are kept (a newer build's settings file must not be quietly gutted by an older
        one), and with *merge* false the file replaces the current settings outright.
        """
        if not isinstance(data, dict):
            raise ValueError("a settings file is a JSON object")

        def eff(d, k):
            return d[k] if k in d else DEFAULTS.get(k)

        keys = set(self._data) | set(data)
        before = {k: eff(self._data, k) for k in keys}
        if not merge:
            self._data = {}
        for k, v in data.items():
            self._data[k] = copy.deepcopy(v)
        self.save()
        return sorted(k for k in keys if eff(before, k) != eff(self._data, k))

    def export_to(self, path) -> Path:
        """Write the settings somewhere else - a memory stick, a second machine, the job folder."""
        p = Path(path)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return p

    def load_from(self, path, merge: bool = True) -> list[str]:
        p = Path(path)
        return self.from_dict(json.loads(p.read_text("utf-8")), merge=merge)

    def add_recent(self, path: str, limit: int = 10):
        try:
            norm = str(Path(path).resolve())
        except Exception:
            norm = str(path).strip()
        if not norm:
            return
        existing = self.get("recent_files", [])
        rec = []
        for p in existing:
            try:
                p_norm = str(Path(p).resolve())
            except Exception:
                p_norm = str(p).strip()
            if p_norm.casefold() != norm.casefold():
                rec.append(p_norm)
        rec.insert(0, norm)
        self.set("recent_files", rec[:limit])

    @property
    def tile_cache_dir(self) -> Path:
        d = self.get("tile_cache_dir")
        p = Path(d) if d else user_dir() / "tiles"
        p.mkdir(parents=True, exist_ok=True)
        return p


_instance: Settings | None = None


def settings() -> Settings:
    global _instance
    if _instance is None:
        _instance = Settings()
    return _instance
