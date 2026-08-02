"""Sequencer log-record → canonical QSO store glue (§7.5, UC-005).

Polled on the composition watchdog: the sequencer holds a completed
``QSORecord`` exactly once and has no locking, so the caller pops it on
the event loop thread; this helper only offloads the blocking repository
write, where the 30-second void window and ADIF export already apply.
"""

from __future__ import annotations

import asyncio

from .repository import Repository
from .sequencer import QSORecord


async def record_qso(repository: Repository, record: QSORecord) -> None:
    """Record one completed QSO; the blocking write runs off the event loop."""

    await asyncio.to_thread(repository.record_qso, record)
