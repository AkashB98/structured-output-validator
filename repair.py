"""Heuristic repair loop for almost-right LLM JSON outputs.

An LLM tool call comes back as text. Common failure shapes:
  1. wrapped in a markdown code fence (```json ... ```)
  2. trailing commas ({"a": 1,})
  3. double-encoded / blob-wrapped ({"output": "{\\"a\\": 1}"})
  4. obvious type slips ("42" where the schema wants 42, "true" for true)

``repair`` applies these fixes in order, re-validates after each pass, and
gives up cleanly after ``max_attempts`` cycles. No model calls -- heuristics
only, so it runs fully offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from schema import ValidationError, validate

_PARSE = object()  # sentinel: JSON parsing failed even after text repairs


@dataclass
class RepairReport:
    ok: bool                       # True when the output now validates
    value: Any = None              # the repaired (decoded) value, or best effort
    errors: list[ValidationError] = field(default_factory=list)
    repairs_applied: list[str] = field(default_factory=list)
    attempts: int = 0              # validate cycles actually run

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "value": self.value,
            "errors": [e.to_dict() for e in self.errors],
            "repairs_applied": self.repairs_applied,
            "attempts": self.attempts,
        }


# ---------------------------------------------------------------------------
# text-level repairs (run before json.loads)
# ---------------------------------------------------------------------------

def strip_code_fences(text: str) -> tuple[str, bool]:
    """Remove ```json ... ``` / ``` ... ``` wrappers. Returns (text, changed)."""
    stripped = text.strip()
    m = re.match(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    if m:
        return m.group(1).strip(), True
    return text, False


def fix_trailing_commas(text: str) -> tuple[str, bool]:
    """Drop `,}` / `,]` sequences, string-aware (commas inside strings kept)."""
    out: list[str] = []
    changed = False
    in_string = False
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                changed = True   # swallow the comma
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def _try_parse(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _PARSE


def parse_with_text_repairs(text: str) -> tuple[Any, list[str]]:
    """Parse JSON, applying fence-stripping and trailing-comma fixes as needed."""
    repairs: list[str] = []
    value = _try_parse(text)
    if value is not _PARSE:
        return value, repairs
    candidate, changed = strip_code_fences(text)
    if changed:
        repairs.append("strip_code_fence")
        value = _try_parse(candidate)
        if value is not _PARSE:
            return value, repairs
    candidate2, changed2 = fix_trailing_commas(candidate if changed else text)
    if changed2:
        repairs.append("fix_trailing_comma")
        value = _try_parse(candidate2)
        if value is not _PARSE:
            return value, repairs
    return _PARSE, repairs


# ---------------------------------------------------------------------------
# value-level repairs (run on the decoded value, guided by the schema)
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_TRUE_WORDS = {"true", "1", "yes", "y"}
_FALSE_WORDS = {"false", "0", "no", "n"}


def _coerce_scalar(value: Any, type_names: list[str], path: str,
                   repairs: list[str]) -> tuple[Any, bool]:
    """Try obvious scalar coercions. Returns (new_value, changed)."""
    changed = False

    def note(frm: str, to: str):
        repairs.append(f"coerce {path}: {frm} -> {to}")

    # --- string -> number / integer ---
    if isinstance(value, str):
        s = value.strip()
        if "integer" in type_names and _INT_RE.match(s):
            note(f"{value!r}", "integer"); return int(s), True
        if "number" in type_names and _FLOAT_RE.match(s):
            note(f"{value!r}", "number"); return float(s), True
        if "boolean" in type_names and s.lower() in _TRUE_WORDS | _FALSE_WORDS:
            note(f"{value!r}", "boolean"); return s.lower() in _TRUE_WORDS, True
        if "null" in type_names and s.lower() in {"null", "none", ""}:
            note(f"{value!r}", "null"); return None, True
        return value, False

    # --- number -> integer (only when integral, so 4.5 never becomes 4) ---
    if isinstance(value, float) and not isinstance(value, bool) \
            and "integer" in type_names and value.is_integer():
        note(f"{value!r}", "integer"); return int(value), True

    # --- number / boolean -> string ---
    if isinstance(value, (int, float, bool)) and "string" in type_names:
        note(f"{value!r}", "string"); return str(value), True

    return value, changed


def _declared_types(subschema: Any) -> list[str] | None:
    if not isinstance(subschema, dict):
        return None
    declared = subschema.get("type")
    if declared is None:
        return None
    return declared if isinstance(declared, list) else [declared]


def coerce_types(value: Any, schema: Any, path: str = "$",
                 repairs: list[str] | None = None) -> tuple[Any, list[str]]:
    """Walk *value* alongside *schema*, coercing obvious type slips in place.

    Returns (possibly new value, repairs list). Pure: never mutates inputs.
    """
    if repairs is None:
        repairs = []
    if not isinstance(schema, dict):
        return value, repairs

    type_names = _declared_types(schema)
    if type_names is not None and not isinstance(value, (dict, list)):
        new_value, changed = _coerce_scalar(value, type_names, path, repairs)
        if changed:
            return new_value, repairs
        return value, repairs

    if isinstance(value, dict):
        props = schema.get("properties", {})
        out = {}
        for key, item in value.items():
            subschema = props.get(key)
            new_item, _ = coerce_types(item, subschema, _join_path(path, key), repairs) \
                if isinstance(subschema, dict) else (item, repairs)
            out[key] = new_item
        # additionalProperties as a schema also guides coercion
        addl = schema.get("additionalProperties")
        if isinstance(addl, dict):
            for key in out:
                if key not in props:
                    out[key], _ = coerce_types(out[key], addl, _join_path(path, key), repairs)
        return out, repairs

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            return [coerce_types(item, items, f"{path}[{i}]", repairs)[0]
                    for i, item in enumerate(value)], repairs
        if isinstance(items, list):
            return [coerce_types(item, items[i], f"{path}[{i}]", repairs)[0]
                    if i < len(items) else item
                    for i, item in enumerate(value)], repairs

    return value, repairs


def _join_path(path: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{path}.{key}"
    return f'{path}["{key}"]'


_WRAPPER_KEYS = ("output", "result", "data", "response", "json", "tool_output")


def unwrap_blob(value: Any, schema: dict) -> tuple[Any, list[str]]:
    """Unwrap blob-wrapped outputs: {"output": "{...}"} -> {...}.

    Picks the candidate (a parseable string, or a dict under a wrapper key)
    with the fewest validation errors, and only accepts it when it is strictly
    better than the current value.
    """
    if not isinstance(value, dict) or not isinstance(schema, dict):
        return value, []
    current_errors = len(validate(value, schema))
    best: Any = _PARSE
    best_errors = current_errors
    best_label = ""

    def consider(candidate: Any, label: str):
        nonlocal best, best_errors, best_label
        n = len(validate(candidate, schema))
        if n < best_errors:
            best, best_errors, best_label = candidate, n, label

    # single-key dict whose value is a JSON string (double-encoded blob)
    if len(value) == 1:
        (only,) = value.values()
        if isinstance(only, str):
            parsed = _try_parse(only)
            if parsed is not _PARSE:
                consider(parsed, "unwrap double-encoded blob")
    # common wrapper keys
    for key in _WRAPPER_KEYS:
        if key in value:
            inner = value[key]
            if isinstance(inner, str):
                parsed = _try_parse(inner)
                if parsed is not _PARSE:
                    consider(parsed, f"unwrap wrapper key {key!r}")
            elif isinstance(inner, (dict, list)):
                consider(inner, f"unwrap wrapper key {key!r}")

    if best is not _PARSE:
        return best, [best_label]
    return value, []


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def repair(raw_text: str, schema: dict, max_attempts: int = 3) -> RepairReport:
    """Try to turn *raw_text* into a schema-valid value.

    Each attempt: parse (with text repairs) -> validate -> coerce -> validate ->
    unwrap -> validate. Stops early on success; otherwise runs all
    ``max_attempts`` cycles and fails cleanly with the final errors.
    """
    report = RepairReport(ok=False)
    text = raw_text
    for attempt in range(1, max_attempts + 1):
        report.attempts = attempt
        value, text_repairs = parse_with_text_repairs(text)
        report.repairs_applied.extend(text_repairs)
        if value is _PARSE:
            report.errors = [ValidationError(
                path="$", message="output is not valid JSON",
                error_class="json_parse", keyword="parse")]
            continue

        errors = validate(value, schema)
        if not errors:
            report.ok, report.value, report.errors = True, value, []
            return report

        value, _ = coerce_types(value, schema, "$", report.repairs_applied)
        errors = validate(value, schema)
        if not errors:
            report.ok, report.value, report.errors = True, value, []
            return report

        value, unwrap_labels = unwrap_blob(value, schema)
        report.repairs_applied.extend(unwrap_labels)
        errors = validate(value, schema)
        if not errors:
            report.ok, report.value, report.errors = True, value, []
            return report

        report.value = value
        report.errors = errors

    return report
