"""Tests for the per-bank parsers.

We use the ``parse_text`` hook to avoid fixturing real PDFs (which
would carry the parser's own bank-data and be a leak risk anyway).
The synthetic text mimics the layout pdfplumber would emit with
``layout=True`` extraction.
"""

from __future__ import annotations

from decimal import Decimal

from duitku.parsers import maybank, ryt, uob


# ---- Maybank credit card -------------------------------------------


MAYBANK_CC = """
MAYBANK CREDIT CARD STATEMENT
Statement Date 15 JUN 2024
CARD NUMBER: 5123 12** **** 1234
CURRENCY: MYR

POSTDATE  TXNDATE  DESCRIPTION                              AMOUNT
15/06     14/06    GRAB*RIDE KL                              42.50
16/06     15/06    PAYMENT VIA M2U                          500.00CR
17/06     16/06    STARBUCKS KLCC                            18.90
"""


def test_maybank_credit_card_basic():
    s = maybank.parse_text(MAYBANK_CC)
    assert s.bank == "maybank"
    assert s.account_id == "1234"
    assert s.currency == "MYR"
    assert len(s.transactions) == 3

    debit = s.transactions[0]
    assert debit.kind == "withdrawal"
    assert debit.amount == Decimal("42.50")
    assert "GRAB" in debit.description
    assert debit.date.year == 2024

    credit = s.transactions[1]
    assert credit.kind == "deposit"
    assert credit.amount == Decimal("500.00")


# ---- Maybank savings ------------------------------------------------


MAYBANK_SA = """
MAYBANK SAVINGS ACCOUNT STATEMENT
Statement Date 15 JUN 2024
ACCOUNT NO: ******5678
OPENING BALANCE                                          5,000.00
DD/MM    DESCRIPTION                       AMOUNT          BALANCE
15/06    CDM CASH DEPOSIT                  1,000.00+      6,000.00
16/06    DUITNOW TO 1234567890                50.00-      5,950.00
         REF: 0987654321
ENDING BALANCE                                           5,950.00
"""


def test_maybank_savings_basic():
    s = maybank.parse_text(MAYBANK_SA)
    assert s.bank == "maybank"
    assert s.account_id == "5678"
    assert s.currency == "MYR"
    assert len(s.transactions) == 2
    assert s.opening_balance == Decimal("5000.00")
    assert s.closing_balance == Decimal("5950.00")

    deposit = s.transactions[0]
    assert deposit.kind == "deposit"
    assert deposit.amount == Decimal("1000.00")

    withdrawal = s.transactions[1]
    assert withdrawal.kind == "withdrawal"
    assert withdrawal.amount == Decimal("50.00")
    # Continuation line picked up the bank reference.
    assert withdrawal.bank_reference == "0987654321"


def test_maybank_savings_reconciles():
    from duitku.normalise import reconcile

    s = maybank.parse_text(MAYBANK_SA)
    assert reconcile(s)


# ---- UOB layout A (two-column) -------------------------------------


UOB_A = """
UOB CURRENT ACCOUNT STATEMENT
Statement Date 30 JUN 2024
ACCOUNT NUMBER: ****-****-1111
CURRENCY: MYR
OPENING BALANCE                                          1,000.00
DATE         DESCRIPTION                  DEBIT      CREDIT     BALANCE
15 JUN 2024  GRAB*RIDE KL                  42.50                  957.50
18 JUN 2024  PAYROLL CREDIT                          5,000.00   5,957.50
CLOSING BALANCE                                                 5,957.50
"""


def test_uob_layout_a():
    s = uob.parse_text(UOB_A)
    assert s.bank == "uob"
    assert s.account_id == "1111"
    assert s.currency == "MYR"
    assert len(s.transactions) == 2
    # Debit row: first amount in the column to the left of midline.
    debit = next(t for t in s.transactions if t.amount == Decimal("42.50"))
    assert debit.kind == "withdrawal"
    credit = next(t for t in s.transactions if t.amount == Decimal("5000.00"))
    assert credit.kind == "deposit"


# ---- UOB layout B (signed-amount column) ----------------------------


UOB_B = """
UOB SAVINGS ACCOUNT
Statement Date 30 JUN 2024
ACCOUNT NUMBER: ****-****-2222
CURRENCY: SGD
DATE         DESCRIPTION              AMOUNT     SIGN   BALANCE
15/06/2024   GRAB*RIDE SG              42.50     DR     957.50
18/06/2024   PAYROLL CREDIT         5,000.00     CR   5,957.50
"""


def test_uob_layout_b():
    s = uob.parse_text(UOB_B)
    assert s.bank == "uob"
    assert s.account_id == "2222"
    assert s.currency == "SGD"
    assert len(s.transactions) == 2
    debit = next(t for t in s.transactions if t.amount == Decimal("42.50"))
    assert debit.kind == "withdrawal"


# ---- Ryt ------------------------------------------------------------


RYT_TEXT = """
RYT BANK
Statement Date 15 JUN 2024
ACCOUNT NUMBER: ****9012
OPENING BALANCE                                          0.00
DATE         DESCRIPTION                    AMOUNT      BALANCE
15/06/2024   DUITNOW FROM ALI               1,000.00+   1,000.00
16/06/2024   GRAB*RIDE KL                      42.50-     957.50
CLOSING BALANCE                                            957.50
"""


def test_ryt_basic():
    s = ryt.parse_text(RYT_TEXT)
    assert s.bank == "ryt"
    assert s.account_id == "9012"
    assert s.currency == "MYR"
    assert len(s.transactions) == 2
    assert s.transactions[0].kind == "deposit"
    assert s.transactions[1].kind == "withdrawal"
