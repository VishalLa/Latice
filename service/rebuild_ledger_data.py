from __future__ import annotations

from datetime import date as date_
from typing import List, Optional

from schema import (
    Account, AccountGroup, DrCr, EntryLine, JournalEntry,
    TDSEntry, DeducteeType, TDSRegister,
)
from database.ledger_tax_models import JournalEntryModel, TDSEntryModel

_GROUP_BY_VALUE = {g.value: g for g in AccountGroup}

def _account_group_from_value(value: Optional[str]) -> AccountGroup:
    return _GROUP_BY_VALUE.get(value or "", AccountGroup.CURRENT_ASSETS)

def rebuild_journal_entry(model: JournalEntryModel) -> JournalEntry:
    lines = [
        EntryLine(
            account=Account(name=l.account_name, group=_account_group_from_value(l.account_group)),
            dr_cr=DrCr(l.dr_cr),
            amount=l.amount,
            narration=l.narration or "",
        )
        for l in model.lines
    ]
    return JournalEntry(
        entry_id=model.entry_id,
        date=model.date,
        voucher_type=model.voucher_type,
        narration=model.narration,
        lines=lines,
        source_file=model.source_file or "",
        invoice_number=model.invoice_number or "",
        vendor_name=model.vendor_name or "",
        direction=model.direction or "",
    )

def rebuild_journal_entries(models: List[JournalEntryModel]) -> List[JournalEntry]:
    return sorted((rebuild_journal_entry(m) for m in models), key=lambda e: e.date)

def rebuild_tds_entry(model: TDSEntryModel) -> TDSEntry:
    return TDSEntry(
        section_code=model.section_code,
        deductee_name=model.deductee_name,
        gross_amount=model.gross_amount,
        tds_base=model.tds_base,
        tds_rate=model.tds_rate,
        tds_amount=model.tds_amount,
        net_payment=model.net_payment,
        entry_id=model.entry_id,
        date=model.date,
        deductee_pan=model.deductee_pan,
        deductee_type=DeducteeType(model.deductee_type),
        deductee_gstin=model.deductee_gstin,
        source_journal_id=model.journal_entry.entry_id if model.journal_entry else None,
        invoice_number=model.invoice_number,
        rate_enhanced_206aa=model.rate_enhanced_206aa,
        deposit_date=model.deposit_date,
        challan_bsr_code=model.challan_bsr_code,
        challan_serial=model.challan_serial,
        challan_date=model.challan_date,
    )

def rebuild_tds_register(
    models: List[TDSEntryModel],
    period_start: date_,
    period_end: date_,
) -> TDSRegister:
    entries = [rebuild_tds_entry(m) for m in models]
    return TDSRegister(entries=entries, period_start=period_start, period_end=period_end)
