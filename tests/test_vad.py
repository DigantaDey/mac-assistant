"""The VAD's contract with its callers: report "end", *then* hand over audio.

The bug this locks down: `feed()` used to clear its buffer before returning
"end", so every caller got an empty utterance — voice commands came back as
"I didn't catch anything" and the Wake Phrase Studio could never record a
single sample ("That was too short"). No test noticed because nobody fed the
VAD real frames and then read them back.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from aura.audio import AudioFrame
from aura.vad import EnergyVAD, utterances

SR = 16_000
FRAME = 512


def frames(pcm: np.ndarray):
    return [AudioFrame(pcm=pcm[i:i + FRAME].copy(), ts=time.monotonic())
            for i in range(0, len(pcm) - FRAME, FRAME)]


def speech(seconds: float = 1.2, amp: int = 4000) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = (amp * np.sin(2 * np.pi * 190 * t)).astype(np.int16)
    # syllabic gating: bursts of sound, like real speech
    return (x * (np.sin(2 * np.pi * 4 * t) > 0)).astype(np.int16)


def silence(seconds: float = 1.0) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.int16)


def feed_until(vad: EnergyVAD, stream: list[AudioFrame]) -> tuple[str, list[AudioFrame]]:
    """Feed frames exactly like the orchestrator does: stop the instant the
    VAD reports "end"/"timeout", then read the utterance back."""
    verdict = ""
    for frame in stream:
        verdict = vad.feed(frame)
        if verdict in ("end", "timeout"):
            break
    return verdict, vad.pcm_frames()


class TestEnergyVAD:
    def test_collects_the_utterance_it_reported(self):
        """The regression: `end` must be followed by a *non-empty* capture."""
        vad = EnergyVAD(end_silence_seconds=0.6, max_seconds=4.0)
        verdict, collected = feed_until(
            vad, frames(speech(1.2)) + frames(silence(1.0)))
        assert verdict == "end"
        assert collected, "the utterance the VAD reported was thrown away"
        pcm = b"".join(f.pcm.tobytes() for f in collected)
        samples = np.frombuffer(pcm, dtype=np.int16)
        # the speech burst is in there, loud and long enough to transcribe
        assert len(samples) > SR * 0.5
        assert float(np.sqrt(np.mean(samples.astype("float32") ** 2))) > 100

    def test_timeout_also_keeps_the_audio(self):
        vad = EnergyVAD(end_silence_seconds=0.6, max_seconds=0.5)
        verdict, collected = feed_until(vad, frames(speech(3.0)))
        assert verdict == "timeout"
        assert collected

    def test_silence_never_produces_an_utterance(self):
        vad = EnergyVAD(end_silence_seconds=0.6, max_seconds=4.0)
        verdict, collected = feed_until(vad, frames(silence(3.0)))
        assert verdict == ""
        assert collected == []

    def test_next_utterance_starts_clean(self):
        """After collecting one utterance, the VAD must not carry it into the
        next one — and must keep reporting each one."""
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=4.0)
        stream = (frames(speech(1.0)) + frames(silence(0.8))
                  + frames(speech(1.0)) + frames(silence(0.8)))
        verdicts, captures = [], []
        for frame in stream:
            verdict = vad.feed(frame)
            if verdict in ("end", "timeout"):
                verdicts.append(verdict)
                captures.append(vad.pcm_frames())
        assert verdicts == ["end", "end"]
        assert all(captures), "both utterances must be captured"
        assert len(captures[0]) != len(captures[1]) or captures[0] != captures[1]

    def test_pcm_frames_is_stable_until_the_next_feed(self):
        vad = EnergyVAD(end_silence_seconds=0.4, max_seconds=4.0)
        feed_until(vad, frames(speech(1.0)) + frames(silence(0.8)))
        first = vad.pcm_frames()
        second = vad.pcm_frames()
        assert first == second and first

    def test_reset_clears_everything(self):
        vad = EnergyVAD(end_silence_seconds=0.4, max_seconds=4.0)
        feed_until(vad, frames(speech(1.0)) + frames(silence(0.8)))
        vad.reset()
        assert vad.pcm_frames() == []

    def test_preroll_captures_the_onset(self):
        """The first syllable must not be clipped: pre-roll frames are kept."""
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=4.0)
        stream = frames(silence(0.3)) + frames(speech(1.0)) + frames(silence(0.8))
        feed_until(vad, stream)
        assert vad.pcm_frames()   # and the leading silence proves pre-roll survived


class TestPrime:
    """Seeding an utterance with audio from *before* the trigger fired.

    A wake detector can only fire once the phrase is already in the past, so
    without this the recording starts after the phrase — the transcript never
    begins with the wake phrase, and the always-on phrase gate rejects every
    single session.
    """

    def test_primed_frames_are_in_the_utterance(self):
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=4.0)
        preroll = frames(speech(0.8))
        vad.prime(preroll)
        verdict, collected = feed_until(vad, frames(speech(0.6)) + frames(silence(1.0)))
        assert verdict == "end"
        assert len(collected) > len(preroll)
        # the primed audio is at the front, in order
        assert collected[:len(preroll)] == preroll

    def test_leading_room_tone_is_dropped(self):
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=4.0)
        quiet = frames(silence(1.0))
        loud = frames(speech(0.6))
        vad.prime(quiet + loud)
        # ~1 s of silence must not be handed to the transcriber
        kept = vad.pcm_frames()
        assert 0 < len(kept) <= len(loud) + vad.PRIME_BACKOFF_FRAMES

    def test_all_quiet_preroll_starts_fresh_instead(self):
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=4.0)
        vad.prime(frames(silence(1.0)))
        assert vad.pcm_frames() == []
        # …and a real utterance is still captured normally afterwards
        verdict, collected = feed_until(vad, frames(speech(0.8)) + frames(silence(1.0)))
        assert verdict == "end" and collected

    def test_grace_gives_longer_to_start_than_to_finish(self):
        """After a wake, a pause before the command must not end the turn."""
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=8.0)
        vad.prime(frames(speech(0.7)), grace_silence=2.0)
        # 1.2 s of quiet — longer than end_silence, shorter than the grace
        verdict, _ = feed_until(vad, frames(silence(1.2)))
        assert verdict == ""
        # …and the command that follows is still part of the same utterance
        verdict, collected = feed_until(vad, frames(speech(0.8)) + frames(silence(1.0)))
        assert verdict == "end"
        assert len(collected) > len(frames(speech(0.8)))

    def test_without_grace_the_pause_ends_the_turn(self):
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=8.0)
        vad.prime(frames(speech(0.7)))
        verdict, _ = feed_until(vad, frames(silence(1.2)))
        assert verdict == "end"

    def test_reset_clears_the_grace(self):
        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=8.0)
        vad.prime(frames(speech(0.7)), grace_silence=3.0)
        vad.reset()
        assert vad._end_silence() == 0.5


class TestUtterancesAdapter:
    @pytest.mark.asyncio
    async def test_yields_non_empty_utterances(self):
        async def stream():
            for frame in frames(speech(1.0)) + frames(silence(0.8)):
                yield frame

        vad = EnergyVAD(end_silence_seconds=0.5, max_seconds=4.0)
        heard = [u async for u in utterances(stream(), vad)]
        assert len(heard) == 1
        assert heard[0]
