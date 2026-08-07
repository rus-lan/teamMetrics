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

SUPPORTED_SCHEMA_VERSION = 3


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


def stat_value_html(value_html: str, unit: str = "") -> str:
    """Inner markup for a `.stat-value` tile. A bare em-dash reads as a
    broken tile rather than a deliberate no-data state (§P1-9), so a
    formatted value that came back as the standard EM_DASH renders as a
    small muted «нет данных» label instead."""
    if value_html == EM_DASH:
        return '<span class="stat-nodata">нет данных</span>'
    unit_html = f'<span class="u">{esc(unit)}</span>' if unit else ""
    return f"{value_html}{unit_html}"


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


def shorten_axis_labels(labels: list) -> tuple:
    """Finds the longest run of characters every label in `labels` starts
    with, trims it back to the last shared space/dash/slash boundary, and
    returns (caption, [suffix, ...]) so a caller can hoist the shared
    prefix into one axis caption instead of repeating it on every tick —
    e.g. ["T100-T102 26.1", "T100-T102 26.2"] -> ("T100-T102 26.", ["1", "2"]).
    Returns (None, labels) unchanged when there are fewer than 2 labels,
    any label is empty after stripping the prefix, or the prefix saves
    fewer than 4 characters (not worth hoisting)."""
    labels = [str(x) for x in labels]
    if len(labels) < 2:
        return None, labels
    prefix = labels[0]
    for lab in labels[1:]:
        i = 0
        max_i = min(len(prefix), len(lab))
        while i < max_i and prefix[i] == lab[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            return None, labels
    cut = 0
    for i, ch in enumerate(prefix):
        if ch in " -/–—_.":
            cut = i + 1
    prefix = prefix[:cut]
    if len(prefix) < 4:
        return None, labels
    suffixes = [lab[len(prefix):] for lab in labels]
    if any(not s for s in suffixes):
        return None, labels
    return prefix, suffixes


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


def _tick_label_svg(x: float, y: float, full: str, short: str, rotate: bool) -> str:
    """A single categorical-axis tick label. `rotate` draws it at -45°,
    right-anchored at (x, y), for the fallback case where no common prefix
    could be hoisted into an axis caption and the plain horizontal label
    would collide with its neighbour (§P0-3)."""
    if rotate:
        return (
            f'<text class="c-tick-label" x="{x:.1f}" y="{y:.1f}" text-anchor="end" '
            f'transform="rotate(-45 {x:.1f} {y:.1f})"><title>{esc(full)}</title>{esc(short)}</text>'
        )
    return f'<text class="c-tick-label" x="{x:.1f}" y="{y:.1f}" text-anchor="middle"><title>{esc(full)}</title>{esc(short)}</text>'


_TICK_LABEL_CHAR_W = 6.2  # px/char estimate for the 11px .c-tick-label font;
# generous vs. a straight font-size ratio, since Cyrillic glyphs run wider
# than Latin ones in the report's default sans stack.
_ROTATE_LABEL_MARGIN = 10.0  # px of slack past the estimated rotated-text extent


def _rotated_label_reach(short_labels: Iterable) -> float:
    """How far a -45° rotated tick label (`_tick_label_svg`, drawn
    text-anchor="end" so it pivots around its own right/bottom corner)
    reaches beyond its anchor point, in both the down and the left
    direction. Scales with the longest label actually drawn instead of a
    fixed guess, since names vary in length."""
    labels = [str(s) for s in short_labels]
    if not labels:
        return 0.0
    max_w = max(len(s) * _TICK_LABEL_CHAR_W for s in labels)
    return max_w / math.sqrt(2) + _ROTATE_LABEL_MARGIN


def prepare_category_axis(labels: list) -> tuple:
    """Shared P0-3 fix for every categorical-axis chart builder: tries to
    hoist a common prefix into one caption (`shorten_axis_labels`); when
    that does not apply and there are enough categories to collide at a
    narrow chart width, falls back to rotating the full labels -45°
    instead of guessing at a shorter axis width. Returns
    (caption_or_None, short_labels, rotate: bool)."""
    caption, short_labels = shorten_axis_labels(labels)
    rotate = caption is None and len(labels) > 6
    return caption, short_labels, rotate


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
        f'<svg id="{esc(chart_id)}" class="chart" style="min-width:{W}px" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
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


def donut_block(chart_id: str, title: str, items: list, center_label: str, hint_html: str, is_rate_pair: bool = False) -> str:
    """Chooses a real donut when 2+ categories carry data, a compact
    numeric stat tile when exactly one does — a full-circle ring conveys
    no comparison, just a number in the middle (§P1-7) — and the standard
    chart_block empty-data box when every value is zero or missing.

    `center_label` is only safe to reuse as-is for the single-segment
    stat tile when it is a total that necessarily equals the one
    surviving segment (e.g. an issue-type count where the total IS that
    segment's own count). `is_rate_pair` marks the other shape — items
    are a success/fail-style pair and `center_label` is a rate pinned to
    the FIRST item — where reusing it verbatim would label whichever
    segment survived with a rate that describes the other one. When set,
    the tile instead shows that segment's own share of the pair."""
    vals = [(str(label), float(v)) for label, v in items if v is not None and v > 0]
    if not vals:
        return chart_block(chart_id, title, None, hint_html)
    if len(vals) == 1:
        label, v = vals[0]
        value_html = center_label
        if is_rate_pair:
            total = sum(float(x) for _, x in items if x is not None)
            if total > 0:
                value_html = fmt_pct(v / total * 100)
        return (
            f'<div class="chartbox card stat-tile-box" id="cb-{esc(chart_id)}"><h3>{esc(title)}</h3>'
            f'<div class="stat"><div class="stat-label">{esc(label)}</div>'
            f'<div class="stat-value">{esc(value_html)}</div></div>'
            f'<p class="hint">{hint_html}</p></div>'
        )
    svg = build_donut_svg(chart_id, title, items, center_label)
    return chart_block(chart_id, title, svg, hint_html)


def build_hbar_svg(chart_id: str, title: str, items: list, unit: str = "", logins: Optional[list] = None) -> Optional[str]:
    """items = [(label, value_or_none), ...] — a None row prints «нет данных».
    Every bar shares one colour (§P1-7): the row is already labelled by
    name, so a 21-colour palette added no information. `logins`, if given,
    parallels `items` and wraps each row in `<g data-login="...">` so the
    people multi-select (tabs 05/06) can show/hide it."""
    n = len(items)
    if n == 0:
        return None
    W, row_h = 480, 30
    H = max(80, 44 + n * row_h + 12)
    label_x = 172
    plot_w = W - label_x - 62
    vals = [v for _, v in items if v is not None]
    vmax = max(vals) if vals else 1
    if vmax <= 0:
        vmax = 1

    rows = []
    for i, (label, value) in enumerate(items):
        full = str(label)
        short = truncate(full, 22)
        y = 34 + i * row_h
        login = logins[i] if logins and i < len(logins) else None
        g_open = f'<g data-login="{esc(login)}">' if login else "<g>"
        if value is None:
            rows.append(
                g_open
                + f'<text class="c-hbar-label" x="{label_x - 8}" y="{y + 13}" text-anchor="end">'
                f'<title>{esc(full)}</title>{esc(short)}</text>'
                f'<text class="c-hbar-nodata" x="{label_x}" y="{y + 13}">нет данных</text></g>'
            )
            continue
        bar_w = max((value / vmax) * plot_w, 2)
        val_txt = f"{fmt_num(value, 1)} {unit}".strip()
        rows.append(
            g_open
            + f'<text class="c-hbar-label" x="{label_x - 8}" y="{y + 13}" text-anchor="end">'
            f'<title>{esc(full)}</title>{esc(short)}</text>'
            f'<rect class="c-bar" x="{label_x}" y="{y}" width="{bar_w:.1f}" height="18" rx="3">'
            f'<title>{esc(full)}: {esc(val_txt)}</title></rect>'
            f'<text class="c-hbar-value" x="{label_x + bar_w + 6:.1f}" y="{y + 14}">{esc(val_txt)}</text></g>'
        )
    return (
        f'<svg id="{esc(chart_id)}" class="chart" style="min-width:{W}px" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">' + "".join(rows) + "</svg>"
    )


def build_grouped_bar_svg(chart_id: str, title: str, cat_labels: list, series: list, unit: str = "", rotate_labels: bool = False, cat_logins: Optional[list] = None) -> Optional[str]:
    """series = [(name_ru, [values_by_category]), ...]. Category count here
    is typically the number of people (unbounded, can exceed 20 — §P0-2),
    so unlike the sprint-axis bar charts this one keeps its width
    proportional to `cat_labels` and renders through `chart_block_wide`
    (horizontal scroll, no squeeze) instead of trying to fit into one
    fixed-width column. `cat_logins`, if given, parallels `cat_labels` and
    wraps each person's bar group in `<g data-login="...">` for the
    people multi-select (§control C)."""
    n_cats = len(cat_labels)
    n_series = len(series)
    if n_cats == 0 or n_series == 0:
        return None
    short_labels = [truncate(str(cat), 16) for cat in cat_labels]
    W = max(420, 70 + n_cats * 64)
    bottom = 200 + (18 if rotate_labels else 0)
    plot_h = bottom - 46 - 16 - (18 if rotate_labels else 0)
    left = 40
    chart_w = W - left - 24
    xstep = chart_w / max(n_cats, 1)
    bar_w = max((xstep - 18) / max(n_series, 1), 10)

    # Rotated labels (§P0-3 fallback) pivot on their own right/bottom
    # corner (`_tick_label_svg`), so a long name reaches further down AND
    # further left than a fixed reserve accounts for (§tab05 cycle-time
    # regression — real names overflowed the viewBox by up to 35px). `W`
    # and `left` grow by the same amount so `chart_w`/`xstep`/`bar_w`
    # above stay exactly as computed — only the canvas gets wider, not
    # the bars.
    reach = _rotated_label_reach(short_labels) if rotate_labels else 0.0
    first_tick_x = left + xstep / 2
    extra_left = math.ceil(max(0.0, reach - first_tick_x)) if rotate_labels else 0
    left += extra_left
    W += extra_left

    all_vals = [v for _n, vals in series for v in vals if v is not None]
    vmax = max(all_vals) if all_vals else 1
    if vmax <= 0:
        vmax = 1
    H = bottom + max(52, math.ceil(14 + reach + 22))

    parts = [
        f'<svg id="{esc(chart_id)}" class="chart-svg" style="min-width:{W}px" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
    ]
    parts.append(f'<line class="c-axis" x1="{left}" y1="{bottom}" x2="{W - 12}" y2="{bottom}"/>')
    for i, cat in enumerate(cat_labels):
        xg = left + i * xstep
        login = cat_logins[i] if cat_logins and i < len(cat_logins) else None
        parts.append(f'<g data-login="{esc(login)}">' if login else "<g>")
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
        short = short_labels[i]
        tick_x = xg + xstep / 2
        parts.append(_tick_label_svg(tick_x, bottom + 14, label, short, rotate_labels))
        parts.append("</g>")

    ly, lx = H - 22, 8
    for j, (sname, _vals) in enumerate(series):
        cls = _series_class(j)
        parts.append(f'<rect class="{cls}" x="{lx}" y="{ly}" width="10" height="10" rx="2"/>')
        parts.append(f'<text class="c-legend-text" x="{lx + 14}" y="{ly + 9}">{esc(sname)}</text>')
        lx += 34 + len(sname) * 6.5
    parts.append("</svg>")
    return "".join(parts)


def build_stacked_bar_svg(chart_id: str, title: str, cat_labels: list, series: list, unit: str = "", ref_line: Optional[float] = None, rotate_labels: bool = False) -> Optional[str]:
    """series = [(name_ru, [values_by_category]), ...], segments stacked
    bottom-up in series order. Maps the full range of partial sums reached
    while stacking (not just each category's final total), so a segment
    with a negative running sum still lands inside the viewBox instead of
    the fixed zero-at-bottom assumption pushing it off-canvas.

    Width is a FIXED baseline (§P0-2) rather than growing with `cat_labels`
    — this builder is used for the bounded sprint axis, and a viewBox
    wider than the chart-grid column it renders into is what shrank axis
    text down to 3-6px in production. Categories that still don't fit get
    shortened labels from `shorten_axis_labels` (called by the tab
    builder) or, failing that, `rotate_labels`."""
    n_cats = len(cat_labels)
    if n_cats == 0 or not series:
        return None
    W = 480
    bottom = 200 + (18 if rotate_labels else 0)
    plot_h = bottom - 46 - 16 - (18 if rotate_labels else 0)
    left = 40
    chart_w = W - left - 24
    xstep = chart_w / max(n_cats, 1)
    bar_w = max(xstep - 16, 6)

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
    H = bottom + 52

    parts = [
        f'<svg id="{esc(chart_id)}" class="chart" style="min-width:{W}px" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
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
        short = truncate(label, 10)
        tick_x = xg + bar_w / 2
        parts.append(_tick_label_svg(tick_x, bottom + 14, label, short, rotate_labels))
    if ref_line is not None:
        ry = y_of(ref_line)
        parts.append(f'<line class="c-marker" x1="{left}" y1="{ry:.1f}" x2="{W - 12}" y2="{ry:.1f}"/>')
        parts.append(f'<text class="c-marker-label" x="{W - 12}" y="{ry - 4:.1f}" text-anchor="end">{fmt_num(ref_line, 0)}</text>')

    ly, lx = H - 22, 8
    for j, (sname, _vals) in enumerate(series):
        cls = _series_class(j)
        parts.append(f'<rect class="{cls}" x="{lx}" y="{ly}" width="10" height="10" rx="2"/>')
        parts.append(f'<text class="c-legend-text" x="{lx + 14}" y="{ly + 9}">{esc(sname)}</text>')
        lx += 34 + len(sname) * 6.5
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
    rotate_labels: bool = False,
) -> Optional[str]:
    """Single-series vertical bars over a bounded category axis (sprints).
    `ref_lines` (list of (value, label_text)) draws multiple horizontal
    threshold lines at once (e.g. several named targets on the same %
    axis); `ref_line` is the single-value convenience form. Width is a
    fixed §P0-2 baseline, not proportional to `values` — see
    `build_stacked_bar_svg` for why."""
    n = len(values)
    if n == 0:
        return None
    W = 480
    H = 260 + (18 if rotate_labels else 0)
    x0, x1 = 46.0, W - 14.0
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
    tick_y = y_base + 16
    for x, cat, v in zip(xs, cat_labels, values):
        label = str(cat)
        short = truncate(label, 10)
        tick_x = x + bar_w / 2
        if v is None:
            bars.append(_tick_label_svg(tick_x, tick_y, label, short, rotate_labels))
            continue
        h = y_base - y_of(v)
        val_txt = fmt_num(v, 1)
        bar_title = f"{label}: {val_txt} {unit}".strip()
        bars.append(
            f'<rect class="c-bar" x="{x:.2f}" y="{y_of(v):.2f}" width="{bar_w:.2f}" height="{max(h, 1):.2f}" rx="2">'
            f'<title>{esc(bar_title)}</title></rect>'
        )
        bars.append(_tick_label_svg(tick_x, tick_y, label, short, rotate_labels))

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
        f'<svg id="{esc(chart_id)}" class="chart" style="min-width:{W}px" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
        f"<g>{grid}</g><g>{y_labels}</g>"
        f'<line class="c-axis" x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}"/>'
        + "".join(bars)
        + "".join(ref_html_parts)
        + "</svg>"
    )


