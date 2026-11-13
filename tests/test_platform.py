import copy
import json
import threading
import unittest
from pathlib import Path

from wfp.config import Registry, validate_config
from wfp.dependencies import DependencyRegistry, SimulatedFraudScoreService, SimulatedSanctionsService
from wfp.engine import Engine
from wfp.errors import (IdempotencyConflict, IllegalTransition, InvalidConfig, InvalidRequest,
                        PersistenceError, StoreError)
from wfp.store import Store

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
GOOD = {"vendor_name": "Acme Supplies Pvt Ltd", "country": "IN", "tax_id": "ABCDE1234F", "annual_revenue": 2500000,
        "years_in_business": 6, "has_insurance": True, "requested_credit_limit": 80000}


def make(sanctions=None, fraud=None):
    store = Store(":memory:")
    reg = Registry(CONFIG_DIR)
    deps = DependencyRegistry()
    sanctions = sanctions or SimulatedSanctionsService()
    fraud = fraud or SimulatedFraudScoreService()
    deps.register("sanctions", sanctions)
    deps.register("fraud_score", fraud)
    return Engine(store, reg, deps, sleep=lambda s: None), sanctions, fraud


class HappyAndRules(unittest.TestCase):
    def test_happy_path_approved_with_full_trail(self):
        e, svc, _ = make()
        req, dup = e.submit("vendor_approval", GOOD)
        self.assertEqual(req["status"], "APPROVED")
        self.assertFalse(dup)
        hist = e.store.history(req["id"])
        self.assertEqual(hist[0]["to_status"], "RECEIVED")
        self.assertEqual(hist[-1]["to_status"], "APPROVED")
        kinds = [a["kind"] for a in e.store.audit(req["id"])]
        for k in ("SYSTEM", "RULE", "DEPENDENCY", "BRANCH", "DECISION"):
            self.assertIn(k, kinds)
        seqs = [a["seq"] for a in e.store.audit(req["id"])]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_mandatory_rule_rejects_and_explains(self):
        e, svc, _ = make()
        req, _ = e.submit("vendor_approval", {**GOOD, "has_insurance": False})
        self.assertEqual(req["status"], "REJECTED")
        self.assertEqual(svc.calls, 0, "dependency must not be called after a mandatory reject")
        ex = e.explain(req["id"])
        self.assertEqual(ex["failed_rules"], ["V1"])
        self.assertTrue(any("V1" in l and "has_insurance" in l for l in ex["reasoning"]))

    def test_threshold_reject_and_dependency_based_reject(self):
        e, _, _ = make()
        r1, _ = e.submit("vendor_approval", {**GOOD, "annual_revenue": 1000, "tax_id": "AAAAAAAA11"})
        self.assertEqual(r1["status"], "REJECTED")
        self.assertEqual(e.explain(r1["id"])["failed_rules"], ["T1"])
        r2, _ = e.submit("vendor_approval", {**GOOD, "country": "KP", "tax_id": "BBBBBBBB22"})
        self.assertEqual(r2["status"], "REJECTED")
        self.assertEqual(e.explain(r2["id"])["failed_rules"], ["S1"])

    def test_conditional_branch_to_enhanced_review_then_manual(self):
        e, _, _ = make()
        req, _ = e.submit("vendor_approval", {**GOOD, "requested_credit_limit": 200000, "years_in_business": 2, "annual_revenue": 300000})
        self.assertEqual(req["status"], "MANUAL_REVIEW")
        self.assertIn("enhanced_review", e.explain(req["id"])["stage_path"])
        self.assertEqual(sorted(e.explain(req["id"])["failed_rules"]), ["E1", "E2"])

    def test_branch_high_exposure_passes_enhanced(self):
        e, _, _ = make()
        req, _ = e.submit("vendor_approval", {**GOOD, "requested_credit_limit": 200000})
        self.assertEqual(req["status"], "APPROVED")
        self.assertIn("enhanced_review", e.explain(req["id"])["stage_path"])

    def test_second_workflow_same_engine_claims(self):
        e, _, fraud = make()
        base = {"claim_type": "auto", "claim_amount": 5000, "incident_date": "2026-10-30", "policy_active": True, "documents_attached": True}
        self.assertEqual(e.submit("claim_processing", base)[0]["status"], "APPROVED")
        self.assertEqual(e.submit("claim_processing", {**base, "claim_amount": 60000})[0]["status"], "MANUAL_REVIEW")
        self.assertEqual(e.submit("claim_processing", {**base, "claim_amount": 90000})[0]["status"], "REJECTED")
        self.assertEqual(e.submit("claim_processing", {**base, "policy_active": False})[0]["status"], "REJECTED")


