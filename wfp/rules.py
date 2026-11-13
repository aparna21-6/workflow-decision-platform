"""Rule/condition evaluator. Pure functions: no I/O, no state -> trivially testable.

Condition grammar (JSON):
  leaf      {"field": "input.amount", "op": "gte", "value": 100}
  all       {"all": [cond, cond, ...]}
  any       {"any": [cond, cond, ...]}
  not       {"not": cond}
Fields are dotted paths into the context: `input.*` (request data) or `ext.*` (dependency results).
Every leaf evaluated is recorded in a trace (field, op, expected, observed, result) for the audit log.
"""
import re

OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "matches", "exists", "not_exists"}


def get_path(ctx, path):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _compare(op, observed, expected):
    try:
        if op == "eq":
            return observed == expected
        if op == "ne":
            return observed != expected
        if op == "gt":
            return observed > expected
        if op == "gte":
            return observed >= expected
        if op == "lt":
            return observed < expected
        if op == "lte":
            return observed <= expected
        if op == "in":
            return observed in expected
        if op == "not_in":
            return observed not in expected
        if op == "contains":
            return expected in observed
        if op == "matches":
            return re.fullmatch(expected, observed) is not None
    except TypeError:
        return False  # incomparable types => condition is simply not satisfied
    raise ValueError(f"unknown operator {op}")


def _eval(cond, ctx, trace):
    if "all" in cond:
        return all([_eval(c, ctx, trace) for c in cond["all"]])  # list => no short-circuit, full trace
    if "any" in cond:
        return any([_eval(c, ctx, trace) for c in cond["any"]])
    if "not" in cond:
        return not _eval(cond["not"], ctx, trace)
    found, observed = get_path(ctx, cond["field"])
    op = cond["op"]
    if op == "exists":
        result = found
    elif op == "not_exists":
        result = not found
    elif not found:
        result = False  # missing data never silently passes a check
    else:
        result = _compare(op, observed, cond.get("value"))
    trace.append({"field": cond["field"], "op": op, "expected": cond.get("value"),
                  "observed": observed if found else None, "found": found, "result": bool(result)})
    return bool(result)


def evaluate(cond, ctx):
    """Return (passed: bool, trace: list[dict])."""
    trace = []
    return _eval(cond, ctx, trace), trace


def validate_condition(cond, where):
    errs = []
    if not isinstance(cond, dict):
        return [f"{where}: condition must be an object"]
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            return [f"{where}: '{key}' needs a non-empty list"]
        for i, c in enumerate(cond[key]):
            errs += validate_condition(c, f"{where}.{key}[{i}]")
    elif "not" in cond:
        errs += validate_condition(cond["not"], f"{where}.not")
    else:
        if "field" not in cond or "op" not in cond:
            errs.append(f"{where}: leaf needs 'field' and 'op'")
        elif cond["op"] not in OPS:
            errs.append(f"{where}: unknown operator '{cond['op']}'")
        elif cond["op"] not in ("exists", "not_exists") and "value" not in cond:
            errs.append(f"{where}: operator '{cond['op']}' needs 'value'")
    return errs