def build_forecast_histogram_svg(chart_id: str, title: str, bins: list, percentiles: list, unit_ru: str) -> Optional[str]:
    """bins = [(sp_value, count), ...]; percentiles = [(sp_value, label), ...].

    Fixes §P1-4: the old renderer marked P50/P85/P95 with the categorical
    vbar builder's horizontal `ref_lines` — a Y-axis threshold marker —
    even though a percentile is a value on the X axis (story points), not
    the Y axis (iteration count); all three collapsed into one
    near-invisible horizontal band at the bottom. Bars sit on a
    continuous numeric X axis keyed by their `sp` value, so a percentile
    gets an exact X position instead of being forced onto a bar's
    categorical slot. Markers are vertical, span the full plot height,
    and stagger their labels onto two rows when two percentiles land
    within ~40px of each other."""
    bins = [(float(sp), int(count)) for sp, count in bins if sp is not None]
    if not bins:
        return None
    W, H = 480, 282
    x0, x1 = 46.0, W - 14.0
    y_top, y_base = 44.0, 210.0

    sp_values = [sp for sp, _c in bins]
    counts = [c for _sp, c in bins]
    p_values = [p for p, _l in percentiles if p is not None]
    lo = min(sp_values + p_values)
    hi = max(sp_values + p_values)
    if hi <= lo:
        hi = lo + 1.0
    span = hi - lo

    def X(v: float) -> float:
        return x0 + (v - lo) / span * (x1 - x0)

    n_ticks = 5
    step_val = nice_step(max(counts) if counts else 1.0, n_ticks)
    top = step_val * n_ticks

    def Y(v: float) -> float:
        return y_base - (v / top) * (y_base - y_top) if top else y_base

    sorted_sp = sorted(set(sp_values))
    gaps = [b - a for a, b in zip(sorted_sp, sorted_sp[1:]) if b > a]
    gap = min(gaps) if gaps else span or 1.0
    bar_w = max((gap / span) * (x1 - x0) * 0.72, 6)

    grid = "".join(f'<line class="c-grid" x1="{x0}" y1="{Y(step_val * i):.1f}" x2="{x1}" y2="{Y(step_val * i):.1f}"/>' for i in range(1, n_ticks + 1))
    y_tick_decimals = axis_label_decimals([step_val * i for i in range(0, n_ticks + 1)])
    y_labels = "".join(
        f'<text class="c-unit-label" x="{x0 - 8}" y="{Y(step_val * i) + 4:.1f}" text-anchor="end">{fmt_num(step_val * i, y_tick_decimals)}</text>'
        for i in range(0, n_ticks + 1)
    )

    bars = []
    for sp, count in bins:
        x = X(sp)
        h = y_base - Y(count)
        val_txt = fmt_int(count)
        bar_title = f"{fmt_num(sp, 1)} {unit_ru}: {val_txt} прогонов".strip()
        bars.append(
            f'<rect class="c-bar" x="{x - bar_w / 2:.2f}" y="{Y(count):.2f}" width="{bar_w:.2f}" '
            f'height="{max(h, 1):.2f}" rx="2"><title>{esc(bar_title)}</title></rect>'
        )
    x_ticks = "".join(
        f'<text class="c-tick-label" x="{X(sp):.1f}" y="{y_base + 16:.0f}" text-anchor="middle">{fmt_num(sp, 1)}</text>'
        for sp in sorted_sp
    )

    marker_positions = sorted(((X(p), lab) for p, lab in percentiles if p is not None), key=lambda t: t[0])
    markers = []
    # Greedy 2-row bin-packing keyed by each label's own estimated text
    # width (labels are full Russian phrases like "безопасный внешний
    # срок", not just "P95" — a fixed pixel gap collided regardless of
    # how long the label actually was). Each label goes in row 0 unless
    # it would overlap the last label already placed there, in which
    # case it drops to row 1.
    row_right_edge = [float("-inf"), float("-inf")]
    for x, lab in marker_positions:
        half_w = 3.3 * len(str(lab)) + 6
        row = 0 if x - half_w >= row_right_edge[0] else 1
        row_right_edge[row] = x + half_w
        label_y = y_top - 6 - (14 if row == 1 else 0)
        markers.append(f'<line class="c-marker-v" x1="{x:.1f}" y1="{y_top}" x2="{x:.1f}" y2="{y_base}"/>')
        markers.append(f'<text class="c-marker-v-label" x="{x:.1f}" y="{label_y:.1f}" text-anchor="middle">{esc(lab)}</text>')

    return (
        f'<svg id="{esc(chart_id)}" class="chart" style="min-width:{W}px" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
        f"<g>{grid}</g><g>{y_labels}</g>"
        f'<line class="c-axis" x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}"/>'
        + "".join(bars)
        + x_ticks
        + "".join(markers)
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
    rotate_labels: bool = False,
    external_legend: bool = False,
    series_logins: Optional[list] = None,
) -> Optional[str]:
    """series = [(name_ru, [values_or_none_by_category]), ...]. Width is a
    fixed §P0-2 baseline matching the chart-grid column, not proportional
    to `labels` — see `build_stacked_bar_svg`.

    `external_legend=True` skips the in-SVG legend entirely (the caller
    then builds one from the same `series` list via `chart_legend_html`,
    §P1-6 — a legend outside the viewBox does not shrink with it and does
    not eat into plot height). `series_logins`, when given, parallels
    `series` and wraps each person's path+points in `<g data-login="...">`
    so the people multi-select (tabs 05/06, §control C) can toggle a
    whole series at once."""
    n = len(labels)
    has_any = any(v is not None for _n, vals in series for v in vals)
    if n == 0 or not series or not has_any:
        return None

    left, right, top = 46.0, 14.0, 42.0
    W = 480

    if external_legend:
        legend_rows = 0
    else:
        legend_rows = 1
        lx = left
        for sname, _vals in series:
            w = 24 + len(sname) * 6.2
            if lx + w > W - right:
                lx = left
                legend_rows += 1
            lx += w
    extra_bottom = 18 if rotate_labels else 0
    if legend_rows > 1:
        H = 300 + 16 * (legend_rows - 1) + extra_bottom
        bottom = 44 + 16 * (legend_rows - 1) + extra_bottom
    else:
        H = 300 + extra_bottom
        bottom = 44 + extra_bottom
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
        f'<svg id="{esc(chart_id)}" class="chart" style="min-width:{W}px" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="{esc(title)}" xmlns="http://www.w3.org/2000/svg">'
    ]
    grid_values = [ymin + (ymax - ymin) * k / 4 for k in range(5)]
    grid_decimals = axis_label_decimals(grid_values)
    for v in grid_values:
        y = Y(v)
        parts.append(f'<line class="c-grid" x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}"/>')
        parts.append(f'<text class="c-unit-label" x="{left - 6}" y="{y + 3:.1f}" text-anchor="end">{fmt_num(v, grid_decimals)}</text>')

    tick_y = H - bottom + 14
    for i, lab in enumerate(labels):
        txt = str(lab)
        short = truncate(txt, 10)
        parts.append(_tick_label_svg(X(i), tick_y, txt, short, rotate_labels))

    for rv in ref_lines:
        if rv is None:
            continue
        ry = Y(rv)
        parts.append(f'<line class="c-marker" x1="{left}" y1="{ry:.1f}" x2="{W - right}" y2="{ry:.1f}"/>')
        parts.append(f'<text class="c-marker-label" x="{W - right}" y="{ry - 4:.1f}" text-anchor="end">{fmt_num(rv, 0)}</text>')

    for si, (sname, vals) in enumerate(series):
        cls = _series_class(si)
        login = series_logins[si] if series_logins and si < len(series_logins) else None
        g_attr = f' data-login="{esc(login)}"' if login else ""
        parts.append(f"<g{g_attr}>")
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
        parts.append("</g>")

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

    if not external_legend:
        ly, lx = H - 8, left
        for si, (sname, _vals) in enumerate(series):
            cls = _series_class(si)
            w = 24 + len(sname) * 6.2
            if lx + w > W - right:
                lx = left
                ly -= 16
            parts.append(f'<rect class="{cls}" x="{lx}" y="{ly - 9}" width="10" height="10" rx="2"/>')
            parts.append(f'<text class="c-legend-text" x="{lx + 14}" y="{ly}">{esc(sname)}</text>')
            lx += w
    parts.append("</svg>")
    return "".join(parts)


