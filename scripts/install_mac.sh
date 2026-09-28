#!/usr/bin/env bash
# Aura — Mac bootstrap. Idempotent; safe to re-run.
set -euo pipefail

say_step() { printf "\n\033[1;36m▸ %s\033[0m\n" "$1"; }

say_step "Checking Homebrew"
if ! command -v brew >/dev/null; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

say_step "Installing system dependencies (portaudio, whisper-cpp, ollama)"
brew install portaudio whisper-cpp ollama

say_step "Starting Ollama and pulling the planner model"
brew services start ollama || true
sleep 2
ollama pull qwen3:4b

say_step "Creating the Python environment"
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[mac]"

say_step "Doctor — what will run on this Mac"
python -m aura doctor || true

say_step "Done"
cat <<EOF

  Start Aura:            source .venv/bin/activate && python -m aura serve
  Make it yours:         python scripts/train_wakeword.py "your phrase"
  First run permissions: System Settings → Privacy & Security
                           • Accessibility  (drive apps)
                           • Microphone     (hear you)
                           • Automation     (per-app AppleScript consent)

  The UI lives at http://127.0.0.1:7331 — nothing leaves this machine.
EOF
