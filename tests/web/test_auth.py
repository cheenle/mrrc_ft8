"""Auth regressions: Argon2id, session lifetimes, re-auth, rate limit (NFR-032..039)."""

from __future__ import annotations

import pytest

from server.web.auth import (
    AuthConfig,
    AuthService,
    hash_password,
    host_allowed,
    origin_allowed,
)

PASSWORD = "correct horse battery staple"


class FakeClock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def auth(clock: FakeClock) -> AuthService:
    return AuthService(hash_password(PASSWORD), clock=clock)


def test_hash_is_argon2id_salted_and_verifiable() -> None:
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first.startswith("$argon2id$")
    assert first != second  # per-hash salt (NFR-032)
    assert PASSWORD not in first
    service = AuthService(first)
    assert service.verify_password(PASSWORD)
    assert not service.verify_password("wrong")
    assert not service.verify_password("")


def test_empty_password_and_hash_are_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("")
    with pytest.raises(ValueError):
        AuthService("")


def test_login_issues_opaque_unique_sessions(auth: AuthService) -> None:
    one = auth.login(PASSWORD).session
    two = auth.login(PASSWORD).session
    assert one is not None and two is not None
    assert one.id != two.id
    assert len(one.id) >= 32
    assert auth.session_count == 2


def test_authenticate_refreshes_idle_timer(auth: AuthService, clock: FakeClock) -> None:
    session = auth.login(PASSWORD).session
    assert session is not None
    clock.advance(1_700.0)  # inside the 30 min idle window
    assert auth.authenticate(session.id) is not None
    clock.advance(1_700.0)  # idle timer refreshed by the previous call
    assert auth.authenticate(session.id) is not None


def test_idle_and_absolute_expiry(clock: FakeClock) -> None:
    auth = AuthService(hash_password(PASSWORD), clock=clock)
    session = auth.login(PASSWORD).session
    assert session is not None
    clock.advance(1_801.0)  # past the idle timeout
    assert auth.authenticate(session.id) is None
    assert auth.session_count == 0

    again = auth.login(PASSWORD).session
    assert again is not None
    for _ in range(30):  # keep it idle-alive but age past 12 h absolute
        clock.advance(1_500.0)
        auth.authenticate(again.id)
    assert auth.authenticate(again.id) is None


def test_logout_is_idempotent(auth: AuthService) -> None:
    session = auth.login(PASSWORD).session
    assert session is not None
    auth.logout(session.id)
    auth.logout(session.id)
    auth.logout(None)
    assert auth.authenticate(session.id) is None


def test_sweep_removes_only_expired(auth: AuthService, clock: FakeClock) -> None:
    old = auth.login(PASSWORD).session
    clock.advance(1_801.0)
    fresh = auth.login(PASSWORD).session
    assert old is not None and fresh is not None
    assert auth.sweep_expired() == 1
    assert auth.authenticate(fresh.id) is not None


def test_failed_logins_face_progressive_bounded_delay(
    auth: AuthService, clock: FakeClock
) -> None:
    first = auth.login("nope")
    assert first.session is None
    assert first.retry_after_s == pytest.approx(1.0)
    blocked = auth.login(PASSWORD)  # even correct credentials wait
    assert blocked.session is None
    assert blocked.retry_after_s > 0
    clock.advance(1.0)
    second = auth.login("nope")
    assert second.retry_after_s == pytest.approx(2.0)
    clock.advance(2.0)
    for expected in (4.0, 8.0, 16.0, 32.0, 60.0, 60.0):  # capped progression
        auth.login("nope")
        clock.advance(60.0 if expected == 60.0 else expected + 10)
    assert auth.login("nope").retry_after_s == pytest.approx(60.0)


def test_successful_login_resets_the_delay(auth: AuthService, clock: FakeClock) -> None:
    auth.login("nope")
    clock.advance(1.0)
    session = auth.login(PASSWORD).session
    assert session is not None
    again = auth.login("nope")  # failure counter was reset
    assert again.retry_after_s == pytest.approx(1.0)


def test_reauthentication_window(auth: AuthService, clock: FakeClock) -> None:
    session = auth.login(PASSWORD).session
    assert session is not None
    assert not auth.has_recent_reauth(session.id)
    assert not auth.reauthenticate(session.id, "wrong password")
    assert auth.reauthenticate(session.id, PASSWORD)
    assert auth.has_recent_reauth(session.id)
    clock.advance(301.0)  # five-minute window closed (NFR-039)
    assert not auth.has_recent_reauth(session.id)
    assert not auth.reauthenticate("no-such-session", PASSWORD)
    assert not auth.has_recent_reauth(None)


ALLOWED = frozenset({"ft8.example.com"})


@pytest.mark.parametrize(
    "host,expected",
    [
        ("ft8.example.com", True),
        ("ft8.example.com:443", True),
        ("FT8.EXAMPLE.COM", True),
        ("evil.example.com", False),
        ("", False),
        (None, False),
    ],
)
def test_host_validation(host: str | None, expected: bool) -> None:
    assert host_allowed(host, ALLOWED) is expected


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("https://ft8.example.com", True),
        ("https://ft8.example.com:443", True),
        ("https://FT8.EXAMPLE.COM/path", True),
        ("https://evil.example.com", False),
        ("ft8.example.com", False),  # scheme required
        ("", False),
    ],
)
def test_origin_validation(origin: str, expected: bool) -> None:
    assert origin_allowed(origin, "ft8.example.com", ALLOWED) is expected


def test_origin_absent_is_allowed_but_bad_host_is_not() -> None:
    assert origin_allowed(None, "ft8.example.com", ALLOWED)
    assert not origin_allowed(None, "evil.example.com", ALLOWED)
    assert not origin_allowed("https://ft8.example.com", "evil.example.com", ALLOWED)
