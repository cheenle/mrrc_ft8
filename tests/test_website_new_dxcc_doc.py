"""Contract tests for the new-DXCC doc build (pandoc wrapper + SVG inlining)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "build_new_dxcc_doc", ROOT / "website" / "build_new_dxcc_doc.py"
)
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


def test_inline_svg_replaces_img_with_figure(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "demo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>',
        encoding="utf-8",
    )
    out = tmp_path / "zh" / "page.html"
    html = '<p>x</p>\n<img src="../images/demo.svg" alt="图 1 演示" />\n<p>y</p>'
    result = build.inline_svg_figures(html, out)
    assert '<figure class="doc-fig">' in result
    assert "<figcaption>图 1 演示</figcaption>" in result
    assert 'viewBox="0 0 10 10"' in result
    assert "<img" not in result


def test_inline_svg_missing_file_fails(tmp_path: Path) -> None:
    out = tmp_path / "zh" / "page.html"
    html = '<img src="../images/nope.svg" alt="x" />'
    with pytest.raises(SystemExit):
        build.inline_svg_figures(html, out)


def test_non_svg_img_untouched(tmp_path: Path) -> None:
    out = tmp_path / "zh" / "page.html"
    html = '<img src="../images/cockpit-ft8.jpg" alt="cockpit" />'
    assert build.inline_svg_figures(html, out) == html
