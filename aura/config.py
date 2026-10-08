"""Configuration: a single TOML file, environment overrides, zero dependencies.

Resolution order (last wins):
    1. Built-in defaults (DEFAULTS below)
    2. `config.default.toml` shipped with the repo
    3. User file:  macOS  ~/Library/Application Support/Aura/config.toml
                   other  ~/.config/aura/config.toml
    4. Environment variables:  AURA_PROFILE, AURA_HOST, AURA_PORT, AURA_DATA_DIR
    5. Runtime overrides:  <data_dir>/runtime.toml  (written by the UI —
       the wake-word toggle lives here — so choices survive restarts)

`watch_paths()` lists everything the hot-reloader polls; `write_overrides()`
persists UI changes atomically.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

APP_DIR_NAME = "Aura"
RUNTIME_FILE = "runtime.toml"

# Fields the hot-reloader may apply to a running process. Everything else
# requires a restart — an honest, explicit list.
LIVE_FIELDS: dict[str, set[str]] = {
    "wake": {"enabled", "mode", "models", "threshold", "refractory_seconds", "phrase"},
    "stt": {"whisper_model"},   # the orchestrator rebuilds the STT engine live
    "tts": {"enabled", "voice", "rate"},
    "laya": {"confidence_threshold", "destructive_threshold", "route_threshold"},
    "logging": {"level", "event_level"},
    "safety": {"show_plan_before_run", "confirm_destructive", "blocked_patterns"},
    "session": {"max_utterance_seconds", "end_of_speech_seconds",
                "confirmation_timeout_seconds", "idle_unload_seconds",
                "max_session_seconds"},
}


# --------------------------------------------------------------------------- #
# Dataclasses — one per config section.                                       #
# --------------------------------------------------------------------------- #


@dataclass
class WakeConfig:
    enabled: bool = True
    # "manual"       — trigger from UI / hotkey (works everywhere, great for testing)
    # "openwakeword" — always-on on-device wake model(s); a custom phrase model
    #                  trained in-app via the Wake Phrase panel can be dropped in.
    mode: str = "manual"
    # Pretrained model names, or absolute paths to custom ONNX models the user
    # trained for their own activation phrase.
    models: list[str] = field(default_factory=lambda: ["hey_jarvis"])
    threshold: float = 0.55          # per-frame confidence needed to fire
    refractory_seconds: float = 2.5  # cooldown after a fire
    # Spoken prefix that must appear in the transcript to accept a wake in
    # always-on mode (e.g. "hey aura"). Empty disables the gate.
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
    # Aura's brain is Laya — a non-autoregressive decision model that answers
    # typed questions (which skill? does it match? is it destructive?) in one
    # forward pass. There is no LLM anywhere in the product: Laya is fast
    # enough to sit on the voice path and it cannot generate text, so it can
    # never invent an action or an argument.
    #   "laya"  — rules parse the everyday commands, Laya routes the rest
    #             (the default, and the only brain the product ships).
    #   "rules" — the deterministic rule layer alone.
    #   "mock"  — alias of "rules" (demo profile, tests).
    engine: str = "laya"
    max_actions: int = 3


@dataclass
class LayaConfig:
    enabled: bool = True
    # "auto"      — use the `laya` package when importable, otherwise the
    #               deterministic heuristic gate (demo profile, CI, first run).
    # "heuristic" — never load the package; the offline gate answers.
    backend: str = "auto"
    # A proposed action is auto-approved only when the gate is at least this
    # confident that it matches the request and is safe. Below it → ask.
    confidence_threshold: float = 0.62
    destructive_threshold: float = 0.55  # score above this ⇒ require confirmation
    # Routing (which skill a request means) is a `choice` question; a pick
    # below this probability is treated as "no skill" rather than acted on.
    route_threshold: float = 0.30
    # "" → let the package choose (MPS/CUDA/CPU); or "cpu", "mps", "cuda"…
    device: str = ""
    # "" → the router picks per language ("english", "multilingual", …)
    model: str = ""
    # The checkpoint to load. Resolution order (first found wins):
    #   1. adapter_dir      — your fine-tuned checkpoint (see scripts/nightly_laya.py)
    #   2. checkpoint_dir   — any native Laya checkpoint directory
    #   3. $AURA_LAYA_CHECKPOINT
    #   4. the bundled navigation checkpoint (assets/models/aura-nav-laya)
    #   5. the official ~421M checkpoint from the Hugging Face hub
    # "" on every one of them means "use the bundled checkpoint, else the hub".
    checkpoint_dir: str = ""
    # True keeps *every* checkpoint resident (fastest, ~GBs of RAM). False
    # warms just the one the router routes to — the shipped default.
    preload: bool = False
    max_loaded: int = 2              # how many checkpoints may stay in memory
    # 0 = keep the model resident forever. A positive value releases it after
    # that many idle seconds; the next question pays the reload.
    idle_unload_seconds: float = 0.0
    # A single question may not hold the session hostage: past this, the
    # offline gate answers and the reason is logged. Laya is not an LLM —
    # tens of milliseconds on a warm model — but a cold load or a slow CPU
    # gets honest headroom instead of a silent fallback.
    call_budget_seconds: float = 6.0
    adapter_dir: str = ""            # fine-tuned checkpoint directory


@dataclass
class TTSConfig:
    # "auto" — `say` on macOS, otherwise null; "say" | "piper" | "null"
    engine: str = "auto"
    enabled: bool = True
    voice: str = ""                  # empty → system default
    rate: int = 178                  # words per minute for `say`


@dataclass
class SafetyConfig:
    # Strict mode: when on, even safe, confident actions pause for a yes.
    # Destructive/risky actions ALWAYS confirm, with this on or off —
    # the safety gate's "ask" can never be switched off.
    show_plan_before_run: bool = False
    confirm_destructive: bool = True
    # Skills that always require explicit confirmation regardless of scoring.
    always_confirm: list[str] = field(default_factory=list)
    # Never execute, even if asked. Substring match on the raw command.
    blocked_patterns: list[str] = field(
        default_factory=lambda: ["rm -rf /", "diskutil erase", "sudo dd"]
    )


@dataclass
class LoggingConfig:
    """Two levels: what the file/stderr get, and what the app's feed shows."""
    level: str = "info"              # debug | info | warning | error
    event_level: str = "info"        # what is mirrored into the UI Activity feed
    file: bool = True                # also write <data_dir>/aura.log (rotating)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"          # the product never exposes itself to LAN
    port: int = 7331


