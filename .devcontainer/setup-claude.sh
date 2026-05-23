#!/bin/bash
set -e

CLAUDE_DATA=/claude-auth

# Named volumes are owned by root inside the container; fix that so we can write to them
sudo chown -R vscode:vscode "$CLAUDE_DATA"

mkdir -p "$CLAUDE_DATA/.claude"

# Persist .claude.json via symlink
if [ ! -f "$CLAUDE_DATA/.claude.json" ]; then
    touch "$CLAUDE_DATA/.claude.json"
fi
ln -sf "$CLAUDE_DATA/.claude.json" ~/.claude.json

# Persist .claude/ directory via symlink
rm -rf ~/.claude
ln -sf "$CLAUDE_DATA/.claude" ~/.claude

exec "$@"
