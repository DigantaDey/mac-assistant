"""Aura — keyboardless, voice-activated smart navigation for macOS.

Aura listens for *your* wake phrase, understands the request on-device with
Laya — a non-autoregressive decision model that answers typed questions in
one forward pass — proposes a plan you can see, and executes it across your
Mac. There is no LLM anywhere: nothing that generates text, nothing that can
invent an action, nothing to serve. Every confirmation and correction you
give becomes training signal, so Aura quietly becomes *your* assistant.

Layers (see ARCHITECTURE.md):

    audio → wake word → VAD → STT → planner (rules reflexes, then Laya routing)
                                         ↓
                                laya decision gate  ← examples ← your feedback
                                         ↓
                                skills (AX / AppleScript / browser / files)
                                         ↓
                                    TTS + timeline UI
"""

__app_name__ = "Aura"
__version__ = "0.7.1"
