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
    def transcribe(self, pcm_frames) -> str:
        raise NotImplementedError


def _write_wav(pcm_frames, path: Path, sample_rate: int = 16_000) -> None:
    # Collect all PCM data first, then write in one shot — avoids repeated
    # small writes that stall on file-system buffering.
    chunks: list[bytes] = []
    for frame in pcm_frames:
        pcm = frame.pcm
        chunks.append(pcm.tobytes() if hasattr(pcm, "tobytes") else bytes(pcm))
    pcm_data = b"".join(chunks)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)


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

    def transcribe(self, pcm_frames) -> str:
        # Build WAV in memory and pipe through stdin — eliminates disk I/O
        # (no temp file create/write/read/unlink per utterance).
        import io

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16_000)
            chunks: list[bytes] = []
            for frame in pcm_frames:
                pcm = frame.pcm
                chunks.append(pcm.tobytes() if hasattr(pcm, "tobytes") else bytes(pcm))
            wf.writeframes(b"".join(chunks))
        wav_data = buf.getvalue()

        try:
            proc = subprocess.run(
                [self.bin, "-m", self.model, "-f", "-",
                 "-l", self.language, "-nt", "-np"],
                input=wav_data, capture_output=True, text=True, timeout=60,
            )
            return " ".join(proc.stdout.split()).strip()
        except Exception:
            # Some whisper.cpp builds don't support stdin ("-" as filename).
            # Fall back to the temp-file approach.
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav_path = Path(tmp.name)
            try:
                _write_wav(pcm_frames, wav_path)
                proc = subprocess.run(
                    [self.bin, "-m", self.model, "-f", str(wav_path),
                     "-l", self.language, "-nt", "-np"],
                    capture_output=True, text=True, timeout=60,
                )
                return " ".join(proc.stdout.split()).strip()
            finally:
                wav_path.unlink(missing_ok=True)


class FasterWhisperSTT(STTEngine):
    """faster-whisper (CTranslate2 int8) — pure-Python fallback.

    The model is loaded on first use, not at startup, and `unload()` drops it
    again after idle time — the orchestrator's lightweight promise in action.
    """

    def __init__(self, model_size: str = "small", language: str = "en") -> None:
        self.model_size = model_size
        self.language = language
        self._model = None  # lazy — nothing resident until the first utterance

    def _ensure_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except Exception as exc:  # pragma: no cover
                raise RuntimeError(f"faster-whisper unavailable: {exc}") from exc
            self._model = WhisperModel(self.model_size, device="auto",
                                       compute_type="auto")
        return self._model

    def unload(self) -> bool:
        """Drop the resident model. Returns True if something was unloaded."""
        if self._model is not None:
            self._model = None
            return True
        return False

    def transcribe(self, pcm_frames) -> str:
        model = self._ensure_model()
        # Fast path: pass the raw float32 numpy array directly — no disk I/O.
        try:
            import numpy as np

            chunks: list[bytes] = []
            for frame in pcm_frames:
                pcm = frame.pcm
                chunks.append(pcm.tobytes() if hasattr(pcm, "tobytes") else bytes(pcm))
            pcm_data = b"".join(chunks)
            audio = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = model.transcribe(audio, language=self.language)
            return " ".join(s.text for s in segments).strip()
        except Exception:
            # Fallback to WAV-based transcription if numpy path fails.
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav_path = Path(tmp.name)
            try:
                _write_wav(pcm_frames, wav_path)
                segments, _ = model.transcribe(str(wav_path), language=self.language)
                return " ".join(s.text for s in segments).strip()
            finally:
                wav_path.unlink(missing_ok=True)


class NullSTT(STTEngine):
    """Demo/CI: there is no audio to transcribe."""

    def transcribe(self, pcm_frames) -> str:
        return ""


def build_stt(cfg) -> STTEngine:
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
