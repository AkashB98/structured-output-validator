"""Tests for schema.py: the hand-rolled JSON Schema validator."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schema import is_valid, summarize, validate
from schemas import TOOL_SCHEMAS

WO = TOOL_SCHEMAS["create_work_order"]
MSG = TOOL_SCHEMAS["send_customer_message"]
VISIT = TOOL_SCHEMAS["schedule_technician_visit"]
ENVELOPE = TOOL_SCHEMAS["tool_call"]


def paths(errors):
    return [e.path for e in errors]


class TestSchema(unittest.TestCase):
    def test_valid_full_work_order(self):
        v = {"title": "Replace smart thermostat", "priority": "high",
             "estimated_cost": 189.99, "tags": ["hvac"], "notify_customer": True}
        self.assertEqual(validate(v, WO), [])

    def test_valid_minimal_work_order(self):
        self.assertEqual(validate({"title": "Fix sink", "priority": "low"}, WO), [])

    def test_type_mismatch_message_and_path(self):
        v = {"title": "Fix sink", "priority": "low", "estimated_cost": "expensive"}
        errs = validate(v, WO)
        self.assertEqual(len(errs), 1)
        e = errs[0]
        self.assertEqual(e.path, "$.estimated_cost")
        self.assertEqual(e.message, "expected number, got string")
        self.assertEqual(e.error_class, "type_mismatch")
        self.assertEqual(e.keyword, "type")

    def test_nested_array_path(self):
        v = {"title": "Fix sink", "priority": "low", "tags": ["plumbing", 5]}
        errs = validate(v, WO)
        self.assertEqual(paths(errs), ["$.tags[1]"])
        self.assertIn("expected string, got integer", errs[0].message)

    def test_missing_required(self):
        errs = validate({"title": "Fix sink"}, WO)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].path, "$.priority")
        self.assertEqual(errs[0].error_class, "missing_required")
        self.assertIn("missing required property", errs[0].message)

    def test_additional_properties_rejected(self):
        v = {"title": "Fix sink", "priority": "low", "zzz": 1}
        errs = validate(v, WO)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].path, "$.zzz")
        self.assertEqual(errs[0].error_class, "extra_property")

    def test_pattern_failure(self):
        errs = validate({"customer_id": "C-12", "channel": "sms", "body": "hi"}, MSG)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].path, "$.customer_id")
        self.assertEqual(errs[0].error_class, "pattern")

    def test_enum_failure(self):
        errs = validate({"customer_id": "C-101", "channel": "fax", "body": "hi"}, MSG)
        self.assertEqual(errs[0].path, "$.channel")
        self.assertEqual(errs[0].error_class, "enum")

    def test_min_length(self):
        errs = validate({"title": "ab", "priority": "low"}, WO)
        self.assertEqual(errs[0].path, "$.title")
        self.assertEqual(errs[0].error_class, "length")

    def test_number_range(self):
        errs = validate({"title": "Fix sink", "priority": "low",
                         "estimated_cost": -5}, WO)
        self.assertEqual(errs[0].path, "$.estimated_cost")
        self.assertEqual(errs[0].error_class, "range")

    def test_bool_is_not_integer(self):
        # Python bool subclasses int; the validator must not treat True as 1.
        errs = validate({"work_order_id": "WO-1001", "date": "2026-10-06",
                         "window": "morning", "technician_id": True}, VISIT)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].path, "$.technician_id")
        self.assertEqual(errs[0].error_class, "type_mismatch")

    def test_bool_is_not_number(self):
        errs = validate({"title": "Fix sink", "priority": "low",
                         "estimated_cost": True}, WO)
        self.assertEqual(errs[0].error_class, "type_mismatch")

    def test_bool_is_boolean(self):
        self.assertEqual(validate({"title": "Fix sink", "priority": "low",
                                   "notify_customer": False}, WO), [])

    def test_int_counts_as_number(self):
        self.assertEqual(validate({"title": "Fix sink", "priority": "low",
                                   "estimated_cost": 50}, WO), [])

    def test_type_union_string_or_null(self):
        v = {"work_order_id": "WO-1001", "date": "2026-10-06", "window": "morning",
             "notes": None}
        self.assertEqual(validate(v, VISIT), [])
        v["notes"] = "bring ladder"
        self.assertEqual(validate(v, VISIT), [])
        v["notes"] = 5
        errs = validate(v, VISIT)
        self.assertEqual(errs[0].path, "$.notes")
        self.assertEqual(errs[0].error_class, "type_mismatch")
        self.assertIn("expected string or null, got integer", errs[0].message)

    def test_unique_items(self):
        v = {"title": "Fix sink", "priority": "low", "tags": ["a", "a"]}
        errs = validate(v, WO)
        self.assertEqual(errs[0].path, "$.tags[1]")
        self.assertEqual(errs[0].error_class, "length")

    def test_max_items(self):
        v = {"title": "Fix sink", "priority": "low",
             "tags": [f"t{i}" for i in range(9)]}
        errs = validate(v, WO)
        self.assertEqual(errs[0].path, "$.tags")
        self.assertEqual(errs[0].error_class, "length")

    def test_const(self):
        errs = validate("b", {"const": "a"})
        self.assertEqual(errs[0].error_class, "enum")

    def test_enum_json_equality_bool_vs_int(self):
        # True must not satisfy enum [1]: JSON treats them as different values.
        self.assertNotEqual(validate(True, {"enum": [1]}), [])
        self.assertEqual(validate(1, {"enum": [1]}), [])

    def test_multiple_of(self):
        self.assertEqual(validate(10, {"type": "integer", "multipleOf": 5}), [])
        self.assertEqual(validate(7, {"type": "integer", "multipleOf": 5})[0]
                         .error_class, "range")

    def test_exclusive_bounds(self):
        self.assertEqual(validate(0, {"type": "number", "exclusiveMinimum": 0})[0]
                         .keyword, "exclusiveMinimum")
        self.assertEqual(validate(0, {"type": "number", "minimum": 0}), [])

    def test_weird_key_bracket_path(self):
        errs = validate({"a.b": 1}, {"type": "object",
                                     "properties": {"a.b": {"type": "string"}}})
        self.assertEqual(errs[0].path, '$["a.b"]')

    def test_envelope_nested_required(self):
        v = {"name": "create_work_order", "arguments": {"x": 1}}
        self.assertEqual(validate(v, ENVELOPE), [])  # arguments is free-form
        errs = validate({"name": "nope", "arguments": {}}, ENVELOPE)
        self.assertEqual(errs[0].path, "$.name")
        self.assertEqual(errs[0].error_class, "enum")

    def test_type_mismatch_short_circuits_keywords(self):
        # A string where an object is expected: exactly one error, no cascade.
        errs = validate("nope", {"type": "object", "required": ["a"],
                                 "properties": {"a": {"type": "string"}}})
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].keyword, "type")

    def test_is_valid_and_summarize(self):
        self.assertTrue(is_valid({"title": "Fix sink", "priority": "low"}, WO))
        self.assertFalse(is_valid({"title": "Fix sink"}, WO))
        errs = validate({"title": "Fix sink"}, WO)
        self.assertIn("$.priority", summarize(errs))

    def test_bad_pattern_in_schema_reported_not_raised(self):
        errs = validate("x", {"type": "string", "pattern": "([a-z"})
        self.assertTrue(errs)
        self.assertEqual(errs[0].keyword, "pattern")


if __name__ == "__main__":
    unittest.main()
