"""Verifier for the cohort-retention chart.

Parses /app/retention.svg directly (no sidecar), reconstructs each plotted
series from marker/line geometry, calibrates pixel->data coordinates from the
axis tick labels, and checks the encoded values against the ground truth built
at task-authoring time (tests/ground_truth.json).

Requires the SVG to contain readable <text> (matplotlib rcParam
svg.fonttype='none'); the instruction mandates this.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SVG_PATH = Path(os.environ.get("RETENTION_SVG", "/app/retention.svg"))
GROUND_TRUTH = Path(__file__).with_name("ground_truth.json")

SVG_NS = "http://www.w3.org/2000/svg"
NS = {"s": SVG_NS}

Y_TOL_PP = 1.0     # percentage-point tolerance on retention (brackets pixel rounding)
X_TOL = 0.2        # week tolerance before rounding to integer k
GRID_COLOR = "#b0b0b0"


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def _tag(e):
    return e.tag.split("}")[-1]


def _stroke(style):
    m = re.search(r"stroke:\s*(#[0-9a-fA-F]{6})", style or "")
    return m.group(1).lower() if m else None


def _stroke_width(style):
    m = re.search(r"stroke-width:\s*([0-9.]+)", style or "")
    return float(m.group(1)) if m else None


def _find_id(root, target):
    for e in root.iter():
        if e.get("id") == target:
            return e
    return None


def _text_of(group):
    return "".join(t.text or "" for t in group.findall(".//s:text", NS)).strip()


def _linfit(pairs):
    """Least-squares fit pixel->data. pairs = [(pixel, data), ...]. Returns
    (slope, intercept) so data ≈ slope*pixel + intercept."""
    n = len(pairs)
    sx = sum(p for p, _ in pairs)
    sy = sum(d for _, d in pairs)
    sxx = sum(p * p for p, _ in pairs)
    sxy = sum(p * d for p, d in pairs)
    denom = n * sxx - sx * sx
    assert abs(denom) > 1e-9, "degenerate axis calibration"
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _axis_calibration(root, axis_id, tick_prefix):
    """Build a pixel->data mapping from numeric tick labels on one axis."""
    axis = _find_id(root, axis_id)
    assert axis is not None, f"missing {axis_id} (axis) in SVG"
    pairs = []
    for g in axis.iter():
        gid = g.get("id") or ""
        if not gid.startswith(tick_prefix):
            continue
        use = g.find(".//s:use", NS)
        label = _text_of(g)
        if use is None or not label:
            continue
        try:
            data = float(label.replace("%", ""))
        except ValueError:
            continue
        px = float(use.get("x")) if tick_prefix == "xtick" else float(use.get("y"))
        pairs.append((px, data))
    assert len(pairs) >= 2, (
        f"need >=2 numeric ticks to calibrate {axis_id}; got {len(pairs)}. "
        "Is svg.fonttype='none' set so tick labels are readable text?"
    )
    return _linfit(pairs)


def _parse_path_points(d):
    nums = re.findall(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?", d or "")
    pts = []
    for i in range(0, len(nums) - 1, 2):
        pts.append((float(nums[i]), float(nums[i + 1])))
    return pts


class Chart:
    def __init__(self, svg_text):
        self.root = ET.fromstring(svg_text)
        self.xslope, self.xint = _axis_calibration(
            self.root, "matplotlib.axis_1", "xtick"
        )
        self.yslope, self.yint = _axis_calibration(
            self.root, "matplotlib.axis_2", "ytick"
        )
        self.legend = self._parse_legend()
        self.legend_colors = {c for c, _ in self.legend}
        self.series = self._parse_series()

    def to_data(self, px, py):
        return self.xslope * px + self.xint, self.yslope * py + self.yint

    def _parse_legend(self):
        """Return ordered list of (color, label) top-to-bottom."""
        leg = _find_id(self.root, "legend_1")
        assert leg is not None, "missing legend (legend_1) in SVG"
        samples = []  # (y, color)
        labels = []   # (y, text)
        for ch in leg.iter():
            gid = ch.get("id") or ""
            if _tag(ch) == "g" and gid.startswith("line2d"):
                color = None
                for p in ch.iter():
                    c = _stroke(p.get("style"))
                    if c and c != GRID_COLOR:
                        color = c
                        break
                use = ch.find(".//s:use", NS)
                path = ch.find(".//s:path", NS)
                y = None
                if use is not None and use.get("y"):
                    y = float(use.get("y"))
                elif path is not None:
                    ys = [p[1] for p in _parse_path_points(path.get("d"))]
                    if ys:
                        y = sum(ys) / len(ys)
                if color and y is not None:
                    samples.append((y, color))
            elif _tag(ch) == "g" and gid.startswith("text"):
                txt = _text_of(ch)
                tnode = ch.find(".//s:text", NS)
                if txt and tnode is not None and tnode.get("y"):
                    labels.append((float(tnode.get("y")), txt))
        # Pair each sample with the nearest label by y.
        out = []
        used = set()
        for sy, color in sorted(samples):
            best, bestd = None, 1e18
            for i, (ly, txt) in enumerate(labels):
                if i in used:
                    continue
                d = abs(ly - sy)
                if d < bestd:
                    best, bestd = i, d
            if best is not None:
                used.add(best)
                out.append((color, labels[best][1]))
        return out

    def _parse_series(self):
        """color -> {'markers': [(x,y)...], 'line': [(x,y)...]} in data coords."""
        axes = _find_id(self.root, "axes_1")
        assert axes is not None, "missing axes_1 in SVG"
        series = {}
        for ch in list(axes):
            gid = ch.get("id") or ""
            if _tag(ch) != "g" or not gid.startswith("line2d"):
                continue
            # Identify the line path: colored, plot-weight (>1.0), color in legend.
            color = None
            line_pts = []
            for p in ch.findall(".//s:path", NS):
                st = p.get("style") or ""
                c = _stroke(st)
                w = _stroke_width(st) or 0.0
                if c and c in self.legend_colors and w >= 1.2:
                    color = c
                    line_pts = _parse_path_points(p.get("d"))
                    break
            if color is None:
                continue
            markers = []
            for u in ch.findall(".//s:use", NS):
                if u.get("x") is not None and u.get("y") is not None:
                    markers.append((float(u.get("x")), float(u.get("y"))))
            series[color] = {
                "markers": [self.to_data(x, y) for x, y in markers],
                "line": [self.to_data(x, y) for x, y in line_pts],
            }
        return series

    def vertices_for(self, color):
        """Data-coordinate vertices for a series: markers if present else line."""
        s = self.series.get(color)
        if s is None:
            return None
        pts = s["markers"] if s["markers"] else s["line"]
        # snap x to nearest integer week, keep the y value
        out = {}
        for x, y in pts:
            k = round(x)
            assert abs(x - k) <= X_TOL, f"marker x={x:.3f} not near an integer week"
            out[k] = y
        return out


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def gt():
    with GROUND_TRUTH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def chart():
    assert SVG_PATH.exists(), "/app/retention.svg was not created."
    text = SVG_PATH.read_text(encoding="utf-8")
    assert "svg" in text[:500].lower(), "output is not an SVG document"
    return Chart(text)


@pytest.fixture(scope="module")
def color_by_label(chart):
    m = {}
    for color, label in chart.legend:
        m[label] = color
    return m


# --------------------------------------------------------------------------
# Tests (each is a graded success criterion)
# --------------------------------------------------------------------------
def test_legend_labels_and_order(chart, gt):
    """(a) Correct set + chronological order of cohort labels, Blended last."""
    expected = [c["label"] for c in gt["cohorts"]] + ["Blended"]
    got = [label for _, label in chart.legend]
    assert got == expected, f"legend order/labels wrong.\n expected {expected}\n got {got}"
    colors = [c for c, _ in chart.legend]
    assert len(set(colors)) == len(colors), f"legend colors not unique: {colors}"


def test_cohort_curve_values(chart, gt, color_by_label):
    """(b)+(c) Every cohort vertex within ±1pp, and the k-set matches exactly
    (so each line terminates at its last observed week — no filled zeros)."""
    problems = []
    for c in gt["cohorts"]:
        label = c["label"]
        color = color_by_label.get(label)
        if color is None:
            problems.append(f"{label}: no legend color")
            continue
        verts = chart.vertices_for(color)
        if verts is None:
            problems.append(f"{label}: no plotted series for color {color}")
            continue
        expected = {int(k): v for k, v in c["points"]}
        if set(verts) != set(expected):
            problems.append(
                f"{label}: week set {sorted(verts)} != expected {sorted(expected)}"
            )
            continue
        for k, ev in expected.items():
            if abs(verts[k] - ev) > Y_TOL_PP:
                problems.append(
                    f"{label} k={k}: {verts[k]:.2f}% vs expected {ev:.2f}% (>1pp)"
                )
    assert not problems, "cohort curve errors:\n" + "\n".join(problems)


def test_entirely_censored_cohort(chart, gt, color_by_label):
    """(d) The entirely-censored cohort is a lone marker at (0, 100%)."""
    label = gt["entirely_censored_label"]
    color = color_by_label.get(label)
    assert color is not None, f"censored cohort {label} missing from legend"
    s = chart.series.get(color)
    assert s is not None, f"censored cohort {label} not plotted"
    assert len(s["markers"]) == 1, (
        f"censored cohort {label} must be a single marker, got {len(s['markers'])}"
    )
    x, y = s["markers"][0]
    assert abs(x - 0) <= X_TOL, f"censored marker x={x:.3f}, expected 0"
    assert abs(y - 100.0) <= Y_TOL_PP, f"censored marker y={y:.3f}, expected 100"
    assert len(s["line"]) <= 1, (
        f"censored cohort {label} must not draw a line to week 1 "
        f"(found {len(s['line'])} line vertices)"
    )


def test_blended_curve(chart, gt, color_by_label):
    """(e) Blended matches the observed-only size-weighted mean within ±1pp."""
    color = color_by_label.get("Blended")
    assert color is not None, "Blended series missing from legend"
    verts = chart.vertices_for(color)
    assert verts is not None, "Blended series not plotted"
    expected = {int(k): v for k, v in gt["blended"]["points"]}
    assert set(verts) == set(expected), (
        f"Blended week set {sorted(verts)} != expected {sorted(expected)}"
    )
    problems = []
    for k, ev in expected.items():
        if abs(verts[k] - ev) > Y_TOL_PP:
            problems.append(f"k={k}: {verts[k]:.2f}% vs expected {ev:.2f}% (>1pp)")
    assert not problems, "blended curve errors:\n" + "\n".join(problems)
