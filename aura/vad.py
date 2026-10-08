"""Utterance segmentation: turn a raw frame stream into complete phrases.

A deliberately small energy gate with pre-roll and hangover — no model, no
latency, ~zero CPU. It runs *after* the wake word fires, so there is no need
for a heavyweight VAD here; Silero can slot in later behind the same interface
if noisy environments demand it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator

try:
    import numpy as np

    HAS_NUMPY = True
except Exception:  # pragma: no cover
    HAS_NUMPY = False

from .audio import FRAME_SAMPLES, SAMPLE_RATE, AudioFrame

#: One frame's worth of time. Every duration below is counted in whole frames,
#: so this is the single place that decides how long a frame is.
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE


class EnergyVAD:
    """Collect frames until `end_silence` of quiet follows real speech.

    Contract with the caller: `feed()` reports "end"/"timeout" and *then*
    `pcm_frames()` hands over the utterance. The buffer therefore stays
    readable after an "end" and is only cleared when the next utterance
    starts — clearing it inside `feed()` (the old behaviour) gave every
    caller an empty list, which silently broke both voice commands and the
    Wake Phrase Studio.
    """

    #: How much pre-roll `prime()` keeps, and how far back it reaches before
    #: the first frame of speech so a soft opening consonant isn't clipped.
    PRIME_MAX_SECONDS = 2.0
    PRIME_BACKOFF_FRAMES = 2

    def __init__(
        self,
        rms_threshold: float = 320.0,   # int16 RMS; conservative — better to
        end_silence_seconds: float = 0.7,  # capture a breath than clip a word
        max_seconds: float = 12.0,
        preroll_seconds: float = 0.4,
    ) -> None:
        self.threshold = rms_threshold
        self.end_silence = end_silence_seconds
        self.max_seconds = max_seconds
        self.preroll_seconds = preroll_seconds

        self._preroll: deque[AudioFrame] = deque(
            maxlen=max(1, round(preroll_seconds / FRAME_SECONDS)))
        self._active = False
        self._finished = False   # an utterance was reported; await a new one
        self._collected: list[AudioFrame] = []
        self._silence_run = 0.0
        self._active_seconds = 0.0
        # Set by prime(): until the user actually starts speaking after a wake,
        # wait longer for them to begin than we wait for them to finish.
        self._grace_silence: float | None = None
        self._speech_after_prime = False

    def reset(self) -> None:
        self._active = False
        self._finished = False
        self._collected.clear()
        self._silence_run = 0.0
        self._active_seconds = 0.0
        self._preroll.clear()
        self._grace_silence = None
        self._speech_after_prime = False

    @staticmethod
    def _rms(frame: AudioFrame) -> float:
        if HAS_NUMPY and isinstance(frame.pcm, np.ndarray):
            # Device reconfiguration can produce a zero-sample callback. NumPy
            # warns and returns NaN for its mean, which then poisons VAD state.
            if frame.pcm.size == 0:
                return 0.0
            return float(np.sqrt(np.mean(frame.pcm.astype("float32") ** 2)))
        return 0.0

    def _end_silence(self) -> float:
        """Trailing silence needed to close the utterance, right now."""
        grace = self._grace_silence
        if grace is not None and not self._speech_after_prime:
            return grace
        return self.end_silence

    def prime(self, frames, grace_silence: float | None = None) -> None:
        """Begin an utterance from audio captured *before* the trigger fired.

        A wake detector can only fire once the phrase is already in the past,
        so by the time recording starts the phrase itself is gone. Seeding the
        buffer with the pre-roll keeps it — the transcript then really does
        begin with the wake phrase, which is exactly what the always-on phrase
        gate checks. Without this, every always-listening session was
        transcribed as the command alone and then rejected by that gate.

        Leading quiet frames are dropped so the transcript starts at speech
        rather than a second of room tone, and `grace_silence` gives the user
        longer to *start* the command than to finish it.
        """
        self.reset()
        kept = self._trim_leading_silence(list(frames))
        self._grace_silence = grace_silence
        if not kept:
            return                      # nothing but room tone — start fresh
        self._collected = kept
        self._active = True
        self._active_seconds = FRAME_SECONDS * len(kept)

    def _trim_leading_silence(self, frames: list) -> list:
        if not frames:
            return []
        first = next((i for i, frame in enumerate(frames)
                      if self._rms(frame) >= self.threshold), None)
        if first is None:
            return []
        start = max(0, first - self.PRIME_BACKOFF_FRAMES)
        limit = max(1, round(self.PRIME_MAX_SECONDS / FRAME_SECONDS))
        return frames[start:][-limit:]

    def feed(self, frame: AudioFrame) -> str:
        """Consume one frame; return "" | "start" | "end" | "timeout"."""
        if self._finished:
            # The previous utterance has been reported (and usually collected
            # via pcm_frames()); start a fresh one now.
            self.reset()
        if not self._active:
            self._preroll.append(frame)
            if self._rms(frame) >= self.threshold:
                self._active = True
                self._collected = list(self._preroll)
                self._preroll.clear()
                self._active_seconds = FRAME_SECONDS
                self._speech_after_prime = True
                return "start"
            return ""

        self._collected.append(frame)
        self._active_seconds += FRAME_SECONDS
        if self._rms(frame) < self.threshold:
            self._silence_run += FRAME_SECONDS
        else:
            self._silence_run = 0.0
            self._speech_after_prime = True
        if self._silence_run >= self._end_silence():
            # Report the utterance, but keep it readable for pcm_frames().
            self._finished = True
            self._active = False
            return "end"
        if self._active_seconds >= self.max_seconds:
            self._finished = True
            self._active = False
            return "timeout"
        return ""

    def pcm_frames(self) -> list[AudioFrame]:
        return list(self._collected)


async def utterances(frames: AsyncIterator[AudioFrame], vad: EnergyVAD) -> AsyncIterator[list[AudioFrame]]:
    """Convenience adapter: async frame stream → complete utterances."""
    async for frame in frames:
        state = vad.feed(frame)
        if state in ("end", "timeout"):
            yield vad.pcm_frames()
