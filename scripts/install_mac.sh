#!/usr/bin/env bash
# Aura — one-command Mac install. Idempotent; safe to re-run to update.
#
#   ./scripts/install.sh
#
# Everything Aura needs is installed or downloaded HERE — nothing is fetched
# later at runtime except a model file this script doesn't already have:
#   1. system deps          portaudio, whisper.cpp (Homebrew)
#   2. the speech model     whisper.cpp ggml-base.en (~150 MB, once)
#   3. the engine           repo copy + venv in Application Support/Aura/engine
#                           (pip-installs `laya`; weights download on first use)
#   4. the decision model   Laya checkpoints, warmed and proven end-to-end
#   5. wake-word models     openWakeWord pretrained set, cached
#   6. Aura.app             built and installed into /Applications, then opened
#
# No model server, no Ollama, no LLM: Aura's brain is Laya, a non-autoregressive
# decision model that answers typed questions in one forward pass. The navigation
# checkpoint (assets/models/aura-nav-laya) is trained on this Mac by step 4
# when it is missing — no hub download required; re-running is safe (a
# half-finished training run is detected by its missing training_report.json
# and redone, never mistaken for a real checkpoint).
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
brew install portaudio whisper-cpp 2>/dev/null || brew install portaudio whisper-cpp

say_step "2/6  Speech model (whisper.cpp ggml-base.en, ~150 MB, once)"
mkdir -p "$MODEL_DIR"
if [ -s "$MODEL_DIR/ggml-base.en.bin" ]; then
  note "Already downloaded."
else
  curl -L --fail --retry 3 -o "$MODEL_DIR/ggml-base.en.bin" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
fi

say_step "3/6  The engine (Application Support/Aura/engine + venv)"
mkdir -p "$ENGINE_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'build' --exclude '.build' \
  --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' \
  --exclude 'node_modules' \
  "$REPO/" "$ENGINE_DIR/"
cd "$ENGINE_DIR"
if [ ! -x .venv/bin/python ]; then
  # The Xcode command-line tools ship an old python3 (3.9); Aura needs ≥3.11.
  # Prefer a real 3.11/3.12, brew or otherwise, before falling back.
  PYBIN="$(command -v python3.12 || command -v python3.11 || true)"
  if [ -z "$PYBIN" ]; then
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PYBIN="$(command -v python3)"
    else
      note "Need Python 3.11+ — installing python@3.12 via Homebrew (one time)…"
      brew install python@3.12
      PYBIN="$(command -v python3.12)"
    fi
  fi
  note "Creating the Python environment with $PYBIN (first time)…"
  "$PYBIN" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet ".[mac]"

say_step "4/6  Decision model (Laya) — the bundled checkpoint, proven end-to-end"
CKPT="$ENGINE_DIR/assets/models/aura-nav-laya"
# A checkpoint only counts when training RAN TO COMPLETION: the trainer writes
# training_report.json last and now stages + renames atomically. An interrupted
# earlier run (or one that hit the laya < 0.3.22 ImportError) can leave
# scaffolding plus untrained weights behind — a directory that LOOKS done, so
# this script would skip retraining and laya-check would load random weights.
if [ -f "$CKPT/model.safetensors" ] && [ ! -f "$CKPT/training_report.json" ]; then
  note "Found an incomplete checkpoint (an earlier training run did not finish) — retraining."
  rm -rf "$CKPT"
fi
if [ ! -f "$CKPT/model.safetensors" ]; then
  note "Navigation checkpoint missing — training it now (one-time: a few minutes on"
  note "Apple Silicon, longer on an Intel Mac; the progress lines below are live)…"
  # The trainer exits non-zero when its internal quality bar is not met; the
  # verdict the user lives with is the laya-check below, so keep going either
  # way and let the self-test be the judge.
  .venv/bin/python scripts/train_navigation_laya.py --out "$CKPT" \
    || note "training did not clear its quality bar — verifying with the self-test below…"
fi
if .venv/bin/python -m aura laya-check; then
  note "Laya is answering — Aura's brain is on this Mac."
else
  note "Laya isn't answering yet. Aura still works (the offline gate answers"
  note "every decision), and the reason is in: $SUPPORT/aura.log"
  note "Re-run this script to retry."
fi

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
  If anything looks wrong, the first command to run is:
      ~/Library/Application\ Support/Aura/engine/.venv/bin/python -m aura doctor
      ~/Library/Application\ Support/Aura/engine/.venv/bin/python -m aura laya-check
  Re-run this script any time to update Aura.
EOF
