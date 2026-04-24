"""Parser + validators for BMO Everyday Banking deposit statements.

Covers any product that uses BMO's "Everyday Banking" statement template — today
that's Primary Chequing and Savings Builder, but the same template applies to
any deposit-account variant. The specific sub-type is detected from the PDF's
`<TypeName>Account#<number>` section header and stored in `header.account_type`.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..schema import DepositAccountDetail, DepositAccountHeader, DepositAccountStatement

BANK = "bmo"
PRODUCT = "everyday_banking"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
)}
_M = "(?:" + "|".join(_MONTHS) + ")"
_AMOUNT = r"[\d,]+\.\d{2}"
_BALANCE = r"-?[\d,]+\.\d{2}"          # can be negative when account is overdrawn

# PDF extraction squishes many tokens, e.g. "Jan06 Openingbalance 16,713.10".
_ROW_LEAD = re.compile(rf"^({_M})(\d{{1,2}})\s+(.+)$")
_OPENING_TAIL = re.compile(rf"^Openingbalance\s+({_BALANCE})$")
_CLOSING_TAIL = re.compile(rf"^Closingtotals\s+({_AMOUNT})\s+({_BALANCE})$")
_DETAIL_TAIL = re.compile(rf"^(.+?)\s+({_AMOUNT})\s+({_BALANCE})$")

# Mid-statement noise that appears between transactions across page breaks or
# repeated headers. Kept intentionally broad so it works for any owner / product.
_NOISE = re.compile(
    r"^("
    r"Pleasereportany"
    r"|Page\s*\d"
    r"|Amountsdeducted\b"
    r"|Amountsadded\b"
    r"|Date\s+Description"
    r"|Owner:\s*$"
    r"|continued\s*$"
    r"|[A-Za-z]+Account\s*#"       # any <Type>Account#<num>
    r"|(?:MS|MR|MRS|MISS|DR)\b"    # owner line with title
    r"|[A-Z]{4,}(?:\s+[A-Z]{2,})*\s*$"  # all-caps owner name (squished or spaced)
    r")"
)

_PERIOD_END = re.compile(
    r"For the period ending\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2}),\s+(\d{4})"
)
_FULL_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], start=1
)}


def parse(pdf_path: Path) -> tuple[DepositAccountHeader, list[DepositAccountDetail]]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    full_text = "\n".join(pages)
    period_end = _parse_period_end(full_text)
    summary = _parse_summary_row(full_text)
    account_type = _detect_account_type(full_text)
    period_start, details = _parse_details(
        full_text, period_end, summary["account_number"], summary["opening_balance"]
    )

    header = DepositAccountHeader(
        bank=BANK,
        product=PRODUCT,
        account_type=account_type,
        account_holder=_extract_owner(full_text),
        account_number=summary["account_number"],
        branch_name=_search(full_text, r"YourBranch\s*\n([A-Z][A-Z\s&]+?)\s*\n") or "",
        transit_number=_search(full_text, r"Transitnumber:(\d+)") or "",
        plan_name=_search(full_text, r"YourPlan\s*\n[^\n]*?([A-Z][A-Za-z]+Plan)") or "",
        period_start=period_start,
        period_end=period_end,
        opening_balance=summary["opening_balance"],
        total_deducted=summary["total_deducted"],
        total_added=summary["total_added"],
        closing_balance=summary["closing_balance"],
    )
    return header, details


def build_statement(
    header: DepositAccountHeader, details: list[DepositAccountDetail]
) -> DepositAccountStatement:
    return DepositAccountStatement(header=header, details=details)


# --- validation --------------------------------------------------------------

def validate_internal(
    header: DepositAccountHeader, details: list[DepositAccountDetail]
) -> list[str]:
    """Return discrepancies between header totals, running balances, and detail rows."""
    issues: list[str] = []

    deducted = sum((-d.amount for d in details if d.amount < 0), Decimal("0"))
    added = sum((d.amount for d in details if d.amount > 0), Decimal("0"))

    if deducted != header.total_deducted:
        issues.append(f"total_deducted: header={header.total_deducted} detail_sum={deducted}")
    if added != header.total_added:
        issues.append(f"total_added: header={header.total_added} detail_sum={added}")

    expected_closing = header.opening_balance - header.total_deducted + header.total_added
    if expected_closing != header.closing_balance:
        issues.append(
            f"closing_balance: expected {expected_closing}, got {header.closing_balance}"
        )

    running = header.opening_balance
    for d in details:
        running += d.amount
        if running != d.running_balance:
            issues.append(
                f"running_balance mismatch at item {d.item_num}: expected {running}, got {d.running_balance}"
            )
        if not (header.period_start <= d.posting_date <= header.period_end):
            issues.append(
                f"posting_date out of period: item {d.item_num} {d.posting_date} "
                f"not in [{header.period_start}, {header.period_end}]"
            )

    if details and details[-1].running_balance != header.closing_balance:
        issues.append(
            f"last running_balance {details[-1].running_balance} != closing_balance {header.closing_balance}"
        )

    return issues


def validate_against_csv(
    details: list[DepositAccountDetail],
    csv_path: Path,
    period: tuple[date, date] | None = None,
) -> list[str]:
    """Compare (posting_date, amount) multisets against CSV rows within the statement period.

    Description is not compared: the PDF squishes whitespace and uses long bank-speak
    labels ("Pre-AuthorizedPaymentNoFee,BMOPAYMENT"), while the CSV uses canonical
    merchant codes with type tags ("[DN]BMO PAYMENT BPY/FAC"). They are not directly
    comparable — the downstream pipeline should reconcile names, not this validator.
    """
    issues: list[str] = []
    ref = _load_reference_csv(csv_path)

    if period is None and details:
        period = (min(d.posting_date for d in details), max(d.posting_date for d in details))
    if period is not None:
        start, end = period
        ref = [r for r in ref if start <= r["posting_date"] <= end]

    parsed_keys = sorted((d.posting_date, d.amount) for d in details)
    ref_keys = sorted((r["posting_date"], r["amount"]) for r in ref)

    if len(parsed_keys) != len(ref_keys):
        issues.append(f"row count mismatch: parsed={len(parsed_keys)} csv={len(ref_keys)}")

    for pk, rk in zip(parsed_keys, ref_keys):
        if pk != rk:
            issues.append(f"(date, amount) mismatch: parsed={pk} csv={rk}")

    return issues


def _load_reference_csv(path: Path) -> list[dict]:
    lines = path.read_text().splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.lstrip("﻿").startswith("First Bank Card")
    )
    reader = csv.reader(lines[start:])
    rows = list(reader)
    hdr = [h.strip() for h in rows[0]]
    out: list[dict] = []
    for row in rows[1:]:
        if not row or not any(c.strip() for c in row):
            continue
        cells = dict(zip(hdr, row))
        out.append({
            "card": cells["First Bank Card"].strip("'"),
            "type": cells["Transaction Type"].strip(),
            "posting_date": _yyyymmdd(cells["Date Posted"].strip()),
            "amount": Decimal(cells["Transaction Amount"].strip()),
            "description": cells["Description"].strip(),
        })
    return out


def _yyyymmdd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


# --- header / summary parsing -----------------------------------------------

def _parse_period_end(text: str) -> date:
    m = _PERIOD_END.search(text)
    if not m:
        raise ValueError("period end not found")
    return date(int(m.group(3)), _FULL_MONTHS[m.group(1)], int(m.group(2)))


def _parse_summary_row(text: str) -> dict:
    """Extract the account summary line: `#<acct> opening deducted added closing`."""
    m = re.search(
        rf"#(\d{{4}}\s?\d{{4}}-\d{{3}})\s+"
        rf"({_AMOUNT})\s+({_AMOUNT})\s+({_AMOUNT})\s+({_AMOUNT})",
        text,
    )
    if not m:
        raise ValueError("summary row not found")
    return {
        "account_number": m.group(1),
        "opening_balance": _money(m.group(2)),
        "total_deducted": _money(m.group(3)),
        "total_added": _money(m.group(4)),
        "closing_balance": _money(m.group(5)),
    }


def _detect_account_type(text: str) -> str:
    """Find `<CamelCase>Account#<num>` and snake_case it, e.g. `primary_chequing`."""
    m = re.search(r"([A-Z][A-Za-z]+?)Account#\d", text)
    if not m:
        return "unknown"
    return _camel_to_snake(m.group(1))


def _camel_to_snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def _extract_owner(text: str) -> str:
    """Owner name — either from the squished all-caps line (`MSGRACEWILLIAMS`)
    or the spaced `MS GRACE WILLIAMS` form in the address block.
    """
    # Address-block form: two-or-more capitalised words followed by a street number.
    m = re.search(
        r"^((?:MS|MR|MRS|MISS|DR)?\s*[A-Z][A-Z]+\s+[A-Z][A-Z]+(?:\s+[A-Z][A-Z]+)*)\s*\n\d+\s",
        text,
        re.MULTILINE,
    )
    if m:
        return " ".join(m.group(1).split())
    # Owner-block squished form: `Owner:\n<ALLCAPSNAME>`.
    m = re.search(r"Owner:\s*\n([A-Z]+)\s*\n", text)
    return m.group(1) if m else ""


def _parse_details(
    text: str, period_end: date, account_number: str, opening_balance: Decimal
) -> tuple[date, list[DepositAccountDetail]]:
    """Walk the detail lines, using balance delta to sign each amount.

    Returns (period_start, details) where period_start is the date of the
    "Opening balance" row — the first day of the statement period.
    """
    details: list[DepositAccountDetail] = []
    last_balance = opening_balance
    period_start: date | None = None
    seen_opening = False
    in_details = False
    pending: dict | None = None

    def flush() -> None:
        nonlocal pending, last_balance
        if pending is None:
            return
        desc = re.sub(r"\s+", " ", pending["desc"]).strip()
        new_balance = pending["balance"]
        delta = new_balance - last_balance
        unsigned = pending["unsigned_amount"]
        if delta == unsigned:
            amount = unsigned
        elif delta == -unsigned:
            amount = -unsigned
        else:
            raise ValueError(
                f"balance delta {delta} doesn't match unsigned amount {unsigned} "
                f"on row: {desc!r}"
            )
        details.append(DepositAccountDetail(
            item_num=len(details) + 1,
            account_number=account_number,
            posting_date=pending["posting_date"],
            description=desc,
            amount=amount,
            running_balance=new_balance,
        ))
        last_balance = new_balance
        pending = None

    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("Here'swhathappenedinyouraccount"):
            in_details = True
            continue
        if not in_details:
            continue
        if _NOISE.match(ln):
            continue

        m = _ROW_LEAD.match(ln)
        if not m:
            if pending is not None:
                pending["desc"] += " " + ln
            continue

        month_abbr, day, rest = m.group(1), int(m.group(2)), m.group(3)
        posting_date = _infer_date(month_abbr, day, period_end)

        if (om := _OPENING_TAIL.match(rest)) is not None:
            flush()
            seen_opening = True
            period_start = posting_date
            opening_from_row = _money(om.group(1))
            if opening_from_row != opening_balance:
                raise ValueError(
                    f"opening balance from detail row ({opening_from_row}) "
                    f"disagrees with summary ({opening_balance})"
                )
            last_balance = opening_from_row
            continue

        if _CLOSING_TAIL.match(rest) is not None:
            flush()
            break

        if not seen_opening:
            continue

        if (dm := _DETAIL_TAIL.match(rest)) is not None:
            flush()
            pending = {
                "posting_date": posting_date,
                "desc": dm.group(1),
                "unsigned_amount": _money(dm.group(2)),
                "balance": _money(dm.group(3)),
            }
            continue

        if pending is not None:
            pending["desc"] += " " + rest

    flush()
    if period_start is None:
        raise ValueError("opening balance row not found")
    return period_start, details


def _infer_date(month_abbr: str, day: int, period_end: date) -> date:
    """Pick the most recent date ≤ period_end matching (month_abbr, day)."""
    month = _MONTHS[month_abbr]
    candidate = date(period_end.year, month, day)
    if candidate > period_end:
        candidate = date(period_end.year - 1, month, day)
    return candidate


def _search(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _money(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))
