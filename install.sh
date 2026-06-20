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

# Launcher: a 2-line shim that execs the venv python against vox.py.
BIN_DIR="${VOX_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/vox"
cat > "$LAUNCHER" <<EOF
#!/bin/bash
exec "$HERE/.venv/bin/python" "$HERE/vox.py" "\$@"
EOF
chmod +x "$LAUNCHER"

echo "==> Installed launcher: $LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "    NOTE: $BIN_DIR is not on your PATH. Add it, e.g.:"
     echo "          echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc" ;;
esac

echo ""
echo "Done. Try it:"
echo "    vox \"Hello — vox is installed.\""
echo "    vox --list-voices"
