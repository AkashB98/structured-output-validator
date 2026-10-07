"""Meta-test: the golden eval suite itself passes end to end."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "evals"))

from run_evals import main as run_evals_main, run_all


class TestEvals(unittest.TestCase):
    def test_all_golden_evals_pass(self):
        report = run_all()
        failed = [c["id"] for c in report["cases"] if not c["passed"]]
        self.assertTrue(report["drift_check"]["passed"],
                        report["drift_check"]["got"])
        self.assertEqual(failed, [])
        self.assertEqual(report["summary"]["failed"], 0)

    def test_eval_report_written_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = os.path.join(tmp, "r1.json")
            p2 = os.path.join(tmp, "r2.json")
            self.assertEqual(run_evals_main(p1), 0)
            self.assertEqual(run_evals_main(p2), 0)
            with open(p1, "rb") as f1, open(p2, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())


if __name__ == "__main__":
    unittest.main()
