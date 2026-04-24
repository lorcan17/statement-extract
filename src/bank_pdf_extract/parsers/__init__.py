"""One module per bank/product. Each exposes:

    BANK: str
    PRODUCT: str
    parse(pdf_path: Path) -> tuple[StatementHeader, list[StatementDetail]]
"""
