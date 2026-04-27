"""Parser + validators for BMO CashBack World Elite Mastercard statements."""
from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..schema import CashbackRewards, CreditCardDetail, CreditCardHeader, CreditCardStatement

BANK = "bmo"
PRODUCT = "cashback_world_elite_mastercard"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
)}
_M = "(?:" + "|".join(_MONTHS) + ")"

_LONG_DATE = re.compile(rf"({_M})\.?\s+(\d{{1,2}}),\s+(\d{{4}})")
# Trans-day → post-month separator allows zero whitespace ("Dec. 10Dec. 10"):
# pdfplumber occasionally drops that gap when the columns are tightly packed.
_TRANS_HEAD = re.compile(rf"^({_M})\.?\s+(\d{{1,2}})\s*({_M})\.?\s+(\d{{1,2}})\s+(.+)$")
_AMOUNT_TAIL = re.compile(r"^(.*)\s+([\d,]+\.\d{2})(\s+CR)?(?:\s+.+)?\s*$")
_FX_PREFIX = re.compile(r"^([A-Z]{3})\s+([\d,]+\.\d{2})@([\d.]+)\s+(.+)$")

_NOISE_PREFIXES = (
    "Page ", "(continued", "Transactions since", "TRANS ", "DATE ",
    "BMO CashBack", "Card number",
)


def parse(pdf_path: Path) -> tuple[CreditCardHeader, list[CreditCardDetail]]:
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    if _is_legacy_layout(pages[0]):
        return _parse_legacy(pages)
    header = _parse_header(pages[0])
    details = _parse_details("\n".join(pages[2:]), header)
    return header, details


def _is_legacy_layout(page1: str) -> bool:
    # 2023-era statements have the squished "PERIODCOVEREDBYTHISSTATEMENT" marker
    # and no modern "Statement period ..." line.
    return "PERIODCOVEREDBYTHISSTATEMENT" in page1 and "Statement period" not in page1


def build_statement(header: CreditCardHeader, details: list[CreditCardDetail]) -> CreditCardStatement:
    return CreditCardStatement(header=header, details=details)


# --- validation --------------------------------------------------------------

def validate_internal(header: CreditCardHeader, details: list[CreditCardDetail]) -> list[str]:
    """Return discrepancies between header totals and detail rows. Empty = OK."""
    issues: list[str] = []

    # Most rows split cleanly by sign, but a dishonoured payment shows up as
    # both `AUTOMATIC PYMT RECEIVED` (negative) and `PAYMENT ADJUSTMENT`
    # (positive reversal). BMO nets these into `Payments and credits`, not
    # into charges — so route any "PAYMENT ADJUSTMENT/REVERSAL" row into the
    # credits bucket regardless of sign.
    def _is_credit_side(d):
        u = d.description.upper()
        return d.amount < 0 or "PAYMENT ADJUSTMENT" in u or "PAYMENT REVERSAL" in u
    credits = sum((d.amount for d in details if _is_credit_side(d)), Decimal("0"))
    charges = sum((d.amount for d in details if not _is_credit_side(d)), Decimal("0"))

    # Detail rows lump every "cost to cardholder" transaction together
    # (purchases + fees + interest + cash advances + installments).
    expected_charges = (
        header.purchases_and_other_charges + header.fees + header.cash_advances
        + header.total_interest_charges + header.new_installments
    )
    if credits != header.payments_and_credits:
        issues.append(f"payments_and_credits: header={header.payments_and_credits} detail_sum={credits}")
    if charges != expected_charges:
        issues.append(f"charges: expected {expected_charges} detail_sum={charges}")

    expected = (
        header.previous_balance + header.payments_and_credits + header.purchases_and_other_charges
        + header.new_installments + header.cash_advances + header.total_interest_charges + header.fees
    )
    if expected != header.total_balance:
        issues.append(f"total_balance: expected {expected}, got {header.total_balance}")

    for d in details:
        if not (header.period_start <= d.posting_date <= header.period_end):
            issues.append(
                f"posting_date out of period: item {d.item_num} {d.posting_date} "
                f"not in [{header.period_start}, {header.period_end}]"
            )

    return issues


def validate_against_csv(details: list[CreditCardDetail], csv_path: Path) -> list[str]:
    """Return row-level discrepancies between parsed details and a reference CSV."""
    issues: list[str] = []
    ref = _load_reference_csv(csv_path)

    if len(ref) != len(details):
        issues.append(f"row count mismatch: parsed={len(details)} csv={len(ref)}")

    for parsed, r in zip(details, ref):
        if parsed.amount != r["amount"]:
            issues.append(f"item {parsed.item_num} amount: parsed={parsed.amount} csv={r['amount']}")
        if parsed.transaction_date != r["transaction_date"]:
            issues.append(
                f"item {parsed.item_num} trans_date: parsed={parsed.transaction_date} csv={r['transaction_date']}"
            )
        if parsed.posting_date != r["posting_date"]:
            issues.append(
                f"item {parsed.item_num} posting_date: parsed={parsed.posting_date} csv={r['posting_date']}"
            )
        if _norm(parsed.description) != _norm(r["description"]):
            issues.append(
                f"item {parsed.item_num} description: parsed={parsed.description!r} csv={r['description']!r}"
            )

    return issues


