from __future__ import annotations

import asyncio

import pytest

from server.core.models import DecodeBatch, DecodePath, DecodeResult
from server.engine.orchestrator import (
    DEFAULT_DECISION_CUTOFF_SECONDS,
    DELIVERY_GRACE_SECONDS,
    FT4_PERIOD_SECONDS,
    FT8_PERIOD_SECONDS,
    Orchestrator,
    SlotDecode,
    slot_id_for,
    slot_parity,
    slot_start_epoch,
)
from server.engine.sequencer import QSOState, Sequencer


class FakeClock:
    def __init__(self, epoch: float) -> None:
        self.epoch = epoch

    def __call__(self) -> float:
        return self.epoch

    def advance(self, seconds: float) -> None:
        self.epoch += seconds

    async def sleep_until(self, epoch: float) -> None:
        self.epoch = max(self.epoch, epoch)


def make_result(text: str, snr: int = -10, slot_id: int = 0) -> DecodeResult:
    return DecodeResult(
        slot_id=slot_id,
        sync=1.5,
        snr=snr,
        dt=0.0,
        frequency=1500.0,
        text=text,
        ap_type=0,
        quality=1.0,
        flags=0,
    )


def make_batch(slot_id: int, texts: list[str] | None = None) -> DecodeBatch:
    return DecodeBatch(
        slot_id=slot_id,
        path=DecodePath.IMPROVED,
        results=tuple(make_result(text, slot_id=slot_id) for text in (texts or [])),
        overflow=False,
        elapsed_seconds=0.25,
        deadline_missed=False,
    )


class FakeDecoder:
    def __init__(
        self,
        clock: FakeClock,
        *,
        texts: list[str] | None = None,
        delay: float = 0.0,
        error: Exception | None = None,
        wrong_slot: bool = False,
    ) -> None:
        self.clock = clock
        self.texts = texts or []
        self.delay = delay
        self.error = error
        self.wrong_slot = wrong_slot
        self.calls: list[tuple[int, bytes]] = []

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch:
        self.calls.append((slot_id, samples))
        if self.delay:
            self.clock.advance(self.delay)
        if self.error is not None:
            raise self.error
        return make_batch(slot_id + 1 if self.wrong_slot else slot_id, self.texts)


class Harness:
    """Deterministic orchestrator wiring around a FakeClock."""

    def __init__(
        self,
        *,
        epoch: float = 0.0,
        decoder: FakeDecoder | None = None,
        sequencer: Sequencer | None = None,
        slots: dict[int, bytes | None] | None = None,
    ) -> None:
        self.clock = FakeClock(epoch)
        self.decoder = decoder or FakeDecoder(self.clock)
        self.sequencer = sequencer or Sequencer("N0CALL", "FN42")
        self.events: list[SlotDecode] = []
        self.errors: list[tuple[int, Exception]] = []
        self.started: list[int] = []
        pcm = b"\x00" * 360_000
        if slots is None:
            source = lambda slot_id: pcm
        else:
            source = lambda slot_id: slots.get(slot_id, pcm)
        self.orchestrator = Orchestrator(
            self.decoder,
            source,
            self.sequencer,
            clock=self.clock,
            sleep_until=self.clock.sleep_until,
            on_slot_start=self.started.append,
            on_decode=self._on_decode,
            on_decode_error=self._on_decode_error,
        )
        self._stop_after_decodes: int | None = None
        self._stop_after_errors: int | None = None

    def _on_decode(self, event: SlotDecode) -> None:
        self.events.append(event)
        if self._stop_after_decodes is not None and len(self.events) >= self._stop_after_decodes:
            self.orchestrator.stop()

    def _on_decode_error(self, slot_id: int, error: Exception) -> None:
        self.errors.append((slot_id, error))
        if self._stop_after_errors is not None and len(self.errors) >= self._stop_after_errors:
            self.orchestrator.stop()

    def run(self, *, decodes: int | None = None, errors: int | None = None) -> None:
        self._stop_after_decodes = decodes
        self._stop_after_errors = errors
        asyncio.run(self.orchestrator.run())


def test_slot_math_floor_identity() -> None:
    assert slot_id_for(0.0) == 0
    assert slot_id_for(14.999_999) == 0
    assert slot_id_for(15.0) == 1
    assert slot_id_for(1_755_000_000.0) == 117_000_000
    assert slot_id_for(-0.001) == -1
    assert slot_id_for(7.499_999, FT4_PERIOD_SECONDS) == 0
    assert slot_id_for(7.5, FT4_PERIOD_SECONDS) == 1


def test_slot_start_and_parity() -> None:
    assert slot_start_epoch(0) == 0.0
    assert slot_start_epoch(5) == 75.0
    assert slot_start_epoch(117_000_000) == 1_755_000_000.0
    assert [slot_parity(s) for s in range(4)] == [0, 1, 0, 1]


