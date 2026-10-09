"""The trained wake phrase must actually wake Aura — end to end.

This is the flow the field failure came from, reproduced without a
microphone:

  1. The Wake Phrase Studio records VAD captures — preroll + phrase + the
     0.6 s trailing silence that ends a take (≈ 1.8–2.4 s clips).
  2. `train_wake` calibrates a template + threshold on those captures.
  3. `TemplateWakeEngine` scores a live 16 kHz stream — room tone, the
     phrase spoken again (a *different* rendition), a pause, a command.

The shipped calibration embedded whole 2-second captures while the live
detector scores 1-second windows: mean-pooled over silence-diluted frames,
the stored threshold lived in a score space the microphone stream never
reached, so trained phrases simply never fired and the log stayed empty.
Every test here fails on that build and passes on the fixed one.
"""

from __future__ import annotations

import time

import pytest

np = pytest.importorskip("numpy")

from aura.audio import FRAME_SAMPLES, AudioFrame
from aura.wakeword import TemplateWakeEngine, build_wake_engine
from aura.wakeword_trainer import (
    SR,
    WINDOW_S,
    detector_windows,
    save_template,
    spectral_embedding,
    train_wake,
)

# --------------------------------------------------------------------------- #
# Synthetic "speech" — two-syllable phrase renditions with natural variation    #
# --------------------------------------------------------------------------- #


def phrase_rendition(seed: int, dur: float = 0.8, f0: float = 165.0) -> np.ndarray:
    """One spoken rendition of a two-syllable phrase. Tempo, pitch, formants,
    loudness and a vowel gap all vary per seed — like a real person repeating
    a phrase six times, and like the detection-time utterance that must NOT be
    the exact audio the template was trained on."""
    r = np.random.default_rng(seed)
    dur *= float(r.uniform(0.85, 1.25))
    f0 *= float(r.uniform(0.92, 1.10))
    t = np.arange(int(dur * SR)) / SR
    x = np.zeros_like(t)
    gap = float(r.uniform(0.02, 0.08))
    split = dur * float(r.uniform(0.44, 0.56))
    for start, end, f1, f2 in [
        (0.0, split, r.uniform(280, 360), r.uniform(850, 1050)),
        (split + gap, dur, r.uniform(480, 620), r.uniform(1300, 1700)),
    ]:
        m = (t >= start) & (t < end)
        env = np.sin(np.pi * (t[m] - start) / max(end - start, 1e-6)) ** 0.7
        vib = 1 + 0.02 * np.sin(2 * np.pi * 5.5 * t[m])
        x[m] += env * (0.55 * np.sin(2 * np.pi * f0 * vib * t[m])
                       + 0.35 * np.sin(2 * np.pi * f1 * t[m])
                       + 0.25 * np.sin(2 * np.pi * f2 * t[m])
                       + 0.04 * r.standard_normal(int(m.sum())))
    return (x * r.uniform(5000, 15000)).astype(np.int16)


def other_phrase(seed: int, dur: float = 0.9) -> np.ndarray:
    """A clearly different three-syllable phrase — the distractor."""
    r = np.random.default_rng(seed)
    t = np.arange(int(dur * SR)) / SR
    x = np.zeros_like(t)
    for k, (f1, f2) in enumerate([(900, 2400), (400, 900), (1200, 2600)]):
        start, end = k * dur / 3, (k + 1) * dur / 3
        m = (t >= start) & (t < end)
        env = np.sin(np.pi * (t[m] - start) / max(end - start, 1e-6))
        x[m] += env * (0.5 * np.sin(2 * np.pi * 110 * t[m])
                       + 0.35 * np.sin(2 * np.pi * f1 * t[m])
                       + 0.25 * np.sin(2 * np.pi * f2 * t[m]))
    return (x * r.uniform(6000, 14000)).astype(np.int16)


def room_tone(seconds: float, level: float = 80, seed: int = 0) -> np.ndarray:
    r = np.random.default_rng(seed or None)
    return (r.standard_normal(int(seconds * SR)) * level).astype(np.int16)


