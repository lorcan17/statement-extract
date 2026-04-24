"""Archive layout manager.

Turns a flat, hyphen-named archive (as downloaded from each bank) into the
canonical `<owner>/<bank_product>/<last4>/<file>.pdf` layout. Re-runnable /
idempotent — already-reorganised files are left in place.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .parsers import amex_cobalt, bmo_credit_card, bmo_deposit_account, coast_capital_chequing, coast_capital_credit, eq_bank
from .schema import MultiAccountDepositStatement

# Source-folder name (as it arrives from the bank's download UI) → target
# product folder name (underscore, matches parser key) + parser module.
# Multiple source folders can map to the same target when one parser covers
# multiple products (e.g. BMO chequing + savings → bmo_chequing).
_ALIASES: dict[str, tuple[str, ModuleType | None]] = {
    "bmo-chequing":    ("bmo_deposit_account", bmo_deposit_account),
    "bmo_chequing":    ("bmo_deposit_account", bmo_deposit_account),
    "bmo-savings":     ("bmo_deposit_account", bmo_deposit_account),
    "bmo_savings":     ("bmo_deposit_account", bmo_deposit_account),
    "bmo_deposit_account": ("bmo_deposit_account", bmo_deposit_account),
    "bmo-credit":      ("bmo_credit_card", bmo_credit_card),
    "bmo-credit-card": ("bmo_credit_card", bmo_credit_card),
    "bmo_credit_card": ("bmo_credit_card", bmo_credit_card),
    "bmo-mastercard":  ("bmo_credit_card", bmo_credit_card),
    "bmo_mastercard":  ("bmo_credit_card", bmo_credit_card),
    "eq-bank-savings": ("eq_bank_savings", eq_bank),
    "eq_bank_savings": ("eq_bank_savings", eq_bank),
    "amex":            ("amex",            amex_cobalt),
    "amex-cobalt":     ("amex",            amex_cobalt),
    "coast_capital_chequing": ("coast_capital_chequing", coast_capital_chequing),
    "coast-capital-chequing": ("coast_capital_chequing", coast_capital_chequing),
    "coast_capital_credit":   ("coast_capital_credit",   coast_capital_credit),
    "coast-capital-credit":   ("coast_capital_credit",   coast_capital_credit),
    "coast-captial-credit":   ("coast_capital_credit",   coast_capital_credit),
    "coast_captial_credit":   ("coast_capital_credit",   coast_capital_credit),
}


@dataclass
class Plan:
    moves: list[tuple[Path, Path]]        # (src, dst)
    skipped: list[tuple[Path, str]]       # (path, reason)
    rename_only: list[tuple[Path, Path]]  # folder renames without file moves

    def summary(self) -> str:
        return (
            f"{len(self.moves)} file move(s), "
            f"{len(self.rename_only)} folder rename(s), "
            f"{len(self.skipped)} skipped"
        )


def plan_reorg(archive_root: Path) -> Plan:
    """Compute all moves without touching disk."""
    moves: list[tuple[Path, Path]] = []
    skipped: list[tuple[Path, str]] = []
    rename_only: list[tuple[Path, Path]] = []

    for owner_dir in sorted(p for p in archive_root.iterdir() if p.is_dir()):
        for product_dir in sorted(p for p in owner_dir.iterdir() if p.is_dir()):
            alias = _ALIASES.get(product_dir.name)
            if alias is None:
                skipped.append((product_dir, f"unknown product folder {product_dir.name!r}"))
                continue
            target_name, parser = alias
            target_dir = owner_dir / target_name

            # Folder-only rename when (a) no parser yet or (b) source is already the
            # canonical name — collect loose PDFs if any, but partition requires a parser.
            if parser is None:
                if product_dir.name != target_name:
                    rename_only.append((product_dir, target_dir))
                continue

            # Source-side dedup: same product folder occasionally contains
            # `Foo.pdf` and `Foo (1).pdf` after re-downloading. Hash and keep
            # the lexicographically-first path per content; skip the rest.
            seen_hashes: dict[str, Path] = {}
            for pdf in sorted(product_dir.glob("*.pdf")):
                digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
                if digest in seen_hashes:
                    skipped.append((pdf, f"duplicate of {seen_hashes[digest].name}"))
                    continue
                seen_hashes[digest] = pdf

            for pdf in sorted(seen_hashes.values()):
                try:
                    res = parser.parse(pdf)
                    if isinstance(res, MultiAccountDepositStatement):
                        # One PDF covers multiple accounts — keep flat under target_dir.
                        dst = target_dir / pdf.name
                    else:
                        header, _ = res
                        last4 = _last4(header.account_number if hasattr(header, "account_number")
                                        else header.card_number_last4)
                        dst = target_dir / last4 / pdf.name
                except Exception as e:
                    skipped.append((pdf, f"parse failed: {e}"))
                    continue
                if pdf.resolve() == dst.resolve():
                    continue  # already in place
                moves.append((pdf, dst))

    return Plan(moves=moves, skipped=skipped, rename_only=rename_only)


def apply_plan(plan: Plan) -> None:
    """Execute a plan. Creates target dirs, moves files, removes emptied sources."""
    for src, dst in plan.moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if _same_file(src, dst):
                src.unlink()
                continue
            raise FileExistsError(f"destination exists with different content: {dst}")
        shutil.move(str(src), str(dst))

    for src_dir, dst_dir in plan.rename_only:
        if dst_dir.exists():
            # merge: move children in, remove empty src
            for child in list(src_dir.iterdir()):
                shutil.move(str(child), str(dst_dir / child.name))
            src_dir.rmdir()
        else:
            src_dir.rename(dst_dir)

    # Remove any now-empty source product directories.
    for src, _ in plan.moves:
        parent = src.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


def _last4(account_id: str) -> str:
    digits = "".join(ch for ch in account_id if ch.isdigit())
    if len(digits) < 4:
        raise ValueError(f"not enough digits to form last4: {account_id!r}")
    return digits[-4:]


def _same_file(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()
