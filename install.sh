#!/usr/bin/env bash
# Brainchild bootstrap installer — mac + linux.
# Usage:  curl -fsSL https://brainchild.sh/install | bash
#         (or)  bash install.sh

set -eu

REPO_URL="${BRAINCHILD_REPO:-https://github.com/Unioedtech/brainchild.git}"
INSTALL_DIR="$HOME/.brainchild"
REPO_DIR="$INSTALL_DIR/repo"

say()  { printf "  %s\n" "$1" >&2; }
err()  { printf "  ✗ %s\n" "$1" >&2; }
ok()   { printf "  ✓ %s\n" "$1" >&2; }
bail() { err "$1"; exit 1; }

printf "\n────────────────────────────────────────────────────────────\n" >&2
printf "  Brainchild bootstrap\n" >&2
printf "────────────────────────────────────────────────────────────\n\n" >&2

# ---- OS detect -------------------------------------------------------------
case "$(uname -s)" in
  Darwin*) OS="macos" ;;
  Linux*)  OS="linux" ;;
  *)       bail "unsupported OS: $(uname -s). On Windows use install.ps1." ;;
esac
ok "OS: $OS"

# ---- Python 3.8+ -----------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  if [ "$OS" = "macos" ]; then
    bail "Python 3.8+ not found. Install with: brew install python"
  else
    bail "Python 3.8+ not found. Install with: sudo apt install python3 python3-pip  (or your distro equivalent)"
  fi
fi
PY_VER=$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "Python: $PY_VER"
$PY -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' \
  || bail "Python 3.8+ required (found $PY_VER)"

# ---- node / npm (for claude install) ---------------------------------------
if ! command -v npm >/dev/null 2>&1; then
  say "node/npm not found — needed only if you don't yet have Claude Code."
  say "Install Node from https://nodejs.org/ or via your package manager."
fi

# ---- claude binary ---------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
  say "Claude Code not found."
  printf "  Install now via npm? [Y/n] " >&2
  read -r ans || ans=y
  case "${ans:-y}" in
    n|N|no) bail "Install Claude Code first: npm i -g @anthropic-ai/claude-code" ;;
    *)      npm i -g @anthropic-ai/claude-code || bail "npm install failed" ;;
  esac
fi
ok "claude: $(command -v claude)"

# ---- git -------------------------------------------------------------------
command -v git >/dev/null 2>&1 || bail "git not found. Install git first."

# ---- clone / update repo ---------------------------------------------------
mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR" 2>/dev/null || true
if [ -d "$REPO_DIR/.git" ]; then
  say "updating existing checkout…"
  git -C "$REPO_DIR" fetch --quiet origin
  git -C "$REPO_DIR" reset --hard --quiet origin/HEAD
else
  say "cloning $REPO_URL → $REPO_DIR"
  git clone --quiet "$REPO_URL" "$REPO_DIR" \
    || bail "git clone failed (set BRAINCHILD_REPO to override URL, or place repo manually)"
fi

# ---- pip install (local, no sudo) -----------------------------------------
say "installing Python deps (user-local)…"
$PY -m pip install --quiet --user --upgrade pip
$PY -m pip install --quiet --user --force-reinstall --no-cache-dir --no-deps "$REPO_DIR" 2>&1 | tail -20 || \
  bail "pip install (brainchild) failed"
# Deps separately so they're only installed if missing (avoid reinstalling big wheels each run)
$PY -m pip install --quiet --user \
  "keyring>=24" "imageio-ffmpeg>=0.4.9" "pypdf>=4.0" "python-docx>=1.1" 2>&1 | tail -20 || \
  bail "pip install (deps) failed"
ok "Python package installed"

# ---- hand off to wizard ----------------------------------------------------
ok "bootstrap complete — launching wizard"
echo
exec $PY -m brainchild install
