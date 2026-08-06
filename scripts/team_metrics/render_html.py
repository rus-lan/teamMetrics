"""Renders a `build_combined_report()` dict into one self-contained HTML file.

Reads `templates/report.html` (a token-templated copy of the design
prototype, `.research/design/report-prototype.html`) and substitutes every
`{{TOKEN}}` placeholder with data computed from the report dict. Stdlib only
— no jinja2, no external templating, no network access from the rendered
output.

Two kinds of placeholder live in the template:

- scalar tokens: a plain `{{TOKEN}}` sitting where one escaped value belongs
  (text, or inside an attribute).
- block regions: `<!-- {{X_START}} --> ... <!-- {{X_END}} -->` markers.
  A "fixed" region (KPI tiles, ENG tiles, footer params, forecast
  percentiles) has its content written out in full in the template already
  and only the marker comments need to disappear. A "repeat" region
  (board rows, heatmap rows, person rows/cards, project rows, ...) holds one
  item template between the markers that gets rendered once per data item
  and concatenated.

See `.research/design/DESIGN-NOTES.md` for the full token inventory and the
product decisions (status thresholds, delta-direction table, the four
empty-value states) this module implements.
"""

from __future__ import annotations

import argparse
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
ARROW_UP = "&#9650;"
ARROW_DOWN = "&#9660;"
ARROW_FLAT = "&#9670;"

TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class TemplateError(Exception):
    """Raised when the template is missing a marker/token this generator needs."""


# --------------------------------------------------------------------------
# Escaping / formatting primitives
# --------------------------------------------------------------------------


def esc(value: Any) -> str:
    """HTML-escapes any value bound for text or an attribute. Every value that
    ultimately comes from Jira/GitLab (summaries, names, labels, projects,
    error strings) must go through this — never interpolated raw."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def esc_or_dash(value: Any) -> str:
    if value is None or value == "":
        return EM_DASH
    return esc(value)


def fmt_num(value: Optional[float], decimals: int = 2) -> str:
    """Rounds to `decimals`, drops a trailing `.0`/`.00`. None -> em dash."""
    if value is None:
        return EM_DASH
    f = round(float(value), decimals)
    if f == int(f):
        return str(int(f))
    s = f"{f:.{decimals}f}"
    s = s.rstrip("0").rstrip(".")
    return s


def fmt_int(value: Optional[float]) -> str:
    if value is None:
        return EM_DASH
    return str(int(value))


def fmt_signed(value: Optional[float], decimals: int = 2, unit: str = "") -> str:
    if value is None:
        return EM_DASH
    sign = "+" if value >= 0 else MINUS
    return f"{sign}{fmt_num(abs(value), decimals)}{unit}"


def ru_plural(n: float, one: str, few: str, many: str) -> str:
    """Picks the Russian noun form for a count: 1 (not 11) -> `one`,
    2-4 (not 12-14) -> `few`, everything else -> `many`. Each argument may
    be a whole phrase (e.g. "календарных дня") when an agreeing adjective
    needs to travel with the noun. For a noun already governed by an outer
    oblique case (genitive after "до"/"без", dative after "по", ...) where
    Russian collapses few/many into one shared form, pass the same string
    for `few` and `many`."""
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


# --------------------------------------------------------------------------
# Template block plumbing
# --------------------------------------------------------------------------


def _marker(token: str) -> str:
    return f"<!-- {{{{{token}}}}} -->"


_SCALAR_MARKER_RE = re.compile(r"<!--\s*\{\{([A-Z0-9_]+)\}\}\s*-->")


def strip_scalar_marker_comments(text: str) -> str:
    """Drops the documentation-only `<!-- {{TOKEN}} -->` comment that
    precedes each scalar placeholder, keeping only the real `{{TOKEN}}`
    substitution point that follows it. Block region markers (`..._START`
    / `..._END`) are left in place — for those the comment IS the
    substitution point, not a duplicate of one — so every value is
    substituted exactly once instead of once per occurrence."""

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
    """For a 'fixed' region whose content is already fully written in the
    template: just drop the two marker comments, keep everything between."""
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
    is an axis max at or above `value`. Rounding a plain `nice_max(value) /
    n_ticks` instead does not keep every intermediate tick round (e.g. max
    20 over 6 ticks gives steps of 3.33, which then round to uneven gaps
    like 3,4,3,3,4,3) — picking the step first and multiplying it back up
    keeps every tick, not just the top one, a clean number."""
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


def linspace(lo: float, hi: float, n: int) -> list:
    if n <= 1:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def spark_status_class(status: str) -> str:
    return {"good": "good", "warn": "warn", "bad": "bad", "none": "mute"}.get(status, "mute")


def build_spark_svg(values: list, status_class: str) -> str:
    """8-KPI / 5-ENG tile sparkline. Handles 0/1/N points and a flat (all
    equal) series without dividing by zero."""
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


# ==========================================================================
# Report-shape helpers
# ==========================================================================


def _target_entries(report: dict) -> list:
    return [(i, s) for i, s in enumerate(report.get("sprints") or []) if s.get("target")]


def primary_target(report: dict) -> tuple:
    """The most recent target sprint — the one the KPI row, burndown and
    heatmap sections describe. `build_report`/`build_combined_report`
    guarantee at least one target sprint."""
    targets = _target_entries(report)
    if not targets:
        raise TemplateError("report has no target sprint entries")
    return targets[-1]


def base_entries(report: dict) -> list:
    return [s for s in (report.get("sprints") or []) if not s.get("target")]


def history_len_at(sprints: list, idx: int, cap: int = 5) -> int:
    """Count of non-target entries strictly preceding index `idx`, capped —
    mirrors report_data._velocity_history_for exactly, so this is an exact
    (not heuristic) reconstruction of whether SMA5 had any history to average."""
    n = 0
    for j in range(idx):
        if not sprints[j].get("target"):
            n += 1
    return min(n, cap)


def div0_flags(sprints: list, idx: int) -> dict:
    """Exact DIV0 determination for a board_table/KPI row, derived from that
    row's own metrics fields — mirrors metrics.py's ratio_pct denominators
    literally, so this needs no heuristics."""
    m = sprints[idx]["payload"]["metrics"]
    return {
        "performance_pct": m["committed_sp"] <= 0,
        "load_pct": m["velocity_sma5_sp"] <= 0,
        "scope_change_pct": m["committed_sp"] <= 0,
        "velocity_sma5_sp": history_len_at(sprints, idx) == 0,
        "closure_pct_items": (m["committed_items"] + m["scope_added_items"] - m["scope_removed_items"]) <= 0,
        "closure_pct_sp": (m["committed_sp"] + m["scope_added_sp"] - m["scope_removed_sp"]) <= 0,
    }


def _guess_project_key(payload: dict) -> str:
    for issue in payload.get("issues") or []:
        key = issue.get("key") or ""
        if "-" in key:
            return key.split("-", 1)[0]
    return payload.get("sprint", {}).get("board_name", "") or EM_DASH


# ==========================================================================
# Header
# ==========================================================================


def build_header_ctx(report: dict, ctx: dict) -> None:
    idx, primary = primary_target(report)
    payload = primary["payload"]
    sprint = payload["sprint"]
    sprints = report["sprints"]
    params = report.get("params") or {}
    n_base = len(base_entries(report))

    target_names = [s["payload"]["sprint"]["name"] for s in sprints if s["target"]]
    sprint_name_joined = ", ".join(target_names)

    ctx["REPORT_TITLE"] = esc(f"Отчёт по спринту — {sprint_name_joined}")
    ctx["HEADER_TITLE"] = "Отчёт по спринту"
    ctx["HEADER_BOARD_NAME"] = esc(report.get("board", {}).get("name", EM_DASH))
    ctx["HEADER_PROJECT_KEY"] = esc(_guess_project_key(payload))
    ctx["HEADER_SPRINT_NAME"] = esc(sprint_name_joined)
    ctx["HEADER_DATE_RANGE"] = esc(f"{fmt_date(sprint['start_at'])} – {fmt_date(sprint['end_at'])}")

    burndowns = report.get("burndowns") or []
    calendar_days = len(burndowns[-1]["points"]) if burndowns else 0
    working_days = len(sprint.get("working_days") or [])
    ctx["HEADER_WORKING_DAYS"] = esc(f"{working_days} из {calendar_days} календарных" if calendar_days else f"{working_days} рабочих")

    ctx["HEADER_SPRINT_STATE"] = esc(sprint.get("state", EM_DASH))
    requested_history = params.get("history_sprint_count")
    ctx["HEADER_BASE_SPRINTS"] = esc(
        f"{n_base} (запрошено {requested_history})" if requested_history is not None else str(n_base)
    )
    ctx["HEADER_GENERATED_AT"] = esc(fmt_datetime(params.get("generated_at")))
    ctx["HEADER_JIRA_HOST"] = "скрыт (переменная окружения JIRA_BASE_URL)"

    ctx["TAB_TEAM_LABEL"] = "Команда"
    ctx["TAB_PERSON_LABEL"] = "Персональные"
    ctx["TAB_ENG_LABEL"] = "Инженерия"


# ==========================================================================
# Board warnings strip
# ==========================================================================

_WARNING_TEXT = {
    "WARN_DIVISION_BY_ZERO": "Знаменатель метрики был равен нулю — показанное значение 0 не отражает реальную величину.",
    "WARN_SPRINT_ACTIVE_PARTIAL": "Один из показанных спринтов ещё не закрыт — его метрики посчитаны на текущий момент и могут измениться.",
    "WARN_STATUS_UNMAPPED": "Один или несколько статусов не отнесены ни к одной категории (нет ни в cancelled_statuses, ни в status_map, ни в каталоге Jira). В хитмапе такие ячейки помечены отдельным цветом.",
    "WARN_BASELINE_SHORT": "SMA5 усреднена по числу базовых спринтов меньше пяти — velocity_sma5_sp и производный от неё load_pct опираются на укороченную базу.",
    "WARN_THROUGHPUT_UNSTABLE": "Недельный коэффициент вариации пропускной способности превышает порог 50% — прогноз построен, но разброс отражает реальную нестабильность выборки.",
}


def build_warnings_block(report: dict) -> str:
    warnings = report.get("warnings") or []
    if not warnings:
        return '<p class="section-desc" style="margin:14px 16px">Предупреждений нет.</p>'
    items = []
    for code in warnings:
        text = _WARNING_TEXT.get(code, "Код предупреждения не описан этим генератором.")
        items.append(
            "<li>"
            f'<span class="warn-badge">{esc(code)}</span> '
            f"<span>{esc(text)}</span>"
            "</li>"
        )
    return '<ul class="warn-list">' + "".join(items) + "</ul>"


# ==========================================================================
# KPI tiles (tab 1)
# ==========================================================================


def _status_ge_baseline(value: float, baseline: Optional[float]) -> str:
    if baseline is None or baseline <= 0:
        return "none"
    if value >= baseline:
        return "good"
    if value >= baseline * 0.9:
        return "warn"
    return "bad"


def _status_band(value: float, good_min: float, warn_min: float, higher_is_better: bool = True) -> str:
    if higher_is_better:
        if value >= good_min:
            return "good"
        if value >= warn_min:
            return "warn"
        return "bad"
    if value <= good_min:
        return "good"
    if value <= warn_min:
        return "warn"
    return "bad"


def _load_status(value: float) -> str:
    if 85 <= value <= 110:
        return "good"
    if 70 <= value < 85 or 110 < value <= 125:
        return "warn"
    return "bad"


STATUS_LABELS = {
    "good": "норма",
    "warn": "под наблюдением",
    "bad": "критично",
    "none": "нет базы",
}


def _dir_and_arrow(delta: Optional[float], higher_is_good: bool) -> tuple:
    if delta is None:
        return "none", ARROW_FLAT
    if abs(delta) < 1e-9:
        return "flat", ARROW_FLAT
    if delta > 0:
        return ("up-good" if higher_is_good else "up-bad"), ARROW_UP
    return ("down-bad" if higher_is_good else "down-good"), ARROW_DOWN


def _kpi_series(sprints: list, key: str) -> list:
    return [s["payload"]["metrics"][key] for s in sprints]


def _kpi_tile(
    ctx: dict,
    tid: str,
    *,
    value: float,
    value_available: bool,
    unit: str,
    status: str,
    delta_dir: str,
    delta_arrow: str,
    delta_text: str,
    delta_note: str,
    decimals: int,
    series: list,
) -> None:
    """`status == "none"` only means "no baseline to band or compare
    against" — several metrics (velocity_sp, throughput_items,
    closure_pct_*) are perfectly real without one. `value_available` is the
    real div0 signal that decides whether the big number itself is an
    em dash; it must never be derived from `status`."""
    ctx[f"KPI_{tid}_STATUS"] = status
    ctx[f"KPI_{tid}_STATUS_LABEL"] = esc(STATUS_LABELS[status])
    ctx[f"KPI_{tid}_VALUE"] = fmt_num(value, decimals) if value_available else EM_DASH
    ctx[f"KPI_{tid}_UNIT"] = esc(unit)
    ctx[f"KPI_{tid}_DELTA_DIR"] = delta_dir
    ctx[f"KPI_{tid}_DELTA_ARROW"] = delta_arrow
    ctx[f"KPI_{tid}_DELTA"] = esc(delta_text)
    ctx[f"KPI_{tid}_DELTA_NOTE"] = esc(delta_note)
    ctx[f"KPI_{tid}_SPARK_POINTS"] = build_spark_svg(series, spark_status_class(status))


