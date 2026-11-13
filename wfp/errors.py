"""All custom exceptions in one place so callers (API/CLI/tests) can map them to responses."""


class WorkflowError(Exception):
    pass


class UnknownWorkflow(WorkflowError):
    pass


class NotFound(WorkflowError):
    pass


class InvalidRequest(WorkflowError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


class InvalidConfig(WorkflowError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


class IdempotencyConflict(WorkflowError):
    """Same idempotency key re-used with a DIFFERENT payload."""


class IllegalTransition(WorkflowError):
    """Requested state change is not allowed by the lifecycle."""


class DependencyError(WorkflowError):
    """An external dependency failed (timeout, 5xx, ...)."""


class StoreError(WorkflowError):
    """A single database write failed (transient)."""


class ConflictError(WorkflowError):
    """Optimistic-lock failure: someone else changed the request first."""


class PersistenceError(WorkflowError):
    """Writes kept failing after retries. State on disk is still consistent."""
