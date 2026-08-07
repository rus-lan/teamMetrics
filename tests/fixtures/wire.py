"""Real API-shaped Jira/GitLab fixture data + the FakeOpener wiring for it.

Shapes are taken from the actual client code (scripts/team_metrics/
jira_client.py, gitlab_client.py) and the two specs the team-lead brief
named (.research/jira-metrics-source/SPEC.md §2,
.research/ai-integration-metrics/SPEC.md §4). Anywhere a shape isn't pinned
down by either, a comment says so and states which client code path the
shape was reverse-engineered from.

Scenario in one paragraph: one board (501) with two closed sprints (base
history) and one ACTIVE target sprint whose name carries a literal quote
(`Sprint "42"`, proving the header escapes it). Three Jira issues: one
delivered in a base sprint, one added-but-unfinished in the target sprint,
and one delivered in the target sprint by an assignee whose login is a raw
`<script>` payload (`HOSTILE_USER`) — the same login is reused as a GitLab
MR author, so the identical hostile string flows through both halves of the
personal-metrics tab and must come out escaped in the rendered HTML. The one
GitLab project's path also carries markup, to prove the engineering tab
escapes it too. PROJ-3's search-embedded changelog is deliberately marked
truncated (`total > maxResults`) so the pipeline must re-fetch the full
changelog via `GET /rest/api/2/issue/PROJ-3?expand=changelog` — the test
asserts that endpoint was actually hit.
"""

from __future__ import annotations

from typing import Optional

from .http_fake import FakeOpener, bearer_token, private_token

# --------------------------------------------------------------------------
# Identity / auth
# --------------------------------------------------------------------------

JIRA_VALID_TOKEN = "jira-valid-pat-9f3a1c"
GITLAB_VALID_TOKEN = "gitlab-valid-pat-7e2b44"

# --------------------------------------------------------------------------
# Board / sprints
# --------------------------------------------------------------------------

BOARD_ID = 501
BOARD_NAME = "Team Board"

SPRINT_BASE1_ID = 199
SPRINT_BASE2_ID = 200
SPRINT_TARGET_ID = 201
# Literal quote in the sprint name — must come out HTML-escaped in the
# rendered header (render_html.py: HEADER_SPRINT_NAME = esc(...)).
TARGET_SPRINT_NAME = 'Sprint "42"'

HOSTILE_USER = "<script>alert(1)</script>"
# render_html.py: PERSON_USER = esc(person["user"]) — this string must never
# appear unescaped in the rendered report.

GITLAB_PROJECT_ID = 4242
# render_html.py: ENG_PROJECT_NAME = esc(row["project"]) — same requirement
# as HOSTILE_USER, for the engineering tab.
GITLAB_PROJECT_PATH = 'team/checkout-web"><script>alert(2)</script>'


def _sprint_dto(id_: int, name: str, state: str, board_id: int, start: str, end: str, complete: Optional[str] = None) -> dict:
    dto = {"id": id_, "self": f"https://jira.example.com/rest/agile/1.0/sprint/{id_}",
           "state": state, "name": name, "startDate": start, "endDate": end, "originBoardId": board_id}
    if complete:
        dto["completeDate"] = complete
    return dto


SPRINT_BASE1 = _sprint_dto(
    SPRINT_BASE1_ID, "Sprint 40", "closed", BOARD_ID,
    "2025-12-01T09:00:00.000+0000", "2025-12-12T18:00:00.000+0000", "2025-12-12T18:05:00.000+0000",
)
SPRINT_BASE2 = _sprint_dto(
    SPRINT_BASE2_ID, "Sprint 41", "closed", BOARD_ID,
    "2025-12-15T09:00:00.000+0000", "2025-12-26T18:00:00.000+0000", "2025-12-26T18:07:00.000+0000",
)
SPRINT_TARGET = _sprint_dto(
    SPRINT_TARGET_ID, TARGET_SPRINT_NAME, "active", BOARD_ID,
    "2025-12-29T09:00:00.000+0000", "2026-01-09T18:00:00.000+0000",
)
SPRINTS_BY_ID = {SPRINT_BASE1_ID: SPRINT_BASE1, SPRINT_BASE2_ID: SPRINT_BASE2, SPRINT_TARGET_ID: SPRINT_TARGET}