def build_kpi_tiles(report: dict, ctx: dict) -> None:
    sprints = report["sprints"]
    idx, primary = primary_target(report)
    m = primary["payload"]["metrics"]
    kpi = report.get("kpi") or {}
    flags = div0_flags(sprints, idx)

    # --- VELOCITY -----------------------------------------------------
    # velocity_sp is delivered_sp — always a real number. Only the SMA5
    # baseline comparison (band + delta) needs history to exist.
    own_sma5 = m["velocity_sma5_sp"]
    status = _status_ge_baseline(m["velocity_sp"], own_sma5)
    delta = None if status == "none" else m["velocity_sp"] - own_sma5
    ddir, darrow = _dir_and_arrow(delta, higher_is_good=True)
    _kpi_tile(
        ctx, "VELOCITY",
        value=m["velocity_sp"], value_available=True, unit="SP · velocity_sp = delivered_sp",
        status=status, delta_dir=ddir, delta_arrow=darrow,
        delta_text=(fmt_signed(delta, 1, " SP") if delta is not None else "без сравнения"),
        delta_note=(f"к SMA5 {fmt_num(own_sma5, 1)} SP" if status != "none" else "нет базовых спринтов"),
        decimals=1, series=_kpi_series(sprints, "velocity_sp"),
    )

    # --- SMA5 -----------------------------------------------------------
    hist_len = history_len_at(sprints, idx)
    if flags["velocity_sma5_sp"]:
        sma_status = "none"
    elif hist_len < 5:
        sma_status = "warn"
    else:
        sma_status = "good"
    base_velocities = [sprints[j]["payload"]["metrics"]["velocity_sp"] for j in range(idx) if not sprints[j]["target"]]
    base_velocities = base_velocities[-5:]
    sample_note = "выборка " + " / ".join(fmt_num(v, 1) for v in base_velocities) if base_velocities else "нет истории"
    ctx["KPI_SMA5_STATUS"] = sma_status
    ctx["KPI_SMA5_STATUS_LABEL"] = esc(
        "короткая база" if sma_status == "warn" else ("нет базы" if sma_status == "none" else "полная база")
    )
    # SMA5's own value genuinely is undefined with no history — the one
    # tile where "no baseline" and "no value" are the same real div0.
    ctx["KPI_SMA5_VALUE"] = EM_DASH if sma_status == "none" else fmt_num(own_sma5, 1)
    ctx["KPI_SMA5_UNIT"] = "SP · среднее по базовым спринтам"
    ctx["KPI_SMA5_DELTA_DIR"] = "flat"
    ctx["KPI_SMA5_DELTA_ARROW"] = ARROW_FLAT
    ctx["KPI_SMA5_DELTA"] = esc(f"{hist_len} из 5 спринтов")
    ctx["KPI_SMA5_DELTA_NOTE"] = esc(sample_note)
    ctx["KPI_SMA5_SPARK_POINTS"] = build_spark_svg(_kpi_series(sprints, "velocity_sma5_sp"), spark_status_class(sma_status))
    ctx["KPI_SMA5_WARN"] = (
        '<div class="kpi-warn"><span class="warn-badge">WARN_BASELINE_SHORT</span></div>' if sma_status == "warn" else ""
    )

    # --- PERFORMANCE ------------------------------------------------------
    # performance_pct's own denominator is this sprint's committed_sp, not
    # a baseline — the band uses fixed thresholds, so it needs no history
    # either. Only the delta (vs the board average) needs base sprints.
    baseline = kpi.get("avg_performance_pct")
    status = "none" if flags["performance_pct"] else _status_band(m["performance_pct"], 95, 80, higher_is_better=True)
    delta = None if status == "none" or baseline is None else m["performance_pct"] - baseline
    ddir, darrow = _dir_and_arrow(delta, higher_is_good=True)
    _kpi_tile(
        ctx, "PERFORMANCE",
        value=m["performance_pct"], value_available=not flags["performance_pct"], unit="% · delivered_sp / committed_sp",
        status=status, delta_dir=ddir, delta_arrow=darrow,
        delta_text=(fmt_signed(delta, 2, " п.п.") if delta is not None else "без сравнения"),
        delta_note=(f"к среднему по базе {fmt_num(baseline, 2)}%" if baseline is not None and status != "none" else "нет базовых спринтов"),
        decimals=2, series=_kpi_series(sprints, "performance_pct"),
    )

    # --- LOAD ---------------------------------------------------------
    # load_pct's own denominator IS the SMA5 baseline, so unlike the other
    # ratio tiles a missing baseline genuinely leaves the value undefined.
    status = "none" if flags["load_pct"] else _load_status(m["load_pct"])
    if status == "none":
        ddir, darrow, delta_text = "none", ARROW_FLAT, "без сравнения"
    elif abs(m["load_pct"] - 100.0) < 1e-9:
        ddir, darrow, delta_text = "flat", ARROW_FLAT, "ровно 100%"
    elif m["load_pct"] > 100.0:
        ddir, darrow, delta_text = "up-bad", ARROW_UP, fmt_signed(m["load_pct"] - 100.0, 2, " п.п.")
    else:
        ddir, darrow, delta_text = "down-bad", ARROW_DOWN, fmt_signed(m["load_pct"] - 100.0, 2, " п.п.")
    _kpi_tile(
        ctx, "LOAD",
        value=m["load_pct"], value_available=not flags["load_pct"], unit="% · committed_sp / velocity_sma5_sp",
        status=status, delta_dir=ddir, delta_arrow=darrow, delta_text=delta_text,
        delta_note=(f"взяли {fmt_num(m['committed_sp'], 1)} SP при базе {fmt_num(own_sma5, 1)}" if status != "none" else "нет базы SMA5"),
        decimals=2, series=_kpi_series(sprints, "load_pct"),
    )

    # --- SCOPE_CHANGE -------------------------------------------------
    baseline = kpi.get("avg_scope_change_pct")
    status = "none" if flags["scope_change_pct"] else _status_band(m["scope_change_pct"], 10, 25, higher_is_better=False)
    delta = None if status == "none" or baseline is None else m["scope_change_pct"] - baseline
    ddir, darrow = _dir_and_arrow(delta, higher_is_good=False)
    _kpi_tile(
        ctx, "SCOPE_CHANGE",
        value=m["scope_change_pct"], value_available=not flags["scope_change_pct"], unit="% · (added + est.change + removed) / committed",
        status=status, delta_dir=ddir, delta_arrow=darrow,
        delta_text=(fmt_signed(delta, 2, " п.п.") if delta is not None else "без сравнения"),
        delta_note=(f"к среднему по базе {fmt_num(baseline, 2)}%" if baseline is not None and status != "none" else "нет базовых спринтов"),
        decimals=2, series=_kpi_series(sprints, "scope_change_pct"),
    )

    # --- THROUGHPUT -----------------------------------------------------
    # throughput_items is a plain count — never div0'd. Only the band
    # (relative to the board average) needs a baseline.
    baseline = kpi.get("throughput_avg_items")
    status = _status_ge_baseline(m["throughput_items"], baseline)
    delta = None if status == "none" else m["throughput_items"] - baseline
    ddir, darrow = _dir_and_arrow(delta, higher_is_good=True)
    _kpi_tile(
        ctx, "THROUGHPUT",
        value=m["throughput_items"], value_available=True, unit="задач · сумма throughput_daily",
        status=status, delta_dir=ddir, delta_arrow=darrow,
        delta_text=(fmt_signed(delta, 2) if delta is not None else "без сравнения"),
        delta_note=(f"к среднему по базе {fmt_num(baseline, 2)}" if baseline else "нет базовых спринтов"),
        decimals=0, series=_kpi_series(sprints, "throughput_items"),
    )

    # --- CLOSURE_ITEMS ----------------------------------------------
    baseline = kpi.get("avg_closure_pct_items")
    status = "none" if flags["closure_pct_items"] else _status_band(m["closure_pct_items"], 85, 70, higher_is_better=True)
    delta = None if status == "none" or baseline is None else m["closure_pct_items"] - baseline
    ddir, darrow = _dir_and_arrow(delta, higher_is_good=True)
    _kpi_tile(
        ctx, "CLOSURE_ITEMS",
        value=m["closure_pct_items"], value_available=not flags["closure_pct_items"], unit="% · delivered / (committed + added − removed)",
        status=status, delta_dir=ddir, delta_arrow=darrow,
        delta_text=(fmt_signed(delta, 2, " п.п.") if delta is not None else "без сравнения"),
        delta_note=(f"{m['delivered_items']} из {m['committed_items'] + m['scope_added_items'] - m['scope_removed_items']} задач" if status != "none" else "нет базовых спринтов"),
        decimals=2, series=_kpi_series(sprints, "closure_pct_items"),
    )

    # --- CLOSURE_SP ---------------------------------------------------
    baseline = kpi.get("avg_closure_pct_sp")
    status = "none" if flags["closure_pct_sp"] else _status_band(m["closure_pct_sp"], 85, 70, higher_is_better=True)
    delta = None if status == "none" or baseline is None else m["closure_pct_sp"] - baseline
    ddir, darrow = _dir_and_arrow(delta, higher_is_good=True)
    total_sp = m["committed_sp"] + m["scope_added_sp"] - m["scope_removed_sp"]
    _kpi_tile(
        ctx, "CLOSURE_SP",
        value=m["closure_pct_sp"], value_available=not flags["closure_pct_sp"], unit="% · delivered_sp / итоговый объём",
        status=status, delta_dir=ddir, delta_arrow=darrow,
        delta_text=(fmt_signed(delta, 2, " п.п.") if delta is not None else "без сравнения"),
        delta_note=(f"{fmt_num(m['delivered_sp'], 1)} из {fmt_num(total_sp, 1)} SP" if status != "none" else "нет базовых спринтов"),
        decimals=2, series=_kpi_series(sprints, "closure_pct_sp"),
    )


# ==========================================================================
# Sprint scalar metric groups (tab 1)
# ==========================================================================


def _value_class(status: str) -> str:
    return {"good": "is-good", "warn": "is-warn", "bad": "is-bad", "none": "is-none"}.get(status, "")


def build_metric_groups(report: dict, ctx: dict) -> None:
    sprints = report["sprints"]
    idx, primary = primary_target(report)
    m = primary["payload"]["metrics"]
    n_base = len(base_entries(report))
    flags = div0_flags(sprints, idx)

    ctx["METRIC_COMMITTED_SP"] = fmt_num(m["committed_sp"], 1)
    ctx["METRIC_COMMITTED_ITEMS"] = fmt_int(m["committed_items"])
    ctx["METRIC_DELIVERED_SP"] = fmt_num(m["delivered_sp"], 1)
    ctx["METRIC_DELIVERED_ITEMS"] = fmt_int(m["delivered_items"])
    ctx["METRIC_SCOPE_ADDED_SP"] = fmt_num(m["scope_added_sp"], 1)
    ctx["METRIC_SCOPE_ADDED_ITEMS"] = fmt_int(m["scope_added_items"])
    ctx["METRIC_SCOPE_REMOVED_SP"] = fmt_num(m["scope_removed_sp"], 1)
    ctx["METRIC_SCOPE_REMOVED_ITEMS"] = fmt_int(m["scope_removed_items"])
    ctx["METRIC_SCOPE_ESTIMATION_CHANGE_SP"] = fmt_num(m["scope_estimation_change_sp"], 1)
    ctx["METRIC_SCOPE_CHANGE_PCT"] = EM_DASH if (flags["scope_change_pct"]) else fmt_num(m["scope_change_pct"], 2)
    ctx["METRIC_SCOPE_CHANGE_PCT_CLASS"] = (
        "is-none" if flags["scope_change_pct"] else _value_class(_status_band(m["scope_change_pct"], 10, 25, higher_is_better=False))
    )
    ctx["METRIC_PERFORMANCE_PCT"] = EM_DASH if flags["performance_pct"] else fmt_num(m["performance_pct"], 2)
    ctx["METRIC_PERFORMANCE_PCT_CLASS"] = (
        "is-none" if flags["performance_pct"] else _value_class(_status_band(m["performance_pct"], 95, 80, higher_is_better=True))
    )
    ctx["METRIC_VELOCITY_SP"] = fmt_num(m["velocity_sp"], 1)
    ctx["METRIC_VELOCITY_SMA5_SP"] = EM_DASH if flags["velocity_sma5_sp"] else fmt_num(m["velocity_sma5_sp"], 1)
    ctx["METRIC_VELOCITY_SMA5_SP_CLASS"] = "is-none" if flags["velocity_sma5_sp"] else ("is-warn" if history_len_at(sprints, idx) < 5 else "")
    ctx["METRIC_LOAD_PCT"] = EM_DASH if flags["load_pct"] else fmt_num(m["load_pct"], 2)
    ctx["METRIC_LOAD_PCT_CLASS"] = _value_class(_load_status(m["load_pct"])) if not flags["load_pct"] else "is-none"
    ctx["METRIC_THROUGHPUT_ITEMS"] = fmt_int(m["throughput_items"])
    ctx["METRIC_CLOSURE_PCT_ITEMS"] = EM_DASH if flags["closure_pct_items"] else fmt_num(m["closure_pct_items"], 2)
    ctx["METRIC_CLOSURE_PCT_ITEMS_CLASS"] = (
        "is-none" if flags["closure_pct_items"] else _value_class(_status_band(m["closure_pct_items"], 85, 70, higher_is_better=True))
    )
    ctx["METRIC_CLOSURE_PCT_SP"] = EM_DASH if flags["closure_pct_sp"] else fmt_num(m["closure_pct_sp"], 2)
    ctx["METRIC_CLOSURE_PCT_SP_CLASS"] = (
        "is-none" if flags["closure_pct_sp"] else _value_class(_status_band(m["closure_pct_sp"], 85, 70, higher_is_better=True))
    )

    total_sp = m["committed_sp"] + m["scope_added_sp"] - m["scope_removed_sp"]
    ctx["METRIC_SCOPEBAR_TOTAL"] = fmt_num(total_sp, 1)
    ctx["METRIC_SCOPEBAR_CAPTION"] = esc(f"· {fmt_num(m['delivered_sp'], 1)} из {fmt_num(total_sp, 1)} SP")
    fill_pct = 0.0 if total_sp <= 0 else max(0.0, min(100.0, m["delivered_sp"] / total_sp * 100.0))
    mark_pct = 0.0 if total_sp <= 0 else max(0.0, min(100.0, m["committed_sp"] / total_sp * 100.0))
    ctx["METRIC_SCOPEBAR_GEOMETRY"] = (
        '<div class="scopebar" role="img" '
        f'aria-label="Поставлено {fmt_num(m["delivered_sp"], 1)} SP из итогового объёма {fmt_num(total_sp, 1)} SP; '
        f'отметка обязательства — {fmt_num(m["committed_sp"], 1)} SP">'
        f'<span class="fill" style="width:{fill_pct:.2f}%"></span>'
        f'<span class="mark" style="left:{mark_pct:.2f}%"></span>'
        "</div>"
    )


# ==========================================================================
# Burndown chart (tab 1)
# ==========================================================================

_WEEKDAY_RU_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _parse_date(date_str: str) -> tuple:
    y, mo, d = (int(x) for x in date_str.split("-"))
    return y, mo, d


def _weekday(date_str: str) -> int:
    import datetime as _dt

    y, mo, d = _parse_date(date_str)
    return _dt.date(y, mo, d).weekday()


