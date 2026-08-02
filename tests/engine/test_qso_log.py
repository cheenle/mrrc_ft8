from __future__ import annotations

import asyncio

from server.engine.msgparse import parse_message
from server.engine.qso_log import record_qso
from server.engine.repository import Repository
from server.engine.sequencer import Sequencer


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def drive_full_qso(sequencer: Sequencer) -> None:
    """CQ side: answer → R+report → RR73 (completes and logs)."""

    sequencer.start_cq()
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.on_message(parse_message("M0XX K1ABC R-10"), snr_db=-10)
    sequencer.on_message(parse_message("M0XX K1ABC RR73"), snr_db=-9)
    sequencer.next_tx_message()  # courtesy 73
    sequencer.next_tx_message()  # partner silent → DONE


def test_completed_qso_is_recorded_once() -> None:
    repository = Repository(":memory:")
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    drive_full_qso(sequencer)
    record = sequencer.pop_log_record()
    assert record is not None
    assert sequencer.pop_log_record() is None  # popped exactly once
    run(record_qso(repository, record))
    qsos = repository.list_qsos()
    assert len(qsos) == 1 and qsos[0].dx_call == "K1ABC"


def test_idle_sequencer_records_nothing() -> None:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    assert sequencer.pop_log_record() is None
