"""
colorbar_core - find the linear color bar in a press-sheet PDF.

Pure Python plus pypdf, so the same file runs under CPython (command line,
tests) and inside the browser through Pyodide (the public web page).

Main entry points
-----------------
analyze(source)          -> dict   source: file path, bytes, or binary file object
analyze_for_web(source)  -> dict   same, plus a CSV string for each candidate bar
candidate_csv(candidate) -> str

How the bar is found
--------------------
1. Every filled rectangle is collected with its position, size and ink.
   Shapes filled with production inks (dieline, registration "All", cut,
   crease ...) are set aside. Images, text and curved shapes are never bar
   patches.
2. Rectangles of the same size are grouped. A color bar shows up as a group
   whose members sit on one straight line, advance by a constant pitch, and
   use at least three different fills.
3. The best-scoring line wins. Smaller rectangles inside the bar's outline
   (for example a tint ladder stacked in one cell) are attached as
   sub-patches.
4. If a QR-style corner mark (three concentric squares in the ratio 7 : 5 : 3)
   sits in line with the bar, just past one end, the patch next to the mark
   becomes the first patch and the bar is read away from it.
"""
import csv
import io
import math
import os
import re
import statistics
from collections import Counter

from pypdf import PdfReader
from pypdf.generic import ContentStream

__version__ = "1.1.0"

PT2MM = 25.4 / 72.0
MM2PT = 72.0 / 25.4

# --------------------------------------------------------------------------
# Matrices
# --------------------------------------------------------------------------


