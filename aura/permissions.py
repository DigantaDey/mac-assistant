"""Permissions — what Aura may touch, and how the user grants it.

Philosophy: macOS already has the perfect consent system (TCC). Aura doesn't
work around it; it *guides the user through it*. Every check here is honest:

  accessibility  AXIsProcessTrusted() — the real API, read from a short-lived
                 child so a grant made while Aura runs is seen immediately
  microphone     whether Aura's own audio bridge opened the mic
  automation     a harmless AppleEvent actually sent; result observed
  whisper/laya   local readiness checks (binaries + checkpoint load)

On non-Mac platforms everything degrades to None/"unavailable" so the Setup
wizard can show itself anywhere without lying about anything.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
import sys
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


def _load_application_services() -> Any:
    return ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )


_ACCESSIBILITY_PROBE = r"""
import ctypes
lib = ctypes.CDLL(
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
lib.AXIsProcessTrusted.restype = ctypes.c_bool
print("1" if lib.AXIsProcessTrusted() else "0")
"""


def _fresh_accessibility_check(timeout: float = 2.0) -> bool | None:
    """Read TCC from a fresh process so a settings change is not hidden by
    ApplicationServices' per-process cached AXIsProcessTrusted result.

    The app's engine is a child of Aura.app. Spawning the same interpreter for
    this read keeps the responsible-app attribution while giving Application
    Services a fresh cache. This is only a read; the UI owns the consent prompt.
    """
    if not sys.executable:
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _ACCESSIBILITY_PROBE],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in reversed((proc.stdout or "").splitlines()):
        value = line.strip().lower()
        if value in {"1", "true"}:
            return True
        if value in {"0", "false"}:
            return False
    return None


def _read_accessibility() -> bool | None:
    """Ask macOS, without trusting a cached answer from this process."""
    fresh = _fresh_accessibility_check()
    if fresh is not None:
        return fresh
    # Last resort. This does poison this process's own HIServices cache, which
    # is why it is a fallback rather than the first move: a short-lived child
    # has nothing cached and can therefore see a grant made mid-session.
    try:
        lib = _load_application_services()
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def check_accessibility() -> bool | None:
    """True when the process that drives your apps may use Accessibility.

    `AXIsProcessTrusted` is answered from a per-process cache that a
    long-lived engine fills once and keeps, so a grant made in System Settings
    while Aura runs stays invisible to it. Reading from a short-lived child
    gets the live answer, which is what lets the Setup panel acknowledge a
    grant the moment the user makes it — no restart, no "check again".

    Deliberately not memoised: a permission panel showing a state the user has
    already changed is worse than one extra process spawn.
    """
    if not is_mac():
        return None
    return _read_accessibility()


# --------------------------------------------------------------------------- #
# Which app actually holds the grant — and the words to say about it          #
# --------------------------------------------------------------------------- #


def _process_path(pid: int) -> str:
    try:
        proc = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def _parent_pid(pid: int) -> int:
    try:
        proc = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    try:
        return int((proc.stdout or "").strip() or 0)
    except ValueError:
        return 0


_identity_cache: str | None = None
_identity_resolved = False


def accessibility_identity() -> str | None:
    """The app macOS lists beside the Accessibility switch for this engine.

    TCC attributes Accessibility to the app that owns the process, so an
    engine started from a terminal is granted as *that terminal*, not as Aura.
    Naming it is the difference between "not granted" and an instruction the
    user can actually follow.

    Memoised because walking the parent chain costs a `ps` per hop and the
    answer cannot change: a process's ancestry is fixed for its lifetime.
    """
    global _identity_cache, _identity_resolved
    if _identity_resolved:
        return _identity_cache
    _identity_cache = _resolve_identity()
    _identity_resolved = True
    return _identity_cache


def _resolve_identity() -> str | None:
    if not is_mac():
        return None
    pid = os.getpid()
    seen: set[int] = set()
    for _ in range(8):
        pid = _parent_pid(pid)
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        match = re.search(r"/([^/]+)\.app/", _process_path(pid))
        if match:
            return match.group(1)
    return None


def accessibility_detail(granted: bool | None = None) -> str:
    """One honest sentence about the Accessibility state, for the UI and log.

    `granted` lets a caller that has already read the state describe it without
    paying for a second probe.
    """
    if granted is None:
        granted = check_accessibility()
    if granted is None:
        return "Accessibility can't be checked on this platform."
    identity = accessibility_identity()
    who = identity or "the app running Aura's engine"
    if granted:
        return f"Granted to {who} — Aura can act inside your apps."
    detail = f"macOS hasn't granted Accessibility to {who} yet."
    if identity and identity != "Aura":
        detail += (f" The engine was started from {identity}, so macOS files the "
                   f"grant under {identity} — launch Aura.app instead, or enable "
                   f"{identity} in the Accessibility list.")
    else:
        detail += " Switch Aura on under System Settings › Privacy & Security › Accessibility."
    return detail


def request_accessibility() -> tuple[str, str]:
    """Report Accessibility status; the native app owns the consent prompt.

    Calling AXIsProcessTrustedWithOptions from Aura's Python engine attributes
    the prompt to a different process on some macOS releases. Aura.app calls
    the native API itself, then this endpoint re-checks from a fresh process.

    status: "ok" | "asked" | "unavailable"
    """
    if not is_mac():
        return "unavailable", "Accessibility exists only on macOS"
    granted = check_accessibility()
    return ("ok" if granted else "asked"), accessibility_detail(granted)


def check_microphone(orch: Any) -> bool | None:
    """True when Aura's audio bridge is live (which requires the TCC grant)."""
    if not is_mac():
        return None
    return bool(getattr(orch, "_has_audio", False))


def request_microphone() -> tuple[str, str]:
    """Actually open the microphone — that's what triggers the consent dialog.

    status: "ok" | "denied" | "unavailable"
    """
    if not is_mac():
        return "unavailable", "Microphone access exists only on macOS"
    try:
        import numpy as np  # noqa: F401
        import sounddevice as sd
    except Exception as exc:
        return "unavailable", (
            "The voice components aren't installed yet — use “Download and set up” "
            f"in Setup. ({exc.__class__.__name__})")
    import time as _time

    stream = None
    try:
        try:
            stream = sd.InputStream(samplerate=16_000, channels=1, dtype="int16",
                                    blocksize=512)
            stream.start()
        except Exception:
            # The consent dialog may appear *while* the first open is being
            # rejected; give the user a beat to tap Allow, then try once more.
            _time.sleep(2.5)
            stream = sd.InputStream(samplerate=16_000, channels=1, dtype="int16",
                                    blocksize=512)
            stream.start()
        _time.sleep(0.5)          # let TCC settle
        return "ok", "Microphone is ready — Aura can hear you."
    except Exception as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("unauthorized", "permission", "tcc", "not authorized")):
            return "denied", ("macOS hasn't granted microphone access. Allow Aura under "
                              "System Settings › Privacy & Security › Microphone, then check again.")
        return "denied", f"Couldn't open the microphone: {str(exc)[:180]}"
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


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


def open_path(target: str) -> tuple[bool, str]:
    """Open a file or folder in Finder — the "show me my data" affordance."""
    if not is_mac():
        return False, "Opening folders in Finder exists only on macOS"
    from pathlib import Path as _Path

    if not _Path(target).expanduser().exists():
        return False, "That doesn't exist yet."
    try:
        subprocess.run(["open", str(_Path(target).expanduser())],
                       check=True, timeout=10)
        return True, "Opened in Finder."
    except Exception as exc:
        return False, f"Couldn't open it: {exc}"


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


def check_wake_models(cfg: Any) -> tuple[bool, str]:
    """Do the configured wake model files exist? (See Setup › Always listening.)"""
    from .wakeword import wake_models_ready

    return wake_models_ready(cfg)


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
    """Is the brain available? (Name kept for the app's existing contract.)

    Aura has no model server any more — the planner is Laya, in-process.
    This reports what the Setup panel needs to know: whether the decision
    model can run here (`mock`/`rules` engines are the deterministic layer
    alone, so they report False exactly like before).
    """
    if cfg.planner.engine in ("mock", "rules"):
        return False
    from .laya import laya_available

    return laya_available()