def build_burndown_svg(points: list) -> tuple:
    """Returns (aria_summary, full <svg> markup, has_weekend) — the third
    value drives whether the caller keeps or drops the "выходные" legend
    entry, which must not claim a weekend band that isn't in this chart."""
    x0, x1 = 56.0, 730.0
    y_top, y_base = 24.0, 288.0
    n = len(points)

    if n == 0:
        aria = "Burndown недоступен: у целевого спринта нет календарных дней."
        svg = (
            '<svg class="chart-svg" viewBox="0 0 760 344" role="img" aria-label="'
            + esc(aria)
            + '"><text class="c-axis-title" x="380" y="172" text-anchor="middle">нет данных</text></svg>'
        )
        return aria, svg, False

    remaining = [p["remaining_sp"] for p in points]
    ideal = [p["ideal_sp"] for p in points]
    n_ticks = 6
    step_val = nice_step(max(max(remaining), max(ideal), 1.0), n_ticks)
    top = step_val * n_ticks

    def y_of(v: float) -> float:
        return y_base - (v / top) * (y_base - y_top)

    xs = linspace(x0, x1, n)
    # Same spacing the tick/point x-positions use (linspace over n-1
    # intervals) — a single-day chart has no interval to derive a width
    # from, so fall back to a tenth of the plot width instead of shading
    # the whole thing.
    day_width = (x1 - x0) / (n - 1) if n > 1 else (x1 - x0) / 10.0

    grid = "".join(f'<line x1="{x0}" y1="{y_of(step_val*i):.1f}" x2="{x1}" y2="{y_of(step_val*i):.1f}"/>' for i in range(1, n_ticks + 1))
    y_labels = "".join(
        f'<text x="{x0-10}" y="{y_of(step_val*i)+4:.1f}">{fmt_num(step_val*i, 0)}</text>' for i in range(0, n_ticks + 1)
    )

    weekend_rects = []
    for x, p in zip(xs, points):
        if _weekday(p["date"]) >= 5:
            left = max(x - day_width / 2, x0)
            right = min(x + day_width / 2, x1)
            weekend_rects.append(f'<rect class="c-weekend" x="{left:.1f}" y="{y_top}" width="{right-left:.1f}" height="{y_base-y_top:.1f}"/>')
    weekend_html = "".join(weekend_rects)

    x_ticks = "".join(f'<line x1="{x:.2f}" y1="{y_base}" x2="{x:.2f}" y2="{y_base+5}"/>' for x in xs)
    x_labels = "".join(
        f'<text x="{x:.2f}" y="{y_base+18}">{_parse_date(p["date"])[2]:02d}</text>' for x, p in zip(xs, points)
    )
    x_sub = "".join(
        f'<text x="{x:.2f}" y="{y_base+29}">{_WEEKDAY_RU_SHORT[_weekday(p["date"])]}</text>' for x, p in zip(xs, points)
    )

    ideal_path = "M " + " L ".join(f"{x:.2f} {y_of(v):.2f}" for x, v in zip(xs, ideal))

    actual_path_pts = " ".join(f"{x:.2f},{y_of(v):.2f}" for x, v in zip(xs, remaining))
    area_path = (
        "M " + " L ".join(f"{x:.2f} {y_of(v):.2f}" for x, v in zip(xs, remaining))
        + f" L {xs[-1]:.2f} {y_base} L {xs[0]:.2f} {y_base} Z"
    )

    dots = []
    for x, p in zip(xs, points):
        title = f"{p['date']} — остаток {fmt_num(p['remaining_sp'], 1)} SP"
        dots.append(f'<circle cx="{x:.2f}" cy="{y_of(p["remaining_sp"]):.2f}" r="3.2"><title>{esc(title)}</title></circle>')
    dots_html = "".join(dots)

    last_x, last_y = xs[-1], y_of(remaining[-1])
    callout = (
        f'<g><line class="c-callout" x1="{last_x:.2f}" y1="{last_y:.2f}" x2="{last_x:.2f}" y2="{y_base}"/>'
        f'<text class="c-callout-text" x="{last_x-8:.2f}" y="{(last_y+y_base)/2:.2f}" text-anchor="end">'
        f"осталось {fmt_num(remaining[-1], 1)} SP</text></g>"
    )

    day_word = ru_plural(n, "календарный день", "календарных дня", "календарных дней")
    aria = (
        f"Burndown спринта. Идеальная линия падает с {fmt_num(ideal[0], 1)} до {fmt_num(ideal[-1], 1)} SP "
        f"за {n} {day_word}. Фактический остаток: "
        + ", ".join(fmt_num(v, 1) for v in remaining)
        + f" SP. На конец спринта осталось {fmt_num(remaining[-1], 1)} SP."
    )

    svg = (
        '<svg class="chart-svg" viewBox="0 0 760 344" preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{esc(aria)}">'
        '<defs><linearGradient id="burndownFill" x1="0" y1="0" x2="0" y2="1">'
        '<stop class="c-area-top" offset="0%"/><stop class="c-area-bottom" offset="100%"/></linearGradient></defs>'
        f"{weekend_html}"
        f'<g class="c-grid">{grid}</g>'
        f'<g class="c-unit-label" text-anchor="end">{y_labels}</g>'
        f'<text class="c-axis-title" x="16" y="{(y_top+y_base)/2:.0f}" text-anchor="middle" '
        f'transform="rotate(-90 16 {(y_top+y_base)/2:.0f})">Остаток, story points</text>'
        f'<line class="c-axis" x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}"/>'
        f'<line class="c-axis" x1="{x0}" y1="{y_top}" x2="{x0}" y2="{y_base}"/>'
        f'<g class="c-tick">{x_ticks}</g>'
        f'<g class="c-tick-label" text-anchor="middle">{x_labels}</g>'
        f'<g class="c-tick-sub" text-anchor="middle">{x_sub}</g>'
        f'<text class="c-axis-title" x="{(x0+x1)/2:.0f}" y="{y_base+48:.0f}" text-anchor="middle">Календарный день</text>'
        f'<path class="c-ideal" d="{ideal_path}"/>'
        f'<path class="c-area" d="{area_path}"/>'
        f'<polyline class="c-actual" points="{actual_path_pts}"/>'
        f'<g class="c-actual-pt">{dots_html}</g>'
        f"{callout}"
        "</svg>"
    )
    return aria, svg, bool(weekend_rects)


# ==========================================================================
# Heatmap table (tab 1) + export Table A
# ==========================================================================

_CAT_LABEL = {
    "new": "не начата",
    "indeterminate": "в работе",
    "done": "завершена",
    "cancelled": "отменена",
}


def build_heatmap_cols(days: list) -> str:
    cells = []
    for d in days:
        y, mo, day = _parse_date(d)
        dow = _WEEKDAY_RU_SHORT[_weekday(d)]
        cells.append(f'<th scope="col">{day:02d}.{mo:02d}<span class="dow">{dow}</span></th>')
    return "".join(cells)


def build_heatmap_row_cells(days: list, cells_by_date: dict) -> str:
    out = []
    for d in days:
        cell = cells_by_date.get(d)
        if cell is None:
            out.append('<td class="hm-cell" data-cat="absent" title="не в спринте в этот день">&mdash;</td>')
            continue
        cat = cell["status_category"] or "unmapped"
        status = cell["status"]
        title = ""
        if cat == "unmapped":
            title = ' title="статус не отнесён к категории — WARN_STATUS_UNMAPPED"'
        out.append(f'<td class="hm-cell" data-cat="{esc(cat)}"{title}>{esc_or_dash(status)}</td>')
    return "".join(out)


def build_heatmap_row_html(inner: str, row: dict, days: list) -> str:
    cells_by_date = {c["date"]: c for c in row["cells"]}
    mapping = {
        "HEATMAP_ROW_KEY": esc(row["issue_key"]),
        "HEATMAP_ROW_CELLS": build_heatmap_row_cells(days, cells_by_date),
    }
    return sub_tokens(inner, mapping)


def build_heatmap_section(report: dict, ctx: dict) -> list:
    """Sets the fixed heatmap tokens on `ctx`; returns the rows for the
    caller to render through the template's own row markup."""
    heatmaps = report.get("heatmaps") or []
    hm = heatmaps[-1] if heatmaps else {"days": [], "rows": []}
    ctx["HEATMAP_COLS"] = build_heatmap_cols(hm["days"])

    ctx["HEATMAP_CATEGORY_LEGEND"] = (
        '<div class="cat-scale"><span class="scale-title">Категория статуса</span>'
        + "".join(
            f'<span class="scale-step"><i class="scale-chip" style="background:var(--cat-{cat})"></i>'
            f"<code>{cat or chr(34)+chr(34)}</code> — {label}</span>"
            for cat, label in _CAT_LABEL.items()
        )
        + '<span class="scale-step"><i class="scale-chip absent"></i>нет в спринте в этот день</span></div>'
    )
    return hm["rows"]


def build_table_a(report: dict, ctx: dict) -> list:
    export = report.get("export") or {}
    heatmap_table = export.get("heatmap_table") or {"header": [], "rows": []}
    header = heatmap_table["header"]
    day_headers = header[7:-1] if len(header) >= 8 else []

    cols_html = []
    for h in day_headers:
        parts = h.split(" / ")
        if len(parts) == 3:
            sid, date, dow = parts
            cols_html.append(f'<th scope="col">{esc(sid)} / {esc(date)}<span class="dow">{esc(dow)}</span></th>')
        else:
            cols_html.append(f"<th scope=\"col\">{esc(h)}</th>")
    ctx["TABLE_HEATMAP_COLS"] = "".join(cols_html)
    # Header is 7 fixed + N day columns + End = N+8. The footer's first two
    # cells consume colspan=2 (epic+issue) + 1 td (SP) + 1 td (QA), so the
    # note cell's colspan must be N+4 to reach the same column count.
    ctx["TABLE_HEATMAP_FOOTER_COLSPAN"] = str(len(day_headers) + 4)

    total_sp = 0.0
    total_qa = 0.0
    for row in heatmap_table["rows"]:
        padded = list(row) + [""] * max(0, 8 - len(row))
        try:
            total_sp += float(padded[2])
            total_qa += float(padded[3])
        except (TypeError, ValueError):
            pass
    total_rows = len(heatmap_table["rows"])
    row_word = ru_plural(total_rows, "строка", "строки", "строк")
    ctx["TABLE_HEATMAP_FOOTER_LABEL"] = esc(f"{total_rows} {row_word}")
    ctx["TABLE_HEATMAP_TOTAL_SP"] = fmt_num(total_sp, 1)
    ctx["TABLE_HEATMAP_TOTAL_QA"] = fmt_num(total_qa, 1)
    ctx["TABLE_HEATMAP_FOOTER_NOTE"] = "Пустая ячейка = задачи не было в спринте в этот день"
    idx, primary = primary_target(report)
    ctx["TABLE_HEATMAP_CAPTION"] = esc(
        f"Таблица A — Heatmap · одна строка на задачу, дневные колонки "
        f"«<sprintID> / YYYY-MM-DD / <день недели>» — спринт {primary['payload']['sprint']['name']}"
    )
    return heatmap_table["rows"]


def build_table_a_row_html(inner: str, row: list) -> str:
    # header is guarded against a short row (len < 8); the data rows need
    # the same guard so a short export row indexes safely (nit).
    padded = list(row) + [""] * max(0, 8 - len(row))
    epic, issue, sp, qa, role, labels, before = padded[0], padded[1], padded[2], padded[3], padded[4], padded[5], padded[6]
    days = padded[7:-1]
    end = padded[-1]
    days_html = "".join(f"<td>{esc_or_dash(d)}</td>" for d in days)
    mapping = {
        "TABLE_HEATMAP_ROW_EPIC_CLASS": "cell-key" if epic else "cell-muted",
        "TABLE_HEATMAP_ROW_EPIC": esc(epic) if epic else EM_DASH,
        "TABLE_HEATMAP_ROW_ISSUE": esc(issue),
        "TABLE_HEATMAP_ROW_SP": esc(sp),
        "TABLE_HEATMAP_ROW_QA": esc(qa),
        "TABLE_HEATMAP_ROW_ROLE": esc(role),
        "TABLE_HEATMAP_ROW_LABELS": esc(labels),
        "TABLE_HEATMAP_ROW_BEFORE": esc(before),
        "TABLE_HEATMAP_ROW_DAYS": days_html,
        "TABLE_HEATMAP_ROW_END": esc(end),
    }
    return sub_tokens(inner, mapping)


# ==========================================================================
# Export Table B (board_table) + board KPI averages
# ==========================================================================


def _avg_over_base(sprints: list, key: str) -> Optional[float]:
    vals = [s["payload"]["metrics"][key] for s in sprints if not s["target"]]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _div0_cell(flag: bool, value: float, decimals: int) -> tuple:
    """Returns (cell_html, css_class_suffix) for a board_table ratio column —
    every DIV0'd ratio gets the same badge AND the same `cell-nodata` class
    (m1), not just load_pct/velocity_sma5_sp."""
    if flag:
        return '0 <span class="warn-badge">DIV0</span>', " cell-nodata"
    return fmt_num(value, decimals), ""


def build_board_row_html(inner: str, entry: tuple, sprints: list) -> str:
    i, s = entry
    p = s["payload"]
    sp = p["sprint"]
    m = p["metrics"]
    flags = div0_flags(sprints, i)
    state_pill = (
        f'<span class="pill closed">{esc(sp["state"])}</span>'
        if sp["state"] == "closed"
        else f'<span class="pill active">{esc(sp["state"])}</span>'
    )
    name_html = esc(sp["name"]) + (' <span class="pill target">целевой</span>' if s["target"] else "")

    perf_cell, perf_class = _div0_cell(flags["performance_pct"], m["performance_pct"], 2)
    load_cell, load_class = _div0_cell(flags["load_pct"], m["load_pct"], 2)
    scope_cell, scope_class = _div0_cell(flags["scope_change_pct"], m["scope_change_pct"], 2)
    sma5_cell, sma5_class = _div0_cell(flags["velocity_sma5_sp"], m["velocity_sma5_sp"], 1)
    clos_items_cell, clos_items_class = _div0_cell(flags["closure_pct_items"], m["closure_pct_items"], 2)
    clos_sp_cell, clos_sp_class = _div0_cell(flags["closure_pct_sp"], m["closure_pct_sp"], 2)

    mapping = {
        "BOARD_ROW_SPRINT": name_html,
        "BOARD_ROW_STATE": state_pill,
        "BOARD_ROW_START": fmt_date(sp["start_at"]),
        "BOARD_ROW_END": fmt_date(sp["end_at"]),
        "BOARD_ROW_COMMITTED_SP": fmt_num(m["committed_sp"], 1),
        "BOARD_ROW_DELIVERED_SP": fmt_num(m["delivered_sp"], 1),
        "BOARD_ROW_ADDED_SP": fmt_num(m["scope_added_sp"], 1),
        "BOARD_ROW_REMOVED_SP": fmt_num(m["scope_removed_sp"], 1),
        "BOARD_ROW_ESTIMATION_CHANGE_SP": fmt_num(m["scope_estimation_change_sp"], 1),
        "BOARD_ROW_PERFORMANCE_PCT": perf_cell,
        "BOARD_ROW_PERFORMANCE_PCT_NODATA_CLASS": perf_class,
        "BOARD_ROW_LOAD_PCT": load_cell,
        "BOARD_ROW_LOAD_PCT_NODATA_CLASS": load_class,
        "BOARD_ROW_SCOPE_CHANGE_PCT": scope_cell,
        "BOARD_ROW_SCOPE_CHANGE_PCT_NODATA_CLASS": scope_class,
        "BOARD_ROW_VELOCITY_SP": fmt_num(m["velocity_sp"], 1),
        "BOARD_ROW_SMA5_SP": sma5_cell,
        "BOARD_ROW_SMA5_SP_NODATA_CLASS": sma5_class,
        "BOARD_ROW_THROUGHPUT_COUNT": fmt_int(m["throughput_items"]),
        "BOARD_ROW_CLOSURE_COUNT_PCT": clos_items_cell,
        "BOARD_ROW_CLOSURE_COUNT_PCT_NODATA_CLASS": clos_items_class,
        "BOARD_ROW_CLOSURE_SP_PCT": clos_sp_cell,
        "BOARD_ROW_CLOSURE_SP_PCT_NODATA_CLASS": clos_sp_class,
    }
    return sub_tokens(inner, mapping)


