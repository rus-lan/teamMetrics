"""Coverage for labels_ru.py — the single Russian text dictionary (SPEC §E)."""

import _pathfix  # noqa: F401

import ast
import pathlib
import re
import unittest

from team_metrics import engineering_metrics, forecast, gitlab_client, jira_client, labels_ru, metrics, model, personal_metrics


class MetricDefsTests(unittest.TestCase):
    def test_exactly_28_entries(self):
        self.assertEqual(len(labels_ru.METRIC_DEFS_RU), 28)

    def test_every_entry_has_non_empty_label_unit_comment(self):
        for entry in labels_ru.METRIC_DEFS_RU:
            self.assertTrue(entry["key"])
            self.assertTrue(entry["label_ru"])
            self.assertTrue(entry["unit_ru"])
            self.assertTrue(entry["comment_ru"])
            self.assertIn(entry["category"], ("gl", "jira", "link", "infra"))
            self.assertIn(entry["scope"], ("person", "team", "both"))
            self.assertIsInstance(entry["is_pct"], bool)

    def test_keys_are_unique(self):
        keys = [d["key"] for d in labels_ru.METRIC_DEFS_RU]
        self.assertEqual(len(keys), len(set(keys)))

    def test_infra_rows_are_team_scoped(self):
        for d in labels_ru.METRIC_DEFS_RU:
            if d["category"] == "infra":
                self.assertEqual(d["scope"], "team")

    def test_spot_check_verbatim_labels(self):
        by_key = {d["key"]: d for d in labels_ru.METRIC_DEFS_RU}
        self.assertEqual(by_key["mr_count"]["label_ru"], "Число MR")
        self.assertEqual(by_key["coverage_avg_pct"]["label_ru"], "Покрытие тестами")
        self.assertEqual(by_key["rework_rate_pct"]["unit_ru"], "%")


class MetricLabelRuTests(unittest.TestCase):
    def test_falls_back_to_column_labels(self):
        self.assertEqual(labels_ru.metric_label_ru("sprint"), "Спринт")

    def test_falls_back_to_key_itself_when_unmapped(self):
        self.assertEqual(labels_ru.metric_label_ru("totally_unknown_key"), "totally_unknown_key")

    def test_prefers_metric_defs_over_columns(self):
        self.assertEqual(labels_ru.metric_label_ru("mr_count"), "Число MR")


class WarnMessageTests(unittest.TestCase):
    def test_known_code_maps_to_exact_text(self):
        self.assertEqual(
            labels_ru.warn_message("WARN_DIVISION_BY_ZERO"),
            "Знаменатель формулы равен нулю — метрика обнулена.",
        )
        self.assertEqual(
            labels_ru.warn_message("WARN_BASELINE_SHORT"),
            "Закрытых спринтов для базы меньше 5 — SMA5/загрузка считаются по имеющимся.",
        )

    def test_unknown_code_falls_back_without_raising(self):
        text = labels_ru.warn_message("SOME_MADE_UP_CODE")
        self.assertIn("SOME_MADE_UP_CODE", text)
        self.assertTrue(text.startswith("Предупреждение:"))

    def test_warning_obj_shape(self):
        obj = labels_ru.warning_obj("WARN_BASELINE_SHORT")
        self.assertEqual(set(obj), {"code", "message_ru", "detail"})
        self.assertIsNone(obj["detail"])

    def test_warning_obj_from_suffixed_splits_metric_name_into_detail(self):
        obj = labels_ru.warning_obj_from_suffixed("WARN_DIVISION_BY_ZERO:mr_merge_rate_pct")
        self.assertEqual(obj["code"], "WARN_DIVISION_BY_ZERO")
        self.assertEqual(obj["detail"], "Доля merge-запросов")

    def test_warning_obj_from_suffixed_bare_code_has_null_detail(self):
        obj = labels_ru.warning_obj_from_suffixed("WARN_DIFF_STATS_UNAVAILABLE")
        self.assertIsNone(obj["detail"])


def _literal_warn_err_codes_from_source(*modules) -> set:
    """Collects every `WARN_*`/`ERR_*` string literal assigned to a
    module-level constant or passed as a literal argument anywhere in the
    given modules' source — a static, no-network way to enumerate every code
    the package can actually emit."""
    pattern = re.compile(r"^(WARN|ERR)_[A-Z0-9_]+$")
    codes: set = set()
    for mod in modules:
        source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and pattern.match(node.value):
                codes.add(node.value)
    return codes


class WarnCodeCoverageTests(unittest.TestCase):
    """Every WARN_*/ERR_* literal found in the package's own source must be
    mapped by warn_message() — a code with no Russian text would reach a
    report as a raw string, which §B.4 forbids."""

    def test_every_emitted_code_is_mapped(self):
        codes = _literal_warn_err_codes_from_source(
            model, metrics, personal_metrics, engineering_metrics, forecast, gitlab_client, jira_client, labels_ru
        )
        # ':'-suffixed codes (personal_metrics/engineering_metrics's own
        # "<CODE>:<metric_name>" convention) are handled by
        # warning_obj_from_suffixed(), not warn_message() directly, and are
        # already covered by their own bare-code entry.
        missing = [c for c in sorted(codes) if c not in labels_ru.WARN_ERR_RU]
        self.assertEqual(missing, [], f"codes with no Russian text: {missing}")


