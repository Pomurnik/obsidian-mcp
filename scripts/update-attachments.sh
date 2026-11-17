#!/usr/bin/env bash
# Update the source and build/test the image; deployment remains a separate step.
set -euo pipefail

kb_repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$kb_repo_dir"
if [ "$(git branch --show-current)" != main ]; then
  echo 'Switch to the main branch before updating.' >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo 'The checkout has local changes. Commit or move them before updating.' >&2
  exit 1
fi
git pull --ff-only origin main
docker build --progress=plain -t local/kb-mcp-attachments:2.1.2-1 extensions/attachments
printf '\nBuild and tests passed. Recreate the MCP service in Dockhand to apply this image.\n'
