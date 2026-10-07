"""Tests for llm_seam.py: the optional LLM regeneration seam.

The seam must NEVER fire in tests/evals/demo. These tests pin that contract:
unconfigured -> RuntimeError without touching the network; configured ->
it really would call the network (proved with a stubbed transport).
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_seam
from schema import validate
from schemas import TOOL_SCHEMAS

WO = TOOL_SCHEMAS["create_work_order"]
ENV_KEYS = ("STRUCT_VALIDATOR_LLM", "STRUCT_VALIDATOR_LLM_BASE_URL",
            "STRUCT_VALIDATOR_LLM_API_KEY", "STRUCT_VALIDATOR_LLM_MODEL")


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    return mock.patch.dict(os.environ, env, clear=True)


class TestLlmSeam(unittest.TestCase):
    def test_not_configured_raises_without_network(self):
        with clean_env():
            self.assertFalse(llm_seam.is_configured())
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                llm_seam.regenerate(WO, "{}", validate({}, WO))

    def test_enabled_but_no_url_still_raises(self):
        with clean_env(), mock.patch.dict(os.environ,
                                           {"STRUCT_VALIDATOR_LLM": "1"}):
            self.assertFalse(llm_seam.is_configured())
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                llm_seam.regenerate({}, "{}", [])

    def test_configured_would_call_network(self):
        # With config present, the seam builds a real HTTP request; the
        # stubbed transport proves the call path without any actual I/O.
        env = {"STRUCT_VALIDATOR_LLM": "1",
               "STRUCT_VALIDATOR_LLM_BASE_URL": "https://llm.example/v1",
               "STRUCT_VALIDATOR_LLM_API_KEY": "test-key"}
        with clean_env(), mock.patch.dict(os.environ, env):
            self.assertTrue(llm_seam.is_configured())
            calls = []

            class FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return (b'{"choices": [{"message": {"content": '
                            b'"{\\"title\\": \\"Fix sink\\", '
                            b'\\"priority\\": \\"low\\"}"}}]}')

            def fake_urlopen(req, timeout=None):
                calls.append(req)
                return FakeResp()

            with mock.patch("urllib.request.urlopen", fake_urlopen):
                out = llm_seam.regenerate({"type": "object"},
                                          '{"title": 1}', [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].full_url,
                         "https://llm.example/v1/chat/completions")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer test-key")
        self.assertEqual(out, '{"title": "Fix sink", "priority": "low"}')


if __name__ == "__main__":
    unittest.main()