class RoleLabelTests(unittest.TestCase):
    def test_all_six_roles_present(self):
        for code in ("FE", "BE", "BA", "SA", "QA", "TL"):
            self.assertIn(code, labels_ru.ROLES_RU)
            self.assertTrue(labels_ru.ROLES_RU[code])

    def test_unknown_role_returned_as_is(self):
        self.assertEqual(labels_ru.role_label_ru("XX"), "XX")

    def test_known_role_resolves(self):
        self.assertEqual(labels_ru.role_label_ru("BE"), "Backend-разработчик")


class StatusCategoryLabelTests(unittest.TestCase):
    def test_five_categories_present(self):
        for cat in ("new", "indeterminate", "done", "cancelled", "unmapped"):
            self.assertIn(cat, labels_ru.STATUS_CATEGORIES_RU)

    def test_empty_category_maps_to_unmapped(self):
        self.assertEqual(labels_ru.status_category_label_ru(""), "Не сопоставлено")


class GlossaryTests(unittest.TestCase):
    def test_19_entries_12_verbatim_plus_7_additions(self):
        self.assertEqual(len(labels_ru.GLOSSARY_RU), 19)

    def test_every_entry_has_term_and_definition(self):
        for entry in labels_ru.GLOSSARY_RU:
            self.assertTrue(entry["term"])
            self.assertTrue(entry["definition_ru"])

    def test_terms_are_unique(self):
        terms = [e["term"] for e in labels_ru.GLOSSARY_RU]
        self.assertEqual(len(terms), len(set(terms)))


class RiskTextTests(unittest.TestCase):
    def test_format_pct1_strips_trailing_zero(self):
        self.assertEqual(labels_ru.format_pct1(55.0), "55")
        self.assertEqual(labels_ru.format_pct1(63.4), "63.4")
        self.assertEqual(labels_ru.format_pct1(51.16), "51.2")

    def test_risk_body_defect_rate_substitutes_value_never_multiplied(self):
        body = labels_ru.risk_body_defect_rate_ru(23.5)
        self.assertIn("23.5%", body)

    def test_risk_body_coverage_uses_value_as_is_not_multiplied_by_100(self):
        # Regression for source bug (a): a coverage of 55.0 (already 0..100)
        # must print "55%", never "5500%".
        body = labels_ru.risk_body_coverage_ru(55.0)
        self.assertIn("55%", body)
        self.assertNotIn("5500", body)

    def test_all_ok_and_titles_present(self):
        for key in ("speed_vs_quality", "defect_rate", "rework", "coverage", "all_ok"):
            self.assertIn(key, labels_ru.RISK_TITLES_RU)
            self.assertTrue(labels_ru.RISK_TITLES_RU[key])


class ColumnLabelsCompletenessTests(unittest.TestCase):
    """Finding #9: COLUMN_LABELS_RU must carry the sprints[].metrics key
    names verbatim (not just the CSV-header aliases), plus the person-only
    extras §E.1 lists."""

    def test_sprint_metrics_keys_have_russian_labels(self):
        for key in (
            "scope_added_sp", "scope_removed_sp", "scope_estimation_change_sp",
            "velocity_sma5_sp", "throughput_items", "closure_pct_items", "closure_pct_sp",
            "scope_added_items", "scope_removed_items",
        ):
            self.assertIn(key, labels_ru.COLUMN_LABELS_RU)
            self.assertTrue(labels_ru.COLUMN_LABELS_RU[key])

    def test_aliases_match_their_csv_header_counterpart(self):
        self.assertEqual(labels_ru.COLUMN_LABELS_RU["scope_added_sp"], labels_ru.COLUMN_LABELS_RU["added_sp"])
        self.assertEqual(labels_ru.COLUMN_LABELS_RU["scope_removed_sp"], labels_ru.COLUMN_LABELS_RU["removed_sp"])
        self.assertEqual(labels_ru.COLUMN_LABELS_RU["velocity_sma5_sp"], labels_ru.COLUMN_LABELS_RU["sma5_sp"])
        self.assertEqual(labels_ru.COLUMN_LABELS_RU["throughput_items"], labels_ru.COLUMN_LABELS_RU["throughput_count"])
        self.assertEqual(labels_ru.COLUMN_LABELS_RU["closure_pct_items"], labels_ru.COLUMN_LABELS_RU["closure_rate_count_pct"])
        self.assertEqual(labels_ru.COLUMN_LABELS_RU["closure_pct_sp"], labels_ru.COLUMN_LABELS_RU["closure_rate_sp_pct"])

    def test_personal_pipeline_success_rate_label_is_distinct_from_the_team_infra_one(self):
        self.assertEqual(labels_ru.COLUMN_LABELS_RU["pipeline_success_rate_pct"], "Доля успешных пайплайнов (личная)")
        team_row = next(d for d in labels_ru.METRIC_DEFS_RU if d["key"] == "pipeline_success_rate_pct")
        self.assertEqual(team_row["label_ru"], "Доля успешных пайплайнов")
        self.assertNotEqual(team_row["label_ru"], labels_ru.COLUMN_LABELS_RU["pipeline_success_rate_pct"])

    def test_mr_commits_sum_label_present(self):
        self.assertIn("mr_commits_sum", labels_ru.COLUMN_LABELS_RU)
        self.assertTrue(labels_ru.COLUMN_LABELS_RU["mr_commits_sum"])


if __name__ == "__main__":
    unittest.main()
