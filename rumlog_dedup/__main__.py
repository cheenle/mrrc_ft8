"""CLI for the RUMLogNG duplicate cleanup tool.

Usage:
  python -m rumlog_dedup --dry-run                     # scan + print plan + SQL
  python -m rumlog_dedup --apply --yes --backup DIR    # verified backup, then apply

Safety guarantees:
  * Never writes without --apply.
  * --apply requires --backup DIR (creates and verifies a snapshot) and --yes.
  * Refuses to write while RUMLogNG appears to be running (override --force).
  * Keeps the earliest QSO row and merges complementary fields into it before
    deleting duplicates (merge-then-delete).
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    build_plan,
    render_plan_text,
    render_sql,
    scan_duplicates,
    summarize,
)

DEFAULT_RUMLOG_DB = (
    "/Users/cheenle/Library/Containers/de.dl2rum.RUMlogNG/Data/Library/"
    "Application Support/RUMLogNG/CoreQsoModel_1.sqlite"
)

_COLUMNS = "Z_PK,ZCALLSIGN,ZBAND,ZMODE,ZQRG,ZRSTTX,ZRSTRX,ZLOCATOR,ZDATETIME,ZUUID"


def _read_rows(path: str) -> list[dict[str, object]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT {_COLUMNS} FROM ZCORE_QSO ORDER BY Z_PK"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _count(path: str) -> int:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    try:
        return con.execute("SELECT COUNT(*) FROM ZCORE_QSO").fetchone()[0]
    finally:
        con.close()


def _backup(src: str, dest_dir: str) -> str:
    """Create a consistent snapshot via the SQLite online-backup API and verify
    the row count matches the source before returning the path."""

    dest_dir_path = Path(dest_dir)
    dest_dir_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir_path / f"CoreQsoModel_1-{stamp}.sqlite"
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10.0)
    dest_con = sqlite3.connect(str(dest))
    try:
        src_con.backup(dest_con)
    finally:
        dest_con.close()
        src_con.close()
    src_n = _count(src)
    dst_n = _count(str(dest))
    if src_n != dst_n:
        raise RuntimeError(
            f"backup verification failed: source {src_n} rows vs backup {dst_n} rows"
        )
    return str(dest)


def _rumlog_running() -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "RUMlogNG"], capture_output=True, text=True
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except OSError:
        return False


def _apply(plan: list[dict[str, object]], path: str) -> None:
    """Merge-then-delete in one transaction (all-or-nothing)."""

    con = sqlite3.connect(path, timeout=30.0)
    try:
        con.execute("BEGIN")
        for p in plan:
            keep_zpk = p["keep_zpk"]
            for field, value in p["merge_updates"].items():
                con.execute(
                    f"UPDATE ZCORE_QSO SET {field} = ? WHERE Z_PK = ?",
                    (value, keep_zpk),
                )
        all_delete = [z for p in plan for z in p["delete_zpk"]]
        for i in range(0, len(all_delete), 900):
            chunk = all_delete[i : i + 900]
            marks = ",".join("?" for _ in chunk)
            con.execute(
                f"DELETE FROM ZCORE_QSO WHERE Z_PK IN ({marks})", chunk
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rumlog_dedup", description="RUMLogNG duplicate cleanup tool"
    )
    parser.add_argument("--rumlog-db", default=DEFAULT_RUMLOG_DB)
    parser.add_argument("--window", type=float, default=120.0)
    parser.add_argument(
        "--keep", choices=("earliest", "lowest_zpk"), default="earliest"
    )
    parser.add_argument("--no-merge", action="store_true")
    parser.add_argument(
        "--apply", action="store_true", help="actually write (UPDATE/DELETE)"
    )
    parser.add_argument("--yes", action="store_true", help="skip confirmation with --apply")
    parser.add_argument(
        "--backup", default=None, help="backup dir (required with --apply)"
    )
    parser.add_argument(
        "--force", action="store_true", help="apply even if RUMLogNG is running"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="apply only the first N clusters (biggest first)",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    rows = _read_rows(args.rumlog_db)
    clusters = scan_duplicates(rows, window=args.window)
    plan = build_plan(clusters, keep_rule=args.keep, merge=not args.no_merge)
    # biggest clusters first, so --limit stages the unambiguous mega-clusters
    plan.sort(key=lambda p: (-p["dup_count"], p["callsign"], p["keep_zpk"] or 0))

    if args.limit:
        plan = plan[: args.limit]

    stats = summarize(plan)
    print(f"total rows: {len(rows)}")
    print(
        f"duplicate clusters: {stats['clusters']}  rows involved: {stats['rows_involved']}"
        f"  to delete: {stats['rows_to_delete']}  merges: {stats['merges']}"
    )
    print()
    print(render_plan_text(plan, limit=50))
    if len(plan) > 50:
        print(f"... ({len(plan) - 50} more clusters)")
    print()
    print(render_sql(plan))

    if not args.apply:
        print("\n[dry-run] no writes performed. Add --apply --yes --backup DIR to execute.")
        return 0

    if not args.backup:
        print("error: --apply requires --backup DIR (verified snapshot).", file=sys.stderr)
        return 2
    if not args.yes:
        print("error: --apply requires --yes.", file=sys.stderr)
        return 2
    if _rumlog_running() and not args.force:
        print(
            "error: RUMLogNG appears to be running; quit it first (or --force).",
            file=sys.stderr,
        )
        return 2

    backup_path = _backup(args.rumlog_db, args.backup)
    print(f"\nbackup verified: {backup_path}")

    _apply(plan, args.rumlog_db)

    remaining = scan_duplicates(_read_rows(args.rumlog_db), window=args.window)
    print(f"\napplied. remaining duplicate clusters: {len(remaining)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