def build_table_b(report: dict, ctx: dict) -> list:
    sprints = report["sprints"]
    kpi = report.get("kpi") or {}
    avg_committed = _avg_over_base(sprints, "committed_sp")
    avg_delivered = _avg_over_base(sprints, "delivered_sp")
    avg_added = _avg_over_base(sprints, "scope_added_sp")
    avg_removed = _avg_over_base(sprints, "scope_removed_sp")
    avg_estchg = _avg_over_base(sprints, "scope_estimation_change_sp")

    idx, primary = primary_target(report)
    n_base = len(base_entries(report))
    base_range = ""
    base_names = [s["payload"]["sprint"]["name"] for s in sprints if not s["target"]]
    if base_names:
        base_range = f" ({base_names[0]}…{base_names[-1]})" if len(base_names) > 1 else f" ({base_names[0]})"
    ctx["TABLE_BOARD_FOOTER_LABEL"] = esc(f"Среднее по базовым спринтам{base_range}" if n_base else "Нет базовых спринтов")
    ctx["KPI_AVG_COMMITTED_SP"] = fmt_num(avg_committed, 1) if avg_committed is not None else EM_DASH
    ctx["KPI_AVG_DELIVERED_SP"] = fmt_num(avg_delivered, 1) if avg_delivered is not None else EM_DASH
    ctx["BOARD_AVG_ADDED_SP"] = fmt_num(avg_added, 1) if avg_added is not None else EM_DASH
    ctx["BOARD_AVG_REMOVED_SP"] = fmt_num(avg_removed, 1) if avg_removed is not None else EM_DASH
    ctx["BOARD_AVG_ESTIMATION_CHANGE_SP"] = fmt_num(avg_estchg, 1) if avg_estchg is not None else EM_DASH
    ctx["KPI_AVG_PERFORMANCE_PCT"] = fmt_num(kpi.get("avg_performance_pct"), 2) if n_base else EM_DASH
    ctx["KPI_AVG_SCOPE_CHANGE_PCT"] = fmt_num(kpi.get("avg_scope_change_pct"), 2) if n_base else EM_DASH
    ctx["KPI_VELOCITY_SMA5_SP"] = fmt_num(kpi.get("velocity_sma5_sp"), 1) if n_base else EM_DASH
    ctx["KPI_THROUGHPUT_AVG_ITEMS"] = fmt_num(kpi.get("throughput_avg_items"), 2) if n_base else EM_DASH
    ctx["KPI_AVG_CLOSURE_PCT_ITEMS"] = fmt_num(kpi.get("avg_closure_pct_items"), 2) if n_base else EM_DASH
    ctx["KPI_AVG_CLOSURE_PCT_SP"] = fmt_num(kpi.get("avg_closure_pct_sp"), 2) if n_base else EM_DASH
    ctx["TABLE_BOARD_CAPTION"] = "Таблица B — Board · одна строка на спринт: базовые и целевые, по возрастанию start_at."
    return list(enumerate(sprints))


# ==========================================================================
# Forecast (tab 1)
# ==========================================================================

_FORECAST_ERROR_TEXT = {
    "ERR_FORECAST_NOT_ENOUGH_DATA": "Недостаточно ненулевых дней пропускной способности (нужно минимум 10) — прогноз не построен.",
}


def build_forecast(report: dict, ctx: dict) -> str:
    """Returns which block to keep: 'grid' or 'unavailable'."""
    forecast = report.get("forecast")
    forecast_error = report.get("forecast_error")

    if forecast is None:
        reason = _FORECAST_ERROR_TEXT.get(forecast_error, forecast_error or "прогноз недоступен")
        ctx["FORECAST_UNAVAILABLE"] = (
            '<div class="card" style="padding:16px"><p class="section-desc" style="margin:0;text-align:left">'
            f"Прогноз не построен: {esc(reason)}</p></div>"
        )
        ctx["FORECAST_WARNING_BLOCK"] = ""
        return "unavailable"

    warnings = forecast.get("warnings") or []
    if "WARN_THROUGHPUT_UNSTABLE" in warnings:
        ctx["FORECAST_WARNING_BLOCK"] = (
            '<div class="banner"><span class="banner-tag">WARN_THROUGHPUT_UNSTABLE</span>'
            '<div class="banner-body"><b>Пропускная способность нестабильна.</b>'
            f'<span class="detail">Недельный коэффициент вариации <b class="num">{fmt_num(forecast["throughput_cv_pct"], 1)}%</b> '
            "при пороге 50%. Прогноз построен, но разброс перцентилей отражает реальный разброс недель, "
            "а не узкую оценку.</span></div></div>"
        )
    else:
        ctx["FORECAST_WARNING_BLOCK"] = ""

    ctx["FORECAST_TARGET_ITEMS"] = fmt_int(forecast["target_items"])
    percentiles = {p["percentile"]: p["days"] for p in forecast["percentiles"]}
    ctx["FORECAST_P50_KEY"] = "p50"
    ctx["FORECAST_P50_VALUE"] = fmt_int(percentiles.get(50))
    ctx["FORECAST_P85_KEY"] = "p85"
    ctx["FORECAST_P85_VALUE"] = fmt_int(percentiles.get(85))
    ctx["FORECAST_P95_KEY"] = "p95"
    ctx["FORECAST_P95_VALUE"] = fmt_int(percentiles.get(95))
    ctx["FORECAST_CV_PCT"] = fmt_num(forecast["throughput_cv_pct"], 1)
    ctx["FORECAST_SAMPLE_SPRINTS"] = fmt_int(forecast["sample_sprints"])
    sample_days = forecast["sample_days"]
    ctx["FORECAST_SAMPLE_DAYS"] = esc(
        f"{sample_days} {ru_plural(sample_days, 'календарный день', 'календарных дня', 'календарных дней')}"
    )

    aria, svg = build_forecast_svg(forecast)
    ctx["FORECAST_HISTOGRAM_ARIA"] = ""
    ctx["FORECAST_HISTOGRAM_SVG"] = svg
    return "grid"


def build_forecast_svg(forecast: dict) -> tuple:
    x0, x1 = 48.0, 740.0
    y_top, y_base = 20.0, 232.0
    histogram = forecast.get("histogram") or []

    if not histogram:
        aria = "Гистограмма недоступна: симуляция не вернула исходов."
        return aria, (
            '<svg class="chart-svg" viewBox="0 0 760 300" role="img" aria-label="'
            + esc(aria)
            + '"><text class="c-axis-title" x="380" y="150" text-anchor="middle">нет данных</text></svg>'
        )

    days_vals = [b["days"] for b in histogram]
    counts = [b["count"] for b in histogram]
    n = len(histogram)
    n_ticks = 5
    step_val = nice_step(max(counts), n_ticks)
    top = step_val * n_ticks

    def y_of(c: float) -> float:
        return y_base - (c / top) * (y_base - y_top)

    spacing = (x1 - x0) / n
    bar_w = spacing * 0.72
    xs = [x0 + i * spacing + (spacing - bar_w) / 2 for i in range(n)]

    percentiles = {p["percentile"]: p["days"] for p in forecast["percentiles"]}
    p50_days = percentiles.get(50)
    mode_days = days_vals[counts.index(max(counts))]

    bars = []
    for x, days, count in zip(xs, days_vals, counts):
        strong = days == p50_days or days == mode_days
        cls = "c-bar-strong" if strong else "c-bar"
        h = y_base - y_of(count)
        day_word = ru_plural(days, "день", "дня", "дней")
        run_word = ru_plural(count, "прогон", "прогона", "прогонов")
        title = f"{days} {day_word} — {count} {run_word}"
        bars.append(f'<rect class="{cls}" x="{x:.2f}" y="{y_of(count):.2f}" width="{bar_w:.2f}" height="{h:.2f}" rx="2"><title>{esc(title)}</title></rect>')
    bars_html = "".join(bars)

    grid = "".join(f'<line x1="{x0}" y1="{y_of(step_val*i):.1f}" x2="{x1}" y2="{y_of(step_val*i):.1f}"/>' for i in range(1, n_ticks + 1))
    y_labels = "".join(f'<text x="{x0-8}" y="{y_of(step_val*i)+4:.1f}">{fmt_num(step_val*i, 0)}</text>' for i in range(0, n_ticks + 1))
    x_labels = "".join(f'<text x="{x+bar_w/2:.2f}" y="{y_base+16:.0f}">{days}</text>' for x, days in zip(xs, days_vals))

    def marker_x(days_value: Optional[int]) -> Optional[float]:
        if days_value is None:
            return None
        if days_value in days_vals:
            i = days_vals.index(days_value)
            return xs[i] + bar_w / 2
        return x0 + (days_value - days_vals[0]) * spacing + bar_w / 2

    p85_x = marker_x(percentiles.get(85))
    p95_x = marker_x(percentiles.get(95))
    p50_x = marker_x(p50_days)

    markers = []
    labels = []
    if p85_x is not None:
        markers.append(f'<line x1="{p85_x:.2f}" y1="{y_top}" x2="{p85_x:.2f}" y2="{y_base}"/>')
        labels.append(f'<text x="{p85_x:.2f}" y="{y_base+58:.0f}">p85</text>')
    if p95_x is not None:
        markers.append(f'<line x1="{p95_x:.2f}" y1="{y_top}" x2="{p95_x:.2f}" y2="{y_base}"/>')
        labels.append(f'<text x="{p95_x:.2f}" y="{y_base+58:.0f}">p95</text>')
    markers_html = f'<g class="c-marker">{"".join(markers)}</g>' if markers else ""
    p50_marker = f'<line class="c-marker-strong" x1="{p50_x:.2f}" y1="{y_top}" x2="{p50_x:.2f}" y2="{y_base}"/>' if p50_x is not None else ""
    p50_label = f'<text class="c-marker-label-strong" x="{p50_x:.2f}" y="{y_base+58:.0f}" text-anchor="middle">p50</text>' if p50_x is not None else ""

    mode_day_word = ru_plural(mode_days, "день", "дня", "дней")
    aria = (
        f"Гистограмма исходов симуляции. Пик приходится на {mode_days} {mode_day_word}. "
        f"Перцентили: p50 на {fmt_int(percentiles.get(50))} днях, p85 на {fmt_int(percentiles.get(85))} днях, "
        f"p95 на {fmt_int(percentiles.get(95))} днях."
    )

    svg = (
        '<svg class="chart-svg" viewBox="0 0 760 300" preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{esc(aria)}">'
        f'<g class="c-grid">{grid}</g>'
        f'<g class="c-unit-label" text-anchor="end">{y_labels}</g>'
        f'<text class="c-axis-title" x="14" y="{(y_top+y_base)/2:.0f}" text-anchor="middle" '
        f'transform="rotate(-90 14 {(y_top+y_base)/2:.0f})">Прогонов симуляции</text>'
        f'<g class="c-bar">{bars_html}</g>'
        f'<line class="c-axis" x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}"/>'
        f'<line class="c-axis" x1="{x0}" y1="{y_top}" x2="{x0}" y2="{y_base}"/>'
        f'<g class="c-tick-label" text-anchor="middle">{x_labels}</g>'
        f'<text class="c-axis-title" x="{(x0+x1)/2:.0f}" y="{y_base+36:.0f}" text-anchor="middle">'
        f"Календарных дней до закрытия {fmt_int(forecast['target_items'])} "
        f"{ru_plural(forecast['target_items'], 'задачи', 'задач', 'задач')}</text>"
        f"{markers_html}"
        f'<g class="c-marker-label" text-anchor="middle">{"".join(labels)}</g>'
        f"{p50_marker}{p50_label}"
        "</svg>"
    )
    return aria, svg


# ==========================================================================
# Engineering tab (tab 3)
# ==========================================================================

_ENG_TILE_META = {
    "PIPELINES_COUNT": ("pipelines", "count"),
    "PIPELINES_SUCCESS_RATE": ("pipelines", "success_rate_pct"),
    "DEPLOYMENTS_COUNT": ("deployments", "count"),
    "DEPLOYMENTS_PER_WEEK": ("deployments", "per_week"),
    "COVERAGE": ("coverage", "coverage_avg_pct"),
}


def _eng_tile_common(
    ctx: dict, tid: str, *, status: str, value_text: str, unit: str, caveat: bool = False, caveat_token: Optional[str] = None
) -> None:
    ctx[f"ENG_{tid}_STATUS"] = status
    ctx[f"ENG_{tid}_STATUS_LABEL"] = esc(STATUS_LABELS[status])
    ctx[f"ENG_{tid}_VALUE"] = value_text
    ctx[f"ENG_{tid}_UNIT"] = esc(unit)
    # Token names per DESIGN-NOTES §7 do not all follow the tile id 1:1
    # (e.g. the pipelines.count tile's marker is {{ENG_PIPELINES_WINDOW_CAVEAT}},
    # not {{ENG_PIPELINES_COUNT_WINDOW_CAVEAT}}, and the success-rate tile has
    # no caveat span at all) — the caller passes the exact documented token
    # name explicitly rather than deriving it from `tid`.
    if caveat_token:
        ctx[caveat_token] = '<span class="caveat">вне окна отчёта</span>' if caveat else ""


