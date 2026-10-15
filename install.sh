#!/bin/bash
# Notion Search installer. Run it with this one command:
#   curl -fsSL https://raw.githubusercontent.com/dipanshudaga/Notion-Search/main/install.sh | bash
set -euo pipefail

DEST="${NOTION_SEARCH_HOME:-$HOME/.notion-search}"
REPO="https://github.com/dipanshudaga/Notion-Search.git"

[[ "$(uname -s)-$(uname -m)" == "Darwin-arm64" ]] || { echo "Notion Search needs a Mac with an Apple Silicon chip."; exit 1; }

# Git comes with Apple's command line tools (a small download, not the full Xcode app).
if ! xcode-select -p >/dev/null 2>&1 && [[ ! -x /opt/homebrew/bin/git ]]; then
  echo "Git is missing. A window opens now to install Apple's command line tools."
  echo "When it finishes, run this install command again."
  xcode-select --install 2>/dev/null || true
  exit 1
fi

if ! command -v python3.12 >/dev/null; then
  command -v brew >/dev/null || { echo "Python 3.12 is missing. Install Homebrew from https://brew.sh, then run this command again."; exit 1; }
  echo "Installing Python 3.12 with Homebrew..."
  brew install python@3.12
fi

if [[ -d "$DEST/.git" ]]; then
  git -C "$DEST" pull --ff-only -q
else
  git clone --depth 1 -q "$REPO" "$DEST"
fi

exec "$DEST/notion-search" install
