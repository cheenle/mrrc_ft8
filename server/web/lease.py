"""The single control lease with dead-man handling (AD-010, §6, §15.4).

NFR-037: many observer sessions, exactly one renewable control lease; only
the holder may issue transmit-starting actions, while STOP stays open to
every authenticated session (NFR-038, enforced in the API layer).  The
holder must heartbeat every 5 s; the TTL is 15 s.  Lease expiry — and a
controller WS disconnect during active/queued TX, without waiting for the
TTL — invokes the dead-man callback, which the composition layer wires to
``SafetyController.stop_tx``.  A restart creates a fresh service, so a
lease is never restored (§6).

The service is synchronous and framework-free; expiry is driven by calling
:meth:`check_expiry` from a periodic task (about once per second).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

LEASE_TTL_S = 15.0
HEARTBEAT_INTERVAL_S = 5.0


class LeaseEventKind(StrEnum):
    ACQUIRE = "acquire"
    RELEASE = "release"
    EXPIRE = "expire"
    DISCONNECT = "disconnect"


@dataclass(frozen=True, slots=True)
class LeaseEvent:
    """One auditable lease change (§10: lease changes are audited)."""

    kind: LeaseEventKind
    session_id: str
    epoch: float


@dataclass(frozen=True, slots=True)
class Lease:
    """The current control lease and its deadline."""

    session_id: str
    acquired_epoch: float
    expires_epoch: float


DeadManCallback = Callable[[str, str], None]
"""Called as ``(session_id, reason)``; wired to priority STOP TX."""


class LeaseService:
    """Arbitrates the one control lease; fires the dead-man callback."""

    def __init__(
        self,
        *,
        ttl_s: float = LEASE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        on_dead_man: DeadManCallback | None = None,
        on_event: Callable[[LeaseEvent], None] | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("lease TTL must be positive")
        self._ttl = ttl_s
        self._clock = clock
        self._on_dead_man = on_dead_man
        self._on_event = on_event
        self._lease: Lease | None = None
        self._events: list[LeaseEvent] = []

    @property
    def events(self) -> tuple[LeaseEvent, ...]:
        return tuple(self._events)

    def current(self) -> Lease | None:
        """The live lease, or ``None`` when free/expired (no side effects)."""

        lease = self._lease
        if lease is not None and self._clock() >= lease.expires_epoch:
            return None
        return lease

    def is_owner(self, session_id: str | None) -> bool:
        lease = self.current()
        return lease is not None and lease.session_id == session_id

    def expires_in(self) -> float | None:
        """Seconds until the live lease lapses; ``None`` when free."""

        lease = self.current()
        if lease is None:
            return None
        return max(0.0, lease.expires_epoch - self._clock())

    def acquire(self, session_id: str) -> Lease | None:
        """Take the lease when free; renew when already held by the caller."""

        now = self._clock()
        existing = self._lease
        if existing is not None and now < existing.expires_epoch:
            if existing.session_id == session_id:
                return self._renew(now)
            return None  # held by another session: observers stay observers
        if existing is not None:
            self._drop(LeaseEventKind.EXPIRE, fire_dead_man=True)
        self._lease = Lease(session_id, now, now + self._ttl)
        self._emit(LeaseEventKind.ACQUIRE, session_id, now)
        return self._lease

    def heartbeat(self, session_id: str) -> Lease | None:
        """Renew the TTL; only the holder may renew (every 5 s by contract)."""

        if not self.is_owner(session_id):
            return None
        return self._renew(self._clock())

    def release(self, session_id: str) -> bool:
        """Give the lease back early; idempotent for non-holders."""

        if not self.is_owner(session_id):
            return False
        self._drop(LeaseEventKind.RELEASE, fire_dead_man=False)
        return True

    def check_expiry(self) -> bool:
        """Fire the dead-man callback when the TTL lapsed; run periodically."""

        lease = self._lease
        if lease is None or self._clock() < lease.expires_epoch:
            return False
        self._drop(LeaseEventKind.EXPIRE, fire_dead_man=True)
        return True

    def disconnect(self, session_id: str) -> bool:
        """Controller WS gone: invoke STOP immediately, no TTL wait (§15.4)."""

        if not self.is_owner(session_id):
            return False
        self._drop(LeaseEventKind.DISCONNECT, fire_dead_man=True)
        return True

    # ---- internals ---------------------------------------------------------

    def _renew(self, now: float) -> Lease:
        lease = self._lease
        assert lease is not None  # guarded by callers
        self._lease = Lease(lease.session_id, lease.acquired_epoch, now + self._ttl)
        return self._lease

    def _drop(self, kind: LeaseEventKind, *, fire_dead_man: bool) -> None:
        lease, self._lease = self._lease, None
        assert lease is not None
        now = self._clock()
        self._emit(kind, lease.session_id, now)
        if fire_dead_man and self._on_dead_man is not None:
            reason = "lease_expired" if kind is LeaseEventKind.EXPIRE else "controller_disconnect"
            self._on_dead_man(lease.session_id, reason)

    def _emit(self, kind: LeaseEventKind, session_id: str, epoch: float) -> None:
        event = LeaseEvent(kind=kind, session_id=session_id, epoch=epoch)
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)
