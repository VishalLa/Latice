from __future__ import annotations

from typing import Optional
from schema import JournalEntry
from .journal import to_journal_entries
from .ledger import GeneralLedger


def build_ledger(
    bills:             list[dict],
    opening_balances:  Optional[list[JournalEntry]] = None,
    _prebuilt_entries: Optional[list[JournalEntry]] = None,
) -> tuple[GeneralLedger, list[JournalEntry]]:
    """
    Full pipeline: bills → journal entries → general ledger.

    Parameters
    ----------
    bills
        List of bill dicts from the scanner.  Ignored when
        _prebuilt_entries is provided.
    opening_balances
        Optional list of JournalEntry objects for the opening entry
        (from opening_balances.py).  Posted before transaction entries.
    _prebuilt_entries
        When supplied by the pipeline (after TDS modification), these
        entries are used directly instead of calling to_journal_entries().
        Callers outside the pipeline should leave this as None.

    Returns
    -------
    (GeneralLedger, list[JournalEntry]) — ledger + full audit trail.
    """
    gl          = GeneralLedger()
    all_entries: list[JournalEntry] = []

    # Opening balances first — they carry the period-start date
    if opening_balances:
        gl.post_entries(opening_balances)
        all_entries.extend(opening_balances)

    # Transaction entries — either pre-built (TDS path) or freshly built
    if _prebuilt_entries is not None:
        tx_entries = _prebuilt_entries
    else:
        tx_entries = to_journal_entries(bills)

    gl.post_entries(tx_entries)
    all_entries.extend(tx_entries)

    # Sort full audit trail by date
    all_entries.sort(key=lambda e: e.date)

    return gl, all_entries
