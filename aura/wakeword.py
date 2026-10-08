"""Wake word — the user's own activation phrase, running always-on and offline.

Two sources of wake models, both on-device:

1. Pretrained openWakeWord models (e.g. "hey_jarvis") — instant, works today.
2. A custom model the user trains for *their* phrase via
   in-app Wake Phrase training (aura.wakeword_trainer). The trained
   ONNX file is dropped in the data dir and referenced from config; nothing
   about this module changes.

`phrase_gate` adds a second factor in always-on mode: the transcript must
start with the configured phrase, which kills nearly all false accepts.
"""

from __future__ import annotations

import time
from pathlib import Path

try:
    import numpy as np

    HAS_NUMPY = True
except Exception:  # pragma: no cover
    HAS_NUMPY = False


class WakeEngine:
    """Interface. `feed(frame)` → True exactly once per accepted wake."""

    def feed(self, frame) -> bool:
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

    def feed(self, frame) -> bool:
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

    def feed(self, frame) -> bool:
        if self._flag:
            self._flag = False
            return True
        return False


class TemplateWakeEngine(WakeEngine):
    """Detects the user's own trained phrase — the .npz from the in-app
    Wake Phrase Studio. Scores the rolling one-second window with the same
    embedding and length normalization the trainer calibrated on; fires once
    per accepted wake, with a refractory period."""

    SCORE_EVERY = 8          # frames (~0.26 s) between scoring passes

    def __init__(self, model_path: str, threshold_margin: float = 0.0,
                 refractory_seconds: float = 2.5) -> None:
        try:
            import numpy as np_
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"numpy unavailable: {exc}") from exc
        self._np = np_
        from .wakeword_trainer import SR, WINDOW_S, spectral_embedding

        self._embed = spectral_embedding
        self._window_samples = int(WINDOW_S * SR) * 2  # int16 → bytes
        try:
            data = np_.load(model_path, allow_pickle=False)
        except Exception as exc:
            raise RuntimeError(f"could not load wake template {model_path}: {exc}") from exc
        self.template = data["template"]
        self.threshold = float(data["threshold"]) + threshold_margin
        self.phrase = str(data.get("phrase", ""))
        self.refractory = max(0.0, float(refractory_seconds))
        self._buf = bytearray()
        self._since_score = 0
        self._last_fire = 0.0

    def feed(self, frame) -> bool:
        pcm = frame.pcm
        self._buf.extend(pcm.tobytes() if hasattr(pcm, "tobytes") else bytes(pcm))
        self._since_score += 1
        if self._since_score < self.SCORE_EVERY:
            return False
        self._since_score = 0
        if len(self._buf) < self._window_samples:
            return False

        window = self._np.frombuffer(
            bytes(self._buf[-self._window_samples:]), dtype=self._np.int16)
        emb = self._embed(window)
        denom = float(self._np.linalg.norm(emb) * self._np.linalg.norm(self.template))
        if denom == 0:
            return False
        score = float(self._np.dot(emb, self.template) / denom)
        if score < self.threshold:
            return False
        now = time.monotonic()
        if now - self._last_fire < self.refractory:
            return False
        self._last_fire = now
        self._buf.clear()
        return True

    def reset(self) -> None:
        self._buf.clear()


def load_template_meta(model_path: str) -> dict:
    """Phrase + threshold of a trained template, for display in the UI."""
    try:
        import numpy as np_

        data = np_.load(model_path, allow_pickle=False)
        return {"phrase": str(data.get("phrase", "")),
                "threshold": float(data["threshold"])}
    except Exception:
        return {}


def wake_models_ready(cfg, models: list[str] | None = None) -> tuple[bool, str]:
    """Cheap, honest check: do the configured wake model files exist on disk?

    Used by the Setup panel so "always listening" can offer a one-tap
    download instead of silently falling back to manual wake. `models`
    overrides the configured list — the factory passes just the pretrained
    names it is about to hand to openWakeWord.
    """
    if models is None:
        models = list(getattr(cfg.wake, "models", None) or [])
    else:
        models = list(models)
    if not models:
        return True, "No wake model needed — manual wake is active."
    for m in models:
        path = str(m)
        if path.endswith((".npz", ".onnx")):
            if not Path(path).expanduser().is_file():
                return False, f"Model file missing: {path}"
            continue
        # Pretrained openWakeWord name → look for the cached .onnx resource.
        try:
            import openwakeword  # type: ignore
        except Exception:
            return False, "openWakeWord package not installed — install from Setup."
        res = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        matches = list(res.glob(f"{path}*.onnx")) if res.is_dir() else []
        if not matches:
            return False, f"Pretrained model “{path}” not downloaded yet."
    return True, "Wake models are on this Mac."


def build_wake_engine(cfg, on_fallback=None) -> WakeEngine:
    """Factory used by the orchestrator; never raises — falls back to manual.

    Any failure (missing package, missing model file, corrupt model) degrades
    to manual wake. `on_fallback(reason)` is invoked so the reason reaches the
    log and the Setup panel instead of a crash.
    """
    mode = getattr(cfg.wake, "mode", "manual")
    if mode != "openwakeword":
        return ManualTrigger()

    models = [str(m) for m in (cfg.wake.models or [])]
    templates = [m for m in models if m.endswith(".npz")]
    # A trained template (.npz from the Wake Phrase panel) always wins — it is
    # the user's own voice, calibrated on this machine. openWakeWord cannot
    # read one, so it never sees the list while a template is configured.
    for template in templates:
        try:
            return TemplateWakeEngine(
                template,
                refractory_seconds=getattr(cfg.wake, "refractory_seconds", 2.5))
        except Exception as exc:
            if on_fallback:
                on_fallback(f"trained phrase model unreadable: {exc}")
    pretrained = [m for m in models if not m.endswith(".npz")]
    try:
        ready, detail = wake_models_ready(cfg, pretrained)
        if not ready:
            raise RuntimeError(detail)
        return OpenWakeWordEngine(
            models=pretrained,
            threshold=cfg.wake.threshold,
            refractory_seconds=cfg.wake.refractory_seconds,
        )
    except Exception as exc:
        if on_fallback:
            on_fallback(f"always-listening unavailable, using manual wake: {exc}")
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
