"""Drift-stats tracker: aggregate validation outcomes over a stream of outputs.

Every FDE team shipping LLM features watches the same question: "are our
model outputs getting sloppier, and where?" This tracker records per-output
pass/fail plus the path-level errors and rolls them up into per-field failure
counts, pass/fail rate, and most-common error classes -- exportable as JSON
for dashboards or CI gates.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from schema import ValidationError


@dataclass
class DriftTracker:
    total: int = 0
    passed: int = 0
    failed: int = 0
    field_failures: Counter = field(default_factory=Counter)   # "$.a.b" -> count
    error_classes: Counter = field(default_factory=Counter)    # "type_mismatch" -> count
    by_schema: Counter = field(default_factory=Counter)        # schema name -> total
    schema_failures: Counter = field(default_factory=Counter)  # schema name -> failed

    def record(self, ok: bool, errors: list[ValidationError] | None = None,
               schema: str = "") -> None:
        """Record one validated output. ``errors`` required when ``ok`` is False."""
        self.total += 1
        if schema:
            self.by_schema[schema] += 1
        if ok:
            self.passed += 1
            return
        self.failed += 1
        if schema:
            self.schema_failures[schema] += 1
        for err in errors or []:
            self.field_failures[err.path] += 1
            self.error_classes[err.error_class] += 1

    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def fail_rate(self) -> float:
        return self.failed / self.total if self.total else 0.0

    def summary(self) -> dict[str, Any]:
        """Deterministic summary dict (sorted keys) -- safe to snapshot in evals."""
        # top_* lists break count ties alphabetically so a live tracker and a
        # loaded-from-disk tracker always agree (Counter.most_common would
        # otherwise leak insertion order, which save/load re-sorts).
        def _top(counter: Counter, n: int = 5) -> list[str]:
            ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
            return [k for k, _ in ranked[:n]]

        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate(), 4),
            "fail_rate": round(self.fail_rate(), 4),
            "field_failures": dict(sorted(self.field_failures.items())),
            "error_classes": dict(sorted(self.error_classes.items())),
            "by_schema": dict(sorted(self.by_schema.items())),
            "schema_failures": dict(sorted(self.schema_failures.items())),
            "top_fields": _top(self.field_failures),
            "top_error_classes": _top(self.error_classes),
        }

    def to_json(self) -> str:
        return json.dumps(self.summary(), indent=2, sort_keys=True)

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            fh.write(self.to_json() + "\n")

    @classmethod
    def load(cls, path: str) -> "DriftTracker":
        with open(path) as fh:
            data = json.load(fh)
        t = cls()
        t.total = data.get("total", 0)
        t.passed = data.get("passed", 0)
        t.failed = data.get("failed", 0)
        t.field_failures = Counter(data.get("field_failures", {}))
        t.error_classes = Counter(data.get("error_classes", {}))
        t.by_schema = Counter(data.get("by_schema", {}))
        t.schema_failures = Counter(data.get("schema_failures", {}))
        return t

    def merge(self, other: "DriftTracker") -> None:
        """Fold another tracker in (e.g. per-worker shards -> global)."""
        self.total += other.total
        self.passed += other.passed
        self.failed += other.failed
        self.field_failures.update(other.field_failures)
        self.error_classes.update(other.error_classes)
        self.by_schema.update(other.by_schema)
        self.schema_failures.update(other.schema_failures)
