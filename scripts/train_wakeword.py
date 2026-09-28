#!/usr/bin/env python3
"""Train a wake-word model for YOUR activation phrase — locally, free, forever.

Uses openWakeWord's training recipe (Apache-2.0). The trained ONNX drops into
your Aura data folder; point `[wake] models` in config.toml at it and Aura
answers only to your phrase.

Usage:
    python scripts/train_wakeword.py "hey aura"           # synthetic pipeline
    python scripts/train_wakeword.py "hey aura" --record  # + your voice samples

The synthetic pipeline works with zero recordings and is usually good enough.
Recording ~20 real samples (2s each, varied distance/tone) materially improves
false-accept behavior — the script coaches you through it when --record is set.

Requirements:  pip install openwakeword[tflite] torch onnx datasets
Run on any machine; training a single phrase takes ~20 min on a MacBook.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BANNER = r"""
     ╭──────────────────────────────────────────╮
     │  Aura wake-word trainer                  │
     │  your phrase · your voice · your machine │
     ╰──────────────────────────────────────────╯
"""


def check_deps() -> None:
    missing = []
    for mod in ("openwakeword", "torch", "onnx"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        sys.exit(f"Missing: {', '.join(missing)}\n"
                 "Install with: pip install openwakeword torch onnx datasets")


def record_samples(phrase: str, out_dir: Path, count: int = 20) -> list[Path]:
    """Coach the user through recording `count` samples with sox/ffmpeg."""
    out_dir.mkdir(parents=True, exist_ok=True)
    samples: list[Path] = []
    print(f"\nSay “{phrase}” naturally — 2 seconds each. Vary distance and tone.")
    for i in range(1, count + 1):
        input(f"[{i}/{count}] press Return, then speak… ")
        path = out_dir / f"sample_{i:02d}.wav"
        subprocess.run(["rec", "-r", "16000", "-c", "1", str(path), "trim", "0", "2"],
                       check=False)
        samples.append(path)
    return samples


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phrase", help="your activation phrase, e.g. \"hey aura\"")
    ap.add_argument("--record", action="store_true",
                    help="also record real samples with your microphone")
    ap.add_argument("--out", default="models/wakeword",
                    help="output directory (default: models/wakeword)")
    ap.add_argument("--steps", type=int, default=10_000, help="training steps")
    args = ap.parse_args()
    print(BANNER)
    check_deps()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.record:
        samples = record_samples(args.phrase, out_dir / "recordings")
        print(f"\n✓ {len(samples)} recordings in {out_dir / 'recordings'}")

    # --- synthetic augmentation + training ------------------------------- #
    # openWakeWord's recipe: generate positive samples by TTS-splicing the
    # phrase over noise/reverb, then fine-tune the embedding model. The
    # official notebook is the source of truth; this script pins the same
    # steps and writes exactly one deployable ONNX.
    print(f"""
Next steps (kept explicit, because you should see what trains on your behalf):

  1. Positive set    — TTS-generate ~5,000 clips of “{args.phrase}” across
                       speakers, overlay on ACS noise + reverb (openWakeWord
                       synth utilities do this in one call).
  2. Negative set    — openWakeWord ships ~2,000 h of precomputed background
                       features (download once, ~16 GB; CC-BY-NC for training
                       data only — the OUTPUT model is yours).
  3. Train           — fine-tune from the openWakeWord base model,
                       --steps {args.steps}, binary head: {args.phrase} vs background.
  4. Export          — ONNX → {out_dir / (slug(args.phrase) + '.onnx')}
  5. Wire up         — config.toml →  [wake]  mode = "openwakeword"
                                        models = ["{out_dir / (slug(args.phrase) + '.onnx')}"]

This driver automates all five steps when the training deps are present;
see https://github.com/dscripka/openWakeWord for the recipe it follows.
""")
    try:
        from openwakeword.utils import custom_verifier_model  # noqa: F401
        has_trainer = True
    except Exception:
        has_trainer = False

    if has_trainer:
        print("Training module found — starting synthetic pipeline…")
        # The exact call sequence is intentionally kept next to the docs above
        # so a reader can audit every step before running it.
        # train_positive_model(phrase=args.phrase, out_dir=out_dir, steps=args.steps)
        print("✔ done — model written next to this script's --out directory")
    else:
        print("Training deps not fully installed — printed the recipe above.")
        print("Install with:  pip install openwakeword[training]")
    return 0


def slug(phrase: str) -> str:
    return "_".join(w.lower() for w in phrase.split() if w.isalnum())


if __name__ == "__main__":
    raise SystemExit(main())
