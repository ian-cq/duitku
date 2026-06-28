"""Bank parser registry.

Dispatch by bank slug. Each parser module exposes ``parse(path,
passwords=...)`` returning a :class:`duitku.models.Statement` and a
``parse_text(text, statement_year=...)`` test hook.

To add a new bank:
1. Create ``duitku/parsers/<slug>.py`` mirroring an existing module
   (``maybank.py`` is the most complete reference).
2. Add ``"<slug>": <slug>.parse`` to :data:`PARSERS` below.
3. Add an entry under the slug in ``accounts.yaml`` so the operator
   can map the parser's ``account_id`` to a Firefly asset account.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from duitku.models import Statement
from duitku.parsers import maybank, ryt, uob


class _Parser(Protocol):
    def __call__(
        self, path: Path, *, passwords: list[str]
    ) -> Statement: ...  # pragma: no cover


PARSERS: dict[str, _Parser] = {
    maybank.BANK: maybank.parse,
    uob.BANK: uob.parse,
    ryt.BANK: ryt.parse,
}


def parse(bank: str, path: Path, *, passwords: list[str]) -> Statement:
    """Dispatch to the bank's parser. ``KeyError`` if bank is unknown."""
    try:
        fn = PARSERS[bank]
    except KeyError as exc:
        raise KeyError(
            f"no parser registered for bank {bank!r}; known: {sorted(PARSERS)}"
        ) from exc
    return fn(path, passwords=passwords)


# Test-hook dispatch by slug, mirroring :func:`parse`.
_TEXT_PARSERS: dict[str, Callable[..., Statement]] = {
    maybank.BANK: maybank.parse_text,
    uob.BANK: uob.parse_text,
    ryt.BANK: ryt.parse_text,
}


def parse_text(bank: str, text: str, *, statement_year: int | None = None) -> Statement:
    try:
        fn = _TEXT_PARSERS[bank]
    except KeyError as exc:
        raise KeyError(
            f"no parser registered for bank {bank!r}; known: {sorted(_TEXT_PARSERS)}"
        ) from exc
    return fn(text, statement_year=statement_year)


__all__ = ["PARSERS", "parse", "parse_text"]
