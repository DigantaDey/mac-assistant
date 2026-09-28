"""Permissions — what Aura may touch, and how the user grants it.

Philosophy: macOS already has the perfect consent system (TCC). Aura doesn't
work around it; it *guides the user through it*. Every check here is honest:

  accessibility  AXIsProcessTrusted() — the real API, no guessing
  microphone     whether Aura's own audio bridge opened the mic
  automation     a harmless AppleEvent actually sent; result observed
  whisper/llm    local readiness checks (binaries + endpoints)

On non-Mac platforms everything degrades to None/"unavailable" so the Setup
wizard can show itself anywhere without lying about anything.
"""

from __future__ import annotations

import platform
import subprocess
import urllib.request
from typing import Any

# Deep links into System Settings → Privacy & Security. Apple keeps moving
# these panes; the anchors below are the stable ones across Ventura and later.
_SETTING_URLS = {
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
}


def is_mac() -> bool:
    return platform.system() == "Darwin"


def check_accessibility() -> bool | None:
    """True when this process may read UI elements / send keystrokes.

    Uses AXIsProcessTrusted — the same API Voice Control and every automation
    utility sit on. When TCC hasn't decided yet, this is False; once the user
    flips the toggle it becomes True without a restart.
    """
    if not is_mac():
        return None
    try:
        import ctypes

        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def check_microphone(orch: Any) -> bool | None:
    """True when Aura's audio bridge is live (which requires the TCC grant)."""
    if not is_mac():
        return None
    return bool(getattr(orch, "_has_audio", False))


def open_settings(target: str) -> tuple[bool, str]:
    """Open the exact Privacy pane for a permission. Returns (ok, message)."""
    url = _SETTING_URLS.get(target)
    if url is None:
        return False, f"unknown settings target {target!r}"
    if not is_mac():
        return False, "System Settings exists only on macOS"
    try:
        subprocess.run(["open", url], check=True, timeout=10)
        return True, f"opened {target} settings"
    except Exception as exc:
        return False, f"couldn't open System Settings: {exc}"


# A deliberate no-op: read the name of the first process. If TCC hasn't been
# granted, macOS shows the consent dialog (or returns error -1743 if denied) —
# which is exactly what we want the user to see, on their terms.
_AUTOMATION_PROBE = (
    'tell application "System Events" to get name of first application process'
)


def test_automation(timeout: float = 8.0) -> tuple[str, str]:
    """Actually send one AppleScript event. Returns (status, detail).

    status: "ok" | "denied" | "unavailable"
    """
    if not is_mac():
        return "unavailable", "AppleScript automation exists only on macOS"
    try:
        proc = subprocess.run(
            ["osascript", "-e", _AUTOMATION_PROBE],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ("denied", "The AppleEvent timed out — a consent dialog may be "
                          "waiting in System Settings › Privacy & Security › Automation.")
    if proc.returncode == 0:
        return "ok", f"Automation works (reached “{proc.stdout.strip() or 'System Events'}”)."
    err = (proc.stderr or "").strip()
    if "-1743" in err or "not allowed" in err.lower():
        return "denied", "macOS denied the AppleEvent — grant Aura under " \
                         "System Settings › Privacy & Security › Automation."
    return "denied", err or "osascript failed"


def check_whisper_cpp(cfg: Any) -> bool:
    """Is a working whisper.cpp setup configured (binary + model)?"""
    if not is_mac():
        return False
    from .stt import WhisperCppSTT

    try:
        WhisperCppSTT(cfg.stt.whisper_model, cfg.stt.language, cfg.stt.whisper_cpp_bin)
        return True
    except RuntimeError:
        return False


def check_planner_server(cfg: Any, timeout: float = 0.8) -> bool:
    """Is the local LLM endpoint (Ollama / mlx_lm / llama-server / LM Studio) alive?"""
    if cfg.planner.engine == "mock":
        return False
    url = cfg.planner.base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False
