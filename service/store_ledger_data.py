from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from schema import JournalEntry, TDSEntry
from database.ledger_tax_models import (
    BillModel,
    JournalEntryModel,
    JournalLineModel,
    TDSEntryModel,
    TDSAggregateModel,
    GSTR1RecordModel,
)

def fy_label_for_date(d) -> str:
    year = d.year if d.month >= 4 else d.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


class PushLedgerData:

    @staticmethod
    def prime_tds_engine_aggregates(
        session: Session,
        user_id: str,
        deductee_name: str,
        financial_year: str,
        tds_engine,
    ) -> None:
        key = deductee_name.lower().strip()
        rows = session.query(TDSAggregateModel).filter(
            TDSAggregateModel.user_id == user_id,
            TDSAggregateModel.deductee_key == key,
            TDSAggregateModel.financial_year == financial_year,
        ).all()
        for row in rows:
            tds_engine._aggregates[(row.deductee_key, row.section_code)] = row.running_gross

    @staticmethod
    def persist_tds_engine_aggregates(
        session: Session,
        user_id: str,
        financial_year: str,
        tds_engine,
    ) -> None:
        try:
            for (deductee_key, section_code), running in tds_engine._aggregates.items():
                existing = session.query(TDSAggregateModel).filter(
                    TDSAggregateModel.user_id == user_id,
                    TDSAggregateModel.deductee_key == deductee_key,
                    TDSAggregateModel.section_code == section_code,
                    TDSAggregateModel.financial_year == financial_year,
                ).first()
                if existing:
                    existing.running_gross = running
                else:
                    session.add(TDSAggregateModel(
                        user_id=user_id, deductee_key=deductee_key,
                        section_code=section_code, financial_year=financial_year,
                        running_gross=running,
                    ))
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while persisting TDS aggregates: {e}")

    @staticmethod
    def push_journal_entry(
        session: Session,
        user_id: str,
        journal_entry: JournalEntry,
        bill: Optional[BillModel] = None,
    ) -> Optional[JournalEntryModel]:
        """Persist one JournalEntry (with its EntryLines) for `user_id`."""
        try:
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
            session.commit()
            session.refresh(model)
            return model
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting journal entry: {e}")
            return None

    @staticmethod
    def push_tds_entry(
        session: Session,
        user_id: str,
        tds_entry: TDSEntry,
        journal_entry_model: Optional[JournalEntryModel] = None,
    ) -> Optional[TDSEntryModel]:
        try:
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
            session.commit()
            session.refresh(model)
            return model
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting TDS entry: {e}")
            return None

    @staticmethod
    def replace_gstr1_period(
        session: Session,
        user_id: str,
        period_label: str,
        gstr1: dict,
    ) -> bool:
        try:
            session.query(GSTR1RecordModel).filter(
                GSTR1RecordModel.user_id == user_id,
                GSTR1RecordModel.period_label == period_label,
            ).delete(synchronize_session=False)

            rows: list[GSTR1RecordModel] = []

            for row in gstr1.get("b2b", []):
                rows.append(GSTR1RecordModel(
                    user_id=user_id, period_label=period_label, table_type="b2b",
                    invoice_number=row.get("invoice_number"),
                    invoice_value=row.get("invoice_value"),
                    recipient_gstin=row.get("receiver_gstin"),
                    recipient_name=row.get("receiver_name"),
                    place_of_supply=row.get("place_of_supply"),
                    tax_rate=row.get("gst_rate"),
                    taxable_value=row.get("taxable_value", 0.0),
                    igst=row.get("igst", 0.0), cgst=row.get("cgst", 0.0),
                    sgst=row.get("sgst", 0.0), cess=row.get("cess", 0.0),
                    raw=row,
                ))

            for row in gstr1.get("b2c_large", []):
                rows.append(GSTR1RecordModel(
                    user_id=user_id, period_label=period_label, table_type="b2c_large",
                    place_of_supply=row.get("place_of_supply"),
                    tax_rate=row.get("gst_rate"),
                    taxable_value=row.get("taxable_value", 0.0),
                    igst=row.get("igst", 0.0), cgst=row.get("cgst", 0.0),
                    sgst=row.get("sgst", 0.0), cess=row.get("cess", 0.0),
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
                    user_id=user_id, period_label=period_label, table_type="nil_rated",
                    taxable_value=total_nil_exempt_non_gst,
                    raw=nil_rated,
                ))

            for row in gstr1.get("hsn_summary", []):
                rows.append(GSTR1RecordModel(
                    user_id=user_id, period_label=period_label, table_type="hsn_summary",
                    hsn_code=row.get("hsn_sac"),
                    description=row.get("description"),
                    tax_rate=row.get("gst_rate"),
                    taxable_value=row.get("taxable_value", 0.0),
                    igst=row.get("igst", 0.0), cgst=row.get("cgst", 0.0),
                    sgst=row.get("sgst", 0.0), cess=row.get("cess", 0.0),
                    raw=row,
                ))

            session.add_all(rows)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while replacing GSTR-1 period data: {e}")
            return False
