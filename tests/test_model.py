import _pathfix  # noqa: F401

import unittest
from datetime import datetime, timedelta, timezone

from jira_metrics import model

from helpers import dt, make_cfg, make_issue, make_timeline

UTC = timezone.utc


class MembershipIntervalsTests(unittest.TestCase):
    def test_no_events_current_field_contains_sprint_creates_single_open_interval(self):
        created = dt(2026, 1, 1)
        out = model.build_membership_intervals(created, [], current_ids=[181], sprint_id=181)
        self.assertEqual(out, [model.Interval(from_=created, until=None)])

    def test_no_events_current_field_missing_sprint_gives_no_interval(self):
        out = model.build_membership_intervals(dt(2026, 1, 1), [], current_ids=[999], sprint_id=181)
        self.assertEqual(out, [])

    def test_first_event_from_already_contains_sprint_opens_from_created(self):
        created = dt(2026, 1, 1)
        events = [model.SprintChange(date=dt(2026, 1, 10), from_ids=[181], to_ids=[])]
        out = model.build_membership_intervals(created, events, current_ids=[], sprint_id=181)
        self.assertEqual(out, [model.Interval(from_=created, until=dt(2026, 1, 10))])

    def test_multiple_exits_and_reentries(self):
        created = dt(2026, 1, 1)
        events = [
            model.SprintChange(date=dt(2026, 1, 5), from_ids=[], to_ids=[181]),
            model.SprintChange(date=dt(2026, 1, 10), from_ids=[181], to_ids=[]),
            model.SprintChange(date=dt(2026, 1, 15), from_ids=[], to_ids=[181]),
        ]
        out = model.build_membership_intervals(created, events, current_ids=[181], sprint_id=181)
        self.assertEqual(
            out,
            [
                model.Interval(from_=dt(2026, 1, 5), until=dt(2026, 1, 10)),
                model.Interval(from_=dt(2026, 1, 15), until=None),
            ],
        )


class ObservedEndTests(unittest.TestCase):
    """SprintTimeline.observed_end() = min(schedule_end, classify_end)."""

    def test_schedule_end_before_classify_end_wins(self):
        tl = model.SprintTimeline(start=dt(2026, 1, 1), schedule_end=dt(2026, 1, 9), classify_end=dt(2026, 1, 20))
        self.assertEqual(tl.observed_end(), dt(2026, 1, 9))

    def test_classify_end_before_schedule_end_wins(self):
        # An active sprint's classify_end ("now") can be earlier than its
        # nominal schedule_end (endDate) — observed_end must not look ahead.
        tl = model.SprintTimeline(start=dt(2026, 1, 1), schedule_end=dt(2026, 1, 20), classify_end=dt(2026, 1, 9))
        self.assertEqual(tl.observed_end(), dt(2026, 1, 9))

    def test_equal_bounds_returns_that_value(self):
        tl = model.SprintTimeline(start=dt(2026, 1, 1), schedule_end=dt(2026, 1, 9), classify_end=dt(2026, 1, 9))
        self.assertEqual(tl.observed_end(), dt(2026, 1, 9))


