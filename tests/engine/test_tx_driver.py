from __future__ import annotations

import asyncio

import numpy as np
import pytest

from server.core.supervisor import WorkerFault
from server.engine.dsp_encode import TxEncodeError
from server.engine.msgparse import parse_message
from server.engine.safety import TxRefused
from server.engine.sequencer import Sequencer
from server.engine.tx_driver import TxDriver

WAVEFORM = np.zeros(606_720, dtype=np.float32)


class FakeEncoder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, float, int]] = []
        self.error = error

    async def encode(self, message: str, frequency: float, *, slot_id: int) -> np.ndarray:
        self.calls.append((message, frequency, slot_id))
        if self.error is not None:
            raise self.error
        return WAVEFORM


class FakeSafety:
    def __init__(self, error: Exception | None = None) -> None:
        self.transmissions: list[np.ndarray] = []
        self.error = error

    async def transmit(self, samples: np.ndarray) -> None:
        if self.error is not None:
            raise self.error
        self.transmissions.append(samples)


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def make_driver(encoder: FakeEncoder, safety: FakeSafety) -> tuple[Sequencer, TxDriver]:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    driver = TxDriver(sequencer, encoder, safety)  # type: ignore[arg-type]
    return sequencer, driver


def test_cq_transmits_on_even_slots_only() -> None:
    sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    sequencer.start_cq()
    run(driver.on_slot_start(0))   # even: TX
    run(driver.on_slot_start(1))   # odd: RX, no TX
    run(driver.on_slot_start(2))   # even: TX
    assert [c[0] for c in driver.encoder.calls] == ["CQ M0XX IO91", "CQ M0XX IO91"]
    assert [c[2] for c in driver.encoder.calls] == [0, 2]
    assert len(driver.safety.transmissions) == 2
    assert driver.counters["tx_attempts"] == 2
    assert driver.counters["tx_failed"] == 0


def test_idle_sequencer_transmits_nothing() -> None:
    _sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    run(driver.on_slot_start(0))
    assert driver.encoder.calls == []


def test_encode_failure_counts_and_does_not_raise() -> None:
    sequencer, driver = make_driver(FakeEncoder(TxEncodeError("dsp_error", "x")), FakeSafety())
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert driver.counters["tx_failed"] == 1
    assert driver.safety.transmissions == []


def test_tx_refused_counts_and_does_not_raise() -> None:
    sequencer, driver = make_driver(FakeEncoder(), FakeSafety(TxRefused("not armed")))
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert driver.counters["tx_failed"] == 1


def test_worker_fault_counts_and_does_not_raise() -> None:
    """Transport/health failures from supervisor.request must not propagate."""

    fault = WorkerFault("ipc_timeout", "worker request timed out", 1)
    sequencer, driver = make_driver(FakeEncoder(fault), FakeSafety())
    sequencer.start_cq()
    run(driver.on_slot_start(0))
    assert driver.counters["tx_failed"] == 1
    assert driver.safety.transmissions == []


def test_overlap_guard_skips_slot_while_tx_in_flight() -> None:
    sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    sequencer.start_cq()
    driver._tx_in_flight = True
    run(driver.on_slot_start(0))
    assert driver.encoder.calls == []
    assert driver.counters["tx_attempts"] == 0


def test_on_tx_error_hook_receives_slot_and_error() -> None:
    sequencer, driver = make_driver(FakeEncoder(TxEncodeError("dsp_error", "x")), FakeSafety())
    sequencer.start_cq()
    recorded: list[tuple[int, Exception]] = []
    driver.on_tx_error = lambda slot_id, error: recorded.append((slot_id, error))  # type: ignore[method-assign]
    run(driver.on_slot_start(0))
    assert len(recorded) == 1
    assert recorded[0][0] == 0
    assert isinstance(recorded[0][1], TxEncodeError)


def test_driver_transmits_on_sequencer_tx_phase() -> None:
    """The TX parity follows the sequencer's QSO phase (UC-003 opposite slot).

    A reply to an even-slot message arms phase 1: the driver transmits on odd
    slots and stays silent on even ones — the fixed tx_parity is gone.
    """

    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    encoder, safety = FakeEncoder(), FakeSafety()
    driver = TxDriver(sequencer, encoder, safety)  # type: ignore[arg-type]
    sequencer.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10, tx_phase=1)
    run(driver.on_slot_start(0))  # even: no TX
    run(driver.on_slot_start(1))  # odd: TX
    run(driver.on_slot_start(2))  # even: no TX
    run(driver.on_slot_start(3))  # odd: retransmission
    assert [c[0] for c in encoder.calls] == ["K1ABC M0XX IO91", "K1ABC M0XX IO91"]
    assert [c[2] for c in encoder.calls] == [1, 3]
    assert driver.counters["tx_attempts"] == 2


class FakeClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_manual_reply_makes_the_current_slot_when_tapped_in_time() -> None:
    """A Reply armed inside the decision window transmits in the slot right
    after the message it answers — not a full T/R cycle later (I9)."""

    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    encoder, safety = FakeEncoder(), FakeSafety()
    clock = FakeClock(30.9)  # on_slot_start(2) fires 0.9 s into slot 2

    async def window_sleep(delay: float) -> None:
        clock.t += delay  # jump to the cutoff
        sequencer.reply_to(parse_message("CQ K1ABC FN42"), -10, tx_phase=0)

    driver = TxDriver(
        sequencer, encoder, safety,  # type: ignore[arg-type]
        clock=clock, sleep=window_sleep,
    )
    run(driver.on_slot_start(2))  # even slot, reply phase 0: fits
    assert [c[0] for c in encoder.calls] == ["K1ABC M0XX IO91"]
    assert [c[2] for c in encoder.calls] == [2]


def test_manual_reply_waits_when_the_slot_parity_does_not_match() -> None:
    """A reply armed with an odd phase must not transmit on an even slot."""

    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    encoder, safety = FakeEncoder(), FakeSafety()
    clock = FakeClock(30.9)

    async def window_sleep(delay: float) -> None:
        clock.t += delay
        sequencer.reply_to(parse_message("CQ K1ABC FN42"), -10, tx_phase=1)

    driver = TxDriver(
        sequencer, encoder, safety,  # type: ignore[arg-type]
        clock=clock, sleep=window_sleep,
    )
    run(driver.on_slot_start(2))  # even slot, but the reply needs odd
    assert encoder.calls == []


def test_reply_past_the_fit_deadline_defers_to_the_next_slot() -> None:
    """A 12.64 s waveform cannot start past ~2.4 s into a 15 s slot: a Reply
    armed after that must not overrun — it waits for the next eligible slot."""

    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    encoder, safety = FakeEncoder(), FakeSafety()
    clock = FakeClock(33.0)  # 3 s into slot 2: past the fit deadline

    async def never_sleep(delay: float) -> None:
        raise AssertionError("fit guard must return before sleeping")

    driver = TxDriver(
        sequencer, encoder, safety,  # type: ignore[arg-type]
        clock=clock, sleep=never_sleep,
    )
    run(driver.on_slot_start(2))
    assert encoder.calls == []  # deferred; the next slot will transmit
    assert driver.counters["tx_attempts"] == 0


def test_no_reply_within_the_window_transmits_nothing() -> None:
    """If the operator never taps, the open window just closes silently."""

    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    encoder, safety = FakeEncoder(), FakeSafety()
    clock = FakeClock(30.9)

    async def window_sleep(delay: float) -> None:
        clock.t += delay  # nobody taps

    driver = TxDriver(
        sequencer, encoder, safety,  # type: ignore[arg-type]
        clock=clock, sleep=window_sleep,
    )
    run(driver.on_slot_start(2))
    assert encoder.calls == []
    assert driver.counters["tx_attempts"] == 0


def test_retry_exhaustion_stops_transmissions() -> None:
    """CQ repeats forever by design (UC-004); the budget bounds QSO messages."""

    sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    sequencer.max_retransmissions = 0  # budget: one send only
    sequencer.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-12)
    run(driver.on_slot_start(0))  # Tx1 sent
    run(driver.on_slot_start(2))  # budget exhausted → RETRY_EXHAUSTED, no encode
    assert len(driver.encoder.calls) == 1
    assert sequencer.tx_enabled is False


def test_tx_refused_does_not_report_dsp_fault() -> None:
    """Safety refusals/aborts (STOP cancel, disarm, watchdog) are not DSP faults.

    Regression: an in-flight playback cancelled by the dead-man STOP used to
    flow through on_tx_error into the DSP interlock latch, refusing every
    subsequent arm until a manual clear.
    """

    sequencer, driver = make_driver(
        FakeEncoder(), FakeSafety(TxRefused("playback cancelled"))
    )
    sequencer.start_cq()
    recorded: list[tuple[int, Exception]] = []
    driver.on_tx_error = lambda slot_id, error: recorded.append((slot_id, error))  # type: ignore[method-assign]
    run(driver.on_slot_start(0))
    assert driver.counters["tx_failed"] == 1
    assert recorded == []
