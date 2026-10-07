"""Golden evals for the structured-output validator.

Seeded, deterministic, fully offline. Covers:
  valid      -- clean outputs validate on the first attempt, untouched
  invalid    -- bad outputs are detected with the exact expected error path
  repaired   -- heuristically fixable outputs repair to valid, expected
               repair labels present
  unfixable  -- hopeless outputs fail cleanly after exactly N attempts
  drift      -- a fixed record stream yields byte-exact drift stats

Writes evals/eval_report.json (no timestamps; sorted keys). Run twice:
the file must be byte-identical.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift import DriftTracker
from repair import repair
from schema import validate
from schemas import TOOL_SCHEMAS

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(HERE, "eval_report.json")
MAX_ATTEMPTS = 3

WO = "create_work_order"
MSG = "send_customer_message"
VISIT = "schedule_technician_visit"

# (id, schema, raw input, expectation, extra expectations)
CASES = [
    # ---- valid: untouched, first attempt ----
    ("valid/full-work-order", WO,
     '{"title": "Replace smart thermostat", "priority": "high", '
     '"estimated_cost": 189.99, "tags": ["hvac"], "notify_customer": true}',
     "valid", {}),
    ("valid/minimal-work-order", WO,
     '{"title": "Fix sink", "priority": "low"}', "valid", {}),
    ("valid/customer-message", MSG,
     '{"customer_id": "C-101", "channel": "sms", "body": "Tech is on the way", '
     '"scheduled_for": "2026-10-06T09:30"}', "valid", {}),
    ("valid/visit-null-notes", VISIT,
     '{"work_order_id": "WO-1001", "date": "2026-10-06", "window": "morning", '
     '"technician_id": 7, "notes": null}', "valid", {}),

    # ---- invalid: exact error paths ----
    ("invalid/type-mismatch-path", WO,
     '{"title": "Fix sink", "priority": "low", "estimated_cost": "expensive"}',
     "invalid", {"paths": ["$.estimated_cost"], "classes": ["type_mismatch"]}),
    ("invalid/missing-required", WO,
     '{"title": "Fix sink"}',
     "invalid", {"paths": ["$.priority"], "classes": ["missing_required"]}),
    ("invalid/nested-array-path", WO,
     '{"title": "Fix sink", "priority": "low", "tags": ["plumbing", 5]}',
     "invalid", {"paths": ["$.tags[1]"], "classes": ["type_mismatch"]}),
    ("invalid/pattern", MSG,
     '{"customer_id": "C-12", "channel": "sms", "body": "hi"}',
     "invalid", {"paths": ["$.customer_id"], "classes": ["pattern"]}),
    ("invalid/enum", MSG,
     '{"customer_id": "C-101", "channel": "fax", "body": "hi"}',
     "invalid", {"paths": ["$.channel"], "classes": ["enum"]}),
    ("invalid/extra-property", WO,
     '{"title": "Fix sink", "priority": "low", "zzz": 1}',
     "invalid", {"paths": ["$.zzz"], "classes": ["extra_property"]}),
    ("invalid/min-length", WO,
     '{"title": "ab", "priority": "low"}',
     "invalid", {"paths": ["$.title"], "classes": ["length"]}),
    ("invalid/range", WO,
     '{"title": "Fix sink", "priority": "low", "estimated_cost": -5}',
     "invalid", {"paths": ["$.estimated_cost"], "classes": ["range"]}),
    ("invalid/bool-not-integer", VISIT,
     '{"work_order_id": "WO-1001", "date": "2026-10-06", "window": "morning", '
     '"technician_id": true}',
     "invalid", {"paths": ["$.technician_id"], "classes": ["type_mismatch"]}),
    ("invalid/duplicate-tags", WO,
     '{"title": "Fix sink", "priority": "low", "tags": ["a", "a"]}',
     "invalid", {"paths": ["$.tags[1]"], "classes": ["length"]}),

    # ---- repaired: heuristics fix, labels recorded ----
    ("repaired/code-fence", WO,
     '```json\n{"title": "Fix porch light", "priority": "low"}\n```',
     "repaired", {"repairs": ["strip_code_fence"]}),
    ("repaired/trailing-comma", WO,
     '{"title": "Fix sink", "priority": "low",}',
     "repaired", {"repairs": ["fix_trailing_comma"]}),
    ("repaired/stringly-number", WO,
     '{"title": "Unclog drain", "priority": "medium", "estimated_cost": "149.50"}',
     "repaired", {"repairs": ["coerce $.estimated_cost"]}),
    ("repaired/stringly-bool", WO,
     '{"title": "Fix sink", "priority": "low", "notify_customer": "true"}',
     "repaired", {"repairs": ["coerce $.notify_customer"]}),
    ("repaired/stringly-integer", VISIT,
     '{"work_order_id": "WO-1001", "date": "2026-10-06", "window": "evening", '
     '"technician_id": "42"}',
     "repaired", {"repairs": ["coerce $.technician_id"]}),
    ("repaired/double-encoded-blob", WO,
     '{"output": "{\\"title\\": \\"Paint nursery\\", \\"priority\\": \\"medium\\"}"}',
     "repaired", {"repairs": ["unwrap"]}),
    ("repaired/wrapper-key", WO,
     '{"result": {"title": "Paint nursery", "priority": "medium"}}',
     "repaired", {"repairs": ["unwrap"]}),
    ("repaired/combined", WO,
     '```json\n{"title": "Fix sink", "priority": "low", "estimated_cost": "99"}\n```',
     "repaired", {"repairs": ["strip_code_fence", "coerce $.estimated_cost"]}),

    # ---- unfixable: clean failure after exactly N attempts ----
    ("unfixable/missing-plus-enum", WO,
     '{"title": "x", "priority": "whenever"}', "unfixable", {}),
    ("unfixable/not-json", WO,
     'hello world', "unfixable", {"error_class": "json_parse"}),
    ("unfixable/enum-only", MSG,
     '{"customer_id": "C-101", "channel": "carrier-pigeon", "body": "hi"}',
     "unfixable", {}),
    ("unfixable/non-json-wrapper", WO,
     '{"output": "not json", "title": "Fix sink", "priority": "low"}',
     "unfixable", {}),
]

DRIFT_STREAM = [
    # (schema, raw) -- repair decides ok/errors, tracker aggregates
    (WO, '{"title": "Fix sink", "priority": "low"}'),                    # pass
    (WO, '{"title": "x"}'),                                              # fail: $.title length, $.priority missing
    (WO, '{"title": "Fix sink", "priority": "low", "zzz": 1}'),          # fail: $.zzz extra
    (MSG, '{"customer_id": "C-101", "channel": "fax", "body": "hi"}'),   # fail: $.channel enum
    (WO, '{"title": "Fix sink", "priority": "low"}'),                    # pass
    (VISIT, '{"work_order_id": "WO-1", "date": "2026-10-06", '
            '"window": "morning"}'),                                     # fail: $.work_order_id pattern
]

EXPECTED_DRIFT = {
    "total": 6, "passed": 2, "failed": 4,
    "pass_rate": 0.3333, "fail_rate": 0.6667,
    "field_failures": {
        "$.channel": 1, "$.priority": 1, "$.title": 1,
        "$.work_order_id": 1, "$.zzz": 1,
    },
    "error_classes": {
        "enum": 1, "extra_property": 1, "length": 1,
        "missing_required": 1, "pattern": 1,
    },
    "by_schema": {
        "create_work_order": 4, "schedule_technician_visit": 1,
        "send_customer_message": 1,
    },
    "schema_failures": {
        "create_work_order": 2, "schedule_technician_visit": 1,
        "send_customer_message": 1,
    },
    "top_fields": ["$.channel", "$.priority", "$.title",
                   "$.work_order_id", "$.zzz"],
    "top_error_classes": ["enum", "extra_property", "length",
                          "missing_required", "pattern"],
}


def run_case(case):
    cid, schema_name, raw, expectation, extra = case
    schema = TOOL_SCHEMAS[schema_name]
    detail = {}
    if expectation == "valid":
        report = repair(raw, schema, MAX_ATTEMPTS)
        ok = (report.ok and report.attempts == 1
              and report.repairs_applied == [])
        detail = {"attempts": report.attempts,
                  "repairs": report.repairs_applied}
    elif expectation == "invalid":
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"id": cid, "expectation": expectation, "passed": False,
                    "detail": "input did not parse as JSON"}
        errors = validate(value, schema)
        got_paths = [e.path for e in errors]
        got_classes = [e.error_class for e in errors]
        ok = (bool(errors)
              and all(p in got_paths for p in extra["paths"])
              and all(c in got_classes for c in extra["classes"]))
        detail = {"paths": got_paths, "classes": got_classes}
    elif expectation == "repaired":
        report = repair(raw, schema, MAX_ATTEMPTS)
        ok = (report.ok and all(
            any(want in got for got in report.repairs_applied)
            for want in extra["repairs"]))
        # repaired value must validate clean
        ok = ok and validate(report.value, schema) == []
        detail = {"attempts": report.attempts,
                  "repairs": report.repairs_applied}
    elif expectation == "unfixable":
        report = repair(raw, schema, MAX_ATTEMPTS)
        ok = (not report.ok and report.attempts == MAX_ATTEMPTS
              and len(report.errors) > 0)
        if "error_class" in extra:
            ok = ok and report.errors[0].error_class == extra["error_class"]
        detail = {"attempts": report.attempts,
                  "errors": [e.to_dict() for e in report.errors]}
    else:
        raise ValueError(f"unknown expectation {expectation!r}")
    return {"id": cid, "expectation": expectation, "passed": ok, "detail": detail}


def run_drift_check():
    tracker = DriftTracker()
    for schema_name, raw in DRIFT_STREAM:
        report = repair(raw, TOOL_SCHEMAS[schema_name], MAX_ATTEMPTS)
        tracker.record(report.ok, report.errors, schema_name)
    got = tracker.summary()
    return {"passed": got == EXPECTED_DRIFT, "got": got,
            "expected": EXPECTED_DRIFT}


def run_all():
    cases = [run_case(c) for c in CASES]
    drift = run_drift_check()
    passed = sum(1 for c in cases if c["passed"]) + (1 if drift["passed"] else 0)
    total = len(cases) + 1
    return {
        "tool": "structured-output-validator",
        "max_attempts": MAX_ATTEMPTS,
        "cases": cases,
        "drift_check": {"passed": drift["passed"], "got": drift["got"]},
        "summary": {"total": total, "passed": passed, "failed": total - passed},
    }


def main(out_path: str = REPORT_PATH) -> int:
    report = run_all()
    # determinism: run the whole suite again in-process; must be identical
    again = run_all()
    report["deterministic"] = (json.dumps(report, sort_keys=True)
                               == json.dumps(again, sort_keys=True))
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    with open(out_path, "w") as fh:
        fh.write(payload)
    failed = [c["id"] for c in report["cases"] if not c["passed"]]
    if not report["drift_check"]["passed"]:
        failed.append("drift_check")
    if not report["deterministic"]:
        failed.append("determinism")
    print(f"{report['summary']['passed']}/{report['summary']['total']} evals passed")
    for fid in failed:
        print(f"  FAILED: {fid}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
