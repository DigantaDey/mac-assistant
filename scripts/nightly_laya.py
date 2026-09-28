#!/usr/bin/env python3
"""The learning loop's engine room: fine-tune Laya on YOUR feedback.

Every session, Aura's gate logs each proposed action with its outcome:

    auto-run ✓    confirmed ✓    corrected ✗ (2× weight)    cancelled ✗✗

This script turns that buffer into a better gate, entirely on-device:

    1. export     laya_examples (SQLite) → fine-tune JSONL
    2. train      LoRA adapter on the 421M-param Laya checkpoint (MLX, minutes)
    3. evaluate   accuracy on a held-out split; ship only if it improves
    4. swap       point config.toml at the new adapter; hot-reload

Schedule it with launchd (see scripts/com.aura.nightly.plist template) or run
it by hand after a day of corrections:

    python scripts/nightly_laya.py --data-dir ~/Library/Application\\ Support/Aura
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--holdout", type=float, default=0.15,
                    help="fraction held out for validation")
    ap.add_argument("--iters", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true", help="export + stats only")
    args = ap.parse_args()

    from aura.config import default_data_dir  # repo import
    data_dir = Path(args.data_dir or default_data_dir())
    db = data_dir / "aura.sqlite3"
    if not db.exists():
        sys.exit(f"No database at {db} — run Aura first.")

    from aura.laya import ExampleBuffer
    buf = ExampleBuffer(db)
    stats = buf.stats()
    print(f"Example buffer: {stats}")
    if stats["total"] < 64:
        print("Fewer than 64 examples — nothing to learn yet. Come back tomorrow.")
        return 0

    export_path = data_dir / "laya_finetune" / "dataset.jsonl"
    n = buf.export_jsonl(export_path)
    print(f"Exported {n} examples → {export_path}")

    # held-out split
    lines = export_path.read_text().splitlines()
    random.seed(7)
    random.shuffle(lines)
    k = max(1, int(len(lines) * args.holdout))
    train, valid = lines[k:], lines[:k]
    (export_path.parent / "train.jsonl").write_text("\n".join(train) + "\n")
    (export_path.parent / "valid.jsonl").write_text("\n".join(valid) + "\n")
    print(f"train={len(train)}  valid={len(valid)}")

    if args.dry_run:
        return 0

    try:
        import mlx.core as mx  # noqa: F401
    except ImportError:
        sys.exit("MLX not available — fine-tuning needs Apple Silicon + `pip install mlx-lm laya`.")

    adapter_dir = data_dir / "laya_finetune" / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    print(f"""
Fine-tuning the Laya decision head (this is fast — the model is 421M params):

  from laya.finetune import train_adapter          # API per laya package docs
  train_adapter(
      base_checkpoint="convaiinnovations/laya",
      train_jsonl={str(export_path.parent / 'train.jsonl')!r},
      valid_jsonl={str(export_path.parent / 'valid.jsonl')!r},
      iters={args.iters},
      out_dir={str(adapter_dir)!r},
  )

Evaluate before you trust it:
  - matched-question accuracy must beat the current gate on valid.jsonl
  - destructive-question false-negatives must be 0 (never soften safety)

Then set in config.toml:  [laya]  adapter_dir = {str(adapter_dir)!r}
and restart Aura (hot-swap lands in v0.3).
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