_PEOPLE_FILTER_EMPTY_TEXT = "Текущий фильтр не выбирает ни одного человека — отметьте хотя бы одного, чтобы увидеть данные."


def _people_filter_empty_note() -> str:
    return f'<p class="filter-empty">{esc(_PEOPLE_FILTER_EMPTY_TEXT)}</p>'


def chart_block(chart_id: str, title: str, svg_or_none: Optional[str], hint_html: str, caption: Optional[str] = None, legend_html: str = "", people_scope: bool = False) -> str:
    inner = f'<div class="scroll-x">{svg_or_none}</div>' if svg_or_none is not None else '<p class="empty">Недостаточно данных для построения графика.</p>'
    caption_html = f'<p class="axis-caption">{esc(caption)}…</p>' if caption else ""
    scoped = people_scope and svg_or_none is not None
    scope_attr = " data-people-scope" if scoped else ""
    empty_note = _people_filter_empty_note() if scoped else ""
    return (
        f'<div class="chartbox card"{scope_attr} id="cb-{esc(chart_id)}"><h3>{esc(title)}</h3>{caption_html}{inner}{empty_note}{legend_html}'
        f'<p class="hint">{hint_html}</p></div>'
    )


def _table_scroll_note(total_rows: Optional[int]) -> str:
    """A height-capped `.table-wrap` (420px, no `no-cap`/`scroll-x`) clips
    rows below the fold with only a visual fade — this spells out the real
    row count so a reader knows there's more instead of assuming the table
    just ends there. `total_rows` must be the caller's own real row count;
    every capped table gets one of these, not just the ones somebody
    remembered to wire up."""
    if total_rows is not None and total_rows > 10:
        row_word = ru_plural(total_rows, "строка", "строки", "строк")
        return f'<p class="scroll-note">Показаны не все строки — всего {total_rows} {row_word}, прокрутите таблицу, чтобы увидеть остальные.</p>'
    return ""


