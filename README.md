# jira-metrics-report

A self-contained HTML sprint/team-metrics report, built from Jira (and optionally GitLab) data
by two stdlib-only Python 3.9+ CLIs. No third-party packages, no build step, no server — the
output is one `.html` file with zero external references that opens from `file://` forever.

It is a **Python port of two internal tools**: the Jira sprint-metrics engine
(`internal/app/board/{translate,service,report,forecast}.go`, see
`.research/jira-metrics-source/SPEC.md`) and the GitLab-derived personal/engineering-metrics
skill (`.research/ai-integration-metrics/SPEC.md`). The numbers, thresholds, and even quirks
(field semantics that differ between the two tabs, an "off by one" in `throughput_items` vs
`delivered_items`, the exact percentile/histogram math of the forecast) are reproduced
**deliberately** — this is a faithful port, not a redesign. See `.research/*/SPEC.md` for the
line-by-line source citations, and `.research/design/DESIGN-NOTES.md` for the rendering
decisions (status thresholds, delta directions, the four empty-value states) layered on top in
`render_html.py`.

For how to trigger and run this as an agent skill, see [`SKILL.md`](./SKILL.md).

## What it produces

Three tabs in one HTML file:

| Tab | Source | Content |
|---|---|---|
| «Команда» | Jira only | Sprint KPIs, board history table, burndown chart, per-issue heatmap, Monte-Carlo forecast |
| «Персональные» | GitLab + Jira | Per-person MR and task metrics: merge rate, cycle time, diff size, defect rate, rework, story points, per-sprint breakdown |
| «Инженерия» | GitLab only | Team-level pipelines, deployments, test coverage, per-project breakdown |

Tabs 2 and 3 are optional — without `GITLAB_URL`/`GITLAB_TOKEN` (or with `--no-gitlab`), they
report themselves unavailable with a reason and tab 1 still renders normally.

A demo report generated from an offline fixture (no network) lives at
[`demo/report-demo.html`](./demo/report-demo.html) — open it in a browser to see every tab
exercised at once, including the edge cases (missing GitLab diff stats, a skipped project,
several DIV0 metrics, a forecast that succeeds).

## Install

Either works — this is a plain Python package, not something that needs "installing":

- **As a Claude Code / opencode skill**: copy this whole directory (`SKILL.md`, `README.md`,
  `scripts/`, `templates/`, `.jira-metrics.example.json`, `demo/`) into
  `~/.claude/skills/jira-metrics-report/`. Both assistants auto-discover skills from
  `~/.claude/skills/`.
- **Run the scripts directly**: clone/copy this repo and invoke `scripts/jira-metrics` (or the two
  engines it wraps, `scripts/jira_metrics/report_data.py` / `render_html.py`) with `python3` — no
  install step, no virtualenv needed (stdlib only).

## Quickstart

```sh
scripts/jira-metrics init                         # writes ./.jira-metrics.json, lists missing env vars

export JIRA_BASE_URL="https://jira.example.com"
export JIRA_TOKEN="<your Jira personal access token>"
export GITLAB_URL="https://gitlab.example.com"          # optional — enables tabs 2/3
export GITLAB_TOKEN="<your GitLab PAT, read_api scope>"  # optional

# edit .jira-metrics.json: projects/employees/story_points_field_id for your team

scripts/jira-metrics check --sprint-names "Sprint 42"   # verifies setup, builds nothing

scripts/jira-metrics run --sprint-names "Sprint 42" --history 5
# -> writes ./report.json and ./report.html, prints both paths
```

`report.html` is the finished file — open it in any browser, `file://` included, no network
required after this point. To re-render it later from the saved JSON, with no network at all:

```sh
scripts/jira-metrics report report.json -o report.html
```

