#!/usr/bin/env bash
# ci-proof uninstaller script
# Removes all files installed by scripts/install.sh:
#   - $CI_PROOF_BIN_DIR/ci-proof (default: ~/.local/bin/ci-proof)
#   - $CI_PROOF_INSTALL_DIR      (default: ~/.ci-proof)
#   - cleans up empty ~/.local/bin and ~/.local directories if created solely for ci-proof

set -euo pipefail

INSTALL_DIR="${CI_PROOF_INSTALL_DIR:-$HOME/.ci-proof}"
USER_BIN="${CI_PROOF_BIN_DIR:-$HOME/.local/bin}"
SYMLINK="$USER_BIN/ci-proof"

REMOVED=0

if [ -L "$SYMLINK" ] || [ -e "$SYMLINK" ]; then
  rm -f "$SYMLINK"
  echo "  ✓ Removed $SYMLINK"
  REMOVED=1
fi

if [ -d "$INSTALL_DIR" ]; then
  rm -rf "$INSTALL_DIR"
  echo "  ✓ Removed $INSTALL_DIR"
  REMOVED=1
fi

# Clean up empty parent bin directory if nothing else is in it
if [ -d "$USER_BIN" ] && [ -z "$(ls -A "$USER_BIN" 2>/dev/null)" ]; then
  rmdir "$USER_BIN" 2>/dev/null || true
  PARENT_LOCAL="$(dirname "$USER_BIN")"
  if [ -d "$PARENT_LOCAL" ] && [ -z "$(ls -A "$PARENT_LOCAL" 2>/dev/null)" ]; then
    rmdir "$PARENT_LOCAL" 2>/dev/null || true
  fi
fi

if [ "$REMOVED" -eq 1 ]; then
  echo "✓ ci-proof uninstalled cleanly (zero files remaining)."
else
  echo "ci-proof was not installed at $INSTALL_DIR or $SYMLINK."
fi
