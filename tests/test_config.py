"""Config: runtime overrides, live-field filtering, hot reload."""

from __future__ import annotations

from conftest import DemoStack

from aura.config import (
    LIVE_FIELDS,
    load_config,
    runtime_overrides_path,
    watch_paths,
    write_overrides,
)


class TestWriteOverrides:
    def test_write_and_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path))
        write_overrides(tmp_path, {"wake": {"mode": "openwakeword", "phrase": "hey aura"}})
        cfg = load_config()
        assert cfg.wake.mode == "openwakeword"
        assert cfg.wake.phrase == "hey aura"

    def test_merges_with_existing(self, tmp_path):
        write_overrides(tmp_path, {"wake": {"mode": "manual"}})
        write_overrides(tmp_path, {"wake": {"phrase": "okay mac"}})
        raw = (runtime_overrides_path(tmp_path)).read_text()
        assert 'mode = "manual"' in raw
        assert 'phrase = "okay mac"' in raw

    def test_filters_non_live_fields(self, tmp_path):
        write_overrides(tmp_path, {"planner": {"model": "evil-model"}})
        raw = runtime_overrides_path(tmp_path).read_text()
        assert "planner" not in raw          # planner fields are not live-editable
        assert "evil-model" not in raw

    def test_atomic_and_valid_toml(self, tmp_path):
        for i in range(3):
            write_overrides(tmp_path, {"wake": {"threshold": 0.5 + i / 10}})
        import tomllib
        raw = tomllib.loads(runtime_overrides_path(tmp_path).read_text())
        assert raw["wake"]["threshold"] == 0.7


class TestLiveApply:
    def build(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AURA_DATA_DIR", str(tmp_path))
        return DemoStack(tmp_path)

    def test_apply_live_config_updates_allowed_fields(self, tmp_path, monkeypatch):
        stack = self.build(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        fresh = load_config()
        fresh.laya.confidence_threshold = 0.9
        fresh.wake.threshold = 0.8
        orch.apply_live_config(fresh)
        assert orch.cfg.laya.confidence_threshold == 0.9
        assert orch.cfg.wake.threshold == 0.8

    def test_apply_ignores_restart_only_fields(self, tmp_path, monkeypatch):
        stack = self.build(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        fresh = load_config()
        fresh.planner.model = "should-not-apply"
        orch.apply_live_config(fresh)
        assert orch.cfg.planner.model != "should-not-apply"

    def test_poll_detects_file_change(self, tmp_path, monkeypatch):
        stack = self.build(tmp_path, monkeypatch)
        orch = stack.build_orchestrator()
        orch._remember_mtimes()

        write_overrides(tmp_path, {"wake": {"threshold": 0.7}})
        import os
        os.utime(runtime_overrides_path(tmp_path), (0, 0))  # force mtime change
        orch._poll_config_changes()
        assert orch.cfg.wake.threshold == 0.7


class TestWatchPaths:
    def test_includes_runtime_file(self, tmp_path):
        paths = watch_paths(tmp_path)
        assert runtime_overrides_path(tmp_path) in paths


def test_live_fields_catalog_sane():
    assert "mode" in LIVE_FIELDS["wake"]
    assert "enabled" in LIVE_FIELDS["tts"]
    assert "planner" not in LIVE_FIELDS   # model swaps are restart-only, honestly


def test_type_mismatch_is_skipped_with_warning(tmp_path, capsys):
    """A typo'd value in the user's file must not corrupt the running config."""
    from aura.config import Config, _apply

    cfg = Config()
    _apply(cfg.wake, {"models": "hey_jarvis"})        # a string where a list belongs
    assert cfg.wake.models == ["hey_jarvis"]           # the default, untouched
    _apply(cfg.laya, {"confidence_threshold": "high"})  # a string where a number belongs
    assert cfg.laya.confidence_threshold == 0.62
    _apply(cfg.safety, {"show_plan_before_run": 1})    # a number where a bool belongs
    assert cfg.safety.show_plan_before_run is False
    assert "ignoring" in capsys.readouterr().err


def test_valid_overrides_still_apply(tmp_path):
    from aura.config import Config, _apply

    cfg = Config()
    _apply(cfg.wake, {"models": ["hey_jarvis", "alexa"], "threshold": 0.7, "enabled": False})
    assert cfg.wake.models == ["hey_jarvis", "alexa"]
    assert cfg.wake.threshold == 0.7
    assert cfg.wake.enabled is False
