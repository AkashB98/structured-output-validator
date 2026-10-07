# structured-output-validator

**The validation + repair layer every team shipping LLM agents to customers needs.**

An **LLM** — large language model, the AI that generates text — that calls tools for you will eventually hand you almost-right **JSON** — JavaScript Object Notation, the data format APIs use. A `"42"` where the **schema** — the rulebook describing what shape the data must have — demands `42`. A markdown code fence around the whole blob. A trailing comma that kills the parse. A missing required field. Downstream code explodes, the customer sees the error, and the on-call engineer gets paged. This project is the layer that sits between the model and your code: it validates every **tool call** — a structured request from the AI to run a function, like `create_work_order` — against a **JSON Schema** — a standard for declaring the required shape of JSON data — with precise path-level errors, repairs the obviously-fixable ones with **heuristics** — rule-of-thumb fixes, no AI involved — and tracks **drift** — how output quality degrades over time — so you can see failure patterns before your customers do.

Where it sits in this portfolio's agent-shipping story: `mcp-tool-server` runs agents live against tools, `ai-red-teaming-harness` + `prompt-injection-firewall` grade and defend under attack, `tool-call-trace-replay` pins behavior in **CI** — continuous integration, the automated checks that run on every code change — and **this one makes tool-call outputs customer-safe**.

## What it does

