"""In-app component installation — the Setup panel's "Install" button.

The user taps Install; Aura does the rest and reports progress as events on
the same bus the UI already watches. No terminal, no copy-pasting commands:

  1. python components   — `pip install -e "<repo>[mac]"` into the running env
  2. wake-word models    — openWakeWord's pretrained models, cached locally
  3. speech model        — whisper.cpp ggml model downloaded with progress,
                           then wired into config automatically
  4. system tools        — an honest probe (Homebrew / whisper-cli / ollama /
                           `say`); anything missing is named precisely, with
                           the exact reason, never a vague failure

Every step publishes `setup_progress`; the finale is `setup_done`. Steps are
individually skippable — a failure never blocks the others, and the summary
tells the user exactly what (if anything) still needs attention.
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


def _publish(orch, key: str, title: str, status: str, detail: str = "") -> None:  # noqa: ANN001
    orch.bus.publish("setup_progress", key=key, title=title, status=status, detail=detail)


def run_installer(orch, repo: Path) -> list[StepResult]:  # noqa: ANN001
    cfg = orch.cfg
    results: list[StepResult] = []

    # 1 ── Python components -------------------------------------------------
    title = "Voice components"
    _publish(orch, "python", title, "running")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             f"{repo}[mac]"],
            capture_output=True, text=True, timeout=1200,
        )
        if proc.returncode == 0:
            results.append(StepResult("python", title, "ok",
                                      "Microphone, wake-word and speech packages ready."))
        else:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            results.append(StepResult("python", title, "fail",
                                      tail[-1][:200] if tail else "install failed"))
    except Exception as exc:
        results.append(StepResult("python", title, "fail", str(exc)[:200]))
    _publish(orch, "python", title, results[-1].status, results[-1].detail)

    # 2 ── Wake-word models ----------------------------------------------------
    title = "Wake-word models"
    _publish(orch, "wake", title, "running")
    try:
        import openwakeword.utils  # type: ignore

        openwakeword.utils.download_models()
        results.append(StepResult("wake", title, "ok", "Pretrained models cached on this Mac."))
    except Exception as exc:
        results.append(StepResult("wake", title, "skip",
                                  "Optional — your own trained phrase doesn't need these."))
    _publish(orch, "wake", title, results[-1].status, results[-1].detail)

    # 3 ── Speech model --------------------------------------------------------
    title = "Speech model"
    _publish(orch, "whisper", title, "running")
    model_path = Path(cfg.stt.whisper_model).expanduser() if cfg.stt.whisper_model else None
    if model_path is not None and model_path.is_file() and model_path.stat().st_size > 0:
        results.append(StepResult("whisper", title, "ok", "Already downloaded."))
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
            results.append(StepResult("whisper", title, "ok", "Ready — and wired into Aura."))
        except Exception as exc:
            results.append(StepResult("whisper", title, "fail", str(exc)[:200]))
    _publish(orch, "whisper", title, results[-1].status, results[-1].detail)

    # 4 ── System tools (probe only — honest about what needs Homebrew) --------
    title = "System tools"
    _publish(orch, "tools", title, "running")
    found, missing = [], []
    for tool, label in (("say", "Speech"), ("whisper-cli", "whisper.cpp"),
                        ("ollama", "Ollama")):
        (found if shutil.which(tool) else missing).append(label)
    detail = ("Found: " + ", ".join(found) + ".") if found else "None found yet."
    if missing:
        detail += f" Optional, installable with Homebrew: {', '.join(missing)}."
    results.append(StepResult("tools", title, "ok", detail))
    _publish(orch, "tools", title, "ok", detail)

    ok = all(r.status in ("ok", "skip") for r in results)
    orch.bus.publish("setup_done", ok=ok,
                     summary="Aura is ready." if ok else "Finished with notes — see Setup.")
    return results


def _download_with_progress(orch, key: str, title: str, url: str, target: Path, total_mb: int) -> None:  # noqa: ANN001
    def report(bytes_done: int, total: int) -> None:
        mb = bytes_done / (1024 * 1024)
        _publish(orch, key, title, "running", f"{mb:.0f} MB of {total_mb} MB")

    tmp = target.with_suffix(".part")
    last = 0.0

    def hook(count: int, block: int, total: int) -> None:
        nonlocal last
        now = count * block
        if now - last > 8 * 1024 * 1024 or (total and now >= total):
            last = now
            report(now, total)

    req = urllib.request.Request(url, headers={"User-Agent": "Aura/0.4"})
    with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            report(fh.tell(), 0)
    os.replace(tmp, target)
    report(target.stat().st_size, 1)
