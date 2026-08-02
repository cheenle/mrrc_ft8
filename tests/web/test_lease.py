"""Control-lease regressions: arbitration, TTL, dead-man STOP (§15.4, NFR-037)."""

from __future__ import annotations

import pytest

from server.web.lease import LeaseEventKind, LeaseService


class FakeClock:
    def __init__(self, now: float = 5_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeadMan:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, session_id: str, reason: str) -> None:
        self.calls.append((session_id, reason))


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def dead_man() -> DeadMan:
    return DeadMan()


@pytest.fixture()
def lease(clock: FakeClock, dead_man: DeadMan) -> LeaseService:
    return LeaseService(clock=clock, on_dead_man=dead_man)


def test_acquire_grants_only_when_free(lease: LeaseService) -> None:
    granted = lease.acquire("session-a")
    assert granted is not None
    assert lease.is_owner("session-a")
    assert lease.acquire("session-b") is None  # observers stay observers
    assert lease.current() is not None
    assert [e.kind for e in lease.events] == [LeaseEventKind.ACQUIRE]


def test_acquire_by_owner_renews(lease: LeaseService, clock: FakeClock) -> None:
    first = lease.acquire("session-a")
    clock.advance(10.0)
    second = lease.acquire("session-a")
    assert first is not None and second is not None
    assert second.expires_epoch > first.expires_epoch
    assert len(lease.events) == 1  # no duplicate ACQUIRE audit


def test_heartbeat_renews_only_for_the_holder(lease: LeaseService, clock: FakeClock) -> None:
    lease.acquire("session-a")
    assert lease.heartbeat("session-b") is None
    clock.advance(12.0)  # 3 s before the TTL would lapse
    renewed = lease.heartbeat("session-a")
    assert renewed is not None
    clock.advance(14.0)  # would have expired without the heartbeat
    assert not lease.check_expiry()
    assert lease.is_owner("session-a")


def test_expiry_fires_dead_man_once(lease: LeaseService, clock: FakeClock, dead_man: DeadMan) -> None:
    lease.acquire("session-a")
    clock.advance(15.0)
    assert lease.check_expiry()
    assert dead_man.calls == [("session-a", "lease_expired")]
    assert lease.current() is None
    assert not lease.check_expiry()  # one shot
    assert [e.kind for e in lease.events] == [
        LeaseEventKind.ACQUIRE,
        LeaseEventKind.EXPIRE,
    ]


def test_disconnect_invokes_stop_without_waiting_for_ttl(
    lease: LeaseService, clock: FakeClock, dead_man: DeadMan
) -> None:
    lease.acquire("session-a")
    clock.advance(2.0)  # far inside the TTL
    assert lease.disconnect("session-a")
    assert dead_man.calls == [("session-a", "controller_disconnect")]
    assert lease.current() is None
    assert [e.kind for e in lease.events][-1] == LeaseEventKind.DISCONNECT


def test_disconnect_by_non_holder_is_ignored(lease: LeaseService, dead_man: DeadMan) -> None:
    lease.acquire("session-a")
    assert not lease.disconnect("session-b")
    assert dead_man.calls == []
    assert lease.is_owner("session-a")


def test_release_frees_without_dead_man(lease: LeaseService, dead_man: DeadMan) -> None:
    lease.acquire("session-a")
    assert lease.release("session-a")
    assert dead_man.calls == []
    assert lease.current() is None
    assert not lease.release("session-a")  # idempotent
    assert lease.acquire("session-b") is not None  # free for the next operator


def test_expired_lease_is_taken_over_with_dead_man(
    lease: LeaseService, clock: FakeClock, dead_man: DeadMan
) -> None:
    lease.acquire("session-a")
    clock.advance(20.0)  # TTL silently lapsed
    granted = lease.acquire("session-b")
    assert granted is not None
    assert dead_man.calls == [("session-a", "lease_expired")]
    assert [e.kind for e in lease.events] == [
        LeaseEventKind.ACQUIRE,
        LeaseEventKind.EXPIRE,
        LeaseEventKind.ACQUIRE,
    ]


def test_restart_never_restores_the_lease(clock: FakeClock, dead_man: DeadMan) -> None:
    first = LeaseService(clock=clock, on_dead_man=dead_man)
    first.acquire("session-a")
    restarted = LeaseService(clock=clock, on_dead_man=dead_man)  # main restart
    assert restarted.current() is None
    assert restarted.acquire("session-b") is not None


def test_event_callback_receives_every_change(clock: FakeClock) -> None:
    seen = []
    service = LeaseService(clock=clock, on_event=seen.append)
    service.acquire("session-a")
    service.release("session-a")
    assert [e.kind for e in seen] == [LeaseEventKind.ACQUIRE, LeaseEventKind.RELEASE]
    assert all(e.session_id == "session-a" for e in seen)


def test_invalid_ttl_is_rejected() -> None:
    with pytest.raises(ValueError):
        LeaseService(ttl_s=0.0)
