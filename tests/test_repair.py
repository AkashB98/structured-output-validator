"""Tests for repair.py: the heuristic repair loop."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair import (coerce_types, fix_trailing_commas, parse_with_text_repairs,
                    repair, strip_code_fences, unwrap_blob, _PARSE)
from schemas import TOOL_SCHEMAS

WO = TOOL_SCHEMAS["create_work_order"]
VISIT = TOOL_SCHEMAS["schedule_technician_visit"]


class TestTextRepairs(unittest.TestCase):
    def test_strip_code_fences_json_tag(self):
        text, changed = strip_code_fences('```json\n{"a": 1}\n```')
        self.assertTrue(changed)
        self.assertEqual(text, '{"a": 1}')

    def test_strip_code_fences_plain(self):
        text, changed = strip_code_fences('```\n{"a": 1}\n```')
        self.assertTrue(changed)
        self.assertEqual(text, '{"a": 1}')

    def test_strip_code_fences_noop(self):
        text, changed = strip_code_fences('{"a": 1}')
        self.assertFalse(changed)
        self.assertEqual(text, '{"a": 1}')

    def test_fix_trailing_commas_object_and_array(self):
        text, changed = fix_trailing_commas('{"a": 1, "b": [1, 2,],}')
        self.assertTrue(changed)
        self.assertEqual(text, '{"a": 1, "b": [1, 2]}')

    def test_fix_trailing_commas_keeps_comma_inside_string(self):
        # A comma followed by } INSIDE a string literal must survive.
        text, changed = fix_trailing_commas('{"note": "ends with ,}"}')
        self.assertFalse(changed)
        self.assertEqual(text, '{"note": "ends with ,}"}')

    def test_fix_trailing_commas_noop(self):
        text, changed = fix_trailing_commas('{"a": 1}')
        self.assertFalse(changed)

    def test_parse_with_text_repairs_fenced_then_trailing_comma(self):
        value, repairs = parse_with_text_repairs('```json\n{"a": 1,}\n```')
        self.assertEqual(value, {"a": 1})
        self.assertEqual(repairs, ["strip_code_fence", "fix_trailing_comma"])

    def test_parse_with_text_repairs_unparseable(self):
        value, _ = parse_with_text_repairs("hello world")
        self.assertIs(value, _PARSE)


class TestCoercion(unittest.TestCase):
    def test_coerce_string_to_integer(self):
        v, r = coerce_types({"technician_id": "42"}, VISIT)
        self.assertEqual(v["technician_id"], 42)
        self.assertEqual(r, ["coerce $.technician_id: '42' -> integer"])

    def test_coerce_string_to_number(self):
        v, r = coerce_types({"estimated_cost": "149.50"}, WO)
        self.assertEqual(v["estimated_cost"], 149.50)
        self.assertTrue(any("number" in x for x in r))

    def test_coerce_string_to_boolean(self):
        v, _ = coerce_types({"notify_customer": "true"}, WO)
        self.assertIs(v["notify_customer"], True)
        v, _ = coerce_types({"notify_customer": "FALSE"}, WO)
        self.assertIs(v["notify_customer"], False)

    def test_coerce_string_to_null(self):
        v, _ = coerce_types({"notes": "null"}, VISIT)
        self.assertIsNone(v["notes"])

    def test_coerce_integral_float_to_integer(self):
        v, _ = coerce_types({"technician_id": 42.0}, VISIT)
        self.assertEqual(v["technician_id"], 42)
        self.assertIsInstance(v["technician_id"], int)

    def test_no_coerce_non_integral_float(self):
        # 4.5 must never silently become 4.
        v, r = coerce_types({"technician_id": 4.5}, VISIT)
        self.assertEqual(v["technician_id"], 4.5)
        self.assertEqual(r, [])

    def test_coerce_number_to_string(self):
        v, _ = coerce_types({"title": 42}, WO)
        self.assertEqual(v["title"], "42")

    def test_no_coerce_garbage_string(self):
        v, r = coerce_types({"estimated_cost": "expensive"}, WO)
        self.assertEqual(v["estimated_cost"], "expensive")
        self.assertEqual(r, [])

    def test_coerce_nested_array_items(self):
        v, _ = coerce_types({"tags": [1, "b"]}, WO)
        self.assertEqual(v["tags"], ["1", "b"])

    def test_coerce_does_not_mutate_input(self):
        original = {"estimated_cost": "10"}
        coerce_types(original, WO)
        self.assertEqual(original, {"estimated_cost": "10"})


class TestUnwrap(unittest.TestCase):
    def test_unwrap_double_encoded_blob(self):
        inner = '{"title": "Paint nursery", "priority": "medium"}'
        value, labels = unwrap_blob({"output": inner}, WO)
        self.assertEqual(value, {"title": "Paint nursery", "priority": "medium"})
        self.assertEqual(labels, ["unwrap double-encoded blob"])

    def test_unwrap_wrapper_key_dict(self):
        inner = {"title": "Paint nursery", "priority": "medium"}
        value, labels = unwrap_blob({"result": inner, "extra": 1}, WO)
        self.assertEqual(value, inner)
        self.assertEqual(labels, ["unwrap wrapper key 'result'"])

    def test_unwrap_rejects_non_json_wrapper(self):
        outer = {"output": "not json", "title": "Fix sink", "priority": "low"}
        value, labels = unwrap_blob(outer, WO)
        self.assertEqual(value, outer)
        self.assertEqual(labels, [])

    def test_unwrap_noop_when_already_valid(self):
        valid = {"title": "Fix sink", "priority": "low"}
        value, labels = unwrap_blob(valid, WO)
        self.assertEqual(value, valid)
        self.assertEqual(labels, [])

    def test_unwrap_prefers_fewest_errors(self):
        inner = '{"title": "Paint nursery", "priority": "medium"}'
        outer = {"output": inner, "zzz": 1}  # missing required + extra property
        value, labels = unwrap_blob(outer, WO)
        self.assertEqual(value, {"title": "Paint nursery", "priority": "medium"})
        self.assertTrue(labels)


class TestRepairLoop(unittest.TestCase):
    def test_repair_valid_input_noop(self):
        r = repair('{"title": "Fix sink", "priority": "low"}', WO)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 1)
        self.assertEqual(r.repairs_applied, [])
        self.assertEqual(r.value, {"title": "Fix sink", "priority": "low"})

    def test_repair_fenced(self):
        r = repair('```json\n{"title": "Fix porch light", "priority": "low"}\n```', WO)
        self.assertTrue(r.ok)
        self.assertIn("strip_code_fence", r.repairs_applied)

    def test_repair_trailing_comma_and_coercion(self):
        raw = ('{"title": "Unclog drain", "priority": "medium", '
               '"estimated_cost": "149.50",}')
        r = repair(raw, WO)
        self.assertTrue(r.ok)
        self.assertEqual(r.value["estimated_cost"], 149.50)
        self.assertIn("fix_trailing_comma", r.repairs_applied)
        self.assertTrue(any("coerce $.estimated_cost" in x
                            for x in r.repairs_applied))

    def test_repair_double_encoded_blob_records_label(self):
        raw = '{"output": "{\\"title\\": \\"Paint\\", \\"priority\\": \\"low\\"}"}'
        r = repair(raw, WO)
        self.assertTrue(r.ok)
        self.assertTrue(any("unwrap" in x for x in r.repairs_applied),
                        r.repairs_applied)

    def test_repair_unfixable_runs_all_attempts(self):
        raw = '{"title": "x", "priority": "whenever"}'  # minLength + enum
        r = repair(raw, WO, max_attempts=3)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 3)
        self.assertEqual(len(r.errors), 2)
        self.assertEqual(r.repairs_applied, [])

    def test_repair_not_json_fails_cleanly(self):
        r = repair("hello world", WO, max_attempts=2)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.errors[0].error_class, "json_parse")

    def test_repair_missing_required_unfixable(self):
        r = repair('{"title": "Fix sink"}', WO, max_attempts=3)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 3)
        self.assertTrue(any(e.error_class == "missing_required"
                            for e in r.errors))

    def test_repair_report_to_dict(self):
        r = repair("hello", WO, max_attempts=1)
        d = r.to_dict()
        self.assertFalse(d["ok"])
        self.assertEqual(d["attempts"], 1)
        self.assertEqual(d["errors"][0]["error_class"], "json_parse")


if __name__ == "__main__":
    unittest.main()
