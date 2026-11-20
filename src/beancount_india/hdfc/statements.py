"""Importer for HDFC Bank account statements in Delimited format."""

import csv
import datetime
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import beangulp
from beancount.core import amount, data, flags

from beancount_india.hdfc._common import _CURRENCY, _FilenameMatch, _SuffixAccounts

_FILENAME_PATTERN = re.compile(
    r"Acct_Statement_XXXXXXXX(?P<suffix>\d{4})_"
    r"(?P<download_date>\d{8})\.txt"
)
_HEADER = (
    "Date",
    "Narration",
    "Value Dat",
    "Debit Amount",
    "Credit Amount",
    "Chq/Ref Number",
    "Closing Balance",
)


class HDFCAccountImporter(beangulp.Importer):
    """Imports HDFC Bank account statements in Delimited format."""

    def __init__(self, accounts: Mapping[str, str]) -> None:
        """Initialize the importer.

        Args:
            accounts: Full HDFC account numbers mapped to Beancount accounts.

        Raises:
            ValueError: If an account number is invalid or suffixes are ambiguous.
        """
        self._accounts = _SuffixAccounts(accounts, "account number")

    def identify(self, filepath: str) -> bool:
        filename_match = _match_filename(filepath)
        return filename_match is not None and filename_match.suffix in self._accounts

    def account(self, filepath: str) -> str:
        filename_match = _match_filename(filepath)
        if filename_match is None:
            raise ValueError(f"Not an HDFC account statement: {filepath!r}")

        return self._accounts.account(filename_match.suffix)

    def date(self, filepath: str) -> datetime.date | None:
        filename_match = _match_filename(filepath)
        return filename_match.download_date if filename_match is not None else None

    def filename(self, filepath: str) -> str | None:
        rows = _parse_statement(Path(filepath))
        if not rows:
            return None

        first_date = min(row.value_date for row in rows)
        last_date = max(row.value_date for row in rows)
        if first_date == last_date:
            return f"statement.{first_date}.txt"
        return f"statement.{first_date}_{last_date}.txt"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        account = self.account(filepath)
        rows = _parse_statement(Path(filepath))
        if not rows:
            return []

        filename = Path(filepath).name
        entries: data.Entries = []
        opening_balance = _make_opening_balance(rows[0], account, filename, existing)
        if opening_balance is not None:
            entries.append(opening_balance)

        entries.extend(_make_transaction(row, account, filename) for row in rows)

        return entries


@dataclass(frozen=True, slots=True)
class _StatementRow:
    line_number: int
    transaction_date: datetime.date
    narration: str
    value_date: datetime.date
    debit: Decimal
    credit: Decimal
    reference_number: str
    closing_balance: Decimal

    @property
    def amount(self) -> Decimal:
        return self.credit - self.debit


def _parse_statement(path: Path) -> list[_StatementRow]:
    rows: list[_StatementRow] = []

    # newline="" lets csv.reader handle the file's line endings itself.
    with path.open(encoding="utf-8", newline="") as statement:
        rows_with_lines = _nonempty_rows(csv.reader(statement))
        try:
            header_line, header = next(rows_with_lines)
        except StopIteration:
            raise ValueError(f"{path.name}: statement has no header")
        if tuple(cell.strip() for cell in header) != _HEADER:
            raise ValueError(f"{path.name}:{header_line}: unexpected header")

        for line_number, fields in rows_with_lines:
            if len(fields) < len(_HEADER):
                raise ValueError(
                    f"{path.name}:{line_number}: expected at least {len(_HEADER)} fields"
                )

            try:
                (
                    transaction_date,
                    *narration,
                    value_date,
                    debit,
                    credit,
                    reference_number,
                    closing_balance,
                ) = fields
                parsed_debit = _parse_decimal(debit)
                parsed_credit = _parse_decimal(credit)
                if parsed_debit and parsed_credit:
                    raise ValueError("both debit and credit are nonzero")

                rows.append(
                    _StatementRow(
                        line_number=line_number,
                        transaction_date=_parse_date(transaction_date),
                        narration=",".join(narration).strip(),
                        value_date=_parse_date(value_date),
                        debit=parsed_debit,
                        credit=parsed_credit,
                        reference_number=reference_number.strip(),
                        closing_balance=_parse_decimal(closing_balance),
                    )
                )
            except (InvalidOperation, ValueError) as error:
                raise ValueError(
                    f"{path.name}:{line_number}: invalid statement row"
                ) from error

    return rows


def _nonempty_rows(rows: Iterable[list[str]]) -> Iterator[tuple[int, list[str]]]:
    for line_number, row in enumerate(rows, start=1):
        if any(field.strip() for field in row):
            yield line_number, row


def _make_opening_balance(
    row: _StatementRow,
    account: str,
    filepath: str,
    existing: data.Entries,
) -> data.Balance | None:
    # The reported closing balance already includes the first transaction.
    opening_amount = amount.Amount(row.closing_balance - row.amount, _CURRENCY)

    suppress_balance = False
    for entry in existing:
        if (
            isinstance(entry, data.Balance)
            and entry.date == row.value_date
            and entry.account == account
            and entry.amount.currency == _CURRENCY
        ):
            if entry.amount != opening_amount:
                raise ValueError(
                    f"Existing balance for {account!r} on {row.value_date} "
                    "conflicts with statement"
                )
            suppress_balance = True
        elif (
            isinstance(entry, data.Transaction)
            and entry.date == row.value_date
            and any(posting.account == account for posting in entry.postings)
        ):
            # Beancount cannot assert a balance between same-day transactions.
            suppress_balance = True

    if suppress_balance:
        return None

    return data.Balance(
        data.new_metadata(filepath, row.line_number),
        row.value_date,
        account,
        opening_amount,
        None,
        None,
    )


def _make_transaction(
    row: _StatementRow, account: str, filepath: str
) -> data.Transaction:
    metadata = data.new_metadata(filepath, row.line_number)
    if row.reference_number:
        metadata["reference"] = row.reference_number
    posting = data.Posting(
        account,
        amount.Amount(row.amount, _CURRENCY),
        None,
        None,
        None,
        None,
    )

    return data.Transaction(
        metadata,
        row.value_date,
        flags.FLAG_OKAY,
        None,
        row.narration,
        data.EMPTY_SET,
        data.EMPTY_SET,
        [posting],
    )


def _match_filename(filepath: str) -> _FilenameMatch | None:
    """Parse metadata from an original HDFC statement filename."""
    regex_match = _FILENAME_PATTERN.fullmatch(Path(filepath).name)
    if regex_match is None:
        return None

    try:
        download_date = datetime.datetime.strptime(
            regex_match.group("download_date"), "%d%m%Y"
        ).date()
    except ValueError:
        return None

    return _FilenameMatch(regex_match.group("suffix"), download_date)


def _parse_date(value: str) -> datetime.date:
    return datetime.datetime.strptime(value.strip(), "%d/%m/%y").date()


def _parse_decimal(value: str) -> Decimal:
    return Decimal(value.strip() or "0")