def reverbish(x: np.ndarray, decay: float = 0.25) -> np.ndarray:
    """Two early reflections — a room, not an anechoic chamber. Real takes
    carry wall bounce; embeddings of reverberant speech sit slightly off the
    clean-template manifold, which is exactly where a miscalibrated threshold
    shows up."""
    y = x.astype(np.float64).copy()
    d1 = int(0.05 * SR)
    y[d1:] += decay * x[:-d1]
    d2 = int(0.11 * SR)
    y[d2:] += decay * decay * x[:-d2]
    return y.astype(np.int16)


def studio_capture(seed: int) -> np.ndarray:
    """Exactly what the Wake Phrase Studio's VAD hands the trainer:
    preroll (0.25–0.45 s) + phrase + trailing silence (≥ 0.6 s — the take
    ends *because* of that silence, so it is always in the clip)."""
    r = np.random.default_rng(seed * 7919)
    preroll = room_tone(float(r.uniform(0.25, 0.45)), seed=seed + 1)
    tail = room_tone(float(r.uniform(0.60, 0.90)), seed=seed + 2)
    return np.concatenate([preroll, phrase_rendition(1000 + seed), tail]).astype(np.float32)


def frames_of(pcm: np.ndarray, chunk: int = FRAME_SAMPLES) -> list[AudioFrame]:
    now = time.time()
    return [AudioFrame(pcm=pcm[i:i + chunk].astype(np.int16, copy=False),
                       ts=now + i / SR)
            for i in range(0, len(pcm) - chunk + 1, chunk)]


def train_from_studio_captures(tmp_path, n: int = 6):
    captures = [studio_capture(i) for i in range(n)]
    trained = train_wake(captures)
    return save_template(trained, "hey aura", tmp_path / "hey-aura.npz"), trained


def stream_with(utterance: np.ndarray) -> np.ndarray:
    """A live always-listening stream: tone → utterance → pause → a longer
    'command' → tone. The detector sees the phrase the way a microphone does."""
    return np.concatenate([
        room_tone(1.2, seed=11),
        utterance,
        room_tone(0.35, seed=12),
        phrase_rendition(555, dur=1.4),      # the "command" that follows
        room_tone(1.0, seed=13),
    ]).astype(np.int16)


# --------------------------------------------------------------------------- #
# The regression itself                                                        #
# --------------------------------------------------------------------------- #


