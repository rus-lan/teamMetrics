---
name: jira-metrics-report
version: 1.1.0
description: |
  Builds a self-contained HTML sprint/team-metrics report from Jira (and,
  optionally, GitLab) data: a "Команда" tab with sprint KPIs, burndown,
  heatmap and a Monte-Carlo forecast; a "Персональные" tab with per-person
  GitLab+Jira metrics; and an "Инженерия" tab with team-level GitLab
  pipelines/deployments/coverage. One stdlib-only Python command surface
  (`scripts/jira-metrics init/check/run/report`), zero third-party
  dependencies, works fully offline once the HTML file exists.

  TRIGGER when the user asks for "отчёт по метрикам команды", "отчёт по
  спринту", "метрики спринта", "командные метрики", "персональные метрики
  разработчиков", "спринт-отчёт", "jira metrics report", "sprint report",
  "team metrics report", "engineering metrics report", "burndown report",
  or invokes /jira-metrics-report.
user-invocable: true
compatibility: claude-code opencode
allowed-tools:
  - Bash
  - Read
  - Write
---

# jira-metrics-report

One stdlib-only Python 3.9+ command surface, no `pip install` anywhere:

```sh
<skill-dir>/scripts/jira-metrics {init|check|run|report} ...
```

Four subcommands cover the whole workflow — `init` writes the config file, `check` verifies
Jira/GitLab reachability without building anything, `run` fetches + computes + writes both the
JSON data file and the HTML report, `report` re-renders HTML from an already-fetched JSON file
with **zero** network calls. See "Invocation" below for the exact sequence to run.

Under the hood this dispatches to the same two engines as before (still directly usable, see
"Direct script invocation" further down):

1. **`report_data.py`** — fetches Jira (and optionally GitLab) data, computes every metric, and
   emits one JSON-serializable dict (`build_combined_report()`).
2. **`render_html.py`** — reads that dict and substitutes it into `templates/report.html`,
   producing one self-contained `.html` file: no `<script src>`, no remote `<link>`, no
   `@import`, no live network calls at view time. Opens from `file://` forever, offline.

This file covers what the model needs to run the tool correctly. For the full JSON config
schema, the metric definitions, and where the numbers come from, read
[`README.md`](./README.md) (`<skill-dir>/README.md`, installed at
`~/.claude/skills/jira-metrics-report/README.md`).

## Requirements

- Python 3.9+, standard library only. No third-party packages, no network access needed to
  *view* the finished report (only to *generate* it, and only against Jira/GitLab).
- `JIRA_BASE_URL` and `JIRA_TOKEN` in the environment — **required**.
- `GITLAB_URL` and `GITLAB_TOKEN` in the environment — **optional**; without them tabs 2/3
  report themselves `"available": false` with a reason, tab 1 still renders normally.

## Bundled files

Resolve every path below relative to this skill (`<skill-dir>`); once installed that is
`~/.claude/skills/jira-metrics-report/`:

- `<skill-dir>/scripts/jira-metrics` — the command surface: `init`/`check`/`run`/`report`
  (executable, `python3 <skill-dir>/scripts/jira-metrics ...` also works without `chmod +x`)
- `<skill-dir>/scripts/jira_metrics/cli.py` — dispatcher implementation behind `jira-metrics`;
  also reachable as `python3 -m jira_metrics <command> ...` run from `<skill-dir>/scripts`
- `<skill-dir>/scripts/jira_metrics/report_data.py` — data/CLI entry point (`run`/`report_data.py`
  engine)
- `<skill-dir>/scripts/jira_metrics/render_html.py` — HTML renderer/CLI entry point (`report`/
  `render_html.py` engine)
- `<skill-dir>/scripts/jira_metrics/*.py` — the rest of the library (`jira_client.py`,
  `gitlab_client.py`, `metrics.py`, `forecast.py`, `personal_metrics.py`,
  `engineering_metrics.py`, `heatmap.py`, `burndown.py`, `model.py`, `config.py`)
- `<skill-dir>/templates/report.html` — the token-templated HTML shell `render_html.py` fills in
- `<skill-dir>/.jira-metrics.example.json` — copy to `.jira-metrics.json` and edit; see
  README.md for what each key does (JSON has no comments)
