# Agent session provenance (developer contract)

For host tools such as [ai-bridge-cli](https://github.com/numanzahid/ai-bridge-cli)
that need to know which agent chats were **created through MCP**. The server
(`python -m ai_bridge`) neither ships nor needs that CLI.

## Record location

Read as the service user, under `AI_BRIDGE_STATE_DIR` (default `~/.local/state/ai-bridge`):

```text
<state_dir>/management/provenance/<sha256(agent + ':' + session_id)>.json
```

- Digest: lowercase hex SHA256 of the UTF-8 string, no newline.
- `agent`: `claude`, `codex` or `cursor`. `session_id`: the exact full UUID.
- Only 64-hex `.json` names are records. Ignore hidden `.job-*.json` files.

## Schema

```json
{
  "schema": 1,
  "agent": "codex",
  "session_id": "11111111-2222-4333-8444-555555555555",
  "origin": "mcp",
  "first_seen": 1792270000.25,
  "job_ids": ["1018-120000-0123456789ab"]
}
```

| Field | Rule |
|---|---|
| `first_seen` | UTC epoch seconds of first observation; never changes (not the chat's creation time) |
| `job_ids` | Up to 64 distinct bridge job IDs, oldest dropped first |
| size | At most 16384 bytes |

Readers should validate schema, agent, UUID and origin, and ignore unknown fields.
A missing or invalid record means **unknown**, never "native only".

## What counts as MCP origin

- A new `agent_send` turn (no resume ID) whose adapter reported this UUID.
- A native chat later resumed through MCP stays **unclassified**.
- Resuming an MCP-origin chat over MCP appends the job ID; `first_seen` is unchanged.
- Native run modes, SDK markers, cwd or prompts are never evidence.

`Sessions.enrich` returns `bridge_origin: "mcp"` for a valid record, else `null`
(plus `bridge_provenance_error` if the record is unreadable). Native fields are untouched.

## Guarantees

- Written while the turn runs, and backfilled from job snapshots before retention
  deletes them. A failed write keeps the job and reports `provenance_error`.
- Records never expire and survive restarts; full uninstall removes them.
- Directories are 0700 and records 0600, owner-only. Symlinks, hard links and
  permissive modes are refused.
- Writes are atomic (temp file, fsync, rename), so readers need no lock. Readers
  should apply the same no-follow and permission checks.
- Same-user code can forge or delete records; that is inside the trust boundary.