BOARD_DTO = {"id": BOARD_ID, "self": f"https://jira.example.com/rest/agile/1.0/board/{BOARD_ID}", "name": BOARD_NAME, "type": "scrum"}

# GET /rest/api/2/serverInfo — real Jira Server/DC shape (a handful of extra
# fields like buildDate/serverTime/scmInfo exist on the wire too, omitted
# here since jira_client.ServerInfo only reads the five it needs).
JIRA_SERVER_INFO = {
    "baseUrl": "https://jira.example.com",
    "version": "9.12.28",
    "versionNumbers": [9, 12, 28],
    "deploymentType": "Server",
    "buildNumber": 912000,
    "serverTitle": "Jira",
}

# --------------------------------------------------------------------------
# Field / status catalogs (SPEC §2.1, §2.4)
# --------------------------------------------------------------------------

SPRINT_FIELD_ID = "customfield_10001"
STORY_POINTS_FIELD_ID = "customfield_10016"

FIELD_CATALOG = [
    {"id": "summary", "name": "Summary", "custom": False},
    {"id": SPRINT_FIELD_ID, "name": "Sprint", "custom": True},
    {"id": STORY_POINTS_FIELD_ID, "name": "Story Points", "custom": True},
    {"id": "customfield_10020", "name": "QA Estimation", "custom": True},
    {"id": "customfield_10030", "name": "Role", "custom": True},
    {"id": "customfield_10014", "name": "Epic Link", "custom": True},
]

# statusCategory keys are exactly Jira's three real ones (new/indeterminate/
# done) — "cancelled" is never a Jira statusCategory; it only ever comes from
# the config file's own `cancelled_statuses` name list (model.py:
# effective_status_category checks cancelled_statuses before the catalog).
STATUS_TODO = {"id": "1", "name": "To Do", "statusCategory": {"id": 2, "key": "new", "name": "To Do"}}
STATUS_IN_PROGRESS = {"id": "2", "name": "In Progress", "statusCategory": {"id": 4, "key": "indeterminate", "name": "In Progress"}}
STATUS_DONE = {"id": "3", "name": "Done", "statusCategory": {"id": 3, "key": "done", "name": "Done"}}
STATUS_CATALOG = [STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE]


def _sprint_field_entry(sprint_id: int, board_id: int, state: str, name: str) -> dict:
    """Dict-shaped Sprint custom-field entry — jira_client._parse_sprint_field_entry
    accepts this OR the greenhopper toString form; the dict shape is the one
    modern Jira Server/DC (>=8) returns for `expand`-free field access, which
    is what a plain `/rest/api/2/search` fields projection gives (SPEC §2.7)."""
    return {"id": sprint_id, "boardId": board_id, "state": state, "name": name}


def _history(created_at: str, field: str, from_: str, to: str, from_string: str, to_string: str) -> dict:
    return {
        "id": "10000",
        "author": {"name": "system"},
        "created": created_at,
        "items": [{"field": field, "fieldtype": "jira" if field != "Sprint" else "custom",
                   "from": from_, "fromString": from_string, "to": to, "toString": to_string}],
    }


# --------------------------------------------------------------------------
# Issues
# --------------------------------------------------------------------------
# PROJ-1: base-sprint (199) issue, delivered. No Sprint-changelog event at
# all — current Sprint field already names 199, exercising
# build_membership_intervals()'s "no events, current field still names the
# sprint -> open interval from `created`" branch (model.py:107-110).

