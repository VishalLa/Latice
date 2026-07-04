from __future__ import annotations 

from typing import List, Optional
from schema import BankStatement, LedgerFormat
from .fuzzy_match import extract_utr


def _get_iso_date_str(rec: object, is_bank: bool = False) -> Optional[str]:
    """Return an ISO-like date string (YYYY-MM-DD) from record, trying
    primary then raw fields. Returns None if no usable date found."""
    if is_bank:
        for attr in ("date", "date_raw"):
            val = getattr(rec, attr, None)
            if val not in (None, ""):
                s = str(val).strip()
                return s[:10]
    else:
        for attr in ("transaction_date", "transaction_date_raw"):
            val = getattr(rec, attr, None)
            if val not in (None, ""):
                s = str(val).strip()
                return s[:10]
    return None

_DEFAULT_AMOUNT_TOL = 0.05

def _amounts_equal(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol

def exact_matcher(
    gl_records: List[LedgerFormat], 
    bank_records: List[BankStatement],
    same_side: bool = True,        # True = cashbook, False = standard GL
    amount_tol: float = _DEFAULT_AMOUNT_TOL,  # driven by TOLERANCES["EXACT"]
) -> dict: 
    
    print("Exact Match")
    
    ledger_used = [False] * len(gl_records)
    bank_used = [False] * len(bank_records)
    exact_matches: List[dict] = []

    def attempt(require_ref_confirmation: bool) -> None: 
        for gi, gl in enumerate(gl_records):
            if ledger_used[gi]:
                continue
            gld = _get_iso_date_str(gl, is_bank=False)
            if not gld:
                continue

            gl_debit = gl.debit_amount
            gl_credit = gl.credit_amount

            for bi, bank in enumerate(bank_records):
                if bank_used[bi]:
                    continue
                bd = _get_iso_date_str(bank, is_bank=True)
                if not bd:
                    continue

                if bd != gld:
                    continue

                amount_ok = False
                matched_amount = 0.0

                if same_side:
                    if gl_debit > 0 and _amounts_equal(gl_debit, bank.debit, amount_tol):
                        amount_ok = True 
                        matched_amount = gl_debit
                    elif gl_credit > 0 and _amounts_equal(gl_credit, bank.credit, amount_tol):
                        amount_ok = True
                        matched_amount = gl_credit
                else: 
                    if gl_debit > 0 and _amounts_equal(gl_debit, bank.credit, amount_tol):
                        amount_ok = True
                        matched_amount = gl_debit
                    elif gl_credit > 0 and _amounts_equal(gl_credit, bank.debit, amount_tol):
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

                utr_matched = False
                if not ref_matched:
                    utr_gl = extract_utr(gl.reference_id) or extract_utr(gl.account_name)
                    utr_bank = extract_utr(bank.txn_id) or extract_utr(bank.narration)
                    if utr_gl and utr_bank and utr_gl == utr_bank:
                        utr_matched = True
                
                if require_ref_confirmation and not (ref_matched or utr_matched):
                    continue

                # if require_ref_confirmation and not ref_matched:
                #     continue

                ledger_used[gi] = True
                bank_used[bi] = True
                if ref_matched:
                    confirmation_method = "reference_id"
                elif utr_matched:
                    confirmation_method = "narration_utr"
                else:
                    confirmation_method = "amount_date_only"
                exact_matches.append({
                    "ledger_id": gl.ledger_id,
                    "bank_id": bank.row_index,
                    "amount": matched_amount,
                    "date": gld,
                    # "reference_matched": ref_matched
                    "reference_matched": ref_matched or utr_matched,
                    "confirmation_method": confirmation_method,
                })
                break

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
