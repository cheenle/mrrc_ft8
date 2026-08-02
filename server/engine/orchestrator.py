"""UTC slot orchestration: slot identity, decode dispatch and decision cutoff.

SDD AD-006 and §9.2/§9.4: slot identity is always ``floor(epoch / TRperiod)``
computed from an injectable epoch clock — relative timers may wake the loop
but never define protocol phase.  At every slot boundary, after a short
delivery grace for the real audio blocks to land in the ring, the
orchestrator pulls the just-ended 12 kHz slot from the injected source,
dispatches one decode through the injected decoder and applies the I9
decision cutoff: a batch that arrives after slot end + cutoff is
display-only for that slot, is counted as a deadline miss and is never fed
to the sequencer, so a late result cannot trigger a late TX
(NFR-001/NFR-009).

TX keying is deliberately absent here: the sequencer is only *fed* with
on-time messages.  Arming, TX-slot eligibility and PTT belong to the safety
and TX pipeline slices (SDD chapter 15).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from server.core.models import DecodeBatch, DecodeResult
from server.engine.msgparse import ParsedMessage, parse_message
from server.engine.sequencer import Sequencer

FT8_PERIOD_SECONDS = 15.0
FT4_PERIOD_SECONDS = 7.5
# I9 (SDD §13.4): provisional TX decision cutoff, measured on Apple M2.
DEFAULT_DECISION_CUTOFF_SECONDS = 2.5
# Real audio lands one block (~85 ms) plus scheduling after its sample time;
# reading exactly at the boundary always misses the slot tail (found by the
# foreground/launchd deployment: every slot skipped with a healthy ring).
# Decode dispatch therefore waits this delivery grace past slot end; the
# decision cutoff above still has ~2 s of headroom.
DELIVERY_GRACE_SECONDS = 0.4
SLOT_SAMPLES_NBYTES = 360_000  # exact 12 kHz int16 mono slot (AD-004)


def slot_id_for(epoch: float, period: float = FT8_PERIOD_SECONDS) -> int:
    """Return the UTC slot identity ``floor(epoch / period)`` (AD-006)."""

    return math.floor(epoch / period)


def slot_start_epoch(slot_id: int, period: float = FT8_PERIOD_SECONDS) -> float:
    """Return the epoch at which the given slot starts."""

    return slot_id * period


def slot_parity(slot_id: int) -> int:
    """Return the even/odd TX eligibility parity of a slot (0 = even)."""

    return slot_id % 2


class SlotDecoder(Protocol):
    """One decode of an exact 12 kHz int16 slot, returning a native batch."""

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch: ...


@dataclass(frozen=True, slots=True)
class SlotMessage:
    """One decode result paired with its parsed message structure."""

    result: DecodeResult
    parsed: ParsedMessage


@dataclass(frozen=True, slots=True)
class SlotDecode:
    """One completed slot decode as broadcast to state/decode consumers."""

    slot_id: int
    dispatched_epoch: float
    finished_epoch: float
    late: bool
    batch: DecodeBatch
    messages: tuple[SlotMessage, ...]


@dataclass(slots=True)
class OrchestratorCounters:
    """Running totals behind the NFR-002 deadline visibility."""

    slots_started: int = 0
    slots_skipped: int = 0
    decodes: int = 0
    deadline_misses: int = 0
    decode_errors: int = 0


class Orchestrator:
    """UTC slot loop binding slot source, decoder and sequencer together.

    All time sources are injectable: ``clock`` supplies UTC epoch seconds and
    ``sleep_until`` awaits an epoch; production wiring uses ``time.time`` and
    ``asyncio.sleep``, tests drive a fake clock deterministically.
    """

    def __init__(
        self,
        decoder: SlotDecoder,
        slot_source: Callable[[int], bytes | None],
        sequencer: Sequencer,
        *,
        period: float = FT8_PERIOD_SECONDS,
        decision_cutoff: float = DEFAULT_DECISION_CUTOFF_SECONDS,
        delivery_grace: float = DELIVERY_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep_until: Callable[[float], Awaitable[None]] | None = None,
        on_slot_start: Callable[[int], None] | None = None,
        on_decode: Callable[[SlotDecode], None] | None = None,
        on_decode_error: Callable[[int, Exception], None] | None = None,
    ) -> None:
        if period <= 0 or decision_cutoff <= 0:
            raise ValueError("period and decision cutoff must be positive")
        if decision_cutoff >= period:
            raise ValueError("decision cutoff must be smaller than the slot period")
        if delivery_grace < 0:
            raise ValueError("delivery grace must not be negative")
        self._decoder = decoder
        self._slot_source = slot_source
        self._sequencer = sequencer
        self._period = period
        self._decision_cutoff = decision_cutoff
        self._delivery_grace = delivery_grace
        self._clock = clock
        self._sleep_until = sleep_until or self._asyncio_sleep_until
        self._on_slot_start = on_slot_start
        self._on_decode = on_decode
        self._on_decode_error = on_decode_error
        self.counters = OrchestratorCounters()
        self._running = False
        self._announced_slot: int | None = None

    async def _asyncio_sleep_until(self, epoch: float) -> None:
        delay = epoch - self._clock()
        if delay > 0:
            await asyncio.sleep(delay)

    def stop(self) -> None:
        """Stop the loop after the current boundary wait."""

        self._running = False

    async def run(self) -> None:
        """Run the slot loop until :meth:`stop` or task cancellation."""

        self._running = True
        while self._running:
            current = slot_id_for(self._clock(), self._period)
            boundary = slot_start_epoch(current + 1, self._period)
            if current != self._announced_slot:
                self._announced_slot = current
                self.counters.slots_started += 1
                if self._on_slot_start is not None:
                    self._on_slot_start(current)
            await self._sleep_until(boundary + self._delivery_grace)
            if not self._running:
                break
            await self._slot_ended(current, boundary)

    async def _slot_ended(self, slot_id: int, boundary_epoch: float) -> None:
        samples = self._slot_source(slot_id)
        if samples is None:
            self.counters.slots_skipped += 1
            return
        if len(samples) != SLOT_SAMPLES_NBYTES:
            raise ValueError(
                f"slot source must return exactly {SLOT_SAMPLES_NBYTES} bytes"
            )
        dispatched = self._clock()
        try:
            batch = await self._decoder.decode(slot_id, samples)
        except Exception as error:
            self.counters.decode_errors += 1
            if self._on_decode_error is not None:
                self._on_decode_error(slot_id, error)
            return
        finished = self._clock()
        if batch.slot_id != slot_id:
            error = ValueError(
                f"decode returned slot {batch.slot_id}, expected {slot_id}"
            )
            self.counters.decode_errors += 1
            if self._on_decode_error is not None:
                self._on_decode_error(slot_id, error)
            return

        late = finished > boundary_epoch + self._decision_cutoff
        messages = tuple(
            SlotMessage(result, parse_message(result.text))
            for result in batch.results
        )
        self.counters.decodes += 1
        if late:
            # NFR-001/NFR-009: display-only; never fed to the sequencer.
            self.counters.deadline_misses += 1
        else:
            for message in messages:
                self._sequencer.on_message(
                    message.parsed, snr_db=message.result.snr
                )
        if self._on_decode is not None:
            self._on_decode(
                SlotDecode(
                    slot_id=slot_id,
                    dispatched_epoch=dispatched,
                    finished_epoch=finished,
                    late=late,
                    batch=batch,
                    messages=messages,
                )
            )
