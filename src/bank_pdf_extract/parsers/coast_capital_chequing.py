"""Parser for Coast Capital Savings chequing/savings statements."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..schema import (
    DepositAccountDetail,
    DepositAccountHeader,
    DepositAccountStatement,
    MultiAccountDepositStatement,
)

BANK = "coast_capital"
PRODUCT = "chequing_savings"

_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_M = "(?:" + "|".join(_MONTH_MAP) + ")"

# 26 MAR 25 Interac e-Transfer Debit 476.00 5,900.07
# Trailing " OD" marks an overdraft on the running balance (e.g. "3.40 OD").
_TRANS_LINE = re.compile(rf"^(\d{{2}})\s+({_M})\s+(\d{{2}})\s+(.+?)\s+([\d,]+\.\d{{2}})\s+([\d,]+\.\d{{2}})(\s+OD)?$")
# 01 APR 25 Interest Paid 0.35 0.95
_SHORT_TRANS_LINE = re.compile(rf"^(\d{{2}})\s+({_M})\s+(\d{{2}})\s+(.+?)\s+([\d,]+\.\d{{2}})(\s+OD)?$")

_CID_RE = re.compile(r"\(cid:\d+\)")


def parse(pdf_path: Path) -> MultiAccountDepositStatement:
    # Some Coast Capital statements (Oct 2024 onward) embed glyphs the PDF font
    # doesn't map cleanly, so pdfplumber emits `(cid:NN)` placeholders inline
    # — typically standing in for spaces. Strip them at extraction time so the
    # downstream regexes see normal whitespace.
    def _clean(t: str) -> str:
        return _CID_RE.sub(" ", t)

    with pdfplumber.open(str(pdf_path)) as pdf:
        all_lines = []
        page1_text = _clean(pdf.pages[0].extract_text() or "") if pdf.pages else ""
        for p in pdf.pages:
            text = _clean(p.extract_text() or "")
            all_lines.extend(text.splitlines())

    # Coast Capital prints the canonical holder line immediately after
    # "Statement of Account for" — joint accounts render as
    # "<Title>.<Name1> / <Title>.<Name2>".
    holder_m = re.search(r"Statement of Account for\s*\n\s*(.+)", page1_text)
    account_holder = holder_m.group(1).strip() if holder_m else ""
    # Don't add holder names to noise prefixes: transaction descriptions
    # often embed a holder name (e.g. card-payment lines), so dropping
    # those lines would break multi-line description stitching.
    noise_names: tuple[str, ...] = ()

    # Branch name is the first non-empty line on page 1 (Coast Capital
    # prints it as a standalone heading). Transit number isn't printed on
    # chequing statements — leave empty.
    branch_name = ""
    for ln in page1_text.splitlines():
        ln = ln.strip()
        if ln and not re.match(r"^[\d\s\-()]+$", ln):
            branch_name = ln
            break
    transit_number = ""
    
    # Global header info from Page 1
    period_m = None
    for ln in all_lines:
        period_m = re.search(rf"({_M})\s+(\d{{1,2}}),\s+(\d{{4}})\s*-\s*({_M})\s+(\d{{1,2}}),\s+(\d{{4}})", ln)
        if period_m: break
        
    if not period_m:
        raise ValueError("Global statement period not found")
    
    period_start = date(int(period_m.group(3)), _MONTH_MAP[period_m.group(1)], int(period_m.group(2)))
    period_end = date(int(period_m.group(6)), _MONTH_MAP[period_m.group(4)], int(period_m.group(5)))
    
    accounts = []
    current_acc_header = None
    current_details = []
    pending_item = None
    pending_desc = ""
    
    def flush_current_account():
        nonlocal current_acc_header, current_details, pending_item, pending_desc
        if not current_acc_header:
            return
        if pending_item:
            current_details.append(pending_item.model_copy(update={"description": pending_desc.strip()}))
            pending_item = None
            pending_desc = ""
        
        if current_details:
            current_acc_header = current_acc_header.model_copy(update={"closing_balance": current_details[-1].running_balance})
        else:
            current_acc_header = current_acc_header.model_copy(update={"closing_balance": current_acc_header.opening_balance})
        
        accounts.append(DepositAccountStatement(header=current_acc_header, details=current_details))
        current_acc_header = None
        current_details = []

    # State flags
    in_account = False
    
    for line in all_lines:
        ln = line.strip()
        if not ln or ln.startswith("CONTINUED...") or "PAGE" in ln or "MEMBER NUMBER" in ln:
            continue
        if any(ln.startswith(n) for n in noise_names):
            continue
            
        # Check for start of new account header
        header_m = re.search(r"([A-Za-z\-\s]+(?:Account|Acct))\s+WITHDRAWALS\s+DEPOSITS\s+BALANCE", ln)
        if header_m:
            # We don't flush yet because the same account might span pages and repeat header.
            # We only flush when we see a DIFFERENT account number or Net Change or End.
            in_account = True
            # Temporarily store header title to check against acc number
            last_header_title = header_m.group(1).strip()
            continue

        if not in_account: continue
        
        # Account number line
        acc_m = re.search(r"(\d{12})\s+\((\d{3})\)", ln)
        if acc_m:
            new_acc_num = acc_m.group(1)
            if current_acc_header and current_acc_header.account_number != new_acc_num:
                flush_current_account()
            
            if not current_acc_header:
                current_acc_header = DepositAccountHeader(
                    bank=BANK,
                    product=PRODUCT,
                    account_type=last_header_title,
                    account_holder=account_holder,
                    account_number=new_acc_num,
                    branch_name=branch_name,
                    transit_number=transit_number,
                    plan_name=last_header_title,
                    period_start=period_start,
                    period_end=period_end,
                    opening_balance=0,
                    total_deducted=0,
                    total_added=0,
                    closing_balance=0
                )
            continue
            
        # Balance Forward
        open_m = re.search(fr"(\d{{2}}\s+({_M})\s+\d{{2}})\s+Balance Forward\s+([\d,]+\.\d{{2}})", ln)
        if open_m and current_acc_header:
            # Only update opening balance if not already set (don't override on page 2)
            if current_acc_header.opening_balance == 0:
                 current_acc_header = current_acc_header.model_copy(update={"opening_balance": Decimal(open_m.group(3).replace(",", ""))})
            continue
            
        # Totals / End of account
        if ln.startswith("Total Withdrawals") and current_acc_header:
            m = re.search(r"Total Withdrawals\s+([\d,]+\.\d{2})", ln)
            if m:
                current_acc_header = current_acc_header.model_copy(update={
                    "total_deducted": Decimal(m.group(1).replace(",", ""))
                })
            continue

        if ln.startswith("Total Deposits") and current_acc_header:
            m = re.search(r"Total Deposits\s+([\d,]+\.\d{2})", ln)
            if m:
                current_acc_header = current_acc_header.model_copy(update={
                    "total_added": Decimal(m.group(1).replace(",", ""))
                })
            continue

        if ln.startswith("Net Change") and current_acc_header:
             flush_current_account()
             in_account = False
             continue

        if any(x in ln for x in ["Total Assets", "End of Statement", "Your Financial Summary"]):
             flush_current_account()
             in_account = False
             continue
             
        # Transaction line
        m = _TRANS_LINE.match(ln)
        ms = _SHORT_TRANS_LINE.match(ln) if not m else None
        
        if m or ms:
            match = m or ms
            if pending_item:
                current_details.append(pending_item.model_copy(update={"description": pending_desc.strip()}))
            
            day = int(match.group(1))
            month = _MONTH_MAP[match.group(2)]
            year = 2000 + int(match.group(3))
            post_date = date(year, month, day)
            
            desc = match.group(4)
            if m:
                val2 = Decimal(m.group(6).replace(",", ""))
                od_marker = m.group(7)
            else:
                val2 = Decimal(ms.group(5).replace(",", ""))
                od_marker = ms.group(6)
            if od_marker:
                # "3.40 OD" → overdraft, running balance is negative.
                val2 = -val2

            prev_bal = current_details[-1].running_balance if current_details else current_acc_header.opening_balance
            amount = val2 - prev_bal
            
            pending_item = DepositAccountDetail(
                item_num=len(current_details) + 1,
                account_number=current_acc_header.account_number,
                posting_date=post_date,
                description=desc,
                amount=amount,
                running_balance=val2
            )
            pending_desc = desc
        elif pending_item:
            # Multi-line description noise filtering
            if not any(x in ln for x in ["Chequing/Savings", "Statement of Account", "Total Assets", "End of Statement"]):
                pending_desc += " " + ln

    # Final flush
    flush_current_account()

    return MultiAccountDepositStatement(
        bank=BANK,
        period_start=period_start,
        period_end=period_end,
        accounts=accounts
    )

def validate_internal(stmt: MultiAccountDepositStatement) -> list[str]:
    issues = []
    if not stmt.accounts:
        issues.append("no accounts parsed (parser produced an empty statement)")
        return issues
    for acc in stmt.accounts:
        h = acc.header
        d = acc.details
        
        calc_wd = sum(abs(i.amount) for i in d if i.amount < 0)
        calc_dep = sum(i.amount for i in d if i.amount > 0)
        
        if abs(calc_wd - h.total_deducted) > Decimal("0.01"):
            issues.append(f"Account {h.account_number} ({h.account_type}) withdrawals mismatch: header={h.total_deducted} calc={calc_wd}")
        if abs(calc_dep - h.total_added) > Decimal("0.01"):
            issues.append(f"Account {h.account_number} ({h.account_type}) deposits mismatch: header={h.total_added} calc={calc_dep}")
            
        expected_closing = h.opening_balance - h.total_deducted + h.total_added
        if abs(expected_closing - h.closing_balance) > Decimal("0.01"):
            issues.append(f"Account {h.account_number} ({h.account_type}) closing balance mismatch: expected={expected_closing} got={h.closing_balance}")
            
    return issues

def validate_against_csv(stmt: MultiAccountDepositStatement, csv_path: Path) -> list[str]:
    # CSV format: Date,Description,Amount,Balance,Unique ID
    # Date: Jan-31-2026
    # Amount/Balance: "$ -1,700.00"
    issues: list[str] = []
    ref_rows = []
    import csv
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ref_rows.append({
                "date": _parse_csv_date(row["Date"]),
                "amount": _parse_csv_money(row["Amount"]),
                "balance": _parse_csv_money(row["Balance"]),
                "desc": row["Description"],
                "uid": row["Unique ID"]
            })

    for acc in stmt.accounts:
        h = acc.header
        acc_ref = [r for r in ref_rows if h.period_start <= r["date"] <= h.period_end]
        if not acc_ref:
            continue
            
        for d in acc.details:
            matches = [r for r in acc_ref if r["date"] == d.posting_date and abs(r["amount"] - d.amount) < Decimal("0.01")]
            if not matches:
                issues.append(f"Account {h.account_number}: Item not found in CSV: {d.posting_date} {d.description[:30]} {d.amount}")
                
    return issues

def _parse_csv_date(s: str) -> date:
    # Jan-31-2026
    months = {"Jan":1, "Feb":2, "Mar":3, "Apr":4, "May":5, "Jun":6,
              "Jul":7, "Aug":8, "Sep":9, "Oct":10, "Nov":11, "Dec":12}
    parts = s.split("-")
    return date(int(parts[2]), months[parts[0]], int(parts[1]))

def _parse_csv_money(s: str) -> Decimal:
    # "$ -1,700.00" or "$ 8,251.26"
    clean = s.replace("$", "").replace(",", "").replace(" ", "")
    return Decimal(clean)
