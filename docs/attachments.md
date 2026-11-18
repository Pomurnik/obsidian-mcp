# Attachment operations

Attachment tools are part of the main server package and the root Docker image.
They use the same authentication, vault selection, path permissions, locks, and
audit log as the other tools. No extension or alternate entrypoint is required.

## Tools and feature flags

| Tool | Purpose | Required flag |
| --- | --- | --- |
| `inspect_attachment_tool` | Return a revision and size without downloading bytes | None |
| `move_attachment_tool` | Move or rename one attachment and update Markdown references | `ENABLE_MOVE=true` |
| `move_attachments_tool` | Move 1–500 attachments with one preview and commit | `ENABLE_MOVE=true` |
| `delete_attachment_tool` | Delete one attachment, using trash by default | `ENABLE_DELETE=true` |
| `restore_attachment_tool` | Restore an attachment from trash without overwriting an existing file | `ENABLE_DELETE=true` |

The flags control tool discovery. Writes also require `READ_ONLY=false` and
permission under the configured `WRITE_PATHS` and `DENY_WRITE_PATHS` rules.
No particular folder layout is required. Both ends of a move and every note
whose links would change must be writable. A reference in a protected note
blocks the operation, including deletion with `allow_broken_links=true`.

## Preview, then commit

1. Call `move_attachment_tool` with `from_path` and `to_path`, or
   `delete_attachment_tool` with `path`. Both default to `dry_run=true`.
2. Review the affected notes in `data.updated_links_in` or `data.referenced_by`.
3. Repeat the call with `dry_run=false` and `expected_revision` set to the
   top-level `revision` returned by the preview. A changed attachment rejects
   the commit. `inspect_attachment_tool` can also supply a current revision.

A move preserves the file bytes and extension. It updates full and short
wikilinks, inline Markdown links, and Markdown reference definitions while
preserving fragments, aliases, display sizes, and titles. Ambiguous links and
unsupported or literal references are rejected before mutation. References in
Canvas, Base, and other non-Markdown documents require manual handling.

Source and destination must share a filesystem. A cross-filesystem rename
failure occurs before note edits. If a note write fails, the server attempts
to roll back the move and completed note edits using revision checks. A rollback
conflict reports the affected paths for inspection. This is not a transaction
against external editors; concurrent changes can require manual recovery.

Deletion refuses referenced attachments unless `allow_broken_links=true` is
explicitly supplied and every referencing note is writable. Trash deletion
returns `data.trashed_name`; pass that value and a writable `to_path` to
`restore_attachment_tool`. Permanent deletion requires both `trash=false`
and the server setting `ALLOW_PERMANENT_DELETE=true`.

## Moving multiple attachments

Prefer `move_attachments_tool` over a loop of single-file calls. It reads each
candidate document once per planning pass, resolves links against the original
vault, and writes each affected Markdown note once on commit. It does not call
the single-file move tool repeatedly.

Preview an explicit mapping (up to 500 files):

```json
{
  "moves": [
    {"from_path": "Inbox/photo.png", "to_path": "Attachments/photo.png"},
    {"from_path": "Inbox/chart.png", "to_path": "Attachments/chart.png"}
  ],
  "dry_run": true
}
```

The response contains `data.plan_revision`, `data.moves`, `data.count`,
`data.updated_links_in`, and `data.links`. Review the plan, then call the same
tool with the same `moves`, `dry_run=false`, and `expected_plan_revision` set to
the returned `data.plan_revision`. No separate inspect or per-file preview is
needed. An optional `vault` selects the target vault as with other MCP tools.

The plan token binds the mapping, source revisions, and affected note contents.
Changed sources, changed references, new collisions, or newly denied paths
reject the commit before any moves. Reordering the same mapping is allowed.
Duplicate sources/destinations, overwrites, rename chains, and swaps are rejected.
The existing protected-path and unsupported-reference checks apply to the entire
batch. Split larger jobs into batches and preview each immediately before committing.

The server acquires locks in a consistent order and revalidates the plan, moves
the attachments, then writes the combined note updates. An ordinary failure
triggers rollback of completed moves and note writes. Rollback uses revisions
and does not overwrite conflicting external edits; conflicts report the paths
requiring recovery. Empty destination directories may remain after a failed move.
This is a synchronous tool, not a background job or a crash-safe transaction.
If a client times out, inspect the paths before deciding whether to retry.
Each successful attachment move is recorded in the audit log.

## Container builds and updates

Build the server, including attachment operations, from the repository root:

```sh
docker build -t obsidian-mcp .
```

The build installs the locked runtime dependencies and runs attachment tests
as an unprivileged user before producing the runtime image. The normal CI
workflow runs the full test suite and lint checks.

The container workflow publishes Linux amd64 images to
`ghcr.io/pomurnik/obsidian-mcp` after CI succeeds on `main`:

- `latest`: the latest integrated server build.
- `sha-<full-commit-sha>`: a specific source revision.
- `attachments` and `attachments-<full-commit-sha>`: compatibility aliases
  for existing deployments; they contain the same integrated server.

Pull and recreate the container to update it. Existing environment variables,
credentials, vault mounts, and feature flags remain deployment configuration.
MCP clients may need to reconnect to refresh their discovered tool list.
