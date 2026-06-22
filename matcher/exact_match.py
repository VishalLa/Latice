from __future__ import annotations 

from typing import List
from schema import BankStatement, LedgerFormat

_AMOUNT_TOL = 0.05

def _amounts_equal(a: float, b: float) -> bool:
    return abs(a - b) <= _AMOUNT_TOL


def exact_matcher(
    gl_records: List[LedgerFormat], 
    bank_records: List[BankStatement],
    same_sid: bool = True   # True = cashbook, False = standard GL
) -> dict: 
    
    ledger_used = [False] * len(gl_records)
    bank_used = [False] * len(bank_records)
    exact_matches: List[dict] = []

    def attempt(require_ref_confirmation: bool) -> None: 
        for gi, gl in enumerate(gl_records):
            if ledger_used[gi]:
                continue
            if not gl.transaction_date:
                continue

            gl_debit = gl.debit_amount
            gl_credit = gl.credit_amount

            for bi, bank in enumerate(bank_records):
                if bank_used[bi]:
                    continue
                if not bank.date:
                    continue
                
                if bank.date != gl.transaction_date:
                    continue

                amount_ok = False
                matched_amount = 0.0

                if same_sid:
                    if gl_debit > 0 and _amounts_equal(gl_debit, bank.debit):
                        amount_ok = True 
                        matched_amount = gl_debit
                    elif gl_credit > 0 and _amounts_equal(gl_credit, bank.credit):
                        amount_ok = True
                        matched_amount = gl_credit
                else: 
                    if gl_debit > 0 and _amounts_equal(gl_debit, bank.credit):
                        amount_ok = True
                        matched_amount = gl_debit
                    elif gl_credit > 0 and _amounts_equal(gl_credit, bank.debit):
                        amount_ok = True
                        matched_amount = gl_credit

                if not amount_ok:
                    continue

                ref_gl = gl.reference_id
                ref_bank = bank.txn_id

                if ref_gl and ref_bank:
                    if ref_gl == ref_bank:
                        ref_matched = True
                    else:
                        ref_matched = False 
                else:
                    ref_matched = False

                if require_ref_confirmation and not ref_matched:
                    continue

                ledger_used[gi] = True
                bank_used[bi] = True
                exact_matches.append({
                    "ledger_id": gl.ledger_id,
                    "bank_id": bank.row_index,
                    "amount": matched_amount,
                    "date": gl.transaction_date,
                    "reference_matched": ref_matched,
                })
                break # this ledger row is spoken for; move to the next one

    # Pass 1: Prefer matches confirmed by a matching reference/cheque number
    attempt(require_ref_confirmation=True)
    
    # Pass 2: Accept amount+date-only matches for everything still pending.
    attempt(require_ref_confirmation=False)

    pending_ledger = [gl for gi, gl in enumerate(gl_records) if not ledger_used[gi]]
    pending_bank = [b for bi, b in enumerate(bank_records) if not bank_used[bi]]

    return {
        "EXACT_MATCHES": exact_matches,
        "PENDING_FUZZY_LEDGER": pending_ledger,
        "PENDING_FUZZY_BANK": pending_bank,
    }