def build_eng_tab(report: dict, ctx: dict) -> str:
    eng = report.get("engineering") or {}
    if not eng.get("available"):
        ctx["ENG_WINDOW_APPLIED_NOTE"] = esc(eng.get("reason") or "GitLab не настроен для этого запуска.")
        return "unavailable"

    data = eng["data"]
    window = data.get("window_applied") or {}
    ctx["ENG_WINDOW_APPLIED_NOTE"] = esc(
        "Флаги по разделам: "
        + ", ".join(f"{k} = {'true' if v else 'false'}" for k, v in window.items())
        + ". Там, где флаг false, значение посчитано по всему проекту целиком и не сопоставимо с остальными "
        "числами отчёта."
    )

    pipe, dep, cov = data["pipelines"], data["deployments"], data["coverage"]

    # pipelines.count is a run count, not a rate — §5 defines no band for
    # it, so it never borrows the success-rate band (m6).
    p_status = "none"
    _eng_tile_common(
        ctx, "PIPELINES_COUNT", status=p_status,
        value_text=fmt_int(pipe["count"]), unit=f"запусков · pipelines.count · failed {pipe['failed']}",
        caveat=not window.get("pipelines", True), caveat_token="ENG_PIPELINES_WINDOW_CAVEAT",
    )
    ctx["ENG_PIPELINES_COUNT_DELTA_DIR"] = "none"
    ctx["ENG_PIPELINES_COUNT_DELTA_ARROW"] = ARROW_FLAT
    ctx["ENG_PIPELINES_COUNT_DELTA"] = "без сравнения"
    ctx["ENG_PIPELINES_PER_WEEK"] = (
        f'<div class="delta-note">{fmt_num(pipe["per_week"], 2)} в неделю</div>' if pipe.get("per_week") else '<div class="delta-note">частота недоступна</div>'
    )
    ctx["ENG_PIPELINES_COUNT_SPARK_POINTS"] = build_spark_svg([pipe["count"]] if pipe["count"] else [], spark_status_class(p_status))

    sr_status = "none" if pipe["count"] == 0 else _status_band(pipe["success_rate_pct"], 90, 80, higher_is_better=True)
    _eng_tile_common(
        ctx, "PIPELINES_SUCCESS_RATE", status=sr_status,
        value_text=fmt_num(pipe["success_rate_pct"], 2) if pipe["count"] else EM_DASH,
        unit=(f"% · pipelines.success_rate_pct · {pipe['count']-pipe['failed']} из {pipe['count']}" if pipe["count"] else "нет запусков"),
    )
    ctx["ENG_PIPELINES_SUCCESS_RATE_DELTA_DIR"] = "none"
    ctx["ENG_PIPELINES_SUCCESS_RATE_DELTA_ARROW"] = ARROW_FLAT
    ctx["ENG_PIPELINES_SUCCESS_RATE_DELTA"] = "без сравнения"
    ctx["ENG_PIPELINES_SUCCESS_RATE_DELTA_NOTE"] = "история по неделям не входит в контракт данных"
    ctx["ENG_PIPELINES_SUCCESS_RATE_SPARK_POINTS"] = build_spark_svg([pipe["success_rate_pct"]] if pipe["count"] else [], spark_status_class(sr_status))

    dep_status = "none" if dep["count"] == 0 else ("good" if dep["success_rate_pct"] >= 90 else ("warn" if dep["success_rate_pct"] >= 75 else "bad"))
    _eng_tile_common(
        ctx, "DEPLOYMENTS_COUNT", status=dep_status,
        value_text=fmt_int(dep["count"]),
        unit=(f"выкатов · deployments.count · failed {dep['failed']}, success_rate_pct {fmt_num(dep['success_rate_pct'], 2)}" if dep["count"] else "нет выкатов"),
        caveat=not window.get("deployments", True), caveat_token="ENG_DEPLOYMENTS_WINDOW_CAVEAT",
    )
    ctx["ENG_DEPLOYMENTS_COUNT_DELTA_DIR"] = "none"
    ctx["ENG_DEPLOYMENTS_COUNT_DELTA_ARROW"] = ARROW_FLAT
    ctx["ENG_DEPLOYMENTS_COUNT_DELTA"] = "без сравнения"
    ctx["ENG_DEPLOYMENTS_COUNT_DELTA_NOTE"] = esc(f"window_applied.deployments = {'true' if window.get('deployments') else 'false'}")
    ctx["ENG_DEPLOYMENTS_COUNT_SPARK_POINTS"] = build_spark_svg([dep["count"]] if dep["count"] else [], spark_status_class(dep_status))

    # deployments.per_week is a frequency, not a rate — §5 defines no band
    # for it either, so it no longer reads unconditionally "норма" (m6).
    pw_status = "none"
    _eng_tile_common(
        ctx, "DEPLOYMENTS_PER_WEEK", status=pw_status,
        value_text=(fmt_num(dep["per_week"], 2) if dep.get("per_week") else EM_DASH),
        unit="выката в неделю · deployments.per_week",
        caveat=not window.get("deployments", True), caveat_token="ENG_DEPLOYMENTS_PER_WEEK_WINDOW_CAVEAT",
    )
    ctx["ENG_DEPLOYMENTS_PER_WEEK_DELTA_DIR"] = "none"
    ctx["ENG_DEPLOYMENTS_PER_WEEK_DELTA_ARROW"] = ARROW_FLAT
    ctx["ENG_DEPLOYMENTS_PER_WEEK_DELTA"] = "без сравнения"
    ctx["ENG_DEPLOYMENTS_PER_WEEK_DELTA_NOTE"] = esc(f"{dep['count']} выкатов" if dep.get("per_week") else "недостаточно точек для частоты")
    ctx["ENG_DEPLOYMENTS_PER_WEEK_SPARK_POINTS"] = build_spark_svg([dep["per_week"]] if dep.get("per_week") else [], spark_status_class(pw_status))

    cov_available = cov.get("coverage_avg_pct") is not None
    cov_status = "none" if not cov_available else _status_band(cov["coverage_avg_pct"], 80, 65, higher_is_better=True)
    project_word = ru_plural(cov["sample_count"], "проекту", "проектам", "проектам")
    _eng_tile_common(
        ctx, "COVERAGE", status=cov_status,
        value_text=(fmt_num(cov["coverage_avg_pct"], 2) if cov_available else EM_DASH),
        unit=(f"% · coverage.coverage_avg_pct · среднее по {cov['sample_count']} {project_word}" if cov_available else "нет данных о покрытии"),
        caveat=not window.get("coverage", True), caveat_token="ENG_COVERAGE_WINDOW_CAVEAT",
    )
    ctx["ENG_COVERAGE_DELTA_DIR"] = "none"
    ctx["ENG_COVERAGE_DELTA_ARROW"] = ARROW_FLAT
    ctx["ENG_COVERAGE_DELTA"] = "без сравнения"
    ctx["ENG_COVERAGE_SAMPLE_COUNT"] = esc(f"sample_count = {cov['sample_count']}")
    ctx["ENG_COVERAGE_SPARK_POINTS"] = build_spark_svg([cov["coverage_avg_pct"]] if cov_available else [], spark_status_class(cov_status))

    # Weekly trend line chart: engineering_metrics.py returns only aggregate
    # counts, no per-week series, so there is no data to plot here — render
    # the explicit-unavailable state rather than a fabricated line.
    aria = "Понедельная история success_rate_pct недоступна: контракт данных отдаёт только агрегат за окно отчёта, без разбивки по неделям."
    ctx["ENG_CHART_ARIA"] = ""
    ctx["ENG_CHART_SVG"] = (
        '<svg class="chart-svg" viewBox="0 0 760 260" role="img" aria-label="'
        + esc(aria)
        + '"><text class="c-axis-title" x="380" y="130" text-anchor="middle">'
        "понедельная история не входит в контракт данных</text></svg>"
    )

    ctx["ENG_DEPLOYMENTS_TABLE_CAVEAT"] = '<span class="caveat" style="margin:0">вне окна</span>' if not window.get("deployments", True) else ""
    ctx["ENG_COVERAGE_TABLE_CAVEAT"] = '<span class="caveat" style="margin:0">вне окна</span>' if not window.get("coverage", True) else ""

    projects = sorted(
        set(r["project"] for r in pipe["per_project"])
        | set(r["project"] for r in dep["per_project"])
        | set(r["project"] for r in cov["per_project"])
    )
    by_pipe = {r["project"]: r for r in pipe["per_project"]}
    by_dep = {r["project"]: r for r in dep["per_project"]}
    by_cov = {r["project"]: r for r in cov["per_project"]}

    project_rows = [
        {
            "project": proj,
            "pipelines": by_pipe.get(proj, {"count": 0, "failed": 0, "success_rate_pct": None}),
            "deployments": by_dep.get(proj, {"count": 0, "failed": 0, "success_rate_pct": None}),
            "coverage": by_cov.get(proj, {"coverage_avg_pct": None, "sample_count": 0}),
        }
        for proj in projects
    ]
    ctx["_ENG_PROJECT_ROWS"] = project_rows
    repo_word = ru_plural(len(projects), "репозиторий", "репозитория", "репозиториев")
    caption = f"per_project · {len(projects)} {repo_word}"
    # A skipped project must be visible right next to the table it is
    # missing from, not only as a disclaimer buried in the footer.
    skipped = (report.get("gitlab_fetch_issues") or {}).get("skipped_projects") or []
    if skipped:
        skip_word = ru_plural(len(skipped), "проект пропущен", "проекта пропущено", "проектов пропущено")
        names = ", ".join(f"{p.get('project')} ({p.get('code')})" for p in skipped)
        caption += f" — {len(skipped)} {skip_word}: {names}"
    ctx["ENG_TABLE_CAPTION"] = esc(caption)
    ctx["ENG_TABLE_FOOTER_LABEL"] = "Итого"
    ctx["ENG_PIPELINES_COUNT_TOTAL"] = fmt_int(pipe["count"])
    ctx["ENG_PIPELINES_FAILED_TOTAL"] = fmt_int(pipe["failed"])
    ctx["ENG_PIPELINES_SUCCESS_RATE_TOTAL"] = fmt_num(pipe["success_rate_pct"], 2)
    ctx["ENG_DEPLOYMENTS_COUNT_TOTAL"] = fmt_int(dep["count"])
    ctx["ENG_DEPLOYMENTS_FAILED_TOTAL"] = fmt_int(dep["failed"])
    ctx["ENG_DEPLOYMENTS_SUCCESS_RATE_TOTAL"] = fmt_num(dep["success_rate_pct"], 2)
    ctx["ENG_COVERAGE_AVG_PCT_TOTAL"] = fmt_num(cov.get("coverage_avg_pct"), 2) if cov.get("coverage_avg_pct") is not None else EM_DASH
    ctx["ENG_COVERAGE_SAMPLE_COUNT_TOTAL"] = fmt_int(cov["sample_count"])
    return "available"


def build_eng_project_row_html(inner: str, row: dict) -> str:
    pp, dd, cc = row["pipelines"], row["deployments"], row["coverage"]
    cov_val = cc.get("coverage_avg_pct")
    cov_cell = fmt_num(cov_val, 2) if cov_val is not None else '&mdash; <span class="badge-na">нет данных</span>'
    cov_cls = "" if cov_val is not None else " cell-unavail"
    mapping = {
        "ENG_PROJECT_NAME": esc(row["project"]),
        "ENG_PROJECT_PIPELINES_COUNT": fmt_int(pp.get("count", 0)),
        "ENG_PROJECT_PIPELINES_FAILED": fmt_int(pp.get("failed", 0)),
        "ENG_PROJECT_PIPELINES_SUCCESS_RATE_PCT": fmt_num(pp.get("success_rate_pct"), 2),
        "ENG_PROJECT_DEPLOYMENTS_COUNT": fmt_int(dd.get("count", 0)),
        "ENG_PROJECT_DEPLOYMENTS_FAILED": fmt_int(dd.get("failed", 0)),
        "ENG_PROJECT_DEPLOYMENTS_SUCCESS_RATE_PCT": fmt_num(dd.get("success_rate_pct"), 2),
        "ENG_PROJECT_COVERAGE_AVG_PCT": cov_cell,
        "ENG_PROJECT_COVERAGE_AVG_PCT_CLASS": cov_cls,
        "ENG_PROJECT_COVERAGE_SAMPLE_COUNT": fmt_int(cc.get("sample_count", 0)),
    }
    return sub_tokens(inner, mapping)


# ==========================================================================
# Personal tab (tab 2)
# ==========================================================================


_NOSAMPLE_NO_MR = "пустая выборка: ни одного MR"
_NOSAMPLE_NO_CLOSED_TASK = "пустая выборка: ни одной закрытой задачи"
_NOSAMPLE_GENERIC = "пустая выборка: нет исходных значений"


def _cell_state(
    value, warnings: list, warn_code: Optional[str], div0_code: Optional[str],
    decimals: int = 2, unit: str = "", nosample_title: str = _NOSAMPLE_GENERIC,
) -> tuple:
    """Returns (html_for_cell, css_class_suffix) implementing the four
    empty-value states from DESIGN-NOTES §5. `warn_code` is the
    `..._UNAVAILABLE` code that distinguishes 'unavail' from 'nosample';
    `div0_code` (prefixed `WARN_DIVISION_BY_ZERO:`) distinguishes a real
    ratio-DIV0 zero from every other case. `nosample_title` names what was
    empty (no MR / no closed task / ...) — the same generic wording on
    every column made the empty-sample state unreadable column to column."""
    if div0_code and div0_code in warnings:
        return f'0{unit} <span class="warn-badge">DIV0</span>', " cell-nodata"
    if value is None:
        if warn_code and warn_code in warnings:
            return f'&mdash; <span class="badge-na">нет данных</span>', " cell-unavail"
        return f'<span title="{esc(nosample_title)}">&mdash;</span>', " cell-nosample"
    return fmt_num(value, decimals) + unit, ""


