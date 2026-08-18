from __future__ import annotations

import os
import tempfile
from datetime import date as date_
from collections import defaultdict
from typing import Optional, List, Tuple, Dict, Any
from werkzeug.utils import secure_filename

from sqlalchemy.orm import Session

from ledger import GeneralLedger, build_ledger
from schema import (
    JournalEntry, 
    TDSRegister,
    LedgerFormat as SchemaLedger,
    BankStatement as SchemaBank
)
from database import (
    DatabaseManager,
    JournalEntryModel,
    TDSEntryModel,
    GSTR1RecordModel
)

from ._base import Base
from .rebuild_service_ledger import RebuildServiceLedger
from .run_result_service_bank_rec import ResultBankRec

from .reports.bank_recon_xlsx import write_bank_recon_xlsx
from .reports.gstr1_xlsx import write_gstr1_xlsx
from .reports.journal_xlsx import write_journal_xlsx
from .reports.ledger_xlsx import write_ledger_xlsx
from .reports.tds_xlsx import write_tds_xlsx


class GenerateReports:
    EXPORT_DIR = tempfile.gettempdir()
    
    
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        base = Base(db_manager)
        self.db_manager = base.get_manager
        self.config = base.get_config
        self.rebuild_service = RebuildServiceLedger(db_manager=self.db_manager)
        
        
    @staticmethod
    def _quarter_dates(financial_year: str, quarter: int) -> Tuple[date_, date_]:
        """
        financial_year: "2025-26" style (Apr start_year -> Mar start_year+1).
        quarter: 1-4 (Q1 = Apr-Jun ... Q4 = Jan-Mar).
        """
        if quarter not in (1, 2, 3, 4):
            raise ValueError(f"quarter must be 1-4, got {quarter}")

        start_year = int(financial_year.split("-")[0])
        starts = {
            1: date_(start_year, 4, 1),
            2: date_(start_year, 7, 1),
            3: date_(start_year, 10, 1),
            4: date_(start_year + 1, 1, 1),
        }
        ends = {
            1: date_(start_year, 6, 30),
            2: date_(start_year, 9, 30),
            3: date_(start_year, 12, 31),
            4: date_(start_year + 1, 3, 31),
        }
        return starts[quarter], ends[quarter]
    
    
    def _rebuild_gstr1_dict(
        self,
        session: Session,
        period_label: str,
        owner_id: Optional[str] = None,
    ) -> Optional[dict]:
        query = session.query(GSTR1RecordModel).filter(
            GSTR1RecordModel.period_label == period_label
        )
        if owner_id is not None:
            query = query.filter(GSTR1RecordModel.user_id == owner_id)
        rows = query.all()
        if not rows:
            return None
        
        grouped: dict = defaultdict(list)
        for r in rows:
            grouped[r.table_type].append(r.raw or r.to_dict())
            
        totals = {
            "b2b_invoice_count": len(grouped.get("b2b", [])),
            "total_taxable": round(sum(r.taxable_value for r in rows), 2),
            "total_tax": round(sum(r.igst + r.cgst + r.sgst + r.cess for r in rows), 2),
        }

        return {
            "period_label": period_label,
            "b2b": grouped.get("b2b", []),
            "b2c_large": grouped.get("b2c_large", []),
            "nil_rated": (grouped.get("nil_rated") or [{}])[0],
            "hsn_summary": grouped.get("hsn_summary", []),
            "totals": totals,
            "warnings": [],
        }
        
    
    def export_gstr1_report(
        self,
        period_label: str,
        owner_id: Optional[str] = None
    ) -> Tuple[str, bool]:
        def _op(session: Session) -> Optional[dict]:
            return self._rebuild_gstr1_dict(
                session=session,
                period_label=period_label,
                owner_id=owner_id
            )
            
        result = self.db_manager.run(_op)
        if result is None:
            return f"No GSTR-1 data found for period {period_label!r}. Generate it first.", False
        
        filename = secure_filename(f"gstr1_{period_label}.xlsx")
        path = os.path.join(self.EXPORT_DIR, filename)
        write_gstr1_xlsx(result, path)
        
        return path, True
    
    
    def _query_journal_entries(
        self,
        session: Session,
        date_from: date_,
        date_to: date_,
        owner_id: Optional[str] = None
    ) -> Optional[List[JournalEntryModel]]:
        query = session.query(JournalEntryModel).filter(
            JournalEntryModel.date >= date_from,
            JournalEntryModel.date <= date_to
        )
        
        if owner_id is not None:
            query = query.filter(JournalEntryModel.user_id == owner_id)
        return query.order_by(JournalEntryModel.date.asc()).all()
    
    
    def export_journal_report(
        self,
        date_from: date_,
        date_to: date_,
        owner_id: Optional[str] = None
    ) -> Tuple[str, bool]:
        def _op(session: Session) -> Optional[List[JournalEntryModel]]:
            return self._query_journal_entries(
                session=session,
                date_from=date_from,
                date_to=date_to,
                owner_id=owner_id
            )
            
        results = self.db_manager.run(_op)
        if not results:
            return f"No journal entries found for the period: {str(date_from)} to {str(date_to)}", False
        
        entries = self.rebuild_service.rebuild_journal_entries(models=results)
        
        filename = secure_filename(f"journal_{date_from.isoformat()}_{date_to.isoformat()}.xlsx")
        path = os.path.join(self.EXPORT_DIR, filename)
        write_journal_xlsx(entries, path)
        
        return path, True
    
    
    def _load_general_ledger(
        self,
        session: Session,
        as_on: date_,
        owner_id: Optional[str] = None
    ) -> Tuple[GeneralLedger, Optional[List[JournalEntry]]]:
        query = session.query(JournalEntryModel).filter(
            JournalEntryModel.date <= as_on
        )
        if owner_id is not None:
            query = query.filter(JournalEntryModel.user_id == owner_id)
        models = query.order_by(JournalEntryModel.date.asc()).all
        
        entries = self.rebuild_service.rebuild_journal_entries(models=models)
        gl, _, _ = build_ledger([], _prebuilt_entries=entries)
        
        return gl, entries
    
    
    def export_ledger_report(
        self,
        as_on: date_,
        owner_id: Optional[str] = None
    ) -> Tuple[str, bool]:
        def _op(session: Session) -> Tuple[GeneralLedger, Optional[List[JournalEntry]]]:
            return self._load_general_ledger(
                session=session,
                as_on=as_on,
                owner_id=owner_id
            )
            
        gl, entries = self.db_manager.run(_op)
        if not entries:
            return "No ledger entries found up to this date", False
        
        filename = secure_filename(f"ledger_{as_on.isoformat()}.xlsx")
        path = os.path.join(self.EXPORT_DIR, filename)
        write_ledger_xlsx(gl, path, as_on=as_on)
        
        return path, True
    
    
    def _load_tds_register(
        self,
        session: Session,
        period_start: date_, 
        period_end: date_,
        owner_id: Optional[str] = None
    ) -> TDSRegister:
        query = session.query(TDSEntryModel).filter(
            TDSEntryModel.date >= period_start,
            TDSEntryModel.date <= period_end,
        )
        if owner_id is not None:
            query = query.filter(TDSEntryModel.user_id == owner_id)
            
        models = query.order_by(TDSEntryModel.date.asc()).all()
        
        return self.rebuild_service.rebuild_tds_register(
            models=models,
            period_start=period_start,
            period_end=period_end
        )
        
    
    def export_tds_report(
        self,
        period_start: date_, 
        period_end: date_,
        owner_id: Optional[str] = None
    ) -> Tuple[str, bool]:
        def _op(session: Session) -> TDSRegister:
            return self._load_tds_register(
                session=session,
                period_start=period_start,
                period_end=period_end,
                owner_id=owner_id
            )
            
        reg = self.db_manager.run(_op)
        if not reg.entries:
            return f"No TDS entries found for {str(period_start)} to {str(period_end)} period", False
        
        filename = secure_filename(f"tds_register_{period_start.isoformat()}_{period_end.isoformat()}.xlsx")
        path = os.path.join(self.EXPORT_DIR, filename)
        write_tds_xlsx(reg, path)
        
        return path, True
    
    
    def export_bank_rec_report(
        self,
        run_id: str,
        user_id: Optional[str]
    ) -> Tuple[str, bool]:
        def _op(session: Session) -> Optional[Tuple[Dict[str, Any], List[SchemaLedger], List[SchemaBank]]]:
            return ResultBankRec._fetch_run_bundle_internal(
                session=session,
                run_id=run_id,
                user_id=user_id
            )
            
        fetched = self.db_manager.run(_op)
        if fetched is None:
            return "Reconciliation run not found or unauthorized", False
        recon_result, gl_objs, bank_objs = fetched
        
        filename = secure_filename(f"bank_rec_{run_id}.xlsx")
        path = os.path.join(self.EXPORT_DIR, filename)
        write_bank_recon_xlsx(recon_result, gl_objs, bank_objs, path)
                
        return path, True
        
        