"""Parser for Coast Capital Savings (Collabria) credit card statements."""
from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..schema import CollabriaRewards, CreditCardDetail, CreditCardHeader, CreditCardStatement

BANK = "coast_capital"
PRODUCT = "visa"

_TRANS_LINE = re.compile(r"^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s+([A-Z0-9]{17})\s+([-]?\$[\d,]+\.\d{2})$")
_FEE_LINE = re.compile(r"^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s+([-]?\$[\d,]+\.\d{2})$")
_SUMMARY_MONEY = re.compile(r"([-]?\$[\d,]+\.\d{2})")

_NOISE_PREFIXES = (
    "ACCOUNT NUMBER:", "SUMMARY OF ACCOUNT", "PAYMENT INFORMATION",
    "CONTACT US", "Account Number Ending", "Cardholder Service", "Credit Limit",
    "Available Credit", "Annual Interest Rate", "Daily Interest Rate",
    "New Balance", "Minimum Payment Due", "Payment Due Date", "Amount Past Due",
    "An amount preceded", "See reverse side", "Please retain", "LORCAN TRAVERS",
    "COAST CAPITAL FSC", "C/O Collabria", "#450, 110-9th Ave", "Calgary, AB",
    "TRANSACTIONS", "Tran Post Reference", "Date Date Description Number",
    "FEES", "TOTAL FEES", "INTEREST", "Interest Charge on", "TOTAL INTEREST",
    "TOTAL *FINANCE CHARGE*", "INTEREST CHARGED", "Type of Balance", "REWARDS",
    "POINTS PRIOR", "POINTS EARNED", "POINTS ADJUSTED", "POINTS REDEEMED",
    "TOTAL POINTS", "Statement Period:", "Errors in Your", "Report Lost",
    "Address Change:", "Making Payments", "Applying Your", "Missed Payments:",
    "How We Charge", "Foreign Currency",
)

def parse(pdf_path: Path) -> tuple[CreditCardHeader, list[CreditCardDetail]]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    
    header = _parse_header(pages)
    details = _parse_details(pages, header)
    return header, details

def _parse_header(pages: list[str]) -> CreditCardHeader:
    text = pages[0]
    
    # "Statement Period 13/12/2025 - 13/01/2026"
    period = re.search(r"Statement Period\s+(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})", text)
    if not period:
        raise ValueError("Statement period not found")
    period_start = _parse_slash_date(period.group(1))
    period_end = _parse_slash_date(period.group(2))
    
    # Summary of Account Activity
    def get_summary_value(label: str) -> Decimal:
        m = re.search(label + r"\s+([-]?\$[\d,]+\.\d{2})", text)
        if not m:
            return Decimal("0")
        return Decimal(m.group(1).replace("$", "").replace(",", ""))

    prev_bal = get_summary_value("Previous Balance")
    payments = get_summary_value("-Payments")
    other_credits = get_summary_value("-Other Credits")
    purchases = get_summary_value(r"\+Purchases")
    other_debits = get_summary_value(r"\+Other Debits")
    cash_advances = get_summary_value(r"\+Cash Advances")
    fees = get_summary_value(r"\+Fees Charged")
    interest = get_summary_value(r"\+Interest Charged")
    new_bal = get_summary_value("New Balance")
    
    min_due = get_summary_value("Minimum Payment Due")
    due_date = _find_date(text, r"Payment Due Date\s+")
    
    limit = get_summary_value("Credit Limit")
    avail = get_summary_value("Available Credit Limit") # May be on different line
    if limit == 0: # Try alternative
         m = re.search(r"Credit Limit\s+\$([\d,]+\.\d{2})", text)
         if m: limit = Decimal(m.group(1).replace(",", ""))
    m = re.search(r"Available Credit\s+\$([\d,]+\.\d{2})", text)
    if m: avail = Decimal(m.group(1).replace(",", ""))

    # Cards - find all card numbers
    card_nrs = re.findall(r"Account Number Ending In (\d{4})", text)
    if not card_nrs:
        # Try from transactions if not in header
        card_nrs = re.findall(r"Account (\d{4})", "\n".join(pages))
    
    # Rewards - usually at the end of the transaction list
    rewards = _parse_rewards(pages)

    return CreditCardHeader(
        bank=BANK,
        product=PRODUCT,
        account_holder="LORCAN TRAVERS", # Hardcoded or find on p1
        card_number_last4=",".join(sorted(list(set(card_nrs)))),
        statement_date=period_end,
        period_start=period_start,
        period_end=period_end,
        payment_due_date=due_date,
        previous_balance=prev_bal,
        payments_and_credits=payments + other_credits,
        purchases_and_other_charges=purchases + other_debits,
        new_installments=Decimal("0"),
        cash_advances=cash_advances,
        total_interest_charges=interest,
        fees=fees,
        total_balance=new_bal,
        minimum_payment_due=min_due,
        credit_limit=limit,
        available_credit=avail,
        rewards=rewards,
    )

def _parse_rewards(pages: list[str]) -> CollabriaRewards | None:
    text = "\n".join(pages)
    if "REWARDS" not in text:
        return None
    
    def get_val(label: str) -> int:
        m = re.search(label + r"\s+([\d,]+)", text)
        return int(m.group(1).replace(",", "")) if m else 0

    return CollabriaRewards(
        previous_balance=get_val("POINTS PRIOR"),
        earned=get_val("POINTS EARNED"),
        adjusted=get_val("POINTS ADJUSTED"),
        redeemed=get_val("POINTS REDEEMED"),
        new_balance=get_val("TOTAL POINTS AVAILABLE"),
    )