def _person_cell_map(person: dict) -> dict:
    w = person.get("warnings") or []
    m: dict = {}

    def put(name, value, warn_code, div0_code, decimals=2, unit="", nosample_title=_NOSAMPLE_GENERIC):
        text, cls = _cell_state(value, w, warn_code, div0_code, decimals, unit, nosample_title)
        m[name] = text
        m[f"{name}_CLASS"] = cls

    put("PERSON_MR_MERGE_RATE_PCT", person["mr_merge_rate_pct"], None, "WARN_DIVISION_BY_ZERO:mr_merge_rate_pct", 2)
    put("PERSON_MR_CYCLE_TIME_AVG_HOURS", person["mr_cycle_time_avg_hours"], "WARN_MR_CYCLE_TIME_UNAVAILABLE", None, 1, nosample_title=_NOSAMPLE_NO_MR)
    put("PERSON_MR_CYCLE_TIME_MEDIAN_HOURS", person["mr_cycle_time_median_hours"], "WARN_MR_CYCLE_TIME_UNAVAILABLE", None, 1, nosample_title=_NOSAMPLE_NO_MR)
    put("PERSON_MR_DIFF_SIZE_AVG", person["mr_diff_size_avg"], "WARN_DIFF_STATS_UNAVAILABLE", None, 1, nosample_title=_NOSAMPLE_NO_MR)
    put("PERSON_MR_COMMITS_AVG", person["mr_commits_avg"], "WARN_COMMITS_UNAVAILABLE", None, 2, nosample_title=_NOSAMPLE_NO_MR)
    put("PERSON_MR_COMMITS_SUM", person["mr_commits_sum"], "WARN_COMMITS_UNAVAILABLE", None, 0, nosample_title=_NOSAMPLE_NO_MR)
    put("PERSON_MR_CHANGES_COUNT_AVG", person["mr_changes_count_avg"], "WARN_CHANGES_COUNT_UNAVAILABLE", None, 2, nosample_title=_NOSAMPLE_NO_MR)
    put("PERSON_MR_CHANGES_COUNT_SUM", person["mr_changes_count_sum"], "WARN_CHANGES_COUNT_UNAVAILABLE", None, 0, nosample_title=_NOSAMPLE_NO_MR)
    put("PERSON_DEFECT_RATE_PCT", person["defect_rate_pct"], None, "WARN_DIVISION_BY_ZERO:defect_rate_pct", 2)
    put("PERSON_TASK_CYCLE_TIME_AVG_HOURS", person["task_cycle_time_avg_hours"], "WARN_TASK_CYCLE_TIME_UNAVAILABLE", None, 1, nosample_title=_NOSAMPLE_NO_CLOSED_TASK)
    put("PERSON_TASK_CYCLE_TIME_MEDIAN_HOURS", person["task_cycle_time_median_hours"], "WARN_TASK_CYCLE_TIME_UNAVAILABLE", None, 1, nosample_title=_NOSAMPLE_NO_CLOSED_TASK)
    put("PERSON_STORY_POINTS_TOTAL", person["story_points_total"], None, None, 1, nosample_title=_NOSAMPLE_NO_CLOSED_TASK)
    put("PERSON_STORY_POINTS_AVG", person["story_points_avg"], None, None, 2, nosample_title=_NOSAMPLE_NO_CLOSED_TASK)
    put("PERSON_QA_ESTIMATION_TOTAL", person["qa_estimation_total"], None, None, 1, nosample_title=_NOSAMPLE_NO_CLOSED_TASK)
    put("PERSON_QA_ESTIMATION_AVG", person["qa_estimation_avg"], None, None, 2, nosample_title=_NOSAMPLE_NO_CLOSED_TASK)

    # mr_per_task: bare dash, "null by contract" when linked_tasks == 0 —
    # distinct from every other empty-value state (§5's fourth state).
    if person["mr_per_task"] is None:
        m["PERSON_MR_PER_TASK"] = '<span title="null по контракту: linked_tasks = 0">&mdash;</span>'
        m["PERSON_MR_PER_TASK_CLASS"] = " cell-null"
    else:
        m["PERSON_MR_PER_TASK"] = fmt_num(person["mr_per_task"], 2)
        m["PERSON_MR_PER_TASK_CLASS"] = ""

    return m


def _mr_fetch_error_badges(person: dict) -> str:
    badges = []
    for e in person.get("_mr_fetch_errors") or []:
        title = f"{esc(e.get('project'))} · {esc(e.get('state'))}: {esc(e.get('code'))} — {esc(e.get('message'))}"
        badges.append(f'<span class="wbadge" title="{title}">MR_FETCH_ERROR</span>')
    return "".join(badges)


def build_person_row_html(inner: str, person: dict) -> str:
    diff_avail = person.get("mr_diff_size_available_count") or 0
    diff_denom = f'<span class="denom">/ {diff_avail}</span>' if person["mr_diff_size_avg"] is not None else ""
    warnings = person.get("warnings") or []
    fetch_error_badges = _mr_fetch_error_badges(person)
    warn_html = (
        '<div class="row-warnings">'
        + "".join(f'<span class="wbadge">{esc(w)}</span>' for w in warnings)
        + fetch_error_badges
        + "</div>"
        if warnings or fetch_error_badges
        else '<span class="cell-muted">&mdash;</span>'
    )
    cell_map = _person_cell_map(person)
    mapping = {
        "PERSON_USER": esc(person["user"]),
        "PERSON_MR_COUNT": fmt_int(person["mr_count"]),
        "PERSON_MR_MERGED_COUNT": fmt_int(person["mr_merged_count"]),
        "PERSON_MR_CLOSED_COUNT": fmt_int(person["mr_closed_count"]),
        "PERSON_MR_DIFF_SIZE_AVAILABLE_COUNT": diff_denom,
        "PERSON_TASKS_DONE": fmt_int(person["tasks_done"]),
        "PERSON_BUG_COUNT": fmt_int(person["bug_count"]),
        "PERSON_REWORK_TOTAL": fmt_int(person["rework_total"]),
        "PERSON_LINKED_TASKS": fmt_int(person["linked_tasks"]),
        "PERSON_MR_WITH_JIRA_KEY": fmt_int(person["mr_with_jira_key"]),
        "PERSON_WARNINGS": warn_html,
    }
    mapping.update(cell_map)
    return sub_tokens(inner, mapping)


def _sum_field(people: list, key: str) -> float:
    return sum((p.get(key) or 0) for p in people)


def build_person_tab(report: dict, ctx: dict) -> str:
    personal = report.get("personal") or {}
    if not personal.get("available"):
        ctx["PERSON_LIGHT_MODE_CAVEAT"] = ""
        ctx["PERSONAL_SEMANTICS_NOTE"] = (
            '<div class="banner"><span class="banner-tag">НЕДОСТУПНО</span>'
            f'<div class="banner-body"><b>Персональные метрики недоступны.</b>'
            f'<span class="detail">{esc(personal.get("reason") or "GitLab не настроен для этого запуска.")}</span>'
            "</div></div>"
        )
        return "unavailable"

    # A run built with the heavy GitLab fan-outs skipped (audit finding 2a)
    # must say so somewhere a reader will actually see it — not only as a
    # request count in the footer. This is the one tab those two opt-outs
    # actually change (team-level engineering tiles aggregate by pipeline
    # status, not by author, so they are unaffected either way).
    params = report.get("params") or {}
    light_mode_bits = []
    if params.get("gitlab_fetch_mr_details") is False:
        light_mode_bits.append(
            "детали MR (размер диффа, коммиты, изменения) не собирались — соответствующие колонки "
            "и карточки размечены «нет данных», это не означает, что там 0"
        )
    if params.get("gitlab_fetch_pipeline_user") is False:
        light_mode_bits.append(
            "привязка пайплайнов к конкретному человеку не собиралась — «успешность пайплайнов» "
            "на карточках участников размечена «нет данных» у всех"
        )
    ctx["PERSON_LIGHT_MODE_CAVEAT"] = (
        '<div class="banner"><span class="banner-tag">УРЕЗАННЫЙ РЕЖИМ</span>'
        "<div class=\"banner-body\"><b>Этот запуск пропустил часть сбора данных в GitLab ради меньшей нагрузки.</b>"
        f'<span class="detail">{esc("; ".join(light_mode_bits))}.</span></div></div>'
        if light_mode_bits
        else ""
    )

    notes = report.get("semantics_notes") or []
    ctx["PERSONAL_SEMANTICS_NOTE"] = (
        '<div class="banner info"><span class="banner-tag">СЕМАНТИКА</span>'
        '<div class="banner-body"><b>Числа этой вкладки не обязаны сходиться с вкладкой «Команда».</b>'
        "<span class=\"detail\">" + "<br><br>".join(esc(n) for n in notes) + "</span></div></div>"
    )

    people = personal["data"]["people"]
    # Attach each person's mr_fetch_errors (m4) as a shallow-copy field so a
    # failed fetch shows on their own row/card, not only as a bare count in
    # the footer — a silently-low mr_count must not read as real activity.
    mr_fetch_errors = (report.get("gitlab_fetch_issues") or {}).get("mr_fetch_errors") or []
    errors_by_user: dict = {}
    for e in mr_fetch_errors:
        errors_by_user.setdefault(e.get("author"), []).append(e)
    people = [dict(p, _mr_fetch_errors=errors_by_user.get(p["user"], [])) for p in people]
    window = (report.get("engineering") or {}).get("data", {}).get("window_applied") or {}
    mr_window_applied = window.get("merge_requests", True)
    ctx["PERSON_MR_WINDOW_CAVEAT"] = (
        ' <span class="caveat" style="margin-top:0">вне окна отчёта — merge_requests</span>' if not mr_window_applied else ""
    )
    participant_word = ru_plural(len(people), "участник", "участника", "участников")
    ctx["PERSON_TABLE_CAPTION"] = esc(
        f"Разрез по людям · {len(people)} {participant_word}, ключ строки — поле user, общее для GitLab и Jira. "
        "Колонка email в источнике всегда пуста и поэтому не выводится."
    )
    ctx["PERSON_TABLE_FOOTER_LABEL"] = "Итого"

    total_mr_count = _sum_field(people, "mr_count")
    total_merged = _sum_field(people, "mr_merged_count")
    total_closed = _sum_field(people, "mr_closed_count")
    total_commits_sum = _sum_field(people, "mr_commits_sum")
    total_changes_sum = _sum_field(people, "mr_changes_count_sum")
    total_tasks = _sum_field(people, "tasks_done")
    total_bugs = _sum_field(people, "bug_count")
    total_rework = _sum_field(people, "rework_total")
    total_sp = _sum_field(people, "story_points_total")
    total_qa = _sum_field(people, "qa_estimation_total")
    total_linked = _sum_field(people, "linked_tasks")
    total_with_key = _sum_field(people, "mr_with_jira_key")
    total_warnings = sum(len(p.get("warnings") or []) for p in people)

    diff_weight_sum = sum((p.get("mr_diff_size_avg") or 0) * (p.get("mr_diff_size_available_count") or 0) for p in people)
    diff_weight_n = sum((p.get("mr_diff_size_available_count") or 0) for p in people)

    def _weighted(values_weights):
        num = sum(v * w for v, w in values_weights if v is not None)
        den = sum(w for v, w in values_weights if v is not None)
        return num / den if den else None

    total_mr_cycle_avg = _weighted([(p.get("mr_cycle_time_avg_hours"), p.get("mr_count") or 0) for p in people])
    total_mr_cycle_median = _weighted([(p.get("mr_cycle_time_median_hours"), p.get("mr_count") or 0) for p in people])
    total_task_cycle_avg = _weighted([(p.get("task_cycle_time_avg_hours"), p.get("tasks_done") or 0) for p in people])
    total_task_cycle_median = _weighted([(p.get("task_cycle_time_median_hours"), p.get("tasks_done") or 0) for p in people])

    ctx["PERSON_TOTAL_MR_COUNT"] = fmt_int(total_mr_count)
    ctx["PERSON_TOTAL_MR_MERGED_COUNT"] = fmt_int(total_merged)
    ctx["PERSON_TOTAL_MR_CLOSED_COUNT"] = fmt_int(total_closed)
    ctx["PERSON_TOTAL_MR_MERGE_RATE_PCT"] = fmt_num(total_merged / total_mr_count * 100 if total_mr_count else None, 2)
    ctx["PERSON_TOTAL_MR_CYCLE_TIME_AVG_HOURS"] = fmt_num(total_mr_cycle_avg, 1)
    ctx["PERSON_TOTAL_MR_CYCLE_TIME_MEDIAN_HOURS"] = fmt_num(total_mr_cycle_median, 1)
    ctx["PERSON_TOTAL_MR_DIFF_SIZE_AVG"] = (
        fmt_num(diff_weight_sum / diff_weight_n, 2) + f'<span class="denom">/ {int(diff_weight_n)}</span>'
        if diff_weight_n
        else EM_DASH
    )
    ctx["PERSON_TOTAL_MR_COMMITS_AVG"] = fmt_num(total_commits_sum / total_mr_count if total_mr_count else None, 2)
    ctx["PERSON_TOTAL_MR_COMMITS_SUM"] = fmt_int(total_commits_sum)
    ctx["PERSON_TOTAL_MR_CHANGES_COUNT_AVG"] = fmt_num(total_changes_sum / total_mr_count if total_mr_count else None, 2)
    ctx["PERSON_TOTAL_MR_CHANGES_COUNT_SUM"] = fmt_int(total_changes_sum)
    ctx["PERSON_TOTAL_TASKS_DONE"] = fmt_int(total_tasks)
    ctx["PERSON_TOTAL_BUG_COUNT"] = fmt_int(total_bugs)
    ctx["PERSON_TOTAL_DEFECT_RATE_PCT"] = fmt_num(total_bugs / total_tasks * 100 if total_tasks else None, 2)
    ctx["PERSON_TOTAL_TASK_CYCLE_TIME_AVG_HOURS"] = fmt_num(total_task_cycle_avg, 1)
    ctx["PERSON_TOTAL_TASK_CYCLE_TIME_MEDIAN_HOURS"] = fmt_num(total_task_cycle_median, 1)
    ctx["PERSON_TOTAL_REWORK_TOTAL"] = fmt_int(total_rework)
    ctx["PERSON_TOTAL_STORY_POINTS_TOTAL"] = fmt_num(total_sp, 1)
    ctx["PERSON_TOTAL_STORY_POINTS_AVG"] = fmt_num(total_sp / total_tasks if total_tasks else None, 2)
    ctx["PERSON_TOTAL_QA_ESTIMATION_TOTAL"] = fmt_num(total_qa, 1)
    ctx["PERSON_TOTAL_QA_ESTIMATION_AVG"] = fmt_num(total_qa / total_tasks if total_tasks else None, 2)
    ctx["PERSON_TOTAL_LINKED_TASKS"] = fmt_int(total_linked)
    ctx["PERSON_TOTAL_MR_WITH_JIRA_KEY"] = fmt_int(total_with_key)
    ctx["PERSON_TOTAL_MR_PER_TASK"] = fmt_num(total_with_key / total_linked if total_linked else None, 2)
    ctx["PERSON_TOTAL_WARNINGS"] = fmt_int(total_warnings)

    ctx["PERSON_STATE_LEGEND"] = (
        '<div class="state-legend">'
        '<span class="legend-note">Пустая ячейка означает четыре разные вещи. Два последних случая выглядят '
        "одинаково — различие подсказывает всплывающая подсказка ячейки.</span>"
        '<span><span class="cell-nodata num">0 <span class="warn-badge">DIV0</span></span> — '
        "<b>знаменатель равен нулю.</b> Значение действительно 0, но делить было не на что.</span>"
        '<span><span class="cell-unavail num">&mdash; <span class="badge-na">нет данных</span></span> — '
        "<b>источник не вернул статистику.</b> Это не ноль: показателя нет вовсе.</span>"
        '<span><span class="cell-nosample num">&mdash;</span> — '
        "<b>пустая выборка.</b> Среднее или медиана считать не из чего.</span>"
        '<span><span class="cell-null num">&mdash;</span> — '
        '<b>null по контракту.</b> <span class="mono">mr_per_task</span> при <span class="mono">linked_tasks = 0</span>.</span>'
        '<span><span class="denom" style="margin-left:0">/ 12</span> — '
        '<b>размер выборки.</b> Сколько MR попало в среднее (<span class="mono">mr_diff_size_available_count</span>).</span>'
        "</div>"
    )

    ctx["_PEOPLE"] = people  # stashed for the render_html() loop pass
    return "available"


