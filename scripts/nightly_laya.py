#!/usr/bin/env python3
"""The learning loop's engine room: retrain Laya on YOUR feedback.

Every session, the gate records each proposed action with its outcome
(`ExampleBuffer` → `laya_examples` in aura.sqlite3):

    auto-run ✓    confirmed ✓    corrected ✗ (2× weight)    cancelled ✗✗

This script turns that buffer into a better checkpoint, entirely on-device:

    1. export     laya_examples (SQLite) → gate feedback JSONL
    2. train      scripts/train_navigation_laya.py --feedback-jsonl …
                  (the full navigation checkpoint, rebuilt with your verdicts
                  folded into the gate questions)
    3. evaluate   the candidate's route/match/destructive accuracy against the
                  shipping bar before anything is trusted
    4. swap       move the candidate over assets/models/aura-nav-laya

Schedule it with launchd (see scripts/com.aura.nightly.plist template) or run
it by hand after a day of corrections:

    python scripts/nightly_laya.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = REPO_ROOT / "assets" / "models" / "aura-nav-laya"
# The same bar the trainer holds itself to — a feedback-trained checkpoint
# must clear it before replacing anything.
BAR = {"route_acc": 0.85, "match_acc": 0.85, "destr_acc": 0.85}


def export_feedback(db: Path, out_jsonl: Path, holdout: float, dry_run: bool) -> Path:
    from aura.laya import ExampleBuffer

    buf = ExampleBuffer(db)
    stats = buf.stats()
    print(f"Example buffer: {stats}")
    if stats["total"] < 64:
        print("Fewer than 64 examples — nothing to learn yet. Come back tomorrow.")
        raise SystemExit(0)

    export_path = out_jsonl.parent / "dataset.jsonl"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    n = buf.export_jsonl(export_path)
    print(f"Exported {n} examples → {export_path}")
    if dry_run:
        raise SystemExit(0)
    return export_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="")
    # Must match the trainer's proven-to-pass default: fewer steps leave the
    # gate's match question below the bar and the swap would (rightly) never
    # happen. It runs unattended overnight — wall time is cheap here.
    ap.add_argument("--steps", type=int, default=4500)
    ap.add_argument("--dry-run", action="store_true", help="export + stats only")
    ap.add_argument("--keep", action="store_true",
                    help="keep the candidate directory for inspection")
    args = ap.parse_args()

    from aura.config import default_data_dir
    data_dir = Path(args.data_dir or default_data_dir())
    db = data_dir / "aura.sqlite3"
    if not db.exists():
        sys.exit(f"No database at {db} — run Aura first.")

    feedback = export_feedback(db, data_dir / "laya_finetune" / "feedback.jsonl",
                               holdout=0.15, dry_run=args.dry_run)

    # Train a candidate next to the shipped checkpoint, never on top of it.
    candidate = Path(tempfile.mkdtemp(prefix="aura-laya-candidate-"))
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "train_navigation_laya.py"),
           "--out", str(candidate), "--steps", str(args.steps),
           "--feedback-jsonl", str(feedback)]
    print("Rebuilding the checkpoint with your feedback:")
    print("  " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    if proc.returncode != 0:
        shutil.rmtree(candidate, ignore_errors=True)
        sys.exit("Training failed — the shipped checkpoint is untouched.")

    report = candidate / "training_report.json"
    metrics = json.loads(report.read_text()) if report.exists() else {}
    failed = [k for k, bar in BAR.items() if metrics.get(k, 0.0) < bar]
    if failed:
        print(f"Candidate below bar ({failed}: "
              + ", ".join(f"{k}={metrics.get(k, 0):.3f}" for k in failed) + ").")
        if not args.keep:
            shutil.rmtree(candidate, ignore_errors=True)
        sys.exit("Kept the shipped checkpoint.")

    old = data_dir / "laya_finetune" / "previous-checkpoint"
    if SHIPPED.exists():
        shutil.rmtree(old, ignore_errors=True)
        shutil.move(str(SHIPPED), str(old))
    shutil.move(str(candidate), str(SHIPPED))
    print(f"Swapped in the improved checkpoint (previous kept at {old}).")
    print("Restart Aura to pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
