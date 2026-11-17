# Attachment extension in this fork

This fork tracks [ykoellmann/obsidian-mcp](https://github.com/ykoellmann/obsidian-mcp).
The extension lives in `extensions/attachments/` and runs inside the original
authenticated MCP server. It adds `inspect_attachment_tool`,
`move_attachment_tool`, `delete_attachment_tool`, and `restore_attachment_tool`.

The base image remains pinned to upstream **2.1.2** by digest. Upstream version
upgrades require an explicit compatibility review and new passing tests; syncing
upstream Git history alone does not change the pinned runtime.

## First checkout on the Docker host

```sh
cd /opt/knowledge-base
git clone https://github.com/Pomurnik/obsidian-mcp.git mcp-source
cd mcp-source
git remote add upstream https://github.com/ykoellmann/obsidian-mcp.git
docker build --progress=plain -t local/kb-mcp-attachments:2.1.2-1 extensions/attachments
```

The image build runs 31 tests as UID 1000, using temporary vaults only.
The tests cover attachment bytes, Markdown links, permissions, concurrency
preconditions, trash/restore, rollback after a failed write, and MCP discovery.
The complete test suite passed on the target Linux Docker host on 2026-11-17.
This fork includes the test HOME fix and subsequent lint-only cleanup.

## Dockhand

In the existing MCP service, set:

```yaml
image: local/kb-mcp-attachments:2.1.2-1
pull_policy: never
```

Retain your existing OAuth/API-key environment and data mounts. The extension
inherits the existing authorization middleware; it needs no additional public
port and no new credentials. Recreate the service after a successful build.

Recommended policy for the existing Human/AI/Hybrid layout:

```yaml
READ_ONLY: "false"
WRITE_PATHS: "AI/,Hybrid/"
DENY_WRITE_PATHS: ".obsidian/,.git/,.trash/,private/,Human/,_AI_INSTRUCTIONS.md"
ENABLE_MOVE: "true"
ENABLE_DELETE: "true"
```

Use one shared read/write vault mount with a separate read-only `Human` overlay.
Separate mounts for AI and Hybrid prevent rename operations between them.
Permanent deletion additionally requires `ALLOW_PERMANENT_DELETE=true`;
the attachment delete tool defaults to trash regardless of that flag.

## Pull changes from this fork

```sh
cd /opt/knowledge-base/mcp-source
bash scripts/update-attachments.sh
```

The script requires a clean checkout on `main`, fetches only fast-forward
changes from this fork, and rebuilds the local image with its tests. It never
restarts a running container or modifies the vault. After success, recreate the
MCP service in Dockhand and refresh the MCP clients' tool lists.

Equivalent manual commands:

```sh
git pull --ff-only origin main
docker build --progress=plain -t local/kb-mcp-attachments:2.1.2-1 extensions/attachments
```

## Incorporate future upstream changes

Do this in a development checkout, separately from production deployment:

```sh
git fetch upstream
git switch -c maintenance/upstream-sync
git merge upstream/main
```

Resolve conflicts, review changes and run the upstream and extension tests
before merging the branch into this fork's main branch. Review any new upstream
release before deliberately changing the extension's base-image digest/version
guard. The inherited PyPI/GHCR release jobs are gated to the original upstream
repository so this fork cannot accidentally run its release configuration.

## Limitations and rollback

See [extension documentation](../extensions/attachments/README.md) for supported
link syntax and concurrency limitations. References from read-only notes block
moves. Ambiguous links and unsupported reference formats fail before mutation.
There is no shell access tool and no vault-wide permission expansion.

To revert the extension, restore the previous upstream image digest in Dockhand,
remove `pull_policy: never`, and recreate the service. This changes the running
code only; it does not undo file operations already performed.