class ClassifyTests(unittest.TestCase):
    """committed / added / removed / delivered — SPEC §3.3."""

    def setUp(self):
        self.tl = make_timeline(dt(2026, 1, 1), dt(2026, 1, 15))
        self.cfg = make_cfg(jira_status_categories={"Done": "done", "To Do": "new"})

    def test_committed_and_delivered(self):
        issue = make_issue(status_history=[model.StatusChange(at=dt(2026, 1, 10), from_status="To Do", to_status="Done")])
        intervals = [model.Interval(from_=dt(2025, 12, 20), until=None)]
        role = model.classify(issue, intervals, self.tl, self.cfg)
        self.assertTrue(role.committed)
        self.assertFalse(role.added)
        self.assertFalse(role.removed)
        self.assertTrue(role.delivered)

    def test_added_after_start(self):
        issue = make_issue()
        intervals = [model.Interval(from_=dt(2026, 1, 5), until=None)]
        role = model.classify(issue, intervals, self.tl, self.cfg)
        self.assertFalse(role.committed)
        self.assertTrue(role.added)
        self.assertFalse(role.removed)

    def test_removed_before_classify_end(self):
        issue = make_issue()
        intervals = [model.Interval(from_=dt(2025, 12, 20), until=dt(2026, 1, 8))]
        role = model.classify(issue, intervals, self.tl, self.cfg)
        self.assertTrue(role.committed)
        self.assertTrue(role.removed)
        self.assertFalse(role.delivered)  # not a member at classify_end -> delivered can't be true

    def test_not_in_sprint_at_all_gives_empty_role(self):
        issue = make_issue()
        intervals = [model.Interval(from_=dt(2025, 1, 1), until=dt(2025, 6, 1))]
        role = model.classify(issue, intervals, self.tl, self.cfg)
        self.assertFalse(role.in_sprint)
        self.assertFalse(role.committed)
        self.assertFalse(role.added)
        self.assertFalse(role.removed)
        self.assertFalse(role.delivered)

    def test_committed_but_not_delivered_when_still_in_progress(self):
        issue = make_issue(initial_status="In Progress")
        intervals = [model.Interval(from_=dt(2025, 12, 20), until=None)]
        cfg = make_cfg(jira_status_categories={"In Progress": "indeterminate"})
        role = model.classify(issue, intervals, self.tl, cfg)
        self.assertTrue(role.committed)
        self.assertFalse(role.delivered)

    def test_cancelled_status_never_counts_as_delivered_even_if_status_map_says_done(self):
        issue = make_issue(status_history=[model.StatusChange(at=dt(2026, 1, 10), from_status="To Do", to_status="Cancelled")])
        intervals = [model.Interval(from_=dt(2025, 12, 20), until=None)]
        cfg = make_cfg(status_map={"Cancelled": "done"}, cancelled_statuses=["Cancelled"])
        role = model.classify(issue, intervals, self.tl, cfg)
        self.assertTrue(role.committed)
        self.assertFalse(role.delivered)


class EstimationChangeWindowTests(unittest.TestCase):
    """Story-point change events inside vs outside the classification window — SPEC §3.8."""

    def setUp(self):
        self.tl = make_timeline(dt(2026, 1, 1), dt(2026, 1, 15))
        self.intervals = [model.Interval(from_=dt(2025, 12, 20), until=None)]

    def test_event_inside_window_counts(self):
        issue = make_issue(sp_events=[model.SPChange(at=dt(2026, 1, 5), from_value=3.0, to_value=5.0)])
        self.assertEqual(model.estimation_change(issue, self.intervals, self.tl), 2.0)

    def test_event_before_start_is_excluded(self):
        issue = make_issue(sp_events=[model.SPChange(at=dt(2025, 12, 25), from_value=3.0, to_value=5.0)])
        self.assertEqual(model.estimation_change(issue, self.intervals, self.tl), 0.0)

    def test_event_at_or_after_classify_end_is_excluded(self):
        issue = make_issue(
            sp_events=[
                model.SPChange(at=dt(2026, 1, 15), from_value=3.0, to_value=5.0),  # == classify_end, excluded
                model.SPChange(at=dt(2026, 1, 20), from_value=5.0, to_value=8.0),  # after classify_end
            ]
        )
        self.assertEqual(model.estimation_change(issue, self.intervals, self.tl), 0.0)

    def test_event_while_not_a_member_is_excluded(self):
        issue = make_issue(sp_events=[model.SPChange(at=dt(2026, 1, 10), from_value=3.0, to_value=5.0)])
        intervals = [model.Interval(from_=dt(2025, 12, 20), until=dt(2026, 1, 8))]  # left before the event
        self.assertEqual(model.estimation_change(issue, intervals, self.tl), 0.0)

    def test_mixed_events_only_in_window_ones_net(self):
        issue = make_issue(
            sp_events=[
                model.SPChange(at=dt(2025, 12, 25), from_value=0.0, to_value=3.0),  # before window
                model.SPChange(at=dt(2026, 1, 5), from_value=3.0, to_value=5.0),  # inside
                model.SPChange(at=dt(2026, 1, 8), from_value=5.0, to_value=2.0),  # inside
            ]
        )
        self.assertEqual(model.estimation_change(issue, self.intervals, self.tl), -1.0)  # (5-3) + (2-5)


