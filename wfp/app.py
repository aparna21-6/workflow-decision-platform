"""Composition root: wires store + registry + simulated dependencies + engine."""
from pathlib import Path

from .config import Registry
from .dependencies import DependencyRegistry, SimulatedFraudScoreService, SimulatedSanctionsService
from .engine import Engine
from .store import Store

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def build_engine(db_path="wfp.db", config_dir=DEFAULT_CONFIG_DIR, deps=None, sleep=None):
    store = Store(db_path)
    registry = Registry(config_dir)
    if deps is None:
        deps = DependencyRegistry()
        deps.register("sanctions", SimulatedSanctionsService())
        deps.register("fraud_score", SimulatedFraudScoreService())
    kwargs = {"sleep": sleep} if sleep else {}
    return Engine(store, registry, deps, **kwargs)
