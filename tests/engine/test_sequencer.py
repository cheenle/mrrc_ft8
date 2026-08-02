from __future__ import annotations

from server.engine.msgparse import parse_message
from server.engine.sequencer import (
    DisarmReason,
    QSOState,
    Sequencer,
)

MY_CALL = "N0CALL"
MY_GRID = "FN42"
DX = "K1ABC"


def make() -> Sequencer:
    return Sequencer(my_call=MY_CALL, my_grid=MY_GRID)


def feed(seq: Sequencer, text: str, snr_db: int | None = None) -> None:
    seq.on_message(parse_message(text), snr_db=snr_db)


def test_cq_side_full_qso() -> None:
    seq = make()
    seq.start_cq()
    assert seq.state == QSOState.CALLING
    # CQ repeats without a retransmission budget (UC-004).
    for _ in range(10):
        assert seq.next_tx_message() == "CQ N0CALL FN42"

    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    assert seq.state == QSOState.REPORT
    assert seq.dx_call == DX
    assert seq.dx_grid == "FN42"
    assert seq.next_tx_message() == "K1ABC N0CALL -12"

    feed(seq, "N0CALL K1ABC R-05")
    assert seq.state == QSOState.ROGERS
    assert seq.next_tx_message() == "K1ABC N0CALL RR73"

    feed(seq, "N0CALL K1ABC 73")
    assert seq.state == QSOState.DONE
    assert seq.disarm_reason == DisarmReason.COMPLETE
    assert seq.next_tx_message() is None

    record = seq.pop_log_record(started_utc="120000", freq_hz=14_074_000, band="20m")
    assert record is not None
    assert record.dx_call == DX
    assert record.dx_grid == "FN42"
    assert record.report_sent == -12
    assert record.report_rcvd == -5
    assert record.started_utc == "120000"
    assert record.freq_hz == 14_074_000
    assert record.band == "20m"
    assert seq.pop_log_record() is None


def test_answerer_full_qso_logs_on_rr73_and_finishes_after_one_73() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-15)
    assert seq.state == QSOState.REPLYING
    assert seq.next_tx_message() == "K1ABC N0CALL FN42"

    feed(seq, "N0CALL K1ABC -09")
    assert seq.state == QSOState.ROGER_REPORT
    assert seq.next_tx_message() == "K1ABC N0CALL R-15"

    feed(seq, "N0CALL K1ABC RR73")
    assert seq.state == QSOState.SIGNOFF
    # UC-005: RR73 already completes and logs the QSO.
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    # Partner stayed silent: the logged QSO is over.
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE
    assert seq.disarm_reason == DisarmReason.COMPLETE

    record = seq.pop_log_record()
    assert record is not None
    assert record.report_sent == -15
    assert record.report_rcvd == -9


def test_answerer_resends_73_only_when_partner_repeats_rr73() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    feed(seq, "N0CALL K1ABC -09")
    feed(seq, "N0CALL K1ABC RR73")
    assert seq.next_tx_message() == "K1ABC N0CALL 73"

    feed(seq, "N0CALL K1ABC RR73")  # partner did not copy our 73
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE


def test_retry_exhaustion_disarms_and_retains_context() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    # One initial send plus three retransmissions (NFR-055).
    for _ in range(4):
        assert seq.next_tx_message() == "K1ABC N0CALL FN42"
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.IDLE
    assert seq.disarm_reason == DisarmReason.RETRY_EXHAUSTED
    assert seq.dx_call == DX  # context retained for the operator
    assert seq.pop_log_record() is None


def test_repeated_report_from_partner_resets_rogers_budget() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC R-05")
    assert seq.state == QSOState.ROGERS
    for _ in range(3):
        assert seq.next_tx_message() == "K1ABC N0CALL RR73"
    # Partner repeats R-05 (did not copy our RR73): budget restarts.
    feed(seq, "N0CALL K1ABC R-05")
    assert seq.state == QSOState.ROGERS
    for _ in range(4):
        assert seq.next_tx_message() == "K1ABC N0CALL RR73"
    assert seq.next_tx_message() is None
    assert seq.disarm_reason == DisarmReason.RETRY_EXHAUSTED
    assert seq.pop_log_record() is None


def test_partner_calling_someone_else_auto_stops() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    assert seq.state == QSOState.REPORT

    feed(seq, "W9XYZ K1ABC FN42")  # partner turned to another station
    assert seq.state == QSOState.IDLE
    assert seq.disarm_reason == DisarmReason.PARTNER_LOST
    assert seq.next_tx_message() is None


def test_third_station_addressing_us_is_ignored_mid_qso() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL W9XYZ -12")  # addressed to us, but not the partner
    assert seq.state == QSOState.REPORT
    assert seq.dx_call == DX


def test_free_text_and_unaddressed_messages_are_ignored() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "TNX BOB")
    feed(seq, "W9XYZ JA1AAA -12")
    feed(seq, "CQ W9XYZ EM57")
    assert seq.state == QSOState.CALLING
    assert seq.dx_call == ""


def test_manual_stop_retains_context_and_blocks_tx() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    seq.stop()
    assert seq.state == QSOState.IDLE
    assert seq.disarm_reason == DisarmReason.MANUAL
    assert seq.dx_call == DX
    assert seq.next_tx_message() is None


def test_rrr_after_report_advances_to_rogers() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC RRR")
    assert seq.state == QSOState.ROGERS
    assert seq.next_tx_message() == "K1ABC N0CALL RR73"


def test_cq_side_accepts_rr73_shortcut_after_report() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC RR73")
    assert seq.state == QSOState.SIGNOFF
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE
    record = seq.pop_log_record()
    assert record is not None
    assert record.report_rcvd is None


def test_done_is_terminal() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC R-05")
    feed(seq, "N0CALL K1ABC 73")
    assert seq.state == QSOState.DONE
    feed(seq, "N0CALL K1ABC 73")
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE


def test_report_formatting_clamps_to_protocol_range() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=99)
    feed(seq, "N0CALL K1ABC -09")
    assert seq.next_tx_message() == "K1ABC N0CALL R+50"

    seq2 = make()
    seq2.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-99)
    feed(seq2, "N0CALL K1ABC -09")
    assert seq2.next_tx_message() == "K1ABC N0CALL R-50"


def test_partner_grid_is_captured_from_first_reply() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC EM57", snr_db=-12)
    assert seq.dx_grid == "EM57"
    record_source = seq.next_tx_message()
    assert record_source == "K1ABC N0CALL -12"


def test_missing_grid_stays_empty_through_completion() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC"), snr_db=-10)
    feed(seq, "N0CALL K1ABC -09")
    feed(seq, "N0CALL K1ABC RR73")
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    assert seq.next_tx_message() is None
    record = seq.pop_log_record()
    assert record is not None
    assert record.dx_grid == ""