def table_hint(table_html: str, hint_html: str, total_rows: Optional[int] = None) -> str:
    return f'{table_html}{_table_scroll_note(total_rows)}<p class="hint">{hint_html}</p>'


def chart_block_wide(chart_id: str, title: str, svg_or_none: Optional[str], hint_html: str, people_scope: bool = False) -> str:
    """Like `chart_block`, but for a chart whose natural width grows with
    an unbounded category count (people, not sprints) — wraps the SVG in
    `.scroll-x` instead of letting the grid squeeze it into one column,
    which is what caused the §P0-2 viewBox-vs-container mismatch."""
    if svg_or_none is None:
        return chart_block(chart_id, title, None, hint_html)
    scoped = people_scope
    scope_attr = " data-people-scope" if scoped else ""
    empty_note = _people_filter_empty_note() if scoped else ""
    return (
        f'<div class="chartbox card"{scope_attr} id="cb-{esc(chart_id)}"><h3>{esc(title)}</h3>'
        f'<div class="scroll-x">{svg_or_none}</div>{empty_note}'
        f'<p class="hint">{hint_html}</p></div>'
    )


def chart_legend_html(items: list) -> str:
    """items = [(name, css_class, login_or_None), ...]. External HTML
    legend (§P1-6) for charts with too many series for an in-SVG legend to
    stay both space-efficient and readable — a legend entry here always
    corresponds to a drawn series, since both are built from the same
    list. `login`, when given, lets the people multi-select (tabs 05/06)
    hide the legend entry together with its series."""
    if not items:
        return ""
    parts = ['<div class="chart-legend">']
    for name, cls, login in items:
        attr = f' data-login="{esc(login)}"' if login else ""
        parts.append(
            f'<span class="lg-item"{attr}><i class="lg-swatch {esc(cls)}"></i>{esc(name)}</span>'
        )
    parts.append("</div>")
    return "".join(parts)


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
            '<svg class="chart-svg" style="min-width:760px" viewBox="0 0 760 344" role="img" aria-label="нет данных">'
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

    aria = f"Burndown, остаток в {unit_label}: " + ", ".join(fmt_num(v, 1) for v in remaining)
    dots_html = "".join(dots)

    svg = (
        f'<svg id="{esc(chart_id)}" class="chart-svg" style="min-width:760px" viewBox="0 0 760 344" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="{esc(aria)}">'
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
        raise TemplateError(f"report is missing required schema v3 key: {key!r}")
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

_TAB01_ISSUE_DIST_PARA = "Типы задач — по полю «Тип» завершённых задач Jira за период."

_TAB01_ENG_DIST_PARA_TMPL = (
    "Доля успеха — (все запуски − упавшие) / все запуски по данным GitLab; отменённые и пропущенные "
    "запуски считаются неупавшими. Частота: {pw} в неделю."
)


def build_recommendations_html(report: dict) -> str:
    """§CONTRACT: `recommendations` restores the standalone section the
    previous version had — rendered prominently on tab 01 right under the
    KPI tiles, numbered, colour-coded by severity."""
    recos = report.get("recommendations") or []
    intro = report.get("recommendations_intro_ru") or ""
    intro_html = f'<p class="reco-intro">{esc(intro)}</p>' if intro else ""
    if not recos:
        empty = report.get("recommendations_empty_ru") or "Рекомендаций нет."
        return f'{intro_html}<p class="section-desc" style="text-align:left">{esc(empty)}</p>'
    items = []
    for r in recos:
        severity = r.get("severity") or "warn"
        items.append(
            f'<li class="reco-item" data-severity="{esc(severity)}"><div class="reco-body">'
            f'<b>{esc(r.get("metric_ru"))}: {esc(r.get("value_ru"))}</b>'
            f'<div class="reco-signal">{esc(r.get("signal_ru"))}</div>'
            f'<div class="reco-action"><b>Действие:</b> {esc(r.get("action_ru"))}</div>'
            "</div></li>"
        )
    return f'{intro_html}<ul class="reco-list">{"".join(items)}</ul>'


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

    sec_reco = (
        '<section class="section" id="sec-01-recommendations"><div class="section-head">'
        '<span class="section-index">01.2</span><h2 class="section-title">Рекомендации</h2></div>'
        f'<div class="section-body">{build_recommendations_html(report)}</div></section>'
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
        f'<div class="stat-value">{stat_value_html(value, unit)}</div></div>'
        for label, value, unit in overview_cards
    )
    gw = (report.get("params") or {}).get("gitlab_window") or {}
    if gw.get("start") or gw.get("end"):
        gw_period_text = f'{fmt_date(gw.get("start"))}–{fmt_date(gw.get("end"))}'
    else:
        gw_period_text = "не определён (GitLab не настроен для этого запуска)"
    sec2 = (
        '<section class="section" id="sec-01-overview"><div class="section-head">'
        '<span class="section-index">01.3</span><h2 class="section-title">Команда за период</h2></div>'
        f'<div class="section-body"><div class="person-stats stat-grid">{cards_html}</div>'
        f'<p class="hint">{esc(_TAB01_OVERVIEW_PARA.format(period=gw_period_text))}</p>'
        "</div></section>"
    )

    issue_dist = overview.get("issue_type_dist") or {}
    pipe = (eng.get("pipelines") or {}) if eng.get("available") else {}
    dep = (eng.get("deployments") or {}) if eng.get("available") else {}
    pipe_items = [("Успешно", (pipe.get("count") or 0) - (pipe.get("failed") or 0)), ("Упало", pipe.get("failed") or 0)] if pipe else []
    dep_items = [("Успешно", (dep.get("count") or 0) - (dep.get("failed") or 0)), ("Упало", dep.get("failed") or 0)] if dep else []

    pw_pipe = fmt_num(pipe.get("per_week"), 1) if pipe.get("per_week") is not None else "нет данных"
    pw_dep = fmt_num(dep.get("per_week"), 1) if dep.get("per_week") is not None else "нет данных"
    issue_dist_hint = esc(_TAB01_ISSUE_DIST_PARA)
    pipeline_hint = esc(_TAB01_ENG_DIST_PARA_TMPL.format(pw=pw_pipe))
    deploy_hint = esc(_TAB01_ENG_DIST_PARA_TMPL.format(pw=pw_dep))
    sec3 = (
        '<section class="section" id="sec-01-dist"><div class="section-head">'
        '<span class="section-index">01.4</span><h2 class="section-title">Распределение и стабильность</h2></div>'
        '<div class="section-body"><div class="donut-row">'
        + donut_block("chart-issue-type-dist", "Распределение типов задач", list(issue_dist.items()), fmt_int(overview.get("tasks_done_total")), issue_dist_hint)
        + donut_block("chart-pipeline-success", "Доля успешных пайплайнов", pipe_items, fmt_pct(pipe.get("success_rate_pct")) if pipe else EM_DASH, pipeline_hint, is_rate_pair=True)
        + donut_block("chart-deploy-success", "Доля успешных деплоев", dep_items, fmt_pct(dep.get("success_rate_pct")) if dep else EM_DASH, deploy_hint, is_rate_pair=True)
        + "</div></div></section>"
    )

    notes = report.get("semantics_notes") or []
    notes_html = "".join(f"<li>{esc(n)}</li>" for n in notes)
    sec4 = (
        '<section class="section" id="sec-01-warnings"><div class="section-head">'
        '<span class="section-index">01.5</span><h2 class="section-title">Предупреждения</h2></div>'
        f'<div class="section-body">{warnings_list_html(report.get("warnings") or [])}'
        '<details class="thresholds"><summary>Как читать цифры (важные оговорки)</summary>'
        f'<ul class="warn-list">{notes_html}</ul></details></div></section>'
    )

    return sec1 + sec_reco + sec2 + sec3 + sec4


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
        f'<div class="unit-panels"><div class="unit-panel"><div class="scroll-x">{svg_items}</div></div>'
        f'<div class="unit-panel"><div class="scroll-x">{svg_sp}</div></div></div>'
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
    """§control A: burndown/heatmap/breakdown now cover every sprint on
    `sprint_axis`, not just the target sprint(s) — a <select> at the top
    lets the reader jump between them. Every sprint block is rendered and
    left visible in the markup (§graceful degradation); JS narrows the
    view to the selected one instead of revealing hidden content, so a
    reader with JavaScript disabled still sees every sprint, one after
    another."""
    axis = require(report, "sprint_axis")
    primary = primary_target_axis(report)
    primary_sid = primary["id"]

    burndown_by_id = {b["sprint_id"]: b for b in report.get("burndown") or []}
    heatmap_by_id = {h["sprint_id"]: h for h in report.get("heatmap") or []}
    breakdown_by_id = {b["sprint_id"]: b for b in report.get("issue_breakdown") or []}

    options_html = "".join(
        f'<option value="{esc(s["id"])}"{" selected" if s["id"] == primary_sid else ""}>{esc(s["name"])}</option>'
        for s in axis
    )
    select_html = (
        '<div class="control-bar"><label for="sprint-select">Спринт:</label>'
        f'<select id="sprint-select" data-sprint-select>{options_html}</select></div>'
    )

    blocks = []
    for s in axis:
        sid = s["id"]  # raw — used only as a dict key against burndown/heatmap/breakdown
        sid_html = esc(sid)  # escaped — every HTML id/attribute below uses this
        name = s["name"]
        bd_points = (burndown_by_id.get(sid) or {}).get("points") or []
        svg_items, _hw1 = build_burndown_svg(f"chart-burndown-items-{sid_html}", bd_points, "items")
        svg_sp, _hw2 = build_burndown_svg(f"chart-burndown-sp-{sid_html}", bd_points, "sp")
        burndown_html = build_unit_toggle(f"burndown-{sid_html}", svg_items, svg_sp)
        sec_burndown = (
            f'<div class="chartbox card"><h3>Burndown — {esc(name)}</h3>{burndown_html}'
            f'<p class="hint">{esc(_TAB02_BURNDOWN_PARA)}</p></div>'
        )

        hm = heatmap_by_id.get(sid) or {"days": [], "rows": []}
        if hm.get("rows"):
            heatmap_table = build_heatmap_table(report, hm)
        else:
            heatmap_table = empty_state_html("В этом спринте нет задач для тепловой карты.")
        sec_heatmap = (
            f'<div class="table-block"><h3>Тепловая карта статусов — {esc(name)}</h3>{heatmap_table}'
            f'<p class="hint">{auto_code_wrap(esc(_TAB02_HEATMAP_PARA))}</p></div>'
        )

        bd = breakdown_by_id.get(sid) or {"rows": []}
        if bd.get("rows"):
            breakdown_table = build_breakdown_table(report, bd)
        else:
            breakdown_table = empty_state_html("В этом спринте нет задач.")
        sec_breakdown = (
            f'<div class="table-block"><h3>Разбивка по задачам (по спринту) — {esc(name)}</h3>{breakdown_table}'
            f'<p class="hint">{esc(_TAB02_BREAKDOWN_PARA)}</p></div>'
        )

        blocks.append(
            f'<section class="section" id="sec-02-{sid_html}" data-sprint-block data-sprint-id="{sid_html}"><div class="section-head">'
            f'<h2 class="section-title">{esc(name)}</h2></div>'
            f'<div class="section-body">{sec_burndown}{sec_heatmap}{sec_breakdown}</div></section>'
        )
    return select_html + "".join(blocks)


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


# §P2-10: the by-sprint pipeline/deployment charts already live on tab 07
# (with fuller context: window-applied notes, project breakdown). Rendering
# the identical team_series entries here too duplicated two full charts.
_TAB03_SKIP_TEAM_SERIES_KEYS = {"pipelines_deployments", "ci_deploy_success"}


def build_tab03(report: dict) -> str:
    sprints = require(report, "sprints")
    axis = require(report, "sprint_axis")
    cat_labels = [s["name"] for s in axis]
    axis_caption, cat_labels_short, rotate = prepare_category_axis(cat_labels)

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
            build_stacked_bar_svg("chart-board-commitment", "Commitment (SP)", cat_labels_short, [("Обязательство", committed_sp), ("Поставлено", delivered_sp)], "SP", rotate_labels=rotate),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["commitment"])), caption=axis_caption,
        ),
        chart_block(
            "chart-board-performance", "Performance (Say/Do), %",
            build_vbar_svg("chart-board-performance", "Performance (Say/Do), %", cat_labels_short, performance_pct, "%", ref_line=80.0, rotate_labels=rotate),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["performance"])), caption=axis_caption,
        ),
        chart_block(
            "chart-board-load", "Загрузка, %",
            build_vbar_svg("chart-board-load", "Загрузка, %", cat_labels_short, load_pct, "%", ref_line=100.0, rotate_labels=rotate),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["load"])), caption=axis_caption,
        ),
        chart_block(
            "chart-board-scope-change", "Изменение объёма (SP)",
            build_stacked_bar_svg(
                "chart-board-scope-change", "Изменение объёма (SP)", cat_labels_short,
                [("Добавлено", scope_added), ("Изменение оценки", scope_est), ("Убрано", scope_removed)], "SP", rotate_labels=rotate,
            ),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["scope_change"])), caption=axis_caption,
        ),
        chart_block(
            "chart-board-velocity", "Velocity (SP)",
            build_multiline_svg(
                "chart-board-velocity", "Velocity (SP)", cat_labels_short,
                [("Velocity", velocity_sp), ("SMA5", velocity_sma5)], "SP", show_trend=False, rotate_labels=rotate,
            ),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["velocity"])), caption=axis_caption,
        ),
        chart_block(
            "chart-board-throughput", "Throughput (задач)",
            build_vbar_svg("chart-board-throughput", "Throughput (задач)", cat_labels_short, throughput, "задач", rotate_labels=rotate),
            auto_code_wrap(esc(_TAB03_BOARD_CHART_PARA["throughput"])), caption=axis_caption,
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
        if item.get("key") in _TAB03_SKIP_TEAM_SERIES_KEYS:
            continue
        series = [(s["name_ru"], s["values"]) for s in item.get("series") or []]
        svg = build_multiline_svg(
            f'chart-team-{id_safe(item["key"])}', item["title_ru"], cat_labels_short, series, item.get("unit_ru") or "",
            show_trend=bool(item.get("show_trend")), rotate_labels=rotate,
        )
        ts_charts.append(chart_block(f'chart-team-{id_safe(item["key"])}', item["title_ru"], svg, auto_code_wrap(esc(item.get("hint_ru") or "")), caption=axis_caption))
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
    "Прогноз строится по истории Story Points, поставленных за закрытые спринты. Добавьте больше "
    "закрытых спринтов в базу (--history) или дождитесь данных."
)

