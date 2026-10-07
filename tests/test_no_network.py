"""No-network enforcement: socket and urlopen are stubbed to explode.

Every offline path (validate, repair, drift, demo, CLI validate/repair/stats)
must complete without touching the network. The LLM seam is the only module
allowed near the network, and it is pinned to raise before dialing when
unconfigured.
"""

import io
import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demo
from drift import DriftTracker
from llm_seam import is_configured, regenerate
from repair import repair
from schema import validate
from schemas import TOOL_SCHEMAS

WO = TOOL_SCHEMAS["create_work_order"]
ENV_KEYS = ("STRUCT_VALIDATOR_LLM", "STRUCT_VALIDATOR_LLM_BASE_URL")


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(socket, "socket", _no_network),
            mock.patch.object(socket, "create_connection", _no_network),
            mock.patch.object(urllib.request, "urlopen", _no_network),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)
        # The LLM seam must stay unconfigured in every offline test.
        clean = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        self._env_patch = mock.patch.dict(os.environ, clean, clear=True)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _stop_patches(self):
        for p in reversed(self._patches):
            p.stop()


class TestNoNetwork(OfflineTestCase):
    def test_validate_offline(self):
        self.assertEqual(validate({"title": "Fix sink", "priority": "low"},
                                  WO), [])

    def test_repair_offline(self):
        r = repair('```json\n{"title": "Fix sink", "priority": "low",}\n```', WO)
        self.assertTrue(r.ok)

    def test_drift_offline(self):
        t = DriftTracker()
        t.record(False, validate({"title": "x"}, WO), "create_work_order")
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "d.json")
            t.save(p)
            self.assertEqual(DriftTracker.load(p).total, 1)

    def test_demo_offline(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            demo.main()  # asserts internally; must not raise
        self.assertIn("DEMO OK", buf.getvalue())

    def test_cli_validate_repair_stats_offline(self):
        from cli import main as cli_main
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.json")
            with open(good, "w") as fh:
                fh.write('{"title": "Fix sink", "priority": "low",}')
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["repair", "--schema", "create_work_order",
                               "--input", good])
            self.assertEqual(rc, 0)
            results = os.path.join(tmp, "r.jsonl")
            with open(results, "w") as fh:
                fh.write('{"ok": true, "errors": [], '
                         '"schema": "create_work_order"}\n')
            with redirect_stdout(io.StringIO()):
                rc = cli_main(["stats", "--input", results])
            self.assertEqual(rc, 0)

    def test_llm_seam_refuses_before_network(self):
        self.assertFalse(is_configured())
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            regenerate(WO, "{}", [])


if __name__ == "__main__":
    unittest.main()