PROJ1_KEY = "PROJ-1"
PROJ1_DTO = {
    "id": "30001",
    "key": PROJ1_KEY,
    "fields": {
        "summary": "Fix checkout timeout",
        "status": STATUS_DONE,
        "issuetype": {"name": "Story"},
        "created": "2025-11-20T08:00:00.000+0000",
        "labels": ["backend"],
        "assignee": {"name": "alice", "displayName": "Alice Anderson"},
        "resolutiondate": "2025-12-10T15:00:00.000+0000",
        SPRINT_FIELD_ID: [_sprint_field_entry(SPRINT_BASE1_ID, BOARD_ID, "closed", "Sprint 40")],
        STORY_POINTS_FIELD_ID: 5,  # plain JSON number (SPEC §2.4 decoding)
    },
    "changelog": {
        "startAt": 0, "maxResults": 50, "total": 2,
        "histories": [
            _history("2025-12-05T09:00:00.000+0000", "status", "1", "2", "To Do", "In Progress"),
            _history("2025-12-10T15:00:00.000+0000", "status", "2", "3", "In Progress", "Done"),
        ],
    },
}

# PROJ-2: target-sprint (201) issue, created mid-sprint (after tl.start) so
# it classifies as "added" rather than "committed" — never reaches Done, so
# it stays un-delivered (exercises the active/incomplete-sprint path).

PROJ2_KEY = "PROJ-2"
PROJ2_DTO = {
    "id": "30002",
    "key": PROJ2_KEY,
    "fields": {
        "summary": "Add retry to payment webhook",
        "status": STATUS_IN_PROGRESS,
        "issuetype": {"name": "Story"},
        "created": "2025-12-30T10:00:00.000+0000",
        "labels": [],
        "assignee": None,
        "resolutiondate": None,
        SPRINT_FIELD_ID: [_sprint_field_entry(SPRINT_TARGET_ID, BOARD_ID, "active", TARGET_SPRINT_NAME)],
        STORY_POINTS_FIELD_ID: 3,
    },
    "changelog": {
        "startAt": 0, "maxResults": 50, "total": 1,
        "histories": [
            _history("2025-12-30T11:00:00.000+0000", "status", "1", "2", "To Do", "In Progress"),
        ],
    },
}

# PROJ-3: target-sprint issue, delivered, assigned to HOSTILE_USER, and its
# search-embedded changelog is deliberately TRUNCATED (total=3 > maxResults=1)
# — the embedded page carries only the Sprint-membership event, not either
# status transition, so a caller that skipped the truncated-changelog re-read
# (jira_client.py: issue_changelog(), SPEC §2.5) would derive `initial_status`
# from the current status ("Done") instead of "To Do". The full changelog
# (PROJ3_FULL_CHANGELOG_HISTORIES below) is served from
# GET /rest/api/2/issue/PROJ-3?expand=changelog.

PROJ3_KEY = "PROJ-3"
PROJ3_FULL_CHANGELOG_HISTORIES = [
    _history("2025-12-20T09:00:00.000+0000", "Sprint", "", str(SPRINT_TARGET_ID), "", TARGET_SPRINT_NAME),
    _history("2025-12-22T09:00:00.000+0000", "status", "1", "2", "To Do", "In Progress"),
    _history("2026-01-05T12:00:00.000+0000", "status", "2", "3", "In Progress", "Done"),
]
PROJ3_DTO = {
    "id": "30003",
    "key": PROJ3_KEY,
    "fields": {
        "summary": "<script>alert(1)</script>",  # captured onto RawIssue/
        # IssueFacts.summary (schema v2, jira_issues.csv) — the assignee,
        # not this field, still carries the primary HTML-escaping assertion
        # (see the test module's docstring).
        "status": STATUS_DONE,
        "issuetype": {"name": "Bug"},
        "created": "2025-12-20T09:00:00.000+0000",
        "labels": [],
        "assignee": {"name": HOSTILE_USER},
        "resolutiondate": "2026-01-05T12:00:00.000+0000",
        SPRINT_FIELD_ID: [_sprint_field_entry(SPRINT_TARGET_ID, BOARD_ID, "active", TARGET_SPRINT_NAME)],
        STORY_POINTS_FIELD_ID: {"value": 8},  # dict-shaped numeric value (SPEC §2.4)
    },
    "changelog": {
        "startAt": 0, "maxResults": 1, "total": 3,  # total > maxResults -> truncated
        "histories": [PROJ3_FULL_CHANGELOG_HISTORIES[0]],
    },
}
PROJ3_FULL_CHANGELOG_DTO = {
    "key": PROJ3_KEY,
    "fields": PROJ3_DTO["fields"],
    "changelog": {"startAt": 0, "maxResults": 50, "total": 3, "histories": PROJ3_FULL_CHANGELOG_HISTORIES},
}

