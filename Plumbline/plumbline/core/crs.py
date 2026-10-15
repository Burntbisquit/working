"""Coordinate-system manager.

* searchable PROJ/EPSG catalogue  (search_crs, resolve_crs, describe_crs)
* datum-transformation control    (list_operations, make_transform, strategies)
* ProjectCRS: the CRS a project lives in, plus optional grid<->ground scaling
* helpers for combined factors, plausibility checks and CRS suggestions

All transforms use x/y (easting/northing, lon/lat) ordering - never lat/lon.
Elevations are never pushed through PROJ (orthometric heights must not receive
ellipsoidal datum shifts); only unit conversion is applied to Z elsewhere.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np
from pyproj import CRS, Proj, Transformer
from pyproj.database import query_crs_info
from pyproj.enums import PJType, TransformDirection
from pyproj.transformer import TransformerGroup

from . import units as U

WGS84 = CRS.from_epsg(4326)
WEB_MERCATOR = CRS.from_epsg(3857)


# ----------------------------------------------------------------------------- Texas 2011 quick list
# The zone data below lives in plumbline/fieldwork/coord_systems.py - the module that was
# field-tested on Windows against Google Earth imagery.  It is imported rather than copied so
# there is exactly one place a Texas zone can be defined (or corrected).
from ..fieldwork import coord_systems as _tx            # noqa: E402  (data only - no Qt in this module)

TEXAS_ZONES = _tx.EPSG_LIBRARY                # epsg -> {name, state, zone, units, proj, datum}
TEXAS_LCC_PARAMS = _tx.TEXAS_LCC_PARAMS       # pure-python LCC fallback parameters
TEXAS_DEFAULT_EPSG = _tx.MESQUITE_TX_RECOMMENDED_EPSG      # 6584, NAD83(2011) Texas North Central ftUS
LEGACY_TO_2011 = _tx.LEGACY_2011_MAP


#: What an international foot is called in a label, in the library's own words.
_UNIT_WORDS = {"m": "meters", "USft": "US survey feet", "ft": "international feet"}


def zone_info(key) -> dict | None:
    """The library entry for a key given as 6584, ``"6584"`` or ``"6584-ft"`` (or None)."""
    if key in (None, ""):
        return None
    k = key
    if isinstance(k, float):
        k = int(k)
    if isinstance(k, str):
        k = int(k) if k.isdigit() else k
    return TEXAS_ZONES.get(k)


def normal_key(key):
    """The library key in the shape the library stores it (int for EPSG, str for derived)."""
    if key in (None, ""):
        return ""
    k = key
    if isinstance(k, float):
        k = int(k)
    if isinstance(k, str) and k.isdigit():
        k = int(k)
    return k if k in TEXAS_ZONES else str(key)


def is_derived_key(key) -> bool:
    """True for a system this program builds itself because EPSG has no code for it."""
    return _tx.is_derived(key)


def crs_from_key(key) -> CRS:
    """Any key in the library (or any EPSG code) -> a pyproj CRS.

    Handles three shapes, because a Texas crew can be handed all three:

    * ``6584`` / ``"EPSG:6584"``    - a real EPSG code
    * ``"6584-ft"`` / ``"PLUMBLINE:6584-ft"`` - the same projection with international feet,
      which EPSG does not publish (a 2 ppm difference from US survey feet, and at
      state-plane magnitudes that is a real distance)
    * anything else pyproj understands (WKT, PROJ string, ESRI code)
    """
    if isinstance(key, CRS):
        return key
    if isinstance(key, int) or (isinstance(key, str) and key.strip().isdigit()):
        return CRS.from_epsg(int(key))
    text = str(key).strip()
    tail = text.split(":")[-1]
    if _tx.is_derived(tail) or _tx.is_derived(text):
        base = _tx.base_epsg(tail if _tx.is_derived(tail) else text)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")        # pyproj warns about the proj4 round trip
            p4 = CRS.from_epsg(int(base)).to_proj4()
        # US survey feet -> international feet.  Everything else about the zone is unchanged.
        p4 = p4.replace("+units=us-ft", "+units=ft").replace("+no_defs", "").replace("+type=crs", "")
        return CRS.from_proj4(p4.strip())
    return CRS.from_user_input(text)


def texas_zone_records() -> list[CrsRecord]:
    """Every Texas State Plane system in the library: five zones x three datums x three units.

    The picker opens on this rather than on nine thousand EPSG codes, because these are the
    systems a Texas crew can actually be handed:

    * **NAD83(2011)** - the current realisation (EPSG 6577-6588)
    * **NAD83** - the 1986 realisation (EPSG 2275-2279 ftUS, 32137-32141 m), still on live
      records and on jobs that were never re-observed
    * **NAD27** - the old surveys (EPSG 32037-32041, US survey feet)

    each in **metres, US survey feet and international feet**.  Nothing is migrated behind
    the user's back: a job that says 2276 means 2276, and the 2011 equivalent is offered as a
    one-click change instead.
    """
    out = []
    for key, info in TEXAS_ZONES.items():
        if info["state"] != "TX":
            continue
        code = str(key)
        auth = "PLUMBLINE" if _tx.is_derived(key) else "EPSG"
        text = (f"texas {info['zone']} {info['datum']} {_UNIT_WORDS.get(info['units'], info['units'])} "
                f"{code} {info['era']} state plane lcc").lower()
        out.append(CrsRecord(auth, code, info["name"], "projected", "Texas, USA",
                             "Lambert Conic Conformal (2SP)", None, text))
    # 2011 first, newest-first inside a zone, then NAD83, then NAD27 - the order the list shows
    era_rank = {"2011": 0, "1986": 1, "1927": 2}
    unit_rank = {"USft": 0, "m": 1, "ft": 2}
    zone_rank = {"North Central": 0, "North": 1, "Central": 2, "South Central": 3, "South": 4}
    def sort_key(r: CrsRecord):
        info = TEXAS_ZONES[_tx_key_for(r)]
        return (zone_rank.get(info["zone"], 9), era_rank.get(info["era"], 9),
                unit_rank.get(info["units"], 9))
    out.sort(key=sort_key)
    return out


def _tx_key_for(record: CrsRecord):
    """The library key a record came from (records keep their code as a string)."""
    return normal_key(record.code)


def texas_matches(query: str) -> list[CrsRecord]:
    """Texas library entries matching a search string (same AND-of-tokens rule as search_crs)."""
    tokens = [t for t in str(query or "").strip().lower().split() if t]
    if not tokens:
        return texas_zone_records()
    out = []
    for r in texas_zone_records():
        if all(t in r.text or t in r.name.lower() for t in tokens):
            out.append(r)
    return out


def wgs84_record() -> CrsRecord:
    info = TEXAS_ZONES[4326]
    return CrsRecord("EPSG", "4326", info["name"], "geographic", "World", "", None, "wgs84 lat lon 4326")


def migrate_epsg(code):
    """Map a legacy Texas zone code forward to its NAD83(2011) equivalent.

    2276 (NAD83 Texas North Central, US ft) -> 6584.  An unrecognised code - including the
    ``"6584-ft"`` derived keys - is returned unchanged, so this is safe to call on anything.

    This is **offered, never applied silently**: older realisations are still on live records
    and are still selectable, so a project that says 2276 keeps saying 2276 until the user
    changes it (the dialog offers the 2011 equivalent in one click).
    """
    return _tx.migrate_epsg(code)


def is_texas_zone(code) -> bool:
    try:
        return int(str(code).split(":")[-1]) in TEXAS_ZONES
    except (TypeError, ValueError):
        return False


def zone_unit_choices(zone) -> list[tuple[object, str]]:
    """(key, label) for the same zone and datum in every unit that exists.

    ``zone_unit_choices(6584)`` -> metres, US survey feet and international feet of the
    *same* zone and realisation.  Handy because the same physical zone is a different code
    depending on the unit, and picking the wrong one is a 3.28x error rather than a rounding
    error - or, between the two foot definitions, a 2 ppm one.
    """
    info = TEXAS_ZONES.get(zone)
    if not info:
        return []
    out = []
    for key, other in TEXAS_ZONES.items():
        if other["state"] != "TX" or other["zone"] != info["zone"] or other["datum"] != info["datum"]:
            continue
        out.append((key, f"{other['name']}  -  {_UNIT_WORDS.get(other['units'], other['units'])}"))
    order = {"USft": 0, "m": 1, "ft": 2}
    out.sort(key=lambda t: order.get(TEXAS_ZONES[t[0]]["units"], 9))
    return out


# ----------------------------------------------------------------------------- catalogue
@dataclass(frozen=True)
class CrsRecord:
    auth: str
    code: str
    name: str
    kind: str                 # "projected" | "geographic"
    area: str
    method: str
    bounds: tuple | None
    text: str = ""

    @property
    def key(self) -> str:
        return f"{self.auth}:{self.code}"


_RECORDS: list[CrsRecord] | None = None


def all_records() -> list[CrsRecord]:
    global _RECORDS
    if _RECORDS is None:
        infos = query_crs_info(auth_name=None,
                               pj_types=[PJType.PROJECTED_CRS, PJType.GEOGRAPHIC_2D_CRS],
                               allow_deprecated=False)
        recs = []
        for i in infos:
            if i.auth_name not in ("EPSG", "ESRI"):
                continue
            kind = "projected" if i.type == PJType.PROJECTED_CRS else "geographic"
            aou = i.area_of_use
            bounds = (aou.west, aou.south, aou.east, aou.north) if aou else None
            area = aou.name if aou else ""
            method = i.projection_method_name or ""
            text = f"{i.auth_name}:{i.code} {i.code} {i.name} {area} {method}".lower()
            recs.append(CrsRecord(i.auth_name, str(i.code), i.name, kind, area, method, bounds, text))
        _RECORDS = recs
    return _RECORDS


def search_crs(query: str = "", kinds=("projected", "geographic"), limit: int = 400,
               auths=("EPSG", "ESRI")) -> list[CrsRecord]:
    """Case-insensitive AND-search over code, name, area of use and projection method."""
    q = query.strip().lower()
    tokens = q.split()
    scored = []
    for r in all_records():
        if r.kind not in kinds or r.auth not in auths:
            continue
        if tokens and not all(t in r.text for t in tokens):
            continue
        name = r.name.lower()
        if q and (q == r.code or q == r.key.lower()):
            score = 0
        elif q and name.startswith(q):
            score = 1
        elif q and q in name:
            score = 2
        else:
            score = 3
        scored.append((score, r.name, r))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in scored[:limit]]


def resolve_crs(spec) -> CRS:
    """Accept CRS, int EPSG, 'EPSG:2276', '2276', '6584-ft', WKT, PROJ string or ProjectCRS."""
    if isinstance(spec, ProjectCRS):
        return spec.crs
    if isinstance(spec, CRS):
        return spec
    if isinstance(spec, (int, float)):
        return CRS.from_epsg(int(spec))
    s = str(spec).strip()
    if s.isdigit():
        return CRS.from_epsg(int(s))
    tail = s.split(":")[-1]
    if is_derived_key(tail) or tail in TEXAS_ZONES:
        return crs_from_key(tail)
    return CRS.from_user_input(s)


def describe_crs(crs: CRS) -> dict:
    """Everything a user wants to see before trusting a CRS."""
    crs = resolve_crs(crs)
    d: dict = {"name": crs.name, "type": "Projected" if crs.is_projected else
               "Geographic" if crs.is_geographic else crs.type_name}
    try:
        a = crs.to_authority()
        d["authority"] = f"{a[0]}:{a[1]}" if a else "(custom)"
    except Exception:
        d["authority"] = "(custom)"
    try:
        ax = crs.axis_info
        d["axes"] = [f"{x.name} ({x.abbrev}) {x.direction}, {x.unit_name}" for x in ax]
        d["unit"] = (U.unit_from_factor(ax[0].unit_conversion_factor, ax[0].unit_name)
                     if (crs.is_projected or crs.is_engineering) else "deg")
        d["unit_name"] = ax[0].unit_name
    except Exception:
        d["axes"], d["unit"], d["unit_name"] = [], "m", "metre"
    d["datum"] = crs.datum.name if crs.datum else ""
    d["geodetic_crs"] = crs.geodetic_crs.name if crs.geodetic_crs else ""
    if crs.ellipsoid:
        d["ellipsoid"] = f"{crs.ellipsoid.name} (a={crs.ellipsoid.semi_major_metre:.3f} m, 1/f={crs.ellipsoid.inverse_flattening:.9f})"
    d["projection"] = crs.coordinate_operation.method_name if crs.is_projected and crs.coordinate_operation else ""
    d["params"] = ([(p.name, p.value, p.unit_name) for p in crs.coordinate_operation.params]
                   if crs.is_projected and crs.coordinate_operation else [])
    aou = crs.area_of_use
    d["area"] = aou.name if aou else ""
    d["bounds"] = tuple(aou.bounds) if aou else None
    d["deprecated"] = bool(getattr(crs, "is_deprecated", False)) if hasattr(crs, "is_deprecated") else False
    try:
        d["wkt"] = crs.to_wkt(pretty=True)
    except Exception:
        d["wkt"] = ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            d["proj"] = crs.to_proj4()
    except Exception:
        d["proj"] = ""
    return d


# ----------------------------------------------------------------------------- transformations
@dataclass
class OperationInfo:
    name: str
    accuracy: float | None
    available: bool
    area: str = ""
    grids: list = field(default_factory=list)


def list_operations(src, dst) -> list[OperationInfo]:
    """All candidate datum transformations PROJ knows between two CRSs, with accuracy."""
    src, dst = resolve_crs(src), resolve_crs(dst)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tg = TransformerGroup(src, dst, always_xy=True)
    ops: list[OperationInfo] = []
    for t in tg.transformers:
        acc = t.accuracy if (t.accuracy is not None and t.accuracy >= 0) else None
        ops.append(OperationInfo(t.description, acc, True, getattr(t.area_of_use, "name", "") or ""))
    for u in tg.unavailable_operations:
        acc = getattr(u, "accuracy", None)
        acc = acc if (acc is not None and acc >= 0) else None
        grids = [g.short_name for g in getattr(u, "grids", []) or []]
        ops.append(OperationInfo(u.name, acc, False, getattr(getattr(u, "area_of_use", None), "name", "") or "", grids))
    return ops


class CoordTransform:
    """(x, y) -> (x', y') callable with an inverse and a human description."""

    def __init__(self, fwd, inv=None, description: str = "", accuracy: float | None = None,
                 identity: bool = False):
        self._fwd, self._inv = fwd, inv
        self.description = description
        self.accuracy = accuracy
        self.identity = identity

    def __call__(self, x, y):
        return self._fwd(x, y)

    def inverse(self) -> "CoordTransform":
        if self._inv is None:
            raise ValueError("transform is not invertible")
        return CoordTransform(self._inv, self._fwd, "inverse of " + self.description,
                              self.accuracy, self.identity)

    def apply(self, xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, float).reshape(-1, 2)
        x, y = self._fwd(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])


def _identity(x, y):
    return x, y


IDENTITY = CoordTransform(_identity, _identity, "identity", 0.0, identity=True)


def compose(*steps: CoordTransform) -> CoordTransform:
    steps = [s for s in steps if not s.identity]
    if not steps:
        return IDENTITY
    if len(steps) == 1:
        return steps[0]

    def fwd(x, y):
        for s in steps:
            x, y = s(x, y)
        return x, y

    inv_steps = [s.inverse() for s in reversed(steps)]

    def inv(x, y):
        for s in inv_steps:
            x, y = s(x, y)
        return x, y

    acc = [s.accuracy for s in steps if s.accuracy is not None]
    return CoordTransform(fwd, inv, " -> ".join(s.description for s in steps), max(acc) if acc else None)


def _proj_transform(t: Transformer, description: str, *, fallback_on_invalid: bool = True) -> CoordTransform:
    acc = t.accuracy if (t.accuracy is not None and t.accuracy >= 0) else None
    null_datum = None

    def _fallback():
        nonlocal null_datum
        if null_datum is None:
            null_datum = _null_datum(t.source_crs, t.target_crs)
        return null_datum

    def _run(x, y, direction=None):
        if direction is None:
            rx, ry = t.transform(x, y)
        else:
            rx, ry = t.transform(x, y, direction=direction)

        try:
            rx_array, ry_array = np.broadcast_arrays(np.asarray(rx, dtype=float),
                                                    np.asarray(ry, dtype=float))
            invalid = ~(np.isfinite(rx_array) & np.isfinite(ry_array))
        except (TypeError, ValueError):
            return rx, ry

        if not fallback_on_invalid or not np.any(invalid):
            return rx, ry

        fallback = _fallback()
        fallback_transform = fallback if direction is None else fallback.inverse()
        if invalid.ndim == 0:
            return fallback_transform(x, y)

        # PROJ can return a mix of valid and invalid results for an array (for example,
        # coordinates on both sides of a projection's area of use). Preserve every valid
        # result and use the no-datum operation only for the failing coordinate pairs.
        x_array, y_array = np.broadcast_arrays(np.asarray(x, dtype=float),
                                               np.asarray(y, dtype=float))
        if x_array.shape != invalid.shape:
            x_array = np.broadcast_to(x_array, invalid.shape)
            y_array = np.broadcast_to(y_array, invalid.shape)
        out_x = rx_array.copy()
        out_y = ry_array.copy()
        fallback_x, fallback_y = fallback_transform(x_array[invalid], y_array[invalid])
        out_x[invalid] = np.asarray(fallback_x, dtype=float).reshape(-1)
        out_y[invalid] = np.asarray(fallback_y, dtype=float).reshape(-1)
        return out_x, out_y

    def fwd(x, y):
        return _run(x, y)

    def inv(x, y):
        return _run(x, y, TransformDirection.INVERSE)

    return CoordTransform(fwd, inv, description, acc)


def _geo_of(crs: CRS) -> CRS:
    return crs.geodetic_crs if (crs.is_projected and crs.geodetic_crs is not None) else crs


def _null_datum(src: CRS, dst: CRS) -> CoordTransform:
    """Projection maths only - treat source and destination datums as identical."""
    steps = []
    if src.is_projected:
        steps.append(_proj_transform(Transformer.from_crs(src, _geo_of(src), always_xy=True),
                                     f"unproject {src.name}", fallback_on_invalid=False))
    if dst.is_projected:
        steps.append(_proj_transform(Transformer.from_crs(_geo_of(dst), dst, always_xy=True),
                                     f"project {dst.name}", fallback_on_invalid=False))
    ct = compose(*steps)
    ct.description = f"{src.name} -> {dst.name} (no datum shift)"
    ct.accuracy = None
    return ct


_CACHE: dict = {}


def clear_transform_cache():
    _CACHE.clear()


def make_transform(src, dst, strategy: str = "auto") -> CoordTransform:
    """Build a transformer.

    strategy: "auto"        PROJ's best available operation (may be a ballpark null shift)
              "none"        no datum shift at all (pure projection maths)
              "op:<name>"   a specific operation, matched on its description
    """
    src, dst = resolve_crs(src), resolve_crs(dst)
    key = (src.srs, dst.srs, strategy)
    if key in _CACHE:
        return _CACHE[key]
    if src == dst:
        ct = IDENTITY
    elif strategy == "none":
        ct = _null_datum(src, dst)
    else:
        t = None
        if strategy.startswith("op:"):
            want = strategy[3:]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tg = TransformerGroup(src, dst, always_xy=True)
            for cand in tg.transformers:
                if cand.description == want or cand.description.startswith(want):
                    t = cand
                    break
        if t is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                t = Transformer.from_crs(src, dst, always_xy=True)
        ct = _proj_transform(t, t.description)
    _CACHE[key] = ct
    return ct


def set_proj_network(enabled: bool):
    """Allow PROJ to fetch datum-shift grids (NADCON5, GEOID18...) from cdn.proj.org on demand."""
    try:
        import pyproj.network as pn
        pn.set_network_enabled(bool(enabled))
    except Exception:
        pass
    clear_transform_cache()


# ----------------------------------------------------------------------------- ground scaling
@dataclass
class SurfaceAdjustmentFactor:
    """SAF - Surface Adjustment Factor.  **Ground over grid**, not grid over ground.

        ground = base + (grid - base) * saf          <- from_grid()
        grid   = base + (ground - base) / saf        <- to_grid()

    ``saf`` is therefore always > 1 in practice (a ground distance is longer than the
    grid distance between the same two points).  A project in Central Texas might use
    1.00012; TXDOT county factors run around 1.00017.

    Scaling is *affine about a base point*.  For TXDOT work the base point is the
    projection origin and base_x = base_y = 0.0, which makes this a pure scale from
    (0,0):  ground = grid * saf.  Any other base point is allowed (some jobs scale
    about a chosen control monument instead), but the two are NOT interchangeable and
    the base point is written into the project file so a job cannot be reopened with
    the wrong one.

    History note - why the numbers look inverted
    --------------------------------------------
    This class used to be called ``GroundScale`` and stored ``factor`` as a *combined
    factor* = grid/ground (the reciprocal of SAF), which is the textbook CSF.  The
    convention was flipped because the field-side of the program has always worked in
    TXDOT SAF terms (ground-over-grid, scaled from 0,0) and two factors that are
    reciprocals of each other must never be typed into the same box.  ``from_dict``
    still reads old files and inverts the legacy value automatically.
    """
    enabled: bool = False
    base_x: float = 0.0
    base_y: float = 0.0
    saf: float = 1.0           # Surface Adjustment Factor, ground / grid
    note: str = ""

    SCHEMA = 2                 # 1 = legacy GroundScale "factor" (grid/ground)

    # -- compat: reading the old name still gives a number in the old convention,
    #    so any code or report not yet updated keeps printing something sensible.
    @property
    def factor(self) -> float:
        """Legacy combined factor (grid/ground) = 1 / saf.  Read-only."""
        return 1.0 / self.saf if self.saf else 1.0

    @property
    def is_txdot_origin_scale(self) -> bool:
        """True when this is a pure TXDOT-style scale from the projection origin."""
        return self.enabled and abs(self.base_x) < 1e-9 and abs(self.base_y) < 1e-9

    # -- the scaling itself, in one place.  ProjectCRS delegates here, and so does the field
    #    side of the program (plumbline.fieldwork.coord_systems), so the two can never drift.
    def to_grid(self, x, y):
        """Ground coordinates -> grid coordinates (divide by SAF about the base point)."""
        if not self.enabled:
            return x, y
        return (self.base_x + (np.asarray(x) - self.base_x) / self.saf,
                self.base_y + (np.asarray(y) - self.base_y) / self.saf)

    def from_grid(self, x, y):
        """Grid coordinates -> ground coordinates (multiply by SAF about the base point)."""
        if not self.enabled:
            return x, y
        return (self.base_x + (np.asarray(x) - self.base_x) * self.saf,
                self.base_y + (np.asarray(y) - self.base_y) * self.saf)

    def to_dict(self):
        return {"schema": self.SCHEMA, "enabled": self.enabled, "base_x": self.base_x,
                "base_y": self.base_y, "saf": self.saf, "note": self.note}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        if "saf" in d:
            saf = float(d.get("saf") or 1.0)
        elif "factor" in d:                      # legacy file: factor was grid/ground
            f = float(d.get("factor") or 1.0)
            saf = (1.0 / f) if f else 1.0
        else:
            saf = 1.0
        return cls(bool(d.get("enabled")), float(d.get("base_x", 0.0)), float(d.get("base_y", 0.0)),
                   saf, d.get("note", ""))

    @classmethod
    def txdot_default(cls, saf: float, **kw):
        """A pure TXDOT county factor: origin (0,0), ground-over-grid."""
        return cls(True, 0.0, 0.0, float(saf), **kw)


# Backwards-compatible alias - old imports keep working.
GroundScale = SurfaceAdjustmentFactor


def combined_factor(crs, lon: float, lat: float, ellipsoid_height_m: float) -> dict:
    """Grid scale factor, elevation factor and combined factor at a location."""
    crs = resolve_crs(crs)
    f = Proj(crs).get_factors(lon, lat)
    k = 0.5 * (f.meridional_scale + f.parallel_scale)
    a = crs.ellipsoid.semi_major_metre
    inv_f = crs.ellipsoid.inverse_flattening
    flat = 1.0 / inv_f if inv_f else 0.0
    e2 = 2 * flat - flat * flat
    s2 = math.sin(math.radians(lat)) ** 2
    R = a * math.sqrt(1 - e2) / (1 - e2 * s2)           # Gaussian mean radius of curvature
    ef = R / (R + ellipsoid_height_m)
    return {"grid_factor": k, "elevation_factor": ef, "combined": k * ef,
            "convergence_deg": f.meridian_convergence, "radius_m": R}


# ----------------------------------------------------------------------------- ProjectCRS
class LocalCRSError(ValueError):
    """Raised when a geodetic operation is asked of a project that uses local (non-geodetic) coordinates."""


_LOCAL_UNITS = {"ftUS": ("US survey foot", 1200 / 3937), "ft": ("foot", 0.3048), "m": ("metre", 1.0)}


class ProjectCRS:
    """The coordinate system a project's x/y live in (+ optional ground scaling)."""

    def __init__(self, crs, ground: GroundScale | None = None, vunit: str | None = None,
                 vdatum: str = "", strategy: str = "auto", geoid: str = "", key: str = ""):
        self.crs: CRS = resolve_crs(crs)
        self.ground = ground or GroundScale()
        d = describe_crs(self.crs)
        self._unit = d["unit"]
        self.vunit = vunit or self._unit
        self.vdatum = vdatum
        #: Which geoid model ties this project's orthometric heights to the ellipsoid
        #: ("" = none needed, e.g. ellipsoid or assumed heights).  See core.vdatum.
        self.geoid = geoid
        self.strategy = strategy
        #: The library key this was chosen by ("6584", "6584-ft"), when it was chosen from
        #: the list rather than typed as a code - so the label can say the Texas name.
        self.key = str(key or "")

    # --- construction / persistence
    @classmethod
    def from_epsg(cls, code, **kw):
        """From an EPSG code, a "PLUMBLINE:6584-ft" key, or anything resolve_crs takes."""
        return cls.crs_of(code, **kw)

    @classmethod
    def crs_of(cls, key, **kw):
        crs = crs_from_key(key)
        k = normal_key(str(key).split(":")[-1]) if key not in (None, "") else ""
        return cls(crs, key=(k if k not in ("", None) else ""), **kw)

    @classmethod
    def from_key(cls, key, **kw):
        """Choose a library entry by its key - the way the picker hands it over."""
        return cls.crs_of(key, **kw)

    @classmethod
    def unassigned(cls, unit: str = "ftUS", **kw):
        """No coordinate system chosen yet - units are known, the projection is not.

        This is what a new project starts as.  Everything that only needs x/y/z works
        normally (drawing, surfaces, volumes, DXF); everything that needs to know where
        on the earth the job is raises LocalCRSError saying to select a CRS, and the UI
        catches that and offers the coordinate-system dialog.
        """
        p = cls.local(unit, **kw)
        p._unassigned = True
        return p

    @classmethod
    def local(cls, unit: str = "ftUS", **kw):
        """Plain local / assumed coordinates: no datum, no projection, no geodetic position."""
        uname, f = _LOCAL_UNITS.get(unit, _LOCAL_UNITS["ftUS"])
        wkt = ('ENGCRS["Local coordinates (no CRS)",EDATUM["Local engineering datum"],CS[Cartesian,2],'
               f'AXIS["easting (X)",east,ORDER[1],LENGTHUNIT["{uname}",{f!r}]],'
               f'AXIS["northing (Y)",north,ORDER[2],LENGTHUNIT["{uname}",{f!r}]]]')
        return cls(CRS.from_wkt(wkt), **kw)

    @property
    def is_local(self) -> bool:
        return bool(self.crs.is_engineering)

    @property
    def is_unassigned(self) -> bool:
        """True when no CRS has been chosen (the new-project default)."""
        return self.is_local

    def require_crs(self, what: str = "This operation"):
        """Raise LocalCRSError with the 'select CRS' wording the UI shows."""
        self._need_geodetic(what)

    def _need_geodetic(self, what: str = "this"):
        if self.is_local:
            raise LocalCRSError(f"{what} needs a coordinate system - this project's CRS is UNASSIGNED. "
                                f"Select CRS first (Survey > Project Coordinate System...).")

    def to_dict(self) -> dict:
        try:
            a = self.crs.to_authority()
            auth = f"{a[0]}:{a[1]}" if a else None
        except Exception:
            auth = None
        return {"auth": auth, "name": self.name, "wkt": self.crs.to_wkt(), "key": self.key,
                "ground": self.ground.to_dict(), "vunit": self.vunit, "vdatum": self.vdatum,
                "geoid": self.geoid, "strategy": self.strategy}

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectCRS":
        crs, key = None, str(d.get("key") or "")
        if key and (key in TEXAS_ZONES or is_derived_key(key)):
            try:
                crs = crs_from_key(key)          # a derived system has no authority code
            except Exception:
                crs = None
        if crs is None and d.get("auth"):
            try:
                c = CRS.from_user_input(d["auth"])
                if not d.get("name") or c.name == d["name"] or key:
                    crs = c
            except Exception:
                crs = None
        if crs is None:
            crs = CRS.from_wkt(d["wkt"])
        return cls(crs, GroundScale.from_dict(d.get("ground", {})), d.get("vunit"), d.get("vdatum", ""),
                   d.get("strategy", "auto"), d.get("geoid", ""), key)

    def copy(self) -> "ProjectCRS":
        return ProjectCRS.from_dict(self.to_dict())

    # --- properties
    @property
    def name(self) -> str:
        """The name to show a user: the Texas library name when it came from the list
        (a derived international-foot system has no EPSG name of its own), else pyproj's."""
        info = zone_info(self.key)
        return info["name"] if info else self.crs.name

    @property
    def vertical_label(self) -> str:
        """How the vertical datum reads on a status bar: ``NAVD88 (GEOID18)``.

        A label stored by an older version as free text (``"NAVD88 (assumed)"``) is shown as
        written, but a label naming a model we know (``"NAVD88 (GEOID18)"``) is normalised so
        the geoid picker finds it.
        """
        from . import vdatum as VD
        key, geoid, note = VD.resolve_label(self.vdatum or VD.DEFAULT_DATUM)
        if not VD.datum(key):
            return key or "Not Set"
        return VD.datum_label(key, self.geoid or geoid) + (f" ({note})" if note else "")

    @property
    def is_legacy_zone(self) -> bool:
        """True for a pre-2011 Texas realisation, which has a 2011 equivalent available."""
        info = zone_info(self.key)
        return bool(info and info.get("era") in ("1927", "1986"))

    def legacy_replacement(self):
        """The NAD83(2011) key for this zone, if this is an older realisation."""
        if not self.is_legacy_zone:
            return None
        info = zone_info(self.key)
        for k, other in TEXAS_ZONES.items():
            if (other.get("era") == "2011" and other["zone"] == info["zone"]
                    and other["units"] == info["units"]):
                return k
        return None

    @property
    def authority(self) -> str:
        if self.key and is_derived_key(self.key):
            return "PLUMBLINE"
        try:
            a = self.crs.to_authority()
            return f"{a[0]}:{a[1]}" if a else "custom"
        except Exception:
            return "custom"

    @property
    def unit(self) -> str:
        return self._unit

    @property
    def unit_factor(self) -> float:
        return U.M_PER_UNIT.get(self._unit, 1.0)

    @property
    def is_projected(self) -> bool:
        return self.crs.is_projected

    @property
    def label(self) -> str:
        if self.is_local:
            return f"UNASSIGNED (no CRS) - {U.LABEL.get(self._unit, self._unit)}"
        label = f"{self.authority} - {self.name}" if self.authority else self.name
        s = label
        if self.ground.enabled:
            where = "origin (0,0)" if self.ground.is_txdot_origin_scale else f"base N {self.ground.base_y:,.3f} E {self.ground.base_x:,.3f}"
            s += f"  [SAF {self.ground.saf:.8f} from {where}]"
        s += f"\nVertical Datum: {self.vertical_label}"
        return s

    @property
    def geographic(self) -> CRS:
        return _geo_of(self.crs)

    # --- ground <-> grid  (SAF is ground/grid, so ground gets MULTIPLIED)
    def to_grid(self, x, y):
        """Ground coordinates -> grid coordinates (divide by SAF about the base point)."""
        return self.ground.to_grid(x, y)

    def from_grid(self, x, y):
        """Grid coordinates -> ground coordinates (multiply by SAF about the base point)."""
        return self.ground.from_grid(x, y)

    def _ground_step(self, to_grid: bool) -> CoordTransform:
        if not self.ground.enabled:
            return IDENTITY
        a, b = (self.to_grid, self.from_grid) if to_grid else (self.from_grid, self.to_grid)
        return CoordTransform(a, b, "ground->grid (SAF)" if to_grid else "grid->ground (SAF)", 0.0)

    # --- transforms
    def transform_to(self, dst, strategy: str | None = None) -> CoordTransform:
        """project coordinates -> dst coordinates."""
        strategy = strategy or self.strategy
        dst_crs = resolve_crs(dst)
        if self.is_local or dst_crs.is_engineering:
            if self.is_local and dst_crs.is_engineering:        # local -> local: only a unit change is possible
                k = self.unit_factor / U.M_PER_UNIT.get(describe_crs(dst_crs)["unit"], 1.0)
                return CoordTransform(lambda x, y: (np.asarray(x) * k, np.asarray(y) * k),
                                      lambda x, y: (np.asarray(x) / k, np.asarray(y) / k), "local unit change", 0.0)
            raise LocalCRSError("Local coordinates cannot be converted to or from a real coordinate system - "
                                "assign a coordinate system to the project instead.")
        steps = [self._ground_step(True), make_transform(self.crs, dst_crs, strategy)]
        if isinstance(dst, ProjectCRS):
            steps.append(dst._ground_step(False))
        return compose(*steps)

    def transform_from(self, src, strategy: str | None = None) -> CoordTransform:
        """src coordinates -> project coordinates."""
        strategy = strategy or self.strategy
        src_crs = resolve_crs(src)
        if self.is_local or src_crs.is_engineering:
            if self.is_local and src_crs.is_engineering:
                return self.transform_to(src, strategy).inverse()
            raise LocalCRSError("Local coordinates cannot be converted to or from a real coordinate system - "
                                "assign a coordinate system to the project instead.")
        steps = []
        if isinstance(src, ProjectCRS):
            steps.append(src._ground_step(True))
        steps.append(make_transform(resolve_crs(src), self.crs, strategy))
        steps.append(self._ground_step(False))
        return compose(*steps)

    def to_lonlat(self, x, y, target=None, strategy: str | None = None):
        """Project coords -> lon/lat. Default target is the project's own datum (no shift)."""
        self._need_geodetic("Longitude / latitude")
        tgt = self.geographic if target is None else resolve_crs(target)
        return self.transform_to(tgt, strategy)(x, y)

    def from_lonlat(self, lon, lat, source=None, strategy: str | None = None):
        self._need_geodetic("Longitude / latitude")
        src = self.geographic if source is None else resolve_crs(source)
        return self.transform_from(src, strategy)(lon, lat)

    def area_of_use_ok(self, x, y) -> tuple[int, int]:
        """(# inside the CRS area of use, total) for arrays of project coordinates."""
        aou = None if self.is_local else self.crs.area_of_use
        x = np.atleast_1d(np.asarray(x, float)); y = np.atleast_1d(np.asarray(y, float))
        if aou is None or len(x) == 0:
            return len(x), len(x)
        lon, lat = self.to_lonlat(x, y)
        lon, lat = np.asarray(lon), np.asarray(lat)
        w, s, e, n = aou.bounds
        ok = (lon >= w - 0.05) & (lon <= e + 0.05) & (lat >= s - 0.05) & (lat <= n + 0.05) & np.isfinite(lon)
        return int(ok.sum()), len(x)

    def convergence_at(self, x: float, y: float) -> float:
        """Meridian convergence (degrees) at a point - grid north vs true north."""
        self._need_geodetic("Grid convergence")
        gx, gy = self.to_grid(x, y)
        lon, lat = make_transform(self.crs, self.geographic, "none")(gx, gy)
        return Proj(self.crs).get_factors(lon, lat).meridian_convergence


