# Configurable Workflow Decision Platform

A small, dependency-free (Python 3.9+ standard library only) engine that runs **any** business workflow
described in a JSON file: schema validation -> rule checks -> external calls -> branching -> approve / reject /
retry / manual review, with full state history and an explainable audit trail.

## Run it (no install needed)
```bash
python3 -m unittest discover -s tests -v          # 26 tests
python3 examples.py                               # regenerates docs/DECISION_EXAMPLES.md

# CLI (state is kept in wfp.db)
python3 -m wfp.cli workflows
python3 -m wfp.cli submit vendor_approval samples/vendor_good.json --key demo-1
python3 -m wfp.cli submit vendor_approval samples/vendor_flaky_dependency.json   # -> PENDING_RETRY
python3 -m wfp.cli retry <request_id>                                            # dependency recovered -> APPROVED
python3 -m wfp.cli explain <request_id>
python3 -m wfp.cli audit <request_id> ; python3 -m wfp.cli history <request_id>
python3 -m wfp.cli review <request_id> APPROVED --reviewer alice --note "ok"     # for MANUAL_REVIEW

# REST API
python3 -m wfp.cli serve --port 8000
curl -X POST localhost:8000/workflows/vendor_approval/requests -H 'Idempotency-Key: k1' -d @samples/vendor_good.json
curl localhost:8000/requests/<id>/explain
```

## REST endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/workflows/{name}/requests` | Submit (header `Idempotency-Key` optional). 201 new, 200 duplicate, 422 invalid, 409 key reused with different payload |
| GET | `/requests/{id}` , `/history` , `/audit` , `/explain` | State, change history, audit trail, human-readable explanation |
| POST | `/requests/{id}/retry` | Resume a `PENDING_RETRY` request |
| POST | `/requests/{id}/review` | `{"decision":"APPROVED|REJECTED","reviewer":"..","note":".."}` for `MANUAL_REVIEW` |
| GET / POST | `/workflows` , `/admin/workflows` | List versions / publish a new config version without restart |

## Project layout
```
config/            workflow definitions (the ONLY place business rules live)
wfp/schema.py      payload validation from config
wfp/rules.py       condition evaluator (pure functions) + trace
wfp/config.py      config validation + immutable versioned registry
wfp/dependencies.py simulated external services + registry
wfp/store.py       SQLite: requests, history, audit; atomic step commits
wfp/engine.py      stage execution, retries, idempotency, state machine, explain
wfp/api.py cli.py  thin interfaces over the engine
tests/             26 tests   |   docs/ARCHITECTURE.md   |   docs/DECISION_EXAMPLES.md
```

## Configuration model (summary)
```jsonc
{ "name": "vendor_approval", "version": 1,
  "schema": { "fields": { "annual_revenue": {"type":"number","required":true,"min":0} } },
  "start": "mandatory_checks",
  "stages": {
    "mandatory_checks": { "type":"rules", "next":"sanctions_screen",
       "rules":[ {"id":"V1","description":"...","on_fail":"REJECT|MANUAL_REVIEW|CONTINUE",
                  "check":{"field":"input.has_insurance","op":"eq","value":true}} ] },
    "sanctions_screen": { "type":"dependency","service":"sanctions","params":{"country":"input.country"},
       "result_key":"sanctions","max_attempts":3,"backoff_seconds":0.2,
       "on_exhausted":"PENDING_RETRY|MANUAL_REVIEW","next":"..." },
    "route": { "type":"branch","branches":[{"when":{...},"goto":"..."}],"default":"..." },
    "approve": { "type":"terminal","outcome":"APPROVED|REJECTED|MANUAL_REVIEW","reason":"..." } } }
```
Operators: `eq ne gt gte lt lte in not_in contains matches exists not_exists`; combinators: `all any not`.
Fields: `input.<name>` (request) and `ext.<result_key>.<name>` (dependency output).

**Change a rule:** copy the JSON, bump `version`, edit the value, drop it in `config/` (or POST `/admin/workflows`).
New requests use it; in-flight requests keep the version they started with.

**Add a new business use case:** write a new JSON file. No Python changes. (Test `test_brand_new_workflow_needs_only_config`.)
**Add a new external service:** `deps.register("name", callable)` in `wfp/app.py`.

## Output & Execution Report

### 1. Test Suite Execution
The platform includes 26 unit tests covering happy paths, schema validation, idempotency, dependency failures, partial saves, and rule changes.
```text
C:\> python -m unittest discover -s tests -v
...
Ran 26 tests in 0.225s

OK

{
  "request_id": "c75bcc3733ca44dd918ac135d1c91982",
  "workflow": "vendor_approval",
  "workflow_version": 1,
  "status": "APPROVED",
  "current_stage": "approve",
  "updated_at": "2026-11-13T08:53:54.329+00:00",
  "duplicate": false
}

{
  "request_id": "c75bcc3733ca44dd918ac135d1c91982",
  "workflow": "vendor_approval",
  "workflow_version": 1,
  "status": "APPROVED",
  "final_stage": "approve",
  "stage_path": [
    "mandatory_checks",
    "sanctions_screen",
    "sanctions_eval",
    "risk_thresholds",
    "route_by_exposure",
    "approve"
  ],
  "rules_evaluated": [
    {
      "rule_id": "V1",
      "stage": "mandatory_checks",
      "description": "Vendor must hold valid insurance",
      "result": "PASS",
      "on_fail": "REJECT",
      "conditions": [
        {
          "field": "input.has_insurance",
          "op": "eq",
          "expected": true,
          "observed": true,
          "found": true,
          "result": true
        }
      ]
    },
    {
      "rule_id": "T1",
      "stage": "risk_thresholds",
      "description": "Annual revenue must be at least 50,000",
      "result": "PASS",
      "on_fail": "REJECT",
      "conditions": [
        {
          "field": "input.annual_revenue",
          "op": "gte",
          "expected": 50000,
          "observed": 2500000,
          "found": true,
          "result": true
        }
      ]
    }
  ],
  "failed_rules": [],
  "reasoning": [
    "Request c75bcc3733ca44dd918ac135d1c91982 was processed by workflow'vendor_approval' v1 and is APPROVED.",
    "Rules passed: V1, V2, S1, T1, T2.",
    "Call to 'sanctions' succeeded (attempt 1/3).",
    "Final outcome APPROVED: All checks passed."
  ]
}