class StoryPointsAtTests(unittest.TestCase):
    def test_no_events_returns_current_value(self):
        issue = make_issue(story_points=8.0)
        self.assertEqual(model.story_points_at(issue, dt(2026, 6, 1)), 8.0)

    def test_before_first_event_returns_events0_from(self):
        issue = make_issue(
            story_points=8.0,
            sp_events=[model.SPChange(at=dt(2026, 1, 5), from_value=3.0, to_value=5.0)],
        )
        self.assertEqual(model.story_points_at(issue, dt(2026, 1, 1)), 3.0)

    def test_after_last_event_returns_last_to(self):
        issue = make_issue(
            sp_events=[
                model.SPChange(at=dt(2026, 1, 5), from_value=3.0, to_value=5.0),
                model.SPChange(at=dt(2026, 1, 10), from_value=5.0, to_value=8.0),
            ]
        )
        self.assertEqual(model.story_points_at(issue, dt(2026, 6, 1)), 8.0)


class EffectiveStatusCategoryTests(unittest.TestCase):
    def test_cancelled_beats_status_map_done(self):
        cfg = make_cfg(status_map={"Rejected": "done"}, cancelled_statuses=["Rejected"])
        cat, warn = model.effective_status_category("Rejected", cfg)
        self.assertEqual(cat, model.CATEGORY_CANCELLED)
        self.assertEqual(warn, "")

    def test_status_map_overrides_jira_catalog(self):
        cfg = make_cfg(status_map={"Custom Done": "done"}, jira_status_categories={"Custom Done": "indeterminate"})
        cat, warn = model.effective_status_category("Custom Done", cfg)
        self.assertEqual(cat, "done")
        self.assertEqual(warn, "")

    def test_jira_catalog_fallback(self):
        cfg = make_cfg(jira_status_categories={"Done": "done"})
        cat, warn = model.effective_status_category("Done", cfg)
        self.assertEqual(cat, "done")
        self.assertEqual(warn, "")

    def test_unmapped_status_warns(self):
        cfg = make_cfg()
        cat, warn = model.effective_status_category("Mystery", cfg)
        self.assertEqual(cat, "")
        self.assertEqual(warn, model.WARN_STATUS_UNMAPPED)

    def test_empty_status_never_warns(self):
        cfg = make_cfg()
        cat, warn = model.effective_status_category("", cfg)
        self.assertEqual(cat, "")
        self.assertEqual(warn, "")

    def test_case_insensitive_trimmed_match(self):
        cfg = make_cfg(cancelled_statuses=["cancelled"])
        cat, _ = model.effective_status_category("  Cancelled  ", cfg)
        self.assertEqual(cat, model.CATEGORY_CANCELLED)


class WorkingDaysTests(unittest.TestCase):
    def test_excludes_saturday_and_sunday(self):
        # Mon 2026-01-05 .. Sun 2026-01-11 (one full week)
        days = model.working_days(dt(2026, 1, 5), dt(2026, 1, 11))
        self.assertEqual([d.strftime("%Y-%m-%d") for d in days], [
            "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
        ])

    def test_missing_dates_zero_time_sentinel_degrades_to_one_day_no_crash(self):
        # M1: a future sprint's missing startDate/endDate decodes to
        # model.ZERO_TIME (0001-01-01), never None/AttributeError — mirrors
        # Go's parseOptionalJiraTime degrading to "one degenerate day".
        days = model.working_days(model.ZERO_TIME, model.ZERO_TIME)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0], model.ZERO_TIME)