class TestTrainedPhraseWakes:
    def test_studio_captures_produce_clips_longer_than_the_window(self):
        """Guard the premise: real takes are silence-padded ~2 s clips. If a
        future capture path hands the trainer tight 1 s clips, the calibration
        test below weakens and this tells us why."""
        lengths = [len(studio_capture(i)) / SR for i in range(6)]
        assert min(lengths) > WINDOW_S + 0.4, lengths

    def test_threshold_lives_in_the_detector_score_space(self, tmp_path):
        """The stored threshold must be reachable by what the detector scores.

        The shipped bug: positives embedded as whole captures scored ~0.999
        against their own template, the threshold was calibrated near that,
        and the live 1-second windows — the only thing that ever gets scored
        at runtime — measured systematically lower. Held-out renditions in a
        real room (louder tone + early reflections) fell *below* the stored
        number six times out of eight: the phrase could never fire.

        A bare ">" is not enough of a contract — the old build cleared it by
        0.0004 on its luckiest capture. Require real margin."""
        path, trained = train_from_studio_captures(tmp_path)
        data = np.load(path)
        template = data["template"]
        for i in range(6):
            rendition = reverbish(phrase_rendition(800 + i, dur=0.8 + 0.05 * i))
            clip = np.concatenate([room_tone(0.4, level=200, seed=30 + i),
                                   rendition,
                                   room_tone(0.5, level=200, seed=60 + i)])
            scores = [
                float(np.dot(spectral_embedding(w), template)
                      / (np.linalg.norm(spectral_embedding(w))
                         * np.linalg.norm(template) + 1e-12))
                for w in detector_windows(clip)
            ]
            margin = max(scores) - trained.threshold
            assert margin > 0.005, (
                f"a held-out rendition peaks {margin:+.4f} against the calibrated "
                f"{trained.threshold:.4f} — the phrase could not reliably fire")

    def test_the_training_captures_themselves_would_fire(self, tmp_path):
        """The samples the user just recorded are the ground truth: played
        back through the detector's own windows, each one must clear the
        stored threshold with room to spare. The old build calibrated above
        what its own captures could score in detection space."""
        captures = [studio_capture(i) for i in range(6)]
        trained = train_wake(captures)
        for i, capture in enumerate(captures):
            scores = [
                float(np.dot(spectral_embedding(w), trained.template)
                      / (np.linalg.norm(spectral_embedding(w))
                         * np.linalg.norm(trained.template) + 1e-12))
                for w in detector_windows(capture)
            ]
            margin = max(scores) - trained.threshold
            assert margin > 0.005, f"capture {i} margin {margin:+.4f} — Aura could " \
                                   "not wake on the very takes it was trained on"

    def test_engine_fires_on_spoken_phrase_in_a_live_stream(self, tmp_path):
        """The user's exact scenario, end to end: train from studio captures,
        then say the phrase at a running detector."""
        path, trained = train_from_studio_captures(tmp_path)
        for i in range(5):
            engine = TemplateWakeEngine(str(path))
            stream = stream_with(phrase_rendition(900 + i))
            assert any(engine.feed(f) for f in frames_of(stream)), (
                f"rendition {i} never fired against threshold "
                f"{trained.threshold:.4f} — always-listening is dead")

    def test_engine_fires_under_room_conditions(self, tmp_path):
        """The same scenario with a real room: louder ambient tone and early
        reflections on every utterance. This is where the miscalibrated
        threshold turned 'always listening' into 'never listening'."""
        path, trained = train_from_studio_captures(tmp_path)
        for i in range(5):
            engine = TemplateWakeEngine(str(path))
            utterance = reverbish(phrase_rendition(800 + i, dur=0.8 + 0.05 * i))
            stream = np.concatenate([
                room_tone(1.2, level=200, seed=71 + i),
                utterance,
                room_tone(0.35, level=200, seed=81 + i),
                reverbish(phrase_rendition(555 + i, dur=1.4)),
                room_tone(1.0, level=200, seed=91 + i),
            ]).astype(np.int16)
            assert any(engine.feed(f) for f in frames_of(stream)), (
                f"room-conditioned rendition {i} never fired against threshold "
                f"{trained.threshold:.4f} — always-listening is dead")

    def test_long_phrase_fires_via_the_offset_window(self, tmp_path):
        """A slowly spoken phrase (≈1.2 s) exceeds the 1 s window; the offset
        window keeps it detectable instead of silently unreachable."""
        path, _ = train_from_studio_captures(tmp_path)
        engine = TemplateWakeEngine(str(path))
        stream = stream_with(phrase_rendition(910, dur=1.2))
        assert any(engine.feed(f) for f in frames_of(stream))

    def test_noise_and_other_phrases_do_not_fire(self, tmp_path):
        path, _ = train_from_studio_captures(tmp_path)
        engine = TemplateWakeEngine(str(path))
        stream = np.concatenate([
            room_tone(6.0, level=150, seed=21),
            other_phrase(31), room_tone(0.4, seed=22),
            other_phrase(32), room_tone(0.4, seed=23),
            other_phrase(33), room_tone(2.0, seed=24),
        ]).astype(np.int16)
        assert not any(engine.feed(f) for f in frames_of(stream)), (
            "a trained template fired on background and a different phrase")

    def test_refractory_prevents_double_fire(self, tmp_path):
        path, _ = train_from_studio_captures(tmp_path)
        engine = TemplateWakeEngine(str(path))
        stream = np.concatenate([
            room_tone(1.0, seed=41), phrase_rendition(901), room_tone(0.2, seed=42),
            phrase_rendition(902), room_tone(1.0, seed=43),
        ]).astype(np.int16)
        fires = sum(1 for f in frames_of(stream) if engine.feed(f))
        assert fires == 1, "two phrases inside the refractory period woke Aura twice"

    def test_factory_builds_the_trained_engine(self, tmp_path, stack):
        path, _ = train_from_studio_captures(tmp_path)
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(path)]
        stack.cfg.wake.phrase = "hey aura"
        engine = build_wake_engine(stack.cfg)
        assert isinstance(engine, TemplateWakeEngine)
        assert engine.phrase == "hey aura"
        assert engine.calibration == 2


# --------------------------------------------------------------------------- #
# The detector stays honest over weeks of always-on listening                   #
# --------------------------------------------------------------------------- #


