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


def convert(md_path: Path) -> str:
    """Markdown → HTML body via pandoc (same as build_sdd.py)."""
    result = subprocess.run(
        ["pandoc", str(md_path), "-f", "markdown", "-t", "html",
         "--no-highlight", "--wrap=none"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr)
    return result.stdout.strip()


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
    <link rel="stylesheet" href="../css/octen.css?v=4">
    <link rel="stylesheet" href="../css/sunsdrmobile.css?v=1">
    <link rel="stylesheet" href="../css/ft8.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .sdd-layout {{ display: flex; max-width: 1100px; margin: 0 auto; padding: 0 2rem; gap: 2rem; }}
        .sdd-content {{ flex: 1; min-width: 0; padding-bottom: 4rem; }}
        .sdd-content h1 {{ font-size: 2rem; font-weight: 700; margin: 2rem 0 0.5rem; letter-spacing: -0.02em; }}
        .sdd-content h2 {{ font-size: 1.375rem; font-weight: 600; margin: 2rem 0 0.75rem; color: var(--accent); }}
        .sdd-content h3 {{ font-size: 1.125rem; font-weight: 600; margin: 1.5rem 0 0.5rem; }}
        .sdd-content h4 {{ font-size: 1rem; font-weight: 600; margin: 1.25rem 0 0.5rem; }}
        .sdd-content p, .sdd-content li {{ color: var(--text-secondary); line-height: 1.7; margin-bottom: 0.75rem; }}
        .sdd-content table {{ width: 100%; border-collapse: collapse; margin: 1.25rem 0; font-size: 0.875rem; }}
        .sdd-content th, .sdd-content td {{ padding: 0.625rem 0.875rem; text-align: left; border: 1px solid var(--border); }}
        .sdd-content th {{ background: var(--bg-tertiary); color: var(--text-primary); font-weight: 600; }}
        .sdd-content td {{ color: var(--text-secondary); }}
        .sdd-content code {{ font-family: var(--font-mono); font-size: 0.85em; background: var(--bg-tertiary); padding: 1px 6px; border-radius: 3px; color: var(--accent); }}
        .sdd-content pre {{ background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 0.5rem; padding: 1rem; overflow-x: auto; margin: 1rem 0; font-size: 0.8125rem; line-height: 1.6; }}
        .sdd-content pre code {{ background: none; padding: 0; color: var(--text-secondary); }}
        .sdd-content blockquote {{ border-left: 3px solid var(--accent); padding: 0.5rem 1rem; margin: 1rem 0; color: var(--text-muted); font-size: 0.9375rem; background: var(--bg-tertiary); border-radius: 0 0.375rem 0.375rem 0; }}
        .sdd-content ul, .sdd-content ol {{ margin-left: 1.5rem; margin-bottom: 1rem; }}
        .sdd-content hr {{ border: none; border-top: 1px solid var(--border); margin: 2rem 0; }}
        .sdd-content a {{ color: var(--accent); }}
        .sdd-content img {{ max-width: 100%; border-radius: 0.5rem; margin: 1rem 0; }}
        .doc-fig {{ margin: 1.5rem 0; padding: 1rem; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 0.75rem; }}
        .doc-fig svg {{ width: 100%; height: auto; display: block; }}
        .doc-fig figcaption {{ margin-top: 0.75rem; font-size: 0.8125rem; color: var(--text-muted); text-align: center; line-height: 1.5; }}
        @media (max-width: 900px) {{ .sdd-layout {{ flex-direction: column; padding: 0 1rem; }} }}
    </style>
</head>
<body data-site="mrrc_ft8">

<nav class="navbar">
    <div class="container navbar-content">
        <a href="../index.html" class="logo">
            <span class="logo-icon"><i class="fas fa-satellite-dish"></i></span>
            <span>新 DXCC <span style="color: var(--accent);">自动呼叫</span></span>
        </a>
        <ul class="nav-links">
            <li><a href="../index.html#features">功能</a></li>
            <li><a href="../sdd.html">SDD 文档</a></li>
            <li><a href="https://github.com/cheenle/mrrc_ft8" target="_blank" rel="noopener"><i class="fab fa-github"></i> GitHub</a></li>
        </ul>
        <div class="nav-actions">
            <a href="../index.html" class="lang-btn">English</a>
            <button class="mobile-menu-toggle" onclick="toggleMobileMenu()"><i class="fas fa-bars"></i></button>
        </div>
    </div>
</nav>

<main class="sdd-layout">
    <div class="sdd-content">
{body_html}
    </div>
</main>

<footer class="footer" style="margin-top: 0;">
    <div class="container">
        <div class="footer-bottom">
            <p>&copy; 2026 MRRC-FT8 Project · <a href="https://github.com/cheenle/mrrc_ft8" style="color:var(--accent);">GitHub</a></p>
        </div>
    </div>
</footer>

<script>
function toggleMobileMenu() {{
    document.querySelector('.nav-links').classList.toggle('active');
}}
document.querySelectorAll('a[href^="#"]').forEach(a => {{
    a.addEventListener('click', function(e) {{
        e.preventDefault();
        const t = document.querySelector(this.getAttribute('href'));
        if (t) t.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }});
}});
const nav = document.querySelector('.navbar');
window.addEventListener('scroll', () => {{
    nav.style.background = window.scrollY > 50 ? 'rgba(0,0,0,0.95)' : 'rgba(0,0,0,0.8)';
}});
</script>
    <script src="../js/global-nav.js?v=2" defer data-gn="1"></script>
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