def build_person_card(inner: str, person: dict) -> str:
    warnings = person.get("warnings") or []
    fetch_error_badges = _mr_fetch_error_badges(person)
    warn_html = (
        '<div class="row-warnings">'
        + "".join(f'<span class="wbadge">{esc(w)}</span>' for w in warnings)
        + fetch_error_badges
        + "</div>"
        if warnings or fetch_error_badges
        else '<span class="pill role">ok</span>'
    )
    merge_status = "none" if person["mr_count"] == 0 else _status_band(person["mr_merge_rate_pct"], 85, 65, higher_is_better=True)
    merge_val = EM_DASH if person["mr_count"] == 0 else f'{fmt_num(person["mr_merge_rate_pct"], 2)}<span class="u">%</span>'
    merge_extra = '<span class="warn-badge">WARN_DIVISION_BY_ZERO</span>' if person["mr_count"] == 0 else ""
    stat_merge = (
        f'<div class="stat" data-status="{merge_status}"><div class="stat-label">доля смерженных</div>'
        f'<div class="stat-value">{merge_val}</div>{f"<div style=margin-top:5px>{merge_extra}</div>" if merge_extra else ""}</div>'
    )

    cycle_val = person.get("mr_cycle_time_median_hours")
    cycle_status = "none" if cycle_val is None else ""
    cycle_display = f'{fmt_num(cycle_val, 1)}<span class="u">ч</span>' if cycle_val is not None else EM_DASH
    stat_cycle = f'<div class="stat" data-status="{cycle_status}"><div class="stat-label">медиана цикла MR</div><div class="stat-value">{cycle_display}</div></div>'

    stat_tasks = f'<div class="stat"><div class="stat-label">задач закрыто</div><div class="stat-value">{fmt_int(person["tasks_done"])}</div></div>'

    # m8 — rework_share and pipeline_success_rate reach no token on the
    # person table (its column budget is already tuned for print fit), but
    # the card has room. No defined threshold exists for either (DESIGN-
    # NOTES §12.7), so neither gets a good/warn/bad band — same "none only
    # when null" treatment as MR cycle med above.
    rework_share = person.get("rework_share")
    rework_status = "none" if rework_share is None else ""
    rework_display = f'{fmt_num(rework_share * 100, 1)}<span class="u">%</span>' if rework_share is not None else EM_DASH
    stat_rework = f'<div class="stat" data-status="{rework_status}"><div class="stat-label">доля переработок</div><div class="stat-value">{rework_display}</div></div>'

    # Three states, not two (WARN_PIPELINE_SUCCESS_UNAVAILABLE): "not
    # collected" (fetch_pipeline_user=False this run) must read as "нет
    # данных", same as the MR diff-stats gap above — never the same bare
    # dash as "collected, genuinely zero for this person" / "no GitLab
    # pipeline data at all", which keep the plain none-state they already had.
    pipeline_rate = person.get("pipeline_success_rate")
    pipeline_unavailable = "WARN_PIPELINE_SUCCESS_UNAVAILABLE" in (person.get("warnings") or [])
    pipeline_status = "none" if pipeline_rate is None or pipeline_unavailable else ""
    pipeline_display = f'{fmt_num(pipeline_rate * 100, 1)}<span class="u">%</span>' if pipeline_rate is not None else EM_DASH
    pipeline_extra = '<span class="badge-na">нет данных</span>' if pipeline_unavailable else ""
    stat_pipeline = (
        f'<div class="stat" data-status="{pipeline_status}"><div class="stat-label">успешность пайплайнов</div>'
        f'<div class="stat-value">{pipeline_display}</div>{f"<div style=margin-top:5px>{pipeline_extra}</div>" if pipeline_extra else ""}</div>'
    )

    sprints = person.get("sprints") or []
    bars_svg = build_person_bar_svg(sprints)

    inner_after_bars = extract_inner(inner, "LOOP_PERSON_SPRINT_ROWS_START", "LOOP_PERSON_SPRINT_ROWS_END")
    sprint_rows_html = "".join(build_person_sprint_row_html(inner_after_bars, s) for s in sprints)
    inner = replace_block(inner, "LOOP_PERSON_SPRINT_ROWS_START", "LOOP_PERSON_SPRINT_ROWS_END", sprint_rows_html)

    totals = {
        "PERSON_SPRINT_TOTAL_MR_COUNT": fmt_int(sum((s.get("mr_count") or 0) for s in sprints)),
        "PERSON_SPRINT_TOTAL_TASKS_DONE": fmt_int(sum((s.get("throughput") or 0) for s in sprints)),
        "PERSON_SPRINT_TOTAL_STORY_POINTS_TOTAL": fmt_num(sum((s.get("story_points_total") or 0) for s in sprints), 1),
    }

    task_word = ru_plural(person["tasks_done"], "задача", "задачи", "задач")
    mapping = {
        "PERSON_CARD_USER": esc(person["user"]),
        "PERSON_CARD_SUB": esc(
            f'{person["mr_count"]} MR · {person["tasks_done"]} {task_word} · {person["mr_with_jira_key"]} MR с ключом Jira'
        ),
        "PERSON_CARD_WARNINGS": warn_html,
        "PERSON_CARD_STAT_MERGE_RATE": stat_merge,
        "PERSON_CARD_STAT_MR_CYCLE": stat_cycle,
        "PERSON_CARD_STAT_TASKS": stat_tasks,
        "PERSON_CARD_STAT_REWORK_SHARE": stat_rework,
        "PERSON_CARD_STAT_PIPELINE_SUCCESS": stat_pipeline,
        "PERSON_CARD_CHART_BARS": bars_svg,
        "PERSON_SPRINT_TOTAL_LABEL": "Итого",
    }
    mapping.update(totals)
    return sub_tokens(inner, mapping)


def build_person_bar_svg(sprints: list) -> str:
    values = [s.get("mr_count") or 0 for s in sprints]
    if not values:
        aria = "MR по спринтам: нет данных."
        return f'<svg class="person-chart" viewBox="0 0 240 60" role="img" aria-label="{esc(aria)}"></svg>'
    top = max(max(values), 1)
    n = len(values)
    spacing = 232.0 / n
    bar_w = spacing * 0.65
    bars = []
    for i, (v, s) in enumerate(zip(values, sprints)):
        x = 4 + i * spacing + (spacing - bar_w) / 2
        h = 0 if top == 0 else (v / top) * 40.0
        y = 48 - h
        cls = "c-bar-zero" if v == 0 else "c-bar"
        hh = max(h, 2)
        yy = 48 - hh
        title = f'{esc(s.get("sprint",""))} — {v}'
        bars.append(f'<rect class="{cls}" x="{x:.1f}" y="{yy:.1f}" width="{bar_w:.1f}" height="{hh:.1f}" rx="2"><title>{title}</title></rect>')
    aria = "MR по спринтам: " + ", ".join(str(v) for v in values)
    return (
        f'<svg class="person-chart" viewBox="0 0 240 60" preserveAspectRatio="xMinYMid meet" role="img" aria-label="{esc(aria)}">'
        '<line class="c-axis" x1="4" y1="48" x2="236" y2="48"/>'
        f'<g class="c-bar">{"".join(bars)}</g></svg>'
    )


def build_person_sprint_row_html(inner: str, sprint_row: dict) -> str:
    # personal_sprint_breakdown() has no merged-count and no median — only
    # mr_count (all MRs in the window) and an AVERAGE cycle time (M5).
    cycle = sprint_row.get("avg_mr_cycle_hours")
    if cycle is None:
        cycle_html = EM_DASH
        attrs = ' class="cell-nosample" title="пустая выборка: ни одного MR"'
    else:
        cycle_html = fmt_num(cycle, 1)
        attrs = ""
    mapping = {
        "PERSON_SPRINT_NAME": esc(sprint_row.get("sprint", "")),
        "PERSON_SPRINT_MR_COUNT": fmt_int(sprint_row.get("mr_count", 0)),
        "PERSON_SPRINT_TASKS_DONE": fmt_int(sprint_row.get("throughput", 0)),
        "PERSON_SPRINT_STORY_POINTS_TOTAL": fmt_num(sprint_row.get("story_points_total"), 1),
        "PERSON_SPRINT_MR_CYCLE_TIME_AVG_HOURS": cycle_html,
        "PERSON_SPRINT_MR_CYCLE_TIME_AVG_HOURS_ATTRS": attrs,
    }
    return sub_tokens(inner, mapping)


# ==========================================================================
# Footer
# ==========================================================================


def build_footer(report: dict, ctx: dict) -> None:
    params = report.get("params") or {}
    board = report.get("board") or {}
    forecast = report.get("forecast")
    gitlab_issues = report.get("gitlab_fetch_issues") or {"skipped_projects": [], "mr_fetch_errors": []}
    eng = report.get("engineering") or {}
    gitlab_configured = eng.get("available") or (report.get("personal") or {}).get("available")

    ctx["FOOTER_TOOL_NAME"] = "team-metrics"
    ctx["FOOTER_TOOL_VERSION"] = esc(f"calc_schema_version {report.get('schema_version', '?')}")
    disclaimers = []
    if gitlab_issues.get("skipped_projects"):
        # Each entry is {"project", "code", "message"} (gitlab_client.py) —
        # a skipped project must read as "skipped", never as the dict repr
        # and never as silent zero activity.
        disclaimers.append(
            "Пропущены проекты GitLab (не путать с нулевой активностью): "
            + ", ".join(f"{esc(p.get('project'))} ({esc(p.get('code'))})" for p in gitlab_issues["skipped_projects"])
        )
    if gitlab_issues.get("mr_fetch_errors"):
        # {"project","author","state","code","message"} per entry — spelled
        # out so a person whose MR fetch failed isn't mistaken for a
        # genuinely low mr_count from a bare count alone.
        details = "; ".join(
            f"{esc(e.get('project'))}/{esc(e.get('author'))} ({esc(e.get('state'))}): {esc(e.get('code'))}"
            for e in gitlab_issues["mr_fetch_errors"]
        )
        disclaimers.append(
            f"Ошибки при выгрузке merge request'ов ({len(gitlab_issues['mr_fetch_errors'])}): {details}."
        )
    footer_note = "Статический файл — ни одного внешнего запроса, работает из file:// в закрытой сети."
    if disclaimers:
        footer_note += " " + " ".join(disclaimers)
    ctx["FOOTER_NOTE"] = footer_note

    ctx["PARAM_HOST"] = "скрыт (переменная окружения JIRA_BASE_URL)"
    ctx["PARAM_AUTH"] = "PAT (скрыт)"
    ctx["PARAM_CONNECTION_ID"] = EM_DASH
    ctx["PARAM_BOARD"] = esc(f"{board.get('name', EM_DASH)} (board_id {board.get('id', EM_DASH)})")
    ctx["PARAM_SPRINT_IDS"] = esc(json.dumps(params.get("sprint_ids") or []))
    ctx["PARAM_JQL"] = EM_DASH
    n_base = len(base_entries(report))
    ctx["PARAM_HISTORY_SPRINT_COUNT"] = esc(f"{params.get('history_sprint_count', EM_DASH)} (найдено {n_base})")
    ctx["PARAM_FORCE_REFRESH"] = EM_DASH
    ctx["PARAM_STORY_POINTS_FIELD"] = EM_DASH
    ctx["PARAM_QA_FIELD"] = EM_DASH
    ctx["PARAM_ROLE_FIELD"] = EM_DASH
    ctx["PARAM_CANCELLED_STATUSES"] = EM_DASH
    ctx["PARAM_STATUS_MAP"] = EM_DASH
    ctx["PARAM_WORKING_DAYS"] = "пн–пт, UTC, без календаря праздников"
    ctx["PARAM_TIMEZONE"] = "UTC (жёстко, параметра нет)"
    iterations_text = fmt_int(params.get("iterations"))
    seed = params.get("seed")
    ctx["PARAM_MC_ITERATIONS"] = esc(f"{iterations_text}, seed {seed}") if seed is not None else iterations_text
    if forecast is not None:
        requested = params.get("target_items_requested")
        resolved = params.get("target_items_resolved")
        note = "задано явно" if requested is not None else "авто, остаток активного спринта"
        ctx["PARAM_MC_TARGET_ITEMS"] = esc(f"{resolved} ({note})")
    else:
        ctx["PARAM_MC_TARGET_ITEMS"] = EM_DASH
    ctx["PARAM_ENG_SOURCE"] = EM_DASH
    ctx["PARAM_GITLAB_HOST"] = "скрыт (переменная окружения GITLAB_URL)" if gitlab_configured else EM_DASH

    eng_data = eng.get("data") or {}
    projects = set()
    for section in ("pipelines", "deployments", "coverage"):
        for r in (eng_data.get(section) or {}).get("per_project", []):
            projects.add(r["project"])
    ctx["PARAM_GITLAB_PROJECTS"] = esc(", ".join(sorted(projects))) if projects else EM_DASH

    window = eng_data.get("window_applied") or {}
    if window and not (window.get("deployments", True) and window.get("coverage", True)):
        ctx["PARAM_ENG_UNFILTERED"] = "без фильтра по датам (весь проект) для deployments/coverage"
    else:
        ctx["PARAM_ENG_UNFILTERED"] = EM_DASH
    ctx["PARAM_JOB_ID"] = EM_DASH
    ctx["PARAM_GENERATED_AT"] = esc(params.get("generated_at") or EM_DASH)
    ctx["PARAM_RUN_DURATION"] = EM_DASH

    gitlab_request_count = params.get("gitlab_request_count")
    if gitlab_request_count is None:
        ctx["PARAM_GITLAB_REQUEST_COUNT"] = EM_DASH
    else:
        request_word = ru_plural(gitlab_request_count, "запрос", "запроса", "запросов")
        ctx["PARAM_GITLAB_REQUEST_COUNT"] = esc(f"{fmt_int(gitlab_request_count)} {request_word} к GitLab")


