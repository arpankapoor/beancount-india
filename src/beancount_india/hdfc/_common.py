"""Shared implementation details for HDFC importers."""

import datetime
from collections.abc import Mapping
from dataclasses import dataclass

_CURRENCY = "INR"


@dataclass(frozen=True, slots=True)
class _FilenameMatch:
    suffix: str
    download_date: datetime.date


class _SuffixAccounts:
    """Maps unique four-digit identifier suffixes to Beancount accounts."""

    def __init__(self, accounts: Mapping[str, str], identifier: str) -> None:
        self._accounts: dict[str, str] = {}
        for number, account in accounts.items():
            if len(number) <= 4 or not number.isdecimal():
                raise ValueError(
                    f"HDFC {identifier}s must contain more than four digits"
                )

            suffix = number[-4:]
            if suffix in self._accounts:
                raise ValueError(f"Multiple HDFC {identifier}s end in {suffix!r}")
            self._accounts[suffix] = account

    def __contains__(self, suffix: str) -> bool:
        return suffix in self._accounts

    def account(self, suffix: str) -> str:
        try:
            return self._accounts[suffix]
        except KeyError:
            raise ValueError(f"No account configured for suffix {suffix!r}") from None
