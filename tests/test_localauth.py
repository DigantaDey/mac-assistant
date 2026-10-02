"""The engine's front door: token, Host, Origin, and "not a website".

A loopback port is not a security boundary — any process on the Mac, and any
web page you have open, can reach 127.0.0.1. These tests pin the rules that
keep other software from driving Aura behind your back.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request

import pytest
from conftest import auth_headers, get, post

from aura import localauth

# --------------------------------------------------------------------------- #
# The token itself                                                            #
# --------------------------------------------------------------------------- #


class TestTokenFile:
    def test_resolve_creates_a_private_file(self, tmp_path):
        token = localauth.resolve_token(tmp_path)
        assert token and len(token) >= 32
        path = localauth.token_path(tmp_path)
        assert path.is_file()
        # 0600 — the token is a secret; nobody else on the Mac may read it.
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert localauth.read_token(tmp_path) == token

    def test_env_supplied_token_wins_and_is_persisted(self, tmp_path):
        token = localauth.resolve_token(tmp_path, env={"AURA_TOKEN": "from-the-app"})
        assert token == "from-the-app"
        # A later client (or the app after a restart) finds the same secret.
        assert localauth.read_token(tmp_path) == "from-the-app"

    def test_stable_across_runs(self, tmp_path):
        first = localauth.resolve_token(tmp_path)
        assert localauth.resolve_token(tmp_path) == first

    def test_no_auth_escape_hatch(self, tmp_path):
        assert localauth.resolve_token(tmp_path, env={"AURA_NO_AUTH": "1"}) is None

    def test_comparison_never_accepts_empties(self):
        assert localauth.token_matches(None, "secret") is False
        assert localauth.token_matches("", "secret") is False
        assert localauth.token_matches("nope", "secret") is False
        assert localauth.token_matches("secret", "secret") is True
        # No token configured ⇒ the engine is in explicit open mode.
        assert localauth.token_matches(None, None) is True


# --------------------------------------------------------------------------- #
# The rules, over real HTTP                                                   #
# --------------------------------------------------------------------------- #


def _request(url: str, headers: dict | None = None, method: str = "GET",
             body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    return urllib.request.urlopen(req, timeout=5)


class TestAccessRules:
    def test_a_token_is_required(self, server):
        _, srv, cfg = server
        assert srv.token, "production servers always have a token"
        with pytest.raises(urllib.error.HTTPError) as err:
            _request(f"http://127.0.0.1:{cfg.server.port}/api/state")
        assert err.value.code == 401
        body = json.loads(err.value.read())
        assert body["ok"] is False and "error" in body

    def test_a_wrong_token_is_refused(self, server):
        _, srv, cfg = server
        with pytest.raises(urllib.error.HTTPError) as err:
            _request(f"http://127.0.0.1:{cfg.server.port}/api/state",
                     headers={"X-Aura-Token": "not-the-token"})
        assert err.value.code == 401

    def test_the_real_token_works(self, server):
        _, srv, cfg = server
        status, body = get(f"http://127.0.0.1:{cfg.server.port}/api/health")
        assert status == 200 and json.loads(body)["ok"] is True

    def test_a_browser_origin_is_refused_even_with_the_token(self, server):
        """A web page must not be able to drive Aura, token leak or not."""
        _, srv, cfg = server
        with pytest.raises(urllib.error.HTTPError) as err:
            _request(f"http://127.0.0.1:{cfg.server.port}/api/input",
                     headers=auth_headers({"Content-Type": "application/json",
                                           "Origin": "https://evil.example"}),
                     method="POST", body={"text": "empty the trash"})
        assert err.value.code == 403

    def test_a_non_loopback_host_header_is_refused(self, server):
        """DNS rebinding: the name resolves here, but the Host gives it away."""
        _, srv, cfg = server
        with pytest.raises(urllib.error.HTTPError) as err:
            _request(f"http://127.0.0.1:{cfg.server.port}/api/state",
                     headers=auth_headers({"Host": "aura.attacker.example"}))
        assert err.value.code == 403

    def test_localhost_host_header_is_fine(self, server):
        _, srv, cfg = server
        with _request(f"http://127.0.0.1:{cfg.server.port}/api/health",
                      headers=auth_headers({"Host": f"localhost:{cfg.server.port}"})) as r:
            assert r.status == 200

    def test_no_cors_headers_are_ever_sent(self, server):
        _, srv, cfg = server
        with _request(f"http://127.0.0.1:{cfg.server.port}/api/health",
                      headers=auth_headers()) as r:
            assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
            assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_unsupported_methods_get_json_not_html(self, server):
        _, srv, cfg = server
        with pytest.raises(urllib.error.HTTPError) as err:
            _request(f"http://127.0.0.1:{cfg.server.port}/api/state",
                     headers=auth_headers(), method="PUT", body={})
        assert err.value.code == 405
        assert json.loads(err.value.read())["ok"] is False

    def test_refusing_a_remote_bind(self, server):
        """The engine refuses to face the network unless told, loudly, twice."""
        orch, _, cfg = server
        from aura.server import AuraServer

        cfg.server.host = "0.0.0.0"
        cfg.server.port = 0
        with pytest.raises(RuntimeError, match="loopback"):
            AuraServer(orch, cfg, token="x").start()
        # ...and the explicit opt-in lets it through (used by tests/CI only).
        os.environ[localauth.ALLOW_REMOTE_ENV] = "1"
        try:
            srv = AuraServer(orch, cfg, token="x")
            srv.start()
            srv.stop()
        finally:
            os.environ.pop(localauth.ALLOW_REMOTE_ENV, None)


# --------------------------------------------------------------------------- #
# "This is not a website"                                                     #
# --------------------------------------------------------------------------- #


class TestNotAWebsite:
    def test_root_is_json_not_html(self, server):
        _, srv, cfg = server
        status, body = get(f"http://127.0.0.1:{cfg.server.port}/")
        assert status == 200
        payload = json.loads(body)
        assert payload["app"] == "Aura" and payload["surface"] == "engine"
        assert b"<html" not in body.lower()
        assert b"<script" not in body.lower()

    def test_no_static_assets_are_served(self, server):
        _, srv, cfg = server
        for path in ("/app.js", "/app.css", "/index.html", "/../aura/config.py"):
            with pytest.raises(urllib.error.HTTPError) as err:
                get(f"http://127.0.0.1:{cfg.server.port}{path}")
            # /index.html answers with the JSON banner, everything else 404s —
            # nothing ever reads a file from disk.
            assert err.value.code in (200, 404)

    def test_unknown_api_path_is_json_404(self, server):
        _, srv, cfg = server
        with pytest.raises(urllib.error.HTTPError) as err:
            get(f"http://127.0.0.1:{cfg.server.port}/api/nope")
        assert err.value.code == 404


# --------------------------------------------------------------------------- #
# /api/config — what the native Settings screen renders                       #
# --------------------------------------------------------------------------- #


class TestConfigEndpoint:
    def test_live_fields_and_read_only_truth(self, server):
        _, srv, cfg = server
        _, body = get(f"http://127.0.0.1:{cfg.server.port}/api/config")
        snap = json.loads(body)
        assert snap["resolved_profile"] == "demo"
        assert set(snap["live"]) >= {"wake", "tts", "safety", "session"}
        assert snap["live"]["wake"]["mode"] == cfg.wake.mode
        # The brain is a local Laya checkpoint — no model server exists any more.
        assert snap["planner"]["model"] == "laya"
        assert snap["planner"]["base_url"] == ""
        assert snap["files"]["runtime"].endswith("runtime.toml")

    def test_it_reflects_a_saved_change(self, server):
        _, srv, cfg = server
        base = f"http://127.0.0.1:{cfg.server.port}"
        code, res = post(base + "/api/config", {"updates": {"tts": {"enabled": False}}})
        assert code == 200 and res["ok"] is True
        _, body = get(base + "/api/config")
        assert json.loads(body)["live"]["tts"]["enabled"] is False


# --------------------------------------------------------------------------- #
# Install no longer blocks an HTTP request for minutes                        #
# --------------------------------------------------------------------------- #


class TestSetupInstallIsAsync:
    def test_install_answers_immediately(self, server):
        _, srv, cfg = server
        code, res = post(f"http://127.0.0.1:{cfg.server.port}/api/setup/install", {})
        assert code == 200
        # demo profile refuses, but *instantly* and with a reason
        assert res["ok"] is False and "Development build" in res["message"]
