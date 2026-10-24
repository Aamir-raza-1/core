#!/usr/bin/env bash
# ci-proof local installer script
# Usage: bash scripts/install.sh
# Environment overrides:
#   CI_PROOF_INSTALL_DIR (default: $HOME/.ci-proof)
#   CI_PROOF_BIN_DIR     (default: $HOME/.local/bin)

set -euo pipefail

BOLD="$(tput bold 2>/dev/null || true)"
GREEN="$(tput setaf 2 2>/dev/null || true)"
YELLOW="$(tput setaf 3 2>/dev/null || true)"
RED="$(tput setaf 1 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"

echo "${BOLD}Installing ci-proof (Evidence-based CI Audit & Test Selection Engine)...${RESET}"

# 1. OS and Architecture check
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$OS" in
  linux*)  PLATFORM="linux" ;;
  darwin*) PLATFORM="darwin" ;;
  *)       echo "${RED}Error: Unsupported OS: $OS (Linux, macOS, or Windows WSL2 required).${RESET}" >&2; exit 1 ;;
esac

# 2. Check for Python 3.9+
if ! command -v python3 >/dev/null 2>&1; then
  echo "${RED}Error: python3 is required but not installed.${RESET}" >&2
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="$(echo "$PY_VER" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VER" | cut -d. -f2)"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
  echo "${RED}Error: Python 3.9+ is required (found Python $PY_VER).${RESET}" >&2
  exit 1
fi

echo "  ✓ Detected platform: $PLATFORM ($ARCH)"
echo "  ✓ Python version: $PY_VER"

# 3. Verify local repository checkout (remote curl mode is planned for public release D1)
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd || echo "")"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [ ! -d "$REPO_ROOT/engine" ]; then
  echo "${RED}Error: engine/ directory not found at $REPO_ROOT/engine.${RESET}" >&2
  echo "Remote release archive installation is not yet available (repository is private until D1)." >&2
  echo "Run 'bash scripts/install.sh' from a local checkout of the repository." >&2
  exit 1
fi

# 4. Determine installation target directories
INSTALL_DIR="${CI_PROOF_INSTALL_DIR:-$HOME/.ci-proof}"
BIN_DIR="$INSTALL_DIR/bin"
LIB_DIR="$INSTALL_DIR/lib"
USER_BIN="${CI_PROOF_BIN_DIR:-$HOME/.local/bin}"

rm -rf "$LIB_DIR/engine" "$LIB_DIR/profiles"
mkdir -p "$BIN_DIR" "$LIB_DIR" "$USER_BIN"

# 5. Copy engine and profiles (without bytecode caches)
cp -R "$REPO_ROOT/engine" "$LIB_DIR/"
if [ -d "$REPO_ROOT/profiles" ]; then
  cp -R "$REPO_ROOT/profiles" "$LIB_DIR/"
fi
find "$LIB_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$LIB_DIR" \( -name "*.pyc" -o -name "*.pyo" \) -type f -delete 2>/dev/null || true

# 6. Create symlink-safe launcher wrapper script
WRAPPER="$BIN_DIR/ci-proof"
cat <<'EOF' > "$WRAPPER"
#!/usr/bin/env bash
set -euo pipefail
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$SCRIPT_DIR/lib${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m engine.cli "$@"
EOF
chmod +x "$WRAPPER"

# 7. Symlink into ~/.local/bin (or CI_PROOF_BIN_DIR)
ln -sf "$WRAPPER" "$USER_BIN/ci-proof"

echo ""
echo "${GREEN}${BOLD}✓ ci-proof installed successfully${RESET}"
echo "Installed files:"
echo "  - $LIB_DIR/engine/      (engine Python package)"
echo "  - $LIB_DIR/profiles/    (built-in repository profiles)"
echo "  - $WRAPPER              (launcher script)"
echo "  - $USER_BIN/ci-proof    (symlink -> $WRAPPER)"
echo ""

# 8. Check if in PATH
if ! echo ":$PATH:" | grep -q ":$USER_BIN:"; then
  echo "${YELLOW}Note: Add $USER_BIN to your PATH to run ci-proof directly:${RESET}"
  echo "  export PATH=\"$USER_BIN:\$PATH\""
  echo ""
fi

echo "Verify & Uninstall:"
echo "  $USER_BIN/ci-proof --version"
echo "  $USER_BIN/ci-proof --help"
echo "  bash scripts/uninstall.sh"
