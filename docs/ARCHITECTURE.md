# Architecture

## 1. Problem boundaries
**In scope:** intake + validation, rule evaluation, staged execution, state/history, audit + explanation, retries,
idempotency, config-driven behaviour, one simulated dependency.
**Out of scope (assumptions):** authentication, real queues/workers, multi-tenant isolation, a UI for reviewers,
real third-party APIs. Each is a clean extension point (see Scaling).

## 2. Components (one job each, low coupling)
```
 Client ── REST/CLI ──► Engine ──► Rules (pure)        Config Registry (versioned, validated)
                          │  ├──► Dependencies (pluggable; simulated here)
                          │  └──► Schema validator
                          ▼
                        Store (SQLite): requests | history | audit
```
| Component | Responsibility | Knows about |
|---|---|---|
| `schema.py` | Validate input against config schema | nothing else |
| `rules.py` | Evaluate conditions, produce trace | nothing else (pure) |
| `config.py` | Validate configs (dangling stages, bad ops, cycles, duplicate rule ids), keep immutable versions | rules (to validate conditions) |
| `dependencies.py` | Call external services by name | nothing else |
| `store.py` | Durable state, history, audit; atomic step commit; optimistic locking | nothing about business rules |
| `engine.py` | Orchestration, state machine, retries, idempotency | all of the above via interfaces |
| `api.py` / `cli.py` | Transport only | engine |

Business logic never appears in Python: it is in `config/*.json`. The engine has four stage types
(`rules`, `branch`, `dependency`, `terminal`), which is enough to model all five example use cases.

## 3. Data flow
1. **Intake**: look up latest config -> validate payload (422 on error, nothing stored).
2. **Idempotent create**: key = `Idempotency-Key` header or SHA-256 of canonical payload. `INSERT` guarded by
   `UNIQUE(workflow, idem_key)`. Duplicate + same payload -> return existing request (no re-run). Same key + different payload -> 409.
   The config is **snapshotted into the request row** (version pinning).
3. **Run loop**: read current stage -> handler returns *(status, next stage, context, audit entries)* ->
   `_commit` writes state + history + audit in **one transaction** -> repeat until the request stops.
4. **Stop states**: `APPROVED`, `REJECTED` (final), `MANUAL_REVIEW` (waits for human), `PENDING_RETRY` (waits for retry).
5. **Explain**: reads the audit trail back into rule-by-rule reasoning.

## 4. Lifecycle
```
RECEIVED → IN_PROGRESS ⇄ (stage to stage)
IN_PROGRESS → APPROVED | REJECTED | MANUAL_REVIEW | PENDING_RETRY
PENDING_RETRY → IN_PROGRESS (retry)        MANUAL_REVIEW → APPROVED | REJECTED (human)
```
Illegal moves raise `IllegalTransition` (e.g. retrying a finished request, resolving twice).

## 5. Failure handling
| Failure | Handling |
|---|---|
| Dependency timeout/5xx | Inline retries with exponential backoff (per-stage `max_attempts`), every attempt audited. Exhausted → `PENDING_RETRY` (resume exactly at that stage later) or `MANUAL_REVIEW`, chosen **in config** |
| Partial save | State+history+audit share one transaction; failure rolls back everything. Commit is retried 3×; still failing → `PersistenceError` (503) with DB unchanged. Client re-sends same key → request resumes from last committed step |
| Duplicate requests | Unique constraint + payload hash; also safe under concurrent submits (tested with 8 threads) |
| Concurrent workers | Optimistic lock (`version` column); loser gets `ConflictError`, reloads, and returns the winner's state |
| Crash mid-workflow | Current stage is persisted after every step → `run()` resumes; mandatory checks are not repeated (tested) |
| Bad config | Validated at load/publish; published versions immutable |
| Missing data in a rule | Treated as a failed condition, never a silent pass |

**Known limitation (documented trade-off):** dependency calls happen before the step commit, so after a crash a call may be repeated.
Real dependencies should be idempotent or receive the request id as an idempotency key (the params already allow this).

## 6. Explainability
Every rule evaluation stores: stage, rule id, description, PASS/FAIL, `on_fail` effect and each leaf condition
`(field, operator, expected, observed)`. `field` is the data reference (`input.annual_revenue`, `ext.sanctions.flagged`).
Dependency calls store attempt, params, response/error. Manual decisions store reviewer + note. See `docs/DECISION_EXAMPLES.md`.

## 7. Trade-offs and justifications
| Decision | Why | Cost / alternative |
|---|---|---|
| JSON declarative rules, not code/expression strings | Safe (no `eval`), validatable, traceable per leaf | Less expressive than a DSL; add ops in one function |
| 4 generic stage types | Small surface, covers all use cases | Parallel stages / timers would need a new type |
| Config version pinned per request | Rule changes never alter in-flight/past decisions (auditable) | Storage per request; migration of in-flight requests is manual |
| SQLite + stdlib only | Zero setup, real transactions, easy to review | Single writer; swap `Store` for Postgres for scale |
| Synchronous execution | Simple, deterministic, easy to test | Long dependencies block a request; move `run()` to workers |
| Auto idempotency key from payload hash | Protects clients that send no key | Intentional identical resubmission is treated as duplicate; send explicit keys to override |
| Invalid payloads are not persisted | Keeps the store clean; client gets 422 immediately | No server-side record of rejected garbage (log at gateway if needed) |
| All matching rules evaluated in a stage (no short-circuit) | Complete explanation ("all reasons", not just first) | Slightly more work per stage |

## 8. Scaling considerations
- **Stateless engine** → run N API replicas behind a load balancer; all state is in the DB.
- **DB**: replace SQLite with Postgres (same SQL, `SELECT … FOR UPDATE SKIP LOCKED` for work claiming); index `(status)` and `(workflow, idem_key)`; partition `audit` by date and archive cold data.
- **Async execution**: API enqueues `run(request_id)` on a queue (SQS/Kafka); workers consume. Handlers are already resumable and optimistic-locked, so at-least-once delivery is safe. Return 202 + poll/webhook.
- **Dependencies**: add timeouts, circuit breaker, per-service rate limits; a scheduler (cron/delayed queue) can auto-retry `PENDING_RETRY` with jitter instead of manual retry.
- **Config**: store in DB/Git with approval workflow; cache by (name, version) — immutable so caching is trivially safe.
- **Observability**: emit metrics per stage (latency, outcome, retry counts); audit table doubles as event stream (outbox pattern).
- **Security/compliance**: authN/Z on `/admin` and `/review`, PII masking in audit, retention policies.
