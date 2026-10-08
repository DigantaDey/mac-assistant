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
from collections import deque
from pathlib import Path

try:
    import numpy as np

    HAS_NUMPY = True
except Exception:  # pragma: no cover
    HAS_NUMPY = False

#: How long a score stays visible in `stats()["level"]` — the app's listening
#: meter shows "what Aura hears right now", so the peak decays on this window.
LEVEL_WINDOW_SECONDS = 2.5


class WakeEngine:
    """Interface. `feed(frame)` → True exactly once per accepted wake."""

    def feed(self, frame) -> bool:
        return False

    def reset(self) -> None: ...

    def stats(self) -> dict:
        """Live listening telemetry for wake_status(): {"level", "threshold",
        "fires"}. `level` is the best detector score of the last couple of
        seconds — what makes "I said it and nothing happened" answerable."""
        return {"level": None, "threshold": None, "fires": None}


class _ScoreTracker:
    """Rolling peak of recent detector scores, shared by the wake engines."""

    def __init__(self) -> None:
        self._recent: deque[tuple[float, float]] = deque(maxlen=64)
        self.fires = 0

    def track(self, score: float, now: float) -> None:
        self._recent.append((now, score))
        cutoff = now - LEVEL_WINDOW_SECONDS
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

    def peak(self, now: float) -> float | None:
        cutoff = now - LEVEL_WINDOW_SECONDS
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()
        if not self._recent:
            return None
        return max(score for _, score in self._recent)


def _round_level(level: float | None) -> float | None:
    return round(level, 4) if level is not None else None


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
        self._tracker = _ScoreTracker()

    def feed(self, frame) -> bool:
        if not HAS_NUMPY:
            return False
        self._buf.extend(frame.pcm.tobytes())
        fired = False
        # openWakeWord wants 80 ms chunks; keep the remainder for the next frame.
        while len(self._buf) >= self.FRAME_80MS * 2:
            chunk = np.frombuffer(bytes(self._buf[: self.FRAME_80MS * 2]), dtype=np.int16)
            del self._buf[: self.FRAME_80MS * 2]
            scores = self._model.predict(chunk)
            best = max(scores.values()) if scores else 0.0
            now = time.monotonic()
            self._tracker.track(float(best), now)
            if best >= self.threshold and now - self._last_fire >= self.refractory:
                self._last_fire = now
                self._tracker.fires += 1
                self._model.reset()
                fired = True
                break
        return fired

    def stats(self) -> dict:
        return {"level": _round_level(self._tracker.peak(time.monotonic())),
                "threshold": float(self.threshold),
                "fires": self._tracker.fires}

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
    Wake Phrase Studio.

    Scores with the same embedding the trainer calibrated on, over the same
    windows the calibration simulated (`wakeword_trainer.detector_windows`):
    a trailing one-second window every ~128 ms, plus one offset 384 ms behind
    it so phrases longer than the window still get an aligned look. Fires once
    per accepted wake, with a refractory period. The rolling buffer is trimmed
    to what those windows need — an always-on listener runs for weeks, and an
    untrimmed buffer leaks ~2 MB per minute."""

    #: Bytes of new audio between scoring passes (~128 ms at 16 kHz int16).
    STEP_BYTES = 2048 * 2
    #: The offset window sits exactly three steps behind the trailing one, so
    #: both live on the STEP_BYTES grid the trainer calibrates against.
    OFFSET_STEPS = 3

    def __init__(self, model_path: str, threshold_margin: float = 0.0,
                 refractory_seconds: float = 2.5) -> None:
        try:
            import numpy as np_
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"numpy unavailable: {exc}") from exc
        self._np = np_
        from .wakeword_trainer import SR, WINDOW_S, spectral_embedding

        self._embed = spectral_embedding
        self._window_bytes = int(WINDOW_S * SR) * 2  # int16 → bytes
        self._offset_bytes = self.OFFSET_STEPS * self.STEP_BYTES
        self._max_bytes = self._window_bytes + self._offset_bytes
        try:
            data = np_.load(model_path, allow_pickle=False)
        except Exception as exc:
            raise RuntimeError(f"could not load wake template {model_path}: {exc}") from exc
        self.template = data["template"]
        self.threshold = float(data["threshold"]) + threshold_margin
        self.phrase = str(data.get("phrase", ""))
        #: Which score space the stored threshold was calibrated in. Version 1
        #: templates (no key) predate the detector-window calibration; they
        #: still run, but their thresholds are systematically unreachable and
        #: the app recommends retraining (see orchestrator._build_wake).
        self.calibration = int(data.get("calibration", 1) or 1)
        self.refractory = max(0.0, float(refractory_seconds))
        self._buf = bytearray()
        self._unscored_bytes = 0
        self._last_fire = 0.0
        self._tracker = _ScoreTracker()

    def feed(self, frame) -> bool:
        pcm = frame.pcm
        chunk = pcm.tobytes() if hasattr(pcm, "tobytes") else bytes(pcm)
        self._buf.extend(chunk)
        if len(self._buf) > self._max_bytes:
            del self._buf[: len(self._buf) - self._max_bytes]
        self._unscored_bytes += len(chunk)
        if self._unscored_bytes < self.STEP_BYTES or len(self._buf) < self._window_bytes:
            return False
        self._unscored_bytes = 0

        best = self._score_windows()
        now = time.monotonic()
        # -1 means "digital silence, nothing scoreable" — telemetry shows 0,
        # never a negative bar; the fire check is unchanged either way.
        self._tracker.track(max(best, 0.0), now)
        if best < self.threshold:
            return False
        if now - self._last_fire < self.refractory:
            return False
        self._last_fire = now
        self._tracker.fires += 1
        self._buf.clear()
        self._unscored_bytes = 0
        return True

    def _score_windows(self) -> float:
        """Best cosine over the trailing window and the offset window."""
        buf = self._buf
        best = -1.0
        spans = [(len(buf) - self._window_bytes, len(buf))]
        if len(buf) >= self._max_bytes:
            end = len(buf) - self._offset_bytes
            spans.append((end - self._window_bytes, end))
        for start, end in spans:
            if start < 0:
                continue
            window = self._np.frombuffer(bytes(buf[start:end]), dtype=self._np.int16)
            emb = self._embed(window)
            denom = float(self._np.linalg.norm(emb) * self._np.linalg.norm(self.template))
            if denom == 0:
                continue
            score = float(self._np.dot(emb, self.template) / denom)
            best = max(best, score)
        return best

    def stats(self) -> dict:
        return {"level": _round_level(self._tracker.peak(time.monotonic())),
                "threshold": round(float(self.threshold), 4),
                "fires": self._tracker.fires}

    def reset(self) -> None:
        self._buf.clear()
        self._unscored_bytes = 0


def load_template_meta(model_path: str) -> dict:
    """Phrase + threshold + calibration of a trained template, for the UI."""
    try:
        import numpy as np_

        data = np_.load(model_path, allow_pickle=False)
        return {"phrase": str(data.get("phrase", "")),
                "threshold": float(data["threshold"]),
                "calibration": int(data.get("calibration", 1) or 1)}
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


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    """One substitution, insertion or deletion apart — nothing more."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _word_close(a: str, b: str) -> bool:
    """Exact, or one transcription slip for words with some length to them.

    Whisper regularly renders a spoken "hey aura" as "hey auraa" or "hey
    aur" — and under the old exact-match gate every such slip was a *silent
    rejection of a real wake*: the detector had fired on the user's own
    voice, and Aura still did nothing. A near-miss is only accepted when one
    token is at least 4 characters and a single edit apart — the distance
    that separates a slip ("auraa") from a different word ("euro", "aria",
    two edits away). Short words must match exactly.
    """
    if a == b:
        return True
    if max(len(a), len(b)) < 4:
        return False
    return _edit_distance_at_most_one(a, b)


