import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from beancount.core import amount, data
from beancount.parser import printer

from beancount_india.hdfc import HDFCCardImporter
from beancount_india.hdfc.cards import (
    _ACCOUNT_SUMMARY_HEADER,
    _DELIMITER,
    _REWARD_POINTS_SUMMARY_HEADER,
    _parse_card_statement,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "Jan2025_Billedstatements_1234_06-01-25_20-46.csv"
)


class HDFCCardParserTest(unittest.TestCase):
    def test_parses_statement_metadata_and_transactions(self) -> None:
        statement = _parse_card_statement(FIXTURE)

        self.assertEqual(statement.card_suffix, "1234")
        self.assertEqual(statement.statement_date, date(2025, 1, 5))
        self.assertEqual(statement.payment_due_date, date(2025, 1, 25))
        self.assertEqual(statement.total_amount_due, Decimal("2372.00"))
        self.assertEqual(statement.minimum_amount_due, Decimal("120.00"))

        account_summary = statement.account_summary
        self.assertEqual(account_summary.opening_balance, Decimal("2348.40"))
        self.assertEqual(account_summary.payments_and_credits, Decimal("2000.00"))
        self.assertEqual(account_summary.purchases_and_debits, Decimal("2023.60"))
        self.assertEqual(account_summary.finance_charges, Decimal("0.00"))
        self.assertEqual(account_summary.total_dues, Decimal("2372.00"))

        reward_summary = statement.reward_points_summary
        self.assertEqual(reward_summary.opening_balance, 100)
        self.assertEqual(reward_summary.earned, 36)
        self.assertEqual(reward_summary.disbursed, 0)
        self.assertEqual(reward_summary.adjusted_or_lapsed, 0)
        self.assertEqual(reward_summary.closing_balance, 136)
        self.assertEqual(reward_summary.expiring_within_30_days, 0)
        self.assertEqual(reward_summary.expiring_within_60_days, 0)

        self.assertEqual(len(statement.transactions), 2)
        purchase, payment = statement.transactions

        self.assertEqual(purchase.line_number, 18)
        self.assertEqual(purchase.transaction_type, "International")
        self.assertEqual(purchase.cardholder, "EXAMPLE CUSTOMER")
        self.assertEqual(purchase.timestamp, datetime(2025, 1, 2, 18, 56, 14))
        self.assertEqual(purchase.description, "EXAMPLE MERCHANT")
        self.assertEqual(purchase.amount, Decimal("2023.60"))
        self.assertFalse(purchase.credit)
        self.assertEqual(purchase.reward_points, Decimal(36))
        self.assertEqual(purchase.signed_amount, Decimal("-2023.60"))

        self.assertEqual(payment.line_number, 17)
        self.assertEqual(payment.timestamp, datetime(2025, 1, 4, 8, 38, 4))
        self.assertTrue(payment.credit)
        self.assertEqual(payment.reward_points, Decimal(0))
        self.assertEqual(payment.signed_amount, Decimal("2000.00"))

    def test_rejects_missing_transaction_header(self) -> None:
        path = self._write_statement("Statement Date~|~05/01/2025\n")

        with self.assertRaisesRegex(ValueError, "transaction header not found"):
            _parse_card_statement(path)

    def test_rejects_missing_summary_table(self) -> None:
        contents = FIXTURE.read_text(encoding="utf-8")
        for header in (_ACCOUNT_SUMMARY_HEADER, _REWARD_POINTS_SUMMARY_HEADER):
            with self.subTest(header=header[0]):
                path = self._write_statement(
                    contents.replace(_DELIMITER.join(header), "Missing header")
                )
                with self.assertRaisesRegex(ValueError, "table not found"):
                    _parse_card_statement(path)

    def _write_statement(self, contents: str) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", delete=False
        ) as temporary_file:
            temporary_file.write(contents)
            path = Path(temporary_file.name)

        self.addCleanup(path.unlink, missing_ok=True)
        return path


class HDFCCardImporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.importer = HDFCCardImporter({"1234567890121234": "Liabilities:HDFC:Card"})

    def test_file_metadata(self) -> None:
        filepath = str(FIXTURE)

        self.assertTrue(self.importer.identify(filepath))
        self.assertEqual(self.importer.account(filepath), "Liabilities:HDFC:Card")
        self.assertEqual(self.importer.date(filepath), date(2025, 1, 5))
        self.assertEqual(self.importer.filename(filepath), "statement.2025-01-05.csv")

    def test_extracts_purchases_and_payments(self) -> None:
        entries = self.importer.extract(str(FIXTURE), [])

        self.assertEqual(len(entries), 2)
        purchase = cast(data.Transaction, entries[0])
        self.assertEqual(purchase.meta["filename"], FIXTURE.name)
        self.assertEqual(purchase.meta["lineno"], 18)
        self.assertEqual(purchase.meta["reward_points"], Decimal(36))
        self.assertIn("reward_points: 36", printer.format_entry(purchase))
        self.assertEqual(purchase.date, date(2025, 1, 2))
        self.assertEqual(purchase.flag, "*")
        self.assertIsNone(purchase.payee)
        self.assertEqual(purchase.narration, "EXAMPLE MERCHANT")
        self.assertEqual(len(purchase.postings), 1)
        self.assertEqual(purchase.postings[0].account, "Liabilities:HDFC:Card")
        self.assertEqual(
            purchase.postings[0].units,
            amount.Amount(Decimal("-2023.60"), "INR"),
        )

        payment = cast(data.Transaction, entries[1])
        self.assertNotIn("reward_points", payment.meta)
        self.assertEqual(
            payment.postings[0].units,
            amount.Amount(Decimal("2000.00"), "INR"),
        )

    def test_rejects_other_filenames_and_unconfigured_cards(self) -> None:
        unconfigured = "Jan2025_Billedstatements_5678_06-01-25_20-46.csv"
        filenames = (
            "Jan2025_Billedstatements_1234_06-01-25_20-46(1).csv",
            unconfigured,
            "Jan2025_Billedstatements_1234_32-01-25_20-46.csv",
            "statement.csv",
        )

        for filename in filenames:
            with self.subTest(filename=filename):
                self.assertFalse(self.importer.identify(filename))

        with self.assertRaisesRegex(ValueError, "No account configured"):
            self.importer.account(unconfigured)

    def test_rejects_filename_and_statement_suffix_mismatch(self) -> None:
        importer = HDFCCardImporter({"1234567890125678": "Liabilities:HDFC:Card"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Jan2025_Billedstatements_5678_06-01-25_20-46.csv"
            path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "does not match filename"):
                importer.extract(str(path), [])

    def test_rejects_invalid_or_ambiguous_card_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than four digits"):
            HDFCCardImporter({"1234": "Liabilities:HDFC:Card"})

        with self.assertRaisesRegex(ValueError, "Multiple HDFC card numbers"):
            HDFCCardImporter(
                {
                    "1234567890121234": "Liabilities:HDFC:First",
                    "9876543210121234": "Liabilities:HDFC:Second",
                }
            )


if __name__ == "__main__":
    unittest.main()