def mmul(m, n):
    """Matrix that applies m first, then n."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def mapply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# --------------------------------------------------------------------------
# Inks and color spaces
# --------------------------------------------------------------------------

PROCESS_NAMES = {"cyan": "C", "magenta": "M", "yellow": "Y", "black": "K"}
PROCESS_RGB = {
    "C": (0, 174, 239),
    "M": (236, 0, 140),
    "Y": (255, 242, 0),
    "K": (35, 31, 32),
}
PRODUCTION_RE = re.compile(
    r"die|cutter|^cut|crease|perf|fold|bleed|safe|trim|guide|regist|^all$|"
    r"dimension|technical|keyline",
    re.I,
)
COATING_RE = re.compile(r"varnish|opv|coat|gloss|matte|lacquer|aqueous|seal", re.I)
FALLBACK_SPOT_RGB = [(230, 134, 46), (63, 163, 77), (139, 92, 246), (200, 60, 90), (60, 130, 200)]


def ink_role(name):
    low = name.lower().strip()
    if low in PROCESS_NAMES:
        return "process"
    if low == "none":
        return "none"
    if PRODUCTION_RE.search(low):
        return "production"
    if COATING_RE.search(low):
        return "coating"
    if "white" in low:
        return "white"
    return "spot"


def _floats(arr, default):
    try:
        return [float(x) for x in arr]
    except Exception:
        return default


def _fn_info(fn_obj):
    """Only exponential interpolation (type 2) is evaluated; others fall back."""
    try:
        fn = fn_obj.get_object()
        if int(fn.get("/FunctionType", -1)) == 2:
            return {
                "type": 2,
                "c0": _floats(fn.get("/C0", [0.0]), [0.0]),
                "c1": _floats(fn.get("/C1", [1.0]), [1.0]),
                "n": float(fn.get("/N", 1)),
            }
    except Exception:
        pass
    return None


def parse_colorspace(obj):
    """Return a small dict describing a PDF color space."""
    try:
        obj = obj.get_object()
    except Exception:
        pass
    if isinstance(obj, str):
        name = str(obj).lstrip("/")
        table = {
            "DeviceGray": "DeviceGray", "G": "DeviceGray", "CalGray": "DeviceGray",
            "DeviceRGB": "DeviceRGB", "RGB": "DeviceRGB", "CalRGB": "DeviceRGB",
            "DeviceCMYK": "DeviceCMYK", "CMYK": "DeviceCMYK",
        }
        return {"type": table.get(name, "Other"), "names": [], "alt": None, "fn": None}
    if isinstance(obj, list) and obj:
        kind = str(obj[0]).lstrip("/")
        if kind == "Separation" and len(obj) >= 4:
            return {
                "type": "Separation",
                "names": [str(obj[1]).lstrip("/")],
                "alt": parse_colorspace(obj[2])["type"],
                "fn": _fn_info(obj[3]),
            }
        if kind == "DeviceN" and len(obj) >= 4:
            names = [str(n).lstrip("/") for n in obj[1].get_object()]
            return {
                "type": "DeviceN",
                "names": names,
                "alt": parse_colorspace(obj[2])["type"],
                "fn": _fn_info(obj[3]),
            }
        if kind == "ICCBased" and len(obj) >= 2:
            try:
                n = int(obj[1].get_object().get("/N", 3))
            except Exception:
                n = 3
            return {"type": {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(n, "Other"),
                    "names": [], "alt": None, "fn": None}
        if kind in ("CalGray",):
            return {"type": "DeviceGray", "names": [], "alt": None, "fn": None}
        if kind in ("CalRGB",):
            return {"type": "DeviceRGB", "names": [], "alt": None, "fn": None}
    return {"type": "Other", "names": [], "alt": None, "fn": None}


DEVICE_CS = {
    "DeviceGray": {"type": "DeviceGray", "names": [], "alt": None, "fn": None},
    "DeviceRGB": {"type": "DeviceRGB", "names": [], "alt": None, "fn": None},
    "DeviceCMYK": {"type": "DeviceCMYK", "names": [], "alt": None, "fn": None},
}


def initial_values(cs):
    t = cs["type"]
    if t == "DeviceCMYK":
        return [0.0, 0.0, 0.0, 1.0]
    if t == "DeviceRGB":
        return [0.0, 0.0, 0.0]
    if t == "DeviceGray":
        return [0.0]
    if t in ("Separation", "DeviceN"):
        return [1.0] * max(1, len(cs["names"]))
    return [0.0]


# --------------------------------------------------------------------------
# Fill decoding and preview colors
# --------------------------------------------------------------------------


def mix_process(c, m, y, k):
    """Subtractive mix of the four process inks on white (0..1 in, 0..255 out)."""
    rgb = [1.0, 1.0, 1.0]
    for key, t in (("C", c), ("M", m), ("Y", y), ("K", k)):
        t = max(0.0, min(1.0, t))
        for i in range(3):
            rgb[i] *= 1.0 - t * (1.0 - PROCESS_RGB[key][i] / 255.0)
    return tuple(v * 255.0 for v in rgb)


def sep_rgb(cs, tint, spot_index):
    """Approximate preview color of a spot ink at a given tint (0..1)."""
    fn = cs.get("fn")
    if fn and fn["type"] == 2 and cs.get("alt") in ("DeviceCMYK", "DeviceRGB", "DeviceGray"):
        t = max(0.0, min(1.0, tint)) ** fn["n"]
        vals = [a + t * (b - a) for a, b in zip(fn["c0"], fn["c1"])]
        if cs["alt"] == "DeviceCMYK" and len(vals) >= 4:
            return mix_process(*vals[:4])
        if cs["alt"] == "DeviceRGB" and len(vals) >= 3:
            return tuple(v * 255.0 for v in vals[:3])
        if cs["alt"] == "DeviceGray" and vals:
            return (vals[0] * 255.0,) * 3
    base = FALLBACK_SPOT_RGB[spot_index % len(FALLBACK_SPOT_RGB)]
    return tuple(255.0 - tint * (255.0 - v) for v in base)


class Fill:
    __slots__ = ("cs", "vals")

    def __init__(self, cs, vals):
        self.cs = cs
        self.vals = vals


def decode_fill(fill):
    """-> (inks, flags). inks: list of (key, tint 0..1); key is C/M/Y/K or 'spot:<name>'."""
    cs, vals = fill.cs, fill.vals
    t = cs["type"]
    inks, flags = [], set()
    eps = 0.0005
    if t == "DeviceCMYK" and len(vals) >= 4:
        for key, v in zip("CMYK", vals[:4]):
            if v > eps:
                inks.append((key, v))
    elif t == "DeviceGray" and vals:
        if 1.0 - vals[0] > eps:
            inks.append(("K", 1.0 - vals[0]))
    elif t == "DeviceRGB":
        flags.add("rgb")
    elif t in ("Separation", "DeviceN"):
        for name, v in zip(cs["names"], vals):
            if v <= eps:
                continue
            role = ink_role(name)
            if role == "process":
                inks.append((PROCESS_NAMES[name.lower().strip()], v))
            elif role == "none":
                continue
            elif role == "production":
                flags.add("production")
                inks.append(("spot:" + name, v))
            else:
                if role == "coating":
                    flags.add("coating")
                inks.append(("spot:" + name, v))
    else:
        flags.add("other")
    return inks, flags


def fill_signature(fill):
    inks, flags = decode_fill(fill)
    sig = tuple(sorted((k, round(v, 3)) for k, v in inks))
    if "rgb" in flags:
        sig = ("rgb",) + tuple(round(v, 3) for v in fill.vals)
    return sig, flags


def fill_preview_rgb(fill, spot_order):
    inks, flags = decode_fill(fill)
    cs, vals = fill.cs, fill.vals
    if cs["type"] == "DeviceRGB" and len(vals) >= 3:
        return tuple(v * 255.0 for v in vals[:3])
    rgb = [1.0, 1.0, 1.0]
    for key, tint in inks:
        if key.startswith("spot:"):
            name = key[5:]
            idx = spot_order.index(name) if name in spot_order else 0
            single = cs["type"] == "Separation"
            comp = sep_rgb(cs if single else {"fn": None, "alt": None}, tint, idx)
        else:
            comp = mix_process(*(tint if key == k else 0.0 for k in "CMYK"))
        for i in range(3):
            rgb[i] *= comp[i] / 255.0
    return tuple(v * 255.0 for v in rgb)


def hexrgb(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


# --------------------------------------------------------------------------
# Content stream interpreter (collects filled rectangles)
# --------------------------------------------------------------------------


class GState:
    __slots__ = ("ctm", "fill")

    def __init__(self, ctm=IDENTITY, fill=None):
        self.ctm = ctm
        self.fill = fill or Fill(DEVICE_CS["DeviceGray"], [0.0])

    def copy(self):
        return GState(self.ctm, Fill(self.fill.cs, list(self.fill.vals)))


PAINT_FILL = {"f", "F", "f*", "B", "B*", "b", "b*"}
CLOSE_PAINT = {"b", "b*", "s"}


def _simplify_polygon(pts, tol=0.02):
    """Drop a closing duplicate and collinear vertices, so a rectangle that was
    written with extra points (for example starting mid-edge) is still a rectangle."""
    pts = list(pts)
    if len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) < tol and abs(pts[0][1] - pts[-1][1]) < tol:
        pts.pop()
    changed = True
    while changed and len(pts) > 3:
        changed = False
        n = len(pts)
        for i in range(n):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
            dx, dy = c[0] - a[0], c[1] - a[1]
            length = math.hypot(dx, dy)
            if length < 1e-9:
                dist = math.hypot(b[0] - a[0], b[1] - a[1])
            else:
                dist = abs((b[0] - a[0]) * dy - (b[1] - a[1]) * dx) / length
            if dist < tol:
                del pts[i]
                changed = True
                break
    return pts


class Collector:
    def __init__(self, reader):
        self.reader = reader
        self.rects = []  # dicts with x0,y0,x1,y1 (pt, user space) and fill
        self.stats = Counter()
        self._cs_cache = {}

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _get(d, key):
        try:
            v = d.get(key)
            return v.get_object() if v is not None else None
        except Exception:
            return None

    def _lookup_cs(self, name, resources):
        key = (id(resources), str(name))
        if key in self._cs_cache:
            return self._cs_cache[key]
        n = str(name).lstrip("/")
        if n in DEVICE_CS:
            cs = DEVICE_CS[n]
        else:
            table = self._get(resources, "/ColorSpace") or {}
            entry = table.get("/" + n) if hasattr(table, "get") else None
            cs = parse_colorspace(entry) if entry is not None else {"type": "Other", "names": [], "alt": None, "fn": None}
        self._cs_cache[key] = cs
        return cs

    # -- main loop --------------------------------------------------------
    def run(self, ops, resources, gs, depth=0):
        stack = []
        subpaths = []
        cur = None
        for operands, op in ops:
            op = op.decode("latin1") if isinstance(op, bytes) else str(op)

            if op == "q":
                stack.append(gs.copy())
            elif op == "Q":
                if stack:
                    gs = stack.pop()
            elif op == "cm":
                try:
                    gs.ctm = mmul(tuple(float(x) for x in operands), gs.ctm)
                except Exception:
                    pass

            # colors
            elif op == "g":
                gs.fill = Fill(DEVICE_CS["DeviceGray"], [float(operands[0])])
            elif op == "rg":
                gs.fill = Fill(DEVICE_CS["DeviceRGB"], [float(x) for x in operands[:3]])
            elif op == "k":
                gs.fill = Fill(DEVICE_CS["DeviceCMYK"], [float(x) for x in operands[:4]])
            elif op == "cs":
                cs = self._lookup_cs(operands[0], resources)
                gs.fill = Fill(cs, initial_values(cs))
            elif op in ("sc", "scn"):
                vals = []
                for x in operands:
                    try:
                        vals.append(float(x))
                    except Exception:
                        pass
                if vals:
                    gs.fill = Fill(gs.fill.cs, vals)

            # path construction
            elif op == "re":
                x, y, w, h = (float(v) for v in operands[:4])
                subpaths.append({"pts": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], "curved": False})
                cur = None
            elif op == "m":
                cur = {"pts": [(float(operands[0]), float(operands[1]))], "curved": False}
                subpaths.append(cur)
            elif op == "l":
                if cur is not None:
                    cur["pts"].append((float(operands[0]), float(operands[1])))
            elif op in ("c", "v", "y"):
                if cur is not None:
                    cur["curved"] = True
            elif op == "h":
                pass

            # painting
            elif op in PAINT_FILL or op in CLOSE_PAINT:
                if op in PAINT_FILL or op == "b":
                    self._paint(subpaths, gs)
                subpaths, cur = [], None
            elif op in ("S", "s", "n"):
                subpaths, cur = [], None

            # other content
            elif op in ("Tj", "TJ", "'", '"'):
                self.stats["text"] += 1
            elif op in ("BI", "INLINE IMAGE"):
                self.stats["images"] += 1
            elif op == "sh":
                self.stats["shadings"] += 1
            elif op == "Do":
                self._do(operands, resources, gs, depth)

    def _do(self, operands, resources, gs, depth):
        xobjs = self._get(resources, "/XObject") or {}
        name = str(operands[0])
        ref = xobjs.get(name) if hasattr(xobjs, "get") else None
        if ref is None:
            return
        try:
            xo = ref.get_object()
            sub = str(xo.get("/Subtype", ""))
        except Exception:
            return
        if sub == "/Image":
            self.stats["images"] += 1
            return
        if sub != "/Form" or depth > 12:
            return
        matrix = _floats(xo.get("/Matrix", list(IDENTITY)), list(IDENTITY))
        if len(matrix) != 6:
            matrix = list(IDENTITY)
        new_gs = gs.copy()
        new_gs.ctm = mmul(tuple(matrix), gs.ctm)
        res = self._get(xo, "/Resources") or resources
        try:
            ops = ContentStream(xo, self.reader).operations
        except Exception:
            return
        self.stats["forms"] += 1
        self.run(ops, res, new_gs, depth + 1)

    def _paint(self, subpaths, gs):
        for sp in subpaths:
            pts = sp["pts"]
            if sp["curved"]:
                self.stats["curved_shapes"] += 1
                continue
            tp = _simplify_polygon([mapply(gs.ctm, x, y) for x, y in pts])
            if len(tp) != 4:
                self.stats["other_shapes"] += 1
                continue
            xs = [p[0] for p in tp]
            ys = [p[1] for p in tp]
            bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
            tol = 0.02
            ok = all(
                (abs(p[0] - bx0) < tol or abs(p[0] - bx1) < tol)
                and (abs(p[1] - by0) < tol or abs(p[1] - by1) < tol)
                for p in tp
            ) and len({(round(p[0], 1), round(p[1], 1)) for p in tp}) == 4
            if not ok or bx1 - bx0 < 0.05 or by1 - by0 < 0.05:
                self.stats["other_shapes"] += 1
                continue
            self.rects.append(
                {"x0": bx0, "y0": by0, "x1": bx1, "y1": by1, "fill": Fill(gs.fill.cs, list(gs.fill.vals))}
            )


# --------------------------------------------------------------------------
# Bar detection
# --------------------------------------------------------------------------


def _cluster(values, tol):
    """Greedy 1-D clustering of (value, item) pairs; returns list of item lists."""
    values = sorted(values, key=lambda p: p[0])
    groups, last = [], None
    for v, item in values:
        if last is not None and v - last <= tol:
            groups[-1].append(item)
        else:
            groups.append([item])
        last = v
    return groups


def evaluate_line(members, axis):
    """members: rect dicts of equal size on one line. axis 'x' (horizontal) or 'y' (vertical)."""
    key = "x0" if axis == "x" else "top"
    size_key = "w" if axis == "x" else "h"
    members = sorted(members, key=lambda r: r[key])
    # drop duplicates sitting on the same position
    uniq = []
    for r in members:
        if uniq and abs(r[key] - uniq[-1][key]) < 0.5:
            continue
        uniq.append(r)
    members = uniq
    if len(members) < 4:
        return None
    starts = [r[key] for r in members]
    steps = [b - a for a, b in zip(starts, starts[1:])]
    size_along = statistics.mean(r[size_key] for r in members)
    base = min(steps)
    if base < size_along * 0.95:
        return None
    # refine the base pitch with the single-slot steps
    singles = [s for s in steps if abs(s - base) <= max(0.6, 0.02 * base)]
    base = statistics.mean(singles)
    conform = 0
    for s in steps:
        k = max(1, round(s / base))
        if abs(s - k * base) <= max(0.8, 0.02 * base):
            conform += 1
    if conform < 0.8 * len(steps):
        return None
    sigs = {r["sig"] for r in members}
    if len(sigs) < 3:
        return None
    return {
        "members": members,
        "pitch": base,
        "conform": conform / len(steps),
        "distinct": len(sigs),
        "axis": axis,
    }


def find_bars(rects, page_w, page_h, include_rgb=False):
    elig = []
    for r in rects:
        w, h = r["x1"] - r["x0"], r["y1"] - r["y0"]
        r["w"], r["h"] = w, h
        r["top"] = page_h - r["y1"]
        if min(w, h) < 1.5 * MM2PT or max(w, h) > 60 * MM2PT:
            continue
        if "production" in r["flags"] or "other" in r["flags"]:
            continue
        if "rgb" in r["flags"] and not include_rgb:
            continue
        elig.append(r)

    # cluster by size (both dimensions within ~0.2 mm)
    clusters = []
    for r in elig:
        for c in clusters:
            if abs(c["w"] - r["w"]) < 0.6 and abs(c["h"] - r["h"]) < 0.6:
                c["items"].append(r)
                break
        else:
            clusters.append({"w": r["w"], "h": r["h"], "items": [r]})

    found = []
    for c in clusters:
        if len(c["items"]) < 4:
            continue
        for axis, cross in (("x", "top"), ("y", "x0")):
            for line in _cluster([(r[cross], r) for r in c["items"]], 1.0):
                ev = evaluate_line(line, axis)
                if ev:
                    ev["size"] = (c["w"], c["h"])
                    ev["score"] = len(ev["members"]) + 2 * ev["distinct"]
                    found.append(ev)
    found.sort(key=lambda e: -e["score"])
    return found


# A QR finder pattern is a 7x7 square with a 5x5 light square and a 3x3 square inside it,
# all sharing one centre. The ratios below allow for small drawing tolerances.
MARK_MIN_MM, MARK_MAX_MM = 1.5, 15.0
MARK_MIDDLE_RATIO = (1.28, 1.52)   # outer / middle, ideal 7/5 = 1.40
MARK_INNER_RATIO = (2.10, 2.60)    # outer / inner,  ideal 7/3 = 2.33


def find_corner_marks(rects):
    """Find QR-style corner marks: concentric squares in the ratio 7 : 5 : 3.

    A mark whose light middle square was not drawn as a shape (a frame with a hole)
    is accepted with just the outer and inner squares (kind 'partial'). A mark only
    matters when it also sits in line with a color bar, which is checked later."""
    squares = []
    for r in rects:
        w, h = r["x1"] - r["x0"], r["y1"] - r["y0"]
        # the minimum size applies to the whole mark (its outer square), not to each layer
        if abs(w - h) <= max(0.3, 0.02 * w) and 0.5 * MM2PT <= w <= MARK_MAX_MM * MM2PT:
            squares.append({"cx": (r["x0"] + r["x1"]) / 2, "cy": (r["y0"] + r["y1"]) / 2,
                            "s": (w + h) / 2, "r": r})
    squares.sort(key=lambda q: -q["s"])
    marks, used = [], set()
    for o in squares:
        if id(o["r"]) in used or o["s"] < MARK_MIN_MM * MM2PT:
            continue
        tol = max(0.35, 0.03 * o["s"])

        def nested(q, lo, hi):
            return (q is not o and id(q["r"]) not in used
                    and abs(q["cx"] - o["cx"]) <= tol and abs(q["cy"] - o["cy"]) <= tol
                    and lo <= o["s"] / q["s"] <= hi)

        mids = [q for q in squares if nested(q, *MARK_MIDDLE_RATIO)]
        inns = [q for q in squares if nested(q, *MARK_INNER_RATIO)]
        if not inns:
            continue
        parts = [o, inns[0]] + (mids[:1])
        for q in parts:
            used.add(id(q["r"]))
        marks.append({"cx": o["cx"], "cy": o["cy"], "size": o["s"],
                      "kind": "finder" if mids else "partial", "rects": [q["r"] for q in parts]})
    return marks


def build_candidate(ev, all_rects, page_w, page_h, cid, marks=()):
    axis = ev["axis"]
    members = ev["members"]
    mem_ids = {id(r) for r in members}

    bx0 = min(r["x0"] for r in members)
    bx1 = max(r["x1"] for r in members)
    by0 = min(r["y0"] for r in members)
    by1 = max(r["y1"] for r in members)

    # sub-patches: smaller shapes lying fully inside the bar's outline
    subs = []
    seen = {(round(r["x0"], 1), round(r["y0"], 1), round(r["x1"], 1), round(r["y1"], 1)) for r in members}
    for r in all_rects:
        if id(r) in mem_ids or "production" in r["flags"] or "other" in r["flags"]:
            continue
        w, h = r["x1"] - r["x0"], r["y1"] - r["y0"]
        if min(w, h) < 1.0 * MM2PT:
            continue
        if r["x0"] >= bx0 - 0.5 and r["x1"] <= bx1 + 0.5 and r["y0"] >= by0 - 0.5 and r["y1"] <= by1 + 0.5:
            k = (round(r["x0"], 1), round(r["y0"], 1), round(r["x1"], 1), round(r["y1"], 1))
            if k in seen:
                continue
            seen.add(k)
            subs.append(r)

    everything = [(r, False) for r in members] + [(r, True) for r in subs]
    # bar bounding box in page-top coordinates
    top0 = page_h - by1
    left0 = bx0
    horizontal = axis == "x"
    pitch = ev["pitch"]
    along_len_pt = (bx1 - bx0) if horizontal else (by1 - by0)
    cross_len_pt = (by1 - by0) if horizontal else (bx1 - bx0)

    # A corner mark in line with the bar and just past one end decides where the bar starts.
    near = []
    for m in marks:
        if horizontal:
            a_c, c_off = m["cx"] - bx0, (by0 + by1) / 2 - m["cy"]
        else:
            a_c, c_off = by1 - m["cy"], m["cx"] - (bx0 + bx1) / 2
        half = m["size"] / 2
        if abs(c_off) > max(1.0 * MM2PT, 0.3 * cross_len_pt):
            continue                                   # not in line with the bar
        if a_c - half >= along_len_pt - 0.5:
            side, gap = "end", a_c - half - along_len_pt
        elif a_c + half <= 0.5:
            side, gap = "start", -(a_c + half)
        else:
            continue                                   # overlaps the bar
        if gap > max(3 * pitch, 15 * MM2PT):
            continue                                   # too far away to belong to it
        near.append((gap, side, m, c_off))
    near.sort(key=lambda t: t[0])
    both_ends = len({t[1] for t in near}) == 2
    mark = None if (both_ends or not near) else near[0]
    reverse = bool(mark and mark[1] == "end")

    def along(r):
        if horizontal:
            return (bx1 - r["x1"]) if reverse else (r["x0"] - bx0)
        return (r["y0"] - by0) if reverse else ((page_h - r["y1"]) - top0)

    def cross(r):
        return ((page_h - r["y1"]) - top0) if axis == "x" else (r["x0"] - bx0)

    everything.sort(key=lambda p: (round(along(p[0]), 1), round(cross(p[0]), 1)))

    # spot numbering by first appearance along the bar
    spot_names = []
    for r, _ in everything:
        inks, _f = decode_fill(r["fill"])
        for key, _t in inks:
            if key.startswith("spot:") and key[5:] not in spot_names:
                spot_names.append(key[5:])
    spot_label = {n: "S%d" % (i + 1) for i, n in enumerate(spot_names)}

    patches = []
    for r, is_sub in everything:
        inks, flags = decode_fill(r["fill"])
        w, h = r["x1"] - r["x0"], r["y1"] - r["y0"]
        a, c = along(r), cross(r)
        ink_out, parts = [], []
        for key, t in inks:
            lab = spot_label[key[5:]] if key.startswith("spot:") else key
            pct = int(round(t * 100))
            ink_out.append({"ink": lab, "tint": pct})
            parts.append(f"{lab}{pct}" if len(lab) == 1 else f"{lab} {pct}")
        if "rgb" in flags:
            label = "RGB fill"
        elif not parts:
            label = "Paper (no ink)"
        else:
            label = " ".join(parts)
        rgb = fill_preview_rgb(r["fill"], spot_names)
        a_len, c_len = (w, h) if axis == "x" else (h, w)
        slot = int(round(a / pitch)) if not is_sub else None
        patches.append({
            "slot": slot,
            "sub": is_sub,
            "x_mm": round((r["x0"] - bx0) * PT2MM, 3),
            "y_mm": round((page_h - r["y1"] - top0) * PT2MM, 3),
            "w_mm": round(w * PT2MM, 3),
            "h_mm": round(h * PT2MM, 3),
            "along_mm": round(a * PT2MM, 3),
            "cross_mm": round(c * PT2MM, 3),
            "len_mm": round(a_len * PT2MM, 3),
            "thick_mm": round(c_len * PT2MM, 3),
            "label": label,
            "inks": ink_out,
            "paper": (not parts and "rgb" not in flags),
            "coating": "coating" in flags,
            "rgb": hexrgb(rgb),
        })

    slots_used = sorted({p["slot"] for p in patches if p["slot"] is not None})
    slot_count = (slots_used[-1] + 1) if slots_used else 0
    empty = [s for s in range(slot_count) if s not in slots_used]
    pw, ph = ev["size"]
    bar_w = (bx1 - bx0) * PT2MM
    bar_h = (by1 - by0) * PT2MM

    spots = []
    for n in spot_names:
        role = ink_role(n)
        spots.append({"label": spot_label[n], "role": role, "pdf_name": n})

    warnings = []
    for s in spots:
        if s["role"] == "coating":
            warnings.append(f"{s['label']} is a coating plate, not a printing ink. A densitometer usually does not read it, so you may want to leave its patches out of the template.")
        if s["role"] == "white":
            warnings.append(f"{s['label']} is a white ink.")
    plain = [p for p in patches if not p["sub"]]
    solids = [tuple(i["ink"] for i in p["inks"]) for p in plain]
    if (len(plain) >= 4 and all(len(p["inks"]) == 1 and p["inks"][0]["tint"] == 100 for p in plain)
            and len(set(solids)) == len(solids)):
        warnings.append("This looks like an ink legend (one 100% swatch per ink), not a measurement bar.")
    if any(p["label"] == "RGB fill" for p in plain):
        warnings.append("These patches are filled with RGB values, not printing inks, so this may not be a measurement bar.")
    if both_ends:
        warnings.append("A corner mark sits at both ends of the bar, so the reading order was left unchanged.")
    if ev["conform"] < 1.0:
        warnings.append("Patch spacing is not perfectly regular; some gaps are not whole patch widths.")
    black_like = [p for p in patches if not p["sub"] and len(p["inks"]) == 1 and p["inks"][0]["ink"] in "CMYK"]
    for ink in "CMYK":
        tints = sorted({p["inks"][0]["tint"] for p in black_like if p["inks"][0]["ink"] == ink})
        odd = [t for t in tints if t % 5 != 0]
        if odd:
            warnings.append(f"{ink} has a patch at {odd[0]}% — check that this is the intended tint.")

    if horizontal:
        direction = "right to left" if reverse else "left to right"
        start_pt = (bx1 if reverse else bx0, top0)
    else:
        direction = "bottom to top" if reverse else "top to bottom"
        start_pt = (bx0, (page_h - by0) if reverse else top0)

    corner = None
    if mark:
        gap, side, m, c_off = mark
        corner = {
            "found": True,
            "kind": m["kind"],
            "side": side,                     # which end of the default order it was found at
            "applied": reverse,               # True when the bar was read from the mark's end
            "size_mm": round(m["size"] * PT2MM, 3),
            "module_mm": round(m["size"] / 7 * PT2MM, 3),
            "gap_mm": round(gap * PT2MM, 3),  # from the mark to the nearest patch
            "center_mm": {"x": round(m["cx"] * PT2MM, 2), "y": round((page_h - m["cy"]) * PT2MM, 2)},
            "along_mm": round(-(gap + m["size"]) * PT2MM, 3),   # in the reading frame, before slot 0
            "cross_mm": round((cross_len_pt / 2 + c_off - m["size"] / 2) * PT2MM, 3),
        }

    return {
        "id": cid,
        "orientation": "horizontal" if axis == "x" else "vertical",
        "direction": direction,
        "reference": "corner mark" if mark else "default order",
        "start_mm": {"x": round(start_pt[0] * PT2MM, 2), "y": round(start_pt[1] * PT2MM, 2)},
        "corner_mark": corner,
        "patch_w_mm": round(pw * PT2MM, 3),
        "patch_h_mm": round(ph * PT2MM, 3),
        "pitch_mm": round(pitch * PT2MM, 3),
        "patch_count": len([p for p in patches if not p["sub"]]),
        "subpatch_count": len([p for p in patches if p["sub"]]),
        "slot_count": slot_count,
        "empty_slots": empty,
        "length_mm": round(slot_count * pitch * PT2MM, 3),
        "origin_mm": {"x": round(left0 * PT2MM, 2), "y": round(top0 * PT2MM, 2)},
        "bbox_mm": {"w": round(bar_w, 3), "h": round(bar_h, 3)},
        "along_len_mm": round((bar_w if axis == "x" else bar_h), 3),
        "cross_len_mm": round((bar_h if axis == "x" else bar_w), 3),
        "bbox_pt": {"x0": bx0, "y0": by0, "x1": bx1, "y1": by1},
        "score": ev["score"],
        "spots": spots,
        "patches": patches,
        "warnings": warnings,
    }


def _open(source):
    if isinstance(source, (bytes, bytearray)):
        return PdfReader(io.BytesIO(bytes(source))), "document.pdf"
    if hasattr(source, "read"):
        return PdfReader(source), getattr(source, "name", "document.pdf")
    return PdfReader(str(source)), os.path.basename(str(source))


def analyze(source):
    reader, name = _open(source)
    page = reader.pages[0]
    box = page.cropbox if page.get("/CropBox") is not None else page.mediabox
    llx, lly = float(box.left), float(box.bottom)
    page_w, page_h = float(box.width), float(box.height)
    rot = int(page.get("/Rotate", 0) or 0) % 360

    col = Collector(reader)
    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else {}
    contents = page.get_contents()
    ops = contents.operations if contents is not None else []
    col.run(ops, resources, GState())

    rects = []
    seen = set()
    production = 0
    for r in col.rects:
        r = dict(r)
        r["x0"] -= llx
        r["x1"] -= llx
        r["y0"] -= lly
        r["y1"] -= lly
        r["sig"], r["flags"] = fill_signature(r["fill"])
        key = (round(r["x0"], 2), round(r["y0"], 2), round(r["x1"], 2), round(r["y1"], 2), r["sig"])
        if key in seen:
            continue
        seen.add(key)
        if "production" in r["flags"]:
            production += 1
        rects.append(r)

    marks = find_corner_marks(rects)
    found = find_bars(rects, page_w, page_h)
    if not found:
        found = find_bars(rects, page_w, page_h, include_rgb=True)

    candidates, used = [], []
    for ev in found:
        # skip lines that are mostly the same shapes as a better one
        ids = {id(m) for m in ev["members"]}
        if any(len(ids & u) > 0.5 * len(ids) for u in used):
            continue
        used.append(ids)
        candidates.append(build_candidate(ev, rects, page_w, page_h, len(candidates), marks))
        if len(candidates) >= 4:
            break

    bar_shapes = sum(c["patch_count"] + c["subpatch_count"] for c in candidates[:1])
    ignored = {
        "images": col.stats["images"],
        "text_runs": col.stats["text"],
        "curved_or_irregular_shapes": col.stats["curved_shapes"] + col.stats["other_shapes"],
        "production_ink_rectangles": production,
        "corner_marks": len(marks),
        "other_rectangles": max(0, len(rects) - production - bar_shapes - sum(len(m["rects"]) for m in marks)),
    }
    result = {
        "file": os.path.basename(str(name)),
        "page": {"w_mm": round(page_w * PT2MM, 2), "h_mm": round(page_h * PT2MM, 2), "rotation": rot},
        "rectangles_found": len(rects),
        "ignored": ignored,
        "candidates": candidates,
    }
    if rot:
        result["note"] = "The page is rotated; orientation is reported in unrotated page coordinates."
    return result



# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def candidate_csv(cand):
    spots = [s["label"] for s in cand["spots"]]
    cols = ["C", "M", "Y", "K"] + spots
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["slot", "stacked", "offset_along_mm", "offset_across_mm", "length_mm", "thickness_mm", "label"] + cols)
    for p in cand["patches"]:
        vals = {i["ink"]: i["tint"] for i in p["inks"]}
        w.writerow([
            "" if p["slot"] is None else p["slot"],
            "yes" if p["sub"] else "",
            p["along_mm"], p["cross_mm"], p["len_mm"], p["thick_mm"], p["label"],
        ] + [vals.get(c, 0) for c in cols])
    return out.getvalue()



def analyze_for_web(source):
    """analyze() plus a ready-to-download CSV string on every candidate."""
    res = analyze(source)
    res["engine_version"] = __version__
    for c in res["candidates"]:
        c["csv"] = candidate_csv(c)
    return res
