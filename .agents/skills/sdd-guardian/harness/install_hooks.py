#!/usr/bin/env python3
"""Install SDD-Guardian hooks into ~/.kimi-code/config.toml (idempotent).

Appends the [[hooks]] entries from hooks.snippet.toml between BEGIN/END
sdd-guardian markers. Re-running replaces the marked block instead of
duplicating it. A timestamped backup of config.toml is written first.
"""
import shutil
import sys
import time
from pathlib import Path

CONFIG = Path.home() / ".kimi-code" / "config.toml"
SNIPPET = Path(__file__).resolve().parent / "hooks.snippet.toml"
BEGIN, END = "# BEGIN sdd-guardian", "# END sdd-guardian"


def main() -> int:
    snippet = SNIPPET.read_text(encoding="utf-8")
    block = snippet[snippet.index(BEGIN):snippet.index(END) + len(END)]

    existing = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
    if BEGIN in existing and END in existing:
        pre = existing[:existing.index(BEGIN)].rstrip() + "\n\n"
        post = existing[existing.index(END) + len(END):].lstrip("\n")
        new = pre + block + "\n" + ("\n" + post if post.strip() else "")
        action = "updated"
    else:
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
        new = existing + sep + block + "\n"
        action = "installed"

    if CONFIG.exists():
        backup = CONFIG.with_suffix(f".toml.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(CONFIG, backup)
        print(f"backup: {backup}")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(new, encoding="utf-8")
    print(f"hooks {action} in {CONFIG}")
    print("effective on next `kimi` session start (or /reload).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
