# Known issues

Data-quality issues to revisit (not blocking smoke test):

1. **EQ Bank parser — duplicate rows.** Some EQ Bank statements emit single transactions as two identical rows in parser output. Investigate `src/bank_pdf_extract/parsers/eq_bank.py`.
2. **Archive dedupe.** Some statements exist twice in the source archive (e.g. `<name>.pdf` and `<name> (1).pdf`). The reorg command should grow a dedupe pass.
3. **Transfer detection.** Monthly spend totals are inflated by inter-account savings transfers. Transfer-matching is a downstream concern (see finance-lake SPEC) but worth flagging here so the parser output isn't blamed.
