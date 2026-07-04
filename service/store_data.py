from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from database.bank_renc_model import (
    LedgerSource,
    LedgerFormatModel,
    BankStatementModel,
    IgnoredMetadataRecordModel,
    AuditInvestigationItemModel,
    MatchResultModel,
)

class PushEntryPointData:
    
    @staticmethod 
    def push_bank_statements(session: Session, statements_data: List[Dict[str, Any]], run_id: Optional[int] = None) -> bool:
        """
        Takes a list of dictionaries (from the loader) and pushes them to the BankStatementModel.
        """
        try:
            db_statements = []
            for data in statements_data:
                payload = dict(data)
                if run_id is not None:
                    payload["run_id"] = run_id
                db_statements.append(BankStatementModel(**payload))
            
            session.add_all(db_statements)
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting bank statements: {e}")
            return False

    @staticmethod 
    def push_ledgers(session: Session, ledgers_data: List[Dict[str, Any]], run_id: Optional[int] = None) -> bool:
        """
        Takes a list of dictionaries (from the loader) and pushes them to the LedgerFormatModel.
        """
        try:
            db_ledgers = []
            for data in ledgers_data:
                payload = dict(data)
                if run_id is not None:
                    payload["run_id"] = run_id
                if 'source' in payload and isinstance(payload['source'], str):
                    payload['source'] = LedgerSource(payload['source'])
                    
                db_ledgers.append(LedgerFormatModel(**payload))
                
            session.add_all(db_ledgers)
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting ledgers: {e}")
            return False


    @staticmethod
    def push_all_data(session: Session, statements_data: List[Dict[str, Any]], ledgers_data: List[Dict[str, Any]], run_id: Optional[int] = None) -> bool:
        """
        Pushes both bank statements and ledgers in a single database transaction.
        If one fails, everything rolls back.
        """
        try:
            db_statements = []
            for data in statements_data:
                payload = dict(data)
                if run_id is not None:
                    payload["run_id"] = run_id
                db_statements.append(BankStatementModel(**payload))
            
            db_ledgers = []
            for data in ledgers_data:
                payload = dict(data)
                if run_id is not None:
                    payload["run_id"] = run_id
                if 'source' in payload and isinstance(payload['source'], str):
                    payload['source'] = LedgerSource(payload['source'])
                db_ledgers.append(LedgerFormatModel(**payload))
            
            # Add to session and commit together
            session.add_all(db_statements)
            session.add_all(db_ledgers)
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting reconciliation data: {e}")
            return False
        

    @staticmethod
    def push_match_results(session: Session, matches_data: List[Dict[str, Any]], run_id: Optional[int] = None) -> bool:
        """
        Pushes successfully matched records to the database.
        """
        try:
            db_matches = []
            for data in matches_data:
                payload = dict(data)
                if run_id is not None:
                    payload["run_id"] = run_id
                db_matches.append(MatchResultModel(**payload))
            session.add_all(db_matches)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting match results: {e}")
            return False


    @staticmethod
    def push_ignored_records(session: Session, ignored_data: List[Dict[str, Any]]) -> bool:
        """
        Pushes zero-amount or metadata rows that were skipped during reconciliation.
        """
        try:
            db_ignored = [IgnoredMetadataRecordModel(**data) for data in ignored_data]
            session.add_all(db_ignored)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting ignored records: {e}")
            return False


    @staticmethod
    def push_audit_items(session: Session, audit_data: List[Dict[str, Any]]) -> bool:
        """
        Pushes flagged items (e.g., bank reversals) that require manual GL entry.
        """
        try:
            db_audit_items = [AuditInvestigationItemModel(**data) for data in audit_data]
            session.add_all(db_audit_items)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting audit items: {e}")
            return False


    @staticmethod
    def push_reconciliation_results(
        session: Session, 
        matches_data: List[Dict[str, Any]], 
        ignored_data: List[Dict[str, Any]], 
        audit_data: List[Dict[str, Any]],
        run_id: Optional[int] = None,
    ) -> bool:
        """
        Pushes all post-matching outputs (matches, ignored, audit) in a single transaction.
        Highly recommended to prevent partial saves if something goes wrong.
        """
        try:
            db_matches = []
            for data in matches_data:
                payload = dict(data)
                if run_id is not None:
                    payload["run_id"] = run_id
                db_matches.append(MatchResultModel(**payload))
            
            # 2. Prepare Ignored Records
            db_ignored = [IgnoredMetadataRecordModel(**data) for data in ignored_data]
            
            # 3. Prepare Audit/Investigation Items
            db_audit = [AuditInvestigationItemModel(**data) for data in audit_data]
            
            # Add everything to the session
            session.add_all(db_matches)
            session.add_all(db_ignored)
            session.add_all(db_audit)
            
            # Commit the transaction
            session.commit()
            return True
            
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Database error while inserting complete reconciliation results: {e}")
            return False
        
