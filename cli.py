"""CLI: validate / repair / stats / demo / serve for the structured-output validator."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from drift import DriftTracker
from repair import repair
from schema import ValidationError, validate
from schemas import get_schema

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREPAIRABLE = 2

_tracker = DriftTracker()  # serve-mode in-memory tracker


def _read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path) as fh:
        return fh.read()


def cmd_validate(args: argparse.Namespace) -> int:
    schema = get_schema(args.schema)
    raw = _read_input(args.input)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"INVALID: output is not valid JSON ({exc})")
        return EXIT_INVALID
    errors = validate(value, schema)
    _tracker.record(not errors, errors, args.schema)
    if not errors:
        print("VALID")
        return EXIT_OK
    print(f"INVALID: {len(errors)} error(s)")
    for e in errors:
        print(f"  {e.path}: {e.message}")
    return EXIT_INVALID


def cmd_repair(args: argparse.Namespace) -> int:
    schema = get_schema(args.schema)
    raw = _read_input(args.input)
    report = repair(raw, schema, max_attempts=args.max_attempts)
    _tracker.record(report.ok, report.errors, args.schema)
    print(f"attempts: {report.attempts}")
    for r in report.repairs_applied:
        print(f"  repair: {r}")
    if report.ok:
        print("REPAIRED:")
        print(json.dumps(report.value, indent=2, sort_keys=True))
        return EXIT_OK
    print(f"UNREPAIRABLE after {report.attempts} attempt(s): "
          f"{len(report.errors)} error(s) remain")
    for e in report.errors:
        print(f"  {e.path}: {e.message}")
    return EXIT_UNREPAIRABLE


def cmd_stats(args: argparse.Namespace) -> int:
    tracker = DriftTracker()
    with open(args.input) as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            errors = [ValidationError(path=e.get("path", "$"),
                                      message=e.get("message", ""),
                                      error_class=e.get("error_class", "unknown"),
                                      keyword=e.get("keyword", ""))
                      for e in rec.get("errors", [])]
            tracker.record(bool(rec.get("ok")), errors, rec.get("schema", ""))
    out = tracker.to_json()
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(out + "\n")
        print(f"wrote {args.out}")
    else:
        print(out)
    return EXIT_OK


def cmd_demo(_args: argparse.Namespace) -> int:
    from demo import main as demo_main
    demo_main()
    return EXIT_OK


# ---------------------------------------------------------------------------
# tiny JSON API
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "StructuredOutputValidator/1.0"

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode() or "{}")

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json()
        except (json.JSONDecodeError, ValueError):
            self._send(400, {"error": "request body is not valid JSON"})
            return
        if self.path == "/validate":
            try:
                schema = get_schema(body.get("schema", ""))
            except KeyError as exc:
                self._send(400, {"error": str(exc)})
                return
            raw = body.get("input", "")
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                self._send(200, {"ok": False, "errors": [{
                    "path": "$", "message": "output is not valid JSON",
                    "error_class": "json_parse", "keyword": "parse"}]})
                return
            errors = validate(value, schema)
            _tracker.record(not errors, errors, body.get("schema", ""))
            self._send(200, {"ok": not errors,
                             "errors": [e.to_dict() for e in errors]})
        elif self.path == "/repair":
            try:
                schema = get_schema(body.get("schema", ""))
            except KeyError as exc:
                self._send(400, {"error": str(exc)})
                return
            report = repair(body.get("input", ""), schema,
                            max_attempts=int(body.get("max_attempts", 3)))
            _tracker.record(report.ok, report.errors, body.get("schema", ""))
            self._send(200, report.to_dict())
        else:
            self._send(404, {"error": f"unknown route {self.path}"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/stats":
            self._send(200, _tracker.summary())
        elif self.path == "/schemas":
            from schemas import TOOL_SCHEMAS
            self._send(200, {"schemas": sorted(TOOL_SCHEMAS)})
        else:
            self._send(404, {"error": f"unknown route {self.path}"})

    def log_message(self, *args: Any) -> None:  # keep serve output quiet
        pass


def cmd_serve(args: argparse.Namespace) -> int:
    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"serving on http://127.0.0.1:{args.port} "
          "(POST /validate, POST /repair, GET /stats)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Validate and repair LLM tool-call JSON outputs.")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate a JSON output against a schema")
    v.add_argument("--schema", required=True, help="schema name (see /schemas)")
    v.add_argument("--input", required=True, help="JSON file, or - for stdin")
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("repair", help="repair an almost-right JSON output")
    r.add_argument("--schema", required=True)
    r.add_argument("--input", required=True, help="JSON file, or - for stdin")
    r.add_argument("--max-attempts", type=int, default=3)
    r.set_defaults(func=cmd_repair)

    s = sub.add_parser("stats", help="aggregate a JSONL of validation results")
    s.add_argument("--input", required=True, help="JSONL: {ok, errors[], schema}")
    s.add_argument("--out", default="", help="write stats JSON here (else stdout)")
    s.set_defaults(func=cmd_stats)

    d = sub.add_parser("demo", help="one-command end-to-end demo")
    d.set_defaults(func=cmd_demo)

    sv = sub.add_parser("serve", help="tiny JSON API")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
