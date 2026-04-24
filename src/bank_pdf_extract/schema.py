from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


# --- BMO credit card ---------------------------------------------------------

class CashbackRewards(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_earned: Decimal
    bonus_earned: Decimal
    bonus_groceries: Decimal
    bonus_ground_travel: Decimal
    bonus_gas_ev_charging: Decimal
    bonus_recurring_bill: Decimal
    promotional: Decimal
    adjusted: Decimal
    redeemed: Decimal
    statement_total: Decimal
    balance: Decimal


class MembershipRewards(BaseModel):
    model_config = ConfigDict(frozen=True)

    previous_balance: int
    earned: int
    bonus: int
    adjusted: int
    redeemed: int
    new_balance: int


class CollabriaRewards(BaseModel):
    model_config = ConfigDict(frozen=True)

    previous_balance: int
    earned: int
    adjusted: int
    redeemed: int
    new_balance: int


class CreditCardHeader(BaseModel):
    model_config = ConfigDict(frozen=True)

    bank: str
    product: str
    account_holder: str
    card_number_last4: str  # Can be multiple last-4 digits separated by comma for multi-card statements

    statement_date: date
    period_start: date
    period_end: date
    payment_due_date: date

    previous_balance: Decimal
    payments_and_credits: Decimal
    purchases_and_other_charges: Decimal
    new_installments: Decimal
    cash_advances: Decimal
    total_interest_charges: Decimal
    fees: Decimal
    total_balance: Decimal
    minimum_payment_due: Decimal

    credit_limit: Decimal
    available_credit: Decimal

    interest_rate_purchases: Decimal | None = None
    interest_rate_cash_advances: Decimal | None = None

    rewards: CashbackRewards | MembershipRewards | CollabriaRewards | None = None


class CreditCardDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_num: int
    card_number: str
    transaction_date: date
    posting_date: date
    amount: Decimal
    description: str

    # Populated for foreign-currency transactions where the PDF annotates the
    # original amount and exchange rate; None for CAD-denominated charges.
    original_currency: str | None = None
    original_amount: Decimal | None = None
    exchange_rate: Decimal | None = None


class CreditCardStatement(BaseModel):
    header: CreditCardHeader
    details: list[CreditCardDetail]


# --- BMO deposit accounts (Everyday Banking: chequing + savings) ------------

class DepositAccountHeader(BaseModel):
    model_config = ConfigDict(frozen=True)

    bank: str
    product: str
    account_type: str  # e.g. "primary_chequing", "savings_builder", "savings_amplifier"
    account_holder: str
    account_number: str
    branch_name: str
    transit_number: str
    plan_name: str

    period_start: date
    period_end: date

    opening_balance: Decimal
    total_deducted: Decimal
    total_added: Decimal
    closing_balance: Decimal


class DepositAccountDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_num: int
    account_number: str
    posting_date: date
    description: str
    amount: Decimal  # signed: positive = credit, negative = debit
    running_balance: Decimal


class DepositAccountStatement(BaseModel):
    header: DepositAccountHeader
    details: list[DepositAccountDetail]


class MultiAccountDepositStatement(BaseModel):
    # For banks like Coast Capital that put multiple accounts in one PDF
    bank: str
    period_start: date
    period_end: date
    accounts: list[DepositAccountStatement]
