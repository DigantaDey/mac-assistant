"""TTS failures are optional and must never wedge or leak from a session."""

from __future__ import annotations

import subprocess

from aura import tts


def test_mac_say_timeout_is_a_logged_soft_failure(monkeypatch, caplog):
    monkeypatch.setattr(tts.shutil, "which", lambda _name: "/usr/bin/say")

    def timed_out(*_args, **kwargs):
        raise subprocess.TimeoutExpired(["say"], kwargs["timeout"])

    monkeypatch.setattr(tts.subprocess, "run", timed_out)
    engine = tts.MacSayTTS()

    assert engine.speak("Aura finished the task.") == 0.0
    assert "macOS speech timed out" in caplog.text


def test_mac_say_missing_binary_is_a_soft_failure(monkeypatch, caplog):
    monkeypatch.setattr(tts.shutil, "which", lambda _name: "/usr/bin/say")

    def missing(*_args, **_kwargs):
        raise OSError("gone")

    monkeypatch.setattr(tts.subprocess, "run", missing)
    engine = tts.MacSayTTS()

    assert engine.speak("Hello.") == 0.0
    assert "couldn't start macOS speech" in caplog.text
