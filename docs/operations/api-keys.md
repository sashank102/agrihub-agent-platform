# API keys

There is no password or OAuth login. Operators create users and keys with
the admin CLI. The plaintext key is printed once on stdout and is not stored.

Local development defaults to `AUTH_MODE=api_key`. Set `API_KEY_PEPPER` to at
least 16 characters in `.env`, start PostgreSQL, then:

```bash
./.tools/bin/uv run alembic upgrade head
./.tools/bin/uv run python -m agent_platform create-user --display-name "Local"
./.tools/bin/uv run python -m agent_platform issue-key --user-id "<user-id>" --label local
```

Paste the printed key into the UI. `./start-dev.sh` refuses to start in
`api_key` mode without a pepper.

`AUTH_MODE=disabled` is an explicit local bridge. `/info` advertises
`auth_mode: disabled`, and the UI shows a development-mode gate instead of
accepting an arbitrary string as a key. `ENVIRONMENT=production` rejects
disabled auth.

## Rotation and revocation

```bash
python -m agent_platform rotate-key --key-id "<key-id>"
python -m agent_platform revoke-key --key-id "<key-id>"
python -m agent_platform list-keys --user-id "<user-id>"
```

Rotation revokes the old row and prints a new plaintext key once. Revoked,
expired, and deleted-user keys return 401. The UI clears the thread id,
messages, history, artifacts, and interrupts before another key is accepted.

## Lost key

The plaintext cannot be recovered. Rotate the key, or issue a new one, and
discard the old row with `revoke-key` if it might still be usable.

## Pepper and prefix

`API_KEY_PEPPER` is an HMAC key. Changing it invalidates every stored hash.
There is no pepper-rotation migration: issue new keys after the change and
revoke the old rows once users have switched.

`API_KEY_PREFIX` must match the prefix the keys were issued with. Changing
the prefix does not rewrite hashes. Old keys stop authenticating until they
are reissued. Keep the prefix lowercase letters, 2 to 32 characters.

## Users and audit

```bash
python -m agent_platform disable-user --user-id "<user-id>"
python -m agent_platform delete-user --user-id "<user-id>"
```

Disabled and deleted users fail authentication. Audit rows for `auth.failed`,
`api_key.*`, `user.*`, and `access.denied` are kept by the default retention
command. Query them through SQL:

```sql
SELECT created_at, action, resource_type, resource_id
FROM platform.audit_log
WHERE resource_id = '<key-or-user-id>'
ORDER BY created_at;
```

Audit metadata is passed through secret redaction. It does not contain the
plaintext key.
