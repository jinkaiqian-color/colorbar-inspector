#!/usr/bin/env python3
"""
Command-line wrapper around colorbar_core.

    python colorbar_inspector.py sheet.pdf          # readable summary
    python colorbar_inspector.py sheet.pdf --json   # full result as JSON
    python colorbar_inspector.py sheet.pdf --csv    # patch map as CSV
"""
import argparse
import json
import sys

import colorbar_core as core
from colorbar_core import candidate_csv


def print_summary(res):
    print(f"File: {res['file']}   Page: {res['page']['w_mm']} x {res['page']['h_mm']} mm")
    if not res["candidates"]:
        print("No linear color bar found.")
        return
    c = res["candidates"][0]
    vertical = c["orientation"] == "vertical"
    print(f"\nColor bar: {c['orientation']}")
    print(f"  Patch size: {c['patch_w_mm']:.3f} x {c['patch_h_mm']:.3f} mm"
          + ("  (across x along the strip)" if vertical else "  (width x height)"))
    print(f"  Pitch:      {c['pitch_mm']:.3f} mm")
    print(f"  Patches:    {c['patch_count']} in {c['slot_count']} slots"
          + (f" (+{c['subpatch_count']} stacked sub-patches)" if c["subpatch_count"] else ""))
    print(f"  Length:     {c['length_mm']:.2f} mm")
    m = c.get("corner_mark")
    if m:
        print(f"  Corner mark: {m['kind']} pattern, {m['size_mm']:.2f} mm, {m['gap_mm']:.2f} mm from the bar"
              + (" (bar is read from the patch next to it)" if m["applied"] else " (already at the start of the bar)"))
    print(f"  Reading:    {c['direction']} ({c['reference']})")
    print(f"  Starts at:  x {c['start_mm']['x']} mm from left, y {c['start_mm']['y']} mm from top")
    if c["empty_slots"]:
        print(f"  Empty slots: {c['empty_slots']}")
    for wmsg in c["warnings"]:
        print(f"  ! {wmsg}")
    print("\n  slot  offset(mm)  w x h (mm)         composition")
    for p in c["patches"]:
        s = "  - " if p["slot"] is None else f"{p['slot']:>4}"
        print(f"  {s}  {p['along_mm']:>9.3f}  {p['w_mm']:.3f} x {p['h_mm']:.3f}   {p['label']}"
              + ("  (stacked)" if p["sub"] else ""))
    ig = res["ignored"]
    print(f"\nIgnored: {ig['images']} images, {ig['text_runs']} text runs, "
          f"{ig['curved_or_irregular_shapes']} curved/irregular shapes, "
          f"{ig['production_ink_rectangles']} production-ink rectangles, "
          f"{ig.get('corner_marks', 0)} corner marks, "
          f"{ig['other_rectangles']} other rectangles.")



def main():
    ap = argparse.ArgumentParser(
        description="Find the linear color bar in a press-sheet PDF and report its patch size in mm.")
    ap.add_argument("pdf", help="PDF to inspect")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--csv", action="store_true", help="print the patch map as CSV")
    a = ap.parse_args()
    res = core.analyze(a.pdf)
    if a.json:
        print(json.dumps(res, indent=2))
    elif a.csv:
        if not res["candidates"]:
            sys.exit("No linear color bar found.")
        print(candidate_csv(res["candidates"][0]), end="")
    else:
        print_summary(res)


if __name__ == "__main__":
    main()
