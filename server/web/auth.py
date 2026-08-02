"""Password hashing, sessions, re-authentication and rate limiting (§11.3).

NFR-032: Argon2id with per-hash salt; only the hash reaches this service —
no plaintext is stored anywhere.  NFR-033: sessions are random opaque ids
(``secrets``), delivered by the API layer as ``Secure``, ``HttpOnly``,
``SameSite=Strict`` cookies, with 30-minute idle and 12-hour absolute
expiry.  NFR-036: failed logins face a bounded progressive delay.
NFR-039: diagnostic export needs password re-entry inside a five-minute
window.  Session secrets live in this runtime store only and are never
persisted or exported (§7.5, NFR-075).

All methods are synchronous; the async web layer offloads the Argon2id
verification through ``asyncio.to_thread``.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerificationError, VerifyMismatchError

SESSION_ID_BYTES = 32


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Timeouts and abuse limits (NFR-033/036/039)."""

    idle_timeout_s: float = 1_800.0
    absolute_timeout_s: float = 43_200.0
    reauth_window_s: float = 300.0
    delay_base_s: float = 1.0
    delay_cap_s: float = 60.0


@dataclass(slots=True)
class Session:
    """One opaque authenticated session (runtime-only, never exported)."""

    id: str
    created_epoch: float
    touched_epoch: float
    reauth_epoch: float | None = None


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Login attempt outcome; ``retry_after_s`` enforces the delay."""

    session: Session | None
    retry_after_s: float = 0.0


def hash_password(password: str) -> str:
    """Argon2id hash with per-hash salt (NFR-032); for bootstrap tooling."""

    if not password:
        raise ValueError("password must not be empty")
    return PasswordHasher().hash(password)


def host_allowed(host: str | None, allowed_hosts: frozenset[str]) -> bool:
    """NFR-035: the request Host must be one of the configured names."""

    if not host:
        return False
    name = host.split(":", 1)[0].lower()
    return name in {h.lower() for h in allowed_hosts}


def origin_allowed(
    origin: str | None, host: str | None, allowed_hosts: frozenset[str]
) -> bool:
    """NFR-035: Origin (when present) must resolve to an allowed host."""

    if not host or not host_allowed(host, allowed_hosts):
        return False
    if origin is None:
        return True  # non-browser clients; the Host check above still applies
    marker = "://"
    if marker not in origin:
        return False
    origin_host = origin.split(marker, 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    return origin_host == host.split(":", 1)[0].lower()


class AuthService:
    """Runtime session store plus password verification and login delay."""

    def __init__(
        self,
        password_hash: str,
        *,
        config: AuthConfig = AuthConfig(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not password_hash:
            raise ValueError("a bootstrapped password hash is required")
        self._hash = password_hash
        self._config = config
        self._clock = clock
        self._hasher = PasswordHasher()
        self._sessions: dict[str, Session] = {}
        self._failures = 0
        self._blocked_until = 0.0

    # ---- password ---------------------------------------------------------

    def verify_password(self, password: str) -> bool:
        """Constant-time Argon2id verification; offload via to_thread."""

        try:
            return bool(self._hasher.verify(self._hash, password))
        except (VerifyMismatchError, VerificationError, Argon2Error):
            return False

    # ---- login / rate limiting ----------------------------------------------

    def login(self, password: str) -> LoginResult:
        """Verify and issue a session, honoring the progressive delay."""

        now = self._clock()
        if now < self._blocked_until:
            return LoginResult(None, retry_after_s=self._blocked_until - now)
        if not self.verify_password(password):
            self._failures += 1
            delay = min(
                self._config.delay_base_s * 2 ** (self._failures - 1),
                self._config.delay_cap_s,
            )
            self._blocked_until = now + delay
            return LoginResult(None, retry_after_s=delay)
        self._failures = 0
        self._blocked_until = 0.0
        return LoginResult(self._issue(now))

    # ---- sessions -----------------------------------------------------------

    def authenticate(self, session_id: str | None) -> Session | None:
        """Resolve a cookie value to a live session; refreshes the idle timer."""

        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = self._clock()
        if (
            now - session.touched_epoch > self._config.idle_timeout_s
            or now - session.created_epoch > self._config.absolute_timeout_s
        ):
            self._sessions.pop(session_id, None)
            return None
        session.touched_epoch = now
        return session

    def logout(self, session_id: str | None) -> None:
        """Drop one session; idempotent."""

        if session_id:
            self._sessions.pop(session_id, None)

    def sweep_expired(self) -> int:
        """Remove idle/absolute-expired sessions; returns the count."""

        now = self._clock()
        expired = [
            sid
            for sid, s in self._sessions.items()
            if now - s.touched_epoch > self._config.idle_timeout_s
            or now - s.created_epoch > self._config.absolute_timeout_s
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        return len(expired)

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def reauth_window_s(self) -> float:
        """The sensitive-operation window length (NFR-039)."""

        return self._config.reauth_window_s

    # ---- re-authentication ---------------------------------------------------

    def reauthenticate(self, session_id: str, password: str) -> bool:
        """Open the five-minute sensitive-operation window (NFR-039)."""

        session = self.authenticate(session_id)
        if session is None or not self.verify_password(password):
            return False
        session.reauth_epoch = self._clock()
        return True

    def has_recent_reauth(self, session_id: str | None) -> bool:
        """True while the re-auth window is open for this session."""

        session = self.authenticate(session_id)
        return (
            session is not None
            and session.reauth_epoch is not None
            and self._clock() - session.reauth_epoch <= self._config.reauth_window_s
        )

    # ---- internals -------------------------------------------------------------

    def _issue(self, now: float) -> Session:
        session = Session(
            id=secrets.token_urlsafe(SESSION_ID_BYTES),
            created_epoch=now,
            touched_epoch=now,
        )
        self._sessions[session.id] = session
        return session
