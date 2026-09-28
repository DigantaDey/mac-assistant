"""Speech-to-text, fully local.

Backends, in auto-selection order on a Mac:
  1. whisper.cpp   — single C++ binary, Metal + CoreML(ANE) encoder, MIT.
  2. faster-whisper — Python, int8 CT2, good fallback.
  3. NullSTT       — returns "" (demo profile; typed input drives the session).

Every backend takes 16 kHz int16 PCM and returns plain text. That is the
whole contract — swapping engines is a config change, not a code change.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path


class STTEngine:
    def transcribe(self, pcm_frames) -> str:  # noqa: ANN001
        raise NotImplementedError


def _write_wav(pcm_frames, path: Path, sample_rate: int = 16_000) -> None:  # noqa: ANN001
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for frame in pcm_frames:
            pcm = frame.pcm
            if hasattr(pcm, "tobytes"):
                pcm = pcm.tobytes()
            wf.writeframes(pcm)


class WhisperCppSTT(STTEngine):
    """whisper.cpp via subprocess — zero Python deps, ANE-fast on Apple Silicon."""

    def __init__(self, model_path: str, language: str = "en", binary: str = "") -> None:
        if not model_path:
            raise RuntimeError("whisper.cpp needs `stt.whisper_model` (path to a ggml model)")
        self.model = str(Path(model_path).expanduser())
        self.language = language
        self.bin = binary or shutil.which("whisper-cli") or shutil.which("main") or ""
        if not self.bin:
            raise RuntimeError("whisper.cpp binary not found (looked for whisper-cli, main)")

    def transcribe(self, pcm_frames) -> str:  # noqa: ANN001
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            _write_wav(pcm_frames, wav_path)
            proc = subprocess.run(
                [self.bin, "-m", self.model, "-f", str(wav_path),
                 "-l", self.language, "-nt", "-np"],
                capture_output=True, text=True, timeout=60,
            )
            text = " ".join(proc.stdout.split()).strip()
            return text
        finally:
            wav_path.unlink(missing_ok=True)


class FasterWhisperSTT(STTEngine):
    """faster-whisper (CTranslate2 int8) — pure-Python fallback."""

    def __init__(self, model_size: str = "small", language: str = "en") -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"faster-whisper unavailable: {exc}") from exc
        self._model = WhisperModel(model_size, device="auto", compute_type="auto")
        self.language = language

    def transcribe(self, pcm_frames) -> str:  # noqa: ANN001
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            _write_wav(pcm_frames, wav_path)
            segments, _ = self._model.transcribe(str(wav_path), language=self.language)
            return " ".join(s.text for s in segments).strip()
        finally:
            wav_path.unlink(missing_ok=True)


class NullSTT(STTEngine):
    """Demo/CI: there is no audio to transcribe."""

    def transcribe(self, pcm_frames) -> str:  # noqa: ANN001
        return ""


def build_stt(cfg) -> STTEngine:  # noqa: ANN001
    engine = cfg.stt.engine
    attempts: list[STTEngine] = []
    if engine in ("auto", "whisper_cpp"):
        try:
            attempts.append(WhisperCppSTT(cfg.stt.whisper_model, cfg.stt.language,
                                          cfg.stt.whisper_cpp_bin))
        except RuntimeError:
            pass
    if engine in ("auto", "faster_whisper"):
        try:
            attempts.append(FasterWhisperSTT(cfg.stt.faster_whisper_model, cfg.stt.language))
        except RuntimeError:
            pass
    if attempts:
        return attempts[0]
    return NullSTT()
