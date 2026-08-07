"""Renders a schema-v2 report dict into one self-contained HTML file.

Reads `templates/report.html` (a token-templated skeleton) and substitutes
`{{TOKEN}}` placeholders with strings built from the report dict. Stdlib
only — no jinja2, no external templating, no network access from the
rendered output.

Two kinds of placeholder live in the template:

- scalar tokens: a plain `{{TOKEN}}` sitting where one escaped value belongs.
- block regions: `<!-- {{X_START}} --> ... <!-- {{X_END}} -->` markers,
  used for the fixed 9-tab nav/panels skeleton.

Every visible Russian label, metric definition, glossary entry, risk text
and warning message is read from the report dict (`report["labels"]`,
`report["metric_defs"]`, `report["glossary"]`, `report["risks"]`, and the
`*_ru` fields inside each section) — this module hardcodes only chrome that
has no JSON counterpart: the 9 tab names, a handful of column headers, and
the long-form section explanatory paragraphs mandated verbatim by the
product spec (`.research/v3-redesign/SPEC.md` §D), which the JSON schema
does not carry.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "report.html"

EM_DASH = "—"
MINUS = "−"

TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")

SUPPORTED_SCHEMA_VERSION = 2


class TemplateError(Exception):
    """Raised when the template is missing a marker/token this generator needs,
    or the report dict is missing a §B key the renderer requires."""


# --------------------------------------------------------------------------
# Escaping / formatting primitives
# --------------------------------------------------------------------------


def esc(value: Any) -> str:
    """HTML-escapes any value bound for text or an attribute. Every value
    that ultimately comes from Jira/GitLab (summaries, names, labels,
    projects, error strings) must go through this — never interpolated raw."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def esc_or_dash(value: Any) -> str:
    if value is None or value == "":
        return EM_DASH
    return esc(value)


def fmt_num(value: Optional[float], decimals: int = 2) -> str:
    """Rounds to `decimals`, drops a trailing `.0`/`.00`. None, NaN and
    Inf (both can arrive via `json.loads` on a corrupted report.json) all
    render as the standard no-data dash instead of raising."""
    if value is None:
        return EM_DASH
    try:
        f = round(float(value), decimals)
    except (TypeError, ValueError):
        return EM_DASH
    if math.isnan(f) or math.isinf(f):
        return EM_DASH
    if f == int(f):
        return str(int(f))
    s = f"{f:.{decimals}f}"
    s = s.rstrip("0").rstrip(".")
    return s


def fmt_int(value: Optional[float]) -> str:
    if value is None:
        return EM_DASH
    try:
        f = float(value)
    except (TypeError, ValueError):
        return EM_DASH
    if math.isnan(f) or math.isinf(f):
        return EM_DASH
    return str(int(f))


def fmt_pct(value: Optional[float], decimals: int = 1) -> str:
    if value is None:
        return EM_DASH
    return fmt_num(value, decimals) + "%"


def bool_ru(value: Optional[bool]) -> str:
    if value is None:
        return EM_DASH
    return "да" if value else "нет"


def ru_plural(n: float, one: str, few: str, many: str) -> str:
    """Picks the Russian noun form for a count: 1 (not 11) -> `one`,
    2-4 (not 12-14) -> `few`, everything else -> `many`."""
    n_abs = abs(int(n))
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return few
    return many


def fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return EM_DASH
    return str(iso)[:10]


def fmt_datetime(iso: Optional[str]) -> str:
    if not iso:
        return EM_DASH
    s = str(iso).replace("T", " ").replace("Z", " UTC")
    return s


def fmt_range(start: Optional[str], end: Optional[str]) -> str:
    if not start and not end:
        return EM_DASH
    return f"{fmt_date(start)} – {fmt_date(end)}"


def truncate(text: str, n: int, ellipsis: str = "…") -> str:
    text = str(text)
    if len(text) <= n:
        return text
    return text[: max(0, n - len(ellipsis))] + ellipsis


_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")


def auto_code_wrap(escaped_text: str) -> str:
    """Wraps any snake_case machine-key-shaped token in `<code>` — applied to
    already-`esc()`-ed body text (glossary/risk/comment/hint prose the JSON
    supplies verbatim) so a field name mentioned in free text never renders
    as a bare identifier outside `<code>`. Never apply to attribute values —
    `<code>` cannot nest inside a `title="..."`."""
    return _SNAKE_RE.sub(lambda m: f"<code>{m.group(0)}</code>", escaped_text)


def id_safe(key: str) -> str:
    """Turns a JSON snake_case key into a hyphenated id/class fragment so it
    never surfaces as a bare snake_case token in an `id="..."` attribute."""
    return re.sub(r"[^a-zA-Z0-9-]+", "-", str(key)).replace("_", "-")


# --------------------------------------------------------------------------
# Template block plumbing (unchanged mechanics — reused from v1)
# --------------------------------------------------------------------------


def _marker(token: str) -> str:
    return f"<!-- {{{{{token}}}}} -->"


_SCALAR_MARKER_RE = re.compile(r"<!--\s*\{\{([A-Z0-9_]+)\}\}\s*-->")