class InvalidInput(unittest.TestCase):
    def test_schema_errors_reported_and_nothing_stored(self):
        e, _, _ = make()
        bad = {"vendor_name": "X", "country": "india", "annual_revenue": "lots", "years_in_business": 3,
               "has_insurance": True, "requested_credit_limit": 10, "surprise": 1}
        with self.assertRaises(InvalidRequest) as cm:
            e.submit("vendor_approval", bad)
        text = " | ".join(cm.exception.errors)
        for needle in ("tax_id", "country", "annual_revenue", "vendor_name", "surprise"):
            self.assertIn(needle, text)
        self.assertEqual(e.store.count_requests(), 0)

    def test_non_object_payload(self):
        e, _, _ = make()
        with self.assertRaises(InvalidRequest):
            e.submit("vendor_approval", [1, 2, 3])

    def test_bool_is_not_a_number(self):
        e, _, _ = make()
        with self.assertRaises(InvalidRequest):
            e.submit("vendor_approval", {**GOOD, "annual_revenue": True})


class Idempotency(unittest.TestCase):
    def test_duplicate_key_returns_same_request_without_reprocessing(self):
        e, svc, _ = make()
        r1, d1 = e.submit("vendor_approval", GOOD, "key-1")
        calls, audit_n = svc.calls, len(e.store.audit(r1["id"]))
        r2, d2 = e.submit("vendor_approval", GOOD, "key-1")
        self.assertFalse(d1)
        self.assertTrue(d2)
        self.assertEqual(r1["id"], r2["id"])
        self.assertEqual(svc.calls, calls)
        self.assertEqual(len(e.store.audit(r1["id"])), audit_n)
        self.assertEqual(e.store.count_requests(), 1)

    def test_same_key_different_payload_conflicts(self):
        e, _, _ = make()
        e.submit("vendor_approval", GOOD, "key-1")
        with self.assertRaises(IdempotencyConflict):
            e.submit("vendor_approval", {**GOOD, "annual_revenue": 1}, "key-1")

    def test_auto_key_from_payload_hash(self):
        e, _, _ = make()
        a, _ = e.submit("vendor_approval", GOOD)
        b, dup = e.submit("vendor_approval", dict(reversed(list(GOOD.items()))))  # key order must not matter
        self.assertTrue(dup)
        self.assertEqual(a["id"], b["id"])

    def test_concurrent_duplicates_create_one_request(self):
        e, _, _ = make()
        ids, errors = [], []

        def worker():
            try:
                ids.append(e.submit("vendor_approval", GOOD, "race")[0]["id"])
            except Exception as ex:  # pragma: no cover
                errors.append(ex)

        ts = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(e.store.count_requests(), 1)
        self.assertEqual(e.store.get(ids[0])["status"], "APPROVED")


