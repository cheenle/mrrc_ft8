"""JTDX ADIF import: tolerant parser + field mapping (spec §3.2)."""

from __future__ import annotations

from server.engine.adif_import import dedupe_key, map_record, parse_adif

SAMPLE = (
    "<call:6>BG4UCZ <gridsquare:4>PM02 <mode:3>FT8 <rst_sent:3>+00 "
    "<rst_rcvd:3>+04 <qso_date:8>20230227 <time_on:6>005730 "
    "<qso_date_off:8>20230227 <time_off:6>005829 <band:3>20m "
    "<freq:9>14.075500 <station_callsign:5>BG1SB <my_gridsquare:4>ON80 "
    "<tx_pwr:3>100 <comment:3>1st <eor>"
)


def test_parse_adif_skips_header_and_half_written_trailing_line() -> None:
    text = "WSJT-X ADIF Export<eoh>\n" + SAMPLE + "\n" + "<call:6>HALF<eor-not-yet"
    records = parse_adif(text)
    assert len(records) == 1
    assert records[0]["call"] == "BG4UCZ"


def test_parse_adif_drops_malformed_line() -> None:
    text = "garbage-no-tags<eor>\n" + SAMPLE
    records = parse_adif(text)
    assert len(records) == 1


def test_map_record_full_mapping() -> None:
    fields = parse_adif(SAMPLE)[0]
    mapped = map_record(fields, my_call="BG1SB", my_grid="on80da")
    assert mapped is not None
    record, epoch = mapped
    assert record.my_call == "BG1SB"
    assert record.my_grid == "ON80DA"  # normalized uppercase
    assert record.dx_call == "BG4UCZ"
    assert record.dx_grid == "PM02"
    assert (record.report_sent, record.report_rcvd) == (0, 4)  # '+00' '+04'
    assert record.started_utc == "005730"
    assert record.mode == "FT8"
    assert record.freq_hz == 14_075_500
    assert record.band == "20m"
    assert epoch == 1677459509.0  # 2023-02-27 00:58:29 UTC


def test_map_record_tolerates_missing_optional_fields() -> None:
    fields = {
        "call": "JA1YAD",
        "qso_date": "20230301",
        "time_on": "010203",
        "band": "40m",
    }
    mapped = map_record(fields, my_call="M0XX", my_grid="IO91")
    assert mapped is not None
    record, epoch = mapped
    assert record.dx_grid == ""
    assert record.report_sent is None
    assert record.report_rcvd is None
    assert record.mode == "FT8"
    assert record.freq_hz == 0
    assert epoch == 1677632523.0  # 2023-03-01 01:02:03 UTC


def test_map_record_rejects_unparseable() -> None:
    assert map_record({}, my_call="M0XX", my_grid="IO91") is None
    assert map_record(
        {"call": "K1ABC", "qso_date": "bad", "time_on": "nope"},
        my_call="M0XX",
        my_grid="IO91",
    ) is None  # qso_date/time_on must be valid digits


def test_dedupe_key_uses_adif_date() -> None:
    fields = parse_adif(SAMPLE)[0]
    record, epoch = map_record(fields, my_call="BG1SB", my_grid="ON80DA")
    assert record is not None
    key = dedupe_key(record, epoch, fields["qso_date"])
    assert key == ("BG4UCZ", "20230227", "005730", "20m")