def strip_scalar_marker_comments(text: str) -> str:
    """Drops the documentation-only `<!-- {{TOKEN}} -->` comment that
    precedes each scalar placeholder, keeping only the real `{{TOKEN}}`
    substitution point that follows it. Block region markers (`..._START`
    / `..._END`) are left in place."""

    def _rep(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name.endswith("_START") or name.endswith("_END"):
            return m.group(0)
        return ""

    return _SCALAR_MARKER_RE.sub(_rep, text)


def extract_inner(text: str, start_tok: str, end_tok: str) -> str:
    start = _marker(start_tok)
    end = _marker(end_tok)
    try:
        i = text.index(start) + len(start)
        j = text.index(end, i)
    except ValueError as e:
        raise TemplateError(f"template is missing block markers for {start_tok}/{end_tok}") from e
    return text[i:j]


def replace_block(text: str, start_tok: str, end_tok: str, content: str) -> str:
    start = _marker(start_tok)
    end = _marker(end_tok)
    try:
        i = text.index(start)
        j = text.index(end, i) + len(end)
    except ValueError as e:
        raise TemplateError(f"template is missing block markers for {start_tok}/{end_tok}") from e
    return text[:i] + content + text[j:]


def sub_tokens(template: str, mapping: dict) -> str:
    """Substitutes every {{KEY}} in a small item sub-template. Raises if the
    sub-template references a key the caller forgot to supply."""

    def _rep(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key not in mapping:
            raise TemplateError(f"unresolved token in item sub-template: {{{{{key}}}}}")
        return mapping[key]

    return TOKEN_RE.sub(_rep, template)


def render_loop(text: str, start_tok: str, end_tok: str, items: Iterable[Any], render_item: Callable[[str, Any], str]) -> str:
    inner = extract_inner(text, start_tok, end_tok)
    rendered = "".join(render_item(inner, item) for item in items)
    return replace_block(text, start_tok, end_tok, rendered)


def blank_block_markers(text: str, *token_pairs: tuple) -> str:
    for start_tok, end_tok in token_pairs:
        text = text.replace(_marker(start_tok), "", 1)
        text = text.replace(_marker(end_tok), "", 1)
    return text


def finalize(template: str, ctx: dict) -> str:
    def _rep(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key not in ctx:
            raise TemplateError(f"unresolved token: {{{{{key}}}}}")
        return ctx[key]

    return TOKEN_RE.sub(_rep, template)


# --------------------------------------------------------------------------
# Chart geometry helpers
# --------------------------------------------------------------------------


def nice_step(value: float, n_ticks: int) -> float:
    """Smallest 'round' step (1/2/2.5/5/10 x 10^k) such that `step * n_ticks`
    is an axis max at or above `value`."""
    if n_ticks <= 0:
        n_ticks = 1
    if value <= 0:
        return 1.0
    rough = value / n_ticks
    magnitude = 10 ** math.floor(math.log10(rough))
    for m in (1, 2, 2.5, 5, 10):
        candidate = m * magnitude
        if candidate >= rough - 1e-9:
            return float(candidate)
    return float(10 * magnitude)


def axis_label_decimals(values: list, max_decimals: int = 6) -> int:
    """Smallest decimal count that keeps every value in `values` visually
    distinct once rounded — a fixed decimals=0 collapses adjacent gridline
    labels whenever the axis step is smaller than 1 (or, for a collapsed
    value range, smaller than the base rounding resolution)."""
    for d in range(0, max_decimals + 1):
        rounded = [round(v, d) for v in values]
        if len(rounded) == len(set(rounded)):
            return d
    return max_decimals


def linspace(lo: float, hi: float, n: int) -> list:
    if n <= 1:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def ols_trend(points: list) -> tuple:
    """(k, b) of the least-squares line y = k*x + b over (x, y) points.
    Frozen formula — mirrors the aiIntegrationMetrics source's
    `_linear_regression` (report_generator.py:126-137) exactly:
    x̄ = Σxᵢ/n, ȳ = Σyᵢ/n, k = Σ(xᵢ−x̄)(yᵢ−ȳ) / Σ(xᵢ−x̄)², b = ȳ − k·x̄;
    n < 2 ⇒ k=0, b=y0 (or 0); zero denominator ⇒ k=0, b=ȳ."""
    n = len(points)
    if n < 2:
        b = points[0][1] if points else 0.0
        return 0.0, b
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    den = sum((x - xbar) ** 2 for x in xs)
    if den == 0:
        return 0.0, ybar
    k = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / den
    b = ybar - k * xbar
    return k, b


def spark_status_class(status: str) -> str:
    return {"good": "good", "warn": "warn", "bad": "bad", "none": "mute"}.get(status, "mute")


def build_spark_svg(values: list, status_class: str) -> str:
    """Small tile sparkline. `values` must already have None entries
    filtered out by the caller — handles 0/1/N points and a flat series
    without dividing by zero."""
    if not values:
        return (
            '<svg class="spark" viewBox="0 0 100 32" preserveAspectRatio="none" '
            'aria-hidden="true" focusable="false"></svg>'
        )
    lo, hi = min(values), max(values)

    def y_of(v: float) -> float:
        if hi == lo:
            return 16.0
        return 27.0 - (v - lo) / (hi - lo) * 22.0

    if len(values) == 1:
        y = y_of(values[0])
        return (
            '<svg class="spark" viewBox="0 0 100 32" preserveAspectRatio="none" '
            'aria-hidden="true" focusable="false">'
            f'<circle class="sp-dot-{status_class}" cx="98" cy="{y:.2f}" r="2.4"/></svg>'
        )
    xs = linspace(2.0, 98.0, len(values))
    pts = " ".join(f"{x:.2f},{y_of(v):.2f}" for x, v in zip(xs, values))
    last_x, last_y = xs[-1], y_of(values[-1])
    return (
        '<svg class="spark" viewBox="0 0 100 32" preserveAspectRatio="none" '
        'aria-hidden="true" focusable="false">'
        f'<polyline class="sp-line sp-{status_class}" vector-effect="non-scaling-stroke" points="{pts}"/>'
        f'<circle class="sp-dot-{status_class}" cx="{last_x:.2f}" cy="{last_y:.2f}" r="2.4"/></svg>'
    )


PALETTE_CLASSES = [f"s{i}" for i in range(10)]


def _series_class(i: int) -> str:
    return PALETTE_CLASSES[i % len(PALETTE_CLASSES)]


# --------------------------------------------------------------------------
# §G new SVG builders
# --------------------------------------------------------------------------


def _donut_full_ring_path(cx: float, cy: float, R: float, inr: float) -> str:
    """A full annulus, drawn as two half-arcs per ring so no single arc
    command has coincident start/end points — per SVG 1.1 F.6.2 an arc
    whose endpoints are identical (a 100%, or a 0%-after-filtering, share)
    is dropped by the renderer entirely, leaving an empty donut."""
    return (
        f"M {cx - R:.1f} {cy:.1f} A {R} {R} 0 1 1 {cx + R:.1f} {cy:.1f} "
        f"A {R} {R} 0 1 1 {cx - R:.1f} {cy:.1f} Z "
        f"M {cx - inr:.1f} {cy:.1f} A {inr} {inr} 0 1 1 {cx + inr:.1f} {cy:.1f} "
        f"A {inr} {inr} 0 1 1 {cx - inr:.1f} {cy:.1f} Z"
    )


def build_donut_svg(chart_id: str, title: str, items: list, center_label: str) -> Optional[str]:
    """items = [(label, value), ...]. Returns None when the total is zero —
    the caller (`chart_block`) renders the standard empty-data box."""
    vals = [(str(label), float(v)) for label, v in items if v is not None and v > 0]
    total = sum(v for _, v in vals)
    if total <= 0:
        return None

    cx, cy, R, inr = 170, 140, 105, 58
    W, H_base = 380, 320
    start_y = 245
    legend_items = [(label, v, _series_class(i)) for i, (label, v) in enumerate(vals)]

    legend_rows = 1
    lx = 16
    for label, v, _cls in legend_items:
        text = f"{label}: {fmt_num(v, 1)} ({v / total * 100:.1f}%)"
        w = 14 + len(text) * 6.2
        if lx + w > W - 16:
            lx = 16
            legend_rows += 1
        lx += w
    H = H_base + 18 * (legend_rows - 1) if legend_rows > 1 else H_base

    parts = [
        f'<svg id="{esc(chart_id)}" class="chart" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
    ]
    start = -90.0
    single_segment = len(legend_items) == 1
    for label, v, cls in legend_items:
        frac = v / total
        slice_title = f"{label}: {fmt_num(v, 1)} ({v / total * 100:.1f}%)"
        if single_segment:
            d = _donut_full_ring_path(cx, cy, R, inr)
            parts.append(f'<path class="{cls}" fill-rule="evenodd" d="{d}"><title>{esc(slice_title)}</title></path>')
            continue
        end = start + 360 * frac
        large = 1 if (360 * frac) > 180 else 0
        a0, a1 = math.radians(start), math.radians(end)
        x0, y0 = cx + R * math.cos(a0), cy + R * math.sin(a0)
        x1, y1 = cx + R * math.cos(a1), cy + R * math.sin(a1)
        xi0, yi0 = cx + inr * math.cos(a0), cy + inr * math.sin(a0)
        xi1, yi1 = cx + inr * math.cos(a1), cy + inr * math.sin(a1)
        d = (
            f"M {xi0:.1f} {yi0:.1f} L {x0:.1f} {y0:.1f} A {R} {R} 0 {large} 1 {x1:.1f} {y1:.1f} "
            f"L {xi1:.1f} {yi1:.1f} A {inr} {inr} 0 {large} 0 {xi0:.1f} {yi0:.1f} Z"
        )
        parts.append(f'<path class="{cls}" d="{d}"><title>{esc(slice_title)}</title></path>')
        start = end

    lx, ly = 16, start_y
    for label, v, cls in legend_items:
        text = f"{label}: {fmt_num(v, 1)} ({v / total * 100:.1f}%)"
        w = 14 + len(text) * 6.2
        if lx + w > W - 16:
            lx = 16
            ly += 18
        parts.append(f'<rect class="{cls}" x="{lx:.1f}" y="{ly}" width="12" height="12" rx="3"/>')
        parts.append(f'<text class="c-legend-text" x="{lx + 14:.1f}" y="{ly + 10}">{esc(text)}</text>')
        lx += w

    parts.append(f'<text class="c-donut-value" x="{cx}" y="{cy - 4}" text-anchor="middle">{esc(center_label)}</text>')
    parts.append(f'<text class="c-donut-caption" x="{cx}" y="{cy + 16}" text-anchor="middle">всего</text>')
    parts.append("</svg>")
    return "".join(parts)


def build_hbar_svg(chart_id: str, title: str, items: list, unit: str = "") -> Optional[str]:
    """items = [(label, value_or_none), ...] — a None row prints «нет данных»."""
    n = len(items)
    if n == 0:
        return None
    W, row_h = 560, 30
    H = max(80, 44 + n * row_h + 12)
    label_x = 170
    plot_w = W - label_x - 70
    vals = [v for _, v in items if v is not None]
    vmax = max(vals) if vals else 1
    if vmax <= 0:
        vmax = 1

    rows = []
    for i, (label, value) in enumerate(items):
        full = str(label)
        short = truncate(full, 16)
        y = 34 + i * row_h
        cls = _series_class(i)
        if value is None:
            rows.append(
                f'<text class="c-hbar-label" x="{label_x - 8}" y="{y + 13}" text-anchor="end">'
                f'<title>{esc(full)}</title>{esc(short)}</text>'
                f'<text class="c-hbar-nodata" x="{label_x}" y="{y + 13}">нет данных</text>'
            )
            continue
        bar_w = max((value / vmax) * plot_w, 2)
        val_txt = f"{fmt_num(value, 1)} {unit}".strip()
        rows.append(
            f'<text class="c-hbar-label" x="{label_x - 8}" y="{y + 13}" text-anchor="end">'
            f'<title>{esc(full)}</title>{esc(short)}</text>'
            f'<rect class="{cls}" x="{label_x}" y="{y}" width="{bar_w:.1f}" height="18" rx="3">'
            f'<title>{esc(full)}: {esc(val_txt)}</title></rect>'
            f'<text class="c-hbar-value" x="{label_x + bar_w + 6:.1f}" y="{y + 14}">{esc(val_txt)}</text>'
        )
    return (
        f'<svg id="{esc(chart_id)}" class="chart" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">' + "".join(rows) + "</svg>"
    )


def build_grouped_bar_svg(chart_id: str, title: str, cat_labels: list, series: list, unit: str = "") -> Optional[str]:
    """series = [(name_ru, [values_by_category]), ...]."""
    n_cats = len(cat_labels)
    n_series = len(series)
    if n_cats == 0 or n_series == 0:
        return None
    W = max(380, 120 + n_cats * 96)
    bottom = 200
    plot_h = bottom - 46 - 16
    left = 40
    chart_w = W - left - 24
    xstep = chart_w / max(n_cats, 1)
    bar_w = max((xstep - 18) / max(n_series, 1), 6)

    all_vals = [v for _n, vals in series for v in vals if v is not None]
    vmax = max(all_vals) if all_vals else 1
    if vmax <= 0:
        vmax = 1

    parts = [
        f'<svg id="{esc(chart_id)}" class="chart" viewBox="0 0 {W} 252" width="{W}" height="252" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
    ]
    parts.append(f'<line class="c-axis" x1="{left}" y1="{bottom}" x2="{W - 12}" y2="{bottom}"/>')
    for i, cat in enumerate(cat_labels):
        xg = left + i * xstep
        for j, (sname, vals) in enumerate(series):
            v = vals[i] if i < len(vals) else None
            if v is None:
                continue
            bh = max((v / vmax) * plot_h, 2)
            x = xg + j * bar_w + 4
            y = bottom - bh
            cls = _series_class(j)
            val_txt = fmt_num(v, 1)
            bar_title = f"{sname} · {cat}: {val_txt} {unit}".strip()
            parts.append(
                f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{max(bar_w - 8, 4):.1f}" '
                f'height="{bh:.1f}" rx="4"><title>{esc(bar_title)}</title></rect>'
            )
            parts.append(f'<text class="c-bar-value" x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle">{esc(val_txt)}</text>')
        label = str(cat)
        short = truncate(label, 12)
        parts.append(
            f'<text class="c-tick-label" x="{xg + xstep / 2:.1f}" y="{bottom + 14}" text-anchor="middle">'
            f'<title>{esc(label)}</title>{esc(short)}</text>'
        )

    ly, lx = 230, 8
    for j, (sname, _vals) in enumerate(series):
        cls = _series_class(j)
        parts.append(f'<rect class="{cls}" x="{lx}" y="{ly}" width="10" height="10" rx="2"/>')
        parts.append(f'<text class="c-legend-text" x="{lx + 14}" y="{ly + 9}">{esc(sname)}</text>')
        lx += 34 + len(sname) * 5.5
    parts.append("</svg>")
    return "".join(parts)


def build_stacked_bar_svg(chart_id: str, title: str, cat_labels: list, series: list, unit: str = "", ref_line: Optional[float] = None) -> Optional[str]:
    """series = [(name_ru, [values_by_category]), ...], segments stacked
    bottom-up in series order. Maps the full range of partial sums reached
    while stacking (not just each category's final total), so a segment
    with a negative running sum still lands inside the viewBox instead of
    the fixed zero-at-bottom assumption pushing it off-canvas."""
    n_cats = len(cat_labels)
    if n_cats == 0 or not series:
        return None
    W = max(380, 120 + n_cats * 96)
    bottom = 200
    plot_h = bottom - 46 - 16
    left = 40
    chart_w = W - left - 24
    xstep = chart_w / max(n_cats, 1)
    bar_w = max(xstep - 24, 10)

    extents = [0.0] + ([ref_line] if ref_line is not None else [])
    for i in range(n_cats):
        cum = 0.0
        for _name, vals in series:
            v = vals[i] if i < len(vals) else None
            if v is not None:
                cum += v
                extents.append(cum)
    vmin, vmax = min(extents), max(extents)
    if vmax == vmin:
        vmax = vmin + 1

    def y_of(v: float) -> float:
        return bottom - (v - vmin) / (vmax - vmin) * plot_h

    zero_y = y_of(0.0)

    parts = [
        f'<svg id="{esc(chart_id)}" class="chart" viewBox="0 0 {W} 252" width="{W}" height="252" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
    ]
    parts.append(f'<line class="c-axis" x1="{left}" y1="{zero_y:.1f}" x2="{W - 12}" y2="{zero_y:.1f}"/>')
    for i, cat in enumerate(cat_labels):
        xg = left + i * xstep + (xstep - bar_w) / 2
        cum = 0.0
        for j, (sname, vals) in enumerate(series):
            v = vals[i] if i < len(vals) else None
            if not v:
                continue
            y0, y1 = y_of(cum), y_of(cum + v)
            top, h = min(y0, y1), max(abs(y0 - y1), 1)
            cls = _series_class(j)
            val_txt = fmt_num(v, 1)
            bar_title = f"{sname} · {cat}: {val_txt} {unit}".strip()
            parts.append(f'<rect class="{cls}" x="{xg:.1f}" y="{top:.1f}" width="{bar_w:.1f}" height="{h:.1f}"><title>{esc(bar_title)}</title></rect>')
            cum += v
        label = str(cat)
        short = truncate(label, 12)
        parts.append(
            f'<text class="c-tick-label" x="{xg + bar_w / 2:.1f}" y="{bottom + 14}" text-anchor="middle">'
            f'<title>{esc(label)}</title>{esc(short)}</text>'
        )
    if ref_line is not None:
        ry = y_of(ref_line)
        parts.append(f'<line class="c-marker" x1="{left}" y1="{ry:.1f}" x2="{W - 12}" y2="{ry:.1f}"/>')
        parts.append(f'<text class="c-marker-label" x="{W - 12}" y="{ry - 4:.1f}" text-anchor="end">{fmt_num(ref_line, 0)}</text>')

    ly, lx = 230, 8
    for j, (sname, _vals) in enumerate(series):
        cls = _series_class(j)
        parts.append(f'<rect class="{cls}" x="{lx}" y="{ly}" width="10" height="10" rx="2"/>')
        parts.append(f'<text class="c-legend-text" x="{lx + 14}" y="{ly + 9}">{esc(sname)}</text>')
        lx += 34 + len(sname) * 5.5
    parts.append("</svg>")
    return "".join(parts)


def build_vbar_svg(
    chart_id: str,
    title: str,
    cat_labels: list,
    values: list,
    unit: str = "",
    ref_line: Optional[float] = None,
    ref_lines: Optional[list] = None,
) -> Optional[str]:
    """Single-series vertical bars. `ref_lines` (list of (value, label_text))
    is the extension used by the forecast histogram to draw P50/P85/P95 at
    once; `ref_line` is the single-value convenience form used elsewhere."""
    n = len(values)
    if n == 0:
        return None
    W, H = 640, 260
    x0, x1 = 50.0, W - 16.0
    y_top, y_base = 20.0, 210.0
    nums = [v for v in values if v is not None]
    all_refs = [r for r, _l in (ref_lines or [])] + ([ref_line] if ref_line is not None else [])
    candidates = nums + [r for r in all_refs if r is not None]
    n_ticks = 5
    step_val = nice_step(max(candidates) if candidates else 1.0, n_ticks)
    top = step_val * n_ticks

    def y_of(v: float) -> float:
        return y_base - (v / top) * (y_base - y_top) if top else y_base

    spacing = (x1 - x0) / max(n, 1)
    bar_w = max(spacing * 0.6, 4)
    xs = [x0 + i * spacing + (spacing - bar_w) / 2 for i in range(n)]

    grid = "".join(f'<line class="c-grid" x1="{x0}" y1="{y_of(step_val * i):.1f}" x2="{x1}" y2="{y_of(step_val * i):.1f}"/>' for i in range(1, n_ticks + 1))
    y_tick_decimals = axis_label_decimals([step_val * i for i in range(0, n_ticks + 1)])
    y_labels = "".join(
        f'<text class="c-unit-label" x="{x0 - 8}" y="{y_of(step_val * i) + 4:.1f}" text-anchor="end">{fmt_num(step_val * i, y_tick_decimals)}</text>'
        for i in range(0, n_ticks + 1)
    )

    bars = []
    for x, cat, v in zip(xs, cat_labels, values):
        label = str(cat)
        short = truncate(label, 14)
        if v is None:
            bars.append(f'<text class="c-tick-label" x="{x + bar_w / 2:.1f}" y="{y_base + 16:.0f}" text-anchor="middle"><title>{esc(label)}</title>{esc(short)}</text>')
            continue
        h = y_base - y_of(v)
        val_txt = fmt_num(v, 1)
        bar_title = f"{label}: {val_txt} {unit}".strip()
        bars.append(
            f'<rect class="c-bar" x="{x:.2f}" y="{y_of(v):.2f}" width="{bar_w:.2f}" height="{max(h, 1):.2f}" rx="2">'
            f'<title>{esc(bar_title)}</title></rect>'
        )
        bars.append(f'<text class="c-tick-label" x="{x + bar_w / 2:.1f}" y="{y_base + 16:.0f}" text-anchor="middle"><title>{esc(label)}</title>{esc(short)}</text>')

    ref_html_parts = []
    if ref_line is not None:
        ry = y_of(ref_line)
        ref_html_parts.append(f'<line class="c-marker" x1="{x0}" y1="{ry:.1f}" x2="{x1}" y2="{ry:.1f}"/>')
        ref_html_parts.append(f'<text class="c-marker-label" x="{x1}" y="{ry - 4:.1f}" text-anchor="end">{fmt_num(ref_line, 0)}</text>')
    for rv, rlabel in ref_lines or []:
        if rv is None:
            continue
        ry = y_of(rv)
        ref_html_parts.append(f'<line class="c-marker" x1="{x0}" y1="{ry:.1f}" x2="{x1}" y2="{ry:.1f}"/>')
        ref_html_parts.append(f'<text class="c-marker-label" x="{x1}" y="{ry - 4:.1f}" text-anchor="end">{esc(rlabel)}</text>')

    return (
        f'<svg id="{esc(chart_id)}" class="chart" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
        f"<g>{grid}</g><g>{y_labels}</g>"
        f'<line class="c-axis" x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}"/>'
        + "".join(bars)
        + "".join(ref_html_parts)
        + "</svg>"
    )


def build_multiline_svg(
    chart_id: str,
    title: str,
    labels: list,
    series: list,
    unit: str = "",
    ref_lines: tuple = (),
    show_trend: bool = True,
    trend_values: Optional[list] = None,
) -> Optional[str]:
    """series = [(name_ru, [values_or_none_by_category]), ...]."""
    n = len(labels)
    has_any = any(v is not None for _n, vals in series for v in vals)
    if n == 0 or not series or not has_any:
        return None

    left, right, top = 46.0, 14.0, 42.0
    W = 640

    legend_rows = 1
    lx = left
    for sname, _vals in series:
        w = 24 + len(sname) * 5.6
        if lx + w > W - right:
            lx = left
            legend_rows += 1
        lx += w
    if legend_rows > 1:
        H = 320 + 16 * (legend_rows - 1)
        bottom = 44 + 16 * (legend_rows - 1)
    else:
        H = 320
        bottom = 44
    plot_h = H - top - bottom
    plot_w = W - left - right

    all_vals = [v for _n, vals in series for v in vals if v is not None] + [r for r in ref_lines if r is not None]
    vmin, vmax = min(all_vals), max(all_vals)
    span = (vmax - vmin) or 1
    ymin = vmin - span * 0.1
    ymax = vmax + span * 0.15
    if ymin < 0 and all(v >= 0 for v in all_vals):
        ymin = 0
    if ymax == ymin:
        ymax = ymin + 1

    def X(i: int) -> float:
        return left + plot_w * i / max(n - 1, 1)

    def Y(v: float) -> float:
        return top + plot_h * (1 - (v - ymin) / (ymax - ymin))

    parts = [
        f'<svg id="{esc(chart_id)}" class="chart" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
    ]
    grid_values = [ymin + (ymax - ymin) * k / 4 for k in range(5)]
    grid_decimals = axis_label_decimals(grid_values)
    for v in grid_values:
        y = Y(v)
        parts.append(f'<line class="c-grid" x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}"/>')
        parts.append(f'<text class="c-unit-label" x="{left - 6}" y="{y + 3:.1f}" text-anchor="end">{fmt_num(v, grid_decimals)}</text>')

    for i, lab in enumerate(labels):
        txt = str(lab)
        short = truncate(txt, 14)
        parts.append(f'<text class="c-tick-label" x="{X(i):.1f}" y="{H - bottom + 14}" text-anchor="middle"><title>{esc(txt)}</title>{esc(short)}</text>')

    for rv in ref_lines:
        if rv is None:
            continue
        ry = Y(rv)
        parts.append(f'<line class="c-marker" x1="{left}" y1="{ry:.1f}" x2="{W - right}" y2="{ry:.1f}"/>')
        parts.append(f'<text class="c-marker-label" x="{W - right}" y="{ry - 4:.1f}" text-anchor="end">{fmt_num(rv, 0)}</text>')

    for si, (sname, vals) in enumerate(series):
        cls = _series_class(si)
        pts = [(i, X(i), Y(v)) for i, v in enumerate(vals) if v is not None]
        if len(pts) >= 2:
            d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for _i, x, y in pts)
            parts.append(f'<path class="{cls} c-line" d="{d}"/>')
        for i, x, y in pts:
            val_txt = fmt_num(vals[i], 1)
            pt_title = f"{labels[i]} — {sname}: {val_txt} {unit}".strip()
            parts.append(f'<circle class="{cls} c-point" cx="{x:.1f}" cy="{y:.1f}" r="3.5"><title>{esc(pt_title)}</title></circle>')
            if len(series) == 1 and n <= 12:
                parts.append(f'<text class="c-point-label" x="{x:.1f}" y="{y - 6:.1f}" text-anchor="middle">{esc(val_txt)}</text>')

    if show_trend:
        if trend_values is not None:
            idx_vals = list(enumerate(trend_values))
        elif series:
            idx_vals = list(enumerate(series[0][1]))
        else:
            idx_vals = []
        pts_for_ols = [(i, v) for i, v in idx_vals if v is not None]
        if len(pts_for_ols) >= 2:
            k_, b_ = ols_trend(pts_for_ols)
            y0 = Y(k_ * 0 + b_)
            y1 = Y(k_ * (n - 1) + b_)
            parts.append(f'<line class="c-trend" x1="{left:.1f}" y1="{y0:.1f}" x2="{left + plot_w:.1f}" y2="{y1:.1f}"/>')

    ly, lx = H - 8, left
    for si, (sname, _vals) in enumerate(series):
        cls = _series_class(si)
        w = 24 + len(sname) * 5.6
        if lx + w > W - right:
            lx = left
            ly -= 16
        parts.append(f'<rect class="{cls}" x="{lx}" y="{ly - 9}" width="10" height="10" rx="2"/>')
        parts.append(f'<text class="c-legend-text" x="{lx + 14}" y="{ly}">{esc(sname)}</text>')
        lx += w
    parts.append("</svg>")
    return "".join(parts)


def chart_block(chart_id: str, title: str, svg_or_none: Optional[str], hint_html: str) -> str:
    inner = svg_or_none if svg_or_none is not None else '<p class="empty">Недостаточно данных для построения графика.</p>'
    return (
        f'<div class="chartbox card" id="cb-{esc(chart_id)}"><h3>{esc(title)}</h3>{inner}'
        f'<p class="hint">{hint_html}</p></div>'
    )


def table_hint(table_html: str, hint_html: str) -> str:
    return f'{table_html}<p class="hint">{hint_html}</p>'


def empty_state_html(text: str = "Нет данных.") -> str:
    return f'<p class="empty">{esc(text)}</p>'


# --------------------------------------------------------------------------
# Burndown (bespoke — kept close to the v1 hand-built chart)
# --------------------------------------------------------------------------

_WEEKDAY_RU_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _parse_date(date_str: str) -> tuple:
    y, mo, d = (int(x) for x in date_str.split("-"))
    return y, mo, d


def _weekday(date_str: str) -> int:
    y, mo, d = _parse_date(date_str)
    return _dt.date(y, mo, d).weekday()


def build_burndown_svg(chart_id: str, points: list, unit: str) -> tuple:
    """unit is "items" or "sp". Returns (svg, has_weekend)."""
    key_remain = "remaining_items" if unit == "items" else "remaining_sp"
    key_ideal = "ideal_items" if unit == "items" else "ideal_sp"
    unit_label = "задачи" if unit == "items" else "story points"
    axis_title = "Остаток, задачи" if unit == "items" else "Остаток, story points"

    x0, x1 = 56.0, 730.0
    y_top, y_base = 24.0, 288.0
    n = len(points)
    if n == 0:
        svg = (
            '<svg class="chart-svg" viewBox="0 0 760 344" role="img" aria-label="нет данных">'
            '<text class="c-axis-title" x="380" y="172" text-anchor="middle">нет данных</text></svg>'
        )
        return svg, False

    remaining = [p[key_remain] for p in points]
    ideal = [p[key_ideal] for p in points]
    n_ticks = 6
    step_val = nice_step(max(max(remaining), max(ideal), 1.0), n_ticks)
    top = step_val * n_ticks

    def y_of(v: float) -> float:
        return y_base - (v / top) * (y_base - y_top)

    xs = linspace(x0, x1, n)
    day_width = (x1 - x0) / (n - 1) if n > 1 else (x1 - x0) / 10.0

    grid = "".join(f'<line x1="{x0}" y1="{y_of(step_val * i):.1f}" x2="{x1}" y2="{y_of(step_val * i):.1f}"/>' for i in range(1, n_ticks + 1))
    y_tick_decimals = axis_label_decimals([step_val * i for i in range(0, n_ticks + 1)])
    y_labels = "".join(f'<text x="{x0 - 10}" y="{y_of(step_val * i) + 4:.1f}">{fmt_num(step_val * i, y_tick_decimals)}</text>' for i in range(0, n_ticks + 1))

    weekend_rects = []
    for x, p in zip(xs, points):
        if _weekday(p["date"]) >= 5:
            left = max(x - day_width / 2, x0)
            right = min(x + day_width / 2, x1)
            weekend_rects.append(f'<rect class="c-weekend" x="{left:.1f}" y="{y_top}" width="{right - left:.1f}" height="{y_base - y_top:.1f}"/>')
    weekend_html = "".join(weekend_rects)

    x_ticks = "".join(f'<line x1="{x:.2f}" y1="{y_base}" x2="{x:.2f}" y2="{y_base + 5}"/>' for x in xs)
    x_labels = "".join(f'<text x="{x:.2f}" y="{y_base + 18}">{_parse_date(p["date"])[2]:02d}</text>' for x, p in zip(xs, points))
    x_sub = "".join(f'<text x="{x:.2f}" y="{y_base + 29}">{_WEEKDAY_RU_SHORT[_weekday(p["date"])]}</text>' for x, p in zip(xs, points))

    ideal_path = "M " + " L ".join(f"{x:.2f} {y_of(v):.2f}" for x, v in zip(xs, ideal))
    area_path = (
        "M " + " L ".join(f"{x:.2f} {y_of(v):.2f}" for x, v in zip(xs, remaining))
        + f" L {xs[-1]:.2f} {y_base} L {xs[0]:.2f} {y_base} Z"
    )
    actual_pts = " ".join(f"{x:.2f},{y_of(v):.2f}" for x, v in zip(xs, remaining))

    dots = []
    for x, p, v in zip(xs, points, remaining):
        title = f"{p['date']} — остаток {fmt_num(v, 1)} {unit_label}"
        dots.append(f'<circle cx="{x:.2f}" cy="{y_of(v):.2f}" r="3.2"><title>{esc(title)}</title></circle>')

    last_x, last_y = xs[-1], y_of(remaining[-1])
    callout = (
        f'<g><line class="c-callout" x1="{last_x:.2f}" y1="{last_y:.2f}" x2="{last_x:.2f}" y2="{y_base}"/>'
        f'<text class="c-callout-text" x="{last_x - 8:.2f}" y="{(last_y + y_base) / 2:.2f}" text-anchor="end">'
        f"осталось {fmt_num(remaining[-1], 1)} {unit_label}</text></g>"
    )

    grad_id = f"grad-{esc(chart_id)}"
    aria = f"Burndown, остаток в {unit_label}: " + ", ".join(fmt_num(v, 1) for v in remaining)
    dots_html = "".join(dots)

    svg = (
        f'<svg id="{esc(chart_id)}" class="chart-svg" viewBox="0 0 760 344" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="{esc(aria)}">'
        f'<defs><linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">'
        '<stop class="c-area-top" offset="0%"/><stop class="c-area-bottom" offset="100%"/></linearGradient></defs>'
        f"{weekend_html}"
        f'<g class="c-grid">{grid}</g>'
        f'<g class="c-unit-label" text-anchor="end">{y_labels}</g>'
        f'<text class="c-axis-title" x="16" y="{(y_top + y_base) / 2:.0f}" text-anchor="middle" '
        f'transform="rotate(-90 16 {(y_top + y_base) / 2:.0f})">{esc(axis_title)}</text>'
        f'<line class="c-axis" x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}"/>'
        f'<line class="c-axis" x1="{x0}" y1="{y_top}" x2="{x0}" y2="{y_base}"/>'
        f'<g class="c-tick">{x_ticks}</g>'
        f'<g class="c-tick-label" text-anchor="middle">{x_labels}</g>'
        f'<g class="c-tick-sub" text-anchor="middle">{x_sub}</g>'
        f'<text class="c-axis-title" x="{(x0 + x1) / 2:.0f}" y="{y_base + 48:.0f}" text-anchor="middle">Календарный день</text>'
        f'<path class="c-ideal" d="{ideal_path}"/>'
        f'<path class="c-area" d="{area_path}" style="fill:url(#{grad_id})"/>'
        f'<polyline class="c-actual" points="{actual_pts}"/>'
        f'<g class="c-actual-pt">{dots_html}</g>'
        f"{callout}"
        "</svg>"
    )
    return svg, bool(weekend_rects)


# ==========================================================================
# §B lookup helpers — read labels/metric_defs/glossary/risks from the JSON,
# never a second hardcoded copy.
# ==========================================================================


def require(report: dict, key: str) -> Any:
    if key not in report:
        raise TemplateError(f"report is missing required schema v2 key: {key!r}")
    return report[key]


def labels_of(report: dict) -> dict:
    return report.get("labels") or {}


def col_label(report: dict, key: str, fallback: Optional[str] = None) -> str:
    cols = labels_of(report).get("columns") or {}
    if key in cols:
        return cols[key]
    return fallback if fallback is not None else key


def roles_map(report: dict) -> dict:
    return labels_of(report).get("roles") or {}


def status_categories_map(report: dict) -> dict:
    return labels_of(report).get("status_categories") or {}


def statuses_map(report: dict) -> dict:
    return labels_of(report).get("statuses") or {}


def metric_defs_by_key(report: dict) -> dict:
    return {d["key"]: d for d in (report.get("metric_defs") or [])}


def warn_line(w: dict) -> str:
    msg = auto_code_wrap(esc(w.get("message_ru") or ""))
    detail = w.get("detail")
    if detail:
        msg += f" ({auto_code_wrap(esc(detail))})"
    return msg


def warnings_list_html(warnings: list, empty_text: str = "Предупреждений нет.") -> str:
    if not warnings:
        return f'<p class="section-desc" style="text-align:left;margin:0">{esc(empty_text)}</p>'
    items = "".join(f"<li>{warn_line(w)}</li>" for w in warnings)
    return f'<ul class="warn-list">{items}</ul>'


def warnings_inline_html(warnings: list) -> str:
    if not warnings:
        return ""
    items = "".join(f'<span class="wbadge">{warn_line(w)}</span>' for w in warnings)
    return f'<div class="row-warnings">{items}</div>'


def status_category_label(report: dict, cat_code: str) -> str:
    cats = status_categories_map(report)
    code = cat_code or "unmapped"
    return cats.get(code) or cats.get("unmapped") or "Не сопоставлено"


def status_cell_title(report: dict, status_name: str, cat_code: str) -> str:
    statuses = statuses_map(report)
    entry = statuses.get(status_name) or {}
    cat_ru = entry.get("category_ru") or status_category_label(report, cat_code)
    override = entry.get("override_ru")
    title = f"{status_name} — {cat_ru}"
    if override:
        title += f" ({override})"
    return title


def role_display_html(report: dict, role_code: str, role_ru: str) -> str:
    if role_ru and role_ru != "—":
        return esc(role_ru)
    if not role_code:
        return EM_DASH
    return f'<code title="Роль задаётся в вашей Jira">{esc(role_code)}</code>'


def jira_label_chip(text: str, note: str) -> str:
    return f'<code class="pill" title="{esc(note)}">{esc(text)}</code>'


def person_name_html(display_name: str, login: str) -> str:
    return f'<span title="{esc(login)}">{esc(display_name)}</span>'


def display_name_for_login(report: dict, login: str) -> str:
    """Resolves a bare GitLab/Jira login to the matching person's
    display_name via people[] (§B.7 rule: renderer shows display_name,
    login goes in a title= tooltip). Falls back to the login itself when no
    match is found — e.g. an author GitLab could not attribute to anyone in
    people[]."""
    if not login:
        return login
    for p in report.get("people") or []:
        if p.get("login") == login:
            return p.get("display_name") or login
    return login


def warn_catalog(report: dict) -> dict:
    """Every distinct (code -> message_ru) pair actually present anywhere in
    this report, collected by walking the whole document — the JSON schema
    carries no separate static catalog, so tab 09's dictionary is built from
    what this run actually emitted rather than a hardcoded second copy."""
    out: dict = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            code = node.get("code")
            msg = node.get("message_ru")
            if isinstance(code, str) and isinstance(msg, str) and code not in out:
                out[code] = msg
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(report)
    return out


STATUS_LABELS = {"good": "норма", "warn": "под наблюдением", "bad": "критично"}

_CATEGORY_SOURCE_RU = {"gl": "GitLab", "jira": "Jira", "link": "Связка Jira ↔ GitLab", "infra": "Инфраструктура"}


# ==========================================================================
# Header
# ==========================================================================


def primary_target_axis(report: dict) -> dict:
    axis = require(report, "sprint_axis")
    targets = [s for s in axis if s.get("target")]
    if not targets:
        raise TemplateError("sprint_axis has no target sprint entries")
    return targets[-1]


def build_header_ctx(report: dict, ctx: dict) -> None:
    board = require(report, "board")
    params = require(report, "params")
    primary = primary_target_axis(report)
    target_names = [s["name"] for s in report["sprint_axis"] if s.get("target")]

    ctx["REPORT_TITLE"] = esc(f"Метрики команды — {', '.join(target_names)}" if target_names else "Метрики команды")
    ctx["HEADER_BOARD_NAME"] = esc(board.get("name", EM_DASH))
    ctx["HEADER_SPRINT_NAME"] = esc(primary.get("name", EM_DASH))
    ctx["HEADER_DATE_RANGE"] = esc(fmt_range(primary.get("start"), primary.get("end")))
    ctx["HEADER_GENERATED_AT"] = esc(fmt_datetime(params.get("generated_at")))
    ctx["HEADER_TOOL_VERSION"] = esc(params.get("tool_version") or EM_DASH)
    ctx["HEADER_WARNINGS"] = warnings_list_html(report.get("warnings") or [])


# ==========================================================================
# Tab 01 — Обзор
# ==========================================================================


def build_kpi_tile_html(tile: dict) -> str:
    status = tile.get("status") or "none"
    label = tile.get("label_ru") or ""
    hint = tile.get("hint_ru") or ""
    if "value_text" in tile:
        value_html = esc(tile["value_text"])
        unit_html = "SP"
    else:
        v = tile.get("value")
        value_html = fmt_num(v, 1) if v is not None else EM_DASH
        unit_html = esc(tile.get("unit_ru") or "")
    target_badge = f'<span class="kpi-status">{esc(tile["target_ru"])}</span>' if tile.get("target_ru") else ""
    series = [v for v in (tile.get("series") or []) if v is not None]
    spark = build_spark_svg(series, spark_status_class(status))
    status_label = STATUS_LABELS.get(status, "")
    return (
        f'<div class="kpi" data-status="{esc(status)}" title="{esc(hint)}">'
        f'<div class="kpi-head"><span class="kpi-label">{esc(label)}</span>{target_badge}</div>'
        f'<div class="kpi-value">{value_html}<span class="kpi-unit">{unit_html}</span></div>'
        f'<div class="delta-note">{esc(hint)}</div>'
        f'<div class="kpi-foot"><div class="delta">{esc(status_label)}</div>{spark}</div>'
        "</div>"
    )


_TAB01_KPI_PARA = (
    'Плитки показывают целевой спринт «{name}»: обязательство и поставку в Story Points, Performance '
    "(Say/Do), загрузку относительно средней velocity, изменение объёма, проценты закрытия — а также "
    "средние по базовым спринтам Velocity SMA5 и throughput. Формула каждой плитки — во всплывающей "
    'подсказке и на вкладке «09 Словарь и риски».'
)

_TAB01_OVERVIEW_PARA = (
    "Все показатели — по собранным данным за период {period} (все анализируемые спринты). "
    "Сравнивайте с собственным baseline (показатели за первый замеряемый период), чтобы видеть "
    "динамику и отделять устойчивые изменения от разовых флуктуаций. Средние cycle time — среднее по "
    "личным средним сотрудников; пайплайны и деплои посчитаны один раз по всей команде."
)

_TAB01_DIST_PARA = (
    "Типы задач — по полю «Тип» завершённых задач Jira за период. Доли успеха — (все запуски − "
    "упавшие) / все запуски по данным GitLab; отменённые и пропущенные запуски считаются неупавшими. "
    "Частота: пайплайнов {pw_pipe} в неделю, деплоев {pw_dep} в неделю."
)


def build_tab01(report: dict) -> str:
    board_kpi = require(report, "board_kpi")
    overview = require(report, "overview")
    primary = primary_target_axis(report)
    eng = report.get("engineering") or {}

    tiles_html = "".join(build_kpi_tile_html(t) for t in board_kpi.get("tiles") or [])
    sec1 = (
        '<section class="section" id="sec-01-kpi"><div class="section-head">'
        '<span class="section-index">01.1</span><h2 class="section-title">Ключевые показатели спринта</h2></div>'
        f'<div class="section-body"><div class="kpi-grid">{tiles_html}</div>'
        f'<p class="hint">{esc(_TAB01_KPI_PARA.format(name=primary.get("name", "")))}</p></div></section>'
    )

    overview_cards = [
        ("Сотрудников", fmt_int(overview.get("employees")), ""),
        ("MR (всего)", fmt_int(overview.get("mr_total")), ""),
        ("Завершённых задач", fmt_int(overview.get("tasks_done_total")), ""),
        ("PR cycle time (средн.)", fmt_num(overview.get("pr_cycle_time_avg_hours"), 1), "ч"),
        ("Cycle time задач (средн.)", fmt_num(overview.get("task_cycle_time_avg_hours"), 1), "ч"),
        ("Успешные пайплайны", fmt_pct(overview.get("pipeline_success_rate_pct")), ""),
        ("Успешные деплои", fmt_pct(overview.get("deploy_success_rate_pct")), ""),
        ("Defect rate", fmt_pct(overview.get("defect_rate_pct")), ""),
    ]
    cards_html = "".join(
        f'<div class="stat"><div class="stat-label">{esc(label)}</div>'
        f'<div class="stat-value">{value}<span class="u">{esc(unit)}</span></div></div>'
        for label, value, unit in overview_cards
    )
    gw = (report.get("params") or {}).get("gitlab_window") or {}
    if gw.get("start") or gw.get("end"):
        gw_period_text = f'{fmt_date(gw.get("start"))}–{fmt_date(gw.get("end"))}'
    else:
        gw_period_text = "не определён (GitLab не настроен для этого запуска)"
    sec2 = (
        '<section class="section" id="sec-01-overview"><div class="section-head">'
        '<span class="section-index">01.2</span><h2 class="section-title">Команда за период</h2></div>'
        f'<div class="section-body"><div class="person-stats stat-grid">{cards_html}</div>'
        f'<p class="hint">{esc(_TAB01_OVERVIEW_PARA.format(period=gw_period_text))}</p>'
        "</div></section>"
    )

    issue_dist = overview.get("issue_type_dist") or {}
    donut1 = build_donut_svg("chart-issue-type-dist", "Распределение типов задач", list(issue_dist.items()), fmt_int(overview.get("tasks_done_total")))
    pipe = (eng.get("pipelines") or {}) if eng.get("available") else {}
    dep = (eng.get("deployments") or {}) if eng.get("available") else {}
    pipe_items = [("Успешно", (pipe.get("count") or 0) - (pipe.get("failed") or 0)), ("Упало", pipe.get("failed") or 0)] if pipe else []
    dep_items = [("Успешно", (dep.get("count") or 0) - (dep.get("failed") or 0)), ("Упало", dep.get("failed") or 0)] if dep else []
    donut2 = build_donut_svg("chart-pipeline-success", "Доля успешных пайплайнов", pipe_items, fmt_pct(pipe.get("success_rate_pct")) if pipe else EM_DASH)
    donut3 = build_donut_svg("chart-deploy-success", "Доля успешных деплоев", dep_items, fmt_pct(dep.get("success_rate_pct")) if dep else EM_DASH)

    pw_pipe = fmt_num(pipe.get("per_week"), 1) if pipe.get("per_week") is not None else "нет данных"
    pw_dep = fmt_num(dep.get("per_week"), 1) if dep.get("per_week") is not None else "нет данных"
    dist_hint = esc(_TAB01_DIST_PARA.format(pw_pipe=pw_pipe, pw_dep=pw_dep))
    sec3 = (
        '<section class="section" id="sec-01-dist"><div class="section-head">'
        '<span class="section-index">01.3</span><h2 class="section-title">Распределение и стабильность</h2></div>'
        '<div class="section-body"><div class="donut-row">'
        + chart_block("chart-issue-type-dist", "Распределение типов задач", donut1, dist_hint)
        + chart_block("chart-pipeline-success", "Доля успешных пайплайнов", donut2, dist_hint)
        + chart_block("chart-deploy-success", "Доля успешных деплоев", donut3, dist_hint)
        + "</div></div></section>"
    )

    notes = report.get("semantics_notes") or []
    notes_html = "".join(f"<li>{esc(n)}</li>" for n in notes)
    sec4 = (
        '<section class="section" id="sec-01-warnings"><div class="section-head">'
        '<span class="section-index">01.4</span><h2 class="section-title">Предупреждения</h2></div>'
        f'<div class="section-body">{warnings_list_html(report.get("warnings") or [])}'
        '<details class="thresholds"><summary>Как читать цифры (важные оговорки)</summary>'
        f'<ul class="warn-list">{notes_html}</ul></details></div></section>'
    )

    return sec1 + sec2 + sec3 + sec4


# ==========================================================================
# Tab 02 — Спринт
# ==========================================================================

_TAB02_BURNDOWN_PARA = (
    "Сжигание объёма по календарным дням спринта, включая выходные (статус переносится с последнего "
    'рабочего дня). «Факт» — оставшиеся задачи/SP, чей статус ещё не «Готово» и не «Отменено»; '
    "«Идеал» — равномерное линейное сжигание обязательства от старта к концу: обязательство × "
    "(1 − день/всего дней). Для активного спринта график обрывается на сегодняшнем дне."
)

_TAB02_HEATMAP_PARA = (
    "Каждая строка — задача спринта, каждый столбец — рабочий день (пн–пт). Цвет ячейки — категория "
    "статуса задачи на конец этого дня, восстановленная из истории изменений Jira; сам статус написан "
    "в подсказке ячейки. «Не сопоставлено» значит, что статус не удалось отнести к категории — "
    "проверьте status_map в настройках."
)

_TAB02_BREAKDOWN_PARA = (
    "Список задач спринта с их статусом на момент закрытия спринта. «Поставлена = Да» — задача была в "
    "спринте на его конец и её статус относится к категории «Готово» (с учётом списка отменённых "
    "статусов)."
)

_CAT_ORDER = ["new", "indeterminate", "done", "cancelled", "unmapped"]


def build_unit_toggle(chart_id: str, svg_items: str, svg_sp: str) -> str:
    return (
        f'<div class="unit-toggle">'
        f'<input class="ut-input" type="radio" name="ut-{esc(chart_id)}" id="ut-items-{esc(chart_id)}" checked>'
        f'<input class="ut-input" type="radio" name="ut-{esc(chart_id)}" id="ut-sp-{esc(chart_id)}">'
        f'<div class="unit-tabs">'
        f'<label class="unit-tab" for="ut-items-{esc(chart_id)}">Задачи</label>'
        f'<label class="unit-tab" for="ut-sp-{esc(chart_id)}">SP</label>'
        "</div>"
        f'<div class="unit-panels"><div class="unit-panel">{svg_items}</div><div class="unit-panel">{svg_sp}</div></div>'
        "</div>"
    )


def build_heatmap_table(report: dict, hm: dict) -> str:
    days = hm.get("days") or []
    rows = hm.get("rows") or []
    day_ths = "".join(
        f'<th scope="col">{d[8:10]}.{d[5:7]}<span class="dow">{_WEEKDAY_RU_SHORT[_weekday(d)]}</span></th>' for d in days
    )
    note = labels_of(report).get("jira_label_note_ru") or ""
    rows_html = []
    for row in rows:
        cells_by_date = {c["date"]: c for c in row.get("cells") or []}
        day_cells = []
        for d in days:
            cell = cells_by_date.get(d)
            if cell is None:
                day_cells.append('<td class="hm-cell" data-cat="absent" title="нет в спринте в этот день">&mdash;</td>')
                continue
            cat = cell.get("status_category") or "unmapped"
            title = status_cell_title(report, cell.get("status", ""), cat)
            day_cells.append(f'<td class="hm-cell" data-cat="{esc(cat or "unmapped")}" title="{esc(title)}">{esc_or_dash(cell.get("status"))}</td>')
        labels_html = "".join(jira_label_chip(lb, note) for lb in row.get("labels") or []) or EM_DASH
        rows_html.append(
            "<tr>"
            f'<th scope="row" class="rowkey">{esc(row.get("issue_key"))}</th>'
            f"<td>{person_name_html(row.get('assignee_display_name') or EM_DASH, row.get('assignee_login') or '')}</td>"
            f'<td class="col-num">{fmt_num(row.get("story_points"), 1)}</td>'
            f"<td>{role_display_html(report, row.get('role') or '', row.get('role_ru') or '')}</td>"
            f"<td>{labels_html}</td>"
            + "".join(day_cells)
            + "</tr>"
        )
    legend = "".join(
        f'<span class="scale-step"><i class="scale-chip" style="background:var(--cat-{cat})"></i>{esc(status_category_label(report, cat))}</span>'
        for cat in _CAT_ORDER
    ) + '<span class="scale-step"><i class="scale-chip absent"></i>нет в спринте в этот день</span>'
    table = (
        '<div class="table-card card"><div class="table-wrap scroll-x"><table class="heatmap data wide">'
        f'<thead><tr><th class="hm-corner" scope="col">Задача</th><th scope="col">Исполнитель</th>'
        f'<th scope="col">SP</th><th scope="col">Роль</th><th scope="col">Метки</th>{day_ths}</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table></div>'
        f'<div class="cat-scale"><span class="scale-title">Категория статуса</span>{legend}</div></div>'
    )
    return table


def build_breakdown_table(report: dict, bd: dict) -> str:
    rows = bd.get("rows") or []
    rows_html = []
    for r in rows:
        cat_text = status_category_label(report, r.get("final_status_category") or "")
        delivered = "Да" if r.get("delivered") else "Нет"
        rows_html.append(
            "<tr>"
            f'<th scope="row" class="rowkey">{esc(r.get("key"))}</th>'
            f'<td>{esc(r.get("final_status"))} <span class="pill">{esc(cat_text)}</span></td>'
            f"<td>{esc(delivered)}</td>"
            "</tr>"
        )
    return (
        '<div class="table-card card"><div class="table-wrap"><table class="data mid">'
        '<thead><tr><th scope="col">Задача</th><th scope="col">Финальный статус</th><th scope="col">Поставлена</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table></div></div>'
    )


def build_tab02(report: dict) -> str:
    sprints = require(report, "sprints")
    burndown_by_id = {b["sprint_id"]: b for b in report.get("burndown") or []}
    heatmap_by_id = {h["sprint_id"]: h for h in report.get("heatmap") or []}
    breakdown_by_id = {b["sprint_id"]: b for b in report.get("issue_breakdown") or []}

    blocks = []
    for s in sprints:
        if not s.get("target"):
            continue
        meta = s["meta"]
        sid = meta["id"]
        name = meta["name"]
        bd_points = (burndown_by_id.get(sid) or {}).get("points") or []
        svg_items, has_weekend1 = build_burndown_svg(f"chart-burndown-items-{sid}", bd_points, "items")
        svg_sp, has_weekend2 = build_burndown_svg(f"chart-burndown-sp-{sid}", bd_points, "sp")
        burndown_html = build_unit_toggle(f"burndown-{sid}", svg_items, svg_sp)
        sec_burndown = (
            f'<div class="chartbox card"><h3>Burndown — {esc(name)}</h3>{burndown_html}'
            f'<p class="hint">{esc(_TAB02_BURNDOWN_PARA)}</p></div>'
        )

        hm = heatmap_by_id.get(sid) or {"days": [], "rows": []}
        heatmap_table = build_heatmap_table(report, hm)
        sec_heatmap = (
            f'<div class="table-block"><h3>Тепловая карта статусов — {esc(name)}</h3>{heatmap_table}'
            f'<p class="hint">{auto_code_wrap(esc(_TAB02_HEATMAP_PARA))}</p></div>'
        )

        bd = breakdown_by_id.get(sid) or {"rows": []}
        breakdown_table = build_breakdown_table(report, bd)
        sec_breakdown = (
            f'<div class="table-block"><h3>Разбивка по задачам (по спринту) — {esc(name)}</h3>{breakdown_table}'
            f'<p class="hint">{esc(_TAB02_BREAKDOWN_PARA)}</p></div>'
        )

        blocks.append(
            f'<section class="section" id="sec-02-{sid}"><div class="section-head">'
            f'<h2 class="section-title">{esc(name)}</h2></div>'
            f'<div class="section-body">{sec_burndown}{sec_heatmap}{sec_breakdown}</div></section>'
        )
    return "".join(blocks)


# ==========================================================================
# Tab 03 — Динамика команды
# ==========================================================================

_TAB03_INTRO_PARA = (
    "Разрез метрик по спринтам: видно, ЧТО менялось от спринта к спринту, а не только суммарные цифры "
    "за период. Пунктиром показан тренд (линейная регрессия). Задачи привязаны к спринту по дате "
    "завершения, MR — по дате merge, пайплайны — по дате создания, деплои — по дате завершения."
)

_TAB03_BOARD_CHART_PARA = {
    "commitment": (
        "Обязательство на старте каждого спринта и фактическая поставка к его концу, в Story Points. "
        "Столбцы построены по тем же величинам, что и плитки обзора: committed_sp и delivered_sp "
        "каждого спринта."
    ),
    "performance": (
        "Поставлено / обязательство × 100 по каждому спринту. Пунктирная линия — цель 80%. Значения "
        "ниже — команда систематически обещает больше, чем поставляет."
    ),
    "load": (
        "Обязательство спринта относительно средней velocity предыдущих спринтов (SMA5), × 100. "
        "Пунктирная линия — 100%. Выше 100% — команда взяла больше своей средней скорости; ниже 80% — "
        "недогруз."
    ),
    "scope_change": (
        "Из чего складывалось изменение объёма спринта: добавленные задачи, изменение оценок уже "
        "взятых задач и убранные задачи, в SP. Большие столбцы — нестабильный объём спринта."
    ),
    "velocity": (
        "Velocity (= поставленные SP) каждого спринта и её скользящее среднее по 5 предыдущим "
        "закрытым спринтам (SMA5). Сходимость линий — стабильная скорость."
    ),
    "throughput": (
        'Число задач, доведённых до «Готово» в каждом спринте (по дню последнего перехода в '
        'категорию «Готово»).'
    ),
}


def build_tab03(report: dict) -> str:
    sprints = require(report, "sprints")
    axis = require(report, "sprint_axis")
    cat_labels = [s["name"] for s in axis]

    committed_sp = [s["metrics"]["committed_sp"] for s in sprints]
    delivered_sp = [s["metrics"]["delivered_sp"] for s in sprints]
    performance_pct = [s["metrics"]["performance_pct"] for s in sprints]
    load_pct = [s["metrics"]["load_pct"] for s in sprints]
    scope_added = [s["metrics"]["scope_added_sp"] for s in sprints]
    scope_est = [s["metrics"]["scope_estimation_change_sp"] for s in sprints]
    scope_removed = [s["metrics"]["scope_removed_sp"] for s in sprints]
    velocity_sp = [s["metrics"]["velocity_sp"] for s in sprints]
    velocity_sma5 = [s["metrics"]["velocity_sma5_sp"] for s in sprints]
    throughput = [s["metrics"]["throughput_items"] for s in sprints]

    charts = [
        chart_block(
            "chart-board-commitment", "Commitment (SP)",
            build_stacked_bar_svg("chart-board-commitment", "Commitment (SP)", cat_labels, [("Обязательство", committed_sp), ("Поставлено", delivered_sp)], "SP"),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["commitment"])),
        ),
        chart_block(
            "chart-board-performance", "Performance (Say/Do), %",
            build_vbar_svg("chart-board-performance", "Performance (Say/Do), %", cat_labels, performance_pct, "%", ref_line=80.0),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["performance"])),
        ),
        chart_block(
            "chart-board-load", "Загрузка, %",
            build_vbar_svg("chart-board-load", "Загрузка, %", cat_labels, load_pct, "%", ref_line=100.0),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["load"])),
        ),
        chart_block(
            "chart-board-scope-change", "Изменение объёма (SP)",
            build_stacked_bar_svg(
                "chart-board-scope-change", "Изменение объёма (SP)", cat_labels,
                [("Добавлено", scope_added), ("Изменение оценки", scope_est), ("Убрано", scope_removed)], "SP",
            ),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["scope_change"])),
        ),
        chart_block(
            "chart-board-velocity", "Velocity (SP)",
            build_multiline_svg(
                "chart-board-velocity", "Velocity (SP)", cat_labels,
                [("Velocity", velocity_sp), ("SMA5", velocity_sma5)], "SP", show_trend=False,
            ),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["velocity"])),
        ),
        chart_block(
            "chart-board-throughput", "Throughput (задач)",
            build_vbar_svg("chart-board-throughput", "Throughput (задач)", cat_labels, throughput, "задач"),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["throughput"])),
        ),
    ]
    sec1 = (
        '<section class="section" id="sec-03-board"><div class="section-head">'
        '<span class="section-index">03.1</span><h2 class="section-title">Метрики спринтов (Jira)</h2></div>'
        f'<div class="section-body"><div class="chart-grid">{"".join(charts)}</div></div></section>'
    )

    team_series = report.get("team_series") or []
    ts_charts = []
    for item in team_series:
        series = [(s["name_ru"], s["values"]) for s in item.get("series") or []]
        svg = build_multiline_svg(
            f'chart-team-{id_safe(item["key"])}', item["title_ru"], cat_labels, series, item.get("unit_ru") or "",
            show_trend=bool(item.get("show_trend")),
        )
        ts_charts.append(chart_block(f'chart-team-{id_safe(item["key"])}', item["title_ru"], svg, auto_code_wrap(esc(item.get("hint_ru") or ""))))
    sec2 = (
        '<section class="section" id="sec-03-series"><div class="section-head">'
        '<span class="section-index">03.2</span><h2 class="section-title">Динамика доставки (Jira + GitLab)</h2></div>'
        f'<div class="section-body"><p class="hint">{esc(_TAB03_INTRO_PARA)}</p>'
        f'<div class="chart-grid">{"".join(ts_charts)}</div></div></section>'
    )
    return sec1 + sec2


