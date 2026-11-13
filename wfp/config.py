"""Workflow configuration: validation + versioned registry.

A published (name, version) is IMMUTABLE. To change a rule you publish version N+1.
New requests use the latest version; each request stores a snapshot of the version it started with.
"""
import json
from pathlib import Path

from .errors import InvalidConfig, UnknownWorkflow
from .rules import validate_condition

STAGE_TYPES = {"rules", "branch", "dependency", "terminal"}
OUTCOMES = {"APPROVED", "REJECTED", "MANUAL_REVIEW"}
ON_FAIL = {"REJECT", "MANUAL_REVIEW", "CONTINUE"}


def _edges(stage):
    t = stage.get("type")
    if t in ("rules", "dependency"):
        return [stage.get("next")]
    if t == "branch":
        return [b.get("goto") for b in stage.get("branches", [])] + [stage.get("default")]
    return []


def validate_config(cfg):
    errs = []
    if not isinstance(cfg, dict):
        return ["config must be an object"]
    if not cfg.get("name"):
        errs.append("name is required")
    if not isinstance(cfg.get("version"), int):
        errs.append("version must be an integer")
    if not isinstance(cfg.get("schema", {}).get("fields"), dict):
        errs.append("schema.fields must be an object")
    stages = cfg.get("stages")
    if not isinstance(stages, dict) or not stages:
        return errs + ["stages must be a non-empty object"]
    if cfg.get("start") not in stages:
        errs.append(f"start stage '{cfg.get('start')}' does not exist")
    rule_ids = set()
    for name, st in stages.items():
        where = f"stages.{name}"
        t = st.get("type")
        if t not in STAGE_TYPES:
            errs.append(f"{where}: unknown type '{t}'")
            continue
        for target in _edges(st):
            if target not in stages:
                errs.append(f"{where}: points to missing stage '{target}'")
        if t == "rules":
            for r in st.get("rules", []):
                rid = r.get("id")
                if not rid or rid in rule_ids:
                    errs.append(f"{where}: rule id missing or duplicated ({rid})")
                rule_ids.add(rid)
                if r.get("on_fail", "REJECT") not in ON_FAIL:
                    errs.append(f"{where}.{rid}: on_fail must be one of {sorted(ON_FAIL)}")
                errs += validate_condition(r.get("check"), f"{where}.{rid}.check")
        elif t == "branch":
            for i, b in enumerate(st.get("branches", [])):
                errs += validate_condition(b.get("when"), f"{where}.branches[{i}].when")
        elif t == "dependency":
            for k in ("service", "result_key"):
                if not st.get(k):
                    errs.append(f"{where}: '{k}' is required")
            if st.get("on_exhausted", "PENDING_RETRY") not in ("PENDING_RETRY", "MANUAL_REVIEW"):
                errs.append(f"{where}: on_exhausted must be PENDING_RETRY or MANUAL_REVIEW")
        elif t == "terminal" and st.get("outcome") not in OUTCOMES:
            errs.append(f"{where}: outcome must be one of {sorted(OUTCOMES)}")
    if not errs:  # cycle detection (only meaningful once edges are valid)
        state = {}

        def dfs(n):
            state[n] = 1
            for m in _edges(stages[n]):
                if state.get(m) == 1:
                    return True
                if m not in state and dfs(m):
                    return True
            state[n] = 2
            return False

        if dfs(cfg["start"]):
            errs.append("stages contain a cycle")
    return errs


class Registry:
    def __init__(self, config_dir=None):
        self.configs = {}
        self.dir = Path(config_dir) if config_dir else None
        if self.dir:
            self.load_dir()

    def load_dir(self):
        for f in sorted(self.dir.glob("*.json")):
            self.register(json.loads(f.read_text()))

    def register(self, cfg, persist=False):
        errs = validate_config(cfg)
        if errs:
            raise InvalidConfig(errs)
        versions = self.configs.setdefault(cfg["name"], {})
        if cfg["version"] in versions and versions[cfg["version"]] != cfg:
            raise InvalidConfig([f"{cfg['name']} v{cfg['version']} already exists with different content; bump 'version'"])
        versions[cfg["version"]] = cfg
        if persist and self.dir:
            (self.dir / f"{cfg['name']}_v{cfg['version']}.json").write_text(json.dumps(cfg, indent=2))

    def latest(self, name):
        if name not in self.configs:
            raise UnknownWorkflow(f"unknown workflow '{name}'")
        return self.configs[name][max(self.configs[name])]

    def names(self):
        return {n: sorted(v) for n, v in self.configs.items()}
