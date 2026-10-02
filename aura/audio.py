"""Microphone capture.

`MicStream` wraps PortAudio (via sounddevice) when available — 16 kHz mono
int16, the one format every downstream model wants. `SilentMic` stands in for
machines with no audio device (CI, the demo profile) so the exact same
orchestration code runs end to end.

Frames arrive on a PortAudio callback thread and are handed to the asyncio
loop via `call_soon_threadsafe` — the only safe crossing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

try:  # optional dependency — only needed on a real Mac
    import sounddevice as sd

    HAS_AUDIO = True
except Exception:  # pragma: no cover - depends on host
    HAS_AUDIO = False


SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512  # ~32 ms — small enough for snappy VAD, cheap enough to idle on


@dataclass
class AudioFrame:
    pcm: object  # int16 numpy array when numpy is present, else bytes
    ts: float


class MicStream:
    """Real microphone. Raises MicUnavailable if no device can be opened."""

    class MicUnavailable(RuntimeError):
        pass

    def __init__(self, loop: asyncio.AbstractEventLoop, on_frame) -> None:
        if not HAS_AUDIO:
            raise MicStream.MicUnavailable("sounddevice/numpy not installed")
        self._loop = loop
        self._on_frame = on_frame  # sync callable invoked with AudioFrame
        self._stream: sd.InputStream | None = None
        self._accepting_frames = False

    def start(self) -> None:
        def callback(indata, frames, time_info, status):  # PortAudio thread
            # PortAudio may deliver one last callback while close() is racing
            # engine shutdown. Never call into a closed asyncio loop.
            if not self._accepting_frames or self._loop.is_closed() or frames <= 0:
                return
            if status:
                pass  # overflows are common on first start; drop silently
            frame = AudioFrame(pcm=indata[:, 0].copy(), ts=time_info.currentTime)
            try:
                self._loop.call_soon_threadsafe(self._on_frame, frame)
            except RuntimeError:
                # The loop closed between is_closed() and scheduling.
                self._accepting_frames = False

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                callback=callback,
            )
            self._accepting_frames = True
            self._stream.start()
        except Exception as exc:
            self._accepting_frames = False
            raise MicStream.MicUnavailable(f"could not open microphone: {exc}") from exc

    def stop(self) -> None:
        # Flip this before stopping PortAudio: callback and shutdown run on
        # different threads.
        self._accepting_frames = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None


class SilentMic:
    """No audio at all — demo profile / CI. Wake happens via manual trigger."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_frame) -> None:
        pass

    def start(self) -> None: ...

    def stop(self) -> None: ...
