import _pathfix  # noqa: F401

import csv
import json
import logging
import tempfile
import unittest
from pathlib import Path

from team_metrics import out_writer


class EnsureOutDirTests(unittest.TestCase):
    def test_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b" / "c"
            result = out_writer.ensure_out_dir(target)
            self.assertTrue(target.is_dir())
            self.assertEqual(result, target)

    def test_existing_directory_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_writer.ensure_out_dir(tmp)
            out_writer.ensure_out_dir(tmp)  # must not raise


class CheckSafeFilenameTests(unittest.TestCase):
    """check_safe_filename is public (cli.py validates a bare --out/
    --json-out value with it before any network work begins) — covered
    directly, not just through the writers that also call it internally."""

    def test_plain_name_is_accepted(self):
        out_writer.check_safe_filename("report.html")  # must not raise

    def test_rejects_empty_path_separator_and_dotdot(self):
        for bad_name in ("", "a/b", "a\\b", "..", "../x"):
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(ValueError):
                    out_writer.check_safe_filename(bad_name)


class WriteCsvTests(unittest.TestCase):
    def test_field_union_across_heterogeneous_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                {"a": "1", "b": "2"},
                {"a": "3", "c": "4"},  # "c" first seen here, appended after b
            ]
            path = out_writer.write_csv(tmp, "data.csv", rows)
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                header = next(reader)
                data_rows = list(reader)
        self.assertEqual(header, ["a", "b", "c"])
        self.assertEqual(data_rows[0], ["1", "2", ""])
        self.assertEqual(data_rows[1], ["3", "", "4"])

    def test_empty_rows_returns_none_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("team_metrics.out_writer", level="WARNING"):
                result = out_writer.write_csv(tmp, "data.csv", [])
            self.assertIsNone(result)
            self.assertFalse((Path(tmp) / "data.csv").exists())

    def test_utf8_content_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"name": "Иванов", "note": "café"}]
            path = out_writer.write_csv(tmp, "data.csv", rows)
            text = path.read_text(encoding="utf-8")
        self.assertIn("Иванов", text)
        self.assertIn("café", text)

    def test_rejects_unsafe_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad_name in ("../escape.csv", "a/b.csv", "a\\b.csv", ".."):
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(ValueError):
                        out_writer.write_csv(tmp, bad_name, [{"a": "1"}])

    def test_rejects_writing_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "IMPORTANT.txt"
            target.write_text("do not touch", encoding="utf-8")
            link = Path(tmp) / "data.csv"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                out_writer.write_csv(tmp, "data.csv", [{"a": "1"}])
            self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")

    def test_formula_leading_cell_is_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"a": '=HYPERLINK("http://evil/","click")', "b": "+1", "c": "-1", "d": "@cmd", "e": "safe"}]
            path = out_writer.write_csv(tmp, "data.csv", rows)
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                next(reader)
                data_row = next(reader)
        self.assertEqual(data_row[0], '\'=HYPERLINK("http://evil/","click")')
        self.assertEqual(data_row[1], "'+1")
        self.assertEqual(data_row[2], "'-1")
        self.assertEqual(data_row[3], "'@cmd")
        self.assertEqual(data_row[4], "safe")

    def test_formula_guard_leaves_non_string_cells_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"n": 42, "f": 3.5, "none": None}]
            path = out_writer.write_csv(tmp, "data.csv", rows)
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.reader(f)
                next(reader)
                data_row = next(reader)
        self.assertEqual(data_row, ["42", "3.5", ""])


