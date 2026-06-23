#!/bin/bash
# vox bootstrap — one-command install.
#
#   curl -fsSL https://raw.githubusercontent.com/kohlhofer/vox/main/bootstrap.sh | bash
#
# Clones (or updates) vox into a stable home, then runs install.sh — which builds
# the venv and drops a `vox` launcher on your PATH. Re-running updates in place.
#
# Knobs (all optional), passed as env vars on the bash side of the pipe:
#   VOX_HOME      where to keep the checkout   (default: ~/.local/share/vox)
#   VOX_BIN_DIR   where to put the launcher    (default: ~/.local/bin, see install.sh)
#   VOX_BRANCH    branch to track              (default: main)
#
#   curl -fsSL https://raw.githubusercontent.com/kohlhofer/vox/main/bootstrap.sh | VOX_BIN_DIR=/usr/local/bin bash
set -e

REPO="${VOX_REPO:-https://github.com/kohlhofer/vox.git}"
BRANCH="${VOX_BRANCH:-main}"
HOME_DIR="${VOX_HOME:-$HOME/.local/share/vox}"

if ! command -v git >/dev/null 2>&1; then
  echo "error: git is required but not found." >&2
  echo "       Install the Xcode command line tools with: xcode-select --install" >&2
  exit 1
fi

if [ -d "$HOME_DIR/.git" ]; then
  echo "==> Updating vox at $HOME_DIR"
  git -C "$HOME_DIR" fetch --quiet origin "$BRANCH"
  git -C "$HOME_DIR" checkout --quiet "$BRANCH"
  git -C "$HOME_DIR" reset --hard --quiet "origin/$BRANCH"
elif [ -e "$HOME_DIR" ]; then
  echo "error: $HOME_DIR exists but isn't a vox checkout." >&2
  echo "       Remove it, or set VOX_HOME to a different path and retry." >&2
  exit 1
else
  echo "==> Cloning vox into $HOME_DIR"
  mkdir -p "$(dirname "$HOME_DIR")"
  git clone --quiet --branch "$BRANCH" "$REPO" "$HOME_DIR"
fi

echo "==> Running installer"
exec "$HOME_DIR/install.sh"