def _norm(s: str) -> str:
    return " ".join(s.upper().split())


def _load_reference_csv(path: Path) -> list[dict]:
    lines = path.read_text().splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Item #"))
    reader = csv.DictReader(lines[start:])
    return [
        {
            "item_num": int(row["Item #"]),
            "card_number": row["Card #"].strip("'"),
            "transaction_date": _yyyymmdd(row["Transaction Date"]),
            "posting_date": _yyyymmdd(row["Posting Date"]),
            "amount": Decimal(row["Transaction Amount"]),
            "description": row["Description"],
        }
        for row in reader
    ]


def _yyyymmdd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


# --- header parsing ---------------------------------------------------------

def _parse_header(text: str) -> CreditCardHeader:
    period = re.search(
        rf"Statement period\s+({_M}\.?\s+\d{{1,2}},\s+\d{{4}})\s*[-–]\s*"
        rf"({_M}\.?\s+\d{{1,2}},\s+\d{{4}})",
        text,
    )
    if not period:
        raise ValueError("statement period not found")

    holder = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*\nCard number XXXX XXXX XXXX", text)
    last4 = re.search(r"Card number XXXX XXXX XXXX (\d{4})", text)
    ir_pur = re.search(r"^Purchases\s+[\d.]+\s+([\d.]+)\s+[\d.]+", text, re.MULTILINE)
    ir_cash = re.search(r"Cash Advances\d?\s+[\d.]+\s+([\d.]+)\s+[\d.]+", text)

    return CreditCardHeader(
        bank=BANK,
        product=PRODUCT,
        account_holder=holder.group(1).strip() if holder else "",
        card_number_last4=last4.group(1) if last4 else "",
        statement_date=_find_date(text, r"Statement date\s+"),
        period_start=_parse_long_date(period.group(1)),
        period_end=_parse_long_date(period.group(2)),
        payment_due_date=_find_date(text, r"Payment due date:?\s+"),
        previous_balance=_money_after(text, r"Previous (?:total )?balance[^\n]*?\s+\$?"),
        payments_and_credits=_money_after(text, r"Payments and credits\s+"),
        purchases_and_other_charges=_money_after(text, r"Purchases and other charges\s+\+?"),
        new_installments=_money_after(text, r"New installments\s+", default=Decimal("0")),
        cash_advances=_money_after(text, r"Cash advances\d*\s+"),
        total_interest_charges=_money_after(text, r"Total interest charges\s+"),
        fees=_money_after(text, r"^Fees\s+", default=Decimal("0")),
        total_balance=_money_after(text, r"Total balance\s+\$?"),
        minimum_payment_due=_money_after(text, r"Minimum payment due\s+\$?"),
        credit_limit=_money_after(text, r"Your credit limit\s+\$?"),
        available_credit=_money_after(text, r"Your available credit\s+\$?"),
        interest_rate_purchases=Decimal(ir_pur.group(1)) if ir_pur else None,
        interest_rate_cash_advances=Decimal(ir_cash.group(1)) if ir_cash else None,
        rewards=_parse_rewards(text),
    )


def _parse_rewards(text: str) -> CashbackRewards:
    zero = Decimal("0")
    return CashbackRewards(
        base_earned=_money_after(text, r"(?<!Bonus )Cashback earned\s+\$?", default=zero),
        bonus_earned=_money_after(text, r"Bonus Cashback earned\s+\$?", default=zero),
        bonus_groceries=_money_after(text, r"Groceries\s+\$?", default=zero),
        bonus_ground_travel=_money_after(text, r"Ground travel\s+\$?", default=zero),
        bonus_gas_ev_charging=_money_after(text, r"Gas and EV charging\s+\$?", default=zero),
        bonus_recurring_bill=_money_after(text, r"Recurring bill\s+\$?", default=zero),
        promotional=_money_after(text, r"Promotional offers\s+\$?", default=zero),
        adjusted=_money_after(text, r"Cashback adjusted\s+\$?", default=zero),
        redeemed=_money_after(text, r"Cashback redeemed\s+\$?", default=zero),
        statement_total=_money_after(text, r"Total Cashback earned this(?:\s+statement)?\s+\$?", default=zero),
        balance=_money_after(text, r"Total Cashback balance\s+\$?", default=zero),
    )


