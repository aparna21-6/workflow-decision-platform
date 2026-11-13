"""Simulated EXTERNAL dependencies + a registry the engine calls them through.

The engine only knows `deps.call(name, params)`. Swapping a simulator for a real HTTP client
means registering a different callable; the engine and configs do not change.
"""
from .errors import DependencyError


class DependencyRegistry:
    def __init__(self):
        self.services = {}

    def register(self, name, fn):
        self.services[name] = fn

    def call(self, name, params):
        if name not in self.services:
            raise DependencyError(f"service '{name}' is not registered")
        return self.services[name](params)


class SimulatedSanctionsService:
    """Fake sanctions-screening API.

    fail_first=N     -> first N calls raise a timeout (transient outage)
    always_fail=True -> permanent outage
    name containing 'RETRYME' -> that vendor fails its first 3 calls (exhausts inline retries) (handy for CLI demos)
    """

    def __init__(self, sanctioned_countries=("KP", "IR"), fail_first=0, always_fail=False):
        self.sanctioned = set(sanctioned_countries)
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.calls = 0
        self._per_name = {}

    def __call__(self, params):
        self.calls += 1
        name = str(params.get("name", ""))
        if self.always_fail or self.calls <= self.fail_first:
            raise DependencyError("sanctions service timeout")
        if "RETRYME" in name.upper():
            n = self._per_name.get(name, 0) + 1
            self._per_name[name] = n
            if n <= 3:
                raise DependencyError("sanctions service temporarily unavailable (503)")
        return {"flagged": params.get("country") in self.sanctioned, "list_version": "2026-11"}


class SimulatedFraudScoreService:
    """Deterministic fake fraud model: score = claim amount / 1000, capped at 99."""

    def __init__(self, fail_first=0):
        self.fail_first = fail_first
        self.calls = 0

    def __call__(self, params):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise DependencyError("fraud model unavailable")
        return {"score": min(99, int(float(params["amount"]) / 1000)), "model": "sim-v1"}
