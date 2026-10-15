"""The Coordinate System dialog, driven end to end.

Four tabs, and each one changes something that is expensive to get wrong: which system the
job is on, which vertical datum its heights are on, and whether ground or grid distances are
shown.  These tests open the real dialog against a real project and press its real buttons.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from plumbline.core import crs as C
from plumbline.core import vdatum as VD
from plumbline.core.project import Project
from plumbline.core.settings import settings
from test_ui import pump  # noqa: F401  (shares the offscreen QApplication)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_proj_state():
    was_net = bool(settings().get("proj_network", False))
    yield
    settings().set("proj_network", was_net)
    VD.allow_downloads(was_net)
    C.set_proj_network(was_net)
    C.clear_transform_cache()


@pytest.fixture()
def make_state():
    """A throwaway project the dialog can be pointed at."""
    def build(crs=None, points=True, name="t"):
        pr = Project(name, crs if crs is not None else C.ProjectCRS.from_key(6584))
        if points:                                          # a small cluster near Mesquite
            for i, (e, n) in enumerate(((2552700.0, 6967100.0), (2553300.0, 6967700.0),
                                        (2553900.0, 6968300.0)), start=1):
                pr.add_point(e, n, 500.0, number=str(i), desc="GS")
        return SimpleNamespace(project=pr)
    return build


def _open(state, app, pump=pump):
    from plumbline.ui.crs_dialog import CRSDialog
    d = CRSDialog(state)
    d.show()
    pump(app)
    return d


# ------------------------------------------------------------------ opening and reading

def test_the_dialog_shows_all_four_tabs_and_says_what_the_project_is_on(app, make_state):
    d = _open(make_state(), app)
    titles = [d.tabs.tabText(i) for i in range(d.tabs.count())]
    assert titles == ["Coordinate System", "Vertical Datum",
                      "Surface Adjustment Factor (Ground Scale)", "Coordinate Calculator"]
    assert "NAD83(2011) / Texas North Central (USft)" in d.banner_current.text()
    assert "Vertical Datum: NAVD88" in d.banner_current.text()
    d.close()


def test_project_crs_defaults_to_five_texas_2011_usft_zones_with_collapsed_search(app, make_state):
    from plumbline.ui.crs_dialog import texas_2011_usft_rows

    d = _open(make_state(crs=C.ProjectCRS.unassigned()), app)
    keys = [d.cmb_texas.itemData(i) for i in range(d.cmb_texas.count())]
    assert keys == [6584, 6582, 6578, 6588, 6586]
    assert len(texas_2011_usft_rows()) == 5
    assert all(C.TEXAS_ZONES[key]["units"] == "USft" for key in keys)
    assert all("(USft)" in d.cmb_texas.itemText(i) for i in range(d.cmb_texas.count()))
    assert d.cmb_texas.currentData() == C.TEXAS_DEFAULT_EPSG
    assert not d.search_area.button.isChecked() and d.search_area.body.isHidden()

    d.cmb_texas.setCurrentIndex(d.cmb_texas.findData(6582))
    assert d._build_result().key == "6582"

    d.search_area.button.setChecked(True)
    d.picker.select_key("EPSG:6583")
    assert d._build_result().authority == "EPSG:6583"
    assert d.cmb_texas.currentIndex() == -1
    d.close()


def test_the_saf_box_has_room_for_four_more_characters_than_it_used_to(app, make_state):
    """A Surface Adjustment Factor is read digit by digit, and the box used to be sized for 14
    characters - two or three short of what a user types.  It is sized from its own font now.

    The theme is applied first, exactly as a real start-up does: the box is sized from the font
    in force when it is built, so measuring it with a different font proves nothing.
    """
    from plumbline.ui import theme
    theme.apply_theme(app, "dark")
    d = _open(make_state(), app)
    fm = d.sp_cf.fontMetrics()
    width_in_digits = d.sp_cf.minimumWidth() / fm.horizontalAdvance("0")
    assert width_in_digits >= 18, f"the SAF box is {width_in_digits:.1f} digits wide"
    assert d.sp_cf.minimumWidth() > fm.horizontalAdvance("1.000150000000")
    # the field side sizes its two SAF boxes the same way
    from plumbline.fieldwork.ui_main import _widen_saf
    from PySide6.QtWidgets import QLineEdit
    e = _widen_saf(QLineEdit("1.0"))
    assert e.minimumWidth() > e.fontMetrics().horizontalAdvance("1.000150000000")
    d.close()


def test_a_local_project_cannot_pretend_to_have_a_ground_scale(app, make_state):
    """With no position on the earth there is nothing to convert from and nothing to scale."""
    d = _open(make_state(crs=C.ProjectCRS.local()), app)
    assert d.tabs.isTabEnabled(3) is False
    assert d.r_reproj.isEnabled() is False
    assert d.r_assign.isChecked() is True
    assert "local coordinates" in d.fit.text()
    d.close()


def _form_labels(d):
    """Every QFormLayout label in the dialog, in order - for the 'northing above easting' rule."""
    from PySide6.QtWidgets import QFormLayout
    out = []
    for form in d.findChildren(QFormLayout):
        for r in range(form.rowCount()):
            item = form.itemAt(r, QFormLayout.LabelRole)
            if item is not None and item.widget() is not None:
                out.append(item.widget().text())
    return out


# ------------------------------------------------------------------------- allow downloads

def test_allow_downloads_is_written_to_settings_and_comes_back(app, make_state):
    """The switch is shared with the geoid code, so flipping one has to flip the other, and
    reopening the dialog has to show what was chosen."""
    s = settings()
    was = bool(s.get("proj_network", False))
    try:
        d = _open(make_state(), app)
        d.chk_net.setChecked(False)
        assert s.get("proj_network") is False
        assert VD.allow_downloads() is False
        d._geoid_note()                            # the note above the row follows the switch
        d.chk_net.setChecked(True)
        assert s.get("proj_network") is True and VD.allow_downloads() is True
        d.close()
        # a fresh dialog reads it back off the settings, not off the old widget
        d2 = _open(make_state(), app)
        assert d2.chk_net.isChecked() is True
        d2.close()
    finally:
        settings().set("proj_network", was)
        VD.allow_downloads(was)
        C.set_proj_network(was)


# ------------------------------------------------------------------------------ legacy zone

def test_the_legacy_zone_button_offers_2011_and_the_result_keeps_the_chosen_system(app, make_state):
    st = make_state(crs=C.ProjectCRS.from_epsg(2276))         # NAD83 / Texas North Central (ftUS)
    d = _open(st, app)
    assert d.btn_legacy.isVisible() and "6584" in d.btn_legacy.text()
    d.btn_legacy.click()
    pump(app)
    res = d._build_result()
    assert res.key == "6584" and "2011" in res.name
    assert d.btn_legacy.isVisible() is False                  # the offer is made once, not twice
    assert d.r_assign.isChecked() is True                     # assigning, not silently reprojecting
    d.close()


def test_a_legacy_zone_chosen_by_hand_is_still_accepted(app, make_state):
    """The library offers the 2011 code; the user decides.  A job on 2276 stays on 2276."""
    d = _open(make_state(crs=C.ProjectCRS.from_epsg(2276)), app)
    assert d._build_result().key == "2276"
    assert d.project.crs.is_legacy_zone and d.project.crs.legacy_replacement() == 6584
    d.close()


# ------------------------------------------------------------------- the height rows

@pytest.fixture()
def grid_ready():
    was = VD.allow_downloads(True)
    n = VD.separation(-96.5992, 32.7668, "GEOID18")
    VD.allow_downloads(was)
    if n is None:
        pytest.skip("GEOID18 not available on this machine")
    return n


def test_the_height_converter_is_gone_and_the_reprojection_tool_is_the_one_way_to_move_heights(app, make_state):
    """**Convert a Height is removed** (change order, item 10).

    It was a second way to do what reprojection already does, and two ways to change a height is
    how a job ends up with heights on two different datums and no record of which.  The tab must
    no longer offer it: no easting/northing/height rows, no direction box, no convert button - and
    the note that replaces it has to say where the one remaining path is.
    """
    d = _open(make_state(), app)
    for gone in ("ed_conv_e", "ed_conv_n", "ed_conv_h", "cmb_conv_dir", "lbl_conv", "btn_conv",
                 "_convert_height"):
        assert not hasattr(d, gone), f"{gone} is still on the dialog"
    notes = " ".join(w.text() for w in d.findChildren(type(d.lbl_geoid)) if hasattr(w, "text"))
    assert "reprojection tool" in notes
    assert "no height calculator" in notes.lower()
    d.close()


def test_the_ngvd29_row_still_points_at_vertcon(app, make_state):
    """NGVD29 is tied to NAVD88 by VERTCON, not by a geoid - the note has to say so, because the
    conversion that used to demonstrate it is gone and the datum still needs explaining."""
    d = _open(make_state(), app)
    d.cmb_vdatum.setCurrentIndex(d.cmb_vdatum.findData("NGVD29"))
    pump(app)
    assert "VERTCON" in d.lbl_geoid.text()
    d.close()


def test_the_saf_box_keeps_the_whole_factor_and_is_locked_until_ground_is_ticked(app, make_state):
    """Items 10 and 11 of the change order, in the dialog they are most often typed into.

    ``1.000136506`` must be that number (not ``1.00013650``), the base point and the SAF must be
    unenterable while the job is grid, and northing must be offered above easting.
    """
    d = _open(make_state(), app)
    assert d.sp_cf.decimals() == 11
    d.chk_ground.setChecked(True)
    d.sp_cf.setValue(1.000136506)
    assert abs(d.sp_cf.value() - 1.000136506) < 1e-15
    assert d.sp_cf.text().startswith("1.000136506")
    d.chk_ground.setChecked(False)
    assert not (d.sp_bx.isEnabled() or d.sp_by.isEnabled() or d.sp_cf.isEnabled())
    d.chk_ground.setChecked(True)
    assert d.sp_bx.isEnabled() and d.sp_by.isEnabled() and d.sp_cf.isEnabled()
    # northing above easting: the order a coordinate is read in
    labels = _form_labels(d)
    assert labels.index("Base Northing:") < labels.index("Base Easting:")
    d.close()


# --------------------------------------------------------------------------- searching

def test_searching_the_picker_finds_the_texas_systems_first(app):
    """A Texas surveyor searches "international feet" or "nad27" and has to land on a Texas
    system.  The international-foot realisations have no EPSG code, so a catalogue search can
    never return them - they only exist because the Texas list is consulted first."""
    from PySide6.QtCore import Qt
    from plumbline.ui.crs_dialog import CRSPicker

    def keys_for(text):
        p = CRSPicker()
        p.search.setText(text)
        p.refresh()
        out = []
        for i in range(p.list.count()):
            it = p.list.item(i)
            k = it.data(Qt.UserRole)
            if k:
                out.append((k, it.text()))
        return out

    intl = [k for k, _ in keys_for("international feet")]
    assert intl[0] == "PLUMBLINE:6584-ft"                            # North Central, USft order
    assert "PLUMBLINE:2276-ft" in intl and len(intl) == 10           # all ten, and only those
    assert all(k.startswith("PLUMBLINE:") for k in intl)

    nad27 = [k for k, _ in keys_for("nad27")]
    assert nad27[:5] == ["EPSG:32038", "EPSG:32037", "EPSG:32039", "EPSG:32040", "EPSG:32041"]

    north = [k for k, _ in keys_for("state plane north central")]
    assert north[:3] == ["EPSG:6584", "EPSG:6583", "PLUMBLINE:6584-ft"]

    assert keys_for("zzzz nothing") == []                            # and a miss stays a miss


# ------------------------------------------------------------------------- the result itself

def test_the_result_carries_the_system_the_datum_the_geoid_and_the_ground_scale(app, make_state):
    d = _open(make_state(), app)
    d.picker.select_key("EPSG:6584")
    d.cmb_vdatum.setCurrentIndex(d.cmb_vdatum.findData("NAVD88"))
    d.cmb_geoid.setCurrentIndex(d.cmb_geoid.findData("GEOID18"))
    d.chk_ground.setChecked(True)
    d.sp_bx.setValue(2553000.0)
    d.sp_by.setValue(6968000.0)
    d.sp_cf.setValue(1.00015000)                      # a real Texas SAF: ground is 1.00015x grid
    res = d._build_result()
    assert res.key == "6584" and res.vdatum == "NAVD88" and res.geoid == "GEOID18"
    assert res.ground is not None and res.ground.enabled and not res.ground.is_txdot_origin_scale
    assert (res.ground.base_x, res.ground.base_y) == (2553000.0, 6968000.0)
    assert res.ground.saf == pytest.approx(1.00015)
    assert res.ground.factor == pytest.approx(1.0 / 1.00015)      # the legacy reading, for old reports
    # the SAF box refuses nonsense: a factor that is not near 1 is almost always a typo
    assert d.sp_cf.minimum() >= 0.9 and d.sp_cf.maximum() <= 1.1

    # and it survives the trip through the project file
    back = C.ProjectCRS.from_dict(res.to_dict())
    assert back.key == res.key and back.vdatum == "NAVD88" and back.geoid == "GEOID18"
    assert back.ground.enabled and (back.ground.base_x, back.ground.base_y) == (2553000.0, 6968000.0)
    d.close()


def test_assigning_a_system_keeps_the_numbers_where_they_are(app, make_state):
    """Assign is the default and it must not touch a single coordinate."""
    st = make_state(crs=C.ProjectCRS.local())
    before = [(p.x, p.y) for p in st.project.points.values()]
    d = _open(st, app)
    d.picker.select_key("EPSG:6584")
    res = d._build_result()
    assert res.key == "6584"
    assert [(p.x, p.y) for p in st.project.points.values()] == before
    assert d.r_assign.isChecked() is True
    d.close()