@dataclass
class SessionConfig:
    max_utterance_seconds: float = 12.0
    end_of_speech_seconds: float = 0.7
    confirmation_timeout_seconds: float = 45.0
    # Hard ceiling for *active work* (planning or executing). Confirmation is
    # a user wait and has its own timer. Laya is fast (tens of ms warm), but
    # a cold checkpoint load or a slow machine gets real headroom — the
    # orchestrator still enforces MAX_ACTIVE_REQUEST_SECONDS as the absolute
    # cap, so Aura always answers or fails clearly instead of hanging.
    max_session_seconds: float = 20.0
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
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    session: SessionConfig = field(default_factory=SessionConfig)


# --------------------------------------------------------------------------- #
# Paths                                                                       #
# --------------------------------------------------------------------------- #


def resolved_profile(cfg) -> str:
    """What "auto" means on this machine — mac gets the real stack, the rest
    get the safe demo profile. Everything user-facing must say the same thing."""
    if cfg.profile != "auto":
        return cfg.profile
    return "mac" if sys.platform == "darwin" else "demo"


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


def runtime_overrides_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / RUNTIME_FILE


def watch_paths(data_dir: str | Path | None = None) -> list[Path]:
    """Files the hot-reloader polls (only existing ones matter)."""
    paths = [repo_default_config(), user_config_path()]
    if data_dir:
        paths.append(runtime_overrides_path(data_dir))
    return paths


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #


def _warn_bad_value(section: str, key: str, want: str, value: Any) -> None:
    print(f"[config] ignoring {section}.{key} = {value!r} — expected {want}",
          file=sys.stderr)


