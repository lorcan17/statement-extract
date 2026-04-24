"""Parser + validators for American Express Cobalt Card statements."""
from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..schema import CreditCardDetail, CreditCardHeader, CreditCardStatement, MembershipRewards

BANK = "amex"
PRODUCT = "cobalt"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
)}
_M = "(?:" + "|".join(_MONTHS) + ")"

_LONG_DATE = re.compile(rf"({_M})\s*(\d{{1,2}}),\s+(\d{{4}})")
_SHORT_DATE = re.compile(rf"({_M})\s*(\d{{1,2}})")
_TRANS_HEAD = re.compile(rf"^({_M})\s*(\d{{1,2}})\s+({_M})\s*(\d{{1,2}})\s+(.+?)\s+([-]?[\d,]+\.\d{{2}})$")
_FX_RATE = re.compile(r"^([A-Z\s]+)\s*([\d,]+\.\d{2})@\s*([\d.]+)$")

_NOISE_PREFIXES = (
    "Page ", "Customer Service:", "Prepared For", "Statement of Account", "Your Transactions",
    "Transaction Posting", "Date Date", "Total of", "New Payments", "New Transactions",
    "Other Account", "About Your", "Category Daily", "Membership Rewards", "Account Summary from",
    "americanexpress.ca", "Prepared For", "LORCAN TRAVERS", "AMERICAN EXPRESS", "THIS STATEMENT IS",
    "American Express Cobalt Card", "GRACE WILLIAMS", "LORCAN TRAVERS",
)


def parse(pdf_path: Path) -> tuple[CreditCardHeader, list[CreditCardDetail]]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    
    header = _parse_header(pages)
    # Transactions start on Page 2 and end before Page 6 (About Your Credit Limit)
    trans_text = ""
    for p in pages[1:]:
        if "About Your Credit Limit" in p:
            trans_text += p.split("About Your Credit Limit")[0]
            break
        trans_text += p + "\n"
        
    details = _parse_details(trans_text, header)
    return header, details


def build_statement(header: CreditCardHeader, details: list[CreditCardDetail]) -> CreditCardStatement:
    return CreditCardStatement(header=header, details=details)


# --- validation --------------------------------------------------------------

def validate_internal(header: CreditCardHeader, details: list[CreditCardDetail]) -> list[str]:
    issues: list[str] = []
    
    # In Amex, "Payments and credits" (header) usually only matches the "New Payments" section.
    # Other negative amounts (refunds) are typically bundled into "Purchases".
    payments_sum = sum((d.amount for d in details if "PAYMENT RECEIVED" in d.description.upper()), Decimal("0"))
    # Header payments_and_credits is positive
    if abs(payments_sum) != header.payments_and_credits:
         issues.append(f"payments_and_credits: header={header.payments_and_credits} detail_sum={abs(payments_sum)}")
         
    # Purchases in header includes refunds (negative transactions that are not payments)
    purchases_sum = sum((d.amount for d in details if "PAYMENT RECEIVED" not in d.description.upper() and "MEMBERSHIP FEE INSTALLMENT" not in d.description.upper()), Decimal("0"))
    if purchases_sum != header.purchases_and_other_charges:
        issues.append(f"purchases_and_other_charges: header={header.purchases_and_other_charges} detail_sum={purchases_sum}")

    fees_sum = sum((d.amount for d in details if "MEMBERSHIP FEE INSTALLMENT" in d.description.upper()), Decimal("0"))
    if fees_sum != header.fees:
        issues.append(f"fees: header={header.fees} detail_sum={fees_sum}")

    expected_balance = (
        header.previous_balance - header.payments_and_credits + header.purchases_and_other_charges 
        + header.fees + header.total_interest_charges + header.cash_advances
    )
    if expected_balance != header.total_balance:
        issues.append(f"total_balance: expected {expected_balance}, got {header.total_balance}")

    return issues