class WriteJsonTests(unittest.TestCase):
    def test_insertion_order_and_non_ascii_preserved(self):
        # Key order must be preserved exactly as given -- NEVER alphabetized.
        # report_data.py builds report.json's dicts in a fixed, deliberate
        # order (e.g. labels.roles: FE/BE/BA/SA/QA/TL, SPEC §E.5); at least
        # one render (tab 09's roles table) reads that dict in iteration
        # order, so a `run` and a later `report` on the very file it wrote
        # only agree byte-for-byte if this writer keeps that order untouched.
        with tempfile.TemporaryDirectory() as tmp:
            obj = {"z": 1, "a": "Привет", "m": 2}
            path = out_writer.write_json(tmp, "data.json", obj)
            text = path.read_text(encoding="utf-8")
        self.assertLess(text.index('"z"'), text.index('"a"'))
        self.assertLess(text.index('"a"'), text.index('"m"'))
        self.assertIn("Привет", text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), obj)

    def test_rejects_unsafe_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                out_writer.write_json(tmp, "../escape.json", {})

    def test_rejects_writing_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "IMPORTANT.txt"
            target.write_text("do not touch", encoding="utf-8")
            (Path(tmp) / "data.json").symlink_to(target)
            with self.assertRaises(ValueError):
                out_writer.write_json(tmp, "data.json", {"a": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")


class WriteTextTests(unittest.TestCase):
    def test_writes_text_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = out_writer.write_text(tmp, "notes.txt", "hello\nworld")
            self.assertEqual(path.read_text(encoding="utf-8"), "hello\nworld")

    def test_rejects_writing_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "IMPORTANT.txt"
            target.write_text("do not touch", encoding="utf-8")
            (Path(tmp) / "report.html").symlink_to(target)
            with self.assertRaises(ValueError):
                out_writer.write_text(tmp, "report.html", "<html></html>")
            self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")

    def test_rejects_unsafe_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                out_writer.write_text(tmp, "sub/dir.txt", "x")


class WriteRawTests(unittest.TestCase):
    def test_lands_under_raw_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = out_writer.write_raw(tmp, "payload.json", {"a": 1})
            self.assertEqual(path.parent, Path(tmp) / "raw")
            self.assertTrue(path.exists())

    def test_scrubs_secret_shaped_keys_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = {"user": "alice", "Authorization": "Bearer super-secret-token"}
            path = out_writer.write_raw(tmp, "payload.json", obj)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("super-secret-token", text)
        self.assertIn('"***"', text)

    def test_rejects_unsafe_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                out_writer.write_raw(tmp, "../payload.json", {})


class ScrubTests(unittest.TestCase):
    def test_drops_every_secret_like_key_at_top_level(self):
        obj = {
            "token": "x",
            "password": "x",
            "secret": "x",
            "authorization": "x",
            "apikey": "x",
            "credential": "x",
            "private-token": "x",
            "keep_me": "x",
        }
        scrubbed = out_writer.scrub(obj)
        for key in obj:
            if key == "keep_me":
                self.assertEqual(scrubbed[key], "x")
            else:
                self.assertEqual(scrubbed[key], "***", key)

    def test_various_casing_and_separators_all_caught(self):
        for bad_key in ("PRIVATE_TOKEN", "Access-Token", "API_KEY", "apiKey", "Authorization"):
            with self.subTest(bad_key=bad_key):
                scrubbed = out_writer.scrub({bad_key: "x"})
                self.assertEqual(scrubbed[bad_key], "***")

    def test_recurses_into_nested_dicts(self):
        obj = {"outer": {"inner": {"token": "leak-me", "safe": "keep-me"}}}
        scrubbed = out_writer.scrub(obj)
        self.assertEqual(scrubbed["outer"]["inner"]["token"], "***")
        self.assertEqual(scrubbed["outer"]["inner"]["safe"], "keep-me")

    def test_recurses_into_lists(self):
        obj = {"items": [{"token": "leak-me"}, {"safe": "keep-me"}]}
        scrubbed = out_writer.scrub(obj)
        self.assertEqual(scrubbed["items"][0]["token"], "***")
        self.assertEqual(scrubbed["items"][1]["safe"], "keep-me")

    def test_recurses_into_lists_nested_in_dicts_nested_in_lists(self):
        obj = [{"outer": [{"credential": "leak-me"}]}]
        scrubbed = out_writer.scrub(obj)
        self.assertEqual(scrubbed[0]["outer"][0]["credential"], "***")

    def test_non_dict_non_list_values_pass_through_unchanged(self):
        self.assertEqual(out_writer.scrub("plain string"), "plain string")
        self.assertEqual(out_writer.scrub(42), 42)
        self.assertIsNone(out_writer.scrub(None))


class LoggingTests(unittest.TestCase):
    def test_write_json_logs_created_at_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("team_metrics.out_writer", level="INFO") as cm:
                out_writer.write_json(tmp, "data.json", {"a": 1})
            self.assertTrue(any("Создан" in line for line in cm.output))

    def test_write_raw_logs_created_at_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("team_metrics.out_writer", level=logging.INFO) as cm:
                out_writer.write_raw(tmp, "data.json", {"a": 1})
            self.assertTrue(any("Создан" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
