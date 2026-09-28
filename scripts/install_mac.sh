#!/usr/bin/env bash
# Aura — one-command Mac install. Idempotent; safe to re-run to update.
#
#   ./scripts/install.sh
#
# Everything Aura needs is installed or downloaded HERE — nothing is fetched
# later at runtime except a model file this script doesn't already have:
#   1. system deps          portaudio, whisper.cpp, Ollama (Homebrew)
#   2. the planner model    qwen3:4b via Ollama (~2.5 GB, once)
#   3. the speech model     whisper.cpp ggml-base.en (~150 MB, once)
#   4. the engine           repo copy + venv in Application Support/Aura/engine
#   5. wake-word models     openWakeWord pretrained set, cached
#   6. Aura.app             built and installed into /Applications, then opened
#
# After this, the terminal never appears again: everything (permissions,
# wake phrase, settings, installs) lives inside Aura.
set -euo pipefail

say_step() { printf "\n\033[1;36m▸ %s\033[0m\n" "$1"; }
note()     { printf "  %s\n" "$1"; }

if [ "$(uname)" != "Darwin" ]; then
  echo "This installer is for macOS. On other systems run:  python3 -m aura serve"
  echo "(demo profile — full UI, simulated actions)."
  exit 1
fi

SUPPORT="$HOME/Library/Application Support/Aura"
MODEL_DIR="$SUPPORT/models"
ENGINE_DIR="$SUPPORT/engine"
CONFIG="$SUPPORT/config.toml"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

say_step "1/6  System dependencies (Homebrew)"
if ! command -v brew >/dev/null 2>&1; then
  note "Installing Homebrew…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
fi
brew install portaudio whisper-cpp ollama 2>/dev/null || brew install portaudio whisper-cpp ollama

say_step "2/6  Planner model (qwen3:4b via Ollama, ~2.5 GB, once)"
brew services start ollama 2>/dev/null || true
for _ in $(seq 1 15); do
  if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
  sleep 1
done
if curl -sf http://127.0.0.1:11434/api/tags | grep -q 'qwen3:4b'; then
  note "qwen3:4b already pulled."
else
  ollama pull qwen3:4b
fi

say_step "3/6  Speech model (whisper.cpp ggml-base.en, ~150 MB, once)"
mkdir -p "$MODEL_DIR"
if [ -s "$MODEL_DIR/ggml-base.en.bin" ]; then
  note "Already downloaded."
else
  curl -L --fail --retry 3 -o "$MODEL_DIR/ggml-base.en.bin" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
fi

say_step "4/6  The engine (Application Support/Aura/engine + venv)"
mkdir -p "$ENGINE_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'build' --exclude '.build' \
  --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' \
  --exclude 'node_modules' \
  "$REPO/" "$ENGINE_DIR/"
cd "$ENGINE_DIR"
if [ ! -x .venv/bin/python ]; then
  note "Creating the Python environment (first time)…"
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet ".[mac]"

say_step "5/6  Wake-word models (cached on this Mac)"
.venv/bin/python - <<'PY' || note "skipped — they can be downloaded inside Aura's Setup panel"
import openwakeword.utils
openwakeword.utils.download_models()
print("  wake-word models ready")
PY

say_step "6/6  Aura.app → /Applications"
if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<EOF
[stt]
engine = "whisper_cpp"
whisper_model = "$MODEL_DIR/ggml-base.en.bin"

[wake]
mode = "manual"        # switch to always-listening in Aura's Wake Phrase panel
phrase = "hey aura"
EOF
  note "Seeded $CONFIG"
else
  note "Keeping your existing config."
fi
"$REPO/scripts/make_app.sh" --install

say_step "Done — opening Aura"
open /Applications/Aura.app
cat <<EOF

  ▸ A welcome window opens: grant Microphone and Accessibility when macOS
    asks — each is a normal system dialog, shown once.
  ▸ Train your wake phrase in a few seconds:  Setup / Wake Phrase panel.
  ▸ ⌥Space anywhere wakes Aura. Right-click the menu-bar ◉ for the menu.

  Everything above is on this machine. Aura never phones home.
  Re-run this script any time to update Aura.
EOF