ALL_ISSUE_DTOS = [PROJ1_DTO, PROJ2_DTO, PROJ3_DTO]

# --------------------------------------------------------------------------
# GitLab: merge requests / pipelines / deployments / coverage
# --------------------------------------------------------------------------

MR_ALICE_MERGED = {
    "iid": 501, "title": "Fix checkout timeout PROJ-1", "description": "",
    "author": {"username": "alice", "name": "Alice Anderson"}, "state": "merged",
    "web_url": "https://gitlab.example.com/team/checkout-web/-/merge_requests/501",
    "source_branch": "fix/PROJ-1-timeout",
    "created_at": "2026-01-02T10:00:00.000Z", "merged_at": "2026-01-03T15:30:00.000Z", "closed_at": None,
    # additions/deletions/changes_count/commits deliberately absent — the
    # real GitLab list endpoint documents none of them (gitlab_client.py's
    # own comment at _build_mr_record), forcing the detail + commits-count
    # fallbacks below.
}
MR_ALICE_MERGED_DETAIL = {"additions": 120, "deletions": 30, "changes_count": "7"}  # STRING, per team-lead brief
MR_ALICE_MERGED_COMMITS = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]

MR_HOSTILE_MERGED_P1 = {
    "iid": 502, "title": "Deliver PROJ-3 script-alert feature", "description": "",
    "author": {"username": HOSTILE_USER}, "state": "merged", "web_url": "https://gitlab.example.com/team/checkout-web/-/merge_requests/502",
    "source_branch": "feature/PROJ-3",
    "created_at": "2026-01-04T09:00:00.000Z", "merged_at": "2026-01-05T11:00:00.000Z", "closed_at": None,
}
MR_HOSTILE_MERGED_P1_DETAIL = {"additions": 40, "deletions": 5, "changes_count": "2"}
MR_HOSTILE_MERGED_P1_COMMITS = [{"id": "c4"}]

MR_HOSTILE_MERGED_P2 = {
    "iid": 503, "title": "Second delivery chunk PROJ-3", "description": "",
    "author": {"username": HOSTILE_USER}, "state": "merged", "web_url": "https://gitlab.example.com/team/checkout-web/-/merge_requests/503",
    "source_branch": "feature/PROJ-3-part2",
    "created_at": "2026-01-06T09:00:00.000Z", "merged_at": "2026-01-07T10:00:00.000Z", "closed_at": None,
}
# GitLab returns the literal string "1000+" when a MR's diff is capped
# (gitlab_client._changes_count) — exercised here, not just the plain-string case.
MR_HOSTILE_MERGED_P2_DETAIL = {"additions": 2000, "deletions": 500, "changes_count": "1000+"}
MR_HOSTILE_MERGED_P2_COMMITS = [{"id": "c5"}, {"id": "c6"}]

