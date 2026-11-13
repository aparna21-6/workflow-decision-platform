"""Workflow engine: executes stages defined in config, persists every step, handles failures.

Stage handlers are pure-ish: they take (request, stage, context) and RETURN what should happen
(new status, next stage, new context, audit entries). Only `_commit` writes to the database,
so every step is one atomic transaction and a crash can never leave half-written state.
"""
import copy
import hashlib
import json
import time
import uuid

from .errors import (ConflictError, IdempotencyConflict, IllegalTransition, InvalidRequest,
                     PersistenceError, StoreError, DependencyError, WorkflowError)
from .rules import evaluate, get_path
from .schema import validate_payload

ALLOWED = {  # lifecycle state machine
    "RECEIVED": {"IN_PROGRESS"},
    "IN_PROGRESS": {"IN_PROGRESS", "PENDING_RETRY", "MANUAL_REVIEW", "APPROVED", "REJECTED"},
    "PENDING_RETRY": {"IN_PROGRESS"},
    "MANUAL_REVIEW": {"APPROVED", "REJECTED"},
    "APPROVED": set(),
    "REJECTED": set(),
}
STOP_STATES = {"APPROVED", "REJECTED", "MANUAL_REVIEW"}
MAX_STEPS = 100  # safety net against runaway configs (config validation already forbids cycles)


