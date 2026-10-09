"""Wake-phrase training — entirely on-device, entirely in the app.

The user types a phrase, says it a handful of times, and taps Train. No
terminal, no scripts, no cloud. This module is the machinery behind that
flow:

  1. Capture   — guided samples through Aura's own mic + VAD, each checked
                 for level, clipping, and length before it counts.
  2. Embed     — log-band spectral features (25 ms frames, 10 ms hop, 40
                 log-spaced bands, mean+max pooled). Deterministic, numpy-
                 only, and the exact same function used at detection time —
                 so what you train on is literally what it listens for.
  3. Separate  — a linear scorer fitted to maximize the margin between the
                 user's samples and deterministic synthesized noise, sweeps,
                 hum and speech-like distractors (no slow subprocesses).
  4. Ship      — the template + scorer threshold export as one small .npz
                 (<100 KB), loaded by TemplateWakeEngine. Nothing leaves
                 the machine; there is nothing to send.

If numpy is missing the trainer reports it honestly and the Setup panel
offers to install components — the UI never falls back to terminal steps.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np

    HAS_NUMPY = True
except Exception:  # pragma: no cover - numpy is in the [mac] extra
    np = None  # type: ignore[assignment]
    HAS_NUMPY = False

log = logging.getLogger(__name__)


class TrainingUnavailable(RuntimeError):
    pass


SR = 16_000
WIN = 400          # 25 ms
HOP = 160          # 10 ms
BANDS = 40
FMIN, FMAX = 80.0, 7600.0
EMBED_DIM = BANDS * 2  # mean ∥ max pooling
WINDOW_S = 1.0         # the one window size used at training AND detection —
# samples are normalized to it so "same phrase, different moment" scores the same

#: Detection geometry — shared by TemplateWakeEngine and the calibration below.
#: The live detector scores a trailing WINDOW_S window every DETECT_STEP_S,
#: plus a second window DETECT_OFFSET_S behind it (so a phrase longer than the
#: window still gets a well-aligned look). The offset is an exact multiple of
#: the step, so "every window on the DETECT_STEP_S grid" is the union of both.
DETECT_STEP_S = 0.128
DETECT_OFFSET_S = 0.384

#: Bump when the score space a saved threshold lives in changes. Templates
#: written by older Aura builds (no key ⇒ version 1) were calibrated on raw
#: capture embeddings the live detector never produces; they load and run, but
#: the app recommends retraining them.
TEMPLATE_CALIBRATION = 2


def _require_numpy() -> None:
    if not HAS_NUMPY:
        raise TrainingUnavailable(
            "numpy is not installed — install components from the Setup panel.")


# --------------------------------------------------------------------------- #
# Features — shared by training and detection                                  #
# --------------------------------------------------------------------------- #


def _band_edges() -> np.ndarray:  # type: ignore[name-defined]
    freqs = np.logspace(math.log10(FMIN), math.log10(FMAX), BANDS + 1)
    return np.clip(freqs / (SR / 2) * (WIN // 2), 0, WIN // 2 - 1).astype(int)


_EDGES = None


def spectral_embedding(pcm, sr: int = SR) -> np.ndarray:  # type: ignore[name-defined]
    """(EMBED_DIM,) vector: 40 log-band energies, mean- and max-pooled over time.

    Amplitude-normalized so distance to the mic doesn't change the identity
    of the phrase — only its shape matters.
    """
    _require_numpy()
    global _EDGES
    if _EDGES is None:
        _EDGES = _band_edges()

    x = np.asarray(pcm, dtype=np.float32)
    peak = np.max(np.abs(x)) or 1.0
    x = x / peak
    if len(x) < WIN:
        x = np.pad(x, (0, WIN - len(x)))

    n = 1 + (len(x) - WIN) // HOP
    n = max(n, 1)
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx] * np.hanning(WIN)[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2  # (n, WIN//2+1)

    edges = _EDGES
    bands = np.empty((n, BANDS), dtype=np.float32)
    for b in range(BANDS):
        lo, hi = edges[b], max(edges[b] + 1, edges[b + 1])
        bands[:, b] = spec[:, lo:hi].mean(axis=1)
    bands = np.log1p(1000.0 * bands)

    return np.concatenate([bands.mean(axis=0), bands.max(axis=0)]).astype(np.float32)


def normalize_length(x: np.ndarray, sr: int = SR) -> np.ndarray:  # type: ignore[name-defined]
    """Center every clip in the same WINDOW_S frame — training and detection
    see identical shapes, so position-in-window never skews the score."""
    _require_numpy()
    n = int(WINDOW_S * sr)
    if len(x) >= n:
        start = (len(x) - n) // 2
        return x[start:start + n]
    pad = n - len(x)
    left = pad // 2
    return np.pad(x, (left, pad - left))


def cosine(a: np.ndarray, b: np.ndarray) -> float:  # type: ignore[name-defined]
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def detector_windows(x: np.ndarray, sr: int = SR) -> list[np.ndarray]:  # type: ignore[name-defined]
    """Every WINDOW_S slice of `x` the live detector would actually score.

    TemplateWakeEngine scores on a DETECT_STEP_S cadence, so this is the clip's
    windows on that same grid (the offset window is a whole number of steps
    back, hence already part of it). Calibrating the threshold on *these*
    slices is what makes the stored number live in the score space detection
    produces — the earlier build embedded whole captures instead (preroll +
    phrase + trailing silence ≈ 2 s), whose mean-pooled spectrum is diluted by
    silence the 1 s live window never contains. The result was a threshold no
    real utterance could reach: the trained phrase simply never fired.

    Clips shorter than one window are centered/padded exactly like
    `normalize_length` — the single view the detector would get of them.
    """
    _require_numpy()
    win = int(WINDOW_S * sr)
    step = max(1, round(DETECT_STEP_S * sr))
    x = np.asarray(x)
    if len(x) <= win:
        return [normalize_length(x, sr)]
    return [x[start:start + win] for start in range(0, len(x) - win + 1, step)]


# --------------------------------------------------------------------------- #
# Synthesized negatives — what the phrase must be distinguished FROM           #
# --------------------------------------------------------------------------- #


def synth_negatives(count: int = 8, sr: int = SR) -> list[np.ndarray]:  # type: ignore[name-defined]
    """Background that isn't the phrase: shaped noise, chirps, hum, speech-ish
    warbles. Deterministic seed so training is reproducible."""
    _require_numpy()
    rng = np.random.default_rng(7301)
    out: list[np.ndarray] = []
    dur = 1.2
    t = np.arange(int(dur * sr)) / sr
    for i in range(count):
        kind = i % 4
        if kind == 0:      # pink-ish noise
            x = rng.standard_normal(len(t)) * 0.4
            x = np.convolve(x, np.ones(16) / 16, mode="same")
        elif kind == 1:    # steady tones (HVAC, hum)
            f = float(rng.uniform(120, 400))
            x = 0.3 * np.sin(2 * np.pi * f * t) + 0.1 * rng.standard_normal(len(t))
        elif kind == 2:    # slow chirp (sirens, music)
            f0, f1 = float(rng.uniform(200, 500)), float(rng.uniform(900, 2500))
            x = 0.35 * np.sin(2 * np.pi * (f0 + (f1 - f0) * t / dur) * t)
        else:              # speech-like warble
            f = 180 + 60 * np.sin(2 * np.pi * 3 * t)
            x = 0.4 * np.sin(2 * np.pi * np.cumsum(f) / sr)
            x *= (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 4 * t)))  # syllabic gate
        out.append(x.astype(np.float32))
    return out


def synth_speech_negatives(count: int = 4) -> list[np.ndarray]:  # type: ignore[name-defined]
    """On a Mac: real spoken distractors via `say`, rendered locally.
    Returns [] off-Mac or if `say` is missing — synth_negatives still apply."""
    _require_numpy()
    import sys as _sys
    import wave as _wave

    if _sys.platform != "darwin":
        return []
    import shutil

    if not shutil.which("say"):
        return []
    out: list[np.ndarray] = []
    lines = [
        "The quick brown fox jumps over the lazy dog near the riverbank today.",
        "Could you tell me what the weather looks like for tomorrow afternoon?",
        "Please remember to pick up groceries on your way home this evening.",
    ]
    with tempfile_dir() as tmp:
        for i in range(min(count, len(lines))):
            path = Path(tmp) / f"neg_{i}.wav"
            try:
                subprocess.run(
                    ["say", "-o", str(path), "--data-format=LEI16@16000", lines[i]],
                    check=True, timeout=30, capture_output=True)
                with _wave.open(str(path), "rb") as wf:
                    pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                out.append(pcm.astype(np.float32))
            except Exception as exc:  # one bad render never sinks the batch
                log.debug("negative render %d failed: %s", i, exc)
    return out


class tempfile_dir:
    def __enter__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


# --------------------------------------------------------------------------- #
# Capture quality — the guided part of guided recording                        #
# --------------------------------------------------------------------------- #


@dataclass
class SampleQuality:
    ok: bool
    message: str
    seconds: float
    rms: float
    clipped: bool


def judge_sample(pcm_bytes: bytes, sr: int = SR) -> SampleQuality:
    _require_numpy()
    x = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    seconds = len(x) / sr
    rms = float(np.sqrt(np.mean(x**2))) if len(x) else 0.0
    clipped = bool(np.mean(np.abs(x) > 32000) > 0.01)
    if seconds < 0.35:
        return SampleQuality(False, "That was too short — say the whole phrase.", seconds, rms, clipped)
    if seconds > 4.0:
        return SampleQuality(False, "A little long — just the phrase itself.", seconds, rms, clipped)
    if clipped:
        return SampleQuality(False, "That was too loud — try a touch farther away.", seconds, rms, clipped)
    if rms < 120:
        return SampleQuality(False, "Too quiet — speak up a little.", seconds, rms, clipped)
    return SampleQuality(True, "Nice.", seconds, rms, clipped)


# --------------------------------------------------------------------------- #
# Training — fit the scorer, calibrate the threshold                           #
# --------------------------------------------------------------------------- #


@dataclass
class TrainedWake:
    template: np.ndarray   # type: ignore[name-defined]  # (EMBED_DIM,) phrase centroid
    threshold: float       # calibrated on the user's own data (cosine space)
    margin: float          # positive-mean minus negative-mean separation
    positives: int
    negatives: int


def _window_scores(clips: list, template: np.ndarray) -> np.ndarray:  # type: ignore[name-defined]
    """Best detector-visible score per clip: max over `detector_windows`.

    The live engine fires when ANY scored window clears the threshold, so the
    per-clip maximum is the honest "what the detector would see" statistic —
    for positives (the phrase must fire) and negatives (noise must not) alike.
    """
    scores = []
    for clip in clips:
        windows = detector_windows(clip)
        embeddings = np.stack([spectral_embedding(w) for w in windows])
        embeddings = embeddings / np.maximum(
            np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9)
        scores.append(float(np.max(embeddings @ template)))
    return np.asarray(scores, dtype=np.float64)


#: Where the threshold sits between the strongest negative and the weakest
#: positive, 0 = at the negative, 1 = at the positive. Below the midpoint on
#: purpose: in always-on mode a spoken-phrase gate (whisper transcript must
#: begin with the phrase) rejects acoustic false accepts, so the expensive
#: failure is a phrase that doesn't fire — the product reading as dead — not
#: one extra rejected transcription.
THRESHOLD_POSITION = 0.40


def train_wake(positives: list, negatives: list | None = None) -> TrainedWake:
    """Calibrate a template detector on the user's own voice.

    Positives: samples of the phrase, exactly as the Wake Phrase Studio
    captured them (VAD clip: preroll + phrase + trailing silence). Negatives:
    synthesized backgrounds (always) plus any user-supplied clips.

    Two spaces, kept apart on purpose:

    * The **template** is the mean of each sample's centered one-second
      window — the canonical view of the phrase, position-independent.
    * The **threshold** is calibrated on the scores the *live detector* would
      produce: every window on its scoring grid, best per clip (see
      `detector_windows`). A threshold calibrated anywhere else is a number
      the microphone stream can never reach — which is exactly how the
      previous build shipped trained phrases that never fired.
    """
    _require_numpy()
    if len(positives) < 3:
        raise TrainingUnavailable("Need at least three good samples of your phrase.")

    pos = np.stack([spectral_embedding(normalize_length(p)) for p in positives])
    pos = pos / np.maximum(np.linalg.norm(pos, axis=1, keepdims=True), 1e-9)
    template = pos.mean(axis=0)
    tnorm = float(np.linalg.norm(template))
    if tnorm == 0:
        raise TrainingUnavailable("Those samples were all silence — try again somewhere quieter.")
    template = template / tnorm

    negs = list(negatives or [])
    negs += synth_negatives()
    # Do not render extra phrases with the macOS `say` subprocess here. That
    # used to put three serial, potentially 30-second subprocesses behind the
    # “Train phrase” button (and `say` may emit an AIFF container that Python's
    # wave reader rejects anyway). User-supplied negatives remain supported,
    # while deterministic spectral negatives keep this interactive fit
    # snappy enough to feel instant behind the "Train phrase" button.

    pos_scores = _window_scores(list(positives), template)
    neg_scores = _window_scores(negs, template)
    threshold = float(neg_scores.max()
                      + THRESHOLD_POSITION * (pos_scores.min() - neg_scores.max()))
    threshold = min(threshold, float(pos_scores.min()) - 1e-3)  # never reject a provided sample
    margin = float(pos_scores.mean() - neg_scores.mean())

    return TrainedWake(template=template.astype(np.float32),
                       threshold=threshold, margin=margin,
                       positives=len(positives), negatives=len(negs))


def save_template(trained: TrainedWake, phrase: str, path: Path) -> Path:
    _require_numpy()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        template=trained.template,
        threshold=np.float64(trained.threshold),
        phrase=phrase,
        created=np.float64(time.time()),
        calibration=np.int64(TEMPLATE_CALIBRATION),
    )
    return path


def slugify(phrase: str) -> str:
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    return "-".join(words[:5]) or "wake-phrase"
