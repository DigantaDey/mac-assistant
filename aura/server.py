"""The engine's local API — headless, loopback-only, token-guarded.

Aura is a native macOS app; this HTTP server is *not* a website. It is the
contract between the SwiftUI shell (and the test suite) and the Python
orchestrator, and it is deliberately boring:

  GET  /                       who am I (JSON banner — no HTML, no assets)
  GET  /api/health             liveness + capability summary
  GET  /api/state              current snapshot (state, session, planner)
  GET  /api/config             live-editable settings + read-only truth
  GET  /api/permissions        honest permission + readiness snapshot
  GET  /api/skills             the skill catalog
  GET  /api/metrics            self-observation (RSS, engines, examples)
  GET  /api/log?tail=200       the engine's own log file (what Activity shows)
  GET  /api/history            recent sessions (Activity timeline)
  GET  /api/wake/train         wake-phrase training session status
  GET  /api/events             SSE stream of the EventBus
  POST /api/trigger            wake Aura (orb click / ⌥Space / tests)
  POST /api/input              submit a typed command            {text}
  POST /api/confirm            approve a proposal                {token}
  POST /api/cancel             decline a proposal                {token}
  POST /api/correct            timeline feedback
  POST /api/config             live settings    {updates: {section: {…}}}
  POST /api/wake               switch wake mode  {mode, phrase?}
  POST /api/wake/train         start training    {phrase}
  POST /api/wake/train/capture record one sample
  POST /api/wake/train/finish  train + go live
  POST /api/wake/train/cancel  discard the session
  POST /api/setup/install      start the component installer (progress → SSE)
  POST /api/setup/step         one installer step {step}
  POST /api/permissions/open           {target}
  POST /api/permissions/request        {target}
  POST /api/permissions/test_automation
  POST /api/system/open                {what: data|logs}

Access rules (see aura/localauth.py): every request needs the `X-Aura-Token`
header, must not carry a browser `Origin`, and must address a loopback Host.
The socket binds `127.0.0.1` only — widening that is an explicit, logged
opt-in (`AURA_ALLOW_REMOTE=1`) and never something the app does.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import localauth
from . import log as log_mod

TOKEN_HEADER = "X-Aura-Token"

#: Sent with every response. Aura is not a browser app: no CORS headers are
#: ever emitted, because no web page should be able to read these answers.
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class AuraServer:
    """Owns the HTTP thread. All product logic stays in the orchestrator."""

    def __init__(self, orch, cfg, token: str | None = None) -> None:
        self.orch = orch
        self.cfg = cfg
        # None → resolve from env/file (and create one if needed).
        self.token: str | None = (
            localauth.resolve_token(cfg.data_dir) if token is None else token
        )
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Fire-and-forget coroutines this server started (a running session,
        # an install). Cancelled on stop() so nothing outlives the engine.
        self._inflight: set = set()

    # ---------------------------------------------------------------- #

    def start(self) -> None:
        orch, cfg, token = self.orch, self.cfg, self.token
        inflight = self._inflight   # captured: `self` in a Handler is the request
        host = cfg.server.host or "127.0.0.1"
        if not localauth.is_loopback_host(host) and not os.environ.get(
            localauth.ALLOW_REMOTE_ENV
        ):
            raise RuntimeError(
                f"refusing to bind {host!r}: Aura's API is loopback-only. "
                f"Set {localauth.ALLOW_REMOTE_ENV}=1 if you really mean it."
            )

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "AuraEngine"

            def log_message(self, fmt, *args):  # quiet unless debugging
                if os.environ.get("AURA_HTTP_DEBUG"):
                    super().log_message(fmt, *args)

            # ---------------- plumbing ---------------- #

            def _json(self, obj: Any, status: int = 200,
                      extra: dict[str, str] | None = None) -> None:
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

            def _deny(self, status: int, message: str, note: str = "") -> None:
                # Drain an unread body and close: a half-read keep-alive
                # connection would desync into the next request.
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if 0 < length <= 64_000:
                        self.rfile.read(length)
                except (OSError, ValueError):
                    pass
                self.close_connection = True
                if note:
                    orch.bus.publish("log", line=f"api: refused {self.path} — {note}")
                self._json({"ok": False, "error": message}, status)

            def _guard(self) -> bool:
                """The front door. Returns True when the request may proceed."""
                origin = (self.headers.get("Origin") or "").strip()
                if origin:
                    self._deny(403, "Aura's engine only answers the Aura app.",
                               f"browser origin {origin!r}")
                    return False

                host = (self.headers.get("Host") or "").strip()
                if not host:
                    self._deny(403, "Aura's engine only answers requests for this machine.",
                               "missing Host header")
                    return False
                hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
                if not localauth.is_loopback_host(hostname):
                    self._deny(403, "Aura's engine only answers requests for this machine.",
                               f"host {host!r}")
                    return False

                if not localauth.token_matches(self.headers.get(TOKEN_HEADER), token):
                    self._deny(401, "Not authorised — the Aura app holds the key for this engine.",
                               "bad or missing token")
                    return False
                return True

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 64_000:
                    return {}
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return {}

            def _run(self, coro, timeout: float | None = None,
                     label: str = "request") -> None:
                """Fire an orchestrator coroutine from this server thread.

                Used only by fire-and-forget endpoints (`/api/input`,
                `/api/trigger`), where the HTTP answer has *already* been
                sent — so there is nobody left to time out. Cancelling here
                tore live sessions apart mid-flight and left the state
                machine wedged (the "Thinking…" forever bug); the
                orchestrator's session watchdog is what bounds the work now.
                """

                async def _wrapped():
                    try:
                        if timeout is None:
                            return await coro
                        return await asyncio.wait_for(coro, timeout=timeout)
                    except Exception as exc:  # never lose a background failure
                        detail = log_mod.log_exception(f"{label} failed", exc)
                        orch.bus.publish("log", line=f"{label} failed: {detail}")

                def _spawn() -> None:
                    task = orch.loop.create_task(_wrapped())
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)

                try:
                    orch.loop.call_soon_threadsafe(_spawn)
                except RuntimeError:           # the engine is shutting down
                    coro.close()              # nothing will ever await this

            # ---------------- GET ---------------- #

            def do_GET(self) -> None:
                if not self._guard():
                    return
                path = urlsplit(self.path).path
                if path == "/":
                    # Deliberately not a UI: Aura is the menu-bar app.
                    self._json({
                        "app": "Aura",
                        "surface": "engine",
                        "version": _version(),
                        "message": ("Aura's engine is running. Use the Aura menu-bar app — "
                                    "there is no browser interface by design."),
                        "endpoints": ["/api/health", "/api/state", "/api/events",
                                      "/api/log"],
                    })
                elif path == "/api/health":
                    laya = _laya_snapshot(orch, cfg)
                    self._json({"ok": True, "state": orch.state,
                                "bridge": orch.bridge.platform,
                                "mic_ready": bool(getattr(orch, "_has_audio", False)),
                                "planner_online": getattr(orch, "planner_online", None),
                                "laya_ready": bool(laya.get("ready")),
                                "laya_backend": laya.get("backend", ""),
                                "laya_error": laya.get("error", ""),
                                "auth": bool(token),
                                "version": _version()})
                elif path == "/api/state":
                    self._json(_state_snapshot(orch, cfg, auth=bool(token)))
                elif path == "/api/config":
                    self._json(_config_snapshot(cfg))
                elif path == "/api/skills":
                    self._json({"skills": orch.registry.specs()})
                elif path == "/api/permissions":
                    self._json(_permissions_snapshot(orch, cfg))
                elif path == "/api/metrics":
                    self._json(orch.metrics())
                elif path == "/api/wake/train":
                    self._json(orch.training_status())
                elif path == "/api/history":
                    self._json({"events": orch.memory.recent_events(100)})
                elif path == "/api/log":
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        tail = int((query.get("tail") or ["200"])[0])
                    except ValueError:
                        tail = 200
                    self._json({"file": str(_log_file()), "path": str(log_mod.log_path() or ""),
                                "lines": log_mod.tail(tail),
                                "status": log_mod.log_status()})
                elif path == "/api/events":
                    self._sse()
                else:
                    self._json({"ok": False, "error": "not found"}, 404)

            def _sse(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.close_connection = True
                self.end_headers()
                sid, q = orch.bus.subscribe_queue()
                try:
                    self.wfile.write(b"retry: 2000\n\n")
                    for ev in orch.bus.recent(20):
                        self._write_sse(ev.seq, ev.type, ev.as_dict()["data"], ev.ts)
                    self.wfile.flush()
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            self._write_sse(ev.seq, ev.type, ev.as_dict()["data"], ev.ts)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    orch.bus.unsubscribe_queue(sid)

            def _write_sse(self, seq: int, type_: str, data: dict, ts: float) -> None:
                payload = json.dumps({"seq": seq, "ts": ts, "type": type_, "data": data})
                self.wfile.write(f"id: {seq}\nevent: {type_}\ndata: {payload}\n\n".encode())

            # ---------------- POST ---------------- #

            def do_POST(self) -> None:
                if not self._guard():
                    return
                path = urlsplit(self.path).path
                body = self._read_body()
                loop = orch.loop      # the closure's orchestrator, not self

                if path == "/api/trigger":
                    self._run(orch.trigger_manual(), label="wake")
                    self._json({"ok": True})
                elif path == "/api/input":
                    text = str(body.get("text", ""))[:500].strip()
                    if not text:
                        self._json({"ok": True, "accepted": False,
                                    "message": "Type a command first."})
                    elif orch.state != "armed":
                        self._json({"ok": True, "accepted": False,
                                    "message": "Aura is still working on the last request."})
                    else:
                        self._run(orch.submit_text(text), label="input")
                        self._json({"ok": True, "accepted": True})
                elif path == "/api/confirm":
                    ok = orch.resolve_confirmation(str(body.get("token", "")), "confirm")
                    self._json({"ok": ok})
                elif path == "/api/cancel":
                    ok = orch.resolve_confirmation(str(body.get("token", "")), "cancel")
                    self._json({"ok": ok})
                elif path == "/api/correct":
                    orch.record_feedback(str(body.get("transcript", ""))[:500],
                                         str(body.get("skill", ""))[:100],
                                         "confirmed" if body.get("verdict") == "good" else "corrected",
                                         str(body.get("note", ""))[:500])
                    self._json({"ok": True})
                elif path == "/api/permissions/open":
                    from . import permissions as perms
                    ok, msg = perms.open_settings(str(body.get("target", "")))
                    self._json({"ok": ok, "message": msg})
                elif path == "/api/permissions/test_automation":
                    from . import permissions as perms
                    status, msg = perms.test_automation()
                    self._json({"status": status, "message": msg})
                elif path == "/api/permissions/request":
                    from . import permissions as perms

                    target = str(body.get("target", ""))
                    if target == "microphone":
                        status, msg = orch.run_blocking(perms.request_microphone)
                        if status == "ok":
                            # Permission can be granted after startup, when the
                            # engine is still holding SilentMic. Attach the real
                            # stream now so Wake Phrase Studio works immediately.
                            future = asyncio.run_coroutine_threadsafe(
                                orch.ensure_microphone(), loop)
                            try:
                                ready, live_msg = future.result(timeout=5)
                                status = "ok" if ready else "denied"
                                msg = live_msg
                            except Exception:
                                future.cancel()
                                status = "denied"
                                msg = "Microphone was allowed, but Aura could not attach it."
                    elif target == "accessibility":
                        status, msg = orch.run_blocking(perms.request_accessibility)
                    elif target == "automation":
                        status, msg = orch.run_blocking(perms.test_automation)
                    else:
                        self._json({"ok": False, "status": "unknown",
                                    "message": f"unknown target {target!r}"}, 400)
                        return
                    self._json({"ok": status in ("ok", "asked"),
                                "status": status, "message": msg})
                elif path == "/api/config":
                    future = asyncio.run_coroutine_threadsafe(
                        orch.set_config(body.get("updates") or {}), loop)
                    try:
                        self._json(future.result(timeout=10))
                    except Exception:
                        self._json({"ok": False, "message": "settings update timed out"}, 503)
                elif path == "/api/system/open":
                    from . import permissions as perms

                    what = str(body.get("what", ""))
                    if what == "data":
                        target = orch.cfg.data_dir
                    elif what == "logs":
                        target = str(_log_file())
                    else:
                        self._json({"ok": False, "message": f"unknown target {what!r}"}, 400)
                        return
                    ok, msg = (perms.open_path(target) if perms.is_mac()
                               else (False, "Opening folders in Finder is a macOS thing"))
                    self._json({"ok": ok, "message": msg})
                elif path == "/api/setup/step":
                    step = str(body.get("step", ""))
                    future = asyncio.run_coroutine_threadsafe(
                        orch.run_setup_step(step), loop)
                    try:
                        result = future.result(timeout=1500)
                        self._json({"ok": result.status in ("ok", "skip"),
                                    "status": result.status, "detail": result.detail,
                                    "key": result.key})
                    except Exception:
                        self._json({"ok": False, "message": "that step timed out"}, 503)
                elif path == "/api/setup/install":
                    # Long install: answer immediately and stream progress over
                    # SSE (setup_progress events). A 30 s HTTP stall was a lie —
                    # the install kept running while the app showed an error.
                    # 200 with ok:false — "understood, but I won't" is the
                    # convention everywhere else in this API, and the app shows
                    # the message verbatim.
                    self._json(orch.start_setup())
                elif path == "/api/wake":
                    mode = str(body.get("mode", ""))
                    phrase = body.get("phrase")
                    future = asyncio.run_coroutine_threadsafe(
                        orch.set_wake_mode(mode, phrase), loop)
                    try:
                        self._json(future.result(timeout=5))
                    except Exception:
                        self._json({"ok": False, "message": "wake switch timed out"}, 503)
                elif path == "/api/wake/train":
                    self._json(orch.training_start(str(body.get("phrase", ""))))
                elif path == "/api/wake/train/capture":
                    self._json(orch.training_capture())
                elif path == "/api/wake/train/cancel":
                    self._json(orch.training_cancel())
                elif path == "/api/wake/train/finish":
                    future = asyncio.run_coroutine_threadsafe(
                        orch.training_finish(), loop)
                    try:
                        self._json(future.result(timeout=4.5))
                    except Exception:
                        future.cancel()
                        self._json({"ok": False,
                                    "message": "Training exceeded five seconds — try again."}, 503)
                else:
                    self._json({"ok": False, "error": "not found"}, 404)

            # Everything else is a method Aura doesn't speak. Answer with JSON
            # rather than an HTML error page (there is no HTML here).
            def _method_not_allowed(self) -> None:
                self._json({"ok": False, "error": f"{self.command} is not supported"}, 405)

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _method_not_allowed

        self._http = ThreadingHTTPServer((host, cfg.server.port), Handler)
        self._http.daemon_threads = True
        self._thread = threading.Thread(target=self._http.serve_forever,
                                        name="aura-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # In-flight sessions and installs go down with the engine — a task
        # left running would keep a cancelled session's coroutine (and its
        # skill subprocesses) alive after the user quit.
        self._cancel_inflight()
        if self._http:
            self._http.shutdown()
            self._http.server_close()
            self._http = None

    def _cancel_inflight(self) -> None:
        tasks, self._inflight = self._inflight, set()
        if not tasks:
            return

        def _cancel() -> None:
            for task in tasks:
                task.cancel()

        try:
            self.orch.loop.call_soon_threadsafe(_cancel)
        except RuntimeError:
            pass                      # the loop is already gone — nothing to do


# --------------------------------------------------------------------------- #
# Snapshots — the shapes the native app renders                               #
# --------------------------------------------------------------------------- #


def _version() -> str:
    from . import __version__
    return __version__


def _log_file():
    from pathlib import Path

    return Path.home() / "Library" / "Logs" / "Aura.log"


def _config_snapshot(cfg) -> dict:
    """Live-editable fields (what the app may change) plus read-only truth."""
    from . import config as config_mod

    live: dict[str, dict[str, Any]] = {}
    for section, allowed in sorted(config_mod.LIVE_FIELDS.items()):
        current = getattr(cfg, section, None)
        if current is None:
            continue
        live[section] = {name: getattr(current, name)
                         for name in sorted(allowed)
                         if hasattr(current, name)}
    return {
        "version": _version(),
        "profile": cfg.profile,
        "resolved_profile": config_mod.resolved_profile(cfg),
        "data_dir": cfg.data_dir,
        "host": cfg.server.host,
        "port": cfg.server.port,
        "live": live,
        "planner": {"engine": cfg.planner.engine, "model": cfg.planner.model,
                    "base_url": cfg.planner.base_url},
        "stt": {"engine": cfg.stt.engine, "language": cfg.stt.language},
        "wake_models": list(cfg.wake.models),
        "files": {
            "user_config": str(config_mod.user_config_path()),
            "runtime": str(config_mod.runtime_overrides_path(cfg.data_dir)),
        },
    }


def _permissions_snapshot(orch, cfg) -> dict:
    """The Setup wizard's data: honest, per-permission state, checked live."""
    from . import config as config_mod
    from . import permissions as perms

    wake_ready, wake_detail = perms.check_wake_models(cfg)
    return {
        "platform": "mac" if perms.is_mac() else "other",
        "profile": cfg.profile,
        "resolved_profile": config_mod.resolved_profile(cfg),
        "bridge": orch.bridge.platform,
        "microphone": perms.check_microphone(orch),
        "accessibility": perms.check_accessibility(),
        "whisper_cpp": perms.check_whisper_cpp(cfg),
        "planner_server": perms.check_planner_server(cfg),
        "planner_engine": cfg.planner.engine,
        "model": cfg.planner.model,
        "wake_models": {"ready": wake_ready, "detail": wake_detail},
    }


