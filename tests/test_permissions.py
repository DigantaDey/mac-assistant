"""Permissions: honest detection, graceful off-Mac degradation, live endpoints."""

from __future__ import annotations

import json
import platform
from types import SimpleNamespace

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


class TestAccessibilityRefresh:
    def test_check_uses_fresh_process_after_a_cached_denial(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "_fresh_accessibility_check", lambda: True)
        monkeypatch.setattr(
            permissions, "_load_application_services",
            lambda: pytest.fail("a fresh result should win over the stale process cache"))

        assert permissions.check_accessibility() is True

    def test_check_falls_back_when_fresh_probe_is_unavailable(self, monkeypatch):
        from aura import permissions

        class ApplicationServices:
            @staticmethod
            def AXIsProcessTrusted():
                return False

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "_fresh_accessibility_check", lambda: None)
        monkeypatch.setattr(permissions, "_load_application_services",
                            lambda: ApplicationServices())

        assert permissions.check_accessibility() is False


class TestAccessibilityReporting:
    """The words Aura shows next to the switch — including *which* app holds it.

    TCC attributes Accessibility to the app that owns the process, so an engine
    started from a terminal is granted as that terminal. Saying "not granted"
    without naming it leaves the user staring at a switch that is already on.
    """

    def test_detail_names_the_identity_that_holds_the_grant(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "_fresh_accessibility_check", lambda: True)
        monkeypatch.setattr(permissions, "accessibility_identity", lambda: "Aura")
        detail = permissions.accessibility_detail(True)
        assert "Aura" in detail and "Granted" in detail

    def test_detail_explains_a_terminal_started_engine(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "accessibility_identity", lambda: "Terminal")
        detail = permissions.accessibility_detail(False)
        assert "Terminal" in detail
        assert "Aura.app" in detail          # the actionable part

    def test_detail_reads_the_state_itself_when_not_given_one(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "_fresh_accessibility_check", lambda: True)
        monkeypatch.setattr(permissions, "accessibility_identity", lambda: "Aura")
        assert "Granted" in permissions.accessibility_detail()

    def test_detail_off_mac_is_honest(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: False)
        assert "can't be checked" in permissions.accessibility_detail()

    def test_request_reports_what_it_found(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "_fresh_accessibility_check", lambda: False)
        monkeypatch.setattr(permissions, "accessibility_identity", lambda: "Aura")
        status, message = permissions.request_accessibility()
        assert status == "asked"
        assert "hasn't granted" in message

    def test_identity_walks_the_parent_chain(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "os", SimpleNamespace(getpid=lambda: 100))
        chain = {100: 90, 90: 80, 80: 1}
        monkeypatch.setattr(permissions, "_parent_pid", lambda pid: chain.get(pid, 0))
        paths = {90: "/bin/zsh",
                 80: "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"}
        monkeypatch.setattr(permissions, "_process_path", lambda pid: paths.get(pid, ""))
        assert permissions._resolve_identity() == "Terminal"

    def test_identity_is_none_when_no_app_owns_the_chain(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "os", SimpleNamespace(getpid=lambda: 100))
        monkeypatch.setattr(permissions, "_parent_pid", lambda pid: 1)
        assert permissions._resolve_identity() is None

    def test_identity_is_resolved_once(self, monkeypatch):
        """Walking the chain costs a `ps` per hop; it must not be repeated.

        A process's ancestry never changes, so the answer is fixed for the
        lifetime of the engine — and the Setup panel asks for it on every read.
        """
        from aura import permissions

        calls = []
        monkeypatch.setattr(permissions, "_resolve_identity",
                            lambda: calls.append(1) or "Aura")
        monkeypatch.setattr(permissions, "_identity_resolved", False)
        monkeypatch.setattr(permissions, "_identity_cache", None)
        assert permissions.accessibility_identity() == "Aura"
        assert permissions.accessibility_identity() == "Aura"
        assert len(calls) == 1


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


