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


class TestStaleGrantFromAdHocSigning:
    """A rebuild silently orphans an Accessibility grant when the app is ad-hoc.

    `codesign --sign -` leaves the designated requirement as that build's
    cdhash, so TCC's recorded grant stops matching the new binary while System
    Settings still renders the switch as on. Detecting it is the only way the
    panel can say something the user can act on instead of "try again".
    """

    ADHOC = ("Executable=/Applications/Aura.app/Contents/MacOS/Aura\n"
             "Identifier=app.aura.menubar\n"
             "CodeDirectory v=20400 size=733 flags=0x2(adhoc) hashes=13+3\n"
             "Signature=adhoc\n"
             "TeamIdentifier=not set\n")

    DEVELOPER_ID = ("Executable=/Applications/Aura.app/Contents/MacOS/Aura\n"
                    "Identifier=app.aura.menubar\n"
                    "Authority=Developer ID Application: Aura (TEAMID1234)\n"
                    "TeamIdentifier=TEAMID1234\n")

    def test_adhoc_signature_is_recognised(self, monkeypatch):
        from aura import permissions

        # `codesign -d` writes its report to stderr, not stdout.
        monkeypatch.setattr(permissions.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(stdout="", stderr=self.ADHOC))
        assert permissions._signature_is_adhoc("/Applications/Aura.app") is True

    def test_developer_id_signature_is_not_adhoc(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(stdout="", stderr=self.DEVELOPER_ID))
        assert permissions._signature_is_adhoc("/Applications/Aura.app") is False

    def test_no_signature_report_is_not_a_guess(self, monkeypatch):
        """Unsigned/unreadable must stay None — never a fabricated verdict."""
        from aura import permissions

        monkeypatch.setattr(permissions.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(stdout="", stderr=""))
        assert permissions._signature_is_adhoc("/Applications/Aura.app") is None

    def test_missing_codesign_is_not_a_crash(self, monkeypatch):
        from aura import permissions

        def boom(*a, **k):
            raise OSError("codesign: No such file or directory")

        monkeypatch.setattr(permissions.subprocess, "run", boom)
        assert permissions._signature_is_adhoc("/Applications/Aura.app") is None

    def test_bundle_path_comes_from_the_owning_app(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "is_mac", lambda: True)
        monkeypatch.setattr(permissions, "os", SimpleNamespace(getpid=lambda: 100))
        monkeypatch.setattr(permissions, "_parent_pid", lambda pid: 90)
        monkeypatch.setattr(permissions, "_process_path",
                            lambda pid: "/Applications/Aura.app/Contents/MacOS/Aura")
        assert permissions._app_bundle_path() == "/Applications/Aura.app"

    def test_signature_is_probed_once(self, monkeypatch):
        """The panel reads this on every snapshot; the answer cannot change."""
        from aura import permissions

        calls = []
        monkeypatch.setattr(permissions, "_app_bundle_path",
                            lambda: calls.append(1) or "/Applications/Aura.app")
        monkeypatch.setattr(permissions, "_signature_is_adhoc", lambda b: True)
        monkeypatch.setattr(permissions, "_adhoc_resolved", False)
        monkeypatch.setattr(permissions, "_adhoc_cache", None)
        assert permissions._app_is_adhoc_signed() is True
        assert permissions._app_is_adhoc_signed() is True
        assert len(calls) == 1

    def test_detail_warns_when_a_rebuild_orphaned_the_grant(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "accessibility_identity", lambda: "Aura")
        monkeypatch.setattr(permissions, "_app_is_adhoc_signed", lambda: True)
        detail = permissions.accessibility_detail(granted=False)
        assert "earlier build" in detail
        assert "−" in detail  # the removable row, not "try again"

    def test_detail_stays_quiet_for_a_stable_signature(self, monkeypatch):
        from aura import permissions

        monkeypatch.setattr(permissions, "accessibility_identity", lambda: "Aura")
        monkeypatch.setattr(permissions, "_app_is_adhoc_signed", lambda: False)
        detail = permissions.accessibility_detail(granted=False)
        assert "earlier build" not in detail

    def test_terminal_launch_is_not_blamed_on_signing(self, monkeypatch):
        """A grant filed under Terminal is a different fix; don't conflate them."""
        from aura import permissions

        monkeypatch.setattr(permissions, "accessibility_identity", lambda: "Terminal")
        monkeypatch.setattr(permissions, "_app_is_adhoc_signed", lambda: True)
        detail = permissions.accessibility_detail(granted=False)
        assert "Terminal" in detail
        assert "earlier build" not in detail


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