def validate_against_csv(details: list[CreditCardDetail], csv_path: Path) -> list[str]:
    issues: list[str] = []
    ref = _load_reference_csv(csv_path)

    if len(ref) != len(details):
        issues.append(f"row count mismatch: parsed={len(details)} csv={len(ref)}")

    # Sort both to ensure comparison matches
    # Primary key: date, Secondary: amount, Tertiary: description
    def sort_key(d):
        # handle dict (ref) or CreditCardDetail (parsed)
        if isinstance(d, dict):
            return (d["posting_date"], d["amount"], d["description"])
        return (d.posting_date, d.amount, d.description)

    sorted_parsed = sorted(details, key=sort_key)
    sorted_ref = sorted(ref, key=sort_key)
    
    for i, (parsed, r) in enumerate(zip(sorted_parsed, sorted_ref), 1):
        if parsed.amount != r["amount"]:
            issues.append(f"row {i} amount: parsed={parsed.amount} csv={r['amount']} (desc={parsed.description[:20]})")
        if parsed.transaction_date != r["transaction_date"]:
            issues.append(
                f"row {i} trans_date: parsed={parsed.transaction_date} csv={r['transaction_date']} (desc={parsed.description[:20]})"
            )
        if parsed.posting_date != r["posting_date"]:
            issues.append(
                f"row {i} posting_date: parsed={parsed.posting_date} csv={r['posting_date']} (desc={parsed.description[:20]})"
            )
    return issues


def _load_reference_csv(path: Path) -> list[dict]:
    # Amex CSV format: Date,Date Processed,Description,Card Member,Account #,Amount,...
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append({
                "transaction_date": _parse_csv_date(row["Date"]),
                "posting_date": _parse_csv_date(row["Date Processed"]),
                "description": row["Description"],
                "amount": Decimal(row["Amount"]),
            })
    # PDF usually shows transactions in chronological order of posting, 
    # but grouping by card member.
    # Let's see if we need to sort.
    return rows


def _parse_csv_date(s: str) -> date:
    # "14 Mar 2026"
    parts = s.split()
    day = int(parts[0])
    month = _MONTHS[parts[1]]
    year = int(parts[2])
    return date(year, month, day)


# --- header parsing ---------------------------------------------------------

def _parse_header(pages: list[str]) -> CreditCardHeader:
    text = pages[0]
    
    # "LORCAN TRAVERS XXXX XXXXX4 32008 Feb15, 2026 Mar14, 2026"
    header_line = re.search(
        rf"^(.+?)\s+(XXXX\s+XXXXX\d\s+(\d{{5}}))\s+({_M}\s*\d{{1,2}},\s+\d{{4}})\s+({_M}\s*\d{{1,2}},\s+\d{{4}})",
        text, re.MULTILINE
    )
    if not header_line:
        raise ValueError("Main header line not found")
        
    holder = header_line.group(1).strip()
    last5 = header_line.group(3)
    period_start = _parse_long_date(header_line.group(4))
    period_end = _parse_long_date(header_line.group(5))
    
    # Amex summary fields
    prev_bal = _money_after(text, r"Previous Balance\s+\$?")
    payments = _money_after(text, r"Less Payments\s+\$?")
    other_credits = _money_after(text, r"Less Other Credits\s+\$?")
    interest = _money_after(text, r"Plus Interest\s+\$?")
    purchases = _money_after(text, r"Plus Purchases\s+\$?")
    fees = _money_after(text, r"Plus Fees\s+\$?")
    advances = _money_after(text, r"Plus Credit Advances\s+\$?")
    other_charges = _money_after(text, r"Plus Other Charges\s+\$?")
    new_bal = _money_after(text, r"Equals New Balance\s+\$?")
    
    due_date = _find_date(text, r"Minimum Amount Dueon\s+") # Note the "Dueon" from text extraction
    min_due = _money_after(text, r"Minimum Amount Dueon\s+{_M}\s*\d{{1,2}},\s+\d{{4}}\s+\$?".format(_M=_M))
    
    limit = _money_after(text, r"Credit Limit\s+\$?")
    avail = _money_after(text, r"Available Credit Limit\s+\$?")

    rewards = _parse_rewards(pages)

    return CreditCardHeader(
        bank=BANK,
        product=PRODUCT,
        account_holder=holder,
        card_number_last4=last5, # Actually last 5 for Amex
        statement_date=period_end,
        period_start=period_start,
        period_end=period_end,
        payment_due_date=due_date,
        previous_balance=prev_bal,
        payments_and_credits=payments + other_credits,
        purchases_and_other_charges=purchases + other_charges,
        new_installments=Decimal("0"),
        cash_advances=advances,
        total_interest_charges=interest,
        fees=fees,
        total_balance=new_bal,
        minimum_payment_due=min_due,
        credit_limit=limit,
        available_credit=avail,
        rewards=rewards,
    )