# ----------------------------------------------------------------------------- suggestions
def suggest_crs(x: float, y: float, candidate_keys) -> list[tuple[str, str, float, float]]:
    """Which candidate CRSs have an area of use containing the inverse-projected (x, y)?

    Returns [(key, name, lon, lat)] sorted by distance to the area-of-use centre.
    """
    out = []
    for key in candidate_keys:
        try:
            crs = resolve_crs(key)
            if not crs.is_projected or crs.area_of_use is None:
                continue
            t = Transformer.from_crs(crs, _geo_of(crs), always_xy=True)
            lon, lat = t.transform(x, y)
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            w, s, e, n = crs.area_of_use.bounds
            if w <= lon <= e and s <= lat <= n:
                dist = math.hypot(lon - (w + e) / 2, lat - (s + n) / 2)
                out.append((dist, key, crs.name, lon, lat))
        except Exception:
            continue
    out.sort()
    return [(k, n, lo, la) for _, k, n, lo, la in out]


def format_lonlat(lon: float, lat: float, dms: bool = False, decimals: int = 8) -> str:
    if not dms:
        return f"{lat:.{decimals}f}, {lon:.{decimals}f}"

    def one(v, pos, neg):
        h = pos if v >= 0 else neg
        v = abs(v)
        d = int(v); m = int((v - d) * 60); s = (v - d - m / 60) * 3600
        return f"{d}°{m:02d}'{s:07.4f}\"{h}"

    return f"{one(lat, 'N', 'S')} {one(lon, 'E', 'W')}"


def default_favorites_records() -> list[tuple[str, str]]:
    """[(key, name)] for the favourites in settings (skipping unknown codes)."""
    from .settings import settings
    out = []
    for key in settings().get("crs_favorites", []):
        try:
            out.append((key, resolve_crs(key).name))
        except Exception:
            pass
    return out
