#!/bin/bash
# Notion Search installer. Run it with this one command:
#   curl -fsSL https://raw.githubusercontent.com/dipanshudaga/Notion-Search/main/install.sh | bash
# Needs nothing but a Mac with Apple Silicon: Python 3.12 is fetched if missing, and Git is not used.
set -euo pipefail

DEST="${NOTION_SEARCH_HOME:-$HOME/.notion-search}"

[[ "$(uname -s)-$(uname -m)" == "Darwin-arm64" ]] || { echo "Notion Search needs a Mac with an Apple Silicon chip."; exit 1; }

mkdir -p "$DEST"
curl -fsSL https://github.com/dipanshudaga/Notion-Search/archive/refs/heads/main.tar.gz | tar -xz -C "$DEST" --strip-components 1

exec "$DEST/notion-search" install
