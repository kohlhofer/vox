#!/bin/bash
# Install `vox`: a Python venv with the Kokoro voice deps, plus a launcher on
# your PATH so anything (you, a script, an AI agent) can run `vox "..."`.
#
# Re-running is safe — it updates in place.
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"

# Pick a Python 3.13 interpreter (mlx-audio is proven there). Fall back to
# whatever python3 is around; the `say` path still works even if Kokoro doesn't.
PY="$(command -v python3.13 || command -v python3)"
echo "==> Using interpreter: $PY ($($PY --version))"

echo "==> Creating venv at .venv"
"$PY" -m venv .venv

echo "==> Installing dependencies (this downloads a fair bit the first time)"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

# espeak-ng lets Kokoro phonemize out-of-dictionary words (odd names,
# abbreviations). Without it those words get skipped or fall back to the
# lower-quality macOS `say` voice. Install it via Homebrew when available;
# it's a nice-to-have, so never fail the install over it.
if command -v espeak-ng >/dev/null 2>&1; then
  echo "==> espeak-ng already installed"
elif command -v brew >/dev/null 2>&1; then
  echo "==> Installing espeak-ng (for out-of-dictionary words)"
  brew install espeak-ng || echo "    NOTE: espeak-ng install failed; vox still works, odd words may use the system voice."
else
  echo "    NOTE: espeak-ng not found and Homebrew unavailable. vox works without it,"
  echo "          but unusual words may fall back to the macOS 'say' voice."
fi

# Launcher: a 2-line shim that execs the venv python against vox.py.
BIN_DIR="${VOX_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/vox"

# Never hijack a launcher belonging to a different checkout. Anyone keeping a
# working copy alongside an installed one shares this single shim between them,
# so overwriting it silently repoints `vox` at whichever tree ran install.sh
# last. The symptom is edits that appear to do nothing, with no error explaining
# why. Re-running from the same checkout still updates in place — the common case.
if [ -e "$LAUNCHER" ] && [ -z "$VOX_FORCE_LAUNCHER" ] && ! grep -qF "$HERE/vox.py" "$LAUNCHER" 2>/dev/null; then
  echo "==> Leaving $LAUNCHER alone — it runs another vox, not the one in $HERE"
  echo "    Repoint it here:      VOX_FORCE_LAUNCHER=1 $0"
  echo "    Or install alongside: VOX_BIN_DIR=~/bin $0"
else
  cat > "$LAUNCHER" <<EOF
#!/bin/bash
exec "$HERE/.venv/bin/python" "$HERE/vox.py" "\$@"
EOF
  chmod +x "$LAUNCHER"
  echo "==> Installed launcher: $LAUNCHER"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "    NOTE: $BIN_DIR is not on your PATH. Add it, e.g.:"
     echo "          echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc" ;;
esac

echo ""
echo "Done. Try it:"
echo "    vox \"Hello — vox is installed.\""
echo "    vox --list-voices"
echo "    vox --doctor            # check the install if anything looks off"