class CivilDayTimezoneTests(unittest.TestCase):
    """n3: civil_day/working_days must normalize both ends into the SAME
    location (mirrors Go's t.In(loc)) — otherwise a start/end pair with
    different tzinfos (e.g. a caller-supplied `now` that isn't UTC) can shift
    the derived day boundary by the offset."""

    def test_civil_day_defaults_to_own_tzinfo(self):
        plus3 = timezone(timedelta(hours=3))
        t = datetime(2026, 1, 5, 23, 30, tzinfo=plus3)
        self.assertEqual(model.civil_day(t), datetime(2026, 1, 5, tzinfo=plus3))

    def test_civil_day_normalizes_into_explicit_loc(self):
        # 2026-01-05 23:30 UTC == 2026-01-06 02:30 +03:00 -> civil day shifts
        # forward one day once normalized into +03:00.
        plus3 = timezone(timedelta(hours=3))
        t = datetime(2026, 1, 5, 23, 30, tzinfo=UTC)
        self.assertEqual(model.civil_day(t, plus3), datetime(2026, 1, 6, tzinfo=plus3))

    def test_working_days_uses_starts_location_for_both_ends(self):
        # start in +03:00, end in UTC 45 minutes later in wall-clock terms but
        # actually the same civil day once end is normalized into +03:00.
        plus3 = timezone(timedelta(hours=3))
        start = datetime(2026, 1, 5, 1, 0, tzinfo=plus3)  # 2026-01-04 22:00 UTC
        end = datetime(2026, 1, 4, 23, 0, tzinfo=UTC)  # 2026-01-05 02:00 +03:00
        days = model.working_days(start, end)
        # Both ends land on the same +03:00 civil day (2026-01-05) once
        # normalized -> exactly one working day, not zero/negative.
        self.assertEqual([d.strftime("%Y-%m-%d") for d in days], ["2026-01-05"])


class MembershipEndTests(unittest.TestCase):
    def test_returns_max_to_across_intervals(self):
        intervals = [
            model.PayloadInterval(from_=dt(2026, 1, 1), to=dt(2026, 1, 5)),
            model.PayloadInterval(from_=dt(2026, 1, 10), to=dt(2026, 1, 20)),
        ]
        self.assertEqual(model.membership_end(intervals), dt(2026, 1, 20))

    def test_empty_list_returns_zero_time_sentinel(self):
        self.assertEqual(model.membership_end([]), model.ZERO_TIME)


class DedupeWarningsTests(unittest.TestCase):
    def test_drops_empty_strings_and_duplicates_keeps_first_occurrence_order(self):
        out = model.dedupe_warnings(["", model.WARN_STATUS_UNMAPPED, "", model.WARN_BASELINE_SHORT, model.WARN_STATUS_UNMAPPED])
        self.assertEqual(out, [model.WARN_STATUS_UNMAPPED, model.WARN_BASELINE_SHORT])

    def test_empty_input_gives_empty_output(self):
        self.assertEqual(model.dedupe_warnings([]), [])


class IsEpicTests(unittest.TestCase):
    """m2: exact match only, mirrors Go's `== "Epic"` (search.go:105) — no
    casefold/trim, unlike status_equal()."""

    def test_exact_match(self):
        self.assertTrue(model.is_epic("Epic"))

    def test_case_mismatch_is_not_epic(self):
        self.assertFalse(model.is_epic("epic"))
        self.assertFalse(model.is_epic("EPIC"))

    def test_padded_whitespace_is_not_epic(self):
        self.assertFalse(model.is_epic(" Epic "))


