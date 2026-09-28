"""Local access control for the engine's HTTP surface.

Aura's engine listens on loopback, but *loopback is not a security boundary*:
any process on the Mac — including a web page you happen to have open, via a
plain `fetch('http://127.0.0.1:7331/api/input')` — can reach a port it can
guess. So the engine behaves like every serious local service does:

  1. a shared secret (the **token**) is required on every request, carried in
     the `X-Aura-Token` header;
  2. the token lives in `<data_dir>/token`, mode 0600, and is handed to the
     app that spawns the engine through the environment (`AURA_TOKEN`);
  3. requests carrying a browser `Origin`, or a non-loopback `Host`, are
     refused outright — that kills cross-site request forgery and DNS
     rebinding without needing CORS at all.

Nothing here is a substitute for the macOS permission system: TCC still
decides what Aura may touch. This is what keeps *other software* from
driving Aura behind your back.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

#: Environment variable the native app uses to hand the token to its child.
TOKEN_ENV = "AURA_TOKEN"
#: Escape hatch for local development: `AURA_NO_AUTH=1 python -m aura serve`.
NO_AUTH_ENV = "AURA_NO_AUTH"
#: Escape hatch for deliberate remote exposure (never used by the app).
ALLOW_REMOTE_ENV = "AURA_ALLOW_REMOTE"

TOKEN_FILENAME = "token"
_TOKEN_BYTES = 32

#: Host header / bind addresses that mean "this machine only". Note that
#: 0.0.0.0 is deliberately *not* here: as a bind address it means every
#: interface, which is exactly what Aura refuses to do by default.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_host(host: str) -> bool:
    """True when a bind address / Host header names this machine only."""
    return (host or "").strip().lower().strip("[]") in LOOPBACK_HOSTS


def token_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / TOKEN_FILENAME


def _write_token_file(path: Path, token: str) -> None:
    """Atomically write the token with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(token + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_token(data_dir: str | Path) -> str | None:
    """The stored token, or None when there isn't a usable one yet."""
    try:
        value = token_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def create_token(data_dir: str | Path) -> str:
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    _write_token_file(token_path(data_dir), token)
    return token


def resolve_token(data_dir: str | Path, env: dict[str, str] | None = None) -> str | None:
    """Decide which token this engine run uses.

    Priority: an explicit ``AURA_NO_AUTH`` opt-out, then ``AURA_TOKEN`` from
    the parent process (the app generates it), then the shared file, then a
    freshly generated one. The file is kept in sync so that a later client —
    or the app after a restart — always finds the same secret.
    """
    env = os.environ if env is None else env
    if env.get(NO_AUTH_ENV):
        return None

    supplied = (env.get(TOKEN_ENV) or "").strip()
    existing = read_token(data_dir)

    if supplied:
        if existing != supplied:
            try:
                _write_token_file(token_path(data_dir), supplied)
            except OSError:
                pass  # in-memory use still works; persistence is a nicety
        return supplied

    if existing:
        return existing
    try:
        return create_token(data_dir)
    except OSError:
        # A read-only data dir must not stop Aura from starting; fall back to
        # a process-local secret (clients then need AURA_TOKEN from the app).
        return secrets.token_urlsafe(_TOKEN_BYTES)


def token_matches(supplied: str | None, expected: str | None) -> bool:
    """Constant-time comparison — never leak the token through timing."""
    if expected is None:
        return True
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected)
