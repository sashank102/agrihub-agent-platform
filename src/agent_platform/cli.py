"""Admin commands for users and API keys.

The plaintext key is printed once on stdout. It is never accepted as an
argument and never written to the log.
"""

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_platform.core.settings import get_settings
from agent_platform.db.session import create_platform_engine, create_session_factory
from agent_platform.services.accounts import AccountService, IssuedApiKey


def main(argv: list[str] | None = None) -> int:
    """Run one admin command and return a process exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        payload = asyncio.run(_dispatch(args))
    except (LookupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(payload, default=_json_default))
    return 0


async def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    if settings.DATABASE_URI is None:
        raise RuntimeError("DATABASE_URI is required")
    engine = create_platform_engine(settings.DATABASE_URI)
    try:
        service = AccountService(create_session_factory(engine), settings)
        return await _run(service, args)
    finally:
        await engine.dispose()


async def _run(service: AccountService, args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "create-user":
        user = await service.create_user(
            display_name=args.display_name,
            email=args.email,
        )
        return {
            "user_id": str(user.id),
            "display_name": user.display_name,
            "email": user.email,
            "status": user.status,
        }
    if command == "disable-user":
        user = await service.disable_user(uuid.UUID(args.user_id))
        return {"user_id": str(user.id), "status": user.status}
    if command == "enable-user":
        user = await service.enable_user(uuid.UUID(args.user_id))
        return {"user_id": str(user.id), "status": user.status}
    if command == "delete-user":
        user = await service.delete_user(uuid.UUID(args.user_id))
        return {"user_id": str(user.id), "status": user.status}
    if command == "issue-key":
        issued = await service.issue_api_key(
            user_id=uuid.UUID(args.user_id),
            label=args.label,
            expires_at=_expiry(args.expires_in_days),
        )
        return _issued(issued)
    if command == "list-keys":
        rows = await service.list_api_keys(uuid.UUID(args.user_id))
        return {
            "keys": [
                {
                    "id": str(row.id),
                    "user_id": str(row.user_id),
                    "key_prefix": row.key_prefix,
                    "label": row.label,
                    "created_at": row.created_at,
                    "expires_at": row.expires_at,
                    "last_used_at": row.last_used_at,
                    "revoked_at": row.revoked_at,
                }
                for row in rows
            ]
        }
    if command == "revoke-key":
        row = await service.revoke_api_key(uuid.UUID(args.key_id))
        return {
            "id": str(row.id),
            "key_prefix": row.key_prefix,
            "revoked_at": row.revoked_at,
        }
    if command == "rotate-key":
        issued = await service.rotate_api_key(uuid.UUID(args.key_id))
        return _issued(issued)
    if command == "update-global-agent":
        agent_id = await service.update_global_agent(
            uuid.UUID(args.agent_id),
            name=args.name,
            active=None if args.active is None else args.active == "true",
        )
        return {"agent_id": str(agent_id)}
    raise RuntimeError("unknown command")


def _issued(issued: IssuedApiKey) -> dict[str, Any]:
    return {
        "id": str(issued.id),
        "user_id": str(issued.user_id),
        "key_prefix": issued.key_prefix,
        "label": issued.label,
        "expires_at": issued.expires_at,
        "api_key": issued.plaintext,
    }


def _expiry(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(UTC) + timedelta(days=days)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agrihub-admin")
    commands = parser.add_subparsers(dest="command", required=True)

    create_user = commands.add_parser("create-user")
    create_user.add_argument("--display-name", required=True)
    create_user.add_argument("--email")

    for name in ("disable-user", "enable-user", "delete-user"):
        command = commands.add_parser(name)
        command.add_argument("--user-id", required=True)

    issue = commands.add_parser("issue-key")
    issue.add_argument("--user-id", required=True)
    issue.add_argument("--label")
    issue.add_argument("--expires-in-days", type=int)

    listed = commands.add_parser("list-keys")
    listed.add_argument("--user-id", required=True)

    revoke = commands.add_parser("revoke-key")
    revoke.add_argument("--key-id", required=True)

    rotate = commands.add_parser("rotate-key")
    rotate.add_argument("--key-id", required=True)

    update = commands.add_parser("update-global-agent")
    update.add_argument("--agent-id", required=True)
    update.add_argument("--name")
    update.add_argument("--active", choices=("true", "false"))
    return parser


if __name__ == "__main__":
    sys.exit(main())
