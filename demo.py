"""One-command end-to-end demo: validate, repair, and track drift.

Runs fully offline on fictional Helios Home tool-call outputs. Each stage
asserts what it expects, so the demo doubles as a smoke test.
"""

from __future__ import annotations

import json

from drift import DriftTracker
from repair import repair
from schema import validate
from schemas import TOOL_SCHEMAS

SCHEMA = "create_work_order"
schema = TOOL_SCHEMAS[SCHEMA]

CASES = [
    ("valid output passes untouched",
     '{"title": "Replace smart thermostat", "priority": "high", '
     '"estimated_cost": 189.99, "notify_customer": true}'),
    ("code-fenced blob gets unwrapped",
     '```json\n{"title": "Fix porch light", "priority": "low"}\n```'),
    ("trailing comma + stringly number get repaired",
     '{"title": "Unclog drain", "priority": "medium", '
     '"estimated_cost": "149.50", "tags": ["plumbing",],}'),
    ("double-encoded blob gets unwrapped",
     '{"output": "{\\"title\\": \\"Paint nursery\\", \\"priority\\": \\"medium\\"}"}'),
    ("unfixable: missing required + bad enum fails cleanly",
     '{"title": "x", "priority": "whenever"}'),
]


def main() -> None:
    tracker = DriftTracker()
    print(f"== schema: {SCHEMA} ==")
    print(json.dumps(schema, indent=2, sort_keys=True))

    for i, (label, raw) in enumerate(CASES, 1):
        print(f"\n== case {i}: {label} ==")
        print(f"   raw: {raw[:80]}")
        value = json.loads(raw) if _looks_like_plain_json(raw) else None
        if value is not None:
            errors = validate(value, schema)
            print(f"   direct validate: {'VALID' if not errors else f'{len(errors)} error(s)'}")
            for e in errors:
                print(f"      {e.path}: {e.message}")
        report = repair(raw, schema, max_attempts=3)
        tracker.record(report.ok, report.errors, SCHEMA)
        print(f"   repair: ok={report.ok} attempts={report.attempts} "
              f"repairs={report.repairs_applied}")
        if not report.ok:
            for e in report.errors:
                print(f"      {e.path}: {e.message}")

    # stage assertions: the demo proves what it claims
    r1 = repair(CASES[0][1], schema); assert r1.ok and r1.attempts == 1 and not r1.repairs_applied
    r2 = repair(CASES[1][1], schema); assert r2.ok and "strip_code_fence" in r2.repairs_applied
    r3 = repair(CASES[2][1], schema); assert r3.ok and r3.value["estimated_cost"] == 149.50
    r4 = repair(CASES[3][1], schema); assert r4.ok and any("unwrap" in x for x in r4.repairs_applied)
    r5 = repair(CASES[4][1], schema); assert not r5.ok and r5.attempts == 3

    print("\n== drift stats over the 5 outputs ==")
    print(tracker.to_json())
    s = tracker.summary()
    assert s["total"] == 5 and s["passed"] == 4 and s["failed"] == 1
    assert abs(s["pass_rate"] - 0.8) < 1e-9
    print("\nDEMO OK: 4 repaired/valid, 1 clean failure, drift stats exact.")


def _looks_like_plain_json(raw: str) -> bool:
    try:
        json.loads(raw)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


if __name__ == "__main__":
    main()