class TestDetectorHygiene:
    def test_buffer_is_bounded_over_long_runs(self, tmp_path):
        """The always-on buffer grew without limit (~2 MB/min): a day of
        'always listening' was gigabytes. It must hold only what scoring
        needs — two windows and change."""
        path, _ = train_from_studio_captures(tmp_path, n=3)
        engine = TemplateWakeEngine(str(path))
        silence = AudioFrame(pcm=np.zeros(FRAME_SAMPLES, dtype=np.int16), ts=0.0)
        for _ in range(9000):                      # ~5 minutes of audio
            engine.feed(silence)
        assert len(engine._buf) <= engine._max_bytes

    def test_stats_expose_level_and_threshold(self, tmp_path):
        """wake_status() shows the user what Aura hears; the numbers must be
        there — level None only when nothing was scored recently."""
        path, trained = train_from_studio_captures(tmp_path, n=3)
        engine = TemplateWakeEngine(str(path))
        assert engine.stats()["level"] is None
        for f in frames_of(stream_with(phrase_rendition(903)))[:160]:
            engine.feed(f)
        stats = engine.stats()
        assert stats["threshold"] == pytest.approx(trained.threshold, abs=1e-3)
        assert stats["level"] is not None and stats["level"] > 0.5
        assert stats["fires"] >= 1


# --------------------------------------------------------------------------- #
# Old templates: load, run, and say they should be retrained                    #
# --------------------------------------------------------------------------- #


class TestLegacyTemplates:
    def _v1_template(self, tmp_path):
        """A template saved by the pre-fix build: no calibration key."""
        captures = [studio_capture(i) for i in range(4)]
        pos = np.stack([spectral_embedding(p) for p in captures])   # the old way
        pos = pos / np.maximum(np.linalg.norm(pos, axis=1, keepdims=True), 1e-9)
        template = pos.mean(axis=0)
        template = template / np.linalg.norm(template)
        path = tmp_path / "legacy.npz"
        np.savez_compressed(path, template=template.astype(np.float32),
                           threshold=np.float64(0.98), phrase="hey aura",
                           created=np.float64(time.time()))
        return path

    def test_v1_template_still_loads_and_runs(self, tmp_path, stack):
        path = self._v1_template(tmp_path)
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(path)]
        engine = build_wake_engine(stack.cfg)
        assert isinstance(engine, TemplateWakeEngine)
        assert engine.calibration == 1

    def test_v1_template_raises_the_retrain_notice(self, tmp_path, stack):
        """A threshold from the old score space is practically unreachable;
        Aura must say so instead of pretending to listen."""
        path = self._v1_template(tmp_path)
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(path)]
        orch = stack.build_orchestrator()
        orch._wake = orch._build_wake()
        status = orch.wake_status()
        assert "older Aura" in status["notice"]
        assert "train it again" in status["notice"].lower()

    def test_v2_template_has_no_notice(self, tmp_path, stack):
        path, _ = train_from_studio_captures(tmp_path, n=3)
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(path)]
        orch = stack.build_orchestrator()
        orch._wake = orch._build_wake()
        assert orch.wake_status()["notice"] == ""


# --------------------------------------------------------------------------- #
# Fallbacks leave a trace in the file log, not only in the event bus            #
# --------------------------------------------------------------------------- #


class TestWakeObservability:
    def test_fallback_reason_reaches_the_file_log(self, stack, aura_logs):
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(stack.cfg.data_dir) + "/missing.onnx"]
        orch = stack.build_orchestrator()
        orch._build_wake()
        assert any("wake" in r.getMessage() and r.levelname == "WARNING"
                   for r in aura_logs), [r.getMessage() for r in aura_logs]

    async def test_active_wake_reports_level_telemetry(self, tmp_path, stack):
        import asyncio

        path, trained = train_from_studio_captures(tmp_path, n=3)
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(path)]
        stack.cfg.wake.phrase = "hey aura"
        orch = stack.build_orchestrator()
        orch._has_audio = True
        orch._audio_task = asyncio.current_task()   # a live listener is part of "active"
        orch._wake = orch._build_wake()
        status = orch.wake_status()
        assert status["active"] is True
        assert status["threshold"] == pytest.approx(trained.threshold, abs=1e-3)
        assert status["fires"] == 0
        assert status["level"] is None               # nothing scored yet — honest
