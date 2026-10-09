"""Speech recognition stays on the fast in-memory path and respects the SLO."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import numpy as np

from aura.audio import AudioFrame
from aura.stt import TRANSCRIBE_TIMEOUT_SECONDS, WhisperCppSTT


def _frames() -> list[AudioFrame]:
    return [AudioFrame(pcm=np.ones(512, dtype=np.int16), ts=0.0)]


def test_whisper_cpp_pipes_binary_wav_without_text_mode(monkeypatch):
    """bytes + text=True raises before whisper starts and forced a disk retry."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert isinstance(kwargs["input"], bytes)
        assert "text" not in kwargs
        assert kwargs["timeout"] == TRANSCRIBE_TIMEOUT_SECONDS
        return SimpleNamespace(stdout=b" open youtube \n", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    stt = WhisperCppSTT("/tmp/model.bin", binary="/tmp/whisper-cli")
    assert stt.transcribe(_frames()) == "open youtube"
    assert len(calls) == 1
    assert calls[0][0][calls[0][0].index("-f") + 1] == "-"


def test_transcribe_deadline_outlives_a_cold_model_load():
    """whisper.cpp loads its model per invocation; the old 4-second cap made
    the first command after a wake transcribe as "" — "I didn't catch that"
    for a perfectly good sentence. The deadline must cover a cold start while
    staying inside the orchestrator's 20-second session budget."""
    from aura.orchestrator import MAX_ACTIVE_REQUEST_SECONDS

    assert TRANSCRIBE_TIMEOUT_SECONDS >= 10.0
    assert TRANSCRIBE_TIMEOUT_SECONDS < MAX_ACTIVE_REQUEST_SECONDS


def test_whisper_cpp_timeout_does_not_retry_for_another_minute(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    stt = WhisperCppSTT("/tmp/model.bin", binary="/tmp/whisper-cli")
    assert stt.transcribe(_frames()) == ""
    assert len(calls) == 1


def test_warm_runs_one_silent_pass(monkeypatch):
    """The startup warm-up decodes silence once so the first real utterance
    doesn't pay the model load under the transcription deadline."""
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout=b"", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    stt = WhisperCppSTT("/tmp/model.bin", binary="/tmp/whisper-cli")
    assert stt.warm() is True
    assert len(calls) == 1
    assert isinstance(calls[0]["input"], bytes)          # 0.25 s of silence, in-memory
    assert len(calls[0]["input"]) < 44 + 2 * 16_000      # a WAV header + quarter second


def test_warm_survives_a_broken_binary(monkeypatch):
    def run(argv, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(subprocess, "run", run)
    stt = WhisperCppSTT("/tmp/model.bin", binary="/tmp/whisper-cli")
    assert stt.warm() is False        # best-effort: never raises into startup
