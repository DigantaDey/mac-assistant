"""The local server — zero-dependency HTTP + SSE on the loopback address.

Endpoints:
  GET  /                  the UI (single page, hand-rolled, no build step)
  GET  /api/health        liveness + capability matrix
  GET  /api/state         current snapshot (state, session, skills, config)
  GET  /api/events        SSE stream of the EventBus
  GET  /api/history       recent sessions (for the Activity timeline)
  POST /api/trigger       wake Aura manually (orb / hotkey / tests)
  POST /api/input         submit a typed command
  POST /api/confirm       resolve a proposal  {token}
  POST /api/cancel        resolve a proposal  {token}
  POST /api/correct       timeline feedback    {transcript, skill, verdict, note}
  GET  /api/skills        the skill catalog
  GET  /api/permissions   honest permission + readiness snapshot
  POST /api/wake          live wake-mode switch {mode: manual|openwakeword, phrase?}
  POST /api/wake/train          start a training session {phrase}
  POST /api/wake/train/capture  record the next utterance as a sample
  POST /api/wake/train/finish   train + go live (watches for the phrase)
  POST /api/wake/train/cancel   discard the session
  GET  /api/wake/train          session status
  POST /api/setup/install       in-app component install (progress → SSE)
  GET  /api/metrics       lightweight self-observation (RSS, engines, examples)
  POST /api/permissions/open            {target: microphone|accessibility|automation}
  POST /api/permissions/test_automation  sends one harmless AppleEvent probe

The product binds 127.0.0.1 only. `AURA_HOST` can widen it for development
(the sandboxed preview does this); there is no auth by design, so never
expose it beyond loopback on a real machine.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".woff2": "font/woff2"}


class AuraServer:
    def __init__(self, orch, cfg) -> None:  # noqa: ANN001
        self.orch = orch
        self.cfg = cfg
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- #

    def start(self) -> None:
        orch, cfg = self.orch, self.cfg

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quiet by default
                pass

            def _json(self, obj, status: int = 200) -> None:
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 64_000:
                    return {}
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return {}

            def _file(self, rel: str) -> None:
                path = (UI_DIR / rel).resolve()
                if not str(path).startswith(str(UI_DIR)) or not path.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

            # ---------------- GET ---------------- #

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    self._file("index.html")
                elif path == "/app.css":
                    self._file("app.css")
                elif path == "/app.js":
                    self._file("app.js")
                elif path == "/icon.svg":
                    self._file("icon.svg")
                elif path == "/api/health":
                    self._json({"ok": True, "state": orch.state,
                                "bridge": orch.bridge.platform,
                                "version": _version()})
                elif path == "/api/state":
                    self._json(_state_snapshot(orch, cfg))
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
                elif path == "/api/events":
                    self._sse()
                else:
                    self._json({"error": "not found"}, 404)

            def _sse(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                sid, q = orch.bus.subscribe_queue()
                try:
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

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                body = self._read_body()
                loop = orch.loop

                async def _run(coro):
                    return await asyncio.wait_for(coro, timeout=30)

                if path == "/api/trigger":
                    asyncio.run_coroutine_threadsafe(orch.trigger_manual(), loop)
                    self._json({"ok": True})
                elif path == "/api/input":
                    text = str(body.get("text", ""))[:500]
                    if text:
                        asyncio.run_coroutine_threadsafe(orch.submit_text(text), loop)
                    self._json({"ok": True, "accepted": bool(text)})
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
                        self._json(future.result(timeout=120))
                    except Exception:
                        self._json({"ok": False, "message": "training timed out"}, 503)
                elif path == "/api/setup/install":
                    future = asyncio.run_coroutine_threadsafe(orch.run_setup(), loop)
                    try:
                        self._json(future.result(timeout=30))
                    except Exception:
                        self._json({"ok": False, "message": "install timed out"}, 503)
                else:
                    self._json({"error": "not found"}, 404)

        self._http = ThreadingHTTPServer((cfg.server.host, cfg.server.port), Handler)
        self._http.daemon_threads = True
        self._thread = threading.Thread(target=self._http.serve_forever,
                                        name="aura-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._http:
            self._http.shutdown()
            self._http.server_close()


def _version() -> str:
    from . import __version__
    return __version__


def _permissions_snapshot(orch, cfg) -> dict:  # noqa: ANN001
    """The Setup wizard's data: honest, per-permission state, checked live."""
    from . import permissions as perms

    return {
        "platform": "mac" if perms.is_mac() else "other",
        "profile": cfg.profile,
        "bridge": orch.bridge.platform,
        "microphone": perms.check_microphone(orch),
        "accessibility": perms.check_accessibility(),
        "whisper_cpp": perms.check_whisper_cpp(cfg),
        "planner_server": perms.check_planner_server(cfg),
        "planner_engine": cfg.planner.engine,
        "model": cfg.planner.model,
    }


def _state_snapshot(orch, cfg) -> dict:  # noqa: ANN001
    session = orch.session
    return {
        "state": orch.state,
        "version": _version(),
        "profile": cfg.profile,
        "bridge": orch.bridge.platform,
        "wake_mode": cfg.wake.mode,
        "wake_phrase": cfg.wake.phrase,
        "tts_enabled": cfg.tts.enabled,
        "laya": {
            "backend": type(orch.laya).__name__,
            "examples": orch.examples.stats(),
        },
        "planner": {"engine": cfg.planner.engine, "model": cfg.planner.model,
                    "base_url": cfg.planner.base_url},
        "preferences": orch.memory.all_preferences(),
        "session": None if not session else {
            "id": session.id, "transcript": session.transcript,
            "token": session.proposal_token,
        },
    }