def test_config_validation() -> None:
    clock = FakeClock(0.0)
    decoder = FakeDecoder(clock)
    sequencer = Sequencer("N0CALL", "FN42")
    source = lambda slot_id: None
    with pytest.raises(ValueError):
        Orchestrator(decoder, source, sequencer, period=0.0)
    with pytest.raises(ValueError):
        Orchestrator(decoder, source, sequencer, decision_cutoff=0.0)
    with pytest.raises(ValueError):
        Orchestrator(
            decoder,
            source,
            sequencer,
            period=FT8_PERIOD_SECONDS,
            decision_cutoff=FT8_PERIOD_SECONDS,
        )


def test_loop_dispatches_each_ended_slot_and_announces_starts() -> None:
    harness = Harness(epoch=7.5)  # mid-slot 0
    harness.decoder.texts = ["CQ K1ABC FN42"]
    harness.run(decodes=3)

    assert [call[0] for call in harness.decoder.calls] == [0, 1, 2]
    assert harness.started == [0, 1, 2]
    assert harness.orchestrator.counters.slots_started == 3
    assert harness.orchestrator.counters.decodes == 3
    assert harness.orchestrator.counters.deadline_misses == 0
    assert harness.orchestrator.counters.decode_errors == 0
    for event in harness.events:
        assert not event.late
        assert event.batch.slot_id == event.slot_id
        assert event.messages[0].parsed.is_cq
        assert event.messages[0].result.snr == -10
        # Dispatch happened at the slot boundary plus the delivery grace.
        assert event.dispatched_epoch == pytest.approx(
            slot_start_epoch(event.slot_id + 1) + DELIVERY_GRACE_SECONDS
        )


def test_on_time_results_are_fed_to_the_sequencer() -> None:
    sequencer = Sequencer("N0CALL", "FN42")
    sequencer.start_cq()
    harness = Harness(sequencer=sequencer)
    harness.decoder.texts = ["N0CALL K1ABC FN42", "CQ W9XYZ EM57"]
    harness.run(decodes=1)

    assert sequencer.state == QSOState.REPORT
    assert sequencer.dx_call == "K1ABC"
    assert sequencer.report_sent == -10  # the decode SNR


def test_late_result_is_display_only_and_never_fed() -> None:
    sequencer = Sequencer("N0CALL", "FN42")
    sequencer.start_cq()
    harness = Harness(sequencer=sequencer)
    harness.decoder.texts = ["N0CALL K1ABC FN42"]
    harness.decoder.delay = DEFAULT_DECISION_CUTOFF_SECONDS + 0.5
    harness.run(decodes=1)

    assert len(harness.events) == 1
    assert harness.events[0].late is True
    assert harness.orchestrator.counters.decodes == 1
    assert harness.orchestrator.counters.deadline_misses == 1
    assert sequencer.state == QSOState.CALLING  # no late-triggered TX state


def test_slot_mismatch_is_an_error_event() -> None:
    harness = Harness()
    harness.decoder.wrong_slot = True
    harness.run(errors=1)

    assert harness.events == []
    assert len(harness.errors) == 1
    assert harness.errors[0][0] == 0
    assert isinstance(harness.errors[0][1], ValueError)
    assert harness.orchestrator.counters.decode_errors == 1
    assert harness.orchestrator.counters.decodes == 0


def test_decoder_exception_is_reported_and_the_loop_continues() -> None:
    harness = Harness()
    harness.decoder.error = RuntimeError("worker gone")
    healthy = FakeDecoder(harness.clock, texts=["CQ K1ABC FN42"])

    original_on_error = harness._on_decode_error

    def on_error(slot_id: int, error: Exception) -> None:
        original_on_error(slot_id, error)
        harness.orchestrator._decoder = healthy  # supervisor restarted upstream

    harness.orchestrator._on_decode_error = on_error
    harness.run(decodes=1)

    assert len(harness.errors) == 1
    assert isinstance(harness.errors[0][1], RuntimeError)
    assert len(harness.events) == 1
    assert harness.events[0].slot_id == 1
    assert harness.orchestrator.counters.decode_errors == 1
    assert harness.orchestrator.counters.decodes == 1


def test_missing_slot_samples_are_skipped_without_decode() -> None:
    pcm = b"\x00" * 360_000
    harness = Harness(slots={0: None, 1: pcm})
    harness.run(decodes=1)

    assert [call[0] for call in harness.decoder.calls] == [1]
    assert harness.orchestrator.counters.slots_skipped == 1
    assert len(harness.events) == 1


def test_wrong_sample_length_fails_loudly() -> None:
    harness = Harness()
    bad = Orchestrator(
        harness.decoder,
        lambda slot_id: b"\x00" * 100,
        harness.sequencer,
        clock=harness.clock,
        sleep_until=harness.clock.sleep_until,
    )
    with pytest.raises(ValueError, match="exactly"):
        asyncio.run(bad.run())
