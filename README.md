# beancount-india

Beancount importers for Indian financial institutions.

## Installation

```console
uv add beancount-india
```

## HDFC Bank

### Account statements

On the HDFC NetBanking Account Statement page, download the statement in
**Delimited** format. Supported filenames have this form:

`Acct_Statement_XXXXXXXX1234_01012025.txt`

Transactions use their value dates. When possible, the importer derives an
opening balance from the first transaction in the statement.

### Credit card statements

On the HDFC Bank Get Statement page, download a past statement in **CSV**
format. Supported filenames have this form:

`Jan2025_Billedstatements_1234_06-01-25_20-46.csv`

Purchases produce negative liability postings; payments and refunds produce
positive postings. Nonzero reward points are included as `reward_points`
metadata.

### Configuration

Configure the importers with full account or card numbers mapped to their
Beancount accounts. Numbers must be strings; filenames are matched using their
final four digits.

```python
import beangulp

from beancount_india.hdfc import HDFCAccountImporter, HDFCCardImporter

ingest = beangulp.Ingest(
    [
        HDFCAccountImporter({"12345678901234": "Assets:HDFC:Account"}),
        HDFCCardImporter({"1234567890123456": "Liabilities:HDFC:Card"}),
    ]
)

if __name__ == "__main__":
    ingest()
```
