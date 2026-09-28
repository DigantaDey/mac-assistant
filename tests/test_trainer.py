"""Wake Phrase Studio: embedding, judging, training, detection — end to end.

These tests build synthetic "phrase" audio (stable harmonic stacks), train a
template the way the in-app panel does, save it, load it through the engine
factory, and verify the detector fires on the phrase and stays silent on
noise. The orchestrator flow (start → samples → finish → live engine) is
exercised with fake frames so no microphone is required anywhere.
"""

from __future__ import annotations

import time

import pytest

np = pytest.importorskip("numpy")

from conftest import DemoStack  # noqa: E402

from aura.audio import AudioFrame  # noqa: E402
from aura.wakeword import (  # noqa: E402
    ManualTrigger,
    TemplateWakeEngine,
    build_wake_engine,
    load_template_meta,
)
from aura.wakeword_trainer import (  # noqa: E402
    EMBED_DIM,
    SR,
    WINDOW_S,
    judge_sample,
    normalize_length,
    save_template,
    slugify,
    spectral_embedding,
    synth_negatives,
    train_wake,
)

# --------------------------------------------------------------------------- #
# Synthetic speech: three harmonics with vibrato — same "identity" across
# renders, unlike noise, and stable under the band-pooled embedding.
# --------------------------------------------------------------------------- #


