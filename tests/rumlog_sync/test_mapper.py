"""mapper: Core Data row → FT8 record dict, push ADIF fields, match predicate."""

from __future__ import annotations

from rumlog_sync.mapper import (
    CORE_DATA_EPOCH_OFFSET,
    build_push_adif_fields,
    derive_started_utc,
    map_rumlog_row,
    parse_report,
    same_qso_match,
    to_unix,
)


def test_to_unix_converts_core_data_timestamp() -> None:
    # Core Data timestamps count seconds since 2001-01-01 UTC.
    assert to_unix(978307200) == 978307200 + CORE_DATA_EPOCH_OFFSET


def test_derive_started_utc_formats_hhmmss() -> None:
    assert derive_started_utc(1_677_459_509.0) == "005829"


def test_parse_report_variants() -> None:
    assert parse_report("+00") == 0
    assert parse_report("-15") == -15
    assert parse_report("59") == 59
    assert parse_report("") is None
    assert parse_report("zz") is None


def test_map_rumlog_row_full() -> None:
    row = {
        "Z_PK": 27139,
        "ZCALLSIGN": "TL8GD",
        "ZDATETIME": 1_795_384_049.0 - CORE_DATA_EPOCH_OFFSET,
        "ZQRG": 14.074684,
        "ZBAND": "20m",
        "ZMODE": "FT8",
        "ZLOCATOR": "",
        "ZRSTTX": "-12",
        "ZRSTRX": "-12",
        "ZUUID": bytes.fromhex("C9DE3F473B90413DA8F6049CDFD26A3B"),
    }
    rec = map_rumlog_row(row, my_call="BG1SB", my_grid="ON80DA")
    assert rec["dx_call"] == "TL8GD"
    assert rec["dx_grid"] == ""
    assert rec["mode"] == "FT8"
    assert rec["band"] == "20m"
    assert rec["freq_hz"] == 14_074_684
    assert (rec["report_sent"], rec["report_rcvd"]) == (-12, -12)
    assert rec["rumlog_uuid"] == "C9DE3F473B90413DA8F6049CDFD26A3B"
    assert rec["source"] == "rumlog"
    assert rec["my_call"] == "BG1SB"
    assert rec["my_grid"] == "ON80DA"
    assert rec["completed_epoch"] == 1_795_384_049.0
    assert rec["started_utc"] == derive_started_utc(1_795_384_049.0)


def test_map_rumlog_row_tolerates_garbage_optional_fields() -> None:
    row = {
        "Z_PK": 1,
        "ZCALLSIGN": "JA1YAD",
        "ZDATETIME": 0.0,
        "ZQRG": None,
        "ZBAND": None,
        "ZMODE": None,
        "ZLOCATOR": None,
        "ZRSTTX": None,
        "ZRSTRX": None,
        "ZUUID": None,
    }
    rec = map_rumlog_row(row, my_call="BG1SB", my_grid="ON80DA")
    assert rec["dx_call"] == "JA1YAD"
    assert rec["freq_hz"] == 0
    assert rec["band"] == ""
    assert rec["mode"] == "FT8"  # default
    assert rec["report_sent"] is None
    # Core Data timestamp 0 = 2001-01-01 UTC (unix 978307200).
    assert rec["completed_epoch"] == CORE_DATA_EPOCH_OFFSET


def test_build_push_adif_fields_contains_expected_tags() -> None:
    rec = {
        "dx_call": "BG4UCZ",
        "dx_grid": "PM02",
        "mode": "FT8",
        "band": "20m",
        "freq_hz": 14_075_500,
        "report_sent": 0,
        "report_rcvd": 4,
        "started_utc": "005730",
        "completed_epoch": 1_677_459_509.0,
        "my_call": "BG1SB",
    }
    fields = build_push_adif_fields(rec)
    assert fields["CALL"] == "BG4UCZ"
    assert fields["GRIDSQUARE"] == "PM02"
    assert fields["MODE"] == "FT8"
    assert fields["BAND"] == "20m"
    assert fields["FREQ"] == "14.075500"
    assert fields["RST_SENT"] == "+00"
    assert fields["RST_RCVD"] == "+04"
    assert fields["QSO_DATE"] == "20230227"
    assert fields["TIME_ON"] == "005730"
    assert fields["TIME_OFF"] == "005829"
    assert fields["STATION_CALLSIGN"] == "BG1SB"


def test_same_qso_match_window_and_band() -> None:
    assert same_qso_match("TL8GD", "20m", 100.0, 220.0)  # within 120 s
    assert not same_qso_match("TL8GD", "20m", 100.0, 300.0)  # outside
    assert not same_qso_match("", "20m", 100.0, 150.0)  # empty call
    assert not same_qso_match("TL8GD", "", 100.0, 150.0)  # empty band
    assert not same_qso_match("TL8GD", "20m", 100.0, -50.0)
