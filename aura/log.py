"""Aura's logging — one honest answer to "what actually happened?".

The engine has three audiences for the same stream, so there is exactly one
`logging` tree and three sinks hanging off it:

    stderr              → the app captures this into  ~/Library/Logs/Aura.log
    <data_dir>/aura.log → rotating file, survives a crash, greppable anywhere
    the EventBus        → SSE `log` events → the app's Activity feed

Names are hierarchical (`aura.laya`, `aura.planner`, `aura.orchestrator`), so
`AURA_LOG_LEVEL=debug` turns on full detail for the whole engine, and a single
module can be quietened without touching code.

Two product rules shape this file:

1. **Logging must never break the engine.** A handler that raises (a full
   disk, a closed bus) is swallowed: the user's command matters more than the
   record of it.
2. **Errors carry their cause.** `describe_exception()` is what the
   orchestrator puts in front of the user when something fails — a short,
   precise string an engineer can act on ("LayaError: …"), never a bare
   "something went wrong". The full traceback goes to the other two sinks.

`setup()` is idempotent and safe to call from any thread; `attach_bus()` joins
the UI stream once the EventBus exists.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

#: Every logger in the engine is a child of this one.
ROOT = "aura"
LOG_FILE_NAME = "aura.log"

DEFAULT_LEVEL = "INFO"
#: What gets mirrored into the app's Activity feed. INFO by default: the
#: timeline the user can actually read is the point of this module.
DEFAULT_EVENT_LEVEL = "INFO"
FILE_MAX_BYTES = 2_000_000
FILE_BACKUPS = 3

_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

_lock = threading.RLock()
_state: dict[str, Any] = {
    "configured": False,
    "path": None,
    "bus": None,
    "level": DEFAULT_LEVEL,
    "event_level": DEFAULT_EVENT_LEVEL,
    "hooks": False,
}


# --------------------------------------------------------------------------- #
# Level helpers                                                               #
# --------------------------------------------------------------------------- #


def level_name(value: Any, default: str = DEFAULT_LEVEL) -> str:
    """Anything a config file, an env var, or the UI might hold → a level name."""
    if isinstance(value, int):
        return logging.getLevelName(value)
    text = str(value or "").strip().upper()
    return text if text in logging._nameToLevel else default


def _level(value: Any, default: str = DEFAULT_LEVEL) -> int:
    return logging._nameToLevel.get(level_name(value, default), logging.INFO)


def get_logger(name: str = "") -> logging.Logger:
    """`get_logger("laya")` → the `aura.laya` logger."""
    if not name:
        return logging.getLogger(ROOT)
    if name == ROOT or name.startswith(ROOT + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT}.{name}")


log = get_logger()


# --------------------------------------------------------------------------- #
# The three sinks                                                             #
# --------------------------------------------------------------------------- #


class EventBusHandler(logging.Handler):
    """Mirror records into the app's Activity feed over the EventBus.

    Runs on whatever thread emitted the record — the bus is thread-safe by
    contract (`EventBus.publish` schedules onto the loop). A publish failure is
    swallowed here on purpose: a broken UI stream must not take down a session.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        bus = _state.get("bus")
        if bus is None:
            return
        try:
            line = self.format(record)
            bus.publish("log", line=line, level=record.levelname, logger=record.name)
        except Exception:  # pragma: no cover - the bus is the safety net, not the risk
            pass


class _SafeStreamHandler(logging.StreamHandler):
    """A stream handler that stays quiet when the stream is already gone.

    The app closes the engine's stdout on quit; a late record then used to
    print "--- Logging error ---" into a dead pipe. Nothing useful is lost.
    """

    def handleError(self, record: logging.LogRecord) -> None:
        return


def _make_file_handler(path: Path, level: int) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        str(path), maxBytes=FILE_MAX_BYTES, backupCount=FILE_BACKUPS, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    return handler


def _tag(handler: logging.Handler) -> logging.Handler:
    """Mark our handlers so a reconfigure never touches someone else's."""
    handler._aura_handler = True
    return handler


