"""Parser for HDFC Bank credit card statements in CSV format."""

import datetime
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import beangulp
from beancount.core import amount, data, flags

from beancount_india.hdfc._common import _CURRENCY, _FilenameMatch, _SuffixAccounts

# Despite the .csv extension, HDFC uses this compound delimiter.
_DELIMITER = "~|~"
_FILENAME_PATTERN = re.compile(
    r"[A-Z][a-z]{2}\d{4}_Billedstatements_(?P<suffix>\d{4})_"
    r"(?P<download_date>\d{2}-\d{2}-\d{2})_\d{2}-\d{2}\.csv"
)
_TRANSACTION_HEADER = (
    "Transaction type",
    "Primary / Addon Customer Name",
    "DATE",
    "Description",
    "AMT",
    "Debit /Credit",
    "REWARDS",
    "",
)
_ACCOUNT_SUMMARY_HEADER = (
    "Opening Bal",
    "-",
    "Payment / Credit",
    "+",
    "Purchases / Debits",
    "+",
    "Finance Charges",
    "=",
    "Total Dues",
)
_REWARD_POINTS_SUMMARY_HEADER = (
    "Opening Balance",
    "Feature + Bonus Reward Points Earned",
    "Disbursed",
    "Adjusted/Lapsed",
    "Closing Balance",
    "Points expiring in next 30 days",
    "Points expiring in next 60 days",
)
_CARD_NUMBER_PATTERN = re.compile(r"Card No:.*(?P<card_suffix>\d{4})$")


class HDFCCardImporter(beangulp.Importer):
    """Imports HDFC Bank credit card statements in CSV format."""

    def __init__(self, accounts: Mapping[str, str]) -> None:
        """Initialize the importer with card numbers mapped to accounts."""
        self._accounts = _SuffixAccounts(accounts, "card number")

    def identify(self, filepath: str) -> bool:
        filename_match = _match_filename(filepath)
        return filename_match is not None and filename_match.suffix in self._accounts

    def account(self, filepath: str) -> str:
        filename_match = _match_filename(filepath)
        if filename_match is None:
            raise ValueError(f"Not an HDFC card statement: {filepath!r}")

        return self._accounts.account(filename_match.suffix)

    def date(self, filepath: str) -> datetime.date | None:
        if _match_filename(filepath) is None:
            return None
        return _parse_card_statement(Path(filepath)).statement_date

    def filename(self, filepath: str) -> str:
        statement = _parse_card_statement(Path(filepath))
        return f"statement.{statement.statement_date}.csv"

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        filename_match = _match_filename(filepath)
        if filename_match is None:
            raise ValueError(f"Not an HDFC card statement: {filepath!r}")

        account = self.account(filepath)
        statement = _parse_card_statement(Path(filepath))
        if statement.card_suffix != filename_match.suffix:
            raise ValueError(
                f"Card suffix {statement.card_suffix!r} does not match filename"
            )

        filename = Path(filepath).name
        return [
            _make_transaction(transaction, account, filename)
            for transaction in statement.transactions
        ]


@dataclass(frozen=True, slots=True)
class _CardTransaction:
    line_number: int
    transaction_type: str
    cardholder: str
    timestamp: datetime.datetime
    description: str
    amount: Decimal
    credit: bool
    reward_points: Decimal

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.credit else -self.amount


@dataclass(frozen=True, slots=True)
class _AccountSummary:
    opening_balance: Decimal
    payments_and_credits: Decimal
    purchases_and_debits: Decimal
    finance_charges: Decimal
    total_dues: Decimal


@dataclass(frozen=True, slots=True)
class _RewardPointsSummary:
    opening_balance: int
    earned: int
    disbursed: int
    adjusted_or_lapsed: int
    closing_balance: int
    expiring_within_30_days: int
    expiring_within_60_days: int


@dataclass(frozen=True, slots=True)
class _CardStatement:
    card_suffix: str
    statement_date: datetime.date
    payment_due_date: datetime.date
    total_amount_due: Decimal
    minimum_amount_due: Decimal
    account_summary: _AccountSummary
    reward_points_summary: _RewardPointsSummary
    transactions: list[_CardTransaction]