_TAB04_HISTOGRAM_PARA = (
    "Распределение исходов симуляции: по горизонтали — Story Points, поставленные за спринт, по "
    "вертикали — сколько итераций bootstrap-симуляции дали такой исход. Вертикальные линии — "
    "перцентили из плиток выше."
)

_TAB04_CV_PARA = (
    "CV (коэффициент вариации) — насколько нестабильна поставка по спринтам: среднеквадратичное "
    "отклонение к среднему, ×100. Выше 50% — перцентили ненадёжны."
)


def _forecast_scope_body(scope_id: str, unit_ru: str, data: dict) -> str:
    """One forecast scope (team or one person) — §CONTRACT `forecast.team`
    / one entry of `forecast.people`. Every percentile shows its own
    `label_ru` next to the number, per the user's explicit request."""
    percentiles = data.get("percentiles") or []
    tiles = "".join(
        f'<div class="stat"><div class="stat-label">{esc(p.get("label_ru"))}</div>'
        f'<div class="stat-value">{stat_value_html(fmt_num(p.get("sp"), 1), unit_ru)}</div></div>'
        for p in percentiles
    )
    marker_pairs = [(p.get("sp"), p.get("label_ru") or "") for p in percentiles if p.get("sp") is not None]
    hist = data.get("histogram") or []
    bins = [(h.get("sp"), h.get("count")) for h in hist]
    chart_id = f"chart-forecast-{scope_id}"
    hist_svg = build_forecast_histogram_svg(chart_id, "Гистограмма исходов симуляции", bins, marker_pairs, unit_ru)

    cv_block = ""
    if data.get("cv_warning_ru"):
        cv_block = f'<div class="banner"><span class="banner-tag">CV</span><div class="banner-body">{esc(data["cv_warning_ru"])}</div></div>'

    basis = data.get("basis_ru") or ""
    stats_line = (
        f'<p class="section-desc" style="text-align:left;margin:8px 0">Среднее: '
        f'<b>{fmt_num(data.get("mean_sp"), 1)} {esc(unit_ru)}</b> · Выборка: '
        f'<b>{fmt_int(data.get("sample_sprints"))}</b> спринтов.</p>'
    )
    return (
        f'<div class="person-stats stat-grid">{tiles}</div>'
        + (f'<p class="section-desc" style="text-align:left;margin:8px 0">{esc(basis)}</p>' if basis else "")
        + stats_line
        + chart_block(chart_id, "Гистограмма исходов симуляции", hist_svg, esc(_TAB04_HISTOGRAM_PARA))
        + cv_block
        + f'<p class="hint">{esc(_TAB04_CV_PARA)}</p>'
    )


