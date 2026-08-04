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