def _parse_details(text: str, header: CreditCardHeader) -> list[CreditCardDetail]:
    card_number = "XXXX XXXX XXXX " + header.card_number_last4
    details: list[CreditCardDetail] = []
    pending: dict | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        desc = re.sub(r"\s+", " ", pending["desc"]).strip()
        fx_ccy = fx_amt = fx_rate = None
        if (fx := _FX_PREFIX.match(desc)) is not None:
            fx_ccy = fx.group(1)
            fx_amt = _parse_money(fx.group(2))
            fx_rate = Decimal(fx.group(3))
            desc = fx.group(4).strip()
        details.append(CreditCardDetail(
            item_num=len(details) + 1,
            card_number=card_number,
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
        if ln.startswith("Subtotal"):
            continue  # multi-card statements have mid-stream subtotals — skip, don't break
        if ln.startswith("Total for"):
            break  # real end-of-transactions marker, after which the legal footer begins

        m = _TRANS_HEAD.match(ln)
        if m:
            flush()
            am = _AMOUNT_TAIL.match(m.group(5))
            if not am:
                raise ValueError(f"no amount in transaction line: {ln!r}")
            amount = _parse_money(am.group(2))
            if am.group(3):
                amount = -amount
            pending = {
                "trans": _year_for(m.group(1), int(m.group(2)), header),
                "post": _year_for(m.group(3), int(m.group(4)), header),
                "desc": am.group(1),
                "amount": amount,
            }
        elif pending is not None:
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
    m = re.search(prefix + rf"({_M}\.?\s+\d{{1,2}},\s+\d{{4}})", text)
    if not m:
        raise ValueError(f"date not found after: {prefix!r}")
    return _parse_long_date(m.group(1))


def _parse_long_date(s: str) -> date:
    m = _LONG_DATE.search(s)
    if not m:
        raise ValueError(f"cannot parse long date: {s!r}")
    return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


def _money_after(text: str, prefix: str, *, default: Decimal | None = None) -> Decimal:
    flags = re.MULTILINE if prefix.startswith("^") else 0
    m = re.search(prefix + r"([+-]?\$?[\d,]+\.\d{2})(\s+CR\b)?", text, flags)
    if not m:
        if default is not None:
            return default
        raise ValueError(f"amount not found after: {prefix!r}")
    amount = _parse_money(m.group(1))
    if m.group(2):
        amount = -amount
    return amount


def _parse_money(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", ""))


# --- legacy (2023-era) layout -----------------------------------------------
# PDF text extraction squishes out spaces, labels differ ("NewBalance" vs
# "Total balance"), and transactions begin on page 1 and continue on page 3
# (page 2 is legal disclosures, page 4 is repayment estimate).

_LEGACY_DATE = re.compile(rf"({_M})\.?\s*(\d{{1,2}}),\s*(\d{{4}})")
_LEGACY_TRANS_HEAD = re.compile(rf"^({_M})\.(\d{{1,2}})\s+({_M})\.(\d{{1,2}})\s+(.+)$")


def _parse_legacy(pages: list[str]) -> tuple[CreditCardHeader, list[CreditCardDetail]]:
    header = _parse_legacy_header(pages[0])
    # Details live on page 1 (after the TRANS/POSTING header row) and on page 3+.
    body = _legacy_details_region(pages[0]) + "\n" + "\n".join(pages[2:])
    details = _parse_legacy_details(body, header)
    return header, details


def _parse_legacy_header(text: str) -> CreditCardHeader:
    period = re.search(
        rf"({_M}\.?\s*\d{{1,2}},\s*\d{{4}})\s*-\s*({_M}\.?\s*\d{{1,2}},\s*\d{{4}})",
        text,
    )
    if not period:
        raise ValueError("statement period not found")

    card_full = re.search(r"CardNumber\s+(\d{16})", text)
    last4 = card_full.group(1)[-4:] if card_full else ""
    holder = re.search(r"CustomerName\s+([A-Z]+)", text)
    ir_pur = re.search(r"Purchases\s+[\d.]+\s+([\d.]+)\s+[\d.]+", text)
    ir_cash = re.search(r"CashAdvances\d?\s+[\d.]+\s+([\d.]+)\s+[\d.]+", text)

    return CreditCardHeader(
        bank=BANK,
        product=PRODUCT,
        account_holder=holder.group(1) if holder else "",
        card_number_last4=last4,
        statement_date=_legacy_find_date(text, r"StatementDate\s+"),
        period_start=_legacy_parse_date(period.group(1)),
        period_end=_legacy_parse_date(period.group(2)),
        payment_due_date=_legacy_find_date(text, r"PaymentDueDate\s+"),
        previous_balance=_legacy_money(text, r"PreviousBalance,[^$\n]*\$"),
        payments_and_credits=_legacy_money(text, r"PaymentsandCredits\s+"),
        purchases_and_other_charges=_legacy_money(text, r"Purchasesandothercharges\s+\+?"),
        new_installments=Decimal("0"),
        cash_advances=_legacy_money(text, r"CashAdvances\d?\s+"),
        total_interest_charges=_legacy_money(text, r"TotalInterestCharges\s+"),
        fees=_legacy_money(text, r"^Fees\s+", default=Decimal("0")),
        total_balance=_legacy_money(text, r"NewBalance,[^$\n]*\$"),
        minimum_payment_due=_legacy_money(text, r"MinimumPaymentDue\s+\$?"),
        credit_limit=_legacy_money(text, r"YourCreditLimit\s+\$?"),
        available_credit=_legacy_money(text, r"YourAvailableCredit\s+\$?"),
        interest_rate_purchases=Decimal(ir_pur.group(1)) if ir_pur else None,
        interest_rate_cash_advances=Decimal(ir_cash.group(1)) if ir_cash else None,
        rewards=_parse_legacy_rewards(text),
    )


def _parse_legacy_rewards(text: str) -> CashbackRewards:
    zero = Decimal("0")
    return CashbackRewards(
        base_earned=_legacy_money(text, r"(?<!Bonus)Rewardsearned\s+\$?", default=zero),
        bonus_earned=_legacy_money(text, r"Bonusrewardsearned\s+\$?", default=zero),
        bonus_groceries=zero,
        bonus_ground_travel=zero,
        bonus_gas_ev_charging=zero,
        bonus_recurring_bill=zero,
        promotional=zero,
        adjusted=_legacy_money(text, r"Rewardsadjusted\s+\$?", default=zero),
        redeemed=_legacy_money(text, r"RewardsRedeemed\s+-?\$?", default=zero),
        statement_total=_legacy_money(text, r"Totalrewardsearned\s+\$?", default=zero),
        balance=_legacy_money(text, r"Rewardsbalanceyeartodate\s+\$?", default=zero),
    )


def _legacy_details_region(page1: str) -> str:
    """Return the transaction region of page 1 (between the TRANS/POSTING header
    and the `Continuedonpage` or page-footer marker)."""
    lines = page1.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("CardNumber:"))
    except StopIteration:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("Continuedonpage") or lines[i].startswith("BMOSecurityAlerts"):
            end = i
            break
    return "\n".join(lines[start + 1:end])


_LEGACY_AMOUNT_TAIL = re.compile(r"^(.*?)\s+([\d,]+\.\d{2})(CR)?\s*$")


def _parse_legacy_details(text: str, header: CreditCardHeader) -> list[CreditCardDetail]:
    card_number = "XXXX XXXX XXXX " + header.card_number_last4
    details: list[CreditCardDetail] = []

    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = _LEGACY_TRANS_HEAD.match(ln)
        if not m:
            continue
        am = _LEGACY_AMOUNT_TAIL.match(m.group(5))
        if not am:
            continue
        amount = _parse_money(am.group(2))
        if am.group(3):
            amount = -amount
        # Description + reference number are packed in am.group(1); the ref is
        # the trailing all-digit token when present.
        desc = am.group(1).rstrip()
        ref_m = re.match(r"^(.*?)\s+(\d{9,})\s*$", desc)
        if ref_m:
            desc = ref_m.group(1).strip()
        desc = re.sub(r"\s+", " ", desc).strip()
        details.append(CreditCardDetail(
            item_num=len(details) + 1,
            card_number=card_number,
            transaction_date=_year_for(m.group(1), int(m.group(2)), header),
            posting_date=_year_for(m.group(3), int(m.group(4)), header),
            amount=amount,
            description=desc,
        ))
    return details


def _legacy_find_date(text: str, prefix: str) -> date:
    m = re.search(prefix + rf"({_M}\.?\s*\d{{1,2}},\s*\d{{4}})", text)
    if not m:
        raise ValueError(f"date not found after: {prefix!r}")
    return _legacy_parse_date(m.group(1))


def _legacy_parse_date(s: str) -> date:
    m = _LEGACY_DATE.search(s)
    if not m:
        raise ValueError(f"cannot parse legacy date: {s!r}")
    return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


def _legacy_money(text: str, prefix: str, *, default: Decimal | None = None) -> Decimal:
    flags = re.MULTILINE if prefix.startswith("^") else 0
    m = re.search(prefix + r"([+-]?\$?[\d,]+\.\d{2})", text, flags)
    if not m:
        if default is not None:
            return default
        raise ValueError(f"amount not found after: {prefix!r}")
    return _parse_money(m.group(1))