def _forecast_person_scope(login: str) -> str:
    """Namespaces a person's forecast scope value so a login can never
    collide with the team's "team" sentinel value (a login literally
    named `team` would otherwise match the same <option>/data attribute
    as the whole-team scope)."""
    return "person-" + login


def build_tab04(report: dict) -> str:
    """§control B: `forecast.team` renders by default; a <select> —
    «Вся команда» plus one option per `forecast.people` — swaps in that
    person's percentiles/histogram. Every scope block is rendered up
    front (§graceful degradation: with JS off, both the team scope and
    every person's scope are visible, stacked); JS narrows to the
    selected one."""
    forecast = report.get("forecast") or {}
    team = forecast.get("team")
    people = forecast.get("people") or []
    # `forecast.available` reflects the TEAM scope only — report_data.py
    # still returns a `people` entry per person independently, some of
    # which can be `available: true` even when the team-wide bootstrap
    # didn't have enough closed sprints. Only bail out to the single
    # "unavailable" message when there is truly nothing to show — team
    # AND every person unavailable — otherwise render the select with
    # whatever scopes DO have data, and show team's own error inline
    # instead of hiding person forecasts that exist.
    if team is None and not any(p.get("available") for p in people):
        error = forecast.get("error") or {}
        msg = error.get("message_ru") or "Прогноз недоступен."
        detail = error.get("detail")
        detail_html = f" <code>{esc(detail)}</code>" if detail else ""
        return (
            '<section class="section" id="sec-04-forecast"><div class="section-head">'
            '<span class="section-index">04.1</span><h2 class="section-title">Прогноз Monte-Carlo</h2></div>'
            f'<div class="section-body"><p class="section-desc" style="text-align:left">{esc(msg)}{detail_html}</p>'
            f'<p class="hint">{esc(_TAB04_UNAVAILABLE_PARA)}</p></div></section>'
        )

    unit_ru = forecast.get("unit_ru") or "SP"

    options = ['<option value="team" selected>Вся команда</option>']
    for p in people:
        options.append(f'<option value="{esc(_forecast_person_scope(p["login"]))}">{esc(p.get("display_name") or p["login"])}</option>')
    select_html = (
        '<div class="control-bar"><label for="forecast-scope-select">Показать:</label>'
        f'<select id="forecast-scope-select" data-forecast-select>{"".join(options)}</select></div>'
    )

    blocks = []
    if team:
        blocks.append(f'<div data-forecast-scope="team"><h3>Вся команда</h3>{_forecast_scope_body("team", unit_ru, team)}</div>')
    else:
        error = forecast.get("error") or {}
        msg = error.get("message_ru") or "Командный прогноз недоступен."
        detail = error.get("detail")
        detail_html = f" <code>{esc(detail)}</code>" if detail else ""
        blocks.append(
            f'<div data-forecast-scope="team"><h3>Вся команда</h3>'
            f'<p class="section-desc" style="text-align:left">{esc(msg)}{detail_html}</p></div>'
        )

    for p in people:
        login = p["login"]
        name = p.get("display_name") or login
        scope = _forecast_person_scope(login)
        if not p.get("available"):
            reason = p.get("unavailable_reason_ru") or "Недостаточно данных для прогноза по этому человеку."
            blocks.append(
                f'<div data-forecast-scope="{esc(scope)}" data-login="{esc(login)}"><h3>{esc(name)}</h3>'
                f'<p class="section-desc" style="text-align:left">{esc(reason)}</p></div>'
            )
            continue
        blocks.append(
            f'<div data-forecast-scope="{esc(scope)}" data-login="{esc(login)}"><h3>{esc(name)}</h3>'
            f"{_forecast_scope_body(id_safe(login), unit_ru, p)}</div>"
        )

    return (
        '<section class="section" id="sec-04-forecast"><div class="section-head">'
        '<span class="section-index">04.1</span><h2 class="section-title">Прогноз Monte-Carlo</h2></div>'
        f'<div class="section-body">{select_html}{"".join(blocks)}</div></section>'
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


def build_people_multiselect_html(people: list, select_id: str) -> str:
    """§control C: a checkbox per person plus a "select all" button, all
    hidden until `html.js` (mirrors `.theme-switch`) since it does nothing
    without the listener that toggles `.tm-hidden` — the underlying
    content stays fully visible without it."""
    if not people:
        return ""
    checks = "".join(
        f'<label class="pf-check"><input type="checkbox" checked data-login-check="{esc(p["login"])}"> {esc(p["display_name"])}</label>'
        for p in people
    )
    return (
        f'<div class="people-filter" data-people-filter="{esc(select_id)}">'
        f'<button type="button" class="pf-all" data-select-all="{esc(select_id)}">Выбрать всех</button>{checks}</div>'
    )


def build_tab05(report: dict) -> str:
    people = report.get("people") or []
    parts = [f'<p class="hint">{esc(_TAB05_INTRO_PARA)}</p>', build_people_multiselect_html(people, "tab05")]

    names = [p["display_name"] for p in people]
    logins = [p["login"] for p in people]

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
        chart_block(cid, title, build_hbar_svg(cid, title, _hbar_items(key), unit, logins=logins), compare_hint, people_scope=True)
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
    grouped_svg = build_grouped_bar_svg(
        "chart-cmp-cycle-grouped", "Cycle time по сотрудникам (среднее и медиана, ч)", names, grouped_series, "ч",
        rotate_labels=True, cat_logins=logins,
    )
    sec2 = (
        '<section class="section" id="sec-05-cycle"><div class="section-head">'
        '<span class="section-index">05.2</span><h2 class="section-title">Cycle time по сотрудникам (среднее и медиана, ч)</h2></div>'
        f'<div class="section-body">{chart_block_wide("chart-cmp-cycle-grouped", "Cycle time по сотрудникам (среднее и медиана, ч)", grouped_svg, esc(_TAB05_CYCLE_PARA), people_scope=True)}</div></section>'
    )

    if people:
        mdefs = [d for d in (report.get("metric_defs") or []) if d.get("scope") != "team"]
        cols_meta = labels_of(report).get("columns") or {}
        extra_rows = [(k, cols_meta.get(k, k)) for k in _EXTRA_PERSON_KEYS if any(k in (p.get("metrics") or {}) for p in people)]
        header_people = "".join(f'<th class="col-num" data-login="{esc(p["login"])}">{esc(p["display_name"])}</th>' for p in people)

        def _row(label: str, key: str, is_pct: bool) -> str:
            cells = "".join(
                f'<td class="col-num" data-login="{esc(p["login"])}">'
                f'{(fmt_num(p["metrics"].get(key), 1) + "%") if (is_pct and p["metrics"].get(key) is not None) else fmt_num(p["metrics"].get(key), 2)}</td>'
                for p in people
            )
            return f"<tr><td>{esc(label)}</td>{cells}</tr>"

        rows_html = [_row(d["label_ru"], d["key"], bool(d.get("is_pct"))) for d in mdefs]
        rows_html += [_row(label, key, key.endswith("_pct")) for key, label in extra_rows]
        table = (
            '<div class="table-card card" data-people-scope><div class="table-wrap"><table class="data wide">'
            f'<thead><tr><th scope="col">Метрика</th>{header_people}</tr></thead>'
            f'<tbody>{"".join(rows_html)}</tbody></table></div>{_people_filter_empty_note()}</div>'
        )
        sec3_body = table_hint(table, esc(_TAB05_TABLE_PARA), total_rows=len(rows_html))
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
        no_pipeline_people = []
        for i, p in enumerate(people):
            dist = p.get("issue_type_dist") or {}
            cid1, cid2 = f"chart-p-dist-{i}", f"chart-p-pipe-{i}"
            dist_block = donut_block(cid1, f'Распределение типов задач — {p["display_name"]}', list(dist.items()), fmt_int(p["metrics"].get("tasks_done")), dist_hint)
            rate = p["metrics"].get("pipeline_success_rate_pct")
            if rate is None:
                no_pipeline_people.append(p["display_name"])
                person_cards.append(f'<div class="donut-pair" data-login="{esc(p["login"])}">{dist_block}</div>')
                continue
            d2_items = [("Успешно", rate), ("Не успешно", 100.0 - rate)]
            pipe_block = donut_block(cid2, f'Доля успешных пайплайнов — {p["display_name"]}', d2_items, fmt_pct(rate), dist_hint, is_rate_pair=True)
            person_cards.append(f'<div class="donut-pair" data-login="{esc(p["login"])}">{dist_block}{pipe_block}</div>')
        cards_body = "".join(person_cards)
        if no_pipeline_people:
            cards_body += f'<p class="hint">Нет данных о пайплайнах GitLab: {esc(", ".join(no_pipeline_people))}.</p>'
        # Collapsed by default — this is the tallest single block in the
        # report (a donut pair per person), and <details> keeps every
        # card reachable with a native click, no JS required, unlike the
        # tm-hidden mechanism JS uses elsewhere for the people filter.
        who = ru_plural(len(people), "сотрудник", "сотрудника", "сотрудников")
        sec4_body = (
            f'<details class="donut-wall" data-people-scope><summary>Показать персональные распределения — {len(people)} {who}</summary>'
            f'<div class="donut-wall-body">{cards_body}</div>{_people_filter_empty_note()}</details>'
        )
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
            login = r.get("assignee_login") or ""
            rows_html2.append(
                f'<tr data-login="{esc(login)}">'
                f"<td>{person_name_html(r.get('assignee_display_name') or 'Без исполнителя', login)}</td>"
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
            '<div class="table-card card" data-people-scope><div class="table-wrap"><table class="data mid">'
            "<thead><tr><th scope=\"col\">Исполнитель</th><th scope=\"col\">Обязательство, SP</th>"
            "<th scope=\"col\">Обязательство, задач</th><th scope=\"col\">Поставлено, SP</th>"
            "<th scope=\"col\">Поставлено, задач</th><th scope=\"col\">Performance, %</th>"
            "<th scope=\"col\">Velocity, SP</th><th scope=\"col\">Throughput, задач</th></tr></thead>"
            f'<tbody>{"".join(rows_html2)}</tbody></table></div>{_table_scroll_note(len(rows))}{_people_filter_empty_note()}</div>'
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


def _people_series_charts(report: dict, keys: list, with_trend: bool, axis_caption: Optional[str], cat_labels_short: list, rotate: bool) -> str:
    """A person count that can exceed 20 makes an in-SVG legend both
    unreadable (§P1-6: 6.5px text eating a quarter of the chart height)
    and prone to entries with no matching line, so this always uses the
    external HTML legend and wraps each series in a `data-login` group for
    the people multi-select (§control C)."""
    by_key = {item["key"]: item for item in report.get("people_series") or []}
    charts = []
    for key in keys:
        item = by_key.get(key)
        if not item:
            continue
        series_items = item.get("series") or []
        series = [(s["display_name"], s["values"]) for s in series_items]
        logins = [s["login"] for s in series_items]
        trend_values = _mean_by_index([s["values"] for s in series_items]) if with_trend else None
        chart_id = f"chart-people-{id_safe(key)}"
        svg = build_multiline_svg(
            chart_id, item["title_ru"], cat_labels_short, series, item.get("unit_ru") or "",
            show_trend=with_trend, trend_values=trend_values, rotate_labels=rotate,
            external_legend=True, series_logins=logins,
        )
        legend_html = ""
        if svg:
            # A legend entry must never exist without a matching drawn
            # mark (§P1-6) — a series with zero non-None values draws
            # nothing (no path, no point), so it is dropped here too.
            # Index `i` still comes from the unfiltered list so the
            # legend swatch colour matches the class build_multiline_svg
            # assigned that series internally.
            legend_items = [
                (s["display_name"], _series_class(i), s["login"])
                for i, s in enumerate(series_items)
                if any(v is not None for v in s["values"])
            ]
            legend_html = chart_legend_html(legend_items)
        charts.append(
            chart_block(chart_id, item["title_ru"], svg, auto_code_wrap(esc(item.get("hint_ru") or "")), caption=axis_caption, legend_html=legend_html, people_scope=True)
        )
    return "".join(charts)


def build_tab06(report: dict) -> str:
    axis = require(report, "sprint_axis")
    cat_labels = [s["name"] for s in axis]
    axis_caption, cat_labels_short, rotate = prepare_category_axis(cat_labels)
    people = report.get("people") or []
    parts = [f'<p class="hint">{esc(_TAB06_INTRO_PARA)}</p>', build_people_multiselect_html(people, "tab06")]

    jira_keys = ["throughput_by_person", "task_cycle_time_by_person", "rework_by_person", "story_points_by_person", "qa_estimation_by_person"]
    sec1 = (
        '<section class="section" id="sec-06-jira"><div class="section-head">'
        '<span class="section-index">06.1</span><h2 class="section-title">Задачи и оценки (Jira)</h2></div>'
        f'<div class="section-body"><div class="chart-grid">{_people_series_charts(report, jira_keys, False, axis_caption, cat_labels_short, rotate)}</div></div></section>'
    )

    gitlab_keys = ["mr_count_by_person", "pr_cycle_time_by_person", "mr_weight_by_person"]
    sec2 = (
        '<section class="section" id="sec-06-gitlab"><div class="section-head">'
        '<span class="section-index">06.2</span><h2 class="section-title">Merge-реквесты (GitLab)</h2></div>'
        f'<div class="section-body"><p class="hint">{esc(_TAB06_TREND_PARA)}</p>'
        f'<div class="chart-grid">{_people_series_charts(report, gitlab_keys, True, axis_caption, cat_labels_short, rotate)}</div></div></section>'
    )

    cards = []
    for p in people:
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
            f'<div class="person-card card" data-login="{esc(p["login"])}"><div class="person-head"><div>'
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
        '<div class="section-body"><div data-people-scope>'
        f'<div class="person-cards">{"".join(cards)}</div>{_people_filter_empty_note()}</div>'
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
    per_week_html = stat_value_html(fmt_num(per_week, 1) if per_week is not None else EM_DASH)
    return (
        '<div class="person-stats stat-grid">'
        f'<div class="stat"><div class="stat-label">Всего запусков</div><div class="stat-value">{stat_value_html(fmt_int(count))}</div></div>'
        f'<div class="stat"><div class="stat-label">Упавших</div><div class="stat-value">{stat_value_html(fmt_int(failed))}</div></div>'
        f'<div class="stat"><div class="stat-label">Доля успешных, %</div><div class="stat-value">{stat_value_html(fmt_pct(rate))}</div></div>'
        f'<div class="stat"><div class="stat-label">В неделю, шт</div><div class="stat-value">{per_week_html}</div></div>'
        "</div>"
    )


def _eng_project_table(rows: list, value_cols: list, empty_text: str = "Нет данных.") -> str:
    """§P1-9: an empty `rows` renders one explicit no-data line instead of
    a `<thead>` with headers over an empty `<tbody>`."""
    if not rows:
        return empty_state_html(empty_text)
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
                empty_text="Нет данных о пайплайнах.",
            ),
            esc(_TAB07_PIPELINES_PARA),
            total_rows=len(pipe.get("per_project") or []),
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
                empty_text="Нет данных о деплоях.",
            ),
            esc(_TAB07_DEPLOYMENTS_PARA),
            total_rows=len(dep.get("per_project") or []),
        )
        + "</div></section>"
    )

    cov_tiles = (
        '<div class="person-stats stat-grid">'
        f'<div class="stat"><div class="stat-label">Среднее покрытие, %</div><div class="stat-value">{stat_value_html(fmt_pct(cov.get("coverage_avg_pct")) if cov.get("coverage_avg_pct") is not None else EM_DASH)}</div></div>'
        f'<div class="stat"><div class="stat-label">Число замеров</div><div class="stat-value">{stat_value_html(fmt_int(cov.get("sample_count")))}</div></div>'
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
                empty_text="Нет данных о покрытии тестами.",
            ),
            esc(_TAB07_COVERAGE_PARA),
            total_rows=len(cov.get("per_project") or []),
        )
        + "</div></section>"
    )

    by_sprint = eng.get("by_sprint") or []
    axis = report.get("sprint_axis") or []
    cat_labels = [s["name"] for s in axis]
    axis_caption, cat_labels_short, rotate = prepare_category_axis(cat_labels)
    counts_svg = build_multiline_svg(
        "chart-eng-counts", "Пайплайны и деплои по спринтам", cat_labels_short,
        [("Пайплайны", [b.get("pipeline_count") for b in by_sprint]), ("Деплои", [b.get("deployment_count") for b in by_sprint])],
        "шт", show_trend=False, rotate_labels=rotate,
    )
    rate_svg = build_multiline_svg(
        "chart-eng-rate", "Успешность CI и деплоев, %", cat_labels_short,
        [("Пайплайны", [b.get("pipeline_success_rate_pct") for b in by_sprint]), ("Деплои", [b.get("deployment_success_rate_pct") for b in by_sprint])],
        "%", show_trend=False, rotate_labels=rotate,
    )
    by_sprint_hint = esc(_TAB07_BY_SPRINT_PARA)
    sec4 = (
        '<section class="section" id="sec-07-by-sprint"><div class="section-head">'
        '<span class="section-index">07.4</span><h2 class="section-title">CI/CD по спринтам</h2></div>'
        '<div class="section-body"><div class="chart-grid">'
        + chart_block("chart-eng-counts", "Пайплайны и деплои по спринтам", counts_svg, by_sprint_hint, caption=axis_caption)
        + chart_block("chart-eng-rate", "Успешность CI и деплоев, %", rate_svg, by_sprint_hint, caption=axis_caption)
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
        items.append(f'<li>{esc(sp.get("message_ru"))} — {esc(sp.get("project"))}</li>')
    for me in issues.get("mr_fetch_errors") or []:
        author_login = me.get("author") or ""
        author_html = person_name_html(display_name_for_login(report, author_login), author_login) if author_login else EM_DASH
        items.append(f'<li>{esc(me.get("message_ru"))} — {esc(me.get("project"))} / {author_html}</li>')
    for dw in issues.get("deployment_warnings") or []:
        cls = ' style="border-color:var(--bad);background:var(--bad-soft)"' if dw.get("code") == "PAGINATION_LIMIT" else ""
        projects_count = dw.get("projects_count") or 0
        projects = dw.get("projects") or []
        project_word = ru_plural(projects_count, "проект", "проекта", "проектов")
        details = ""
        if projects:
            proj_items = "".join(f"<li>{esc(pr)}</li>" for pr in projects)
            details = f"<details><summary>Список проектов</summary><ul>{proj_items}</ul></details>"
        items.append(
            f'<li{cls}>{esc(dw.get("message_ru"))} — затронуто {projects_count} {project_word}{details}</li>'
        )
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
    "совместимы по именам с набором aiIntegrationMetrics — и по колонкам везде, кроме "
    "report_per_employee.csv (с 3.1.0 в нём больше нет deployment_count/deployment_failed/"
    "deployment_fail_rate); report.json — полные данные отчёта (схема v3), из него можно "
    "перерисовать HTML командой team-metrics report."
)

