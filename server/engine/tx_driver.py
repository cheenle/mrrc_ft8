"""Slot-boundary TX driver: sequencer message → encode → gated transmit.

The orchestrator announces every slot start; this driver transmits only on
the sequencer's current phase (FT8 TX/RX alternation), pulls at most one
message per eligible slot from the sequencer (driving the NFR-055 budget),
encodes it through the supervised Worker and hands the waveform to the safety
controller.  Encode failures are counted and reported through the error
hook (the composition layer latches the DSP interlock); a ``TxRefused``
from the safety controller is only counted — refusal or abort by the
safety authority (STOP, disarm, watchdog, latched interlock) is the safety
system working as designed, and real CAT/audio faults already latch inside
``transmit`` before it raises.  The driver never retries and never touches
PTT itself.

The decision is provisional (I9): when the sequencer is idle at slot start —
the common case before the operator taps a Reply — the driver keeps the
slot's TX window open until ``decision_cutoff`` seconds into the slot, so a
fast manual reply can still transmit in the slot right after the message it
answers, instead of always waiting a full T/R cycle.  The parity is
re-checked at the cutoff, so a reply whose phase does not fit the current
slot simply waits for the next eligible one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .safety import TxRefused
from .sequencer import Sequencer

DEFAULT_TX_AUDIO_FREQUENCY = 1500.0
DEFAULT_TX_PERIOD_SECONDS = 15.0  # FT8 slot; slot_start = slot_id * period
# A 12.64 s FT8 waveform must start by 15 − 12.64 ≈ 2.36 s into the slot;
# the decision deadline leaves ~0.3 s for encode so the waveform still fits.
TX_DECISION_CUTOFF_SECONDS = 2.0


@dataclass
class TxDriver:
    """One-message-per-eligible-slot transmission pump.

    The TX parity follows the sequencer's per-QSO ``tx_phase`` (UC-003): the
    phase is the slot opposite the one the partner was heard in, so a reply to
    an even-slot message transmits on odd slots and vice versa.  CQ always
    runs on the default even phase.  ``period``, ``decision_cutoff``, ``clock``
    and ``sleep`` are injectable so tests stay deterministic.
    """

    sequencer: Sequencer
    encoder: Any       # SupervisorEncoder; duck-typed for tests
    safety: Any        # SafetyController; duck-typed for tests
    tx_audio_frequency: float = DEFAULT_TX_AUDIO_FREQUENCY
    period: float = DEFAULT_TX_PERIOD_SECONDS
    decision_cutoff: float = TX_DECISION_CUTOFF_SECONDS
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    counters: dict[str, int] = field(
        default_factory=lambda: {"tx_attempts": 0, "tx_failed": 0}
    )
    _tx_in_flight: bool = field(default=False, repr=False)

    async def on_slot_start(self, slot_id: int) -> None:
        """Handle one orchestrator slot-start announcement."""

        if self._tx_in_flight:
            # The previous encode is still running (a slow round trip can
            # span a same-parity recurrence); skipping this slot is the
            # correct degradation — the sequencer retransmits next slot.
            return
        # The parity is decided only once the sequencer has a QSO in mind
        # (tx_enabled).  While idle there is no phase yet — a manual Reply
        # armed later in this slot may target either parity, so keep the
        # window open instead of rejecting the slot on the default phase.
        if self.sequencer.tx_enabled and slot_id % 2 != self.sequencer.tx_phase:
            return

        message = self.sequencer.next_tx_message()
        if message is not None:
            await self._transmit(slot_id, message)
            return

        # Provisional (I9): no message ready yet — the operator may still tap
        # a Reply.  Keep this slot's TX window open until the decision cutoff
        # so a fast reply lands in this slot instead of a full cycle later.
        slot_start = slot_id * self.period
        delay = slot_start + self.decision_cutoff - self.clock()
        if delay > 0:
            await self.sleep(delay)
            if self._tx_in_flight:
                return
            # The armed reply may target the other parity; only use this slot
            # when its parity now matches the sequencer's phase.
            if slot_id % 2 != self.sequencer.tx_phase:
                return
            message = self.sequencer.next_tx_message()
            if message is not None:
                await self._transmit(slot_id, message)

    async def _transmit(self, slot_id: int, message: str) -> None:
        self.counters["tx_attempts"] += 1
        self._tx_in_flight = True
        try:
            waveform = await self.encoder.encode(
                message, self.tx_audio_frequency, slot_id=slot_id
            )
            await self.safety.transmit(waveform)
        except TxRefused:
            # Refused or aborted by the safety authority (STOP cancel,
            # disarm, watchdog, latched interlock): not a DSP fault, and
            # real CAT/audio faults already latched inside transmit().
            self.counters["tx_failed"] += 1
        except Exception as error:
            # Encode-path failures mirror the RX path in orchestrator.py:
            # WorkerFault (transport/health) or OSError (shared memory)
            # beyond TxEncodeError; all are counted, none propagate.
            self.counters["tx_failed"] += 1
            self.on_tx_error(slot_id, error)
        finally:
            self._tx_in_flight = False

    def on_tx_error(self, slot_id: int, error: Exception) -> None:
        """Hook for composition-layer audit/logging; default is a no-op."""
