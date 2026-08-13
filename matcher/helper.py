from __future__ import annotations

import re
from typing import Any, Optional
from datetime import date as _date
from difflib import SequenceMatcher


_STOPWORDS = {
    "the", "inc", "ltd", "llc", "co", "corp", "corporation", "company",
    "payment", "ach", "trfr", "transfer", "ref", "upi", "neft", "rtgs",
    "imps", "dr", "cr", "no",
}


_NSF_RE      = re.compile(r"\b(nsf|bounced?|returned?|dishonou?red)\b", re.I)
_INTEREST_RE = re.compile(r"\b(interest|apy|intt?)\b", re.I)

_REVERSAL_RE = re.compile(r"\b(reversal|return|bounced|reject|dup|duplicate)\b", re.I)

_INDIAN_PREFIX_RE = re.compile(
    r"""
    (?:
        \b(?:UPI|NEFT|IMPS|RTGS|NACH|ACH|ECS|NACH)\s*[/\-]?
        |
        \b(?:UTR|REF|REFNO|TXNID|TXN|TRNREF)\s*[:\-]?\s*[A-Z0-9]{8,22}\b
        |
        \b[0-9]{12,22}\b
        |
        \b[A-Z]{4}0[A-Z0-9]{6}\b
        |
        \b(?:HDFC|ICICI|AXIS|SBI|PNB|BOB|KOTAK|YES|IDBI|INDUS|FEDERAL)\s*BANK\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_UTR_EXTRACT_RE = re.compile(
    r"""
    (?:
        \b(?:UTR|REF|REFNO|TXNID|TXN|TRNREF)\s*[:\-]?\s*(?P<labelled>[A-Z0-9]{8,22})\b
        |
        \b(?P<bare>[0-9]{12,22})\b
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

def extract_utr(text: Optional[str]) -> Optional[str]:
    if not text:
        return None

    candidates = []
    for m in _UTR_EXTRACT_RE.finditer(str(text)):
        value = m.group("labelled") or m.group("bare")
        if value:
            candidates.append(value.upper())

    if not candidates:
        return None

    return max(candidates, key=len)


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


def _amounts_equal(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def _to_date(iso_str: Optional[str]) -> Optional[_date]:
    if not iso_str:
        return None

    try:
        y, m, d = (int(p) for p in str(iso_str)[:10].split("-"))
        return _date(y, m, d)
    except Exception:
        return None


def _days_between(
    ledger_date_iso: Optional[str],
    bank_date_iso:   Optional[str],
) -> Optional[int]:
    ld, bd = _to_date(ledger_date_iso), _to_date(bank_date_iso)
    if ld is None or bd is None:
        return None
    return (bd - ld).days


def _crosses_month_boundary(
    date_iso_a: Optional[str],
    date_iso_b: Optional[str],
) -> bool:
    da, db = _to_date(date_iso_a), _to_date(date_iso_b)
    if da is None or db is None:
        return False
    return (da.year, da.month) != (db.year, db.month)


def _normalize_text(s: str) -> str:
    # Step 1 - strip Indian payment-rail noise
    s = _INDIAN_PREFIX_RE.sub(" ", s)

    # Step 2 - standard normalisation
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set:
    return {t for t in _normalize_text(s).split() if t and t not in _STOPWORDS}


def _acronym(s: str) -> str:
    return "".join(w[0] for w in _normalize_text(s).split() if w)


def text_similarity(account_name: Optional[str], narration: Optional[str]) -> float:
    if not account_name or not narration:
        return 0.0

    ratio = SequenceMatcher(
        None, _normalize_text(account_name), _normalize_text(narration)
    ).ratio()

    a_tok, n_tok = _tokens(account_name), _tokens(narration)

    jaccard = len(a_tok & n_tok) / len(a_tok | n_tok) if (a_tok and n_tok) else 0.0

    acr = _acronym(account_name)

    acr_score = 0.85 if len(acr) >= 2 and acr in n_tok else 0.0
    return max(ratio, jaccard, acr_score)


def _digit_multiset(amount: float) -> str:
    return "".join(sorted(str(int(round(abs(amount) * 100)))))


def is_transposition(amount_a: float, amount_b: float, tol: float) -> bool:
    return (
        not _amounts_equal(amount_a, amount_b, tol)
        and _digit_multiset(amount_a) == _digit_multiset(amount_b)
    )


def _safe_amount(obj: Any, attr: str, default: float = 0.0) -> float:
    try:
        val = getattr(obj, attr, default)
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _safe_str(obj: Any, attr: str, default: str = "") -> str:
    try:
        val = getattr(obj, attr, default)
        return str(val) if val is not None else default
    except Exception:
        return default


def _get_amt(rec: Any) -> float:
    if isinstance(rec, dict):
        return float(rec.get("debit", rec.get("debit_amount", rec.get("credit", rec.get("credit_amount", 0.0)))))
    for attr in ("debit", "debit_amount", "credit", "credit_amount"):
        val = getattr(rec, attr, None)
        if val:
            return float(val)
    return 0.0


def _get_date(rec: Any) -> Optional[_date]:
    d_str = (
        rec.get("date", rec.get("transaction_date"))
        if isinstance(rec, dict)
        else getattr(rec, "date", getattr(rec, "transaction_date", None))
    )
    return _to_date(str(d_str)) if d_str else None
