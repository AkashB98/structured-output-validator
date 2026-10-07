"""Hand-rolled JSON Schema validator (practical subset, stdlib only).

Validates a decoded JSON value against a schema dict and returns precise,
path-level errors, e.g. ``$.tools[0].arguments.amount: expected number, got string``.

Supported keywords:
    type (object/array/string/number/integer/boolean/null, or a list of them),
    properties, required, additionalProperties (bool or schema),
    items (schema or list of schemas), minItems, maxItems, uniqueItems,
    minLength, maxLength, pattern (regex), minimum, maximum,
    exclusiveMinimum, exclusiveMaximum, multipleOf, enum, const.
Nested schemas recurse, so tool-call envelopes validate all the way down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_MISSING = object()


@dataclass
class ValidationError:
    """One schema violation, pinned to the JSON path where it happened."""

    path: str            # e.g. "$.tools[0].arguments.amount"
    message: str         # human-readable, e.g. "expected number, got string"
    error_class: str     # coarse bucket for drift stats: type_mismatch, ...
    keyword: str = ""    # the schema keyword that failed ("type", "required", ...)
    expected: Any = field(default=None)
    actual: Any = field(default=None)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "message": self.message,
            "error_class": self.error_class,
            "keyword": self.keyword,
        }


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, type_name: str) -> bool:
    # bool is a subclass of int in Python -- exclude it explicitly or every
    # boolean would also "match" integer/number and coercion would go sideways.
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def _join(path: str, key: str) -> str:
    # Dot access for identifier-safe names, bracket-quote otherwise.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{path}.{key}"
    return f'{path}["{key}"]'


def _idx(path: str, i: int) -> str:
    return f"{path}[{i}]"


_CLASS_FOR_KEYWORD = {
    "type": "type_mismatch",
    "required": "missing_required",
    "additionalProperties": "extra_property",
    "properties": "type_mismatch",
    "items": "type_mismatch",
    "pattern": "pattern",
    "enum": "enum",
    "const": "enum",
    "minimum": "range",
    "maximum": "range",
    "exclusiveMinimum": "range",
    "exclusiveMaximum": "range",
    "multipleOf": "range",
    "minLength": "length",
    "maxLength": "length",
    "minItems": "length",
    "maxItems": "length",
    "uniqueItems": "length",
}


def _err(path, keyword, message, expected=None, actual=_MISSING):
    return ValidationError(
        path=path,
        message=message,
        error_class=_CLASS_FOR_KEYWORD.get(keyword, "schema_violation"),
        keyword=keyword,
        expected=expected,
        actual=None if actual is _MISSING else actual,
    )


def validate(instance: Any, schema: dict, path: str = "$") -> list[ValidationError]:
    """Validate *instance* against *schema*. Returns [] when valid."""
    errors: list[ValidationError] = []
    if not isinstance(schema, dict):
        return errors

    # --- type gate: a type mismatch short-circuits deeper keyword checks ---
    declared = schema.get("type", _MISSING)
    type_ok = True
    if declared is not _MISSING:
        names = declared if isinstance(declared, list) else [declared]
        type_ok = any(_matches_type(instance, n) for n in names)
        if not type_ok:
            want = " or ".join(names)
            errors.append(_err(
                path, "type",
                f"expected {want}, got {_type_name(instance)}",
                expected=want, actual=_type_name(instance),
            ))
            return errors

    # --- enum / const (apply to any type) ---
    if "enum" in schema:
        allowed = schema["enum"]
        if not any(_json_equal(instance, cand) for cand in allowed):
            errors.append(_err(
                path, "enum",
                f"value {instance!r} not in enum {allowed!r}",
                expected=allowed, actual=instance,
            ))
    if "const" in schema:
        if not _json_equal(instance, schema["const"]):
            errors.append(_err(
                path, "const",
                f"value {instance!r} does not equal const {schema['const']!r}",
                expected=schema["const"], actual=instance,
            ))

    # --- objects ---
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(_err(
                    _join(path, req), "required",
                    f"missing required property {req!r}",
                    expected=req,
                ))
        for key, subschema in props.items():
            if key in instance:
                errors.extend(validate(instance[key], subschema, _join(path, key)))
        addl = schema.get("additionalProperties", True)
        if addl is False:
            for key in instance:
                if key not in props:
                    errors.append(_err(
                        _join(path, key), "additionalProperties",
                        f"additional property {key!r} not allowed",
                        actual=key,
                    ))
        elif isinstance(addl, dict):
            for key in instance:
                if key not in props:
                    errors.extend(validate(instance[key], addl, _join(path, key)))

    # --- arrays ---
    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(instance):
                errors.extend(validate(item, items, _idx(path, i)))
        elif isinstance(items, list):
            for i, subschema in enumerate(items):
                if i < len(instance):
                    errors.extend(validate(instance[i], subschema, _idx(path, i)))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(_err(path, "minItems",
                               f"array has {len(instance)} items, minimum {schema['minItems']}",
                               expected=schema["minItems"], actual=len(instance)))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(_err(path, "maxItems",
                               f"array has {len(instance)} items, maximum {schema['maxItems']}",
                               expected=schema["maxItems"], actual=len(instance)))
        if schema.get("uniqueItems"):
            seen = set()
            for i, item in enumerate(instance):
                canon = _canonical(item)
                if canon in seen:
                    errors.append(_err(_idx(path, i), "uniqueItems",
                                       "duplicate array item", actual=item))
                    break
                seen.add(canon)

    # --- strings ---
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(_err(path, "minLength",
                               f"string length {len(instance)} < minimum {schema['minLength']}",
                               expected=schema["minLength"], actual=len(instance)))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(_err(path, "maxLength",
                               f"string length {len(instance)} > maximum {schema['maxLength']}",
                               expected=schema["maxLength"], actual=len(instance)))
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], instance):
                    errors.append(_err(path, "pattern",
                                       f"string {instance!r} does not match pattern {schema['pattern']!r}",
                                       expected=schema["pattern"], actual=instance))
            except re.error as exc:
                errors.append(_err(path, "pattern",
                                   f"invalid pattern {schema['pattern']!r}: {exc}",
                                   expected=schema["pattern"]))

    # --- numbers ---
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(_err(path, "minimum",
                               f"{instance!r} < minimum {schema['minimum']!r}",
                               expected=schema["minimum"], actual=instance))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(_err(path, "maximum",
                               f"{instance!r} > maximum {schema['maximum']!r}",
                               expected=schema["maximum"], actual=instance))
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(_err(path, "exclusiveMinimum",
                               f"{instance!r} <= exclusiveMinimum {schema['exclusiveMinimum']!r}",
                               expected=schema["exclusiveMinimum"], actual=instance))
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(_err(path, "exclusiveMaximum",
                               f"{instance!r} >= exclusiveMaximum {schema['exclusiveMaximum']!r}",
                               expected=schema["exclusiveMaximum"], actual=instance))
        if "multipleOf" in schema:
            step = schema["multipleOf"]
            if step and abs(instance / step - round(instance / step)) > 1e-9:
                errors.append(_err(path, "multipleOf",
                                   f"{instance!r} is not a multiple of {step!r}",
                                   expected=step, actual=instance))

    return errors


def _json_equal(a: Any, b: Any) -> bool:
    # JSON equality: True != 1 and 1 == 1.0 here (bool is not a number).
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, int):
        return a == b
    if isinstance(a, int) and isinstance(b, float):
        return a == b
    return type(a) is type(b) and a == b


def _canonical(value: Any) -> str:
    import json as _json
    return _json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def is_valid(instance: Any, schema: dict) -> bool:
    """True when *instance* satisfies *schema* with zero errors."""
    return not validate(instance, schema)


def summarize(errors: list[ValidationError]) -> str:
    """One-line-per-error summary, e.g. for the LLM regeneration seam."""
    return "\n".join(f"{e.path}: {e.message}" for e in errors)
