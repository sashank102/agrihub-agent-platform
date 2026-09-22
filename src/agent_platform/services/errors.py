"""Domain errors raised by run and thread orchestration."""


class UnsupportedRunOption(Exception):
    """The client sent a run field this server does not implement."""

    def __init__(self, detail: str) -> None:
        """Store the client-facing explanation."""
        self.detail = detail
        super().__init__(detail)


class ActiveRunConflict(Exception):
    """The thread already has a pending or running run."""

    def __init__(self) -> None:
        """Explain the multitask rejection."""
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