# ==========================================================================
# Tab 04 — Прогноз
# ==========================================================================

_TAB04_UNAVAILABLE_PARA = (
    "Прогноз строится, когда в истории есть не меньше 10 дневных точек с закрытыми задачами. "
    "Добавьте закрытых спринтов в базу (--history) или дождитесь данных."
)

_TAB04_PERCENTILES_PARA = (
    "P50/P85/P95 — за сколько календарных дней команда закроет {target_items} задач с вероятностью "
    "50/85/95%. Метод: bootstrap-симуляция ({iterations} итераций) по дневному throughput последних "
    "{sample_sprints} закрытых спринтов, с нулевыми днями и выходными."
)

_TAB04_HISTOGRAM_PARA = (
    "Распределение исходов симуляции: по горизонтали — число дней, по вертикали — сколько итераций "
    "закончилось за это число дней. Пунктирные линии — перцентили P50/P85/P95."
)

_TAB04_CV_PARA = (
    "CV (коэффициент вариации) — насколько нестабилен понедельный поток закрытий: "
    "среднеквадратичное отклонение недельных сумм к их среднему, ×100. Выше 50% — перцентили "
    "ненадёжны."
)


def build_tab04(report: dict) -> str:
    forecast = report.get("forecast")
    if not forecast or not forecast.get("available"):
        error = (forecast or {}).get("error") or {}
        msg = error.get("message_ru") or "Прогноз недоступен."
        return (
            '<section class="section" id="sec-04-forecast"><div class="section-head">'
            '<span class="section-index">04.1</span><h2 class="section-title">Прогноз Monte-Carlo</h2></div>'
            f'<div class="section-body"><p class="section-desc" style="text-align:left">{esc(msg)}</p>'
            f'<p class="hint">{esc(_TAB04_UNAVAILABLE_PARA)}</p></div></section>'
        )

    percentiles = forecast.get("percentiles") or []
    tiles = "".join(
        f'<div class="stat"><div class="stat-label">{esc(p.get("label_ru"))}</div>'
        f'<div class="stat-value">{fmt_int(p.get("days"))}<span class="u">дней</span></div></div>'
        for p in percentiles
    )
    percentile_map = {p["percentile"]: p["days"] for p in percentiles}
    para = _TAB04_PERCENTILES_PARA.format(
        target_items=forecast.get("target_items"), iterations=(report.get("params") or {}).get("iterations"),
        sample_sprints=forecast.get("sample_sprints"),
    )

    hist = forecast.get("histogram") or []
    cat_labels = [str(h["days"]) for h in hist]
    values = [h["count"] for h in hist]
    ref_lines = [
        (percentile_map.get(50), "P50"),
        (percentile_map.get(85), "P85"),
        (percentile_map.get(95), "P95"),
    ]
    hist_svg = build_vbar_svg("chart-forecast-histogram", "Гистограмма исходов симуляции", cat_labels, values, "прогонов", ref_lines=ref_lines)

    cv_block = ""
    if forecast.get("cv_warning_ru"):
        cv_block = f'<div class="banner"><span class="banner-tag">CV</span><div class="banner-body">{esc(forecast["cv_warning_ru"])}</div></div>'

    target_source = forecast.get("target_items_source_ru") or ""
    body = (
        f'<div class="person-stats stat-grid">{tiles}</div>'
        f'<p class="hint">{esc(para)}</p>'
        f'<p class="section-desc" style="text-align:left;margin:12px 0">Целевое число задач: '
        f'<b>{fmt_int(forecast.get("target_items"))}</b> — {esc(target_source)}. Изменить: флаг '
        "--target-items у команды run.</p>"
        + chart_block("chart-forecast-histogram", "Гистограмма исходов симуляции", hist_svg, esc(_TAB04_HISTOGRAM_PARA))
        + f'<p class="section-desc" style="text-align:left;margin:8px 0">{esc(forecast.get("sample_hint_ru") or "")}</p>'
        + cv_block
        + f'<p class="hint">{esc(_TAB04_CV_PARA)}</p>'
    )
    return (
        '<section class="section" id="sec-04-forecast"><div class="section-head">'
        '<span class="section-index">04.1</span><h2 class="section-title">Прогноз Monte-Carlo</h2></div>'
        f'<div class="section-body">{body}</div></section>'
    )


