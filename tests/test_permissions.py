"""Permissions: honest detection, graceful off-Mac degradation, live endpoints."""

from __future__ import annotations

import json
import platform

import pytest
from conftest import get, post


class TestPermissionsOffMac:
    """On a non-Mac (CI) every check must degrade without lying."""

    @pytest.mark.skipif(platform.system() == "Darwin", reason="needs a non-Mac box")
    def test_checks_degrade(self):
        from aura.permissions import check_accessibility, open_settings, test_automation

        assert check_accessibility() is None
        status, _ = test_automation()
        assert status == "unavailable"
        ok, _ = open_settings("microphone")
        assert ok is False

    @pytest.mark.skipif(platform.system() == "Darwin", reason="needs a non-Mac box")
    def test_unknown_target_refused(self):
        from aura.permissions import open_settings

        ok, _ = open_settings("notification_center")
        assert ok is False


class TestReadinessChecks:
    def test_whisper_off_mac_is_false(self, stack):
        from aura.permissions import check_whisper_cpp

        assert check_whisper_cpp(stack.cfg) is False

    def test_mock_planner_never_counts_as_server(self, stack):
        from aura.permissions import check_planner_server

        stack.cfg.planner.engine = "mock"
        assert check_planner_server(stack.cfg) is False


class TestPermissionsEndpoints:
    def test_snapshot_shape(self, server):
        orch, srv, cfg = server
        status, body = get(f"http://127.0.0.1:{cfg.server.port}/api/permissions")
        assert status == 200
        data = json.loads(body)
        for key in ("platform", "bridge", "microphone", "accessibility",
                    "whisper_cpp", "planner_server", "planner_engine", "model"):
            assert key in data
        # demo stack: dry-run bridge; "planner server" now means "can the
        # Laya decision model run here" — true wherever the package exists.
        from aura.laya import laya_available

        assert data["bridge"] == "dry-run"
        assert data["planner_server"] is laya_available()

    def test_open_settings_endpoint(self, server):
        _, srv, cfg = server
        status, data = post(f"http://127.0.0.1:{cfg.server.port}/api/permissions/open",
                            {"target": "microphone"})
        assert status == 200
        assert "ok" in data and "message" in data

    def test_automation_probe_endpoint(self, server):
        _, srv, cfg = server
        status, data = post(
            f"http://127.0.0.1:{cfg.server.port}/api/permissions/test_automation", {})
        assert status == 200
        assert data["status"] in ("ok", "denied", "unavailable")


