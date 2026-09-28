"""Text-to-speech — how Aura talks back.

macOS ships `say` with every system: offline, instant, zero RAM, decent
voices — the right default for a lightweight assistant. Piper/Kokoro can be
dropped in later for premium voices without touching anything else.
"""

from __future__ import annotations

import re
import shutil
import subprocess


class TTS:
    def speak(self, text: str) -> float:
        """Speak; return seconds spoken (0 if disabled)."""
        raise NotImplementedError


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class MacSayTTS(TTS):
    def __init__(self, voice: str = "", rate: int = 178) -> None:
        if not shutil.which("say"):
            raise RuntimeError("`say` not found — is this a Mac?")
        self.voice = voice
        self.rate = rate

    def speak(self, text: str) -> float:
        text = _CONTROL.sub(" ", text).strip()
        if not text:
            return 0.0
        cmd = ["say"]
        if self.voice:
            cmd += ["-v", self.voice]
        cmd += ["-r", str(self.rate), text]
        subprocess.run(cmd, check=False, timeout=120)
        # ~2.6 words/second at the default rate; good enough for pacing.
        return max(0.6, len(text.split()) / 2.6)


class NullTTS(TTS):
    def speak(self, text: str) -> float:
        return 0.0


def build_tts(cfg) -> TTS:
    if not cfg.tts.enabled or cfg.tts.engine == "null":
        return NullTTS()
    try:
        return MacSayTTS(voice=cfg.tts.voice, rate=cfg.tts.rate)
    except RuntimeError:
        return NullTTS()
