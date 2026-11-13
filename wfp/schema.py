"""Tiny declarative payload validator driven by the workflow's `schema` config."""
import re


def _type_ok(value, t):
    if t == "string":
        return isinstance(value, str)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    return False


def validate_payload(payload, schema):
    """Return a list of human-readable errors (empty list == valid)."""
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    fields = schema.get("fields", {})
    errors = []
    for name, spec in fields.items():
        if payload.get(name) is None:
            if spec.get("required"):
                errors.append(f"{name}: required field is missing")
            continue
        value = payload[name]
        t = spec.get("type", "string")
        if not _type_ok(value, t):
            errors.append(f"{name}: expected {t}, got {type(value).__name__}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"{name}: must be one of {spec['enum']}")
        if t in ("number", "integer"):
            if "min" in spec and value < spec["min"]:
                errors.append(f"{name}: must be >= {spec['min']}")
            if "max" in spec and value > spec["max"]:
                errors.append(f"{name}: must be <= {spec['max']}")
        if t == "string":
            if "min_length" in spec and len(value) < spec["min_length"]:
                errors.append(f"{name}: shorter than {spec['min_length']} characters")
            if "max_length" in spec and len(value) > spec["max_length"]:
                errors.append(f"{name}: longer than {spec['max_length']} characters")
            if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
                errors.append(f"{name}: does not match pattern {spec['pattern']}")
    for key in payload:
        if key not in fields:
            errors.append(f"{key}: unknown field")
    return errors
