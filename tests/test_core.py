import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import colorbar_core as core  # noqa: E402
from make_sample import build_pdf  # noqa: E402


def approx(v, target, tol=0.005):
    return abs(v - target) <= tol


def test_horizontal_bar_size_and_map():
    res = core.analyze(build_pdf("horizontal"))
    assert len(res["candidates"]) == 1, "decoys must not become candidates"
    c = res["candidates"][0]
    assert c["orientation"] == "horizontal"
    assert approx(c["patch_w_mm"], 4.0) and approx(c["patch_h_mm"], 6.0)
    assert approx(c["pitch_mm"], 4.0)
    assert c["patch_count"] == 16
    assert c["slot_count"] == 18
    assert c["empty_slots"] == [8, 9]
    assert approx(c["origin_mm"]["x"], 14.0, 0.05)
    assert c["warnings"] == []


def test_spot_colors_are_numbered_not_named():
    c = core.analyze(build_pdf("horizontal"))["candidates"][0]
    assert [s["label"] for s in c["spots"]] == ["S1"]
    labels = [p["label"] for p in c["patches"]]
    assert "S1 100" in labels and "S1 25" in labels and "S1 75" in labels
    assert "K100" in labels and "C50 M40 Y40" in labels and "Paper (no ink)" in labels


def test_production_ink_and_rgb_decoys_are_ignored():
    res = core.analyze(build_pdf("horizontal"))
    assert res["ignored"]["production_ink_rectangles"] >= 5
    # no candidate should sit in the decoy area (the bar is at the bottom of the page)
    for c in res["candidates"]:
        assert c["origin_mm"]["y"] > 50  # mm from top; decoys are above this


def test_vertical_bar():
    c = core.analyze(build_pdf("vertical"))["candidates"][0]
    assert c["orientation"] == "vertical"
    assert approx(c["patch_w_mm"], 6.0) and approx(c["patch_h_mm"], 4.0)
    assert approx(c["pitch_mm"], 4.0)
    assert c["patch_count"] == 16 and c["empty_slots"] == [8, 9]


def test_no_bar_found():
    res = core.analyze(build_pdf("horizontal", with_bar=False, rgb_decoy=False))
    assert res["candidates"] == []


def test_rgb_only_row_is_reported_with_a_warning():
    # No real bar, but a row of RGB swatches: shown as a last resort, never silently.
    res = core.analyze(build_pdf("horizontal", with_bar=False))
    assert len(res["candidates"]) == 1
    assert any("RGB" in w for w in res["candidates"][0]["warnings"])


def test_rgb_row_is_ignored_when_a_real_bar_exists():
    res = core.analyze(build_pdf("horizontal"))
    assert all(not any("RGB" in w for w in c["warnings"]) for c in res["candidates"])


def test_accepts_bytes_path_and_file_object(tmp_path):
    data = build_pdf("horizontal")
    p = tmp_path / "x.pdf"
    p.write_bytes(data)
    a = core.analyze(data)["candidates"][0]["patch_count"]
    b = core.analyze(str(p))["candidates"][0]["patch_count"]
    with open(p, "rb") as fh:
        c = core.analyze(fh)["candidates"][0]["patch_count"]
    assert a == b == c == 16


def test_web_result_has_csv():
    res = core.analyze_for_web(build_pdf("horizontal"))
    csv_text = res["candidates"][0]["csv"]
    header = csv_text.splitlines()[0].split(",")
    assert header[:2] == ["slot", "stacked"] and "S1" in header
    assert len(csv_text.splitlines()) == 1 + 16


def test_committed_sample_matches_generator():
    path = os.path.join(ROOT, "tests", "sample", "sample_bar.pdf")
    if not os.path.exists(path):
        pytest.skip("sample not generated yet")
    assert open(path, "rb").read() == build_pdf("horizontal", corner_mark="end")


# ---- QR-style corner mark: the patch next to it is the first patch ----------------------------

def labels(c):
    return [p["label"] for p in c["patches"] if not p["sub"]]


def bar(**kw):
    return core.analyze(build_pdf(**kw))["candidates"][0]


def test_without_a_mark_the_default_order_is_kept():
    c = bar(orientation="horizontal")
    assert c["reference"] == "default order" and c["direction"] == "left to right"
    assert c["corner_mark"] is None
    assert labels(c)[0] == "K100" and labels(c)[-1] == "S1 75"


