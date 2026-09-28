"""Configuration: a single TOML file, environment overrides, zero dependencies.

Resolution order (last wins):
    1. Built-in defaults (DEFAULTS below)
    2. `config.default.toml` shipped with the repo
    3. User file:  macOS  ~/Library/Application Support/Aura/config.toml
                   other  ~/.config/aura/config.toml
    4. Environment variables:  AURA_PROFILE, AURA_HOST, AURA_PORT, AURA_DATA_DIR
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

APP_DIR_NAME = "Aura"

# --------------------------------------------------------------------------- #
# Dataclasses — one per config section.                                       #
# --------------------------------------------------------------------------- #


@dataclass
class WakeConfig:
    enabled: bool = True
    # "manual"    — trigger from UI / hotkey (works everywhere, great for testing)
    # "openwakeword" — always-on on-device wake model(s); a custom phrase model
    #                  trained via scripts/train_wakeword.py can be dropped in.
    mode: str = "manual"
    # Pretrained model names, or absolute paths to custom ONNX models the user
    # trained for their own activation phrase.
    models: list[str] = field(default_factory=lambda: ["hey_jarvis"])
    threshold: float = 0.55          # per-frame confidence needed to fire
    refractory_seconds: float = 2.5  # cooldown after a fire
    # Optional spoken prefix that must appear in the transcript to accept a
    # wake in always-on mode (e.g. "hey aura"). Empty disables the gate.
    phrase: str = ""


@dataclass
class STTConfig:
    # "auto"       — whisper.cpp binary if found, else faster-whisper, else null
    # "whisper_cpp" | "faster_whisper" | "null"
    engine: str = "auto"
    language: str = "en"
    whisper_cpp_bin: str = ""        # empty → search PATH (whisper-cli, main)
    whisper_model: str = ""          # path to ggml model (required for whisper.cpp)
    faster_whisper_model: str = "small"  # tiny/base/small/medium…


@dataclass
class PlannerConfig:
    # "auto" — first healthy OpenAI-compatible endpoint; "mock" — deterministic
    # planner used by the demo profile and tests.
    engine: str = "auto"
    # Any OpenAI-compatible server works, all fully local:
    #   Ollama:   http://127.0.0.1:11434/v1        (model e.g. qwen3:4b)
    #   mlx_lm:   http://127.0.0.1:8080/v1         (mlx_lm.server)
    #   llama.cpp: http://127.0.0.1:8080/v1        (llama-server)
    #   LM Studio: http://127.0.0.1:1234/v1
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3:4b"
    api_key: str = "local"           # local servers ignore this; never a cloud key
    temperature: float = 0.2
    timeout_seconds: float = 20.0
    max_actions: int = 3


@dataclass
class LayaConfig:
    enabled: bool = True
    # "auto" — use the `laya` package (MLX/CoreML backends) when importable,
    #          otherwise the deterministic heuristic gate (used in demo/tests).
    backend: str = "auto"
    # A proposed action is auto-approved only when the gate is at least this
    # confident that it matches the request and is safe. Below it → ask.
    confidence_threshold: float = 0.62
    destructive_threshold: float = 0.55  # score above this ⇒ require confirmation
    adapter_dir: str = ""            # fine-tuned adapter loaded at runtime


@dataclass
class TTSConfig:
    # "auto" — `say` on macOS, otherwise null; "say" | "piper" | "null"
    engine: str = "auto"
    enabled: bool = True
    voice: str = ""                  # empty → system default
    rate: int = 178                  # words per minute for `say`


@dataclass
class SafetyConfig:
    show_plan_before_run: bool = True
    confirm_destructive: bool = True
    # Skills that always require explicit confirmation regardless of scoring.
    always_confirm: list[str] = field(default_factory=list)
    # Never execute, even if asked. Substring match on the raw command.
    blocked_patterns: list[str] = field(
        default_factory=lambda: ["rm -rf /", "diskutil erase", "sudo dd"]
    )


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"          # the product never exposes itself to LAN
    port: int = 7331


@dataclass
class SessionConfig:
    max_utterance_seconds: float = 12.0
    end_of_speech_seconds: float = 0.7
    confirmation_timeout_seconds: float = 45.0
    # Unload warm models after this much idle time (lightweight promise).
    idle_unload_seconds: float = 180.0


@dataclass
class Config:
    profile: str = "auto"            # auto | mac | demo
    data_dir: str = ""               # empty → platform default
    wake: WakeConfig = field(default_factory=WakeConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    laya: LayaConfig = field(default_factory=LayaConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    session: SessionConfig = field(default_factory=SessionConfig)


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #


def default_data_dir() -> Path:
    env = os.environ.get("AURA_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path.home() / ".local" / "share" / "aura"


def user_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME / "config.toml"
    return Path.home() / ".config" / "aura" / "config.toml"


def repo_default_config() -> Path:
    return Path(__file__).resolve().parent.parent / "config.default.toml"


def _apply(dc: Any, raw: dict[str, Any]) -> None:
    """Merge a raw dict into a dataclass instance, ignoring unknown keys."""
    if not isinstance(raw, dict):
        return
    known = {f.name for f in fields(dc)} if is_dataclass(dc) else set()
    for key, value in raw.items():
        if key not in known:
            continue
        current = getattr(dc, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            setattr(dc, key, value)
        elif isinstance(current, bool) or isinstance(value, (str, int, float, bool, type(None))):
            setattr(dc, key, value)


def load_config(explicit_path: str | None = None) -> Config:
    cfg = Config()

    for path in (repo_default_config(), user_config_path()):
        if explicit_path and Path(explicit_path) == path:
            pass
        if path.is_file():
            try:
                raw = tomllib.loads(path.read_text())
            except tomllib.TOMLDecodeError as exc:  # unreadable file must not kill the app
                print(f"[config] ignoring malformed {path}: {exc}", file=sys.stderr)
                continue
            _apply(cfg, raw)

    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.is_file():
            _apply(cfg, tomllib.loads(path.read_text()))

    # Environment overrides
    if os.environ.get("AURA_PROFILE"):
        cfg.profile = os.environ["AURA_PROFILE"]
    if os.environ.get("AURA_HOST"):
        cfg.server.host = os.environ["AURA_HOST"]
    if os.environ.get("AURA_PORT"):
        cfg.server.port = int(os.environ["AURA_PORT"])
    if os.environ.get("AURA_DATA_DIR"):
        cfg.data_dir = os.environ["AURA_DATA_DIR"]

    # Demo profile pinning: predictable providers so the flow works anywhere.
    if cfg.profile == "demo":
        cfg.wake.mode = "manual"
        cfg.stt.engine = "null"
        cfg.planner.engine = "mock"
        cfg.tts.engine = "null"

    if not cfg.data_dir:
        cfg.data_dir = str(default_data_dir())
    return cfg
