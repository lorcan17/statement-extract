"""CLI entry point for bank-pdf-extract."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from . import archive, csv_ingest
from .parsers import amex_cobalt, bmo_credit_card, bmo_deposit_account, coast_capital_chequing, coast_capital_credit, eq_bank

app = typer.Typer(help="Convert bank statement PDFs to structured data.")

_PARSERS = {
    "amex_cobalt": amex_cobalt,
    "bmo_credit_card": bmo_credit_card,
    "bmo_chequing": bmo_deposit_account,
    "bmo_savings": bmo_deposit_account,
    "coast_visa": coast_capital_credit,
    "coast_chequing": coast_capital_chequing,
    "eq_bank": eq_bank,
}


@app.command()
def extract(
    pdf: Path = typer.Argument(..., exists=True, readable=True),
    bank: str = typer.Option(
        "bmo_credit_card", help=f"Bank/product key. One of: {', '.join(_PARSERS)}."
    ),
    validate_against: Path | None = typer.Option(
        None, help="Optional reference CSV for external validation."
    ),
    output: Path | None = typer.Option(
        None, help="Write JSON output here. Prints to stdout if omitted."
    ),
) -> None:
    """Parse a PDF statement and emit structured JSON."""
    if bank not in _PARSERS:
        typer.echo(f"unknown bank: {bank}", err=True)
        raise typer.Exit(2)

    parser = _PARSERS[bank]
    res = parser.parse(pdf)

    if isinstance(res, tuple):
        header, details = res
        issues = parser.validate_internal(header, details)
        if validate_against is not None:
            issues += parser.validate_against_csv(details, validate_against)
        data = {
            "header": header.model_dump(mode="json"),
            "details": [d.model_dump(mode="json") for d in details],
        }
    else:
        # MultiAccountDepositStatement
        issues = parser.validate_internal(res)
        if validate_against is not None:
            issues += parser.validate_against_csv(res, validate_against)
        data = res.model_dump(mode="json")

    if issues:
        typer.echo("validation failures:", err=True)
        for i in issues:
            typer.echo(f"  - {i}", err=True)
        raise typer.Exit(1)

    payload = json.dumps(data, indent=2, default=str)
    if output is not None:
        output.write_text(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")


@app.command()
def reorg(
    archive_root: Path = typer.Argument(..., exists=True, file_okay=False),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned moves without touching disk."),
) -> None:
    """Reorganise a flat archive into <owner>/<bank_product>/<last4>/<file>.pdf."""
    plan = archive.plan_reorg(archive_root)

    for src, dst in plan.rename_only:
        typer.echo(f"rename folder: {src.relative_to(archive_root)} -> {dst.relative_to(archive_root)}")
    for src, dst in plan.moves:
        typer.echo(f"move: {src.relative_to(archive_root)} -> {dst.relative_to(archive_root)}")
    for path, reason in plan.skipped:
        typer.echo(f"skip: {path.relative_to(archive_root)} ({reason})", err=True)

    typer.echo(f"\n{plan.summary()}")
    if dry_run or not plan.moves and not plan.rename_only:
        return

    archive.apply_plan(plan)
    typer.echo("applied.")


@app.command()
def ingest_csv(
    folder: Path = typer.Argument(..., exists=True, file_okay=False),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned changes without touching disk."),
) -> None:
    """Rename downloaded CSV exports to `activity_<start>_to_<end>.csv` and drop duplicates."""
    plan = csv_ingest.plan_ingest(folder)

    for src, dst in plan.renames:
        typer.echo(f"rename: {src.name} -> {dst.name}")
    for dup, survivor in plan.duplicates:
        typer.echo(f"duplicate (delete): {dup.name} (same content as -> {survivor.name})")
    for src, dst in plan.conflicts:
        typer.echo(f"conflict: {src.name} and another file both map to {dst.name} but differ", err=True)
    for path, reason in plan.skipped:
        typer.echo(f"skip: {path.name} ({reason})", err=True)

    typer.echo(f"\n{plan.summary()}")
    if dry_run or (not plan.renames and not plan.duplicates):
        return

    csv_ingest.apply_plan(plan)
    typer.echo("applied.")


if __name__ == "__main__":
    app()