class DependencyFailures(unittest.TestCase):
    def test_transient_failure_recovered_by_inline_retries(self):
        e, svc, _ = make(sanctions=SimulatedSanctionsService(fail_first=2))
        req, _ = e.submit("vendor_approval", GOOD)
        self.assertEqual(req["status"], "APPROVED")
        attempts = [a["detail"]["ok"] for a in e.store.audit(req["id"]) if a["kind"] == "DEPENDENCY"]
        self.assertEqual(attempts, [False, False, True])

    def test_exhausted_goes_pending_retry_then_manual_retry_succeeds(self):
        svc = SimulatedSanctionsService(always_fail=True)
        e, _, _ = make(sanctions=svc)
        req, _ = e.submit("vendor_approval", GOOD)
        self.assertEqual(req["status"], "PENDING_RETRY")
        self.assertEqual(req["current_stage"], "sanctions_screen")
        self.assertEqual(svc.calls, 3)
        with self.assertRaises(IllegalTransition):
            e.resolve(req["id"], "APPROVED", "bob")  # not in manual review
        svc.always_fail = False  # dependency recovers
        req = e.retry(req["id"])
        self.assertEqual(req["status"], "APPROVED")
        path = [(h["from_status"], h["to_status"]) for h in e.store.history(req["id"])]
        self.assertIn(("IN_PROGRESS", "PENDING_RETRY"), path)
        self.assertIn(("PENDING_RETRY", "IN_PROGRESS"), path)
        with self.assertRaises(IllegalTransition):
            e.retry(req["id"])  # already final

    def test_mandatory_checks_not_repeated_on_retry(self):
        svc = SimulatedSanctionsService(always_fail=True)
        e, _, _ = make(sanctions=svc)
        req, _ = e.submit("vendor_approval", GOOD)
        before = len([a for a in e.store.audit(req["id"]) if a["rule_id"] == "V1"])
        svc.always_fail = False
        e.retry(req["id"])
        after = len([a for a in e.store.audit(req["id"]) if a["rule_id"] == "V1"])
        self.assertEqual(before, after)

    def test_exhausted_can_route_to_manual_review_via_config(self):
        e, _, fraud = make(fraud=SimulatedFraudScoreService(fail_first=99))
        req, _ = e.submit("claim_processing", {"claim_type": "auto", "claim_amount": 100, "incident_date": "2026-01-01",
                                               "policy_active": True, "documents_attached": True})
        self.assertEqual(req["status"], "MANUAL_REVIEW")


class PartialSaveFailures(unittest.TestCase):
    def test_single_write_failure_is_retried_transparently(self):
        e, _, _ = make()
        state = {"n": 0}

        def fault():
            state["n"] += 1
            if state["n"] == 1:
                raise StoreError("disk hiccup")

        e.store.fault = fault
        req, _ = e.submit("vendor_approval", GOOD)
        self.assertEqual(req["status"], "APPROVED")
        seqs = [a["seq"] for a in e.store.audit(req["id"])]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)), "rolled-back attempt must leave no audit rows")

    def test_persistent_failure_leaves_consistent_state_and_is_recoverable(self):
        e, _, _ = make()

        def boom():
            raise StoreError("db down")

        e.store.fault = boom
        with self.assertRaises(PersistenceError):
            e.submit("vendor_approval", GOOD, "k")
        row = e.store.get_by_key("vendor_approval", "k")
        self.assertEqual(row["status"], "RECEIVED")  # nothing half-written
        self.assertEqual(len(e.store.audit(row["id"])), 1)
        e.store.fault = None
        req, dup = e.submit("vendor_approval", GOOD, "k")  # client retries with same key
        self.assertTrue(dup)
        self.assertEqual(req["id"], row["id"])
        self.assertEqual(req["status"], "APPROVED")


class ManualReview(unittest.TestCase):
    def test_manual_review_resolution_and_finality(self):
        e, _, _ = make()
        req, _ = e.submit("vendor_approval", {**GOOD, "requested_credit_limit": 600000})
        self.assertEqual(req["status"], "MANUAL_REVIEW")
        req = e.resolve(req["id"], "APPROVED", "alice", "verified references")
        self.assertEqual(req["status"], "APPROVED")
        audit = e.store.audit(req["id"])
        self.assertEqual(audit[-1]["kind"], "MANUAL_DECISION")
        self.assertEqual(audit[-1]["detail"]["reviewer"], "alice")
        with self.assertRaises(IllegalTransition):
            e.resolve(req["id"], "REJECTED", "mallory")
        with self.assertRaises(InvalidRequest):
            e.resolve(req["id"], "MAYBE", "alice")


