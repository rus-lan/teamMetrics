"""Writes collected data and reports to disk under one output directory.

Security: every `filename` argument accepted here is a literal defined at
the call site (all 18 CSV names and 7 raw names are hardcoded strings, never
built from Jira/GitLab data such as a project or sprint name) — but this
module rejects a path-separator or `..` anywhere in `filename` regardless,
with a `ValueError`, before touching the filesystem. That guarantee covers
only the `filename` argument of the functions below; the `out_dir` argument
is trusted as given by the caller (e.g. cli.py's `--out-dir`/`--out`/
`--json-out` may legitimately point outside the run's own output folder —
that is a caller decision, not something this module second-guesses).

Every writer here also refuses to write through a path that is itself a
symlink (`ValueError`) — a repeated `run` into an existing `out/` overwrites
what IT wrote there, never silently follows a symlink planted at
report.html/report.json/a CSV name into an unrelated target file.

`write_csv` additionally guards every cell against formula injection: a
string cell whose first character is `=`, `+`, `-`, `@`, a tab, or `\\r` gets
a leading `'` prefixed (mirrors the spreadsheet-formula guard rule) before
`csv.DictWriter` ever sees it — the single place every one of the 18 CSVs
passes through, so no caller needs its own copy of this guard.

Secrets: callers must NEVER pass a token, a PAT, or an Authorization/
PRIVATE-TOKEN header value into `write_raw` (or any other writer here) —
this module is not a secrets vault. `write_raw` additionally runs `scrub()`
on its input as defense in depth, dropping any dict key that merely looks
secret-shaped, but that is a safety net, not a license to hand it real
credentials.
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Union

PathLike = Union[str, Path]

from . import logging_setup

log = logging_setup.get_logger("out_writer")

# Substrings matched against a normalized (lowercased, non-alphanumeric
# stripped) key — mirrors config.py's _is_token_like_key/_normalize_key
# approach so "Authorization", "PRIVATE-TOKEN", "api_key" and similar are all
# caught regardless of separator/casing style. "privatetoken" normalizes
# down to something already containing "token", so it is listed here only to
# document the intent explicitly, not because it adds coverage on its own.
_SECRET_LIKE_SUBSTRINGS = ("token", "password", "secret", "authorization", "apikey", "credential", "privatetoken")


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.strip().lower())


def _is_secret_like_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(sub in normalized for sub in _SECRET_LIKE_SUBSTRINGS)


def scrub(obj: Any) -> Any:
    """Recursively drops dict keys that look secret-shaped, replacing the
    value with "***". Recurses into nested dicts and lists; every other
    value type is returned unchanged."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and _is_secret_like_key(key):
                out[key] = "***"
            else:
                out[key] = scrub(value)
        return out
    if isinstance(obj, list):
        return [scrub(item) for item in obj]
    return obj


def check_safe_filename(filename: str) -> None:
    """Raises `ValueError` unless `filename` is a plain, single-component
    file name — no path separator, no `..`, never empty. Every writer below
    calls this on its `filename` argument before touching the filesystem;
    cli.py also calls it directly to validate a bare `--out`/`--json-out`
    value up front, before any network work begins."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename or os.path.basename(filename) != filename:
        raise ValueError(f"unsafe filename (must be a plain file name): {filename!r}")


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {path}")


# Formula-injection guard: a spreadsheet (Excel/LibreOffice/Sheets) reads a
# cell starting with any of these as a formula on open, regardless of how
# properly the surrounding CSV is quoted. Prefixing a leading single quote
# defuses it while leaving the visible text unchanged.
_FORMULA_TRIGGER_CHARS = "=+-@\t\r"


def _guard_formula_cell(value: Any) -> Any:
    if isinstance(value, str) and value and value[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value


def ensure_out_dir(path: PathLike) -> Path:
    p = Path(path)
    os.makedirs(p, exist_ok=True)
    return p


def write_csv(out_dir: PathLike, filename: str, rows: list[dict]) -> Optional[Path]:
    """Writes `rows` (a list of dicts) as UTF-8 CSV. Column order is the
    first row's keys, then any key first seen in a later row appended in
    first-encounter order — never just the first row's keys, since a later
    row can carry a field the first one didn't. A key missing from a given
    row renders as an empty cell. Every cell value passes through
    `_guard_formula_cell` first (module docstring).

    `rows` empty -> logs a warning and returns None; never writes a
    headerless file."""
    check_safe_filename(filename)
    if not rows:
        log.warning("Нет данных для %s — файл не создан", filename)
        return None

    field_order: list = []
    seen: set = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                field_order.append(key)

    out_path = ensure_out_dir(out_dir) / filename
    _reject_symlink(out_path)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=field_order)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _guard_formula_cell(value) for key, value in row.items()})
    log.info("Создан %s", out_path)
    return out_path


def write_json(out_dir: PathLike, filename: str, obj: Any) -> Path:
    """Writes `obj` as indented, non-ASCII-preserving JSON, key order exactly
    as `obj` iterates -- never alphabetized. `report.json` (report_data.py)
    is built once, deterministically, from the same inputs every run, so
    preserving its own key order (rather than re-sorting it) is what makes
    `run` and a later `report` on that same file agree byte-for-byte: at
    least one render (the tab 09 roles table) reads `report["labels"]["roles"]`
    in dict order, and a `sort_keys=True` write would silently reorder it
    between "rendered straight after `run`" and "rendered after a round trip
    through the written file"."""
    check_safe_filename(filename)
    out_path = ensure_out_dir(out_dir) / filename
    _reject_symlink(out_path)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log.info("Создан %s", out_path)
    return out_path


def write_text(out_dir: PathLike, filename: str, text: str) -> Path:
    check_safe_filename(filename)
    out_path = ensure_out_dir(out_dir) / filename
    _reject_symlink(out_path)
    out_path.write_text(text, encoding="utf-8")
    log.info("Создан %s", out_path)
    return out_path


def write_raw(out_dir: PathLike, filename: str, obj: Any) -> Path:
    """Same as write_json, but under out_dir/raw/ and with `obj` passed
    through scrub() first — the landing spot for a raw upstream API payload
    a caller wants kept for debugging without risking a leaked secret."""
    return write_json(Path(out_dir) / "raw", filename, scrub(obj))
