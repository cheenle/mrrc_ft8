#!/usr/bin/env python3
"""Convert docs/new-dxcc-auto-call.md to a styled HTML page (MRRC-FT8 dark theme).

Mirrors website/build_sdd.py's rendering (pandoc + octen.css) for the
standalone new-DXCC auto-call document published under website/zh/.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "docs" / "new-dxcc-auto-call.md"
OUT = ROOT / "website" / "zh" / "new-dxcc-auto-call.html"

TITLE = "新 DXCC 自动呼叫 —— 端到端机制说明"


def apply_scope_classes(html: str) -> str:
    """Add Scope.css classes to pandoc-generated elements."""
    html = re.sub(r'<table>(?![^<]*class=)', '<table class="scope-table">', html, flags=re.IGNORECASE)
    html = re.sub(r'<pre>(?![^<]*class=)', '<pre class="scope-code">', html, flags=re.IGNORECASE)
    html = re.sub(r'<code>(?![^<]*class=)', '<code class="scope-code">', html, flags=re.IGNORECASE)
    return html


def convert(md_path: Path) -> str:
    """Markdown → HTML body via pandoc (same as build_sdd.py)."""
    result = subprocess.run(
        ["pandoc", str(md_path), "-f", "markdown", "-t", "html",
         "--no-highlight", "--wrap=none"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr)
    return apply_scope_classes(result.stdout.strip())


def build_page(body_html: str) -> str:
    """Wrap the converted body in the site chrome (dark theme, zh nav)."""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="MRRC-FT8 新 DXCC 自动呼叫端到端机制说明：解码 → 实体判定 → 自动应答 → QSO → 落库的完整链路、配置、安全与运维观察点。">
    <meta name="theme-color" content="#0a0a0a">
    <title>{TITLE}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="../css/scope.css?v=1">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .sdd-layout {{ display: flex; max-width: 1100px; margin: 0 auto; padding: calc(var(--scope-gn-h) + 2rem) 2rem 2rem; gap: 2rem; }}
        .sdd-content {{ flex: 1; min-width: 0; padding-bottom: 4rem; }}
        .sdd-content h1 {{ font-family: var(--scope-font-mono); font-size: 2rem; font-weight: 700; margin: 2rem 0 0.5rem; letter-spacing: -0.02em; color: var(--scope-text); }}
        .sdd-content h2 {{ font-family: var(--scope-font-mono); font-size: 1.375rem; font-weight: 600; margin: 2rem 0 0.75rem; color: var(--scope-primary); }}
        .sdd-content h3 {{ font-size: 1.125rem; font-weight: 600; margin: 1.5rem 0 0.5rem; color: var(--scope-text); }}
        .sdd-content h4 {{ font-size: 1rem; font-weight: 600; margin: 1.25rem 0 0.5rem; color: var(--scope-text); }}
        .sdd-content p, .sdd-content li {{ color: var(--scope-text-2); line-height: 1.7; margin-bottom: 0.75rem; }}
        .sdd-content table {{ width: 100%; border-collapse: collapse; margin: 1.25rem 0; font-size: 0.875rem; }}
        .sdd-content th, .sdd-content td {{ padding: 0.625rem 0.875rem; text-align: left; border: 1px solid var(--scope-border); }}
        .sdd-content th {{ background: var(--scope-surface-2); color: var(--scope-text); font-weight: 600; }}
        .sdd-content td {{ color: var(--scope-text-2); }}
        .sdd-content code {{ font-family: var(--scope-font-mono); font-size: 0.85em; background: var(--scope-surface); padding: 0.15rem 0.4rem; border-radius: var(--scope-radius); color: var(--scope-primary); }}
        .sdd-content pre {{ background: var(--scope-surface); border: 1px solid var(--scope-border); border-radius: var(--scope-radius); padding: 1rem; overflow-x: auto; margin: 1rem 0; font-size: 0.8125rem; line-height: 1.6; }}
        .sdd-content pre code {{ background: none; padding: 0; color: var(--scope-text-2); }}
        .sdd-content blockquote {{ border-left: 3px solid var(--scope-primary); padding: 0.5rem 1rem; margin: 1rem 0; color: var(--scope-text-muted); font-size: 0.9375rem; background: var(--scope-surface); border-radius: 0 var(--scope-radius) var(--scope-radius) 0; }}
        .sdd-content ul, .sdd-content ol {{ margin-left: 1.5rem; margin-bottom: 1rem; }}
        .sdd-content hr {{ border: none; border-top: 1px solid var(--scope-border); margin: 2rem 0; }}
        .sdd-content a {{ color: var(--scope-primary); }}
        .sdd-content img {{ max-width: 100%; border-radius: var(--scope-radius); margin: 1rem 0; }}
        .doc-fig {{ margin: 1.5rem 0; padding: 1rem; background: var(--scope-surface); border: 1px solid var(--scope-border); border-radius: var(--scope-radius); }}
        .doc-fig svg {{ width: 100%; height: auto; display: block; }}
        .doc-fig figcaption {{ margin-top: 0.75rem; font-size: 0.8125rem; color: var(--scope-text-muted); text-align: center; line-height: 1.5; }}
        @media (max-width: 900px) {{ .sdd-layout {{ flex-direction: column; padding: calc(var(--scope-gn-h) + 1rem) 1rem 1rem; }} }}
    </style>
</head>
<body data-site="mrrc_ft8" class="fx-grid">

<nav class="scope-site-nav">
    <div class="container scope-site-nav-inner">
        <a href="../index.html" class="scope-site-brand fx-glow">
            <i class="fas fa-satellite-dish"></i>
            <span>MRRC-FT<span style="color:var(--scope-primary)">8</span></span>
        </a>
        <ul class="scope-site-nav-links">
            <li><a href="../index.html#features">功能</a></li>
            <li><a href="../sdd.html">SDD 文档</a></li>
            <li><a href="https://github.com/cheenle/mrrc_ft8" target="_blank" rel="noopener"><i class="fab fa-github"></i> GitHub</a></li>
        </ul>
        <div style="display:flex;align-items:center;gap:0.75rem;">
            <a href="../index.html" class="scope-btn" style="padding:0.4rem 0.8rem;font-size:0.75rem;">English</a>
            <button class="scope-site-nav-toggle" aria-label="Toggle menu"><i class="fas fa-bars"></i></button>
        </div>
    </div>
</nav>

<main class="sdd-layout">
    <div class="sdd-content">
{body_html}
    </div>
</main>

<footer class="scope-footer" style="margin-top: 0;">
    <div class="container">
        <div class="footer-bottom" style="border-top:none;padding-top:0;">
            <p>&copy; 2026 MRRC-FT8 Project · <a href="https://github.com/cheenle/mrrc_ft8">GitHub</a></p>
        </div>
    </div>
</footer>

<script src="../js/scope.js?v=1" defer></script>
</body>
</html>"""


IMG_SVG_RE = re.compile(r'<img src="(\.\./images/[^"]+\.svg)" alt="([^"]*)"\s*/?>')


def inline_svg_figures(html: str, out_path: Path) -> str:
    def _sub(m: re.Match[str]) -> str:
        svg_path = (out_path.parent / m.group(1)).resolve()
        if not svg_path.is_file():
            raise SystemExit(f"missing svg figure: {svg_path}")
        svg = svg_path.read_text(encoding="utf-8").strip()
        caption = f"<figcaption>{m.group(2)}</figcaption>" if m.group(2) else ""
        return f'<figure class="doc-fig">{svg}{caption}</figure>'
    return IMG_SVG_RE.sub(_sub, html)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = convert(MD)
    # pandoc emits the document h1 as the first h1; keep it as the page hero.
    body_html = inline_svg_figures(build_page(body), OUT)
    (OUT).write_text(body_html, encoding="utf-8")
    print(f"  {MD.name} → {OUT} ({len(body)} bytes body)")


if __name__ == "__main__":
    main()