class Engine:
    def __init__(self, store, registry, deps, sleep=time.sleep, commit_retries=3):
        self.store, self.registry, self.deps = store, registry, deps
        self.sleep, self.commit_retries = sleep, commit_retries

    # ------------------------------------------------------------------ public API
    def submit(self, workflow, payload, idempotency_key=None):
        """Validate -> idempotent create -> run. Returns (request_row, duplicate_flag)."""
        cfg = self.registry.latest(workflow)
        errors = validate_payload(payload, cfg["schema"])
        if errors:
            raise InvalidRequest(errors)
        phash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        key = idempotency_key or "auto:" + phash
        req, created = self.store.create_request(uuid.uuid4().hex, cfg, key, phash, payload)
        if not created and req["payload_hash"] != phash:
            raise IdempotencyConflict(f"idempotency key '{key}' was already used with a different payload")
        if created or req["status"] == "RECEIVED":  # RECEIVED+duplicate == earlier run crashed: resume it
            req = self.run(req["id"])
        return req, not created

    def run(self, rid):
        """Drive a request forward until it stops (final, manual review, or pending retry)."""
        req = self.store.get(rid)
        if req["status"] in STOP_STATES:
            return req
        try:
            if req["status"] in ("RECEIVED", "PENDING_RETRY"):
                resumed = req["status"] == "PENDING_RETRY"
                req = self._commit(req, "IN_PROGRESS", req["current_stage"], req["context"],
                                   [{"kind": "SYSTEM", "stage": req["current_stage"],
                                     "message": "Processing resumed after retry request" if resumed else "Processing started"}],
                                   "resumed" if resumed else "started")
            for _ in range(MAX_STEPS):
                stage_name = req["current_stage"]
                stage = req["config"]["stages"][stage_name]
                handler = getattr(self, "_do_" + stage["type"])
                status, nxt, ctx, audits, note = handler(req, stage_name, stage, copy.deepcopy(req["context"]))
                req = self._commit(req, status, nxt, ctx, audits, note)
                if status != "IN_PROGRESS":
                    return req
            raise WorkflowError("MAX_STEPS exceeded")
        except ConflictError:
            return self.store.get(rid)  # another worker advanced it; its state is authoritative

    def retry(self, rid):
        req = self.store.get(rid)
        if req["status"] != "PENDING_RETRY":
            raise IllegalTransition(f"only PENDING_RETRY requests can be retried (status is {req['status']})")
        return self.run(rid)

    def resolve(self, rid, decision, reviewer, note=""):
        """Human decision on a MANUAL_REVIEW request."""
        if decision not in ("APPROVED", "REJECTED"):
            raise InvalidRequest(["decision must be APPROVED or REJECTED"])
        req = self.store.get(rid)
        return self._commit(req, decision, req["current_stage"], req["context"],
                            [{"kind": "MANUAL_DECISION", "stage": req["current_stage"],
                              "message": f"Manual review decision by {reviewer}: {decision}",
                              "detail": {"reviewer": reviewer, "note": note}}],
                            f"manual decision by {reviewer}")

    # ------------------------------------------------------------------ persistence step
    def _commit(self, req, status, stage, ctx, audits, note):
        if status not in ALLOWED[req["status"]]:
            raise IllegalTransition(f"{req['status']} -> {status} is not allowed")
        history = [{"from": req["status"], "to": status, "stage": stage, "note": note}]
        last = None
        for _ in range(self.commit_retries):  # a failed write rolled back fully, so repeating it is safe
            try:
                return self.store.commit_step(req["id"], req["version"], status, stage, ctx, history, audits)
            except StoreError as e:
                last = e
        raise PersistenceError(f"could not save step after {self.commit_retries} attempts: {last}")

    # ------------------------------------------------------------------ stage handlers
    def _do_rules(self, req, name, stage, ctx):
        audits, rejects, reviews = [], [], []
        for rule in stage["rules"]:
            ok, trace = evaluate(rule["check"], ctx)
            on_fail = rule.get("on_fail", "REJECT")
            audits.append({"kind": "RULE", "stage": name, "rule_id": rule["id"], "message": rule["description"],
                           "detail": {"result": "PASS" if ok else "FAIL", "on_fail": on_fail, "conditions": trace}})
            if not ok:
                {"REJECT": rejects, "MANUAL_REVIEW": reviews}.get(on_fail, []).append(rule["id"])
        if rejects:
            audits.append({"kind": "DECISION", "stage": name, "message": f"Rejected: failed mandatory rule(s) {rejects}",
                           "detail": {"failed_rules": rejects}})
            return "REJECTED", name, ctx, audits, f"rejected by {rejects}"
        if reviews:
            audits.append({"kind": "DECISION", "stage": name, "message": f"Routed to manual review: rule(s) {reviews} failed",
                           "detail": {"failed_rules": reviews}})
            return "MANUAL_REVIEW", name, ctx, audits, f"manual review due to {reviews}"
        return "IN_PROGRESS", stage["next"], ctx, audits, f"stage '{name}' passed"

    def _do_branch(self, req, name, stage, ctx):
        audits = []
        for i, b in enumerate(stage["branches"]):
            ok, trace = evaluate(b["when"], ctx)
            audits.append({"kind": "BRANCH", "stage": name, "message": f"Branch #{i + 1} -> '{b['goto']}': {'taken' if ok else 'not taken'}",
                           "detail": {"taken": ok, "conditions": trace}})
            if ok:
                return "IN_PROGRESS", b["goto"], ctx, audits, f"branch to '{b['goto']}'"
        audits.append({"kind": "BRANCH", "stage": name, "message": f"No branch matched; default -> '{stage['default']}'", "detail": {}})
        return "IN_PROGRESS", stage["default"], ctx, audits, f"default branch to '{stage['default']}'"

    def _resolve_params(self, params, ctx):
        out = {}
        for k, v in params.items():
            if isinstance(v, str) and v.startswith(("input.", "ext.")):
                out[k] = get_path(ctx, v)[1]
            else:
                out[k] = v
        return out

    def _do_dependency(self, req, name, stage, ctx):
        params = self._resolve_params(stage.get("params", {}), ctx)
        max_attempts = stage.get("max_attempts", 3)
        base = stage.get("backoff_seconds", 0.2)
        audits = []
        for attempt in range(1, max_attempts + 1):
            try:
                result = self.deps.call(stage["service"], params)
            except DependencyError as e:
                audits.append({"kind": "DEPENDENCY", "stage": name,
                               "message": f"Call to '{stage['service']}' failed (attempt {attempt}/{max_attempts}): {e}",
                               "detail": {"service": stage["service"], "attempt": attempt, "ok": False, "error": str(e)}})
                if attempt < max_attempts:
                    self.sleep(base * (2 ** (attempt - 1)))  # exponential backoff
                continue
            ctx["ext"][stage["result_key"]] = result
            audits.append({"kind": "DEPENDENCY", "stage": name,
                           "message": f"Call to '{stage['service']}' succeeded (attempt {attempt}/{max_attempts})",
                           "detail": {"service": stage["service"], "attempt": attempt, "ok": True, "params": params, "response": result}})
            return "IN_PROGRESS", stage["next"], ctx, audits, f"dependency '{stage['service']}' ok"
        status = stage.get("on_exhausted", "PENDING_RETRY")
        audits.append({"kind": "DECISION", "stage": name,
                       "message": f"'{stage['service']}' unavailable after {max_attempts} attempts -> {status}",
                       "detail": {"service": stage["service"], "on_exhausted": status}})
        return status, name, ctx, audits, f"dependency '{stage['service']}' exhausted"

    def _do_terminal(self, req, name, stage, ctx):
        outcome = stage["outcome"]
        reason = stage.get("reason", "")
        return outcome, name, ctx, [{"kind": "DECISION", "stage": name, "message": f"Final outcome {outcome}" + (f": {reason}" if reason else ""),
                                      "detail": {"outcome": outcome, "reason": reason}}], f"reached terminal '{name}'"

    # ------------------------------------------------------------------ explanation
    def explain(self, rid):
        req = self.store.get(rid)
        audit = self.store.audit(rid)
        rules = [{"rule_id": a["rule_id"], "stage": a["stage"], "description": a["message"],
                  "result": a["detail"]["result"], "on_fail": a["detail"]["on_fail"], "conditions": a["detail"]["conditions"]}
                 for a in audit if a["kind"] == "RULE"]
        path = []
        for a in audit:
            if a["stage"] and (not path or path[-1] != a["stage"]):
                path.append(a["stage"])
        lines = [f"Request {rid} was processed by workflow '{req['workflow']}' v{req['workflow_version']} and is {req['status']}."]
        for r in rules:
            if r["result"] == "FAIL":
                detail = "; ".join(f"{c['field']} {c['op']} {c['expected']!r} (observed {c['observed']!r})"
                                   for c in r["conditions"] if not c["result"]) or "see conditions"
                lines.append(f"Rule {r['rule_id']} FAILED [{r['description']}] -> {r['on_fail']}: {detail}.")
        passed = [r["rule_id"] for r in rules if r["result"] == "PASS"]
        if passed:
            lines.append(f"Rules passed: {', '.join(passed)}.")
        for a in audit:
            if a["kind"] == "DEPENDENCY":
                lines.append(a["message"] + ".")
            elif a["kind"] in ("DECISION", "MANUAL_DECISION"):
                lines.append(a["message"] + ".")
        return {"request_id": rid, "workflow": req["workflow"], "workflow_version": req["workflow_version"],
                "status": req["status"], "final_stage": req["current_stage"], "input": req["payload"],
                "stage_path": path, "rules_evaluated": rules,
                "failed_rules": [r["rule_id"] for r in rules if r["result"] == "FAIL"], "reasoning": lines}