def _parse_card_statement(path: Path) -> _CardStatement:
    rows = [_split_row(line) for line in path.read_text(encoding="utf-8").splitlines()]
    metadata: dict[str, str] = {}
    card_suffix: str | None = None

    try:
        transaction_header_index = rows.index(_TRANSACTION_HEADER)
    except ValueError:
        raise ValueError(f"{path.name}: transaction header not found") from None

    for fields in rows[:transaction_header_index]:
        if len(fields) == 2:
            metadata[fields[0]] = fields[1]

        regex_match = _CARD_NUMBER_PATTERN.fullmatch(fields[0])
        if regex_match is not None:
            card_suffix = regex_match.group("card_suffix")

    if card_suffix is None:
        raise ValueError(f"{path.name}: card number not found")

    try:
        statement_date = _parse_date(metadata["Statement Date"])
        payment_due_date = _parse_date(metadata["Payment Due Date"])
        total_amount_due = _parse_decimal(metadata["Total Amount Due"])
        minimum_amount_due = _parse_decimal(metadata["Minimum Amount Due"])
    except KeyError as error:
        raise ValueError(f"{path.name}: missing {error.args[0]!r}") from None
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{path.name}: invalid statement metadata") from error

    account_summary = _parse_account_summary(path, rows)
    reward_points_summary = _parse_reward_points_summary(path, rows)

    transactions: list[_CardTransaction] = []
    for index, fields in enumerate(
        rows[transaction_header_index + 1 :], start=transaction_header_index + 2
    ):
        if not any(fields):
            break

        if len(fields) != len(_TRANSACTION_HEADER):
            raise ValueError(f"{path.name}:{index}: invalid transaction row")

        (
            transaction_type,
            cardholder,
            timestamp_text,
            description,
            amount_text,
            debit_or_credit,
            reward_points_text,
            trailing_field,
        ) = fields
        if trailing_field or debit_or_credit not in ("", "Cr"):
            raise ValueError(f"{path.name}:{index}: invalid transaction row")

        try:
            timestamp = datetime.datetime.strptime(timestamp_text, "%d/%m/%Y %H:%M:%S")
            amount = _parse_decimal(amount_text)
            credit = debit_or_credit == "Cr"
            reward_points = _parse_decimal(reward_points_text or "0")
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"{path.name}:{index}: invalid transaction row") from error

        transaction = _CardTransaction(
            line_number=index,
            transaction_type=transaction_type,
            cardholder=cardholder,
            timestamp=timestamp,
            description=description,
            amount=amount,
            credit=credit,
            reward_points=reward_points,
        )
        transactions.append(transaction)

    # HDFC groups transactions by type, so file order may not be chronological.
    transactions.sort(key=lambda transaction: transaction.timestamp)
    return _CardStatement(
        card_suffix=card_suffix,
        statement_date=statement_date,
        payment_due_date=payment_due_date,
        total_amount_due=total_amount_due,
        minimum_amount_due=minimum_amount_due,
        account_summary=account_summary,
        reward_points_summary=reward_points_summary,
        transactions=transactions,
    )


def _parse_account_summary(path: Path, rows: list[tuple[str, ...]]) -> _AccountSummary:
    line_number, fields = _find_table_row(path, rows, _ACCOUNT_SUMMARY_HEADER)

    try:
        # The intervening fields are arithmetic symbols from the summary table.
        return _AccountSummary(
            opening_balance=_parse_decimal(fields[0]),
            payments_and_credits=_parse_decimal(fields[2]),
            purchases_and_debits=_parse_decimal(fields[4]),
            finance_charges=_parse_decimal(fields[6]),
            total_dues=_parse_decimal(fields[8]),
        )
    except InvalidOperation as error:
        raise ValueError(
            f"{path.name}:{line_number}: invalid account summary"
        ) from error


def _parse_reward_points_summary(
    path: Path, rows: list[tuple[str, ...]]
) -> _RewardPointsSummary:
    line_number, fields = _find_table_row(path, rows, _REWARD_POINTS_SUMMARY_HEADER)

    try:
        points = [int(field.replace(",", "")) for field in fields]
    except ValueError as error:
        raise ValueError(
            f"{path.name}:{line_number}: invalid reward points summary"
        ) from error

    return _RewardPointsSummary(*points)


def _find_table_row(
    path: Path, rows: list[tuple[str, ...]], header: tuple[str, ...]
) -> tuple[int, tuple[str, ...]]:
    """Return the one-based line number and fields immediately after a header."""
    try:
        header_index = rows.index(header)
        row = rows[header_index + 1]
    except (IndexError, ValueError):
        raise ValueError(f"{path.name}: table not found: {header[0]!r}") from None

    line_number = header_index + 2
    if len(row) != len(header):
        raise ValueError(
            f"{path.name}:{line_number}: expected {len(header)} table fields"
        )

    return line_number, row


def _split_row(line: str) -> tuple[str, ...]:
    """Split a statement row and remove HDFC's field padding."""
    return tuple(field.strip() for field in line.split(_DELIMITER))


def _make_transaction(
    transaction: _CardTransaction, account: str, filename: str
) -> data.Transaction:
    metadata = data.new_metadata(filename, transaction.line_number)
    if transaction.reward_points:
        metadata["reward_points"] = transaction.reward_points

    posting = data.Posting(
        account,
        amount.Amount(transaction.signed_amount, _CURRENCY),
        None,
        None,
        None,
        None,
    )
    return data.Transaction(
        metadata,
        transaction.timestamp.date(),
        flags.FLAG_OKAY,
        None,
        transaction.description,
        data.EMPTY_SET,
        data.EMPTY_SET,
        [posting],
    )


def _match_filename(filepath: str) -> _FilenameMatch | None:
    """Parse metadata from an original HDFC card statement filename."""
    regex_match = _FILENAME_PATTERN.fullmatch(Path(filepath).name)
    if regex_match is None:
        return None

    try:
        download_date = datetime.datetime.strptime(
            regex_match.group("download_date"), "%d-%m-%y"
        ).date()
    except ValueError:
        return None

    return _FilenameMatch(regex_match.group("suffix"), download_date)


def _parse_date(value: str) -> datetime.date:
    return datetime.datetime.strptime(value, "%d/%m/%Y").date()


def _parse_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", "").replace(" ", ""))
