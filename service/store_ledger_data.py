from __future__ import annotations

from typing import List, Dict, Any, Optional

from sqlalchemy.orm import Session

from schema import JournalEntry, TDSEntry
from database import (
    DatabaseManager,
    BillModel,
    JournalEntryModel,
    JournalLineModel,
    TDSEntryModel,
    TDSAggregateModel,
    GSTR1RecordModel,
)
from ledger import TDSEngine

from .helper import _log_db_errors, fy_label_for_date


class PushLedgerData:
    """
    Pushes ledger/journal.py + ledger/tds.py + ledger/gstr1.py engine
    output into the DB, scoped to the owning user.

    Constructed once with a `DatabaseManager` (see session.py), which
    owns the db worker pool and its shared SQLAlchemy Session factory.
    `__init__` stores that manager on the instance so callers don't pass
    a `Session` into every method -- each method builds its own small
    `fn(session)` closure and hands it to `self.db_manager.run(...)`,
    which opens the session on a worker thread, runs the closure, and
    commits on success / rolls back on any exception.

    Usage:

        db_manager = DatabaseManager(db_url, pool_workers=8)
        pusher = PushLedgerData(db_manager)

        pusher.prime_tds_engine_aggregates(user_id, deductee, fy, tds_engine)
        # ... tds_engine.process_bill(...) ...
        pusher.persist_tds_engine_aggregates(user_id, fy, tds_engine)

        entry = pusher.push_journal_entry(user_id, journal_entry, bill=bill)
        pusher.push_tds_entry(user_id, tds_entry, journal_entry_model=entry)
    """

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager


    @_log_db_errors("loading TDS aggregate thresholds")
    def prime_tds_engine_aggregates(
        self,
        user_id: str,
        deductee_name: str,
        financial_year: str,
        tds_engine: TDSEngine,
    ) -> None:
        """
        Load this deductee's running totals (all sections, this user,
        this FY) from TDSAggregateModel into tds_engine._aggregates
        before calling tds_engine.process_bill(), so aggregate-threshold
        sections (194J, 194H, 194C, 194D, 194A...) see prior payments
        correctly even though each bill is processed by a separate
        TDSEngine() instance.
        """

        def _op(session: Session) -> None:
            key = deductee_name.lower().strip()
            rows: List[TDSAggregateModel] = session.query(TDSAggregateModel).filter(
                TDSAggregateModel.user_id == user_id,
                TDSAggregateModel.deductee_key == key,
                TDSAggregateModel.financial_year == financial_year,
            ).all()

            for row in rows:
                tds_engine._aggregates[(row.deductee_key, row.section_code)] = row.running_gross

        self.db_manager.run(_op)


    @_log_db_errors("persisting TDS aggregates")
    def persist_tds_engine_aggregates(
        self,
        user_id: str,
        financial_year: str,
        tds_engine: TDSEngine,
    ) -> None:
        """
        Write tds_engine._aggregates back to TDSAggregateModel after
        process_bill() has run (upsert), so the next bill for the same
        deductee starts from the correct running total.
        """

        def _op(session: Session) -> None:
            for (deductee_key, section_code), running in tds_engine._aggregates.items():
                existing: Optional[TDSAggregateModel] = session.query(TDSAggregateModel).filter(
                    TDSAggregateModel.user_id == user_id,
                    TDSAggregateModel.deductee_key == deductee_key,
                    TDSAggregateModel.section_code == section_code,
                    TDSAggregateModel.financial_year == financial_year,
                ).first()

                if existing:
                    existing.running_gross = running
                else:
                    session.add(TDSAggregateModel(
                        user_id=user_id,
                        deductee_key=deductee_key,
                        section_code=section_code,
                        financial_year=financial_year,
                        running_gross=running,
                    ))

        self.db_manager.run(_op)


    @_log_db_errors("inserting journal entry")
    def push_journal_entry(
        self,
        user_id: str,
        journal_entry: JournalEntry,
        bill: Optional[BillModel] = None,
    ) -> JournalEntryModel:
        """Persist one JournalEntry (with its EntryLines) for `user_id`."""

        def _op(session: Session) -> JournalEntryModel:
            model = JournalEntryModel(
                entry_id=journal_entry.entry_id,
                user_id=user_id,
                bill_id=bill.id if bill else None,
                date=journal_entry.date,
                voucher_type=journal_entry.voucher_type,
                narration=journal_entry.narration,
                source_file=journal_entry.source_file or None,
                invoice_number=journal_entry.invoice_number or None,
                vendor_name=journal_entry.vendor_name or None,
                direction=journal_entry.direction or None,
            )
            for line in journal_entry.lines:
                model.lines.append(JournalLineModel(
                    account_name=line.account.name,
                    account_group=line.account.group.value,
                    dr_cr=line.dr_cr.value,
                    amount=line.amount,
                    narration=line.narration or None,
                ))
                
            session.add(model)
            session.flush()
            session.refresh(model, attribute_names=["created_at"])
            return model

        return self.db_manager.run(_op)


    @_log_db_errors("inserting TDS entry")
    def push_tds_entry(
        self,
        user_id: str,
        tds_entry: TDSEntry,
        journal_entry_model: Optional[JournalEntryModel] = None,
    ) -> TDSEntryModel:
        """Persist one TDSEntry, linked back to its JournalEntryModel if known."""

        def _op(session: Session) -> TDSEntryModel:
            model = TDSEntryModel(
                entry_id=tds_entry.entry_id,
                user_id=user_id,
                journal_entry_id=journal_entry_model.id if journal_entry_model else None,
                date=tds_entry.date,
                section_code=tds_entry.section_code,
                deductee_name=tds_entry.deductee_name,
                deductee_pan=tds_entry.deductee_pan,
                deductee_type=tds_entry.deductee_type.value,
                deductee_gstin=tds_entry.deductee_gstin,
                gross_amount=tds_entry.gross_amount,
                tds_base=tds_entry.tds_base,
                tds_rate=tds_entry.tds_rate,
                tds_amount=tds_entry.tds_amount,
                net_payment=tds_entry.net_payment,
                invoice_number=tds_entry.invoice_number,
                rate_enhanced_206aa=tds_entry.rate_enhanced_206aa,
                deposit_date=tds_entry.deposit_date,
                challan_bsr_code=tds_entry.challan_bsr_code,
                challan_serial=tds_entry.challan_serial,
                challan_date=tds_entry.challan_date,
            )
            session.add(model)
            session.flush()
            session.refresh(model, attribute_names=["created_at"])
            return model

        return self.db_manager.run(_op)


    @_log_db_errors("replacing GSTR-1 period data")
    def replace_gstr1_period(
        self,
        user_id: str,
        period_label: str,
        gstr1: Dict[str, Any],
    ) -> bool:
        """
        Replace all GSTR1Record rows for (user_id, period_label) with the
        output of ledger.gstr1.build_gstr1(). Idempotent: safe to re-run
        a period's GSTR-1 generation after new bills come in.
        """

        def _op(session: Session) -> bool:
            session.query(GSTR1RecordModel).filter(
                GSTR1RecordModel.user_id == user_id,
                GSTR1RecordModel.period_label == period_label,
            ).delete(synchronize_session=False)

            rows: List[GSTR1RecordModel] = []

            for row in gstr1.get("b2b", []):
                rows.append(GSTR1RecordModel(
                    user_id=user_id, 
                    period_label=period_label, 
                    table_type="b2b",
                    invoice_number=row.get("invoice_number"),
                    invoice_value=row.get("invoice_value"),
                    recipient_gstin=row.get("receiver_gstin"),
                    recipient_name=row.get("receiver_name"),
                    place_of_supply=row.get("place_of_supply"),
                    tax_rate=row.get("gst_rate"),
                    taxable_value=row.get("taxable_value", 0.0),
                    igst=row.get("igst", 0.0), 
                    cgst=row.get("cgst", 0.0),
                    sgst=row.get("sgst", 0.0), 
                    cess=row.get("cess", 0.0),
                    raw=row,
                ))

            for row in gstr1.get("b2c_large", []):
                rows.append(GSTR1RecordModel(
                    user_id=user_id, 
                    period_label=period_label, 
                    table_type="b2c_large",
                    place_of_supply=row.get("place_of_supply"),
                    tax_rate=row.get("gst_rate"),
                    taxable_value=row.get("taxable_value", 0.0),
                    igst=row.get("igst", 0.0), 
                    cgst=row.get("cgst", 0.0),
                    sgst=row.get("sgst", 0.0), 
                    cess=row.get("cess", 0.0),
                    raw=row,
                ))

            nil_rated = gstr1.get("nil_rated") or {}
            if nil_rated:
                total_nil_exempt_non_gst = (
                    nil_rated.get("total_nil", 0.0)
                    + nil_rated.get("total_exempt", 0.0)
                    + nil_rated.get("total_non_gst", 0.0)
                )
                rows.append(GSTR1RecordModel(
                    user_id=user_id, 
                    period_label=period_label, 
                    table_type="nil_rated",
                    taxable_value=total_nil_exempt_non_gst,
                    raw=nil_rated,
                ))

            for row in gstr1.get("hsn_summary", []):
                rows.append(GSTR1RecordModel(
                    user_id=user_id, 
                    period_label=period_label, 
                    table_type="hsn_summary",
                    hsn_code=row.get("hsn_sac"),
                    description=row.get("description"),
                    tax_rate=row.get("gst_rate"),
                    taxable_value=row.get("taxable_value", 0.0),
                    igst=row.get("igst", 0.0), 
                    cgst=row.get("cgst", 0.0),
                    sgst=row.get("sgst", 0.0), 
                    cess=row.get("cess", 0.0),
                    raw=row,
                ))

            session.add_all(rows)
            return True

        return self.db_manager.run(_op)