# ==========================================================================
# Tab 05 — Люди — сравнение
# ==========================================================================

_TAB05_INTRO_PARA = "Персональные цифры — для персонального менторинга, не для публичных рейтингов."

_TAB05_COMPARE_PARA = (
    "Каждая полоса — один сотрудник за весь период. Завершённые задачи — по первому переходу задачи в "
    "финальный статус в Jira; MR — по автору в GitLab; cycle time — от первого «В работе» до "
    'финального статуса; rework — повторные входы задачи в работу и переоткрытия. «нет данных» — у '
    "сотрудника нет ни одного значения, а не ноль."
)

_TAB05_CYCLE_PARA = (
    "Среднее чувствительно к одиночным долгим задачам, медиана устойчива. Большой разрыв между ними — "
    "у сотрудника есть задачи-выбросы, которые тянут среднее вверх."
)

_TAB05_TABLE_PARA = (
    "Полный набор персональных метрик из словаря (вкладка 09). Инфраструктурные метрики (пайплайны, "
    "деплои, покрытие) считаются только по команде и в эту таблицу не входят. Определение каждой "
    "метрики — в столбце-подсказке и на вкладке 09."
)

_TAB05_DIST_PARA = (
    "Типы задач — по завершённым задачам сотрудника в Jira. Доля успешных пайплайнов — по пайплайнам "
    "GitLab, атрибутированным сотруднику через первый job пайплайна; если атрибуция не собиралась "
    "(--no-pipeline-users / --light), у всех будет «нет данных»."
)