# ==========================================================================
# Closing recommendations — every item derived from a measured value
# ==========================================================================

# (severity, css_class) — higher severity sorts first; capped to _RECO_MAX.
_RECO_MAX = 7


def _reco_item(severity: int, css_class: str, metric_line: str, signal_line: str, action_line: str) -> tuple:
    return (severity, css_class, metric_line, signal_line, action_line)


def build_recommendations(report: dict, ctx: dict) -> None:
    """Every recommendation names the metric and the value that triggered
    it — no generic advice. Ranked by severity, capped at _RECO_MAX. Per-
    person triggers (rework_share) are reported as a team-level count, never
    naming an individual — this block is read by managers about named
    people and must not become a stick."""
    idx, primary = primary_target(report)
    m = primary["payload"]["metrics"]
    sprints = report["sprints"]
    flags = div0_flags(sprints, idx)
    items: list = []

    # -- forecast unavailable (real module constant: needs >=10 non-zero
    #    daily throughput points, not our heuristic) --------------------
    if report.get("forecast") is None:
        code = report.get("forecast_error")
        reason = _FORECAST_ERROR_TEXT.get(code, code or "неизвестная причина")
        items.append(_reco_item(
            90, "bad",
            "Прогноз не построен",
            f"{reason} Порог модуля (ERR_FORECAST_NOT_ENOUGH_DATA) — не наша эвристика.",
            "Нужно больше закрытых спринтов в истории с ненулевой дневной пропускной способностью; "
            "пока их меньше 10, честнее явно писать, что прогноза нет, чем строить его на скудных данных.",
        ))

    # -- throughput CV > 50% (real module constant) --------------------
    forecast = report.get("forecast")
    if forecast is not None and "WARN_THROUGHPUT_UNSTABLE" in (forecast.get("warnings") or []):
        cv = forecast["throughput_cv_pct"]
        items.append(_reco_item(
            85, "bad",
            f"Недельный коэффициент вариации пропускной способности = {fmt_num(cv, 1)}%",
            "Превышает порог модуля 50% (WARN_THROUGHPUT_UNSTABLE, не наша эвристика) — поток нестабилен от недели к неделе.",
            "Не приводить p50 как единственное число во внешних обязательствах; смотреть на весь диапазон "
            "p50–p95 и разбираться, что вызывает скачки пропускной способности.",
        ))

    # -- performance_pct critical (our heuristic: <80%) -----------------
    if not flags["performance_pct"] and _status_band(m["performance_pct"], 95, 80, higher_is_better=True) == "bad":
        items.append(_reco_item(
            80, "bad",
            f"Выполнение обязательств (performance_pct) = {fmt_num(m['performance_pct'], 2)}%",
            "Критично: ниже 80% (эвристика этого отчёта) — в спринт взяли больше, чем смогли поставить.",
            f"На следующем планировании ориентироваться на SMA5 ({fmt_num(m['velocity_sma5_sp'], 1)} SP), "
            "а не на оптимистичную оценку объёма.",
        ))

    # -- load_pct critical, either direction (our heuristic) ------------
    if not flags["load_pct"] and _load_status(m["load_pct"]) == "bad":
        direction = "перегружена" if m["load_pct"] > 100.0 else "недогружена"
        items.append(_reco_item(
            75, "bad",
            f"Загрузка (load_pct) = {fmt_num(m['load_pct'], 2)}%",
            f"Критично: вне диапазона 85–110% (эвристика этого отчёта) — команда {direction} "
            "относительно своей же скользящей средней.",
            "Пересмотреть объём при планировании следующего спринта относительно фактической SMA5, "
            "а не повторять цифру этого спринта.",
        ))

    # -- scope_change_pct critical (our heuristic: >25%) -----------------
    if not flags["scope_change_pct"] and _status_band(m["scope_change_pct"], 10, 25, higher_is_better=False) == "bad":
        items.append(_reco_item(
            70, "bad",
            f"Изменение объёма (scope_change_pct) = {fmt_num(m['scope_change_pct'], 2)}%",
            "Критично: выше 25% (эвристика этого отчёта) — скоуп заметно менялся уже после старта спринта.",
            "Ужесточить приём изменений в спринт после старта, либо завести отдельный процесс триажа "
            "для срочных добавлений вместо добавления напрямую в текущий спринт.",
        ))

    # -- pipeline success rate / coverage (engineering) ------------------
    eng = report.get("engineering") or {}
    if eng.get("available"):
        pipe = eng["data"]["pipelines"]
        if pipe["count"] > 0 and _status_band(pipe["success_rate_pct"], 90, 80, higher_is_better=True) == "bad":
            items.append(_reco_item(
                65, "bad",
                f"Успешность пайплайнов (pipelines.success_rate_pct) = {fmt_num(pipe['success_rate_pct'], 2)}%",
                "Критично: ниже 80% (эвристика этого отчёта) — часть времени разработки уходит на нестабильный CI.",
                "Разобрать упавшие пайплайны на систематические причины (флаки тестов, инфраструктура), "
                "а не только на конкретный коммит, который их вызвал.",
            ))

        cov = eng["data"]["coverage"]
        cov_val = cov.get("coverage_avg_pct")
        if cov_val is None:
            items.append(_reco_item(
                50, "warn",
                "Покрытие тестами (coverage.coverage_avg_pct) не измерено ни для одного проекта",
                "sample_count = 0 — это отсутствие данных, а не факт хорошего покрытия.",
                "Подключить отчёт о покрытии хотя бы для основных репозиториев, прежде чем делать выводы о качестве тестов.",
            ))
        elif _status_band(cov_val, 80, 65, higher_is_better=True) == "bad":
            items.append(_reco_item(
                50, "warn",
                f"Покрытие тестами (coverage.coverage_avg_pct) = {fmt_num(cov_val, 2)}%",
                "Критично: ниже 65% (эвристика этого отчёта).",
                "Закладывать время на тесты в бэклог наравне с новым функционалом, а не по остаточному принципу.",
            ))

    # -- scope_removed_items high relative to committed (our heuristic) --
    committed_items = m["committed_items"]
    removed_items = m["scope_removed_items"]
    if committed_items > 0 and (removed_items / committed_items) > 0.15:
        pct = removed_items / committed_items * 100.0
        items.append(_reco_item(
            55, "warn",
            f"Убрано из спринта (scope_removed_items) = {fmt_int(removed_items)} из {fmt_int(committed_items)} взятых ({fmt_num(pct, 0)}%)",
            "Выше 15% (наша эвристика, порога в модуле нет) — часть того, что взяли на планировании, снимается по ходу спринта.",
            "На ретро разобрать, почему эти задачи не подошли уже после старта — недооценка при "
            "планировании или изменившиеся приоритеты, а не переносить решение на следующий спринт молча.",
        ))

    # -- rework_share high (per-person trigger, reported team-level only) -
    personal = report.get("personal") or {}
    if personal.get("available"):
        people = personal["data"]["people"]
        high_rework = [p for p in people if p.get("rework_share") is not None and p["rework_share"] > 0.3]
        if high_rework and people:
            worst = max(p["rework_share"] for p in high_rework)
            items.append(_reco_item(
                60, "warn",
                f"Доля переработок (rework_share) выше 30% у {len(high_rework)} из {len(people)} участников "
                f"(максимум {fmt_num(worst * 100.0, 0)}%)",
                "Работа заметно возвращается в разработку после ревью или QA — приведено как счёт по команде, "
                "не как оценка конкретных людей.",
                "Разобрать конкретные случаи на командном ретро, а не в этом отчёте; проверить, не связано "
                "ли с недостаточной детализацией задач на момент взятия в спринт.",
            ))

    items.sort(key=lambda item: -item[0])
    items = items[:_RECO_MAX]

    if not items:
        ctx["RECOMMENDATIONS_BLOCK"] = (
            '<div class="reco-none">По этому прогону явных проблем не видно — измеренные значения '
            "не вышли за пороги, описанные выше по тексту отчёта.</div>"
        )
        return

    html_items = []
    for _severity, css_class, metric_line, signal_line, action_line in items:
        html_items.append(
            f'<li class="reco-item reco-{css_class}">'
            f'<div class="reco-metric">{esc(metric_line)}</div>'
            f'<div class="reco-signal">{esc(signal_line)}</div>'
            f'<div class="reco-action"><b>Действие:</b> {esc(action_line)}</div>'
            "</li>"
        )
    ctx["RECOMMENDATIONS_BLOCK"] = '<ol class="reco-list">' + "".join(html_items) + "</ol>"


# ==========================================================================
# Top level
# ==========================================================================


def _load_template(template_path: Optional[Path] = None) -> str:
    path = template_path or TEMPLATE_PATH
    return Path(path).read_text(encoding="utf-8")


def render_html(report: dict, *, template_path: Optional[Path] = None) -> str:
    """Renders one self-contained HTML report from a `build_combined_report()`
    (or `build_report()`) dict. Pure function: no I/O beyond reading the
    template file."""
    text = strip_scalar_marker_comments(_load_template(template_path))
    ctx: dict = {}

    # ---- person tab: available vs unavailable, then the two real loops ----
    person_state = build_person_tab(report, ctx)
    if person_state == "unavailable":
        text = replace_block(
            text, "PERSON_SECTIONS_START", "PERSON_SECTIONS_END",
            '<section class="section" id="sec-person-unavailable"><div class="section-body">'
            '<p class="section-desc" style="text-align:left">Раздел недоступен для этого запуска — см. баннер выше.</p>'
            "</div></section>",
        )
    else:
        people = ctx.pop("_PEOPLE")
        text = render_loop(text, "PERSON_ROW_START", "PERSON_ROW_END", people, build_person_row_html)
        text = render_loop(text, "PERSON_CARD_START", "PERSON_CARD_END", people, build_person_card)
        text = blank_block_markers(text, ("PERSON_SECTIONS_START", "PERSON_SECTIONS_END"))
    ctx.pop("_PEOPLE", None)

    # ---- engineering tab ----
    eng_state = build_eng_tab(report, ctx)
    if eng_state == "unavailable":
        text = replace_block(
            text, "ENG_SECTIONS_START", "ENG_SECTIONS_END",
            '<section class="section" id="sec-eng-unavailable"><div class="section-body">'
            '<p class="section-desc" style="text-align:left">Раздел недоступен для этого запуска — см. баннер выше.</p>'
            "</div></section>",
        )
    else:
        eng_rows = ctx.pop("_ENG_PROJECT_ROWS")
        text = render_loop(text, "ENG_PROJECT_ROW_START", "ENG_PROJECT_ROW_END", eng_rows, build_eng_project_row_html)
        text = blank_block_markers(text, ("LOOP_ENG_TILES_START", "LOOP_ENG_TILES_END"), ("ENG_SECTIONS_START", "ENG_SECTIONS_END"))

    # ---- board warnings ----
    text = replace_block(text, "LOOP_BOARD_WARNINGS_START", "LOOP_BOARD_WARNINGS_END", build_warnings_block(report))

    # ---- heatmap (tab 1) ----
    heatmap_rows = build_heatmap_section(report, ctx)
    heatmap_days = (report.get("heatmaps") or [{"days": []}])[-1]["days"]
    text = render_loop(text, "LOOP_HEATMAP_ROWS_START", "LOOP_HEATMAP_ROWS_END", heatmap_rows,
                        lambda inner, row: build_heatmap_row_html(inner, row, heatmap_days))

    # ---- export Table A ----
    table_a_rows = build_table_a(report, ctx)
    text = render_loop(text, "LOOP_TABLE_HEATMAP_ROWS_START", "LOOP_TABLE_HEATMAP_ROWS_END", table_a_rows, build_table_a_row_html)

    # ---- export Table B ----
    board_rows = build_table_b(report, ctx)
    sprints = report["sprints"]
    text = render_loop(text, "LOOP_BOARD_ROWS_START", "LOOP_BOARD_ROWS_END", board_rows,
                        lambda inner, entry: build_board_row_html(inner, entry, sprints))

    # ---- forecast ----
    forecast_state = build_forecast(report, ctx)
    if forecast_state == "unavailable":
        text = replace_block(text, "FORECAST_GRID_START", "FORECAST_GRID_END", "")
        text = blank_block_markers(text, ("FORECAST_UNAVAILABLE_START", "FORECAST_UNAVAILABLE_END"))
    else:
        text = replace_block(text, "FORECAST_UNAVAILABLE_START", "FORECAST_UNAVAILABLE_END", "")
        text = blank_block_markers(text, ("FORECAST_GRID_START", "FORECAST_GRID_END"), ("LOOP_FORECAST_PERCENTILES_START", "LOOP_FORECAST_PERCENTILES_END"))

    # ---- header, KPI tiles, metric groups, burndown, footer ----
    build_header_ctx(report, ctx)
    build_kpi_tiles(report, ctx)
    build_metric_groups(report, ctx)
    _, burndown_svg, burndown_has_weekend = build_burndown_svg((report.get("burndowns") or [{"points": []}])[-1]["points"])
    ctx["BURNDOWN_ARIA_SUMMARY"] = ""
    ctx["BURNDOWN_SVG"] = burndown_svg
    ctx["BURNDOWN_WEEKEND_LEGEND"] = (
        '<span class="legend-item">'
        '<svg class="legend-line" viewBox="0 0 26 10" aria-hidden="true" focusable="false">'
        '<rect class="lg-weekend" x="0.5" y="0.5" width="25" height="9"/>'
        "</svg>"
        "<b>выходные</b> &mdash; статус переносится с пятницы"
        "</span>"
        if burndown_has_weekend
        else ""
    )
    build_footer(report, ctx)
    build_recommendations(report, ctx)

    text = blank_block_markers(text, ("LOOP_KPI_TILES_START", "LOOP_KPI_TILES_END"), ("LOOP_FOOTER_PARAMS_START", "LOOP_FOOTER_PARAMS_END"))
    text = blank_block_markers(text, ("LOOP_HEATMAP_COLS_START", "LOOP_HEATMAP_COLS_END"), ("LOOP_TABLE_HEATMAP_COLS_START", "LOOP_TABLE_HEATMAP_COLS_END"))

    return finalize(text, ctx)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="render_html", description="Render a report_data JSON dict to a self-contained HTML report")
    parser.add_argument("report_json", nargs="?", default=None, help="Path to a report_data JSON file (default: stdin)")
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
