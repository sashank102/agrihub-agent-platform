"""Authenticated tenant identity for graph execution and store namespaces."""

from contextvars import ContextVar
from dataclasses import dataclass

tenant_user_id: ContextVar[str | None] = ContextVar("tenant_user_id", default=None)


@dataclass(frozen=True, slots=True)
class TenantIdentity:
    """Identity copied into LangGraph context, config, and metadata."""

    user_id: str
    thread_id: str
    run_id: str

    def as_dict(self) -> dict[str, str]:
        """Return the three identifiers graph code is allowed to read."""
        return {
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
        }


def current_tenant_user_id() -> str | None:
    """Return the user id bound for the in-process graph task."""
    return tenant_user_id.get()
