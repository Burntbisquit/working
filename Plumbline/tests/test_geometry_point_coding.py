from __future__ import annotations

from plumbline.core.point_linework_coder import set_point_line_boundary, set_point_line_code


def test_geometry_point_edit_keeps_other_multicode_sections_intact():
    original = "SW10 ST - TC2 END"
    compacted = set_point_line_code(original, "SW10", "SW1")
    assert compacted == "SW1 ST - TC2 END"

    ended = set_point_line_boundary(compacted, "SW1", "END")
    assert ended == "SW1 END - TC2 END"

    cleared = set_point_line_boundary(ended, "SW1", "")
    assert cleared == "SW1 - TC2 END"