def _laya_snapshot(orch, cfg) -> dict:
    """What the decision layer is actually doing — including why it is not."""
    from . import log as log_mod

    status: dict[str, Any] = {}
    try:
        status = dict(orch.laya.status())
    except Exception as exc:                       # introspect honestly, never raise
        status = {"backend": type(orch.laya).__name__, "ready": False,
                  "error": f"{type(exc).__name__}: {exc}"}
    if not status.get("error"):
        status["error"] = log_mod.log_status().get("last_error", "")
    status.setdefault("backend", type(orch.laya).__name__)
    status["confidence_threshold"] = cfg.laya.confidence_threshold
    status["confidence"] = cfg.laya.confidence_threshold   # kept for the app's card
    status["destructive_threshold"] = cfg.laya.destructive_threshold
    status["examples"] = orch.examples.stats()
    status["log_file"] = log_mod.log_status().get("file")
    return status


def _state_snapshot(orch, cfg, auth: bool = True) -> dict:
    session = orch.session
    planner_status = dict(getattr(orch, "planner_status", {}) or {})
    planner_name = type(getattr(orch, "planner", None)).__name__
    return {
        "state": orch.state,
        "version": _version(),
        "profile": cfg.profile,
        "bridge": orch.bridge.platform,
        "wake_mode": cfg.wake.mode,
        "wake_phrase": cfg.wake.phrase,
        "tts_enabled": cfg.tts.enabled,
        "ask_before_run": cfg.safety.show_plan_before_run,
        "data_dir": cfg.data_dir,
        "mic_ready": bool(getattr(orch, "_has_audio", False)),
        "planner_online": getattr(orch, "planner_online", None),
        "auth": auth,
        "laya": _laya_snapshot(orch, cfg),
        "planner": {"engine": cfg.planner.engine, "class": planner_name,
                    "model": cfg.planner.model,
                    "base_url": cfg.planner.base_url,
                    "last_error": planner_status.get("last_error", ""),
                    "ready": planner_status.get("ready"),
                    "routed_by": getattr(getattr(session, "plan", None), "routed_by", "")},
        "preferences": orch.memory.all_preferences(),
        "session": None if not session else {
            "id": session.id, "transcript": session.transcript,
            "token": session.proposal_token,
        },
    }