_TAB05_INDIVIDUAL_PARA = (
    "Формулы спринтовой вкладки, применённые к задачам каждого исполнителя: обязательство — задачи "
    "человека в спринте на его старте, поставлено — его задачи в категории «Готово» на конец спринта. "
    "Командные величины (Загрузка, SMA5, Изменение объёма) на человека не переносятся — они не "
    'определены персонально. Задачи без исполнителя собраны в строку «Без исполнителя».'
)

_EXTRA_PERSON_KEYS = [
    "mr_closed_count", "bug_count", "rework_tasks", "issue_count",
    "mr_with_jira_key", "mr_diff_size_available_count", "pipeline_success_rate_pct",
]


def build_tab05(report: dict) -> str:
    people = report.get("people") or []
    parts = [f'<p class="hint">{esc(_TAB05_INTRO_PARA)}</p>']

    names = [p["display_name"] for p in people]

    def _hbar_items(key):
        return list(zip(names, [p["metrics"].get(key) for p in people]))

    hbar_defs = [
        ("chart-cmp-tasks-done", "Завершённые задачи, шт", "tasks_done", "шт"),
        ("chart-cmp-mr-count", "Число MR, шт", "mr_count", "шт"),
        ("chart-cmp-task-cycle", "Cycle time задач (средн.), ч", "task_cycle_time_avg_hours", "ч"),
        ("chart-cmp-rework", "Rework (возвраты), шт", "rework_total", "шт"),
    ]
    compare_hint = esc(_TAB05_COMPARE_PARA)
    hbars_html = "".join(
        chart_block(cid, title, build_hbar_svg(cid, title, _hbar_items(key), unit), compare_hint)
        for cid, title, key, unit in hbar_defs
    )
    sec1 = (
        '<section class="section" id="sec-05-compare"><div class="section-head">'
        '<span class="section-index">05.1</span><h2 class="section-title">Сравнение по ключевым метрикам</h2></div>'
        f'<div class="section-body"><div class="chart-grid">{hbars_html}</div></div></section>'
    )

    grouped_series = [
        ("Среднее", [p["metrics"].get("task_cycle_time_avg_hours") for p in people]),
        ("Медиана", [p["metrics"].get("task_cycle_time_median_hours") for p in people]),
    ]
    grouped_svg = build_grouped_bar_svg("chart-cmp-cycle-grouped", "Cycle time по сотрудникам (среднее и медиана, ч)", names, grouped_series, "ч")
    sec2 = (
        '<section class="section" id="sec-05-cycle"><div class="section-head">'
        '<span class="section-index">05.2</span><h2 class="section-title">Cycle time по сотрудникам (среднее и медиана, ч)</h2></div>'
        f'<div class="section-body">{chart_block("chart-cmp-cycle-grouped", "Cycle time по сотрудникам (среднее и медиана, ч)", grouped_svg, esc(_TAB05_CYCLE_PARA))}</div></section>'
    )

    if people:
        mdefs = [d for d in (report.get("metric_defs") or []) if d.get("scope") != "team"]
        cols_meta = labels_of(report).get("columns") or {}
        extra_rows = [(k, cols_meta.get(k, k)) for k in _EXTRA_PERSON_KEYS if any(k in (p.get("metrics") or {}) for p in people)]
        header_people = "".join(f'<th class="col-num">{esc(p["display_name"])}</th>' for p in people)
        rows_html = []
        for d in mdefs:
            key, is_pct = d["key"], d.get("is_pct")
            cells = "".join(
                f'<td class="col-num">{(fmt_num(p["metrics"].get(key), 1) + "%") if (is_pct and p["metrics"].get(key) is not None) else fmt_num(p["metrics"].get(key), 2)}</td>'
                for p in people
            )
            rows_html.append(f'<tr><td>{esc(d["label_ru"])}</td>{cells}</tr>')
        for key, label in extra_rows:
            is_pct = key.endswith("_pct")
            cells = "".join(
                f'<td class="col-num">{(fmt_num(p["metrics"].get(key), 1) + "%") if (is_pct and p["metrics"].get(key) is not None) else fmt_num(p["metrics"].get(key), 2)}</td>'
                for p in people
            )
            rows_html.append(f'<tr><td>{esc(label)}</td>{cells}</tr>')
        table = (
            '<div class="table-card card"><div class="table-wrap"><table class="data wide">'
            f'<thead><tr><th scope="col">Метрика</th>{header_people}</tr></thead>'
            f'<tbody>{"".join(rows_html)}</tbody></table></div></div>'
        )
        sec3_body = table_hint(table, esc(_TAB05_TABLE_PARA))
    else:
        sec3_body = empty_state_html("Нет данных: список сотрудников пуст.") + f'<p class="hint">{esc(_TAB05_TABLE_PARA)}</p>'
    sec3 = (
        '<section class="section" id="sec-05-table"><div class="section-head">'
        '<span class="section-index">05.3</span><h2 class="section-title">Все метрики по сотрудникам</h2></div>'
        f'<div class="section-body">{sec3_body}</div></section>'
    )

    dist_hint = esc(_TAB05_DIST_PARA)
    if people:
        person_cards = []
        for i, p in enumerate(people):
            dist = p.get("issue_type_dist") or {}
            cid1, cid2 = f"chart-p-dist-{i}", f"chart-p-pipe-{i}"
            d1 = build_donut_svg(cid1, f'Распределение типов задач — {p["display_name"]}', list(dist.items()), fmt_int(p["metrics"].get("tasks_done")))
            rate = p["metrics"].get("pipeline_success_rate_pct")
            d2_items = [("Успешно", rate), ("Не успешно", (100.0 - rate) if rate is not None else None)] if rate is not None else []
            d2 = build_donut_svg(cid2, f'Доля успешных пайплайнов — {p["display_name"]}', d2_items, fmt_pct(rate))
            person_cards.append(
                '<div class="donut-pair">'
                + chart_block(cid1, f'Распределение типов задач — {p["display_name"]}', d1, dist_hint)
                + chart_block(cid2, f'Доля успешных пайплайнов — {p["display_name"]}', d2, dist_hint)
                + "</div>"
            )
        sec4_body = "".join(person_cards)
    else:
        sec4_body = empty_state_html("Нет данных: список сотрудников пуст.") + f'<p class="hint">{dist_hint}</p>'
    sec4 = (
        '<section class="section" id="sec-05-dist"><div class="section-head">'
        '<span class="section-index">05.4</span><h2 class="section-title">Персональные распределения</h2></div>'
        f'<div class="section-body">{sec4_body}</div></section>'
    )

    ind_tables = []
    for entry in report.get("people_individual_jira") or []:
        rows = entry.get("rows") or []
        rows_html2 = []
        for r in rows:
            rows_html2.append(
                "<tr>"
                f"<td>{person_name_html(r.get('assignee_display_name') or 'Без исполнителя', r.get('assignee_login') or '')}</td>"
                f'<td class="col-num">{fmt_num(r.get("committed_sp"), 1)}</td>'
                f'<td class="col-num">{fmt_int(r.get("committed_items"))}</td>'
                f'<td class="col-num">{fmt_num(r.get("delivered_sp"), 1)}</td>'
                f'<td class="col-num">{fmt_int(r.get("delivered_items"))}</td>'
                f'<td class="col-num">{fmt_pct(r.get("performance_pct"), 1)}</td>'
                f'<td class="col-num">{fmt_num(r.get("velocity_sp"), 1)}</td>'
                f'<td class="col-num">{fmt_int(r.get("throughput_items"))}</td>'
                "</tr>"
            )
        table2 = (
            '<div class="table-card card"><div class="table-wrap"><table class="data mid">'
            "<thead><tr><th scope=\"col\">Исполнитель</th><th scope=\"col\">Обязательство, SP</th>"
            "<th scope=\"col\">Обязательство, задач</th><th scope=\"col\">Поставлено, SP</th>"
            "<th scope=\"col\">Поставлено, задач</th><th scope=\"col\">Performance, %</th>"
            "<th scope=\"col\">Velocity, SP</th><th scope=\"col\">Throughput, задач</th></tr></thead>"
            f'<tbody>{"".join(rows_html2)}</tbody></table></div></div>'
        )
        ind_tables.append(f'<h3>{esc(entry.get("sprint_name"))}</h3>{table2}')
    sec5 = (
        '<section class="section" id="sec-05-individual"><div class="section-head">'
        '<span class="section-index">05.5</span><h2 class="section-title">Вклад в целевой спринт (по Jira)</h2></div>'
        f'<div class="section-body">{"".join(ind_tables)}<p class="hint">{esc(_TAB05_INDIVIDUAL_PARA)}</p></div></section>'
    )

    return "".join(parts) + sec1 + sec2 + sec3 + sec4 + sec5


