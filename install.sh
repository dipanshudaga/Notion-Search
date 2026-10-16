#!/bin/bash
# Notion Search installer. Run it with this one command:
#   curl -fsSL https://raw.githubusercontent.com/dipanshudaga/Notion-Search/main/install.sh | bash
# Needs only a Mac with Apple Silicon: uv (installed here if missing) fetches Python 3.12 and the package.
set -euo pipefail

[[ "$(uname -s)-$(uname -m)" == "Darwin-arm64" ]] || { echo "Notion Search needs a Mac with an Apple Silicon chip."; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh >/dev/null

# PyPI first; until the package is published there, fall back to the GitHub source archive (no Git needed).
uv tool install --python 3.12 --force --quiet notion-search 2>/dev/null \
  || uv tool install --python 3.12 --force --quiet https://github.com/dipanshudaga/Notion-Search/archive/refs/heads/main.tar.gz

exec notion-search install