`init`/`check`/`run`/`report` are the four subcommands of one dispatcher,
`scripts/jira-metrics` (equivalently `python3 -m jira_metrics <command>` run from `scripts/`) —
see [`SKILL.md`](./SKILL.md#cli-reference--jira-metrics-dispatcher-the-command-an-agent-should-run)
for the full reference, or call the two underlying engines directly (`scripts/jira_metrics/
report_data.py` / `render_html.py`, see SKILL.md's "Direct script invocation") if you want the
JSON and HTML steps fully decoupled.

## Environment variables (secrets — never the config file)

| Variable | Required | Sent as |
|---|---|---|
| `JIRA_BASE_URL` | yes | — |
| `JIRA_TOKEN` | yes | `Authorization: Bearer <token>` |
| `GITLAB_URL` | only to enable tabs 2/3 | — |
| `GITLAB_TOKEN` | only to enable tabs 2/3 | `PRIVATE-TOKEN: <token>` header, `read_api` scope |

`config.py` actively rejects a `.jira-metrics.json` that contains any key shaped like a secret
(`token`, `jira_token`, `gitlab_token`, `pat`, `password`, `secret`, checked recursively one
level into nested objects) — the run aborts before any network call if one is found. There is
no supported way to put a credential in the config file; it only ever comes from the
environment.

## JSON config file — every key

Optional. Default path is `./.jira-metrics.json`; override with `--config <path>`. See
[`.jira-metrics.example.json`](./.jira-metrics.example.json) for a filled-in example — JSON has
no comments, so the explanation of each key lives here instead.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `status_map` | `{status name: category}` | `{}` | Overrides which status *category* (`new` \| `indeterminate` \| `done` \| `cancelled`) a raw Jira status name maps to. Case-insensitive lookup. Precedence: `cancelled_statuses` > `status_map` > Jira's own `/rest/api/2/status` catalog > unmapped (renders as `WARN_STATUS_UNMAPPED`, a distinct heatmap color) |
| `cancelled_statuses` | `[string]` | `["Cancelled", "Отменено", "Rejected"]` | Status names treated as category `cancelled` — takes precedence over `status_map` and the Jira catalog |
| `story_points_field_id` | `string` | `""` (auto-detect by field name) | Jira custom field id for Story Points (e.g. `"customfield_10016"`). Set this only if auto-detection by field name picks the wrong custom field on your instance |
| `history_sprint_count` | `int` | `0` → resolves to `5`, clamped to `20` | How many previous **closed** sprints (besides the targets) feed the board history table and velocity trend on tab 1. Overridden by `--history` on the CLI when that flag is non-zero |
| `gitlab.projects` | `[string]` | `[]` | GitLab project paths (`namespace/project`), fetched for tabs 2/3. A project that fails to resolve (renamed, deleted, no access) is recorded in `gitlab_fetch_issues.skipped_projects` — the run does not abort for it |
| `employees` | `[string]` | `[]` | GitLab usernames to fetch merge requests for — the *same* string is used to match a Jira `assignee` on the personal tab, so this list should be each person's shared GitLab/Jira login |
| `jira.final_statuses` | `[string]` | `["To Test", "Ready for Initial Test", "Done", "Closed", "Ready to Deploy"]` | Named Jira statuses the **personal-metrics tab** (tab 2 only) treats as "done" — first entry into any of these marks a task complete for that tab. Tab 1 never uses this list; it uses Jira's own `done` status category instead (see "How the metrics are defined" below) |

## CLI reference

Two levels, both documented in SKILL.md:

- [`jira-metrics` dispatcher](./SKILL.md#cli-reference--jira-metrics-dispatcher-the-command-an-agent-should-run) —
  `init`/`check`/`run`/`report`, the command surface described in Quickstart above.
- [`report_data.py`/`render_html.py` direct flags](./SKILL.md#cli-reference--report_datapy-the-real-surface-read-configpy-before-inventing-a-flag) —
  the full, verified flag table (`--sprint-ids`/`--sprint-names`, `--board-id`, `--history`,
  `--seed`, `--target-items`, `--iterations`, `--config`, `--json`, `--out`, `--no-gitlab`,
  `--no-personal`, plus `render_html.py`'s `report_json`/`-o`/`--template`) — identical to `run`'s
  flags except `run`'s own `--out`/`--json-out` (HTML + JSON, not JSON-only).

## How the metrics are defined

Full formulas, thresholds, and edge-case rules live in the source specs, not duplicated here to
avoid drift:

- **Sprint metrics (tab 1)** — committed/delivered/scope-change SP and item counts, velocity,
  SMA5, load %, closure %, the heatmap's status-category rules, the burndown model, and the
  Monte-Carlo forecast (bootstrap over calendar daily throughput, nearest-rank percentiles):
  `.research/jira-metrics-source/SPEC.md`.
- **Personal + engineering metrics (tabs 2/3)** — MR merge rate, cycle time, diff size, defect
  rate, rework, story points, per-sprint breakdown, and the team-level pipeline/deployment/
  coverage rollups: `.research/ai-integration-metrics/SPEC.md`.
- **Rendering decisions layered on top** — the good/warn/bad status thresholds (not present in
  either backend — SPEC §7 is explicit that there is no KPI target classification in the data
  layer, so the thresholds are a presentation-layer product decision), the delta-direction table
  (which way is "good" per metric — `load_pct` is bad in *both* directions), and the four
  distinct empty-value states (`DIV0` badge vs "no data" vs empty sample vs null-by-contract):
  `.research/design/DESIGN-NOTES.md`.

Three cross-tab semantic divergences are worth knowing before comparing tab 1 and tab 2 numbers
side by side — sprint attribution (membership intervals vs completion date), the definition of
"done" (status category vs named final statuses), and story points (end-of-sprint-membership
value vs current field value). The tool prints the exact wording to the user automatically
(`semantics_notes` in the JSON report, rendered as a banner on tab 2); `SKILL.md` explains the
WHY behind each one.

## Troubleshooting

See [`SKILL.md`'s Troubleshooting section](./SKILL.md#troubleshooting) — covers a revoked
GitLab token (fails loudly, by design), a skipped GitLab project (disclosed, not fatal), a
degraded forecast (reason shown, report still renders), and the division-by-zero contract (`0` +
`DIV0` badge, never a crash or `NaN`).
