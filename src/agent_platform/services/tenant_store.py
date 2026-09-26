"""Force every long-term store namespace to start with the authenticated user."""

import uuid
from collections.abc import Sequence
from typing import Any

from agent_platform.services.tenant_context import current_tenant_user_id


class TenantStore:
    """Prefix store operations with the user bound on the executing run.

    Tools cannot select another tenant by passing an owner id. A namespace that
    already starts with the authenticated user is kept. A namespace whose first
    element is a different user id is rejected. Any other first element is
    ordinary path data and is placed after the authenticated user id.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap a LangGraph store without taking ownership of its connection."""
        self._inner = inner

    def _namespace(self, namespace: Sequence[object]) -> tuple[str, ...]:
        user_id = current_tenant_user_id()
        if not user_id:
            raise RuntimeError(
                "long-term store access requires an authenticated user"
            )
        parts = tuple(str(part) for part in namespace)
        if parts[:1] == (user_id,):
            return parts
        if parts and _is_foreign_user(parts[0], user_id):
            raise PermissionError("store namespace belongs to another tenant")
        return (user_id, *parts)

    async def aget(
        self,
        namespace: tuple[str, ...],
        key: str,
        **kwargs: Any,
    ) -> Any:
        """Read one item from the caller's namespace."""
        return await self._inner.aget(self._namespace(namespace), key, **kwargs)

    async def aput(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Write one item under the authenticated user."""
        await self._inner.aput(
            self._namespace(namespace),
            key,
            value,
            *args,
            **kwargs,
        )

    async def adelete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete one item from the caller's namespace."""
        await self._inner.adelete(self._namespace(namespace), key)

    async def asearch(
        self,
        namespace_prefix: tuple[str, ...],
        /,
        **kwargs: Any,
    ) -> Any:
        """Search only inside the authenticated user's prefix."""
        return await self._inner.asearch(self._namespace(namespace_prefix), **kwargs)

    async def alist_namespaces(
        self,
        *,
        prefix: Sequence[object] | None = None,
        suffix: Sequence[object] | None = None,
        max_depth: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[str, ...]]:
        """List namespaces under the authenticated user."""
        return await self._inner.alist_namespaces(
            prefix=self._namespace(tuple(prefix or ())),
            suffix=None if suffix is None else tuple(str(part) for part in suffix),
            max_depth=None if max_depth is None else max_depth + 1,
            limit=limit,
            offset=offset,
        )

    def get(self, namespace: tuple[str, ...], key: str, **kwargs: Any) -> Any:
        """Read one item synchronously from the caller's namespace."""
        return self._inner.get(self._namespace(namespace), key, **kwargs)

    def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Write one item synchronously under the authenticated user."""
        self._inner.put(self._namespace(namespace), key, value, *args, **kwargs)

    def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete one item synchronously from the caller's namespace."""
        self._inner.delete(self._namespace(namespace), key)

    def search(self, namespace_prefix: tuple[str, ...], /, **kwargs: Any) -> Any:
        """Search synchronously inside the authenticated user's prefix."""
        return self._inner.search(self._namespace(namespace_prefix), **kwargs)


def _is_foreign_user(value: str, user_id: str) -> bool:
    """Return whether ``value`` is a user id other than the caller."""
    try:
        presented = uuid.UUID(value)
    except (TypeError, ValueError):
        return False
    try:
        caller = uuid.UUID(user_id)
    except (TypeError, ValueError):
        return True
    return presented != caller