_MR_DETAILS = {501: MR_ALICE_MERGED_DETAIL, 502: MR_HOSTILE_MERGED_P1_DETAIL, 503: MR_HOSTILE_MERGED_P2_DETAIL}
_MR_COMMITS = {501: MR_ALICE_MERGED_COMMITS, 502: MR_HOSTILE_MERGED_P1_COMMITS, 503: MR_HOSTILE_MERGED_P2_COMMITS}

PIPELINE_A = {"id": 9001, "ref": "main", "sha": "aaa111", "status": "success",
              "created_at": "2026-01-03T12:00:00.000Z", "updated_at": "2026-01-03T12:20:00.000Z",
              "web_url": "https://gitlab.example.com/team/checkout-web/-/pipelines/9001"}
PIPELINE_B = {"id": 9002, "ref": "main", "sha": "bbb222", "status": "failed",
              "created_at": "2026-01-06T08:00:00.000Z", "updated_at": "2026-01-06T08:10:00.000Z",
              "web_url": "https://gitlab.example.com/team/checkout-web/-/pipelines/9002"}
_PIPELINE_JOB_USER = {9001: {"username": "alice", "name": "Alice A"}, 9002: {"username": HOSTILE_USER, "name": HOSTILE_USER}}
PIPELINE_A_DETAIL = {"id": 9001, "coverage": "87.5"}  # coverage as STRING, per real GitLab

DEPLOYMENT_1 = {"id": 7001, "status": "success", "environment": {"name": "production"}, "ref": "main", "sha": "aaa111",
                "created_at": "2026-01-03T12:25:00.000Z", "finished_at": "2026-01-03T12:30:00.000Z",
                "web_url": "https://gitlab.example.com/team/checkout-web/-/environments/1/deployments/7001",
                "user": {"username": "alice", "name": "Alice A"}}
DEPLOYMENT_2 = {"id": 7002, "status": "failed", "environment": {"name": "staging"}, "ref": "main", "sha": "bbb222",
                "created_at": "2026-01-06T08:15:00.000Z", "finished_at": "2026-01-06T08:20:00.000Z",
                "web_url": "https://gitlab.example.com/team/checkout-web/-/environments/2/deployments/7002",
                "user": {"username": HOSTILE_USER, "name": HOSTILE_USER}}

GITLAB_CURRENT_USER = {"id": 1, "username": "svc-account", "name": "Service Account"}

# --------------------------------------------------------------------------
# FakeOpener wiring
# --------------------------------------------------------------------------


def _jira_auth_ok(request, expected_token: str) -> bool:
    return bearer_token(request) == expected_token


def _gitlab_auth_ok(request, expected_token: str) -> bool:
    return private_token(request) == expected_token


