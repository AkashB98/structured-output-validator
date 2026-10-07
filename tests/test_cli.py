"""Tests for cli.py: validate / repair / stats / serve."""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import _Handler, main


def write_file(tmp, name, text):
    p = os.path.join(tmp, name)
    with open(p, "w") as fh:
        fh.write(text)
    return p


def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


class TestCliCommands(unittest.TestCase):
    def test_validate_ok_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_file(tmp, "good.json",
                           '{"title": "Fix sink", "priority": "low"}')
            rc, out = run_cli(["validate", "--schema", "create_work_order",
                               "--input", p])
        self.assertEqual(rc, 0)
        self.assertIn("VALID", out)

    def test_validate_invalid_file_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_file(tmp, "bad.json", '{"title": "Fix sink"}')
            rc, out = run_cli(["validate", "--schema", "create_work_order",
                               "--input", p])
        self.assertEqual(rc, 1)
        self.assertIn("INVALID", out)
        self.assertIn("$.priority", out)

    def test_validate_not_json_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_file(tmp, "bad.json", "not json")
            rc, _ = run_cli(["validate", "--schema", "create_work_order",
                              "--input", p])
        self.assertEqual(rc, 1)

    def test_validate_stdin(self):
        with mock.patch("sys.stdin", io.StringIO(
                '{"title": "Fix sink", "priority": "low"}')):
            rc, out = run_cli(["validate", "--schema", "create_work_order",
                               "--input", "-"])
        self.assertEqual(rc, 0)
        self.assertIn("VALID", out)

    def test_repair_fixable_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_file(tmp, "fix.json",
                           '```json\n{"title": "Fix sink", "priority": "low",}\n```')
            rc, out = run_cli(["repair", "--schema", "create_work_order",
                                "--input", p])
        self.assertEqual(rc, 0)
        self.assertIn("REPAIRED", out)
        self.assertIn("strip_code_fence", out)

    def test_repair_unfixable_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_file(tmp, "bad.json",
                           '{"title": "x", "priority": "whenever"}')
            rc, out = run_cli(["repair", "--schema", "create_work_order",
                                "--input", p, "--max-attempts", "2"])
        self.assertEqual(rc, 2)
        self.assertIn("UNREPAIRABLE after 2 attempt(s)", out)

    def test_unknown_schema_raises(self):
        with self.assertRaises(KeyError):
            run_cli(["validate", "--schema", "nope", "--input", "-"])

    def test_stats_jsonl(self):
        lines = [
            {"ok": True, "errors": [], "schema": "create_work_order"},
            {"ok": False, "schema": "create_work_order", "errors": [
                {"path": "$.priority", "message": "missing",
                 "error_class": "missing_required", "keyword": "required"}]},
            {"ok": False, "schema": "send_customer_message", "errors": [
                {"path": "$.channel", "message": "bad enum",
                 "error_class": "enum", "keyword": "enum"}]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            p = write_file(tmp, "results.jsonl",
                           "\n".join(json.dumps(l) for l in lines))
            rc, out = run_cli(["stats", "--input", p])
        self.assertEqual(rc, 0)
        summary = json.loads(out)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["field_failures"],
                         {"$.channel": 1, "$.priority": 1})
        self.assertEqual(summary["error_classes"],
                         {"enum": 1, "missing_required": 1})
        self.assertEqual(summary["by_schema"],
                         {"create_work_order": 2, "send_customer_message": 1})

    def test_stats_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write_file(tmp, "r.jsonl",
                           '{"ok": true, "errors": [], "schema": "s"}')
            out = os.path.join(tmp, "stats.json")
            rc, _ = run_cli(["stats", "--input", p, "--out", out])
            self.assertEqual(rc, 0)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["total"], 1)


class TestServe(unittest.TestCase):
    def setUp(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join()

    def _post(self, route, payload):
        req = urllib.request.Request(
            self.base + route, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())

    def _get(self, route):
        with urllib.request.urlopen(self.base + route) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_validate_repair_stats_roundtrip(self):
        status, body = self._post("/validate", {
            "schema": "create_work_order",
            "input": '{"title": "Fix sink", "priority": "low"}'})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, body = self._post("/validate", {
            "schema": "create_work_order", "input": '{"title": "Fix sink"}'})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["errors"][0]["path"], "$.priority")

        status, body = self._post("/repair", {
            "schema": "create_work_order",
            "input": '{"title": "Fix sink", "priority": "low",}'})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("fix_trailing_comma", body["repairs_applied"])

        status, body = self._get("/stats")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["total"], 3)

        status, body = self._get("/schemas")
        self.assertIn("create_work_order", body["schemas"])

    def test_bad_schema_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/validate", {"schema": "nope", "input": "{}"})
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_route_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