- `<skill-dir>/demo/report-demo.html` — a fixture-generated demo report (no network involved in
  producing it) that exercises all three tabs at once: several sprints, a person with missing
  GitLab diff stats, a skipped GitLab project, several DIV0 metrics, and a forecast that
  succeeds. Open it to see the real output shape before running against a live Jira/GitLab.

## Invocation

Run `<skill-dir>/scripts/jira-metrics` from the directory `.jira-metrics.json` should live in
(a real project's working directory, not `<skill-dir>` itself) — or pass `--config` explicitly
to `check`/`run`. Two situations, two short sequences:

**First-time setup for a project** (no `.jira-metrics.json` yet):

```sh
<skill-dir>/scripts/jira-metrics init
# -> writes ./.jira-metrics.json, prints exactly which env vars are still missing

export JIRA_BASE_URL="https://jira.example.com"
export JIRA_TOKEN="<Jira PAT>"
export GITLAB_URL="https://gitlab.example.com"    # optional — enables tabs 2/3
export GITLAB_TOKEN="<GitLab PAT>"                 # optional

# edit ./.jira-metrics.json: gitlab.projects, employees, story_points_field_id if needed

<skill-dir>/scripts/jira-metrics check --sprint-names "Sprint 42"
# -> PASS/FAIL per item: env vars, Jira/GitLab connectivity+auth, story-point field, sprint/board

<skill-dir>/scripts/jira-metrics run --sprint-names "Sprint 42" --history 5
# -> writes ./report.json AND ./report.html, prints both paths
```

**Re-rendering** an already-fetched `report.json` (no network, edit the JSON or just re-style):

```sh
<skill-dir>/scripts/jira-metrics report report.json -o report.html
```

`init`/`check` are one-time or occasional (re-run `check` any time credentials might have
changed); `run` is the normal day-to-day command once setup is done. Both `run` and `report`
also work through the pipe/stdin shape the direct scripts use — see "Direct script invocation"
below if you need the two engines separately (e.g. to inspect/edit the JSON between fetch and
render, which `run` doesn't give you — use `run`'s `--json-out` plus a separate `report` call
for that).

## CLI reference — `jira-metrics` dispatcher (the command an agent should run)

| Command | What it does | Key flags |
|---|---|---|
| `init [--force]` | Writes `.jira-metrics.json` in the current directory from the bundled example. Refuses to overwrite an existing file unless `--force` is passed. Never writes a token. Prints which env vars are still missing and the next command to run | `--force` |
| `check [--sprint-ids/--sprint-names] [--board-id] [--config] [--no-gitlab]` | Verifies the setup with **no report built**: env vars, config file, Jira reachability+auth, story-point field discovery, sprint/board resolution (only if given), GitLab reachability+auth, and — if `gitlab.projects` is configured — that each configured project actually resolves. Prints one `[PASS]`/`[FAIL]`/`[SKIP]` line per item, never a token; exits non-zero on any `[FAIL]` | same target flags as `run`, all optional here |
| `run <target> [flags] [--out <html>] [--json-out <json>]` | Full pipeline: fetch → compute → write **both** the JSON data file and the HTML report. Every `report_data.py` flag below works here too | `--out` (default `report.html`), `--json-out` (default `report.json`) |
| `report [json_path] [-o <html>] [--template <path>]` | Renders HTML from an existing JSON data file — **zero network calls**, no `JIRA_*`/`GITLAB_*` read at all. Rejects a JSON file whose `schema_version` this tool doesn't recognize, with a clear error, instead of rendering garbage | `-o/--out` (default stdout), `--template` |

`run`'s target/history/seed/etc. flags are identical to `report_data.py`'s (see the table right
below) — `run` is a strict superset, not a different flag set, except that its `--out` means the
**HTML** path and `--json-out` is the new flag for the JSON path (`report_data.py`'s own `--out`
still means the JSON path when you call it directly, unchanged).

## Direct script invocation (advanced/low-level — the two engines `run`/`report` wrap)

**`--out` means something different here than on `jira-metrics run`.** Below, `report_data.py
--out` writes the **JSON** data file (there is no HTML output from this script at all — that's
`render_html.py`'s job, via its own separate `-o/--out`). On the dispatcher, `run --out` writes
the **HTML** report instead, and `--json-out` is the JSON path. If you're used to one form,
double-check which `--out` you're holding before you run it — the wrong one silently overwrites
the wrong file.

Two-step pipeline, run from wherever `.jira-metrics.json` should be resolved from (or pass
`--config` explicitly):

```sh
JIRA_BASE_URL="https://jira.example.com" JIRA_TOKEN="<PAT>" \
GITLAB_URL="https://gitlab.example.com" GITLAB_TOKEN="<PAT>" \
python3 <skill-dir>/scripts/jira_metrics/report_data.py \
  --sprint-ids 4821 --history 5 --out /tmp/report.json   # <- JSON path, report_data.py's own --out

python3 <skill-dir>/scripts/jira_metrics/render_html.py /tmp/report.json -o report.html
```

Or in one pipe (`report_data.py --json` prints to stdout; `render_html.py` defaults to
reading stdin):

```sh
JIRA_BASE_URL=... JIRA_TOKEN=... python3 <skill-dir>/scripts/jira_metrics/report_data.py \
  --sprint-names "Sprint 42" --json | python3 <skill-dir>/scripts/jira_metrics/render_html.py -o report.html
```

## CLI reference — `report_data.py` (the real surface; read `config.py` before inventing a flag)

Target sprint, **mutually exclusive, exactly one required**:

| Flag | Meaning |
|---|---|
| `--sprint-ids <id[,id...]>` | Comma-separated Jira sprint ids (target sprints) |
| `--sprint-names <name[,name...]>` | Comma-separated sprint names. Exact match, case-insensitive, trimmed; tie-break on a duplicate name is the smallest sprint id |

Everything else is optional:

| Flag | Default | Meaning |
|---|---|---|
| `--board-id <int>` | none | Cross-checked against the resolved target sprints' board; a mismatch is a hard error, not a silent override |
| `--history <int>` | `0` → 5, clamp 20 | Previous **closed** sprints analyzed in addition to the targets (the board_table/velocity-trend history population, separate from the forecast's own 5-sprint population) |
| `--seed <int>` | `42` | Monte-Carlo bootstrap RNG seed — same seed, same run, same forecast numbers |
| `--target-items <int>` | resolved from the active sprint | Forecast target item count. Default is the board's active sprint's remaining items (`committed + added − removed − delivered`, clamped ≥ 0). If there is no active sprint and this flag is omitted, the forecast degrades gracefully — see Troubleshooting |
| `--iterations <int>` | `0` → 5000 | Monte-Carlo iterations |
| `--config <path>` | `./.jira-metrics.json` if it exists | Path to the JSON config file |
| `--json` | off | Print the full report dict as JSON to stdout |
| `--out <path>` | none | Write the JSON report to this file |
| `--no-gitlab` | off | Skip **both** GitLab-derived tabs even if `GITLAB_URL`/`GITLAB_TOKEN` are set |
| `--no-personal` | off | Skip only the personal-metrics tab; the engineering tab (team-level, not per-person) still runs as long as GitLab is configured |

`render_html.py`: positional `report_json` (path, default stdin), `-o/--out <path>` (default
stdout), `--template <path>` (override, default `templates/report.html` next to the module).

## Environment — secrets never touch the config file

| Variable | Required | Sent as |
|---|---|---|
| `JIRA_BASE_URL` | yes | — |
| `JIRA_TOKEN` | yes | `Authorization: Bearer <token>` |
| `GITLAB_URL` | only to enable tabs 2/3 | — |
| `GITLAB_TOKEN` | only to enable tabs 2/3 | `PRIVATE-TOKEN: <token>` header, needs GitLab `read_api` scope |

State this plainly to the user: **tokens come from the environment only.** The JSON config file
(`.jira-metrics.json`) actively **rejects** any key that looks like a secret — `token`,
`jira_token`, `gitlab_token`, `pat`, `password`, `secret`, checked recursively one level into
nested objects (e.g. under `"gitlab": {...}`) — `config.py` raises `ConfigError` and the run
aborts before touching the network if one is found. Never suggest putting a token in
`.jira-metrics.json`, never echo a token value back to the user, never write one into the
rendered HTML (the footer only ever prints `PAT (скрыт)` / "hidden — see env var").

`GITLAB_URL`/`GITLAB_TOKEN` must both be present or both absent — exactly one set is treated as
a misconfiguration and fails fast, since a half-configured GitLab connection can only fail
loudly later anyway.

## The three tabs, and the four cross-tab semantic divergences

Tab 1 («Команда») comes from the Jira sprint-metrics engine alone. Tabs 2 («Персональные») and
3 («Инженерия») are GitLab-derived, fed from the *same* Jira fetch tab 1 already made (no
second Jira pass) plus a GitLab pass. Because tabs 1 and 2 answer overlapping questions
("how many tasks did we finish?") using **different definitions**, a reader comparing raw
numbers across tabs will otherwise conclude the report contradicts itself. The tool already
prints the exact differences to the user as `semantics_notes` (Russian prose) in both the JSON
report and the rendered HTML banner at the top of tab 2 — here is the WHY behind each one:

1. **Sprint attribution.** Tab 1 attributes an issue to a sprint by its Sprint-field membership
   *intervals* (an issue can enter and leave a sprint more than once — Jira tracks this on the
   changelog). Tabs 2/3 attribute an issue to a sprint by whether its *completion date* falls
   inside that sprint's calendar dates. The same issue can legitimately land in different
   sprints under the two rules.
2. **Definition of "done".** Tab 1 counts an issue as delivered if, at sprint end, its status
   maps to Jira's own `done` status category (net of `cancelled_statuses`). Tab 2 counts an
   issue as done on the *first* time it enters one of the *named* final statuses in
   `jira.final_statuses` — which by default includes pre-release statuses like "To Test" and
   "Ready to Deploy" that are not necessarily category `done` yet. Counts from the two tabs
   never reconcile 1:1 and must not be compared directly.
3. **Throughput day.** Tab 1 uses the day of the *last* transition into a `done`-category status
   inside the sprint (a reopened-then-refixed issue only counts once, on the final fix).
   Tab 2 uses the day of the *first* transition into a final status. On a reopened issue these
   two dates can diverge by weeks.
4. **Story points.** Tab 1 replays the story-point changelog and uses the value the field held
   at the moment the issue *left* the sprint. Tab 2 uses the *current* field value at fetch
   time. Re-estimating an issue after its sprint closed moves tab 2's SP totals but never tab
   1's — this alone can make the two tabs' SP sums disagree even for the exact same issue set.

## Troubleshooting

- **Run `check` before `run` whenever a token/URL might have changed.** It is the fast,
  no-report-built way to find out exactly which item is broken (env vars, config file, Jira
  auth/reachability, story-point field, sprint/board resolution, GitLab auth/reachability) —
  cheaper than a full `run` failing partway through.
- **A revoked/invalid GitLab token fails the whole run loudly, by design.** `AUTH_FAILED`
  deliberately propagates out of `fetch_team_data()` instead of degrading to an empty
  personal/engineering section — a revoked token must never look like a clean, all-zero report.
  Non-zero exit, `error: ...` on stderr.
- **One unreachable or renamed GitLab project is not a hard failure.** It is recorded in
  `gitlab_fetch_issues.skipped_projects` (project path + error code + message) and disclosed in
  the rendered report's footer; every other configured project still runs.
- **The forecast can degrade instead of aborting the report.** Two independent reasons land in
  `forecast_error` (a plain string, report still renders, tab 1's forecast panel shows
  "unavailable" with that reason instead of crashing):
  - no active sprint found to derive `target_items` from, and `--target-items` was not passed;
  - fewer than 10 non-zero daily-throughput points across up to 5 closed sprints
    (`ERR_FORECAST_NOT_ENOUGH_DATA`).
- **A future/active sprint with no `startDate`/`endDate` yet still renders.** The burndown chart
  shows "нет данных" (0 calendar days) instead of raising; the heatmap and board rows for that
  sprint are unaffected.
- **Division by zero never crashes and never shows `NaN`.** Every ratio in this tool (KPI
  percentages, personal MR/task rates, engineering success rates) uses a "0 + warning" contract:
  a zero denominator renders as a literal `0` with a `DIV0` badge and a
  `WARN_DIVISION_BY_ZERO:<metric>` code, which is intentionally distinct from three other
  empty-value states (a source that returned no statistic at all, an average/median over a
  genuinely empty sample, and a value that is `null` by contract) — see README.md and
  `.research/design/DESIGN-NOTES.md §5` in the source project for the exact rendering rules and
  the four-state legend the report itself prints.
- **Zero third-party dependencies, zero external references.** Everything is Python 3.9+ stdlib
  (`urllib.request`; no `requests`, no `jinja2`, no `xlsx` writer). The generated HTML has no
  `<script src>`, no remote `<link>`, no `@import`, no live `http(s)://` reference anywhere —
  verified mechanically, not eyeballed. It opens from `file://` with no network, indefinitely.