def test_mark_past_the_end_makes_the_patch_next_to_it_the_first_patch():
    c = bar(orientation="horizontal", corner_mark="end")
    assert c["reference"] == "corner mark" and c["direction"] == "right to left"
    assert labels(c)[0] == "S1 75" and labels(c)[-1] == "K100"   # reversed
    m = c["corner_mark"]
    assert m["found"] and m["applied"] and m["kind"] == "finder" and m["side"] == "end"
    assert approx(m["size_mm"], 3.5, 0.02) and approx(m["module_mm"], 0.5, 0.01) and approx(m["gap_mm"], 3.0, 0.02)
    assert approx(m["along_mm"], -6.5, 0.03)            # before slot 0, in the reading direction
    assert approx(c["start_mm"]["x"], 86.0, 0.05)       # reading starts at the bar's right edge
    assert c["patches"][0]["slot"] == 0 and approx(c["patches"][0]["along_mm"], 0.0)
    assert c["empty_slots"] == [8, 9] and c["patch_count"] == 16 and c["slot_count"] == 18


def test_mark_before_the_start_keeps_the_order_and_is_reported():
    c = bar(orientation="horizontal", corner_mark="start")
    assert c["direction"] == "left to right" and c["reference"] == "corner mark"
    assert labels(c)[0] == "K100"
    assert c["corner_mark"]["side"] == "start" and c["corner_mark"]["applied"] is False
    assert approx(c["start_mm"]["x"], 14.0, 0.05)


def test_vertical_bar_with_a_mark_at_the_bottom_is_read_bottom_to_top():
    c = bar(orientation="vertical", corner_mark="end")
    assert c["orientation"] == "vertical"
    assert c["direction"] == "bottom to top" and c["reference"] == "corner mark"
    assert labels(c)[0] == "S1 75" and labels(c)[-1] == "K100"
    assert approx(c["start_mm"]["y"], 160.0 - 74.0, 0.05)   # the bar's bottom edge, from the top
    assert approx(c["patch_w_mm"], 6.0) and approx(c["patch_h_mm"], 4.0)


def test_vertical_bar_with_a_mark_at_the_top_keeps_top_to_bottom():
    c = bar(orientation="vertical", corner_mark="start")
    assert c["direction"] == "top to bottom" and labels(c)[0] == "K100"


def test_a_mark_that_is_not_in_line_with_the_bar_is_ignored():
    c = bar(orientation="horizontal", corner_mark="offset")
    assert c["reference"] == "default order" and c["corner_mark"] is None
    assert labels(c)[0] == "K100"


def test_a_mark_too_far_from_the_bar_is_ignored():
    c = bar(orientation="horizontal", corner_mark="far")
    assert c["reference"] == "default order" and c["corner_mark"] is None


def test_marks_at_both_ends_leave_the_order_alone_and_warn():
    c = bar(orientation="horizontal", corner_mark="both")
    assert c["reference"] == "default order" and labels(c)[0] == "K100"
    assert any("both ends" in w for w in c["warnings"])


def test_mark_alone_is_not_a_bar_and_is_counted_as_ignored():
    res = core.analyze(build_pdf("horizontal", with_bar=False, rgb_decoy=False, corner_mark="end"))
    assert res["candidates"] == []
    res = core.analyze(build_pdf("horizontal", corner_mark="end"))
    assert res["ignored"]["corner_marks"] == 1


def test_a_rectangle_written_as_a_polygon_with_extra_points_is_still_a_rectangle():
    poly = [(10, 3), (10, 0), (0, 0), (0, 10), (10, 10)]            # starts mid-edge
    assert len(core._simplify_polygon(poly)) == 4
    closed = [(0, 0), (5, 0), (10, 0), (10, 10), (0, 10), (0, 0)]   # collinear point and closing duplicate
    assert len(core._simplify_polygon(closed)) == 4
    tri = [(0, 0), (10, 0), (5, 8)]
    assert len(core._simplify_polygon(tri)) == 3


def test_mark_detector_needs_the_7_5_3_proportions():
    def sq(cx, cy, s):
        return {"x0": cx - s / 2, "x1": cx + s / 2, "y0": cy - s / 2, "y1": cy + s / 2}
    good = [sq(50, 50, 10), sq(50, 50, 7.1), sq(50, 50, 4.3)]
    assert len(core.find_corner_marks(good)) == 1
    bad = [sq(50, 50, 10), sq(50, 50, 9), sq(50, 50, 8)]           # nested, wrong ratios
    assert core.find_corner_marks(bad) == []
    # a middle square that is off-centre is ignored; the outer and inner squares still make a partial mark
    marks = core.find_corner_marks([sq(50, 50, 10), sq(53, 50, 7.1), sq(50, 50, 4.3)])
    assert len(marks) == 1 and marks[0]["kind"] == "partial"
    # an inner square that is off-centre means no mark at all
    assert core.find_corner_marks([sq(50, 50, 10), sq(53, 50, 7.1), sq(53, 50, 4.3)]) == []
