#!/usr/bin/env bash
# Aura — Mac bootstrap. Idempotent; safe to re-run.
# Everything Aura needs is installed or downloaded HERE — nothing is fetched
# later at runtime except model files this script doesn't already have.
set -euo pipefail

say_step() { printf "\n\033[1;36m▸ %s\033[0m\n" "$1"; }
MODEL_DIR="$HOME/Library/Application Support/Aura/models"

say_step "Checking Homebrew"
if ! command -v brew >/dev/null; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

say_step "Installing system dependencies (portaudio, whisper-cpp, ollama)"
brew install portaudio whisper-cpp ollama

say_step "Starting Ollama and pulling the planner model (~2.5 GB, once)"
brew services start ollama || true
sleep 2
ollama pull qwen3:4b

say_step "Downloading the whisper.cpp speech model (~150 MB, once)"
mkdir -p "$MODEL_DIR"
if [ ! -s "$MODEL_DIR/ggml-base.en.bin" ]; then
  curl -L --fail -o "$MODEL_DIR/ggml-base.en.bin" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
fi

say_step "Creating the Python environment"
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[mac]"

say_step "Pre-fetching on-device wake-word models"
python - <<'PY' || true
try:
    import openwakeword.utils
    openwakeword.utils.download_models()
    print("wake-word models ready")
except Exception as exc:
    print(f"wake-word prefetch skipped ({exc}) — they download on first run")
PY

say_step "Seeding your config with the downloaded model path"
CONFIG_DIR="$HOME/Library/Application Support/Aura"
CONFIG="$CONFIG_DIR/config.toml"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<EOF
[stt]
engine = "whisper_cpp"
whisper_model = "$MODEL_DIR/ggml-base.en.bin"

[wake]
mode = "manual"        # switch to "openwakeword" in Aura's Wake Word panel
phrase = "hey aura"
EOF
  echo "wrote $CONFIG"
else
  echo "keeping your existing $CONFIG"
fi

say_step "Doctor — what will run on this Mac"
python -m aura doctor || true

say_step "Done"
cat <<EOF

  Start Aura:            source .venv/bin/activate && python -m aura serve
  Native menu-bar app:   ./scripts/make_app.sh --install
  Make it yours:         python scripts/train_wakeword.py "your phrase"
  First run permissions: Aura's Setup panel walks you through each one.

  Everything above was downloaded to this machine. Aura never phones home.
EOF
