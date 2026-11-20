import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

from beancount.core import amount, data

from beancount_india.hdfc import HDFCAccountImporter
from beancount_india.hdfc.statements import (
    _HEADER,
    _parse_statement,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "Acct_Statement_XXXXXXXX1234_01012025.txt"
)
HEADER = ",".join(_HEADER) + "\n"


class HDFCAccountParserTest(unittest.TestCase):
    def test_parses_padded_rows_and_unquoted_narration(self) -> None:
        rows = _parse_statement(FIXTURE)

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].line_number, 3)
        self.assertEqual(rows[0].transaction_date, date(2025, 1, 2))
        self.assertEqual(rows[0].narration, "ATM WITHDRAWAL, EXAMPLE LOCATION")
        self.assertEqual(rows[0].debit, Decimal("125.50"))
        self.assertEqual(rows[0].credit, Decimal("0.00"))
        self.assertEqual(rows[0].amount, Decimal("-125.50"))
        self.assertEqual(rows[0].reference_number, "REF001")
        self.assertEqual(rows[0].closing_balance, Decimal("874.50"))
        self.assertEqual(rows[2].transaction_date, date(2025, 1, 4))
        self.assertEqual(rows[2].value_date, date(2025, 1, 5))
        self.assertEqual(rows[2].amount, Decimal("1.25"))

    def test_rejects_unexpected_header(self) -> None:
        path = self._write_statement("Wrong,Header\n")

        with self.assertRaisesRegex(ValueError, "unexpected header"):
            _parse_statement(path)

    def test_rejects_row_with_debit_and_credit(self) -> None:
        path = self._write_statement(
            HEADER + "02/01/25,INVALID,02/01/25,1.00,1.00,REF001,100.00\n"
        )

        with self.assertRaisesRegex(ValueError, "invalid statement row"):
            _parse_statement(path)

    def _write_statement(self, contents: str) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", delete=False
        ) as temporary_file:
            temporary_file.write(contents)
            path = Path(temporary_file.name)

        self.addCleanup(path.unlink, missing_ok=True)
        return path


class HDFCAccountImporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.importer = HDFCAccountImporter({"12345678901234": "Assets:HDFC:Account"})

    def test_file_metadata(self) -> None:
        filepath = "/downloads/Acct_Statement_XXXXXXXX1234_01012025.txt"

        self.assertTrue(self.importer.identify(filepath))
        self.assertEqual(self.importer.account(filepath), "Assets:HDFC:Account")
        self.assertEqual(self.importer.date(filepath), date(2025, 1, 1))

    def test_archive_filename_uses_effective_date_range(self) -> None:
        self.assertEqual(
            self.importer.filename(str(FIXTURE)),
            "statement.2025-01-02_2025-01-05.txt",
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Acct_Statement_XXXXXXXX1234_01012025.txt"
            path.write_text(
                HEADER
                + "02/01/25,SINGLE TRANSACTION,02/01/25,1.00,0.00,REF001,99.00\n",
                encoding="utf-8",
                newline="",
            )
            self.assertEqual(
                self.importer.filename(str(path)), "statement.2025-01-02.txt"
            )

    def test_extracts_opening_balance_and_transactions(self) -> None:
        entries = self.importer.extract(str(FIXTURE), [])

        self.assertEqual(len(entries), 4)
        self.assertIsInstance(entries[0], data.Balance)
        balance = cast(data.Balance, entries[0])
        self.assertEqual(balance.meta["filename"], FIXTURE.name)
        self.assertEqual(balance.meta["lineno"], 3)
        self.assertEqual(balance.date, date(2025, 1, 2))
        self.assertEqual(balance.account, "Assets:HDFC:Account")
        self.assertEqual(balance.amount, amount.Amount(Decimal("1000.00"), "INR"))

        self.assertIsInstance(entries[1], data.Transaction)
        debit = cast(data.Transaction, entries[1])
        self.assertEqual(debit.meta["filename"], FIXTURE.name)
        self.assertEqual(debit.meta["lineno"], 3)
        self.assertEqual(debit.meta["reference"], "REF001")
        self.assertEqual(debit.date, date(2025, 1, 2))
        self.assertEqual(debit.flag, "*")
        self.assertIsNone(debit.payee)
        self.assertEqual(debit.narration, "ATM WITHDRAWAL, EXAMPLE LOCATION")
        self.assertEqual(len(debit.postings), 1)
        self.assertEqual(debit.postings[0].account, "Assets:HDFC:Account")
        self.assertEqual(
            debit.postings[0].units, amount.Amount(Decimal("-125.50"), "INR")
        )

        self.assertIsInstance(entries[2], data.Transaction)
        credit = cast(data.Transaction, entries[2])
        self.assertEqual(
            credit.postings[0].units, amount.Amount(Decimal("500.00"), "INR")
        )

        self.assertIsInstance(entries[3], data.Transaction)
        interest = cast(data.Transaction, entries[3])
        self.assertEqual(interest.date, date(2025, 1, 5))

    def test_opening_balance_respects_existing_entries(self) -> None:
        extracted = self.importer.extract(str(FIXTURE), [])
        balance = cast(data.Balance, extracted[0])
        transaction = cast(data.Transaction, extracted[1])

        with self.subTest("identical balance"):
            entries = self.importer.extract(str(FIXTURE), [balance])
            self.assertEqual(len(entries), 3)
            self.assertIsInstance(entries[0], data.Transaction)

        with self.subTest("same-account transaction"):
            entries = self.importer.extract(str(FIXTURE), [transaction])
            self.assertEqual(len(entries), 3)
            self.assertIsInstance(entries[0], data.Transaction)

        with self.subTest("other-account transaction"):
            other_posting = transaction.postings[0]._replace(account="Assets:Other")
            other_transaction = transaction._replace(postings=[other_posting])
            entries = self.importer.extract(str(FIXTURE), [other_transaction])
            self.assertEqual(len(entries), 4)
            self.assertIsInstance(entries[0], data.Balance)

        with self.subTest("conflicting balance"):
            conflicting = balance._replace(
                amount=amount.Amount(Decimal("999.00"), "INR")
            )
            with self.assertRaisesRegex(ValueError, "conflicts with statement"):
                self.importer.extract(str(FIXTURE), [conflicting])

    def test_rejects_other_filenames_and_unconfigured_accounts(self) -> None:
        unconfigured = "Acct_Statement_XXXXXXXX5678_01012025.txt"
        filenames = (
            "Acct_Statement_XXXXXXXX1234_01012025(1).txt",
            unconfigured,
            "Acct_Statement_XXXXXXXX1234_32012025.txt",
            "statement.csv",
        )

        for filename in filenames:
            with self.subTest(filename=filename):
                self.assertFalse(self.importer.identify(filename))

        with self.assertRaisesRegex(ValueError, "No account configured"):
            self.importer.account(unconfigured)

    def test_rejects_invalid_or_ambiguous_account_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than four digits"):
            HDFCAccountImporter({"1234": "Assets:HDFC:Account"})

        with self.assertRaisesRegex(ValueError, "Multiple HDFC account numbers"):
            HDFCAccountImporter(
                {
                    "12345678901234": "Assets:HDFC:First",
                    "98765432101234": "Assets:HDFC:Second",
                }
            )


if __name__ == "__main__":
    unittest.main()
