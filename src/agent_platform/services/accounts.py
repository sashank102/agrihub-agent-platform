"""User and API-key administration shared by the CLI and the API."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from agent_platform.core.settings import Settings
from agent_platform.db.models import ApiKey, User
from agent_platform.db.repositories.agents import AgentRepository
from agent_platform.db.repositories.api_keys import ApiKeyRepository
from agent_platform.db.repositories.audit_log import AuditLogRepository
from agent_platform.db.repositories.users import UserRepository
from agent_platform.db.session import AsyncSessionFactory, session_scope
from agent_platform.services.api_key_crypto import (
    generate_api_key,
    hash_api_key_secret,
    key_is_usable,
    parse_api_key,
    secrets_match,
)

logger = logging.getLogger(__name__)

AUTH_FAILURE_DETAIL = "invalid authentication credentials"


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """Metadata plus the one-time plaintext key."""

    id: uuid.UUID
    user_id: uuid.UUID
    key_prefix: str
    label: str | None
    expires_at: datetime | None
    plaintext: str


@dataclass(frozen=True, slots=True)
class ApiKeyMetadata:
    """Key fields that are safe to print and store in logs."""

    id: uuid.UUID
    user_id: uuid.UUID
    key_prefix: str
    label: str | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """The user established for one request."""

    user_id: uuid.UUID
    api_key_id: uuid.UUID | None
    auth_mode: str


@dataclass(frozen=True, slots=True)
class AuthenticationFailure:
    """A rejected credential. The reason is for audit, not for clients."""

    reason: str


class AccountService:
    """Create users and hashed API keys through the same repositories as HTTP."""

    def __init__(self, session_factory: AsyncSessionFactory, settings: Settings) -> None:
        """Bind the database and the pepper used for HMAC verification."""
        self.session_factory = session_factory
        self.settings = settings

    async def create_user(
        self,
        *,
        display_name: str,
        email: str | None = None,
    ) -> User:
        """Insert an active user and audit the creation."""
        async with session_scope(self.session_factory) as session:
            user = await UserRepository(session).create(
                display_name=display_name,
                email=email,
            )
            await AuditLogRepository(session).append(
                actor_user_id=user.id,
                action="user.created",
                resource_type="user",
                resource_id=str(user.id),
                metadata={"email_present": email is not None},
            )
            return user

    async def disable_user(self, user_id: uuid.UUID) -> User:
        """Disable a user. Deleted users stay deleted."""
        return await self._set_status(user_id, "disabled")

    async def enable_user(self, user_id: uuid.UUID) -> User:
        """Enable a disabled user. Deleted users cannot be enabled."""
        return await self._set_status(user_id, "active")

    async def delete_user(self, user_id: uuid.UUID) -> User:
        """Soft-delete a user and keep owned rows."""
        return await self._set_status(user_id, "deleted")

    async def issue_api_key(
        self,
        *,
        user_id: uuid.UUID,
        label: str | None = None,
        expires_at: datetime | None = None,
    ) -> IssuedApiKey:
        """Create a key, store only its prefix and HMAC, and return plaintext once."""
        self._require_pepper()
        async with session_scope(self.session_factory) as session:
            user = await UserRepository(session).get(user_id)
            if user is None or user.status != "active":
                raise LookupError("user not found")
            generated = await self._insert_unique_key(
                session,
                user_id=user.id,
                label=label,
                expires_at=expires_at,
            )
            await AuditLogRepository(session).append(
                actor_user_id=user.id,
                action="api_key.issued",
                resource_type="api_key",
                resource_id=str(generated.id),
                metadata={"key_prefix": generated.key_prefix, "label": label},
            )
            return generated

    async def list_api_keys(self, user_id: uuid.UUID) -> list[ApiKeyMetadata]:
        """Return key metadata. Hashes and plaintext are omitted."""
        async with session_scope(self.session_factory) as session:
            rows = await ApiKeyRepository(session).list_for_user(user_id)
            return [_metadata(row) for row in rows]

    async def revoke_api_key(self, key_id: uuid.UUID) -> ApiKeyMetadata:
        """Revoke a key. A second revoke is idempotent."""
        async with session_scope(self.session_factory) as session:
            repository = ApiKeyRepository(session)
            row = await repository.get(key_id)
            if row is None:
                raise LookupError("api key not found")
            if row.revoked_at is None:
                await repository.revoke(row, revoked_at=datetime.now(UTC))
                await AuditLogRepository(session).append(
                    actor_user_id=row.user_id,
                    action="api_key.revoked",
                    resource_type="api_key",
                    resource_id=str(row.id),
                    metadata={"key_prefix": row.key_prefix},
                )
            return _metadata(row)

    async def rotate_api_key(self, key_id: uuid.UUID) -> IssuedApiKey:
        """Revoke a key and issue a replacement for the same user."""
        self._require_pepper()
        async with session_scope(self.session_factory) as session:
            repository = ApiKeyRepository(session)
            current = await repository.get(key_id)
            if current is None:
                raise LookupError("api key not found")
            user = await UserRepository(session).get(current.user_id)
            if user is None or user.status != "active":
                raise LookupError("user not found")
            if current.revoked_at is None:
                await repository.revoke(current, revoked_at=datetime.now(UTC))
            issued = await self._insert_unique_key(
                session,
                user_id=current.user_id,
                label=current.label,
                expires_at=current.expires_at,
            )
            await AuditLogRepository(session).append(
                actor_user_id=current.user_id,
                action="api_key.rotated",
                resource_type="api_key",
                resource_id=str(issued.id),
                metadata={
                    "previous_key_id": str(current.id),
                    "key_prefix": issued.key_prefix,
                },
            )
            return issued

    async def update_agent_for_user(
        self,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        *,
        name: str | None = None,
    ) -> uuid.UUID:
        """Update an owned agent. Global agents are not mutable by ordinary users."""
        async with session_scope(self.session_factory) as session:
            repository = AgentRepository(session)
            visible = await repository.get_for_owner(agent_id, user_id)
            if visible is None:
                raise LookupError("agent not found")
            if visible.owner_user_id is None:
                await AuditLogRepository(session).append(
                    actor_user_id=user_id,
                    action="access.denied",
                    resource_type="agent",
                    resource_id=str(agent_id),
                    metadata={"scope": "global_agent_update"},
                )
                raise PermissionError("global agents cannot be updated by this user")
            if visible.owner_user_id != user_id:
                await AuditLogRepository(session).append(
                    actor_user_id=user_id,
                    action="access.denied",
                    resource_type="agent",
                    resource_id=str(agent_id),
                    metadata={"scope": "agent"},
                )
                raise LookupError("agent not found")
            updated = await repository.update_for_owner(agent_id, user_id, name=name)
            if updated is None:
                raise LookupError("agent not found")
            return updated.id

    async def read_artifact(
        self,
        user_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> uuid.UUID:
        """Return an owned artifact id or hide another tenant's artifact."""
        from agent_platform.db.models import Artifact
        from agent_platform.db.repositories.artifacts import ArtifactRepository

        foreign = False
        async with session_scope(self.session_factory) as session:
            artifact = await ArtifactRepository(session).get_for_owner(
                artifact_id,
                user_id,
            )
            if artifact is not None:
                return artifact.id
            raw = await session.get(Artifact, artifact_id)
            foreign = raw is not None and raw.owner_user_id != user_id
        if foreign:
            await self.record(
                action="access.denied",
                resource_type="artifact",
                resource_id=str(artifact_id),
                actor_user_id=user_id,
                metadata={"scope": "artifact"},
            )
        raise LookupError("artifact not found")

    async def update_global_agent(
        self,
        agent_id: uuid.UUID,
        *,
        name: str | None = None,
        active: bool | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Update a global agent from the admin CLI and audit the change."""
        async with session_scope(self.session_factory) as session:
            agent = await AgentRepository(session).update_global(
                agent_id,
                name=name,
                active=active,
            )
            if agent is None:
                raise LookupError("global agent not found")
            await AuditLogRepository(session).append(
                actor_user_id=actor_user_id,
                action="agent.global_updated",
                resource_type="agent",
                resource_id=str(agent.id),
                metadata={"name_changed": name is not None, "active": active},
            )
            return agent.id

    async def authenticate(
        self,
        token: str,
    ) -> AuthenticatedPrincipal | AuthenticationFailure:
        """Verify a key and optionally refresh its throttled last-used time."""
        pepper = self.settings.API_KEY_PEPPER
        if not pepper:
            return AuthenticationFailure("rejected")
        parsed = parse_api_key(
            token,
            lookup_prefix_length=self.settings.API_KEY_LOOKUP_PREFIX_LENGTH,
        )
        if parsed is None:
            await self._audit_auth_failure("malformed")
            return AuthenticationFailure("malformed")
        now = datetime.now(UTC)
        async with session_scope(self.session_factory) as session:
            rows = await ApiKeyRepository(session).list_by_prefix(parsed.key_prefix)
            matched: ApiKey | None = None
            for row in rows:
                if secrets_match(row.secret_hash, parsed.secret, pepper):
                    matched = row
            if matched is None:
                # Spend a comparison even when the prefix is unknown.
                secrets_match(
                    hash_api_key_secret("missing-key", pepper),
                    parsed.secret,
                    pepper,
                )
                await AuditLogRepository(session).append(
                    action="auth.failed",
                    resource_type="api_key",
                    resource_id=parsed.key_prefix,
                    metadata={"reason": "rejected"},
                )
                return AuthenticationFailure("rejected")
            user = await UserRepository(session).get(matched.user_id)
            usable = key_is_usable(
                revoked_at=matched.revoked_at,
                expires_at=matched.expires_at,
                now=now,
            )
            if user is None or user.status != "active" or not usable:
                reason = "inactive" if user is None or user.status != "active" else "rejected"
                await AuditLogRepository(session).append(
                    actor_user_id=matched.user_id,
                    action="auth.failed",
                    resource_type="api_key",
                    resource_id=str(matched.id),
                    metadata={"reason": reason, "key_prefix": matched.key_prefix},
                )
                return AuthenticationFailure(reason)
            interval = self.settings.API_KEY_LAST_USED_MIN_INTERVAL_SECONDS
            last_used = matched.last_used_at
            if last_used is not None and last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=UTC)
            should_touch = last_used is None or (now - last_used) >= timedelta(
                seconds=interval
            )
            if should_touch:
                await ApiKeyRepository(session).touch_last_used(matched, used_at=now)
            return AuthenticatedPrincipal(
                user_id=user.id,
                api_key_id=matched.id,
                auth_mode="api_key",
            )

    async def record_auth_failure(self, reason: str) -> None:
        """Audit a failure that did not reach key verification."""
        await self._audit_auth_failure(reason)

    async def record(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        actor_user_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit row outside the caller's transaction."""
        try:
            async with session_scope(self.session_factory) as session:
                await AuditLogRepository(session).append(
                    actor_user_id=actor_user_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    metadata=metadata,
                )
        except Exception:
            logger.exception("Could not write audit record %s", action)

    async def _set_status(self, user_id: uuid.UUID, status: str) -> User:
        async with session_scope(self.session_factory) as session:
            repository = UserRepository(session)
            user = await repository.get(user_id)
            if user is None:
                raise LookupError("user not found")
            if status == "active":
                user = await repository.enable(user)
                action = "user.enabled"
            elif status == "disabled":
                if user.status == "deleted":
                    raise ValueError("deleted users cannot be disabled differently")
                user = await repository.disable(user)
                action = "user.disabled"
            elif status == "deleted":
                user = await repository.soft_delete(user)
                action = "user.deleted"
            else:
                raise ValueError("unsupported user status")
            await AuditLogRepository(session).append(
                actor_user_id=user.id,
                action=action,
                resource_type="user",
                resource_id=str(user.id),
                metadata={"status": user.status},
            )
            return user

    async def _insert_unique_key(
        self,
        session: Any,
        *,
        user_id: uuid.UUID,
        label: str | None,
        expires_at: datetime | None,
    ) -> IssuedApiKey:
        pepper = self.settings.API_KEY_PEPPER
        if pepper is None:
            raise RuntimeError("API_KEY_PEPPER is required")
        repository = ApiKeyRepository(session)
        last_error: Exception | None = None
        for _ in range(5):
            generated = generate_api_key(
                pepper=pepper,
                lookup_prefix_length=self.settings.API_KEY_LOOKUP_PREFIX_LENGTH,
                secret_bytes=self.settings.API_KEY_SECRET_BYTES,
            )
            try:
                async with session.begin_nested():
                    row = await repository.create(
                        user_id=user_id,
                        key_prefix=generated.key_prefix,
                        secret_hash=generated.secret_hash,
                        label=label,
                        expires_at=expires_at,
                    )
            except IntegrityError as exc:
                last_error = exc
                continue
            return IssuedApiKey(
                id=row.id,
                user_id=row.user_id,
                key_prefix=row.key_prefix,
                label=row.label,
                expires_at=row.expires_at,
                plaintext=generated.plaintext,
            )
        raise RuntimeError("could not allocate a unique API key prefix") from last_error

    async def _audit_auth_failure(self, reason: str) -> None:
        try:
            async with session_scope(self.session_factory) as session:
                await AuditLogRepository(session).append(
                    action="auth.failed",
                    resource_type="api_key",
                    resource_id="unparsed",
                    metadata={"reason": reason},
                )
        except Exception:
            logger.exception("Could not audit an authentication failure")

    def _require_pepper(self) -> None:
        pepper = self.settings.API_KEY_PEPPER
        if pepper is None or len(pepper) < 16:
            raise RuntimeError("API_KEY_PEPPER is required to issue API keys")


def _metadata(row: ApiKey) -> ApiKeyMetadata:
    return ApiKeyMetadata(
        id=row.id,
        user_id=row.user_id,
        key_prefix=row.key_prefix,
        label=row.label,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )
