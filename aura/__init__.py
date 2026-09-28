"""Aura — a private, offline, voice-controlled assistant for macOS.

Aura listens for *your* wake phrase, understands natural language on-device,
proposes a plan you can see, and executes it across your Mac — gated by a
Laya decision model for speed and calibrated safety. Every confirmation and
correction you give becomes training signal, so Aura quietly becomes *your*
assistant.

Layers (see ARCHITECTURE.md):

    audio → wake word → STT → planner (small local LLM)
                                   ↓
                          laya decision gate  ← examples ← your feedback
                                   ↓
                          skills (AX / AppleScript / browser / files)
                                   ↓
                              TTS + timeline UI
"""

__app_name__ = "Aura"
__version__ = "0.4.0"