def _remove_our_handlers() -> None:
    logger = logging.getLogger(ROOT)
    for handler in list(logger.handlers):
        if getattr(handler, "_aura_handler", False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:             # pragma: no cover - closing is best effort
                pass


def setup(
    data_dir: str | Path | None = None,
    *,
    level: Any = None,
    event_level: Any = None,
    stream: bool = True,
    file: bool = True,
) -> Path | None:
    """Configure the engine's log tree. Idempotent, never raises.

    Returns the log file's path when one is open (the app shows it behind
    "Open Log"; `/api/log` reads it back with `tail()`).

    Reconfiguring is allowed: pointing `setup()` at a different data directory
    (a test, a user who moved the data folder) swaps the file handler instead
    of quietly keeping the old one.
    """
    with _lock:
        logger = logging.getLogger(ROOT)
        want_level = _level(level if level is not None else os.environ.get("AURA_LOG_LEVEL"),
                            DEFAULT_LEVEL)
        want_event = _level(
            event_level if event_level is not None
            else os.environ.get("AURA_LOG_EVENT_LEVEL"),
            DEFAULT_EVENT_LEVEL)

        want_path: Path | None = None
        if file and data_dir:
            try:
                folder = Path(data_dir).expanduser()
                folder.mkdir(parents=True, exist_ok=True)
                want_path = folder / LOG_FILE_NAME
            except OSError as exc:
                want_path = None
                logger.warning("log file unavailable (%s) — logging to stderr only", exc)

        rebuild = (not _state["configured"]) or str(want_path) != str(_state["path"])
        if rebuild:
            _remove_our_handlers()
            _state["configured"] = True
            _state["path"] = None
            if stream:
                sh = _SafeStreamHandler(sys.stderr)
                sh.setLevel(want_level)
                sh.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
                logger.addHandler(_tag(sh))
            if want_path is not None:
                try:
                    logger.addHandler(_tag(_make_file_handler(want_path, want_level)))
                    _state["path"] = str(want_path)
                except OSError as exc:      # unwritable file, read-only volume…
                    logger.warning("could not open %s: %s", want_path, exc)
            logger.addHandler(_tag(EventBusHandler(want_event)))

        # Levels apply on every call, handlers or not.
        logger.setLevel(logging.DEBUG)      # handlers do the filtering
        for handler in logger.handlers:
            if not getattr(handler, "_aura_handler", False):
                continue
            handler.setLevel(want_event if isinstance(handler, EventBusHandler) else want_level)
        logger.propagate = False
        _state["level"] = logging.getLevelName(want_level)
        _state["event_level"] = logging.getLevelName(want_event)
        install_exception_hooks()
        return Path(_state["path"]) if _state["path"] else None


def attach_bus(bus: Any, event_level: Any = None) -> None:
    """Route log records into the app's Activity feed.

    Called by `cmd_serve` once the EventBus exists; a no-op re-attach is fine.
    """
    with _lock:
        _state["bus"] = bus
        if event_level is not None:
            _state["event_level"] = level_name(event_level, DEFAULT_EVENT_LEVEL)
            for handler in logging.getLogger(ROOT).handlers:
                if isinstance(handler, EventBusHandler) and getattr(handler, "_aura_handler", False):
                    handler.setLevel(_level(_state["event_level"]))


def detach_bus() -> None:
    with _lock:
        _state["bus"] = None


def log_path() -> Path | None:
    """The rotating file this process writes to, when one is open."""
    with _lock:
        return Path(_state["path"]) if _state["path"] else None


def log_status() -> dict[str, Any]:
    """For `doctor`, /api/health and the settings panel."""
    with _lock:
        return {
            "level": _state["level"],
            "event_level": _state["event_level"],
            "file": _state["path"],
            "bus": _state["bus"] is not None,
        }


def tail(limit: int = 200) -> list[str]:
    """The last `limit` lines of the log file — what "Open Log" shows.

    Reads the file rather than a memory buffer so it also contains everything
    written *before* this process started (the app restarts the engine, the
    file does not).
    """
    path = log_path()
    if path is None or not path.is_file():
        return []
    limit = max(1, min(int(limit), 5000))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-limit:]]


# --------------------------------------------------------------------------- #
# Errors an engineer can act on                                               #
# --------------------------------------------------------------------------- #


def describe_exception(exc: BaseException | None, limit: int = 160) -> str:
    """`"AttributeError: 'Router' object has no attribute 'ask'"`.

    This is the string the user is shown when a stage fails, so it names the
    class *and* the message, and it is never empty.
    """
    if exc is None:
        return "no error detail available"
    name = type(exc).__name__
    message = " ".join(str(exc).split())
    if not message:
        message = "<no message>"
    text = f"{name}: {message}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def traceback_text(exc: BaseException | None = None, limit: int | None = None) -> str:
    """A traceback string for the file/stderr sinks."""
    if exc is not None:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__,
                                                  limit=limit)).rstrip()
    return "".join(traceback.format_exc(limit=limit)).rstrip()


def log_exception(message: str, exc: BaseException | None = None, *,
                  logger: logging.Logger | None = None) -> str:
    """Log `message` plus the full traceback; return the short description."""
    detail = describe_exception(exc)
    (logger or get_logger()).error("%s — %s\n%s", message, detail,
                                   traceback_text(exc, limit=12))
    return detail


def install_exception_hooks(loop: Any = None) -> None:
    """Catch what nothing else did: unhandled exceptions in any thread, in a
    callback, or on the asyncio loop.

    Without this a crash in a background thread (an stt rebuild, a model load,
    an executor job) is written to a stderr the app may not be showing.
    """
    with _lock:
        if not _state["hooks"]:
            previous = sys.excepthook

            def _hook(exc_type, exc, tb):
                get_logger().error("unhandled exception\n%s",
                                   "".join(traceback.format_exception(exc_type, exc, tb)))
                previous(exc_type, exc, tb)

            sys.excepthook = _hook
            if hasattr(threading, "excepthook"):
                previous_thread = threading.excepthook

                def _thread_hook(args):
                    get_logger().error(
                        "unhandled exception in thread %s\n%s", args.thread and args.thread.name,
                        "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                           args.exc_traceback)))
                    previous_thread(args)

                threading.excepthook = _thread_hook
            _state["hooks"] = True
    if loop is not None:
        loop.set_exception_handler(_loop_exception_handler)


def _loop_exception_handler(loop: Any, context: dict[str, Any]) -> None:
    exc = context.get("exception")
    message = context.get("message") or "unhandled error on the engine loop"
    if exc is not None:
        get_logger().error("%s — %s\n%s", message, describe_exception(exc),
                           traceback_text(exc, limit=12))
    else:
        get_logger().error("%s (no exception attached)", message)