def _phrase_match(said: list[str], wanted: list[str]) -> int:
    """How many leading tokens of `said` are the phrase `wanted` — 0 if absent.

    The phrase must lead the transcript (that is the gate), with one
    concession: a single leading filler token ("um hey aura …") may precede
    it, because the recording starts at a detector fire, not at a sentence
    boundary.
    """
    if not wanted:
        return 0
    for skip in (0, 1):
        head = said[skip:skip + len(wanted)]
        if len(head) == len(wanted) and all(_word_close(a, b)
                                            for a, b in zip(head, wanted)):
            return skip + len(wanted)
    return 0


def phrase_gate(transcript: str, phrase: str) -> bool:
    """In always-on mode the transcript must *start with* the user's phrase.

    Deliberately lenient about punctuation and casing ("Hey, Aura!" == "hey
    aura"), and tolerant of one-character transcription slips per word —
    this gate runs *after* the acoustic detector fired on the user's own
    voice, so its job is to catch distant speech (TV, other phrases), not to
    punish Whisper's spelling. "hey euro" and "high tower" stay rejected;
    "hey auraa" is Whisper's spelling of the user's "hey aura".
    """
    if not phrase.strip():
        return True
    said = _normalize(transcript).split()
    wanted = _normalize(phrase).split()
    return _phrase_match(said, wanted) > 0


def strip_phrase(transcript: str, phrase: str) -> str:
    """Remove a leading wake phrase so the planner sees only the command.

    Uses the same matcher as `phrase_gate`: whatever the gate accepted
    (including a near-miss rendering or one filler word) is exactly what
    gets stripped — a gate that accepts "hey ora" but a stripper that keeps
    it would send the phrase itself to the planner as the command.
    """
    if not phrase.strip():
        return transcript.strip()
    said = _normalize(transcript).split()
    wanted = _normalize(phrase).split()
    matched = _phrase_match(said, wanted)
    if matched == 0:
        return transcript.strip()
    remaining = said[matched:]
    if not remaining:
        return ""
    original_words = transcript.split()
    # Heuristic re-join: take the last N words of the original transcript so
    # capitalization/punctuation survive.
    return " ".join(original_words[-len(remaining):])
