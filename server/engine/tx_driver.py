"""Slot-boundary TX driver: sequencer message → encode → gated transmit.

The orchestrator announces every slot start; this driver transmits only on
its configured parity (FT8 TX/RX alternation), pulls at most one message
per eligible slot from the sequencer (driving the NFR-055 budget), encodes
it through the supervised Worker and hands the waveform to the safety
controller.  Encode failures are counted and reported through the error
hook (the composition layer latches the DSP interlock); a ``TxRefused``
from the safety controller is only counted — refusal or abort by the
safety authority (STOP, disarm, watchdog, latched interlock) is the safety
system working as designed, and real CAT/audio faults already latch inside
``transmit`` before it raises.  The driver never retries and never touches
PTT itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .safety import TxRefused
from .sequencer import Sequencer

DEFAULT_TX_AUDIO_FREQUENCY = 1500.0


@dataclass
class TxDriver:
    """One-message-per-eligible-slot transmission pump."""

    sequencer: Sequencer
    encoder: Any       # SupervisorEncoder; duck-typed for tests
    safety: Any        # SafetyController; duck-typed for tests
    tx_audio_frequency: float = DEFAULT_TX_AUDIO_FREQUENCY
    tx_parity: int = 0  # transmit on even slot ids, receive on odd
    counters: dict[str, int] = field(
        default_factory=lambda: {"tx_attempts": 0, "tx_failed": 0}
    )
    _tx_in_flight: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.tx_parity not in (0, 1):
            raise ValueError("tx_parity must be 0 (even slots) or 1 (odd slots)")

    async def on_slot_start(self, slot_id: int) -> None:
        """Handle one orchestrator slot-start announcement."""

        if slot_id % 2 != self.tx_parity:
            return
        if self._tx_in_flight:
            # The previous encode is still running (a slow round trip can
            # span a same-parity recurrence); skipping this slot is the
            # correct degradation — the sequencer retransmits next slot.
            return
        message = self.sequencer.next_tx_message()
        if message is None:
            return
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