class RuleChanges(unittest.TestCase):
    def test_new_version_applies_to_new_requests_old_requests_keep_theirs(self):
        e, _, _ = make()
        small = {**GOOD, "annual_revenue": 30000, "tax_id": "SMALL1234567"}
        old, _ = e.submit("vendor_approval", small)
        self.assertEqual(old["status"], "REJECTED")  # v1: T1 needs 50,000
        v2 = copy.deepcopy(e.registry.latest("vendor_approval"))
        v2["version"] = 2
        v2["stages"]["risk_thresholds"]["rules"][0]["check"]["value"] = 20000
        v2["stages"]["risk_thresholds"]["rules"][0]["description"] = "Annual revenue must be at least 20,000"
        e.registry.register(v2)
        new, _ = e.submit("vendor_approval", {**small, "tax_id": "SMALL7654321"})
        self.assertEqual(new["workflow_version"], 2)
        self.assertEqual(new["status"], "APPROVED")
        self.assertEqual(e.store.get(old["id"])["workflow_version"], 1)
        self.assertEqual(e.store.get(old["id"])["status"], "REJECTED")

    def test_in_flight_request_keeps_pinned_rules_after_change(self):
        svc = SimulatedSanctionsService(always_fail=True)
        e, _, _ = make(sanctions=svc)
        req, _ = e.submit("vendor_approval", {**GOOD, "annual_revenue": 30000})
        self.assertEqual(req["status"], "PENDING_RETRY")
        v2 = copy.deepcopy(e.registry.latest("vendor_approval"))
        v2["version"] = 2
        v2["stages"]["risk_thresholds"]["rules"][0]["check"]["value"] = 20000
        e.registry.register(v2)
        svc.always_fail = False
        self.assertEqual(e.retry(req["id"])["status"], "REJECTED")  # still judged by v1

    def test_published_version_is_immutable(self):
        e, _, _ = make()
        changed = copy.deepcopy(e.registry.latest("vendor_approval"))
        changed["stages"]["risk_thresholds"]["rules"][0]["check"]["value"] = 1
        with self.assertRaises(InvalidConfig):
            e.registry.register(changed)  # same version, different content

    def test_invalid_configs_are_rejected_before_use(self):
        base = json.loads((CONFIG_DIR / "vendor_approval.json").read_text())
        bad = copy.deepcopy(base); bad["version"] = 9
        bad["stages"]["mandatory_checks"]["next"] = "nowhere"
        self.assertTrue(any("missing stage" in x for x in validate_config(bad)))
        bad = copy.deepcopy(base); bad["version"] = 9
        bad["stages"]["mandatory_checks"]["rules"][0]["check"]["op"] = "approx"
        self.assertTrue(any("unknown operator" in x for x in validate_config(bad)))
        bad = copy.deepcopy(base); bad["version"] = 9
        bad["stages"]["approve"] = {"type": "rules", "rules": [], "next": "mandatory_checks"}
        self.assertTrue(any("cycle" in x for x in validate_config(bad)))

    def test_brand_new_workflow_needs_only_config(self):
        e, _, _ = make()
        e.registry.register({
            "name": "doc_verification", "version": 1,
            "schema": {"fields": {"doc_type": {"type": "string", "required": True, "enum": ["pan", "passport"]},
                                  "confidence": {"type": "number", "required": True}}},
            "start": "check",
            "stages": {"check": {"type": "rules", "rules": [
                {"id": "D1", "description": "OCR confidence >= 0.9", "on_fail": "MANUAL_REVIEW",
                 "check": {"field": "input.confidence", "op": "gte", "value": 0.9}}], "next": "ok"},
                "ok": {"type": "terminal", "outcome": "APPROVED"}}})
        self.assertEqual(e.submit("doc_verification", {"doc_type": "pan", "confidence": 0.95})[0]["status"], "APPROVED")
        self.assertEqual(e.submit("doc_verification", {"doc_type": "pan", "confidence": 0.5})[0]["status"], "MANUAL_REVIEW")


class Explainability(unittest.TestCase):
    def test_explanation_contains_input_rules_and_data_references(self):
        e, _, _ = make()
        req, _ = e.submit("vendor_approval", {**GOOD, "requested_credit_limit": 600000})
        ex = e.explain(req["id"])
        self.assertEqual(ex["input"]["requested_credit_limit"], 600000)
        t2 = [r for r in ex["rules_evaluated"] if r["rule_id"] == "T2"][0]
        self.assertEqual(t2["result"], "FAIL")
        cond = t2["conditions"][0]
        self.assertEqual((cond["field"], cond["op"], cond["expected"], cond["observed"]), ("input.requested_credit_limit", "lte", 500000, 600000))
        self.assertTrue(any("manual review" in l.lower() for l in ex["reasoning"]))


if __name__ == "__main__":
    unittest.main()
