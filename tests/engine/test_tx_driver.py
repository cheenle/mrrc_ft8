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


def test_invalid_tx_parity_raises() -> None:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    with pytest.raises(ValueError):
        TxDriver(sequencer, FakeEncoder(), FakeSafety(), tx_parity=2)  # type: ignore[arg-type]


def test_retry_exhaustion_stops_transmissions() -> None:
    """CQ repeats forever by design (UC-004); the budget bounds QSO messages."""

    sequencer, driver = make_driver(FakeEncoder(), FakeSafety())
    sequencer.max_retransmissions = 0  # budget: one send only
    sequencer.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-12)
    run(driver.on_slot_start(0))  # Tx1 sent
    run(driver.on_slot_start(2))  # budget exhausted → RETRY_EXHAUSTED, no encode
    assert len(driver.encoder.calls) == 1
    assert sequencer.tx_enabled is False
