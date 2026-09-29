"""Speech recognition stays on the fast in-memory path and respects the SLO."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import numpy as np

from aura.audio import AudioFrame
from aura.stt import WhisperCppSTT


def _frames() -> list[AudioFrame]:
    return [AudioFrame(pcm=np.ones(512, dtype=np.int16), ts=0.0)]


def test_whisper_cpp_pipes_binary_wav_without_text_mode(monkeypatch):
    """bytes + text=True raises before whisper starts and forced a disk retry."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert isinstance(kwargs["input"], bytes)
        assert "text" not in kwargs
        assert kwargs["timeout"] == 4
        return SimpleNamespace(stdout=b" open youtube \n", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    stt = WhisperCppSTT("/tmp/model.bin", binary="/tmp/whisper-cli")
    assert stt.transcribe(_frames()) == "open youtube"
    assert len(calls) == 1
    assert calls[0][0][calls[0][0].index("-f") + 1] == "-"


def test_whisper_cpp_timeout_does_not_retry_for_another_minute(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    stt = WhisperCppSTT("/tmp/model.bin", binary="/tmp/whisper-cli")
    assert stt.transcribe(_frames()) == ""
    assert len(calls) == 1
