"""Normalise downloaded bank CSV exports.

Rename files like `activity (3).csv` to `activity_<start>_to_<end>.csv` where
the range is derived from the min/max transaction date actually contained in
the file. Remove content-identical duplicates. Flag conflicts (same date range,
different content) for manual review.

Bank detection is by column-header signature — add new entries to `_BANKS`.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Column-signature substring -> (bank_key, date_column, date_parser_key).
# The signature is a distinctive substring of the header row.
_BANKS: dict[str, tuple[str, str, str]] = {
    "Card Member,Account #,Amount": ("amex", "Date", "dd_mon_yyyy"),
}

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_date(s: str, fmt: str) -> date:
    s = s.strip()
    if fmt == "dd_mon_yyyy":                 # e.g. "14 Apr 2026"
        day, mon, year = s.split()
        return date(int(year), _MONTHS[mon], int(day))
    raise ValueError(f"unknown date format: {fmt}")


@dataclass
class IngestPlan:
    renames: list[tuple[Path, Path]] = field(default_factory=list)
    duplicates: list[tuple[Path, Path]] = field(default_factory=list)  # (dup, survivor_dst)
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)   # (src, collided_dst)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.renames)} rename(s), "
            f"{len(self.duplicates)} duplicate(s), "
            f"{len(self.conflicts)} conflict(s), "
            f"{len(self.skipped)} skipped"
        )


def plan_ingest(folder: Path) -> IngestPlan:
    """Compute renames + dedup without touching disk."""
    analyses: list[tuple[Path, Path, str]] = []  # (src, planned_dst, content_hash)
    plan = IngestPlan()

    for csv_path in sorted(folder.glob("*.csv")):
        try:
            header, rows = _read_csv(csv_path)
        except Exception as e:
            plan.skipped.append((csv_path, f"read failed: {e}"))
            continue

        bank_info = _detect_bank(header)
        if bank_info is None:
            plan.skipped.append((csv_path, "unknown bank (header signature not recognised)"))
            continue
        _, date_col, fmt = bank_info

        try:
            start, end = _date_range(rows, header, date_col, fmt)
        except ValueError as e:
            plan.skipped.append((csv_path, str(e)))
            continue

        dst = folder / f"activity_{start.isoformat()}_to_{end.isoformat()}.csv"
        analyses.append((csv_path, dst, _hash_rows(rows)))

    # Group by target filename to resolve duplicates and conflicts.
    by_dst: dict[Path, list[tuple[Path, str]]] = {}
    for src, dst, h in analyses:
        by_dst.setdefault(dst, []).append((src, h))

    for dst, items in by_dst.items():
        by_hash: dict[str, list[Path]] = {}
        for src, h in items:
            by_hash.setdefault(h, []).append(src)

        if len(by_hash) == 1:
            srcs = next(iter(by_hash.values()))
            survivor = srcs[0]
            if survivor != dst:
                plan.renames.append((survivor, dst))
            for dup in srcs[1:]:
                plan.duplicates.append((dup, dst))
        else:
            for src, _ in items:
                plan.conflicts.append((src, dst))

    return plan


def apply_plan(plan: IngestPlan) -> None:
    """Execute renames and remove duplicates. Conflicts are left alone."""
    for src, dst in plan.renames:
        if dst.exists() and dst != src:
            raise FileExistsError(f"destination already exists: {dst}")
        src.rename(dst)
    for dup, _ in plan.duplicates:
        dup.unlink()


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    return header, rows


def _detect_bank(header: list[str]) -> tuple[str, str, str] | None:
    header_line = ",".join(header)
    for signature, info in _BANKS.items():
        if signature in header_line:
            return info
    return None


def _date_range(
    rows: list[list[str]], header: list[str], date_col: str, fmt: str
) -> tuple[date, date]:
    if date_col not in header:
        raise ValueError(f"date column {date_col!r} not in header")
    idx = header.index(date_col)
    dates: list[date] = []
    for row in rows:
        if idx >= len(row) or not row[idx].strip():
            continue
        try:
            dates.append(_parse_date(row[idx], fmt))
        except Exception:
            continue
    if not dates:
        raise ValueError(f"no parseable dates in column {date_col!r}")
    return min(dates), max(dates)


def _hash_rows(rows: list[list[str]]) -> str:
    """Order-independent content hash — same rows in different order collide
    intentionally (bank exports sometimes differ in sort order but carry the
    same transactions)."""
    sig = sorted(",".join(r) for r in rows)
    return hashlib.sha256("\n".join(sig).encode()).hexdigest()
