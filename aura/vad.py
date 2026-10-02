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

from .audio import AudioFrame


class EnergyVAD:
    """Collect frames until `end_silence` of quiet follows real speech.

    Contract with the caller: `feed()` reports "end"/"timeout" and *then*
    `pcm_frames()` hands over the utterance. The buffer therefore stays
    readable after an "end" and is only cleared when the next utterance
    starts — clearing it inside `feed()` (the old behaviour) gave every
    caller an empty list, which silently broke both voice commands and the
    Wake Phrase Studio.
    """

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

        self._preroll: deque[AudioFrame] = deque(maxlen=64)
        self._active = False
        self._finished = False   # an utterance was reported; await a new one
        self._collected: list[AudioFrame] = []
        self._silence_run = 0.0
        self._active_seconds = 0.0

    def reset(self) -> None:
        self._active = False
        self._finished = False
        self._collected.clear()
        self._silence_run = 0.0
        self._active_seconds = 0.0
        self._preroll.clear()

    @staticmethod
    def _rms(frame: AudioFrame) -> float:
        if HAS_NUMPY and isinstance(frame.pcm, np.ndarray):
            # Device reconfiguration can produce a zero-sample callback. NumPy
            # warns and returns NaN for its mean, which then poisons VAD state.
            if frame.pcm.size == 0:
                return 0.0
            return float(np.sqrt(np.mean(frame.pcm.astype("float32") ** 2)))
        return 0.0

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
                self._active_seconds = 0.032
                return "start"
            return ""

        self._collected.append(frame)
        self._active_seconds += 0.032
        if self._rms(frame) < self.threshold:
            self._silence_run += 0.032
        else:
            self._silence_run = 0.0
        if self._silence_run >= self.end_silence:
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