_OUT_FILES = [
    ("report.json", "полные данные отчёта в схеме v3 — из него перерисовывается HTML", "always"),
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
        f'<div class="section-body">{table_hint(table1, auto_code_wrap(esc(_TAB08_PARAMS_PARA)), total_rows=len(param_rows))}</div></section>'
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

    out_files = _out_files_rows(report)
    files_rows = "".join(f"<tr><td><code>{esc(name)}</code></td><td>{esc(desc)}</td></tr>" for name, desc in out_files)
    table4 = f'<div class="table-card card"><div class="table-wrap"><table class="data"><thead><tr><th scope="col">Файл</th><th scope="col">Что внутри</th></tr></thead><tbody>{files_rows}</tbody></table></div></div>'
    sec4 = (
        '<section class="section" id="sec-08-files"><div class="section-head">'
        '<span class="section-index">08.4</span><h2 class="section-title">Файлы папки out/</h2></div>'
        f'<div class="section-body">{table_hint(table4, auto_code_wrap(esc(_TAB08_FILES_PARA)), total_rows=len(out_files))}</div></section>'
    )

    sec5 = _build_allowlist_section(report)

    return sec1 + sec2 + sec3 + sec4 + sec5


_TAB08_ALLOWLIST_PARA = (
    "employees в .team-metrics.json ограничивает, какие люди попадают в персональные и "
    "инженерные метрики — полезно, когда в Jira/GitLab есть сервисные или внештатные аккаунты, "
    "которых не нужно считать частью команды."
)


def _build_allowlist_section(report: dict) -> str:
    allowlist = (report.get("params") or {}).get("allowlist")
    if not allowlist:
        return ""
    applied = allowlist.get("applied")
    rows = [
        ("Применён", bool_ru(applied)),
        ("Людей в списке", fmt_int(allowlist.get("configured_count"))),
    ]
    rows_html = "".join(f"<tr><td>{esc(k)}</td><td>{esc_or_dash(v)}</td></tr>" for k, v in rows)
    excluded = allowlist.get("excluded_logins") or []
    missing = allowlist.get("missing_logins") or []
    lists_html = ""
    if excluded:
        items = "".join(f"<li>{esc(display_name_for_login(report, lg))}</li>" for lg in excluded)
        lists_html += f"<h3>Исключены (есть в данных, не в списке)</h3><ul class=\"warn-list\">{items}</ul>"
    if missing:
        items = "".join(f"<li><code>{esc(lg)}</code></li>" for lg in missing)
        lists_html += f"<h3>В списке, но не найдены в данных</h3><ul class=\"warn-list\">{items}</ul>"
    note = allowlist.get("note_ru") or ""
    table = f'<div class="table-card card"><div class="table-wrap"><table class="data"><tbody>{rows_html}</tbody></table></div></div>'
    return (
        '<section class="section" id="sec-08-allowlist"><div class="section-head">'
        '<span class="section-index">08.5</span><h2 class="section-title">Allowlist сотрудников</h2></div>'
        f'<div class="section-body">{table_hint(table, auto_code_wrap(esc(_TAB08_ALLOWLIST_PARA)), total_rows=len(rows))}{lists_html}'
        + (f'<p class="section-desc" style="text-align:left;margin-top:10px">{esc(note)}</p>' if note else "")
        + "</div></section>"
    )


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
    table3 = f'<div class="table-card card"><div class="table-wrap no-cap"><table class="data wide"><thead><tr><th scope="col">Метрика</th><th scope="col">Ед.</th><th scope="col">Источник</th><th scope="col">Комментарий</th></tr></thead><tbody>{md_rows}</tbody></table></div></div>'
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
        f'<div class="section-body">{table_hint(table4, esc(_TAB09_ROLES_PARA), total_rows=len(roles))}</div></section>'
    )

    statuses = statuses_map(report)
    st_rows = "".join(
        f'<tr><td>{esc(name)}</td><td>{esc(entry.get("category_ru"))}</td><td>{esc_or_dash(entry.get("override_ru"))}</td></tr>'
        for name, entry in statuses.items()
    )
    table5 = f'<div class="table-card card"><div class="table-wrap no-cap"><table class="data"><thead><tr><th scope="col">Статус (как в Jira)</th><th scope="col">Категория</th><th scope="col">Переопределение из настроек</th></tr></thead><tbody>{st_rows}</tbody></table></div></div>'
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
    note_a = f'<p class="scroll-note">Всего кодов: {len(report_codes)}.</p>' if len(report_codes) > 10 else ""
    note_b = f'<p class="scroll-note">Всего кодов: {len(transport_codes)}.</p>' if len(transport_codes) > 10 else ""
    sec6 = (
        '<section class="section" id="sec-09-warnings"><div class="section-head">'
        '<span class="section-index">09.6</span><h2 class="section-title">Предупреждения и ошибки</h2></div>'
        '<div class="section-body">'
        f'<h3>Коды предупреждений и ошибок отчёта (<code>WARN_*</code>, <code>ERR_*</code>)</h3>{table6a}{note_a}'
        f'<h3>Технические коды запросов к GitLab</h3>{table6b}{note_b}'
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
