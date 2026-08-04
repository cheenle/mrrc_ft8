"""cty.dat parsing + callsign→DXCC lookup (spec §3.1)."""

from __future__ import annotations

from server.engine.dxcc import CtyDatabase, load_cty

# 精简 fixture：实体行 + 跨行续行 + =精确 + (23)数字替换
FIXTURE = """\
Sov Mil Order of Malta:   15:  28:  EU:   41.90:   -12.43:    -1.0:  1A:
    1A;
China:                    24:  44:  AS:   36.00:  -102.00:    -8.0:  BY:
    3H0(23)[42],BI,BJ,BV,=B7P4A,BY;
Monaco:                   14:  27:  EU:   43.73:    -7.40:    -1.0:  3A:
    3A,=3A/4Z5KJ/LH;
"""


def test_load_cty_parses_entities_and_continuation_lines(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert len(db.entities) == 3
    names = [e.name for e in db.entities]
    assert names == ["Sov Mil Order of Malta", "China", "Monaco"]
    china = db.entities[1]
    assert china.continent == "AS"
    # 续行收集：3H0(23)[42] 展开 + BI/BJ/BV/=B7P4A/BY
    assert "3H0" in china.prefixes
    assert "3H2" in china.prefixes
    assert "=B7P4A" in china.prefixes


def test_lookup_prefix_and_continent(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("BI1TX") == ("China", "AS")
    assert db.lookup("BY1OK") == ("China", "AS")
    assert db.lookup("1A0KM") == ("Sov Mil Order of Malta", "EU")
    assert db.lookup("3A2MW") == ("Monaco", "EU")


def test_lookup_exact_match_and_digit_replacement(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    # =B7P4A 精确匹配：B7P4A 整呼号
    assert db.lookup("B7P4A") == ("China", "AS")
    # 3H0(23)[42] → 3H0 / 3H2 / 3H3 前缀
    assert db.lookup("3H0XX") == ("China", "AS")
    assert db.lookup("3H2YY") == ("China", "AS")
    assert db.lookup("3H3ZZ") == ("China", "AS")
    assert db.lookup("3H1QQ") is None  # 数字替换不允许 1


def test_lookup_strips_slash_suffix(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("BI1TX/QRP") == ("China", "AS")  # base = BI1TX
    assert db.lookup("3A2MW/P") == ("Monaco", "EU")


def test_lookup_unknown_returns_none(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("ZZ9ZZZ") is None


# ---- dxcc_summary ---------------------------------------------------------

from server.engine.dxcc import dxcc_summary
from server.engine.repository import Repository
from server.engine.sequencer import QSORecord

FIXTURE2 = """\
China:                    24:  44:  AS:   36.00:  -102.00:    -8.0:  BY:
    BI,BY;
Japan:                    25:  45:  AS:   36.00:    138.00:    -9.0:  JA:
    JA;
Mauritius:                39:  53:  AF:  -20.35:   -57.50:    -4.0:  3B8:
    3B8;
"""


def _repo_with_qsos() -> Repository:
    repo = Repository(":memory:")
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BI1TX", band="20m"),
        completed_epoch=1700000000.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BY1OK", band="20m"),
        completed_epoch=1700000100.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="JA1YAD", band="40m"),
        completed_epoch=1700000200.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="JA1YAD", band="20m"),
        completed_epoch=1700000300.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="3B8CW", band="20m"),
        completed_epoch=1700000400.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="ZZ9ZZZ", band="15m"),
        completed_epoch=1700000500.0,
    )
    return repo


def test_dxcc_summary_counts_entities_bands_and_unmatched(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE2)
    cty = load_cty(str(path))
    summary = dxcc_summary(_repo_with_qsos(), cty)
    assert summary.total == 3                      # China / Japan / Mauritius
    assert summary.unmatched == 1                  # ZZ9ZZZ
    names = [e.name for e in summary.entities]
    assert names == ["China", "Japan", "Mauritius"]  # sorted
    # China: BI1TX + BY1OK 都算一个实体；同实体同波段 20m 只计 1
    china = summary.entities[0]
    assert china.continent == "AS"
    assert china.band_count == 1                   # 只有 20m
    assert china.first_utc == "2023-11-14T22:13:20Z"  # 1700000000
    japan = summary.entities[1]
    assert japan.band_count == 2                   # 40m + 20m
    assert japan.bands == ["20m", "40m"]           # sorted
    # by_band：20m → China+Japan+Mauritius = 3；40m → Japan = 1；15m → 0（unmatched 不计）
    assert summary.by_band == {"20m": 3, "40m": 1}


def test_get_cty_database_loads_repo_cty_singleton() -> None:
    from server.engine.dxcc import get_cty_database

    db1 = get_cty_database()
    db2 = get_cty_database()
    assert db1 is db2                      # 单例
    assert len(db1.entities) > 300         # 仓库内 cty.dat（346 实体）
    assert db1.lookup("BI1TX") == ("China", "AS")