def _parse_details(pages: list[str], header: CreditCardHeader) -> list[CreditCardDetail]:
    details: list[CreditCardDetail] = []
    current_card = ""
    
    # Process pages starting from transaction list (usually page 3)
    # but some might be on page 1 if list is short.
    full_text = "\n".join(pages)
    
    # Find the TRANSACTIONS section
    if "TRANSACTIONS" in full_text:
        trans_section = full_text.split("TRANSACTIONS", 1)[1]
    else:
        trans_section = full_text

    for line in trans_section.splitlines():
        ln = line.strip()
        if not ln or any(ln.startswith(p) for p in _NOISE_PREFIXES):
            continue
            
        # Check for card header "Account 3534"
        card_m = re.match(r"^Account\s+(\d{4})$", ln)
        if card_m:
            current_card = card_m.group(1)
            continue
            
        # Regular transaction line
        # 13/12 13/12 SIMON'S NO FRILLS #367 VANCOUVER BC 5518136AVTN1AYDER $6.14
        m = _TRANS_LINE.match(ln)
        if m:
            trans_date_str = m.group(1)
            post_date_str = m.group(2)
            desc = m.group(3)
            # Reference number is m.group(4)
            amount = Decimal(m.group(5).replace("$", "").replace(",", ""))
            
            details.append(CreditCardDetail(
                item_num=len(details) + 1,
                card_number=current_card,
                transaction_date=_year_for(trans_date_str, header),
                posting_date=_year_for(post_date_str, header),
                amount=amount,
                description=desc,
            ))
            continue
            
        # Fee or Interest line (often shorter)
        # 13/01 13/01 Interest Charge on Purchases $0.00
        m = _FEE_LINE.match(ln)
        if m and ( "Interest" in ln or "Fee" in ln ):
             amount = Decimal(m.group(4).replace("$", "").replace(",", ""))
             if amount == 0: continue # Skip zero interest/fees
             
             details.append(CreditCardDetail(
                item_num=len(details) + 1,
                card_number=current_card,
                transaction_date=_year_for(m.group(1), header),
                posting_date=_year_for(m.group(2), header),
                amount=amount,
                description=m.group(3),
            ))

    return details

def _year_for(date_str: str, header: CreditCardHeader) -> date:
    # date_str is "DD/MM"
    day, month = map(int, date_str.split("/"))
    # Try current year of period_end first
    d = date(header.period_end.year, month, day)
    
    # If it's way after period_end, it might be from previous year
    if d > header.period_end + datetime.timedelta(days=30):
        d = date(header.period_end.year - 1, month, day)
    # If it's way before period_start, it might be from next year (unlikely for transactions)
    elif d < header.period_start - datetime.timedelta(days=30):
        d = date(header.period_end.year + 1, month, day)
        
    return d

import datetime # Need this for timedelta

def _parse_slash_date(s: str) -> date:
    # "13/12/2025"
    d, m, y = map(int, s.split("/"))
    return date(y, m, d)

def _find_date(text: str, prefix: str) -> date:
    m = re.search(prefix + r"(\d{2}/\d{2}/\d{4})", text)
    if not m:
        raise ValueError(f"Date not found for prefix {prefix}")
    return _parse_slash_date(m.group(1))

def validate_internal(header: CreditCardHeader, details: list[CreditCardDetail]) -> list[str]:
    issues: list[str] = []
    
    payments = sum((d.amount for d in details if d.amount < 0), Decimal("0"))
    charges = sum((d.amount for d in details if d.amount > 0), Decimal("0"))
    
    if abs(payments) != header.payments_and_credits:
         issues.append(f"payments_and_credits: header={header.payments_and_credits} detail_sum={abs(payments)}")
         
    expected_charges = header.purchases_and_other_charges + header.fees + header.total_interest_charges + header.cash_advances
    if charges != expected_charges:
        issues.append(f"charges: expected {expected_charges} detail_sum={charges}")

    expected_balance = header.previous_balance - header.payments_and_credits + expected_charges
    if abs(expected_balance - header.total_balance) > Decimal("0.01"):
        issues.append(f"total_balance: expected {expected_balance}, got {header.total_balance}")

    return issues

def validate_against_csv(details: list[CreditCardDetail], csv_path: Path) -> list[str]:
    # Collabria CSV format: Card Number Last 4,Transaction Date,Posted Date,Merchant Description,Transaction Type,Transaction Amount
    # Note: CSV amount is negative for debits (purchases) and positive for credits (payments)
    # PDF is opposite (positive for purchases, negative for payments)
    issues: list[str] = []
    ref = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ref.append({
                "card": row["Card Number Last 4"],
                "trans_date": _parse_mmddyyyy(row["Transaction Date"]),
                "amount": -Decimal(row["Transaction Amount"]), # Flip to match PDF
                "desc": row["Merchant Description"],
            })
            
    if len(ref) != len(details):
        # The CSV might contain more transactions than one statement
        # We should filter ref to match the statement period
        pass

    # Sort for comparison
    def sort_key(d):
        if isinstance(d, dict):
             return (d["trans_date"], abs(d["amount"]), d["desc"][:10])
        return (d.transaction_date, abs(d.amount), d.description[:10])

    # For now, just check if all details exist in ref
    for d in details:
        found = False
        for r in ref:
            if d.card_number == r["card"] and d.transaction_date == r["trans_date"] and abs(d.amount - r["amount"]) < Decimal("0.01"):
                found = True
                break
        if not found:
            issues.append(f"Item not found in CSV: {d.transaction_date} {d.description} {d.amount}")
            
    return issues

def _parse_mmddyyyy(s: str) -> date:
    # "04/17/2026"
    m, d, y = map(int, s.split("/"))
    return date(y, m, d)
