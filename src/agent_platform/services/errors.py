"""Domain errors raised by run and thread orchestration."""


class UnsupportedRunOption(Exception):
    """The client sent a run field this server does not implement."""

    def __init__(self, detail: str) -> None:
        """Store the client-facing explanation."""
        self.detail = detail
        super().__init__(detail)


class ActiveRunConflict(Exception):
    """The thread already has a pending or running run."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        reconciliation_intent: str | None = None,
        graph_succeeded: bool | None = None,
    ) -> None:
        """Explain the rejection and keep operator-visible reconciliation state."""
        self.run_id = run_id
        self.status = status
        self.reconciliation_intent = reconciliation_intent
        self.graph_succeeded = graph_succeeded
        super().__init__("thread already has an active run")


class ThreadNotInterrupted(Exception):
    """A resume command arrived while the checkpoint is not interrupted."""

    def __init__(self) -> None:
        """Explain why resume was rejected."""
        super().__init__("thread is not interrupted")


class CheckpointReferenceError(Exception):
    """A checkpoint reference is missing or belongs to another thread."""

    def __init__(self, detail: str, status_code: int) -> None:
        """Store the HTTP status that should represent this reference error."""
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


class RunCursorError(Exception):
    """A reconnect cursor is not an integer event sequence."""

    def __init__(self) -> None:
        """Explain the invalid cursor."""
        super().__init__("invalid last event id")


class CancellationNotSettled(Exception):
    """Cancellation was requested but no durable terminal state was confirmed."""

    def __init__(self, run_id: str, status: str) -> None:
        """Keep the actual durable status for an operator-visible 503 response."""
        self.run_id = run_id
        self.status = status
        super().__init__("cancellation has not reached a durable terminal state")