# ==========================================================================
# Tab 06 — Люди — динамика
# ==========================================================================

_TAB06_INTRO_PARA = (
    "Каждая линия — один сотрудник. Если линии расходятся — сотрудники работают с разной "
    "скоростью/объёмом; сближение линий — выравнивание. Сравнивайте только с собственным baseline."
)

_TAB06_TREND_PARA = (
    "Точка — значение сотрудника в спринте; пропуск линии — в этом спринте у него нет данных. Пунктир "
    "— общий тренд (линейная регрессия по средним значениям спринтов)."
)

_TAB06_CARDS_PARA = (
    "История сотрудника по всем анализируемым спринтам. Задача попадает в спринт по дате завершения, "
    "MR — по дате merge. Прочерк — нет данных в этом спринте."
)


def _mean_by_index(series_values: list) -> list:
    if not series_values:
        return []
    n = len(series_values[0])
    out = []
    for i in range(n):
        vals = [vals[i] for vals in series_values if i < len(vals) and vals[i] is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def _people_series_charts(report: dict, keys: list, cat_labels: list, with_trend: bool) -> str:
    by_key = {item["key"]: item for item in report.get("people_series") or []}
    charts = []
    for key in keys:
        item = by_key.get(key)
        if not item:
            continue
        series = [(s["display_name"], s["values"]) for s in item.get("series") or []]
        trend_values = _mean_by_index([s["values"] for s in item.get("series") or []]) if with_trend else None
        svg = build_multiline_svg(
            f"chart-people-{id_safe(key)}", item["title_ru"], cat_labels, series, item.get("unit_ru") or "",
            show_trend=with_trend, trend_values=trend_values,
        )
        charts.append(chart_block(f"chart-people-{id_safe(key)}", item["title_ru"], svg, auto_code_wrap(esc(item.get("hint_ru") or ""))))
    return "".join(charts)


def build_tab06(report: dict) -> str:
    axis = require(report, "sprint_axis")
    cat_labels = [s["name"] for s in axis]
    parts = [f'<p class="hint">{esc(_TAB06_INTRO_PARA)}</p>']

    jira_keys = ["throughput_by_person", "task_cycle_time_by_person", "rework_by_person", "story_points_by_person", "qa_estimation_by_person"]
    sec1 = (
        '<section class="section" id="sec-06-jira"><div class="section-head">'
        '<span class="section-index">06.1</span><h2 class="section-title">Задачи и оценки (Jira)</h2></div>'
        f'<div class="section-body"><div class="chart-grid">{_people_series_charts(report, jira_keys, cat_labels, False)}</div></div></section>'
    )

    gitlab_keys = ["mr_count_by_person", "pr_cycle_time_by_person", "mr_weight_by_person"]
    sec2 = (
        '<section class="section" id="sec-06-gitlab"><div class="section-head">'
        '<span class="section-index">06.2</span><h2 class="section-title">Merge-реквесты (GitLab)</h2></div>'
        f'<div class="section-body"><p class="hint">{esc(_TAB06_TREND_PARA)}</p>'
        f'<div class="chart-grid">{_people_series_charts(report, gitlab_keys, cat_labels, True)}</div></div></section>'
    )

    cards = []
    for p in report.get("people") or []:
        rows = []
        for row in p.get("by_sprint") or []:
            has_data = row.get("has_data")
            def cell(key, decimals=1):
                if not has_data:
                    return EM_DASH
                return fmt_num(row.get(key), decimals)
            rows.append(
                "<tr>"
                f'<td>{esc(row.get("sprint_name"))}</td>'
                f'<td class="col-num">{cell("throughput", 0)}</td>'
                f'<td class="col-num">{cell("avg_cycle_time_hours")}</td>'
                f'<td class="col-num">{cell("story_points_total")}</td>'
                f'<td class="col-num">{cell("qa_estimation_total")}</td>'
                f'<td class="col-num">{cell("rework_total", 0)}</td>'
                f'<td class="col-num">{cell("mr_count", 0)}</td>'
                f'<td class="col-num">{cell("avg_mr_cycle_hours")}</td>'
                f'<td class="col-num">{cell("avg_mr_changes_count")}</td>'
                "</tr>"
            )
        warn_html = warnings_inline_html(p.get("warnings") or [])
        card = (
            f'<div class="person-card card"><div class="person-head"><div>'
            f'<h3 class="person-name" title="{esc(p["login"])}">{esc(p["display_name"])}</h3></div></div>'
            f"{warn_html}"
            '<div class="mini-wrap"><table class="sprint-mini">'
            "<thead><tr><th>Спринт</th><th>Завершено задач</th><th>Cycle (ср., ч)</th><th>Story Points</th>"
            "<th>QA Est.</th><th>Rework</th><th>MR</th><th>PR cycle (ср., ч)</th><th>Вес MR (файл.)</th></tr></thead>"
            f'<tbody>{"".join(rows)}</tbody></table></div></div>'
        )
        cards.append(card)
    sec3 = (
        '<section class="section" id="sec-06-cards"><div class="section-head">'
        '<span class="section-index">06.3</span><h2 class="section-title">Карточки сотрудников</h2></div>'
        f'<div class="section-body"><div class="person-cards">{"".join(cards)}</div>'
        f'<p class="hint">{esc(_TAB06_CARDS_PARA)}</p></div></section>'
    )

    return "".join(parts) + sec1 + sec2 + sec3


# ==========================================================================
# Tab 07 — Инженерия
# ==========================================================================

_TAB07_UNAVAILABLE_PARA = (
    "Инженерная вкладка строится из GitLab (пайплайны, деплои, покрытие). Задайте GITLAB_URL/"
    "GITLAB_TOKEN и заполните gitlab.projects в .team-metrics.json."
)

_TAB07_PIPELINES_PARA = (
    "Запуски CI по всем настроенным проектам за период. Доля успешных = (все − упавшие) / все × 100; "
    "отменённые и пропущенные не считаются упавшими. Частота — запуски, делённые на длину периода в "
    "неделях (по датам создания)."
)

_TAB07_DEPLOYMENTS_PARA = (
    "Выкаты по всем проектам за период (по дате завершения деплоя). Формулы те же, что для пайплайнов. "
    "Частота и стабильность деплоев — ключевые DORA-метрики."
)

_TAB07_COVERAGE_PARA = (
    "Покрытие из последнего успешного пайплайна каждого проекта в периоде (поле coverage в GitLab). "
    "Это одна точка на проект, а не средняя по всем запускам — настройте вывод покрытия в CI, если "
    "здесь пусто."
)

_TAB07_BY_SPRINT_PARA = (
    "Те же данные, что в таблицах выше, разложенные по спринтам: пайплайны — по дате создания, деплои "
    "— по дате завершения."
)

_TAB07_COMPLETENESS_PARA = (
    "Здесь перечислено всё, что инструмент НЕ смог получить: пропущенные проекты, авторы с ошибками, "
    "обрезанные списки деплоев. Числа на этой вкладке нужно читать с учётом этих оговорок."
)


def _eng_tiles_html(count, failed, rate, per_week) -> str:
    return (
        '<div class="person-stats stat-grid">'
        f'<div class="stat"><div class="stat-label">Всего запусков</div><div class="stat-value">{fmt_int(count)}</div></div>'
        f'<div class="stat"><div class="stat-label">Упавших</div><div class="stat-value">{fmt_int(failed)}</div></div>'
        f'<div class="stat"><div class="stat-label">Доля успешных, %</div><div class="stat-value">{fmt_pct(rate)}</div></div>'
        f'<div class="stat"><div class="stat-label">В неделю, шт</div><div class="stat-value">{fmt_num(per_week, 1) if per_week is not None else "нет данных"}</div></div>'
        "</div>"
    )


def _eng_project_table(rows: list, value_cols: list) -> str:
    ths = "".join(f'<th scope="col">{esc(h)}</th>' for h in ["Проект"] + [c[0] for c in value_cols])
    trs = []
    for r in rows:
        cells = "".join(f'<td class="col-num">{c[1](r)}</td>' for c in value_cols)
        trs.append(f'<tr><td>{esc(r.get("project"))}</td>{cells}</tr>')
    return f'<div class="table-card card"><div class="table-wrap"><table class="data mid"><thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table></div></div>'


def build_tab07(report: dict) -> str:
    eng = report.get("engineering") or {}
    if not eng.get("available"):
        return (
            '<section class="section" id="sec-07-unavailable"><div class="section-body">'
            f'<p class="section-desc" style="text-align:left">{esc(eng.get("reason_ru") or "Недоступно.")}</p>'
            f'<p class="hint">{esc(_TAB07_UNAVAILABLE_PARA)}</p></div></section>'
        )

    pipe, dep, cov = eng.get("pipelines") or {}, eng.get("deployments") or {}, eng.get("coverage") or {}

    sec1 = (
        '<section class="section" id="sec-07-pipelines"><div class="section-head">'
        '<span class="section-index">07.1</span><h2 class="section-title">Пайплайны</h2></div>'
        f'<div class="section-body">{_eng_tiles_html(pipe.get("count"), pipe.get("failed"), pipe.get("success_rate_pct"), pipe.get("per_week"))}'
        + table_hint(
            _eng_project_table(
                pipe.get("per_project") or [],
                [("Запусков", lambda r: fmt_int(r.get("count"))), ("Упавших", lambda r: fmt_int(r.get("failed"))), ("Доля успешных, %", lambda r: fmt_pct(r.get("success_rate_pct")))],
            ),
            esc(_TAB07_PIPELINES_PARA),
        )
        + "</div></section>"
    )

    sec2 = (
        '<section class="section" id="sec-07-deployments"><div class="section-head">'
        '<span class="section-index">07.2</span><h2 class="section-title">Деплои</h2></div>'
        f'<div class="section-body">{_eng_tiles_html(dep.get("count"), dep.get("failed"), dep.get("success_rate_pct"), dep.get("per_week"))}'
        + table_hint(
            _eng_project_table(
                dep.get("per_project") or [],
                [("Запусков", lambda r: fmt_int(r.get("count"))), ("Упавших", lambda r: fmt_int(r.get("failed"))), ("Доля успешных, %", lambda r: fmt_pct(r.get("success_rate_pct")))],
            ),
            esc(_TAB07_DEPLOYMENTS_PARA),
        )
        + "</div></section>"
    )

    cov_tiles = (
        '<div class="person-stats stat-grid">'
        f'<div class="stat"><div class="stat-label">Среднее покрытие, %</div><div class="stat-value">{fmt_pct(cov.get("coverage_avg_pct")) if cov.get("coverage_avg_pct") is not None else "нет данных"}</div></div>'
        f'<div class="stat"><div class="stat-label">Число замеров</div><div class="stat-value">{fmt_int(cov.get("sample_count"))}</div></div>'
        "</div>"
    )
    sec3 = (
        '<section class="section" id="sec-07-coverage"><div class="section-head">'
        '<span class="section-index">07.3</span><h2 class="section-title">Покрытие тестами</h2></div>'
        f"<div class=\"section-body\">{cov_tiles}"
        + table_hint(
            _eng_project_table(
                cov.get("per_project") or [],
                [("Покрытие, %", lambda r: fmt_pct(r.get("coverage_avg_pct")) if r.get("coverage_avg_pct") is not None else "нет данных"), ("Число замеров", lambda r: fmt_int(r.get("sample_count")))],
            ),
            esc(_TAB07_COVERAGE_PARA),
        )
        + "</div></section>"
    )

    by_sprint = eng.get("by_sprint") or []
    axis = report.get("sprint_axis") or []
    cat_labels = [s["name"] for s in axis]
    counts_svg = build_multiline_svg(
        "chart-eng-counts", "Пайплайны и деплои по спринтам", cat_labels,
        [("Пайплайны", [b.get("pipeline_count") for b in by_sprint]), ("Деплои", [b.get("deployment_count") for b in by_sprint])],
        "шт", show_trend=False,
    )
    rate_svg = build_multiline_svg(
        "chart-eng-rate", "Успешность CI и деплоев, %", cat_labels,
        [("Пайплайны", [b.get("pipeline_success_rate_pct") for b in by_sprint]), ("Деплои", [b.get("deployment_success_rate_pct") for b in by_sprint])],
        "%", show_trend=False,
    )
    by_sprint_hint = esc(_TAB07_BY_SPRINT_PARA)
    sec4 = (
        '<section class="section" id="sec-07-by-sprint"><div class="section-head">'
        '<span class="section-index">07.4</span><h2 class="section-title">CI/CD по спринтам</h2></div>'
        '<div class="section-body"><div class="chart-grid">'
        + chart_block("chart-eng-counts", "Пайплайны и деплои по спринтам", counts_svg, by_sprint_hint)
        + chart_block("chart-eng-rate", "Успешность CI и деплоев, %", rate_svg, by_sprint_hint)
        + "</div></div></section>"
    )

    window = eng.get("window_applied") or {}
    window_line = (
        "Окно дат применено: MR — {mr}; пайплайны — {p}; деплои — {d}; покрытие — {c}."
    ).format(
        mr=bool_ru(window.get("merge_requests")), p=bool_ru(window.get("pipelines")),
        d=bool_ru(window.get("deployments")), c=bool_ru(window.get("coverage")),
    )
    issues = report.get("gitlab_fetch_issues") or {}
    items = []
    for sp in issues.get("skipped_projects") or []:
        items.append(f'<li>{esc(sp.get("message_ru"))} — {esc(sp.get("project"))} <code>{esc(sp.get("message"))}</code></li>')
    for me in issues.get("mr_fetch_errors") or []:
        author_login = me.get("author") or ""
        author_html = person_name_html(display_name_for_login(report, author_login), author_login) if author_login else EM_DASH
        items.append(f'<li>{esc(me.get("message_ru"))} — {esc(me.get("project"))} / {author_html} <code>{esc(me.get("message"))}</code></li>')
    for dw in issues.get("deployment_warnings") or []:
        cls = ' style="border-color:var(--bad);background:var(--bad-soft)"' if dw.get("code") == "PAGINATION_LIMIT" else ""
        items.append(f'<li{cls}>{esc(dw.get("message_ru"))} — {esc(dw.get("project"))} <code>{esc(dw.get("message"))}</code></li>')
    completeness_html = ("<ul class=\"warn-list\">" + "".join(items) + "</ul>") if items else '<p class="section-desc" style="text-align:left">Данные GitLab получены полностью.</p>'
    sec5 = (
        '<section class="section" id="sec-07-completeness"><div class="section-head">'
        '<span class="section-index">07.5</span><h2 class="section-title">Полнота данных GitLab</h2></div>'
        f'<div class="section-body"><p class="section-desc" style="text-align:left">{esc(window_line)}</p>'
        f'{completeness_html}<p class="hint">{esc(_TAB07_COMPLETENESS_PARA)}</p></div></section>'
    )

    return sec1 + sec2 + sec3 + sec4 + sec5


# ==========================================================================
# Tab 08 — Данные
# ==========================================================================

_TAB08_PARAMS_PARA = (
    "Полный список параметров, с которыми построен отчёт. Тот же набор лежит в out/report.json → "
    "params — по нему запуск можно воспроизвести."
)

_TAB08_HEATMAP_EXPORT_PARA = (
    'Таблица «задача × день» по целевым спринтам в формате, совместимом с jiratools: статус задачи на '
    'каждый рабочий день, колонки «До спринта» и «Конец». Файл out/heatmap.csv — та же таблица с '
    "разделителем «;», BOM и защитой от формул для Excel."
)

_TAB08_BOARD_EXPORT_PARA = (
    "Одна строка на спринт со всеми расчётными метриками спринта. Файл out/board.csv — те же данные с "
    "машинными заголовками (sprint, committed_sp, …) для скриптов."
)

_TAB08_FILES_PARA = (
    "Все артефакты запуска лежат в одной папке (по умолчанию ./out, флаг --out-dir). CSV-файлы "
    "совместимы по именам и колонкам с набором aiIntegrationMetrics; report.json — полные данные "
    "отчёта (схема v2), из него можно перерисовать HTML командой team-metrics report."
)

_OUT_FILES = [
    ("report.json", "полные данные отчёта в схеме v2 — из него перерисовывается HTML", "always"),
    ("report.html", "этот файл — самодостаточный HTML-отчёт", "always"),
    ("gitlab_mrs.csv", "все MR за период: автор, статус, время цикла, размер диффа", "gitlab"),
    ("gitlab_pipelines.csv", "все запуски CI: проект, статус, автор, время", "gitlab"),
    ("gitlab_deployments.csv", "все выкаты: проект, окружение, статус, время", "gitlab"),
    ("gitlab_coverage.csv", "точки покрытия тестами по пайплайнам", "gitlab"),
    ("gitlab_users.csv", "логины и отображаемые имена из GitLab", "gitlab"),
    ("jira_users.csv", "логины и отображаемые имена из Jira", "always"),
    ("jira_issues.csv", "задачи периода: статус, тип, исполнитель, оценки", "always"),
    ("jira_cycle_time.csv", "время цикла каждой завершённой задачи", "always"),
    ("jira_rework.csv", "число возвратов в работу по задаче", "always"),
    ("jira_throughput.csv", "завершённые задачи с датой закрытия", "always"),
    ("sprints.csv", "список спринтов оси анализа с датами", "always"),
    ("jira_by_sprint.csv", "агрегаты Jira по каждому спринту оси", "always"),
    ("gitlab_by_sprint.csv", "агрегаты GitLab по каждому спринту оси", "gitlab"),
    ("report_per_employee.csv", "полный набор персональных метрик, одна строка на сотрудника — не пишется, если нет ни одного сотрудника с данными", "always"),
    ("report_team.csv", "командные итоги, посчитанные по плоским спискам", "always"),
    ("report_merged.csv", "MR, сопоставленные с задачами Jira по ключу", "gitlab"),
    ("heatmap.csv", "тепловая карта в формате jiratools (;, BOM)", "always"),
    ("board.csv", "таблица метрик спринтов с машинными заголовками", "always"),
    ("raw/", "необработанные данные фетчей Jira/GitLab (JSON)", "always"),
]


def _out_files_rows(report: dict) -> list:
    """Filters the static file catalog down to what this run actually
    writes — GitLab-derived CSVs/raw dumps only exist when GitLab was
    configured for the run (report["engineering"]["available"]); the tool
    never writes a headerless CSV for an empty row set."""
    gitlab_available = bool((report.get("engineering") or {}).get("available"))
    rows = []
    for name, desc, category in _OUT_FILES:
        if category == "gitlab" and not gitlab_available:
            continue
        if name == "raw/" and not gitlab_available:
            desc = desc + " — в этом запуске без GitLab, только файлы Jira"
        rows.append((name, desc))
    return rows


def build_tab08(report: dict) -> str:
    params = require(report, "params")
    board = require(report, "board")
    gw = params.get("gitlab_window") or {}
    sprint_ids = params.get("sprint_ids") or []
    sprint_names = params.get("sprint_names") or []
    sprint_text = ", ".join(str(x) for x in sprint_ids) if sprint_ids else (", ".join(sprint_names) if sprint_names else None)
    board_text = f'{board.get("name") or EM_DASH} (id {board.get("id", EM_DASH)})'

    # Every value below is RAW (Jira/GitLab-sourced strings included, e.g.
    # board.name, sprint_names) — `rows_html` is the only place any of it is
    # escaped, and it escapes unconditionally. No data-driven raw-HTML
    # branch may exist here: a value that happens to start with "<" (an
    # attacker-controlled board/sprint name) must render as inert text, not
    # markup.
    param_rows = [
        (col_label(report, "sprint_ids"), sprint_text),
        (col_label(report, "board_id"), board_text),
        (col_label(report, "history_sprint_count"), fmt_int(params.get("history_sprint_count"))),
        (col_label(report, "seed"), fmt_int(params.get("seed"))),
        (col_label(report, "iterations"), fmt_int(params.get("iterations"))),
        (col_label(report, "target_items_requested"), fmt_int(params.get("target_items_requested")) if params.get("target_items_requested") is not None else None),
        (col_label(report, "target_items_resolved"), fmt_int(params.get("target_items_resolved")) if params.get("target_items_resolved") is not None else None),
        (col_label(report, "generated_at"), fmt_datetime(params.get("generated_at"))),
        (col_label(report, "tool_version"), params.get("tool_version")),
        (col_label(report, "out_dir"), params.get("out_dir")),
        (col_label(report, "gitlab_window"), fmt_range(gw.get("start"), gw.get("end")) if gw else None),
        (col_label(report, "gitlab_request_count"), fmt_int(params.get("gitlab_request_count")) if params.get("gitlab_request_count") is not None else None),
        (col_label(report, "gitlab_fetch_mr_details"), bool_ru(params.get("gitlab_fetch_mr_details"))),
        (col_label(report, "gitlab_fetch_pipeline_user"), bool_ru(params.get("gitlab_fetch_pipeline_user"))),
        (col_label(report, "no_gitlab"), bool_ru(params.get("no_gitlab"))),
        (col_label(report, "no_personal"), bool_ru(params.get("no_personal"))),
    ]
    rows_html = "".join(f"<tr><td>{esc(k)}</td><td>{esc_or_dash(v)}</td></tr>" for k, v in param_rows)
    table1 = f'<div class="table-card card"><div class="table-wrap"><table class="data"><thead><tr><th scope="col">Параметр</th><th scope="col">Значение</th></tr></thead><tbody>{rows_html}</tbody></table></div></div>'
    sec1 = (
        '<section class="section" id="sec-08-params"><div class="section-head">'
        '<span class="section-index">08.1</span><h2 class="section-title">Параметры запуска</h2></div>'
        f'<div class="section-body">{table_hint(table1, auto_code_wrap(esc(_TAB08_PARAMS_PARA)))}</div></section>'
    )

    def _export_table(tbl: dict) -> str:
        header_ru = tbl.get("header_ru") or []
        ths = "".join(f"<th scope=\"col\">{esc(h)}</th>" for h in header_ru)
        trs = []
        for row in tbl.get("rows") or []:
            trs.append("<tr>" + "".join(f"<td>{esc_or_dash(c)}</td>" for c in row) + "</tr>")
        return f'<div class="table-card card"><div class="table-wrap scroll-x"><table class="data xwide"><thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table></div></div>'

    export_tables = report.get("export_tables") or {}
    sec2 = (
        '<section class="section" id="sec-08-export-heatmap"><div class="section-head">'
        '<span class="section-index">08.2</span><h2 class="section-title">Экспорт: тепловая карта</h2></div>'
        f'<div class="section-body">{table_hint(_export_table(export_tables.get("heatmap") or {}), auto_code_wrap(esc(_TAB08_HEATMAP_EXPORT_PARA)))}</div></section>'
    )
    sec3 = (
        '<section class="section" id="sec-08-export-board"><div class="section-head">'
        '<span class="section-index">08.3</span><h2 class="section-title">Экспорт: доска</h2></div>'
        f'<div class="section-body">{table_hint(_export_table(export_tables.get("board") or {}), auto_code_wrap(esc(_TAB08_BOARD_EXPORT_PARA)))}</div></section>'
    )

    files_rows = "".join(f"<tr><td><code>{esc(name)}</code></td><td>{esc(desc)}</td></tr>" for name, desc in _out_files_rows(report))
    table4 = f'<div class="table-card card"><div class="table-wrap"><table class="data"><thead><tr><th scope="col">Файл</th><th scope="col">Что внутри</th></tr></thead><tbody>{files_rows}</tbody></table></div></div>'
    sec4 = (
        '<section class="section" id="sec-08-files"><div class="section-head">'
        '<span class="section-index">08.4</span><h2 class="section-title">Файлы папки out/</h2></div>'
        f'<div class="section-body">{table_hint(table4, auto_code_wrap(esc(_TAB08_FILES_PARA)))}</div></section>'
    )

    return sec1 + sec2 + sec3 + sec4


# ==========================================================================
# Tab 09 — Словарь и риски
# ==========================================================================

_TAB09_RISKS_PARA = (
    "Главное правило: сравнивайте только с собственным baseline. Рост скорости при росте доли "
    "неуспешных пайплайнов / rework — тревожный сигнал, повод пересмотреть процесс, а не делать "
    "выводы."
)

_TAB09_ROLES_PARA = (
    "Роль берётся из поля Role задачи Jira или выводится из меток; расшифровки выше используются во "
    "всех таблицах отчёта."
)

_TAB09_STATUSES_PARA = (
    "Статусы показываются как есть, категория — рядом. Переопределение задаётся ключом status_labels "
    "в .team-metrics.json."
)

_TAB09_WARN_PARA = (
    "В самом отчёте предупреждения всегда показаны текстом; этот справочник — для чтения report.json "
    "и логов."
)

_TAB09_METRIC_DEFS_PARA = (
    "Все 28 метрик, которые считает инструмент — GitLab, Jira, связка Jira↔GitLab и инфраструктура "
    "(пайплайны, деплои, покрытие). Машинный ключ метрики показан рядом с названием — это единственное "
    "место в отчёте, где такие ключи видны как есть."
)


def _warn_codes_table_html(codes: dict) -> str:
    rows = "".join(f"<tr><td><code>{esc(code)}</code></td><td>{esc(msg)}</td></tr>" for code, msg in sorted(codes.items()))
    return f'<div class="table-card card"><div class="table-wrap"><table class="data"><thead><tr><th scope="col">Код</th><th scope="col">Что означает</th></tr></thead><tbody>{rows}</tbody></table></div></div>'


def build_tab09(report: dict) -> str:
    risks = report.get("risks") or []
    risks_html = "".join(f'<div class="banner"><div class="banner-body"><b>{esc(r.get("title_ru"))}.</b> {auto_code_wrap(esc(r.get("body_ru")))}</div></div>' for r in risks)
    sec1 = (
        '<section class="section" id="sec-09-risks"><div class="section-head">'
        '<span class="section-index">09.1</span><h2 class="section-title">Риски и на что смотреть</h2></div>'
        f'<div class="section-body">{risks_html}<p class="hint">{esc(_TAB09_RISKS_PARA)}</p></div></section>'
    )

    glossary = report.get("glossary") or []
    dl_items = "".join(f"<dt>{esc(g.get('term'))}</dt><dd>{auto_code_wrap(esc(g.get('definition_ru')))}</dd>" for g in glossary)
    sec2 = (
        '<section class="section" id="sec-09-glossary"><div class="section-head">'
        '<span class="section-index">09.2</span><h2 class="section-title">Пояснения к метрикам (словарь для неспециалиста)</h2></div>'
        f'<div class="section-body"><dl class="glossary">{dl_items}</dl></div></section>'
    )

    mdefs = report.get("metric_defs") or []
    md_rows = "".join(
        "<tr>"
        f'<td>{esc(d.get("label_ru"))} <code>{esc(d.get("key"))}</code></td>'
        f'<td>{esc(d.get("unit_ru"))}</td>'
        f'<td>{esc(_CATEGORY_SOURCE_RU.get(d.get("category"), d.get("category")))}</td>'
        f'<td>{auto_code_wrap(esc(d.get("comment_ru")))}</td>'
        "</tr>"
        for d in mdefs
    )
    table3 = f'<div class="table-card card"><div class="table-wrap"><table class="data wide"><thead><tr><th scope="col">Метрика</th><th scope="col">Ед.</th><th scope="col">Источник</th><th scope="col">Комментарий</th></tr></thead><tbody>{md_rows}</tbody></table></div></div>'
    sec3 = (
        '<section class="section" id="sec-09-metric-defs"><div class="section-head">'
        '<span class="section-index">09.3</span><h2 class="section-title">Полный справочник метрик</h2></div>'
        f'<div class="section-body">{table_hint(table3, esc(_TAB09_METRIC_DEFS_PARA))}</div></section>'
    )

    roles = roles_map(report)
    roles_rows = "".join(f"<tr><td><code>{esc(code)}</code></td><td>{esc(label)}</td></tr>" for code, label in roles.items())
    table4 = f'<div class="table-card card"><div class="table-wrap"><table class="data"><thead><tr><th scope="col">Код</th><th scope="col">Расшифровка</th></tr></thead><tbody>{roles_rows}</tbody></table></div></div>'
    sec4 = (
        '<section class="section" id="sec-09-roles"><div class="section-head">'
        '<span class="section-index">09.4</span><h2 class="section-title">Роли</h2></div>'
        f'<div class="section-body">{table_hint(table4, esc(_TAB09_ROLES_PARA))}</div></section>'
    )

    statuses = statuses_map(report)
    st_rows = "".join(
        f'<tr><td>{esc(name)}</td><td>{esc(entry.get("category_ru"))}</td><td>{esc_or_dash(entry.get("override_ru"))}</td></tr>'
        for name, entry in statuses.items()
    )
    table5 = f'<div class="table-card card"><div class="table-wrap"><table class="data"><thead><tr><th scope="col">Статус (как в Jira)</th><th scope="col">Категория</th><th scope="col">Переопределение из настроек</th></tr></thead><tbody>{st_rows}</tbody></table></div></div>'
    jira_note = labels_of(report).get("jira_label_note_ru") or ""
    sec5 = (
        '<section class="section" id="sec-09-statuses"><div class="section-head">'
        '<span class="section-index">09.5</span><h2 class="section-title">Статусы Jira</h2></div>'
        f'<div class="section-body">{table_hint(table5, auto_code_wrap(esc(_TAB09_STATUSES_PARA)))}'
        f'<p class="section-desc" style="text-align:left;margin-top:10px">{esc(jira_note)}</p></div></section>'
    )

    catalog = warn_catalog(report)
    report_codes = {code: msg for code, msg in catalog.items() if code.startswith("WARN_") or code.startswith("ERR_")}
    transport_codes = {code: msg for code, msg in catalog.items() if code not in report_codes}
    table6a = _warn_codes_table_html(report_codes)
    table6b = _warn_codes_table_html(transport_codes)
    sec6 = (
        '<section class="section" id="sec-09-warnings"><div class="section-head">'
        '<span class="section-index">09.6</span><h2 class="section-title">Предупреждения и ошибки</h2></div>'
        '<div class="section-body">'
        f'<h3>Коды предупреждений и ошибок отчёта (<code>WARN_*</code>, <code>ERR_*</code>)</h3>{table6a}'
        f'<h3>Технические коды запросов к GitLab</h3>{table6b}'
        f'<p class="hint">{esc(_TAB09_WARN_PARA)}</p></div></section>'
    )

    return sec1 + sec2 + sec3 + sec4 + sec5 + sec6


# ==========================================================================
# Top level
# ==========================================================================


def _people_unavailable_block(report: dict) -> str:
    reason = report.get("people_reason_ru") or "Персональные метрики недоступны для этого запуска."
    return (
        '<section class="section" id="sec-people-unavailable"><div class="section-body">'
        f'<p class="section-desc" style="text-align:left">{esc(reason)}</p></div></section>'
    )


def _load_template(template_path: Optional[Path] = None) -> str:
    path = template_path or TEMPLATE_PATH
    return Path(path).read_text(encoding="utf-8")


def render_html(report: dict, *, template_path: Optional[Path] = None) -> str:
    """Renders one self-contained HTML report from a schema-v2 report dict.
    Pure function: no I/O beyond reading the template file."""
    text = strip_scalar_marker_comments(_load_template(template_path))
    ctx: dict = {}

    build_header_ctx(report, ctx)

    people_available = report.get("people_available", True)
    ctx["TAB01_CONTENT"] = build_tab01(report)
    ctx["TAB02_CONTENT"] = build_tab02(report)
    ctx["TAB03_CONTENT"] = build_tab03(report)
    ctx["TAB04_CONTENT"] = build_tab04(report)
    ctx["TAB05_CONTENT"] = build_tab05(report) if people_available else _people_unavailable_block(report)
    ctx["TAB06_CONTENT"] = build_tab06(report) if people_available else _people_unavailable_block(report)
    ctx["TAB07_CONTENT"] = build_tab07(report)
    ctx["TAB08_CONTENT"] = build_tab08(report)
    ctx["TAB09_CONTENT"] = build_tab09(report)

    return finalize(text, ctx)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="render_html", description="Render a schema-v2 report JSON to a self-contained HTML report")
    parser.add_argument("report_json", nargs="?", default=None, help="Path to a report.json file (default: stdin)")
    parser.add_argument("-o", "--out", default=None, help="Output HTML path (default: stdout)")
    parser.add_argument("--template", default=None, help="Override the template path (default: templates/report.html)")
    args = parser.parse_args(argv)

    if args.report_json:
        report = json.loads(Path(args.report_json).read_text(encoding="utf-8"))
    else:
        report = json.loads(sys.stdin.read())

    template_path = Path(args.template) if args.template else None
    html_out = render_html(report, template_path=template_path)

    if args.out:
        Path(args.out).write_text(html_out, encoding="utf-8")
    else:
        sys.stdout.write(html_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
