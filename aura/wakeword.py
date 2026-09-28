"""Wake word — the user's own activation phrase, running always-on and offline.

Two sources of wake models, both on-device:

1. Pretrained openWakeWord models (e.g. "hey_jarvis") — instant, works today.
2. A custom model the user trains for *their* phrase via
   scripts/train_wakeword.py (openWakeWord trainer, Apache-2.0). The trained
   ONNX file is dropped in the data dir and referenced from config; nothing
   about this module changes.

`phrase_gate` adds a second factor in always-on mode: the transcript must
start with the configured phrase, which kills nearly all false accepts.
"""

from __future__ import annotations

import time

try:
    import numpy as np

    HAS_NUMPY = True
except Exception:  # pragma: no cover
    HAS_NUMPY = False


class WakeEngine:
    """Interface. `feed(frame)` → True exactly once per accepted wake."""

    def feed(self, frame) -> bool:  # noqa: ANN001
        return False

    def reset(self) -> None: ...


class OpenWakeWordEngine(WakeEngine):
    """openWakeWord (ONNX) — ~tens of MB resident, runs happily on the ANE."""

    FRAME_80MS = 1280  # samples per openWakeWord prediction

    def __init__(
        self,
        models: list[str],
        threshold: float = 0.55,
        refractory_seconds: float = 2.5,
        on_load_error=None,
    ) -> None:
        try:
            from openwakeword.model import Model  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on host
            raise RuntimeError(f"openwakeword unavailable: {exc}") from exc

        local_paths = [m for m in models if str(m).endswith(".onnx")]
        pretrained = [m for m in models if not str(m).endswith(".onnx")]
        self._model = Model(
            wakeword_models=local_paths or pretrained or None,
            inference_framework="onnx",
        )
        self.threshold = threshold
        self.refractory = refractory_seconds
        self._buf = bytearray()
        self._last_fire = 0.0

    def feed(self, frame) -> bool:  # noqa: ANN001
        if not HAS_NUMPY:
            return False
        self._buf.extend(frame.pcm.tobytes())
        # openWakeWord wants 80 ms chunks; keep the remainder for the next frame.
        while len(self._buf) >= self.FRAME_80MS * 2:
            chunk = np.frombuffer(bytes(self._buf[: self.FRAME_80MS * 2]), dtype=np.int16)
            del self._buf[: self.FRAME_80MS * 2]
            scores = self._model.predict(chunk)
            if scores and max(scores.values()) >= self.threshold:
                now = time.monotonic()
                if now - self._last_fire >= self.refractory:
                    self._last_fire = now
                    self._model.reset()
                    return True
        return False

    def reset(self) -> None:
        self._buf.clear()
        try:
            self._model.reset()
        except Exception:
            pass


class ManualTrigger(WakeEngine):
    """Wake on demand — UI orb button, hotkey, CLI, or tests."""

    def __init__(self) -> None:
        self._flag = False

    def fire(self) -> None:
        self._flag = True

    def feed(self, frame) -> bool:  # noqa: ANN001
        if self._flag:
            self._flag = False
            return True
        return False


def build_wake_engine(cfg) -> WakeEngine:  # noqa: ANN001 - Config is dataclass
    """Factory used by the orchestrator; never raises — falls back to manual."""
    mode = getattr(cfg.wake, "mode", "manual")
    if mode == "openwakeword":
        try:
            return OpenWakeWordEngine(
                models=cfg.wake.models,
                threshold=cfg.wake.threshold,
                refractory_seconds=cfg.wake.refractory_seconds,
            )
        except RuntimeError:
            return ManualTrigger()
    return ManualTrigger()


# --------------------------------------------------------------------------- #
# The phrase gate — second factor for always-listening mode                    #
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() or ch == " " else " "
                            for ch in text.lower()).split())


def phrase_gate(transcript: str, phrase: str) -> bool:
    """In always-on mode the transcript must *start with* the user's phrase.

    Deliberately lenient about punctuation and casing ("Hey, Aura!" == "hey
    aura") and strict about word boundaries — "hey auraa" is not the wake
    phrase. This is the second factor that kills the false accepts a purely
    acoustic wake model can produce.
    """
    if not phrase.strip():
        return True
    said = _normalize(transcript).split()
    wanted = _normalize(phrase).split()
    return said[: len(wanted)] == wanted


def strip_phrase(transcript: str, phrase: str) -> str:
    """Remove a leading wake phrase so the planner sees only the command."""
    if not phrase.strip():
        return transcript.strip()
    norm_phrase = _normalize(phrase)
    norm_text = _normalize(transcript)
    if norm_text.startswith(norm_phrase):
        # Cut by position in the normalized string, then carry the remainder
        # from the original so capitalization/punctuation survive.
        remaining = norm_text[len(norm_phrase):]
        words = remaining.split()
        if not words:
            return ""
        original_words = transcript.split()
        # Heuristic re-join: take the last N words of the original transcript.
        return " ".join(original_words[-len(words):]) if words else ""
    return transcript.strip()
