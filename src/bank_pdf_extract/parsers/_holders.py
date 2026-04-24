"""Dynamic holder-name extraction shared across parsers.

Holder names appear in PDFs both as output values (account_holder) and as
repeating header/footer noise that must be skipped during line-based parsing.
Extracting them at runtime avoids hardcoding PII in source.
"""
from __future__ import annotations

import re

_POSTAL = re.compile(r"\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b")
_NAME_LINE = re.compile(r"^[A-Z][A-Z\-'\.]+(?:\s+[A-Z][A-Z\-'\.]+)+$")

_ADDRESS_TOKENS = (
    "STREET", "AVENUE", " AVE", "ROAD", " RD", "BLVD", "BOULEVARD",
    "DRIVE", " DR", "COURT", " CT", "LANE", " LN", "PLACE", " PL",
    "APT", "UNIT", "SUITE", "FLOOR", "#",
    "VANCOUVER", "BURNABY", "SURREY", "RICHMOND", "CALGARY", "TORONTO",
    " BC", " ON", " AB", " QC", " MB", " SK", " NS", " NB",
    "PO BOX", "CANADA",
)


def extract_holders_from_address_block(page_text: str) -> list[str]:
    """Return ALL-CAPS name lines appearing immediately above a postal code.

    Canadian bank statements print the mailing address as:
        NAME(S)
        STREET ADDRESS
        CITY PROVINCE POSTAL
    We locate the postal code, walk upward, and treat consecutive ALL-CAPS
    lines that don't look like street addresses as holder names.
    """
    lines = [ln.strip() for ln in page_text.splitlines()]
    for i, ln in enumerate(lines):
        if not _POSTAL.search(ln):
            continue
        holders: list[str] = []
        for j in range(i - 1, max(-1, i - 6), -1):
            prev = lines[j]
            if not prev:
                if holders:
                    break
                continue
            if _looks_like_address(prev):
                if holders:
                    break
                continue
            if _NAME_LINE.match(prev):
                holders.insert(0, prev)
            elif holders:
                break
        if holders:
            return holders
    return []


def _looks_like_address(s: str) -> bool:
    upper = s.upper()
    if any(tok in upper for tok in _ADDRESS_TOKENS):
        return True
    # Lines starting with digits are almost always street numbers
    return bool(re.match(r"^\d", s))


def join_holders(holders: list[str]) -> str:
    """Render holder list as account_holder string."""
    return " / ".join(holders)
