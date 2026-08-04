"""DXCC entity lookup from the repo's cty.dat (country-files ADIF format).

The file's entity rows are ``name: cqz: ituz: continent: lat: lon: tz: prefix:``
(no DXCC number column — the entity name is the statistical unit).  Prefix
lists span continuation lines, comma-separated, terminated by ``;``.  Entries
may be ``=CALL`` (exact match) or carry ``(digits)`` digit-replacement rules
(e.g. ``3H0(23)[42]`` matches 3H0/3H2/3H3 prefixes); the ``[nn]`` override
number is ignored because this data source has no number column.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_ENTITY_RE = re.compile(
    r"^(.*?):\s*\d+:\s*\d+:\s*(\S+):\s*[-\d.]+:\s*[-\d.]+:\s*[-\d.]+:\s*\S+:\s*$"
)


@dataclass
class CtyEntity:
    name: str
    continent: str
    prefixes: list[str]  # 已展开的匹配模式：`=X` 精确 / `X` 前缀


def _expand_entry(entry: str) -> list[tuple[str, bool]]:
    """One raw prefix entry → [(pattern, exact)]; (23) digit replacement
    expands to one pattern per digit.  ``=`` marks exact call match."""

    entry = entry.strip().rstrip(";").strip()
    if not entry:
        return []
    exact = entry.startswith("=")
    entry = entry.lstrip("=")
    # (23) replaces the trailing digit of the base: 3H0(23) → 3H0/3H2/3H3.
    m = re.match(r"^(.*\d)\((\d+)\)(?:\[\d+\])?$", entry)
    if m:
        base, digits = m.groups()
        results = [(base, exact)]  # 原样（3H0）
        for d in digits:
            results.append((base[:-1] + d, exact))  # 替换末位数字
        return results
    return [(entry, exact)]


@dataclass
class CtyDatabase:
    entities: list[CtyEntity]

    def lookup(self, call: str) -> tuple[str, str] | None:
        """(entity_name, continent) for a callsign; exact match wins, then
        the longest prefix match; None when nothing matches."""

        base = call.split("/", 1)[0].upper()
        best_len = -1
        best: CtyEntity | None = None
        for entity in self.entities:
            for stored in entity.prefixes:
                if stored.startswith("="):
                    if base == stored[1:]:
                        return (entity.name, entity.continent)
                elif base.startswith(stored):
                    if len(stored) > best_len:
                        best_len = len(stored)
                        best = entity
        return (best.name, best.continent) if best else None


def load_cty(path: str) -> CtyDatabase:
    """Parse cty.dat into a CtyDatabase; missing/broken file → empty db
    (callers fall back to all-unmatched, never crash)."""

    entities: list[CtyEntity] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = [line.rstrip("\n") for line in stream]
    except OSError as exc:
        log.exception("cty.dat unreadable: %s", exc)
        return CtyDatabase(entities)
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _ENTITY_RE.match(line)
        if m:
            name, continent = m.groups()
            i += 1
            buf = ""
            while i < len(lines):
                s = lines[i].rstrip()
                if s.startswith((" ", "\t")):
                    buf += s.strip()
                    i += 1
                    if buf.endswith(";"):
                        break
                else:
                    break
            prefixes: list[str] = []
            seen: set[str] = set()
            for raw in buf.split(","):
                for pattern, exact in _expand_entry(raw):
                    stored = f"={pattern}" if exact else pattern
                    if stored not in seen:
                        seen.add(stored)
                        prefixes.append(stored)
            entities.append(CtyEntity(name.strip(), continent, prefixes))
        else:
            i += 1
    return CtyDatabase(entities)


# ---- summary statistics ----------------------------------------------------

from datetime import datetime, timezone


@dataclass
class DxccEntityStat:
    name: str
    continent: str
    first_utc: str
    last_utc: str
    band_count: int
    bands: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "continent": self.continent,
            "first_utc": self.first_utc,
            "last_utc": self.last_utc,
            "band_count": self.band_count,
            "bands": self.bands,
        }


@dataclass
class DxccSummary:
    total: int
    unmatched: int
    entities: list[DxccEntityStat]
    by_band: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "unmatched": self.unmatched,
            "entities": [e.to_dict() for e in self.entities],
            "by_band": self.by_band,
        }


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dxcc_summary(repository, cty: CtyDatabase) -> DxccSummary:
    """Full-scan DXCC statistics over non-void QSOs (spec §3.2).

    Same entity × same band counts once (DXCC Challenge semantics);
    unmatched calls count into ``unmatched`` without failing.
    """

    stats: dict[str, DxccEntityStat] = {}
    by_band: dict[str, set[str]] = {}
    lookup_cache: dict[str, tuple[str, str] | None] = {}
    unmatched = 0
    for qso in repository.list_qsos(include_void=False):
        if qso.dx_call not in lookup_cache:
            lookup_cache[qso.dx_call] = cty.lookup(qso.dx_call)
        result = lookup_cache[qso.dx_call]
        if result is None:
            unmatched += 1
            continue
        name, continent = result
        stat = stats.get(name)
        iso = _utc_iso(qso.completed_epoch)
        if stat is None:
            stat = stats[name] = DxccEntityStat(
                name=name, continent=continent,
                first_utc=iso, last_utc=iso, band_count=0, bands=[],
            )
        else:
            stat.first_utc = min(stat.first_utc, iso)
            stat.last_utc = max(stat.last_utc, iso)
        if qso.band and qso.band not in stat.bands:
            stat.bands.append(qso.band)
            stat.bands.sort()
            stat.band_count = len(stat.bands)
        by_band.setdefault(qso.band, set()).add(name)
    ordered = sorted(stats.values(), key=lambda s: s.name)
    return DxccSummary(
        total=len(ordered),
        unmatched=unmatched,
        entities=ordered,
        by_band={band: len(ents) for band, ents in by_band.items()},
    )
