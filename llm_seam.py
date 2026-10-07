"""Optional LLM regeneration seam: last resort after heuristics fail.

When the heuristic repair loop cannot fix an output (missing required fields,
values outside the enum, genuinely wrong content), this seam can ask a model
to regenerate the output from the schema plus the validation errors. It is
NEVER used in tests, evals, or the demo -- those run fully offline.

Configuration (all via environment, no config files, no keys in code):
    STRUCT_VALIDATOR_LLM=1                 enable the seam
    STRUCT_VALIDATOR_LLM_BASE_URL          OpenAI-compatible base URL,
                                           e.g. https://api.openai.com/v1
    STRUCT_VALIDATOR_LLM_API_KEY           bearer token for the endpoint
    STRUCT_VALIDATOR_LLM_MODEL             model name (default: gpt-4o-mini)

The prompt is deliberately small: schema + broken output + error list, with a
hard instruction to return JSON only.
"""

from __future__ import annotations

import json
import os
import urllib.request

from schema import ValidationError


def is_configured() -> bool:
    """True only when the operator explicitly enabled the seam AND gave a URL."""
    return os.environ.get("STRUCT_VALIDATOR_LLM") == "1" and bool(
        os.environ.get("STRUCT_VALIDATOR_LLM_BASE_URL"))


def regenerate(schema: dict, raw_output: str,
               errors: list[ValidationError]) -> str:
    """Ask the configured model to regenerate a valid output. Returns raw text.

    Raises RuntimeError when the seam is not configured. May raise
    urllib.error.URLError on transport failure -- callers decide retry policy.
    """
    if not is_configured():
        raise RuntimeError(
            "LLM regeneration seam not configured: set STRUCT_VALIDATOR_LLM=1 "
            "and STRUCT_VALIDATOR_LLM_BASE_URL (plus STRUCT_VALIDATOR_LLM_API_KEY).")
    base_url = os.environ["STRUCT_VALIDATOR_LLM_BASE_URL"].rstrip("/")
    api_key = os.environ.get("STRUCT_VALIDATOR_LLM_API_KEY", "")
    model = os.environ.get("STRUCT_VALIDATOR_LLM_MODEL", "gpt-4o-mini")

    error_lines = "\n".join(f"- {e.path}: {e.message}" for e in errors)
    prompt = (
        "You are a JSON repair assistant. The following tool-call output failed "
        "validation against its JSON Schema. Regenerate it so it validates, "
        "changing as little as possible. Return ONLY the JSON object, no "
        "markdown fences, no commentary.\n\n"
        f"SCHEMA:\n{json.dumps(schema, indent=2)}\n\n"
        f"BROKEN OUTPUT:\n{raw_output}\n\n"
        f"VALIDATION ERRORS:\n{error_lines}"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode())
    return payload["choices"][0]["message"]["content"]
