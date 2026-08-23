#!/usr/bin/env python3
"""Convert SDD markdown files to styled HTML pages (MRRC-FT8 dark theme)."""
import subprocess, sys, os, re
from pathlib import Path


def apply_scope_classes(html: str) -> str:
    """Add Scope.css classes to pandoc-generated elements."""
    html = re.sub(r'<table>(?![^<]*class=)', '<table class="scope-table">', html, flags=re.IGNORECASE)
    html = re.sub(r'<pre>(?![^<]*class=)', '<pre class="scope-code">', html, flags=re.IGNORECASE)
    html = re.sub(r'<code>(?![^<]*class=)', '<code class="scope-code">', html, flags=re.IGNORECASE)
    return html

SDD_DIR = Path("/Users/cheenle/HAM/ft8/SDD")
OUT_DIR = Path("/Users/cheenle/HAM/ft8/website/sdd")
CSS_PATH = "../css/scope.css?v=1"

FILES = [
    ("README.md", "index.html", "SDD Overview"),
    ("01-executive-summary.md", "01-executive-summary.html", "Executive Summary"),
    ("02-business-direction.md", "02-business-direction.html", "Business Direction"),
    ("03-project-definition.md", "03-project-definition.html", "Project Definition"),
    ("04-system-context.md", "04-system-context.html", "System Context"),
    ("05-non-functional-requirements.md", "05-non-functional-requirements.html", "Non-Functional Requirements"),
    ("06-use-case-model.md", "06-use-case-model.html", "Use Case Model"),
    ("07-subject-area-model.md", "07-subject-area-model.html", "Subject Area Model"),
    ("08-architecture-decisions.md", "08-architecture-decisions.html", "Architecture Decisions"),
    ("09-architecture-overview.md", "09-architecture-overview.html", "Architecture Overview"),
    ("10-service-model.md", "10-service-model.html", "Service Model"),
    ("11-component-model.md", "11-component-model.html", "Component Model"),
    ("12-operational-model.md", "12-operational-model.html", "Operational Model"),
    ("13-feasibility-assessment.md", "13-feasibility-assessment.html", "Feasibility Assessment"),
    ("14-version-history.md", "14-version-history.html", "Version History"),
    ("15-ptt-safety-architecture.md", "15-ptt-safety-architecture.html", "PTT Safety Architecture"),
]

NAV_ITEMS = [
    ("index.html", "Overview"),
    ("01-executive-summary.html", "1. Executive Summary"),
    ("02-business-direction.html", "2. Business Direction"),
    ("03-project-definition.html", "3. Project Definition"),
    ("04-system-context.html", "4. System Context"),
    ("05-non-functional-requirements.html", "5. NFRs"),
    ("06-use-case-model.html", "6. Use Cases"),
    ("07-subject-area-model.html", "7. Subject Model"),
    ("08-architecture-decisions.html", "8. Architecture Decisions"),
    ("09-architecture-overview.html", "9. Architecture Overview"),
    ("10-service-model.html", "10. Service Model"),
    ("11-component-model.html", "11. Component Model"),
    ("12-operational-model.html", "12. Operational Model"),
    ("13-feasibility-assessment.html", "13. Feasibility"),
    ("14-version-history.html", "14. Version History"),
    ("15-ptt-safety-architecture.html", "15. PTT Safety"),
]

def build_nav_sidebar(current_file: str) -> str:
    items = []
    for href, label in NAV_ITEMS:
        cls = ' class="active"' if href == current_file else ""
        items.append(f'            <li><a href="{href}"{cls}>{label}</a></li>')
    return "\n".join(items)