1. **Hand-rolled JSON Schema validator** (`schema.py`) — stdlib only, no dependencies. Covers a practical subset: `object` / `array` / `string` / `number` / `integer` / `boolean` / `null` (or unions like `["string", "null"]`), `required`, `properties`, `items`, `enum`, `const`, `min/maxLength`, `min/maxItems`, `uniqueItems`, `pattern` (**regex** — regular expression, a text-matching pattern), `minimum` / `maximum` / `exclusiveMinimum` / `exclusiveMaximum` / `multipleOf`, `additionalProperties`, nested schemas all the way down. Every violation gets a path like `$.tools[0].arguments.amount: expected number, got string` plus a coarse **error class** — a bucket like `type_mismatch` or `missing_required` — for aggregation.
2. **Heuristic repair loop** (`repair.py`) — up to N attempts (default 3): strip code fences → fix trailing commas (string-aware, so a comma inside `"ends with ,}"` survives) → **coerce** — convert — obvious type slips (`"42"`→`42`, `"true"`→`true`, `"null"`→`None`, `42.0`→`42`; never `4.5`→`4`) → unwrap double-encoded / wrapper-key blobs (`{"output": "{...}"}`), re-validating after each pass. Unfixable outputs (missing required fields, bad **enum** — a fixed list of allowed values) fail cleanly after exactly N attempts with the final errors.
3. **Drift-stats tracker** (`drift.py`) — over a stream of validated outputs: pass/fail rate, per-field failure counts (`$.priority` broke 12 times today), most-common error classes, per-schema breakdowns. Exports deterministic JSON for dashboards or CI gates; supports save/load and merging per-worker shards.
4. **LLM regeneration seam** (`llm_seam.py`) — optional last resort behind `STRUCT_VALIDATOR_LLM=1` + `STRUCT_VALIDATOR_LLM_BASE_URL` (**OpenAI-compatible** — speaks the same API format as OpenAI's chat endpoint — hook). **Never used in tests, evals, or the demo** — those are heuristics-only and fully offline.

Plus: a CLI (`validate` / `repair` / `stats` / `demo` / `serve`), a one-command `demo.py`, and a tiny JSON API (`POST /validate`, `POST /repair`, `GET /stats`, `GET /schemas`).

Sample data is three fictional Helios Home **tool-call schemas** — rulebooks for sample AI function calls (`create_work_order`, `send_customer_message`, `schedule_technician_visit`) plus the `tool_call` envelope. Zero personal data, zero secrets.

## Quickstart

```bash
# validate a tool-call output against a schema (exit 0 = valid, 1 = invalid)
python cli.py validate --schema create_work_order --input sample.json

# repair an almost-right output (exit 0 = repaired, 2 = unfixable)
python cli.py repair --schema create_work_order --input messy.json --max-attempts 3

# aggregate a JSONL of validation results into drift stats
python cli.py stats --input results.jsonl --out drift.json

# one-command end-to-end demo (valid, fenced, coerced, blob, unfixable + stats)
python demo.py

# tiny JSON API on :8765
python cli.py serve --port 8765
```

`results.jsonl` lines look like `{"ok": false, "schema": "create_work_order", "errors": [{"path": "$.priority", "error_class": "missing_required", ...}]}`.

## Architecture

```
raw model text
    │
    ▼
parse_with_text_repairs   ── strip ``` fences, drop trailing commas (string-aware)
    │                          (json_parse error if still unparseable)
    ▼
validate (schema.py)      ── hand-rolled JSON Schema subset; path-level errors
    │                          with error classes; type mismatch short-circuits
    ▼                         deeper checks (no error cascades)
coerce_types (repair.py)  ── schema-guided scalar coercion, pure (no mutation)
    │
    ▼
unwrap_blob (repair.py)   ── unwrap {"output": "{...}"} / wrapper keys, but only
    │                          when the candidate has strictly fewer errors
    ▼
re-validate ──► ok? return repaired value : next attempt (max N) : fail cleanly
    │
    ▼  (every validate/repair call)
DriftTracker (drift.py)   ── counters → deterministic JSON summary
```

Design rules that matter:

- **Framing over cleverness in repair.** **Coercion** — automatic type conversion — only fires on unambiguous slips: `"42"`→`42` is safe, `4.5`→`4` is data loss and is refused. Unwrap only accepts a strictly-better candidate, so a healthy output wrapped in noise never gets mangled.
- **Errors are data.** Every `ValidationError` carries `path`, `message`, `error_class`, and `keyword` — the exact schema rule that failed — so the drift tracker, the CLI, and a future LLM-regeneration prompt all consume the same structure.
- **Determinism is a feature.** The **eval** — a fixed set of test cases with expected outcomes — report is byte-identical across runs (no timestamps, sorted keys); the drift summary breaks count ties alphabetically so a live tracker and a loaded-from-disk one always agree.
- **The LLM seam is opt-in and fenced.** It raises `RuntimeError` unless explicitly configured, and the no-network test suite stubs sockets to prove the offline paths never dial out.

## Evals

`python evals/run_evals.py` — 27 golden evals, fully offline and seeded:

| group | cases | what it proves |
|---|---|---|
| valid | 4 | clean outputs validate on attempt 1, untouched |
| invalid | 10 | bad outputs detected with the exact expected path (`$.tags[1]`, `$.technician_id` for `true`-as-integer, …) |
| repaired | 8 | fences, trailing commas, stringly numbers/bools/integers, double-encoded blobs, wrapper keys, and a combined mess all repair; expected repair labels recorded |
| unfixable | 4 | missing-required, bad enum, non-JSON prose, and non-JSON wrapper values fail cleanly after exactly 3 attempts |
| drift | 1 | a fixed 6-record stream yields byte-exact drift stats |

`evals/eval_report.json` is committed. The runner executes the suite twice in-process and asserts the reports are identical; run it twice from the shell and the file is byte-identical (`md5sum`).

## Tests

`python -m unittest discover -s tests` — 88 hermetic unit tests, stdlib only:

- `test_schema.py` — validator correctness: paths, nesting, unions, bool-vs-int (Python's `bool` subclasses `int`, so the guard is explicit), **JSON** — JavaScript Object Notation — equality semantics for `enum` (`True` ≠ `1`), short-circuit on type mismatch, invalid regexes reported not raised.
- `test_repair.py` — fence stripping, string-aware trailing-comma fixes, every coercion and its refusals, blob unwrapping (including rejecting worse candidates), loop attempt counting, clean unfixable failures.
- `test_drift.py` — counters, rates, deterministic ordering, save/load round-trip, shard merging.
- `test_llm_seam.py` — unconfigured seam raises without touching the network; configured seam builds the right request (stubbed transport).
- `test_cli.py` — CLI exit codes (0/1/2), JSONL stats aggregation, and a live localhost serve smoke test (`/validate`, `/repair`, `/stats`, `/schemas`, 400s, 404s).
- `test_no_network.py` — `socket` and `urlopen` stubbed to explode; every offline path (validate, repair, drift, demo, CLI) must complete without I/O.
- `test_evals.py` — the golden eval suite passes end to end and the report is deterministic.

## Dev loop: bugs the tests caught

Two real bugs, both verified by reverting to the buggy code and watching the tests fail:

1. **Drift `top_*` lists leaked `Counter` insertion order.** `summary()` used `most_common(5)`, which breaks count ties by first-seen order. A tracker saved to disk and reloaded produced a *different* `top_fields` order than the live one (save sorts keys; load re-inserts sorted), so "drift stats exact" was a lie across save/load. Caught by `test_save_load_roundtrip` and `test_top_fields_and_classes_ordered`. Fix: rank by `(-count, key)` — count descending, path ascending — so live and loaded trackers always agree.
2. **Repair loop silently dropped unwrap labels.** `repair()` called `unwrap_blob()` but discarded its label list (`value, _ = ...`), so `repairs_applied` never recorded *which* unwrap fired — the audit trail lied by omission. Caught by `test_repair_double_encoded_blob_records_label` (fails on the reverted code with `repairs_applied == []`, passes on the fix).

Two test expectations were also wrong on first run (my arithmetic, not the code): `{"title": "x"}` fails *both* `minLength` and `required`, and deterministic tie-breaking puts `$.priority` before `$.title` — the suite kept me honest there too.

## Project structure

```
schema.py    hand-rolled JSON Schema validator (subset), path-level errors
repair.py    heuristic repair loop: fences, trailing commas, coercion, unwrap
drift.py     drift-stats tracker: counters, rates, deterministic JSON export
llm_seam.py  optional OpenAI-compatible regeneration (env-gated, never in tests)
schemas.py   fictional Helios Home tool-call schemas (sample data)
cli.py       validate / repair / stats / demo / serve (+ tiny JSON API)
demo.py      one-command end-to-end demo (also a smoke test: asserts inside)
tests/       88 hermetic unit tests (stdlib unittest, no-network enforced)
evals/       27 golden evals + committed eval_report.json (byte-identical runs)
```

## Limits (honest)

- The validator is a practical **subset** — a deliberately limited portion — of JSON Schema: no `$ref`, no `if/then`, no `format` assertions, no recursive schemas. It covers what LLM tool-call envelopes actually use.
- **Heuristics** — rule-of-thumb fixes — can't invent missing data: a missing required field or a value outside its **enum** — fixed list of allowed values — is unfixable without the model, which is what the LLM seam (or your own retry) is for.
- Coercion is deliberately conservative — it will leave an ambiguous value broken rather than guess wrong. The **eval** — fixed test cases with expected outcomes — suite pins every refusal.

## License

MIT — see `LICENSE`.
