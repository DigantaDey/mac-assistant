"""In-app component installation — the Setup panel's "Install" buttons.

The user taps Install; Aura does the rest and reports progress as events on
the same bus the UI already watches. No terminal, no copy-pasting commands:

  1. python components   — `pip install -e "<repo>[mac]"` into the running env
  2. wake-word models    — openWakeWord's pretrained models, cached locally
  3. speech model        — whisper.cpp ggml model downloaded with progress,
                           then wired into config automatically
  4. system tools        — an honest probe (Homebrew / whisper-cli /
                           `say`); anything missing is named precisely, with
                           the exact reason, never a vague failure

Steps are individually runnable (the Setup panel's per-card buttons call
`run_step`) and together (the big "Set everything up" button calls
`run_installer`). A failure in one step never blocks the others, and the
summary tells the user exactly what (if anything) still needs attention.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

WHISPER_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
WHISPER_MODEL_MB = 142


@dataclass
class StepResult:
    key: str
    title: str
    status: str            # ok | fail | skip
    detail: str = ""


def _publish(orch, key: str, title: str, status: str, detail: str = "") -> None:
    orch.bus.publish("setup_progress", key=key, title=title, status=status, detail=detail)


# --------------------------------------------------------------------------- #
# The steps — each independent, each reporting the same way                    #
# --------------------------------------------------------------------------- #


def step_python(orch, repo: Path) -> StepResult:
    """Install/refresh the Python voice components into the running env."""
    title = "Voice components"
    _publish(orch, "python", title, "running")
    result: StepResult
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             f"{repo}[mac]"],
            capture_output=True, text=True, timeout=1200,
        )
        if proc.returncode == 0:
            result = StepResult("python", title, "ok",
                                "Microphone, wake-word and speech packages ready.")
        else:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            result = StepResult("python", title, "fail",
                                tail[-1][:200] if tail else "install failed")
    except Exception as exc:
        result = StepResult("python", title, "fail", str(exc)[:200])
    _publish(orch, "python", title, result.status, result.detail)
    return result


def step_wake(orch) -> StepResult:
    """Cache openWakeWord's pretrained models on this machine."""
    title = "Wake-word models"
    _publish(orch, "wake", title, "running")
    result: StepResult
    try:
        import openwakeword.utils  # type: ignore

        openwakeword.utils.download_models()
        result = StepResult("wake", title, "ok", "Pretrained models cached on this Mac.")
        # If the user already chose "Always listening" while the models were
        # missing, the wake engine fell back to tap-to-talk. Now it can go
        # real — rebuild it right here, no restart.
        if getattr(orch.cfg.wake, "mode", "manual") == "openwakeword":
            try:
                orch._wake = orch._build_wake()
                status = orch.wake_status()
                result.detail += (" Always-listening is now active."
                                  if status["active"]
                                  else f" Listener not active: {status['detail']}")
            except Exception as exc:
                result.detail += f" Listener rebuild failed: {exc}"
    except Exception as exc:
        result = StepResult("wake", title, "fail",
                            f"Download didn't take ({exc.__class__.__name__}). "
                            "Your own trained phrase doesn't need these.")
    _publish(orch, "wake", title, result.status, result.detail)
    return result


def step_whisper(orch) -> StepResult:
    """Download the whisper.cpp speech model and wire it into config."""
    title = "Speech model"
    _publish(orch, "whisper", title, "running")
    cfg = orch.cfg
    result: StepResult
    model_path = Path(cfg.stt.whisper_model).expanduser() if cfg.stt.whisper_model else None
    if model_path is not None and model_path.is_file() and model_path.stat().st_size > 0:
        result = StepResult("whisper", title, "ok", "Already downloaded.")
    else:
        target = Path(cfg.data_dir) / "models" / "ggml-base.en.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _download_with_progress(orch, "whisper", title, WHISPER_MODEL_URL, target,
                                    WHISPER_MODEL_MB)
            from .config import write_overrides

            write_overrides(cfg.data_dir, {"stt": {"whisper_model": str(target)}})
            cfg.stt.whisper_model = str(target)
            from .stt import build_stt

            orch.stt = build_stt(cfg)
            result = StepResult("whisper", title, "ok", "Ready — and wired into Aura.")
        except Exception as exc:
            result = StepResult("whisper", title, "fail", str(exc)[:200])
    _publish(orch, "whisper", title, result.status, result.detail)
    return result


def step_tools(orch) -> StepResult:
    """Honest probe of the system tools — names exactly what's missing."""
    title = "System tools"
    _publish(orch, "tools", title, "running")
    found, missing = [], []
    for tool, label in (("say", "Speech"), ("whisper-cli", "whisper.cpp")):
        (found if shutil.which(tool) else missing).append(label)
    detail = ("Found: " + ", ".join(found) + ".") if found else "None found yet."
    if missing:
        detail += f" Optional, installable with Homebrew: {', '.join(missing)}."
    result = StepResult("tools", title, "ok", detail)
    _publish(orch, "tools", title, result.status, result.detail)
    return result


STEPS = {
    "python": step_python,
    "wake": step_wake,
    "whisper": step_whisper,
    "tools": step_tools,
}


def run_step(orch, repo: Path, key: str) -> StepResult:
    """Run one named step. `python` needs the repo path; the others don't."""
    key = str(key or "").lower()
    if key not in STEPS:
        return StepResult(key or "?", "Unknown step", "fail",
                          f"no such step {key!r} (expected: {', '.join(sorted(STEPS))})")
    fn = STEPS[key]
    if key == "python":
        return fn(orch, repo)
    return fn(orch)


def run_installer(orch, repo: Path) -> list[StepResult]:
    """Everything, in order — the "Set everything up" button."""
    results: list[StepResult] = [
        step_python(orch, repo),
        step_wake(orch),
        step_whisper(orch),
        step_tools(orch),
    ]
    ok = all(r.status in ("ok", "skip") for r in results)
    orch.bus.publish("setup_done", ok=ok,
                     summary="Aura is ready." if ok else "Finished with notes — see Setup.")
    return results


def _download_with_progress(orch, key: str, title: str, url: str, target: Path, total_mb: int) -> None:
    def report(bytes_done: int, total: int) -> None:
        mb = bytes_done / (1024 * 1024)
        _publish(orch, key, title, "running",
                 f"{mb:.0f} MB of {total_mb} MB" if total else f"{mb:.0f} MB…")

    tmp = target.with_suffix(".part")
    last = 0.0

    def hook(count: int, block: int, total: int) -> None:
        nonlocal last
        now = count * block
        if now - last > 8 * 1024 * 1024 or (total and now >= total):
            last = now
            report(now, total)

    req = urllib.request.Request(url, headers={"User-Agent": "Aura/0.5.1"})
    with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            report(fh.tell(), 0)
    os.replace(tmp, target)
    _publish(orch, key, title, "running",
             f"{target.stat().st_size / (1024 * 1024):.0f} MB — done")
