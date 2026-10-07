"""Tests for drift.py: the drift-stats tracker."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift import DriftTracker
from schema import validate
from schemas import TOOL_SCHEMAS

WO = TOOL_SCHEMAS["create_work_order"]


def _errs(value):
    return validate(value, WO)


class TestDrift(unittest.TestCase):
    def test_record_pass_and_fail(self):
        t = DriftTracker()
        t.record(True, [], "create_work_order")
        t.record(False, _errs({"title": "Fix sink"}), "create_work_order")
        self.assertEqual((t.total, t.passed, t.failed), (2, 1, 1))
        self.assertEqual(t.pass_rate(), 0.5)
        self.assertEqual(t.fail_rate(), 0.5)

    def test_field_failure_counts(self):
        t = DriftTracker()
        t.record(False, _errs({"title": "x"}), "create_work_order")  # $.title length
        t.record(False, _errs({"title": "y"}), "create_work_order")
        t.record(False, _errs({"title": "Fix sink", "priority": "low",
                               "estimated_cost": "expensive"}), "create_work_order")
        self.assertEqual(t.field_failures["$.title"], 2)
        self.assertEqual(t.field_failures["$.estimated_cost"], 1)
        self.assertEqual(t.error_classes["length"], 2)
        self.assertEqual(t.error_classes["type_mismatch"], 1)

    def test_top_fields_and_classes_ordered(self):
        t = DriftTracker()
        for _ in range(3):
            t.record(False, _errs({"title": "x"}), "s")
        t.record(False, _errs({"title": "Fix sink", "priority": "low",
                               "estimated_cost": -1}), "s")
        s = t.summary()
        # ties broken deterministically: count desc, then path asc
        self.assertEqual(s["top_fields"],
                         ["$.priority", "$.title", "$.estimated_cost"])
        self.assertEqual(s["top_error_classes"],
                         ["length", "missing_required", "range"])

    def test_summary_deterministic_and_sorted(self):
        t = DriftTracker()
        t.record(False, _errs({"title": "Fix sink", "priority": "low",
                               "zzz": 1}), "b")
        t.record(False, _errs({"title": "x"}), "a")
        s1 = t.summary()
        s2 = t.summary()
        self.assertEqual(s1, s2)
        self.assertEqual(list(s1["field_failures"]),
                         sorted(s1["field_failures"]))
        self.assertEqual(list(s1["error_classes"]), sorted(s1["error_classes"]))
        self.assertEqual(list(s1["by_schema"]), ["a", "b"])

    def test_empty_tracker_zero_rates(self):
        t = DriftTracker()
        s = t.summary()
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["pass_rate"], 0.0)
        self.assertEqual(s["fail_rate"], 0.0)

    def test_save_load_roundtrip(self):
        t = DriftTracker()
        t.record(True, [], "a")
        t.record(False, _errs({"title": "x"}), "a")
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "drift.json")
            t.save(p)
            t2 = DriftTracker.load(p)
        self.assertEqual(t2.summary(), t.summary())

    def test_merge(self):
        a, b = DriftTracker(), DriftTracker()
        a.record(False, _errs({"title": "x"}), "s")
        b.record(True, [], "s")
        b.record(False, _errs({"title": "Fix sink"}), "s")
        a.merge(b)
        self.assertEqual(a.total, 3)
        self.assertEqual(a.passed, 1)
        self.assertEqual(a.failed, 2)
        self.assertEqual(a.field_failures["$.title"], 1)
        # {"title": "x"} also misses required $.priority, so it counts twice
        self.assertEqual(a.field_failures["$.priority"], 2)
        self.assertEqual(a.by_schema["s"], 3)
        self.assertEqual(a.schema_failures["s"], 2)

    def test_record_without_schema_name(self):
        t = DriftTracker()
        t.record(True, [])
        self.assertEqual(t.total, 1)
        self.assertEqual(dict(t.by_schema), {})


if __name__ == "__main__":
    unittest.main()