def _parse_list_literal(text: str) -> Any:
    """Parse a stringified list with JSON first, then Python literal syntax.

    Aura's own overrides used `str(list)` (single quotes — not JSON), while a
    hand-edited file is more likely to hold JSON. Either spelling is readable.
    """
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return parsed
    return None


def _coerce_list(value: Any) -> list[Any] | None:
    """Read a list out of a value that arrived as a string.

    Aura writes its own runtime overrides, and an earlier build stringified
    list values on the way out — so a real install can hold

        models = "['/…/wakewords/hey-aura.npz']"

    instead of a TOML array. Refusing to read that dropped the user's trained
    wake phrase on every launch, with only a warning on stderr to show for it.
    Repairing it here means an existing install heals itself on the next read;
    `_toml_value` writes a real array from now on.

    Returns None when the value is not a list in any readable spelling.
    """
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return None
    parsed = _parse_list_literal(text)
    return parsed if isinstance(parsed, list) else None


def _heal_value(value: Any) -> Any:
    """`_coerce_list` for rewriting: leave anything else exactly as it was."""
    if isinstance(value, str):
        coerced = _coerce_list(value)
        if coerced is not None:
            return coerced
    return value


def _apply(dc: Any, raw: dict[str, Any]) -> None:
    """Merge a raw dict into a dataclass instance.

    Unknown keys are ignored; a key whose *type* doesn't match is skipped
    with a warning — a typo in the user's file must not corrupt the running
    config (e.g. `models = "hey_jarvis"` where a list belongs). A list that
    arrived stringified is read rather than dropped (see `_coerce_list`).
    """
    if not isinstance(raw, dict):
        return
    section = type(dc).__name__
    known = {f.name for f in fields(dc)} if is_dataclass(dc) else set()
    for key, value in raw.items():
        if key not in known:
            continue
        current = getattr(dc, key)
        if is_dataclass(current):
            if isinstance(value, dict):
                _apply(current, value)
            else:
                _warn_bad_value(section, key, "a table", value)
        elif isinstance(current, bool):
            if isinstance(value, bool):
                setattr(dc, key, value)
            else:
                _warn_bad_value(section, key, "true/false", value)
        elif isinstance(current, list):
            coerced = _coerce_list(value)
            if coerced is not None:
                setattr(dc, key, coerced)
            else:
                _warn_bad_value(section, key, "a list", value)
        elif isinstance(current, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(dc, key, value)
            else:
                _warn_bad_value(section, key, "a number", value)
        elif isinstance(current, str):
            if isinstance(value, str):
                setattr(dc, key, value)
            else:
                _warn_bad_value(section, key, "a string", value)
        else:
            _warn_bad_value(section, key, "a supported type", value)


def _load_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:  # unreadable file must not kill the app
        print(f"[config] ignoring malformed {path}: {exc}", file=sys.stderr)
        return None


def load_config(explicit_path: str | None = None,
                data_dir: str | None = None) -> Config:
    cfg = Config()

    # Data dir must exist before runtime overrides can be located.
    if os.environ.get("AURA_DATA_DIR"):
        cfg.data_dir = os.environ["AURA_DATA_DIR"]
    if not cfg.data_dir:
        cfg.data_dir = str(Path(data_dir).expanduser() if data_dir else default_data_dir())

    # "auto" off-Mac resolves to the demo profile *as a base layer*:
    # predictable providers unless the user's files say otherwise.
    demo_base = cfg.profile == "auto" and sys.platform != "darwin"
    if demo_base:
        cfg.wake.mode = "manual"
        cfg.stt.engine = "null"
        cfg.tts.engine = "null"
        # The planner is deliberately NOT pinned: the brain is Laya on every
        # platform. Off-Mac the dry-run bridge executes safely, so the whole
        # decision path (rules → Laya routing → gate) runs for real.

    # Keys the demo base pins. The shipped config.default.toml describes the
    # real (mac) install, so its pinned keys must not leak into demo mode —
    # user files (explicit intent) still win over the demo base.
    demo_pins = {"wake": {"mode"}, "stt": {"engine"}, "tts": {"engine"}}
    for path in (repo_default_config(), user_config_path(),
                 runtime_overrides_path(cfg.data_dir)):
        raw = _load_toml(path)
        if not raw:
            continue
        if demo_base and path == repo_default_config():
            raw = {sec: ({k: v for k, v in vals.items()
                          if k not in demo_pins.get(sec, set())}
                         if isinstance(vals, dict) else vals)
                   for sec, vals in raw.items()}
        _apply(cfg, raw)

    if explicit_path:
        raw = _load_toml(Path(explicit_path).expanduser())
        if raw:
            _apply(cfg, raw)

    # Environment overrides
    if os.environ.get("AURA_PROFILE"):
        cfg.profile = os.environ["AURA_PROFILE"]
    if os.environ.get("AURA_HOST"):
        cfg.server.host = os.environ["AURA_HOST"]
    if os.environ.get("AURA_PORT"):
        cfg.server.port = int(os.environ["AURA_PORT"])

    # An *explicit* demo profile stays pinned even over user files — that's
    # the reproducible dev mode by definition (the UI says so too).
    if cfg.profile == "demo":
        cfg.wake.mode = "manual"
        cfg.stt.engine = "null"
        cfg.planner.engine = "mock"
        cfg.tts.engine = "null"
        cfg.laya.backend = "heuristic"

    # Environment overrides for logging (the app sets none of these; a
    # developer debugging a Mac install sets all of them).
    if os.environ.get("AURA_LOG_LEVEL"):
        cfg.logging.level = os.environ["AURA_LOG_LEVEL"]
    if os.environ.get("AURA_LOG_EVENT_LEVEL"):
        cfg.logging.event_level = os.environ["AURA_LOG_EVENT_LEVEL"]

    return cfg


# --------------------------------------------------------------------------- #
# Writing runtime overrides (the UI's persistence path)                       #
# --------------------------------------------------------------------------- #


def _toml_escape(text: str) -> str:
    """Escape one string as a TOML basic string body (quotes excluded)."""
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _toml_value(value: Any) -> str | None:
    """Render one Python value as TOML. None means "don't write this key".

    A list must round-trip as a TOML *array*. Stringifying it (what an earlier
    build did) produced `models = "['…hey-aura.npz']"`, which the loader then
    rejected as "expected a list" — so the user's own trained wake phrase was
    silently discarded on every launch after the one that trained it.
    """
    if value is None:
        return None
    if isinstance(value, bool):          # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(float(value)) if isinstance(value, float) else str(value)
    if isinstance(value, (list, tuple)):
        parts = [rendered for rendered in (_toml_value(item) for item in value)
                 if rendered is not None]
        return "[" + ", ".join(parts) + "]"
    return f'"{_toml_escape(str(value))}"'


def write_overrides(data_dir: str | Path, updates: dict[str, dict[str, Any]]) -> Path:
    """Merge updates into <data_dir>/runtime.toml, atomically.

    `updates` is section → {field: value}, e.g.
        {"wake": {"mode": "openwakeword", "phrase": "hey aura"}}
    Only LIVE_FIELDS keys are persisted — the UI cannot smuggle in fields
    that would silently do nothing.
    """
    path = runtime_overrides_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    current: dict[str, dict[str, Any]] = {}
    raw = _load_toml(path) or {}
    for section, values in raw.items():
        if isinstance(values, dict):
            # Heal values an earlier build stringified, so the file converges
            # on the array spelling instead of carrying the broken one forever.
            current[section] = {key: _heal_value(value) for key, value in values.items()}

    for section, values in updates.items():
        allowed = LIVE_FIELDS.get(section, set())
        safe = {k: v for k, v in values.items() if k in allowed}
        if safe:
            current.setdefault(section, {}).update(safe)

    lines: list[str] = []
    for section, values in current.items():
        if not values:
            continue
        rendered = []
        for key, value in values.items():
            text = _toml_value(value)
            if text is not None:          # None has no TOML spelling — omit it
                rendered.append(f"{key} = {text}")
        if not rendered:
            continue
        lines.append(f"[{section}]")
        lines.extend(rendered)
        lines.append("")

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines))
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path