def build_page(body_html: str, title: str, current_file: str) -> str:
    sidebar = build_nav_sidebar(current_file)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — MRRC-FT8 SDD</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="{CSS_PATH}">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7442510147240155" crossorigin="anonymous"></script>
    <style>
        .sdd-layout {{ display: flex; max-width: 1400px; margin: 0 auto; padding: calc(var(--scope-gn-h) + 2rem) 2rem 2rem; gap: 2rem; }}
        .sdd-sidebar {{
            width: 260px; flex-shrink: 0; position: sticky; top: calc(var(--scope-gn-h) + 2rem);
            max-height: calc(100vh - var(--scope-gn-h) - 4rem); overflow-y: auto;
            background: var(--scope-surface); border: 1px solid var(--scope-border);
            border-radius: var(--scope-radius); padding: 1.25rem 0;
        }}
        .sdd-sidebar h4 {{
            font-family: var(--scope-font-mono); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em;
            color: var(--scope-primary); padding: 0 1.25rem; margin-bottom: 0.75rem;
        }}
        .sdd-sidebar ul {{ list-style: none; margin: 0; padding: 0; }}
        .sdd-sidebar a {{
            display: block; padding: 0.375rem 1.25rem; font-size: 0.8125rem;
            color: var(--scope-text-2); transition: all 0.15s;
            border-left: 2px solid transparent;
        }}
        .sdd-sidebar a:hover {{ color: var(--scope-text); border-left-color: var(--scope-border-strong); text-decoration: none; }}
        .sdd-sidebar a.active {{
            color: var(--scope-primary); background: rgba(0, 255, 65, 0.08);
            border-left-color: var(--scope-primary); font-weight: 500;
        }}
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
        .sdd-content code {{
            font-family: var(--scope-font-mono); font-size: 0.85em;
            background: var(--scope-surface); padding: 0.15rem 0.4rem; border-radius: var(--scope-radius);
            color: var(--scope-primary);
        }}
        .sdd-content pre {{
            background: var(--scope-surface); border: 1px solid var(--scope-border);
            border-radius: var(--scope-radius); padding: 1rem; overflow-x: auto;
            margin: 1rem 0; font-size: 0.8125rem; line-height: 1.6;
        }}
        .sdd-content pre code {{
            background: none; padding: 0; color: var(--scope-text-2);
        }}
        .sdd-content blockquote {{
            border-left: 3px solid var(--scope-primary); padding: 0.5rem 1rem;
            margin: 1rem 0; color: var(--scope-text-muted); font-size: 0.9375rem;
            background: var(--scope-surface); border-radius: 0 var(--scope-radius) var(--scope-radius) 0;
        }}
        .sdd-content ul, .sdd-content ol {{ margin-left: 1.5rem; margin-bottom: 1rem; }}
        .sdd-content hr {{ border: none; border-top: 1px solid var(--scope-border); margin: 2rem 0; }}
        .sdd-content a {{ color: var(--scope-primary); }}
        .sdd-content img {{ max-width: 100%; border-radius: var(--scope-radius); margin: 1rem 0; }}
        @media (max-width: 900px) {{
            .sdd-layout {{ flex-direction: column; padding: calc(var(--scope-gn-h) + 1rem) 1rem 1rem; }}
            .sdd-sidebar {{ width: 100%; position: static; max-height: none; }}
        }}
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
            <li><a href="../index.html#features">Features</a></li>
            <li><a href="../index.html#architecture">Architecture</a></li>
            <li><a href="index.html">SDD Docs</a></li>
            <li><a href="https://github.com/cheenle/mrrc_ft8" target="_blank" rel="noopener"><i class="fab fa-github"></i> GitHub</a></li>
        </ul>
        <div style="display:flex;align-items:center;gap:0.75rem;">
            <a href="../zh/index.html" class="scope-btn" style="padding:0.4rem 0.8rem;font-size:0.75rem;">中文</a>
            <button class="scope-site-nav-toggle" aria-label="Toggle menu"><i class="fas fa-bars"></i></button>
        </div>
    </div>
</nav>

<div class="sdd-layout">
    <aside class="sdd-sidebar">
        <h4>SDD Chapters</h4>
        <ul>
{sidebar}
        </ul>
    </aside>
    <main class="sdd-content">
{body_html}
    </main>
</div>

<footer class="scope-footer" style="margin-top: 0;">
    <div class="container">
        <div class="footer-bottom" style="border-top:none;padding-top:0;">
            <p>&copy; 2026 MRRC-FT8 Project · SDD V1.2 · <a href="https://github.com/cheenle/mrrc_ft8">GitHub</a></p>
        </div>
    </div>
</footer>

<script src="../js/scope.js?v=1" defer></script>
</body>
</html>"""

def convert(md_path: Path) -> str:
    """Convert markdown to HTML body using pandoc."""
    result = subprocess.run(
        ["pandoc", str(md_path), "-f", "markdown", "-t", "html",
         "--no-highlight", "--wrap=none"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr}")
        sys.exit(1)
    return result.stdout.strip()

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for md_name, html_name, title in FILES:
        md_path = SDD_DIR / md_name
        if not md_path.exists():
            print(f"  SKIP: {md_name} not found")
            continue
        print(f"  {md_name} → {html_name}")
        body = apply_scope_classes(convert(md_path))
        page = build_page(body, title, html_name)
        (OUT_DIR / html_name).write_text(page, encoding="utf-8")
    print(f"\nDone. {len(FILES)} pages written to {OUT_DIR}")

if __name__ == "__main__":
    main()