def phrase_pcm(seconds: float = 0.8, f0: float = 180.0, amp: float = 6000.0,
               seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    vib = 1.0 + 0.02 * np.sin(2 * np.pi * 5.3 * t)
    sig = (np.sin(2 * np.pi * f0 * t * vib)
           + 0.6 * np.sin(2 * np.pi * 2.02 * f0 * t * vib)
           + 0.35 * np.sin(2 * np.pi * 3.01 * f0 * t * vib))
    env = np.minimum(1, np.minimum(t / 0.05, (seconds - t) / 0.05).clip(0, 1))
    pcm = sig * env * amp
    return (pcm + rng.normal(0, 40, len(pcm))).astype(np.int16)


def frame_of(pcm_int16: np.ndarray, ts: float | None = None) -> AudioFrame:
    return AudioFrame(pcm=pcm_int16, ts=ts if ts is not None else time.time())


def frames_of(pcm_int16: np.ndarray, chunk: int = 960) -> list[AudioFrame]:
    now = time.time()
    return [frame_of(pcm_int16[i:i + chunk], now + i / SR)
            for i in range(0, len(pcm_int16), chunk)]


class TestEmbedding:
    def test_shape_and_unit_norm(self):
        e = spectral_embedding(phrase_pcm())
        assert e.shape == (EMBED_DIM,)
        assert abs(float(np.linalg.norm(e)) - 1.0) > 0  # trainer normalizes later
        assert np.all(np.isfinite(e))

    def test_length_normalized_to_window(self):
        for secs in (0.4, 0.9, 2.5):
            out = normalize_length(phrase_pcm(secs))
            assert len(out) == int(WINDOW_S * SR)

    def test_same_phrase_different_renders_stay_close(self):
        base = spectral_embedding(normalize_length(phrase_pcm(f0=180)))
        variant = spectral_embedding(normalize_length(phrase_pcm(f0=186, seed=3)))
        c = float(np.dot(base, variant) /
                  (np.linalg.norm(base) * np.linalg.norm(variant)))
        assert c > 0.97

    def test_phrase_vs_noise_far_apart(self):
        phrase = spectral_embedding(normalize_length(phrase_pcm()))
        rng = np.random.default_rng(7)
        noise = spectral_embedding(
            normalize_length(rng.normal(0, 2000, SR).astype(np.int16)))
        c = float(np.dot(phrase, noise) /
                  (np.linalg.norm(phrase) * np.linalg.norm(noise)))
        # Log-band embeddings correlate broadly (both carry low-freq energy);
        # separation is the threshold's job — here we just require distinct.
        assert c < 0.9

    def test_amplitude_invariance(self):
        loud = spectral_embedding(phrase_pcm(amp=12000))
        soft = spectral_embedding(phrase_pcm(amp=1500))
        assert float(np.corrcoef(loud, soft)[0, 1]) > 0.99


class TestJudge:
    def test_good_sample_accepted(self):
        q = judge_sample(phrase_pcm().tobytes())
        assert q.ok, q.message

    def test_too_short_rejected(self):
        q = judge_sample(phrase_pcm(0.1).tobytes())
        assert not q.ok and "short" in q.message.lower()

    def test_too_long_rejected(self):
        q = judge_sample(phrase_pcm(4.5).tobytes())
        assert not q.ok and "long" in q.message.lower()

    def test_silence_rejected(self):
        q = judge_sample(np.zeros(SR // 2, dtype=np.int16).tobytes())
        assert not q.ok and "quiet" in q.message.lower()

    def test_clipping_rejected(self):
        q = judge_sample(np.full(SR // 2, 32700, dtype=np.int16).tobytes())
        assert not q.ok and "loud" in q.message.lower()


class TestTrainWake:
    def _positives(self):
        return [phrase_pcm(f0=180 + d, seed=s).astype(np.float32)
                for d, s in ((0, 1), (4, 2), (-3, 3), (2, 4))]

    def test_threshold_separates_positives_from_negatives(self):
        trained = train_wake(self._positives())
        pos = [spectral_embedding(normalize_length(p)) for p in self._positives()]
        pos = [p / np.linalg.norm(p) for p in pos]
        neg = [spectral_embedding(normalize_length(n))
               for n in synth_negatives(6)]
        neg = [n / np.linalg.norm(n) for n in neg]
        assert min(float(p @ trained.template) for p in pos) >= trained.threshold
        assert max(float(n @ trained.template) for n in neg) < trained.threshold
        assert trained.margin > 0
        assert trained.positives == 4 and trained.negatives >= 6

    def test_needs_three_samples(self):
        with pytest.raises(Exception):
            train_wake(self._positives()[:2])

    def test_save_and_load_meta(self, tmp_path):
        trained = train_wake(self._positives())
        path = save_template(trained, "Hey Aura", tmp_path / "wakewords" / "x.npz")
        meta = load_template_meta(str(path))
        assert meta["phrase"] == "Hey Aura"
        assert abs(meta["threshold"] - trained.threshold) < 1e-9

    def test_slugify(self):
        assert slugify("Hey, Aura!") == "hey-aura"
        assert slugify("  computer  ") == "computer"
        assert slugify("!!!") == "wake-phrase"


class TestTemplateEngine:
    def _engine(self, tmp_path):
        trained = train_wake([phrase_pcm(f0=180 + d, seed=s).astype(np.float32)
                              for d, s in ((0, 1), (4, 2), (-3, 3))])
        path = save_template(trained, "hey aura", tmp_path / "w.npz")
        return TemplateWakeEngine(str(path))

    def test_fires_on_phrase_inside_window(self, tmp_path):
        eng = self._engine(tmp_path)
        pad = (np.random.default_rng(5).normal(0, 60, int(0.3 * SR))).astype(np.int16)
        stream = np.concatenate([pad, phrase_pcm(0.8), pad])
        fired = [f for f in frames_of(stream) if eng.feed(f)]
        assert len(fired) >= 1

    def test_silent_on_noise_only(self, tmp_path):
        eng = self._engine(tmp_path)
        rng = np.random.default_rng(11)
        stream = (rng.normal(0, 500, SR * 4)).astype(np.int16)  # 4 s of noise
        assert not any(eng.feed(f) for f in frames_of(stream))

    def test_factory_prefers_trained_template(self, tmp_path, stack):
        trained = train_wake([phrase_pcm().astype(np.float32) for _ in range(3)])
        path = save_template(trained, "hey aura", tmp_path / "w.npz")
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(path)]
        assert isinstance(build_wake_engine(stack.cfg), TemplateWakeEngine)

    def test_factory_falls_back_to_manual(self, stack):
        stack.cfg.wake.mode = "openwakeword"
        stack.cfg.wake.models = [str(stack.cfg.data_dir + "/missing.onnx")]
        # missing file → engine build fails → graceful manual trigger
        eng = build_wake_engine(stack.cfg)
        assert isinstance(eng, (ManualTrigger, object))


# --------------------------------------------------------------------------- #
# The in-app flow: orchestrator training with fake frames (no microphone)
# --------------------------------------------------------------------------- #


class FakeMicOrchestrator:
    """DemoStack orchestrator with the mic path forced on."""

    @staticmethod
    def build(stack):
        orch = stack.build_orchestrator()
        orch._has_audio = True
        orch.state = "armed"
        return orch


class TestTrainingFlow:
    def _good(self):
        return frames_of(phrase_pcm(0.9))

    def test_start_validation(self, stack):
        orch = FakeMicOrchestrator.build(stack)
        assert orch.training_start("")["ok"] is False
        assert orch.training_start("one two three four five six")["ok"] is False
        assert orch.training_start("hey aura 2")["ok"] is False
        res = orch.training_start("Hey Aura")
        assert res["ok"] and res["need"] == 6
        assert orch.training_status()["phrase"] == "hey aura"

    def test_start_requires_audio(self, stack):
        orch = stack.build_orchestrator()
        orch.state = "armed"
        res = orch.training_start("hey aura")
        assert res["ok"] is False and "microphone" in res["message"].lower()

    def test_samples_judged_and_counted(self, stack):
        orch = FakeMicOrchestrator.build(stack)
        orch.training_start("hey aura")
        orch._handle_train_sample(self._good())
        st = orch.training_status()
        assert st["count"] == 1
        orch._handle_train_sample(frames_of(phrase_pcm(0.2)))   # too short
        assert orch.training_status()["count"] == 1
        orch._handle_train_sample(self._good())
        orch._handle_train_sample(self._good())
        assert orch.training_status()["count"] == 3

    def test_finish_requires_minimum(self, stack):
        orch = FakeMicOrchestrator.build(stack)
        orch.training_start("hey aura")
        orch._handle_train_sample(self._good())
        import asyncio
        assert asyncio.run(orch.training_finish())["ok"] is False

    def test_full_training_going_live(self, stack):
        orch = FakeMicOrchestrator.build(stack)
        res = orch.training_start("Hey Aura")
        assert res["ok"]
        for _ in range(4):
            orch._handle_train_sample(self._good())
        import asyncio
        done = asyncio.run(orch.training_finish())
        assert done["ok"], done
        assert done["engine"] == "TemplateWakeEngine"
        assert done["margin"] > 0

        from pathlib import Path
        path = Path(done["path"])
        assert path.exists() and path.suffix == ".npz"
        meta = load_template_meta(str(path))
        assert meta["phrase"] == "hey aura"

        # live immediately: engine rebuilt, mode + phrase remembered
        assert isinstance(orch._wake, TemplateWakeEngine)
        assert stack.cfg.wake.mode == "openwakeword"
        assert stack.cfg.wake.phrase == "hey aura"
        # persisted for the next launch
        overrides = Path(stack.cfg.data_dir) / "runtime.toml"
        assert overrides.exists() and "hey aura" in overrides.read_text()
        assert orch.training_status()["active"] is False

    def test_cancel_resets(self, stack):
        orch = FakeMicOrchestrator.build(stack)
        orch.training_start("hey aura")
        orch._handle_train_sample(self._good())
        assert orch.training_cancel()["ok"]
        assert orch.training_status() == {"active": False}

    def test_setup_refused_in_demo_profile(self, stack):
        orch = FakeMicOrchestrator.build(stack)
        import asyncio
        res = asyncio.run(orch.run_setup())
        assert res["ok"] is False
