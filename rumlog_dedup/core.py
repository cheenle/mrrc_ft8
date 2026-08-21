"""Pure duplicate-detection and dedup-planning for RUMLogNG (no I/O).

This module takes plain row dicts and returns clusters / plans, so the dedup
decision is unit-testable against synthetic rows without touching the real
Core Data store (tests/rumlog_dedup/test_core.py).

Duplicate predicate matches the sync (rumlog_sync/ft8_db.py): equal callsign,
band equal-or-empty (empty is a wildcard on either side), and completion times
within MATCH_WINDOW_S seconds.  Clustering is conservative: a cluster's time
span never exceeds the window (no chaining across distinct QSOs).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

MATCH_WINDOW_S = 120.0
CORE_DATA_EPOCH_OFFSET = 978_307_200

# Fields merged into the kept row when a duplicate holds a more complete value.
# ZLOCATOR prefers the longest (6-char over 4-char); the rest prefer non-empty
# / non-zero over empty / zero.
MERGE_FIELDS = ("ZLOCATOR", "ZRSTTX", "ZRSTRX", "ZQRG", "ZMODE", "ZBAND")

KEEP_RULES = ("earliest", "lowest_zpk")


def utc_str(core_data_seconds: object) -> str:
    """Core Data timestamp (s since 2001-01-01) -> YYYY-MM-DD HH:MM:SS UTC."""

    try:
        t = float(core_data_seconds) + CORE_DATA_EPOCH_OFFSET
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _call(row: dict[str, Any]) -> str:
    return str(row.get("ZCALLSIGN") or "").strip()


def _band(row: dict[str, Any]) -> str:
    return str(row.get("ZBAND") or "").strip()


def _epoch(row: dict[str, Any]) -> float:
    try:
        return float(row.get("ZDATETIME") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _band_compat(a: str, b: str) -> bool:
    return (not a) or (not b) or a == b


def scan_duplicates(
    rows: list[dict[str, Any]], window: float = MATCH_WINDOW_S
) -> list[list[dict[str, Any]]]:
    """Group rows that describe the same QSO into clusters of size >= 2.

    Same predicate as the sync: equal callsign, band equal-or-empty, and every
    row within window seconds of the cluster's earliest row (span never
    exceeds window — a chain of three QSOs 100 s apart stays two separate
    clusters, not one).
    """

    by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_call[_call(row)].append(row)

    clusters: list[list[dict[str, Any]]] = []
    for grp in by_call.values():
        grp.sort(key=lambda r: (_epoch(r), r.get("Z_PK") or 0))
        cur: list[dict[str, Any]] = []
        cur_band = ""
        cur_min = 0.0
        for row in grp:
            band = _band(row)
            t = _epoch(row)
            if cur and _band_compat(band, cur_band) and (t - cur_min) <= window:
                cur.append(row)
                if not cur_band:
                    cur_band = band
            else:
                if len(cur) >= 2:
                    clusters.append(cur)
                cur = [row]
                cur_band = band
                cur_min = t
        if len(cur) >= 2:
            clusters.append(cur)
    return clusters


def pick_keep(cluster: list[dict[str, Any]], rule: str = "earliest") -> dict[str, Any]:
    """The row to keep.  earliest (default) keeps the QSO start time; tie
    breaks on the lowest Z_PK.  lowest_zpk keeps the oldest inserted row."""

    if rule == "lowest_zpk":
        return min(cluster, key=lambda r: r.get("Z_PK") or 0)
    return min(cluster, key=lambda r: (_epoch(r), r.get("Z_PK") or 0))


def _is_empty(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _better(field: str, current: object, candidate: object) -> bool:
    """True when candidate is a more complete value than current."""

    if field == "ZLOCATOR":
        return len(str(candidate or "").strip()) > len(str(current or "").strip())
    if field == "ZQRG":
        try:
            return float(current or 0) == 0 and float(candidate or 0) != 0
        except (TypeError, ValueError):
            return False
    return _is_empty(current) and not _is_empty(candidate)


def compute_merge(cluster: list[dict[str, Any]], keep: dict[str, Any]) -> dict[str, Any]:
    """Complementary fields to copy into keep from the duplicate rows."""

    updates: dict[str, Any] = {}
    for field in MERGE_FIELDS:
        best = keep.get(field)
        for other in cluster:
            if other is keep:
                continue
            candidate = other.get(field)
            if _better(field, best, candidate):
                best = candidate
        if best != keep.get(field):
            updates[field] = best
    return updates


def build_plan(
    clusters: list[list[dict[str, Any]]],
    keep_rule: str = "earliest",
    merge: bool = True,
) -> list[dict[str, Any]]:
    """One plan entry per cluster: keep row, delete rows, merge updates."""

    plan: list[dict[str, Any]] = []
    for cluster in clusters:
        keep = pick_keep(cluster, keep_rule)
        deletes = sorted(
            (r for r in cluster if r is not keep),
            key=lambda r: r.get("Z_PK") or 0,
        )
        plan.append(
            {
                "callsign": _call(keep),
                "band": _band(keep),
                "keep": keep,
                "keep_zpk": keep.get("Z_PK"),
                "deletes": deletes,
                "delete_zpk": [r.get("Z_PK") for r in deletes],
                "merge_updates": compute_merge(cluster, keep) if merge else {},
                "times": sorted(_epoch(r) for r in cluster),
                "dup_count": len(cluster),
            }
        )
    return plan


def summarize(plan: list[dict[str, Any]]) -> dict[str, int]:
    """Cluster / row / delete counts for the plan."""

    return {
        "clusters": len(plan),
        "rows_involved": sum(p["dup_count"] for p in plan),
        "rows_to_delete": sum(len(p["delete_zpk"]) for p in plan),
        "merges": sum(1 for p in plan if p["merge_updates"]),
    }


def render_plan_text(plan: list[dict[str, Any]], limit: int | None = None) -> str:
    """Human-readable per-cluster keep/delete/merge listing."""

    lines: list[str] = []
    for p in plan[:limit] if limit else plan:
        merge = ""
        if p["merge_updates"]:
            merge = "  merge={" + ", ".join(
                f"{f}={p['merge_updates'][f]!r}" for f in p["merge_updates"]
            ) + "}"
        times = [utc_str(t) for t in p["times"]]
        lines.append(
            f"{p['callsign']:<10} {p['band'] or '(空)':<6} x{p['dup_count']}"
            f"  {times[0]}..{times[-1]}"
            f"  keep={p['keep_zpk']}  del={p['delete_zpk']}{merge}"
        )
    return "\n".join(lines)


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_sql(plan: list[dict[str, Any]], limit: int | None = None) -> str:
    """A transaction script that applies the plan (display only; the applier
    uses parameterised statements, not this text)."""

    scope = plan[:limit] if limit else plan
    lines = ["BEGIN TRANSACTION;"]
    for p in scope:
        if p["merge_updates"]:
            sets = ", ".join(
                f"{f} = {_sql_literal(v)}" for f, v in p["merge_updates"].items()
            )
            lines.append(f"-- {p['callsign']} {p['band']}: merge into keep Z_PK={p['keep_zpk']}")
            lines.append(f"UPDATE ZCORE_QSO SET {sets} WHERE Z_PK = {p['keep_zpk']};")
    all_delete = [z for p in scope for z in p["delete_zpk"]]
    if all_delete:
        chunk = ", ".join(str(z) for z in all_delete)
        lines.append(f"DELETE FROM ZCORE_QSO WHERE Z_PK IN ({chunk});")
    lines.append("COMMIT;")
    return "\n".join(lines)