class LookupStatusTieBreakTests(unittest.TestCase):
    """_lookup_status: on multiple case-insensitive-equal keys, the
    lexicographically smallest key's value wins, deterministically —
    independent of dict insertion order."""

    def test_smallest_key_wins_among_case_insensitive_duplicates(self):
        # Query case ("DoNe") is not itself a key, so this only matches via
        # the case-insensitive scan below — the exact-match short-circuit
        # (tested separately) never fires.
        m = {"Done": "A", "done": "B", "DONE": "C"}
        val, found = model._lookup_status(m, "DoNe")
        self.assertTrue(found)
        self.assertEqual(val, "C")  # "DONE" < "Done" < "done" lexicographically

    def test_exact_key_match_short_circuits_before_scanning(self):
        m = {"done": "exact", "DONE": "scanned"}
        val, found = model._lookup_status(m, "done")
        self.assertEqual(val, "exact")

    def test_no_match_returns_not_found(self):
        val, found = model._lookup_status({"Done": "A"}, "Mystery")
        self.assertFalse(found)
        self.assertIsNone(val)


class BuildIssueStatusFieldsTests(unittest.TestCase):
    """status_initial/status_before_end/status_end and the end-of-membership
    story_points value, exercised through build_issue() end to end."""

    def setUp(self):
        self.tl = model.SprintTimeline(start=dt(2026, 1, 5), schedule_end=dt(2026, 1, 9, 18), classify_end=dt(2026, 1, 9, 18))
        self.cfg = make_cfg(jira_status_categories={"To Do": "new", "In Progress": "indeterminate", "Done": "done"})
        self.working_days_list = model.working_days(self.tl.start, self.tl.observed_end())

    def test_committed_issue_status_fields(self):
        issue = make_issue(
            initial_status="To Do",
            created=dt(2025, 12, 1),
            status_history=[
                model.StatusChange(at=dt(2026, 1, 6), from_status="To Do", to_status="In Progress"),
                model.StatusChange(at=dt(2026, 1, 9, 10), from_status="In Progress", to_status="Done"),
            ],
        )
        intervals = [model.Interval(from_=dt(2025, 12, 20), until=None)]
        role = model.classify(issue, intervals, self.tl, self.cfg)
        built = model.build_issue(issue, intervals, role, self.tl, self.working_days_list, self.cfg)

        self.assertEqual(built.status_initial, "To Do")  # committed -> evaluated at tl.start
        self.assertEqual(built.status_before_end, "In Progress")  # end_of_day(second-to-last working day) = Jan 8 EOD
        self.assertEqual(built.status_end, "Done")  # evaluated at classify_end

    def test_added_issue_status_initial_is_evaluated_at_join_date_not_sprint_start(self):
        issue = make_issue(initial_status="In Progress", created=dt(2025, 12, 1))
        intervals = [model.Interval(from_=dt(2026, 1, 7), until=None)]  # joins Wednesday
        role = model.classify(issue, intervals, self.tl, self.cfg)
        built = model.build_issue(issue, intervals, role, self.tl, self.working_days_list, self.cfg)

        self.assertTrue(role.added)
        self.assertFalse(role.committed)
        self.assertEqual(built.status_initial, "In Progress")

    def test_story_points_reflect_value_at_membership_end_not_start_or_classify_end(self):
        issue = make_issue(
            story_points=3.0,
            sp_events=[
                model.SPChange(at=dt(2026, 1, 6, 9, 0), from_value=3.0, to_value=5.0),  # before membership ends
                model.SPChange(at=dt(2026, 1, 8, 9, 0), from_value=5.0, to_value=8.0),  # after membership ends
            ],
        )
        # Removed from the sprint at Jan 7 09:00 -> membership_end == that instant.
        intervals = [model.Interval(from_=dt(2025, 12, 20), until=dt(2026, 1, 7, 9, 0))]
        role = model.classify(issue, intervals, self.tl, self.cfg)
        built = model.build_issue(issue, intervals, role, self.tl, self.working_days_list, self.cfg)

        self.assertEqual(built.story_points, 5.0)  # not 3.0 (start value) or 8.0 (classify_end value)


if __name__ == "__main__":
    unittest.main()
