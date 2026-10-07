"""Fictional Helios Home tool-call schemas (sample data only -- zero personal data).

These mirror the tool envelopes an FDE team would hand an agent: a tool name
plus a JSON-Schema-typed arguments object. The CLI and demo reference them by
name, e.g. ``python cli.py validate --schema create_work_order``.
"""

from __future__ import annotations

TOOL_SCHEMAS: dict[str, dict] = {
    "create_work_order": {
        "type": "object",
        "required": ["title", "priority"],
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 3, "maxLength": 120},
            "priority": {"type": "string",
                         "enum": ["low", "medium", "high", "urgent"]},
            "estimated_cost": {"type": "number", "minimum": 0, "maximum": 100000},
            "tags": {"type": "array", "items": {"type": "string"},
                     "maxItems": 8, "uniqueItems": True},
            "notify_customer": {"type": "boolean"},
        },
    },
    "send_customer_message": {
        "type": "object",
        "required": ["customer_id", "channel", "body"],
        "additionalProperties": False,
        "properties": {
            "customer_id": {"type": "string", "pattern": r"^C-\d{3}$"},
            "channel": {"type": "string", "enum": ["sms", "email", "push"]},
            "body": {"type": "string", "minLength": 1, "maxLength": 500},
            "scheduled_for": {"type": "string",
                              "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$"},
        },
    },
    "schedule_technician_visit": {
        "type": "object",
        "required": ["work_order_id", "date", "window"],
        "additionalProperties": True,
        "properties": {
            "work_order_id": {"type": "string", "pattern": r"^WO-\d{4}$"},
            "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "window": {"type": "string",
                       "enum": ["morning", "afternoon", "evening"]},
            "technician_id": {"type": "integer", "minimum": 1},
            "notes": {"type": ["string", "null"]},
        },
    },
    "tool_call": {
        # The envelope an agent runtime emits: name + arguments.
        "type": "object",
        "required": ["name", "arguments"],
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string",
                     "enum": ["create_work_order", "send_customer_message",
                              "schedule_technician_visit"]},
            "arguments": {"type": "object"},
        },
    },
}


def get_schema(name: str) -> dict:
    try:
        return TOOL_SCHEMAS[name]
    except KeyError:
        raise KeyError(
            f"unknown schema {name!r}; available: {sorted(TOOL_SCHEMAS)}") from None