def build_opener(
    *,
    jira_token: str = JIRA_VALID_TOKEN,
    gitlab_token: str = GITLAB_VALID_TOKEN,
    known_projects: Optional[dict] = None,
    name: str = "scenario",
) -> FakeOpener:
    """Builds one FakeOpener wired for the whole scenario above.

    `jira_token`/`gitlab_token` are the token values THIS opener accepts —
    every route 401s any request whose Authorization/PRIVATE-TOKEN header
    doesn't match. Auth-failure tests don't need a different opener: they
    just point cli.main() at this same one while feeding a WRONG token
    through `environ`, which this opener naturally rejects.

    `known_projects`: {project_path: project_id} — any GitLab project path
    not in this dict 404s from `GET /api/v4/projects/{path}`, covering the
    "unresolvable project" scenario without a second opener either.
    """
    known_projects = known_projects if known_projects is not None else {GITLAB_PROJECT_PATH: GITLAB_PROJECT_ID}
    opener = FakeOpener(name=name)

    # -- Jira ----------------------------------------------------------

    def _jira_401():
        return 401, {"errorMessages": ["Unauthorized (mock)"], "errors": {}}, {}

    def field_catalog(_m, _q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        return 200, FIELD_CATALOG, {}

    def status_catalog(_m, _q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        return 200, STATUS_CATALOG, {}

    def board(m, _q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        board_id = int(m.group("board_id"))
        if board_id != BOARD_ID:
            return 404, {"errorMessages": [f"board {board_id} not found"]}, {}
        return 200, BOARD_DTO, {}

    def board_sprints(m, q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        board_id = int(m.group("board_id"))
        if board_id != BOARD_ID:
            return 404, {"errorMessages": [f"board {board_id} not found"]}, {}
        state = q.get("state")
        start_at = int(q.get("startAt", "0"))
        if state == "closed":
            # Two pages, split by startAt, isLast-driven pagination (SPEC
            # §2.1: "paginate until isLast or empty page").
            if start_at == 0:
                return 200, {"maxResults": 50, "startAt": 0, "isLast": False, "values": [SPRINT_BASE1]}, {}
            return 200, {"maxResults": 50, "startAt": 1, "isLast": True, "values": [SPRINT_BASE2]}, {}
        if state == "active":
            return 200, {"maxResults": 50, "startAt": 0, "isLast": True, "values": [SPRINT_TARGET]}, {}
        return 200, {"maxResults": 50, "startAt": 0, "isLast": True, "values": []}, {}

    def sprint_by_id(m, _q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        sprint_id = int(m.group("sprint_id"))
        dto = SPRINTS_BY_ID.get(sprint_id)
        if dto is None:
            return 404, {"errorMessages": [f"sprint {sprint_id} not found"]}, {}
        return 200, dto, {}

    def search(_m, q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        start_at = int(q.get("startAt", "0"))
        # Two pages via actual-count-vs-total (SPEC §2.1): page 1 returns 2
        # of 3 issues, page 2 the remainder.
        if start_at == 0:
            return 200, {"startAt": 0, "maxResults": 100, "total": 3, "issues": [PROJ1_DTO, PROJ2_DTO]}, {}
        return 200, {"startAt": 2, "maxResults": 100, "total": 3, "issues": [PROJ3_DTO]}, {}

    def issue_changelog(m, q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        key = m.group("key")
        if q.get("expand") != "changelog":
            return 400, {"errorMessages": ["expand=changelog required (mock)"]}, {}
        if key == PROJ3_KEY:
            return 200, PROJ3_FULL_CHANGELOG_DTO, {}
        return 404, {"errorMessages": [f"issue {key} not found"]}, {}

    def server_info(_m, _q, request):
        if not _jira_auth_ok(request, jira_token):
            return _jira_401()
        return 200, JIRA_SERVER_INFO, {}

    opener.route(r"/rest/api/2/field", field_catalog)
    opener.route(r"/rest/api/2/status", status_catalog)
    opener.route(r"/rest/agile/1\.0/board/(?P<board_id>\d+)", board)
    opener.route(r"/rest/agile/1\.0/board/(?P<board_id>\d+)/sprint", board_sprints)
    opener.route(r"/rest/agile/1\.0/sprint/(?P<sprint_id>\d+)", sprint_by_id)
    opener.route(r"/rest/api/2/search", search)
    opener.route(r"/rest/api/2/issue/(?P<key>[^/]+)", issue_changelog)
    opener.route(r"/rest/api/2/serverInfo", server_info)

    # -- GitLab ----------------------------------------------------------

    def _gitlab_401():
        return 401, {"message": "401 Unauthorized (mock)"}, {}

    def gitlab_user(_m, _q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        return 200, GITLAB_CURRENT_USER, {}

    def gitlab_project(m, _q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        import urllib.parse as _up

        path = _up.unquote(m.group("path"))
        pid = known_projects.get(path)
        if pid is None:
            return 404, {"message": "404 Project Not Found"}, {}
        return 200, {"id": pid, "path_with_namespace": path}, {}

    def merge_requests(m, q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        pid = int(m.group("pid"))
        if pid != GITLAB_PROJECT_ID:
            return 404, {"message": "404 Project Not Found"}, {}
        author = q.get("author_username")
        state = q.get("state")
        page = int(q.get("page", "1"))
        if author == "alice" and state == "merged":
            return 200, [MR_ALICE_MERGED], {}
        if author == HOSTILE_USER and state == "merged":
            # X-Next-Page-driven pagination (team-lead brief): the header
            # decides the next page regardless of batch size vs per_page.
            if page == 1:
                return 200, [MR_HOSTILE_MERGED_P1], {"X-Next-Page": "2"}
            return 200, [MR_HOSTILE_MERGED_P2], {"X-Next-Page": ""}
        return 200, [], {}

    def mr_detail(m, _q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        pid, iid = int(m.group("pid")), int(m.group("iid"))
        if pid != GITLAB_PROJECT_ID or iid not in _MR_DETAILS:
            return 404, {"message": "404 Not found"}, {}
        return 200, _MR_DETAILS[iid], {}

    def mr_commits(m, _q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        pid, iid = int(m.group("pid")), int(m.group("iid"))
        if pid != GITLAB_PROJECT_ID or iid not in _MR_COMMITS:
            return 404, {"message": "404 Not found"}, {}
        return 200, _MR_COMMITS[iid], {}

    def pipelines(m, q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        pid = int(m.group("pid"))
        if pid != GITLAB_PROJECT_ID:
            return 404, {"message": "404 Project Not Found"}, {}
        if q.get("status") == "success":
            # coverage()'s own lookup shape: status=success&page=1&per_page=1
            return 200, [PIPELINE_A], {}
        page = int(q.get("page", "1"))
        if page == 1:
            return 200, [PIPELINE_A], {"X-Next-Page": "2"}
        return 200, [PIPELINE_B], {"X-Next-Page": ""}

    def pipeline_jobs(m, _q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        pid, pipeline_id = int(m.group("pid")), int(m.group("pipeline_id"))
        if pid != GITLAB_PROJECT_ID or pipeline_id not in _PIPELINE_JOB_USER:
            return 404, {"message": "404 Not found"}, {}
        return 200, [{"id": 1, "user": _PIPELINE_JOB_USER[pipeline_id]}], {}

    def pipeline_detail(m, _q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        pid, pipeline_id = int(m.group("pid")), int(m.group("pipeline_id"))
        if pid != GITLAB_PROJECT_ID or pipeline_id != PIPELINE_A["id"]:
            return 404, {"message": "404 Not found"}, {}
        return 200, PIPELINE_A_DETAIL, {}

    def deployments(m, _q, request):
        if not _gitlab_auth_ok(request, gitlab_token):
            return _gitlab_401()
        pid = int(m.group("pid"))
        if pid != GITLAB_PROJECT_ID:
            return 404, {"message": "404 Project Not Found"}, {}
        return 200, [DEPLOYMENT_1, DEPLOYMENT_2], {}

    opener.route(r"/api/v4/user", gitlab_user)
    opener.route(r"/api/v4/projects/(?P<path>[^/]+)", gitlab_project)
    opener.route(r"/api/v4/projects/(?P<pid>\d+)/merge_requests", merge_requests)
    opener.route(r"/api/v4/projects/(?P<pid>\d+)/merge_requests/(?P<iid>\d+)/commits", mr_commits)
    opener.route(r"/api/v4/projects/(?P<pid>\d+)/merge_requests/(?P<iid>\d+)", mr_detail)
    opener.route(r"/api/v4/projects/(?P<pid>\d+)/pipelines/(?P<pipeline_id>\d+)/jobs", pipeline_jobs)
    opener.route(r"/api/v4/projects/(?P<pid>\d+)/pipelines/(?P<pipeline_id>\d+)", pipeline_detail)
    opener.route(r"/api/v4/projects/(?P<pid>\d+)/pipelines", pipelines)
    opener.route(r"/api/v4/projects/(?P<pid>\d+)/deployments", deployments)

    return opener
