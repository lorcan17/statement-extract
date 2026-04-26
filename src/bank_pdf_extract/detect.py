"""Content-based parser auto-detection.

Public surface for callers (e.g. Paperless ingestion) that have a PDF but no
folder context to pick a parser by. Tries each parser in turn; first whose
`parse()` succeeds wins. Also exposes `derive_metadata` — the single source
of truth for turning a parsed header into the `(holder, bank_product, last4)`
tuple used by archive layout and downstream metadata writeback.

Owner-key mapping (holder string -> archive owner key, e.g. joint-account
detection) is intentionally NOT done here — it's a deployment concern, lives
in the caller.
"""
from __future__ import annotations

from pathlib import Path
from types import ModuleType

from .parsers import (
    amex_cobalt,
    bmo_credit_card,
    bmo_deposit_account,
    coast_capital_chequing,
    coast_capital_credit,
    eq_bank,
)
from .schema import (
    CreditCardHeader,
    DepositAccountHeader,
    MultiAccountDepositStatement,
)

PARSERS: tuple[ModuleType, ...] = (
    amex_cobalt,
    bmo_credit_card,
    bmo_deposit_account,
    coast_capital_credit,
    coast_capital_chequing,
    eq_bank,
)


def detect_parser(pdf: Path) -> ModuleType | None:
    """Return the first parser whose `parse()` succeeds on `pdf`, else None.

    None means the PDF is not a finance statement we recognise — callers
    should treat this as a no-op (skip), not an error.
    """
    for parser in PARSERS:
        try:
            parser.parse(pdf)
        except Exception:
            continue
        return parser
    return None


def derive_metadata(
    header: CreditCardHeader | DepositAccountHeader | MultiAccountDepositStatement,
) -> tuple[str, str, str]:
    """Return (holder, bank_product, last4) for archive layout + writeback.

    - holder: raw `account_holder` string from the header. Owner-key mapping
      (single vs joint, etc.) is the caller's job.
    - bank_product: `f"{bank}_{product}"` — matches archive folder naming.
    - last4: last 4 digits of the account/card identifier. For multi-account
      deposit statements, returns the first account's last4.
    """
    if isinstance(header, MultiAccountDepositStatement):
        first = header.accounts[0]
        return (
            first.header.account_holder,
            f"{first.header.bank}_{first.header.product}",
            _last4(first.header.account_number),
        )
    if isinstance(header, CreditCardHeader):
        return (
            header.account_holder,
            f"{header.bank}_{header.product}",
            _last4(header.card_number_last4),
        )
    return (
        header.account_holder,
        f"{header.bank}_{header.product}",
        _last4(header.account_number),
    )


def _last4(account_id: str) -> str:
    digits = "".join(ch for ch in account_id if ch.isdigit())
    if len(digits) < 4:
        raise ValueError("not enough digits to form last4")
    return digits[-4:]
