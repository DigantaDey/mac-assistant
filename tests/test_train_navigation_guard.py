"""The navigation trainer must fail fast on an incompatible `laya`.

scripts/train_navigation_laya.py drives *internal* Laya APIs
(`laya.agent._option_logits` among them) that only exist in laya ≥ 0.3.22.
The pyproject pin keeps fresh installs right, but a stale venv used to find
out three minutes into training — via a bare ImportError, after scaffolding
plus untrained weights had already been written into the checkpoint
directory. These tests pin the contract: a clear, immediate, actionable
error, and silence only when the real API surface is present.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAINER_PATH = REPO_ROOT / "scripts" / "train_navigation_laya.py"


def _load_trainer():
    spec = importlib.util.spec_from_file_location("train_navigation_laya", TRAINER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_laya(monkeypatch: pytest.MonkeyPatch, *, with_option_logits: bool) -> None:
    """Install a fake `laya` package with a controllable agent surface."""
    laya = types.ModuleType("laya")
    laya.__version__ = "0.3.21-test"
    laya.Agent = type("Agent", (), {})

    agent = types.ModuleType("laya.agent")
    agent.collate_items = lambda *a, **k: None
    if with_option_logits:
        agent._option_logits = lambda *a, **k: None

    common = types.ModuleType("laya.common")
    common.build_model = lambda *a, **k: None

    monkeypatch.setitem(sys.modules, "laya", laya)
    monkeypatch.setitem(sys.modules, "laya.agent", agent)
    monkeypatch.setitem(sys.modules, "laya.common", common)


class TestRequireSupportedLaya:
    def test_missing_package_exits_with_install_instructions(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "laya", None)  # import laya → ImportError
        trainer = _load_trainer()
        with pytest.raises(SystemExit) as err:
            trainer.require_supported_laya()
        assert "pip install" in str(err.value)

    def test_old_laya_without_option_logits_is_named_and_shamed(self, monkeypatch, capsys):
        _fake_laya(monkeypatch, with_option_logits=False)
        trainer = _load_trainer()
        with pytest.raises(SystemExit) as err:
            trainer.require_supported_laya()
        message = str(err.value)
        assert "0.3.21-test" in message            # the offending version is named
        assert "_option_logits" in message         # …and what it is missing
        assert "pip install --upgrade" in message  # …and how to fix it

    def test_current_api_surface_is_accepted(self, monkeypatch):
        _fake_laya(monkeypatch, with_option_logits=True)
        trainer = _load_trainer()
        assert trainer.require_supported_laya() == "0.3.21-test"

    def test_guard_runs_before_any_checkpoint_is_written(self, monkeypatch, tmp_path, capsys):
        """The whole point: fail FIRST, leave nothing behind."""
        _fake_laya(monkeypatch, with_option_logits=False)
        trainer = _load_trainer()
        monkeypatch.setattr(sys, "argv",
                            ["train_navigation_laya.py", "--out", str(tmp_path / "ckpt"),
                             "--steps", "1"])
        with pytest.raises(SystemExit):
            trainer.main()
        assert not (tmp_path / "ckpt").exists()
        assert list(tmp_path.iterdir()) == []      # not even a staging directory