def _parse_rewards(pages: list[str]) -> MembershipRewards | None:
    # Membership Rewards is usually on a later page
    for page in pages:
        if "Membership Rewards" in page and "Previous Points" in page:
            lines = page.splitlines()
            for i, ln in enumerate(lines):
                if "Previous Points" in ln and "Earned" in ln:
                    # Look at next few lines for the numeric data
                    for j in range(1, 4):
                        if i + j >= len(lines):
                            break
                        data_ln = lines[i+j].strip()
                        if not data_ln:
                            continue
                        parts = [p.replace(",", "") for p in data_ln.split()]
                        # We expect at least 6 numeric parts
                        numeric_parts = []
                        for p in parts:
                            if p.isdigit():
                                numeric_parts.append(int(p))
                            else:
                                # Sometimes there is a code like CM02991806 at the end
                                break
                        
                        if len(numeric_parts) >= 6:
                            return MembershipRewards(
                                previous_balance=numeric_parts[0],
                                earned=numeric_parts[1],
                                bonus=numeric_parts[2],
                                adjusted=numeric_parts[3],
                                redeemed=numeric_parts[4],
                                new_balance=numeric_parts[5],
                            )
    return None


def _parse_details(text: str, header: CreditCardHeader) -> list[CreditCardDetail]:
    details: list[CreditCardDetail] = []
    pending: dict | None = None
    current_card = header.card_number_last4

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        
        desc = pending["desc"].strip()
        fx_ccy = fx_amt = fx_rate = None
        
        # Check for FX line in description
        if "fx_line" in pending:
            m = _FX_RATE.match(pending["fx_line"])
            if m:
                fx_ccy = m.group(1).strip()
                fx_amt = Decimal(m.group(2).replace(",", ""))
                fx_rate = Decimal(m.group(3))
        
        details.append(CreditCardDetail(
            item_num=len(details) + 1,
            card_number=current_card,
            transaction_date=pending["trans"],
            posting_date=pending["post"],
            amount=pending["amount"],
            description=desc,
            original_currency=fx_ccy,
            original_amount=fx_amt,
            exchange_rate=fx_rate,
        ))
        pending = None

    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or any(ln.startswith(p) for p in _NOISE_PREFIXES):
            continue
            
        # Switch current card if we see "New Transactions for..."
        if ln.startswith("New Transactions for"):
             # "New Transactions forGRACE WILLIAMS"
             # We don't have the last 4 for supplementary cards easily from text
             # but we can try to find it or just use holder name.
             # Actually, the CSV has Account # field.
             pass

        m = _TRANS_HEAD.match(ln)
        if m:
            flush()
            pending = {
                "trans": _year_for(m.group(1), int(m.group(2)), header),
                "post": _year_for(m.group(3), int(m.group(4)), header),
                "desc": m.group(5),
                "amount": Decimal(m.group(6).replace(",", "")),
            }
        elif pending is not None:
            if "@" in ln and any(ccy in ln for ccy in ["UNITED STATES DOLLAR", "EURO", "BRITISH POUND"]):
                pending["fx_line"] = ln
            elif "Reference" in ln:
                pass # Ignore reference numbers for now
            elif "ARRIVAL" in ln or "DEPARTURE" in ln or "NIGHTS" in ln:
                pass # Ignore hotel details
            elif re.match(r"^\d{2}/\d{2}/\d{2}", ln):
                pass # Ignore date ranges in desc
            else:
                pending["desc"] += " " + ln
    flush()
    return details


def _year_for(month_abbr: str, day: int, header: CreditCardHeader) -> date:
    month = _MONTHS[month_abbr]
    if header.period_start.year == header.period_end.year:
        return date(header.period_start.year, month, day)
    if month >= header.period_start.month:
        return date(header.period_start.year, month, day)
    return date(header.period_end.year, month, day)


def _find_date(text: str, prefix: str) -> date:
    m = re.search(prefix + rf"({_M}\s*\d{{1,2}},\s+\d{{4}})", text)
    if not m:
        raise ValueError(f"date not found after: {prefix!r}")
    return _parse_long_date(m.group(1))


def _parse_long_date(s: str) -> date:
    m = _LONG_DATE.search(s)
    if not m:
        raise ValueError(f"cannot parse long date: {s!r}")
    return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


def _money_after(text: str, prefix: str, *, default: Decimal | None = None) -> Decimal:
    m = re.search(prefix + r"([-]?\$?[\d,]+\.\d{2})", text)
    if not m:
        if default is not None:
            return default
        raise ValueError(f"amount not found after: {prefix!r}")
    return Decimal(m.group(1).replace("$", "").replace(",", ""))
