"""
Bill-scanning pipeline: turns one uploaded Bill into a persisted
JournalEntry (+lines) and, if applicable, a persisted TDSEntry.
Mirrors the shape of tasks/bank_rec_task.py.

Two entry points:

  process_bill_task(bill_id)
      Runs for one bill. If the bill already has raw_extracted_data
      (e.g. pushed by an API that already ran OCR, or a test fixture),
      OCR is skipped. Otherwise it runs OCR + parse_invoice on
      bill.source_file. Then: to_journal_entry -> TDSEngine.process_bill
      -> persist JournalEntryModel/lines and TDSEntryModel.

  generate_gstr1_task(user_id, period_label, period_start, period_end)
      Rebuilds every GSTR-1 table for one filing period from all of that
      user's "output"-direction, successfully-processed bills whose
      bill_date falls in [period_start, period_end]. Idempotent — safe to
      re-run after new bills are added for the same period.
"""
from __future__ import annotations

from datetime import date as date_
from typing import Optional

from app.celery import app
from database.session import get_session
from database.ledger_tax_models import BillModel
from service import PushLedgerData, fy_label_for_date
from ledger import to_journal_entry, TDSEngine, build_gstr1


def _safe_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _normalize_bill_dict(
    bill_dict: dict, blocks=None, source_file: Optional[str] = None,
    fallback_direction: Optional[str] = None,
) -> dict:
    """
    entry_point/data_extractor.py's parse_invoice() does NOT set a
    direction/_direction key, and the two downstream consumers disagree on
    the key name: ledger/journal.py reads bill["_direction"] while
    ledger/gstr1.py reads bill["direction"]. parse_invoice() also never
    sets "_status", which to_journal_entry() requires to equal "ok".

    This function is the missing glue: it classifies direction (reusing
    entry_point.data_extractor.classify_direction/detect_type when OCR
    blocks are available), then writes BOTH direction keys and _status so
    every downstream module reads consistent values regardless of which
    key name it happens to look for.

    `fallback_direction` should be the owning BillModel.direction column
    — used when the dict itself has neither key (e.g. data written before
    this normalization existed) instead of silently defaulting to "input".
    """
    if "direction" in bill_dict or "_direction" in bill_dict:
        direction = bill_dict.get("_direction") or bill_dict.get("direction")

    elif blocks is not None:
        from entry_point.data_extractor import classify_direction, detect_type
        inv_type = detect_type(blocks)
        direction = classify_direction(bill_dict, inv_type)

    else:
        direction = fallback_direction or "input"

    bill_dict["direction"] = direction
    bill_dict["_direction"] = direction

    if "_status" not in bill_dict:
        bill_dict["_status"] = "ok" if _safe_float(bill_dict.get("grand_total")) > 0 else "failed"

    if source_file and "_source_file" not in bill_dict:
        bill_dict["_source_file"] = source_file

    return bill_dict


def _run_ocr_and_extract(image_path: str) -> dict:
    """Runs OCR + parse_invoice on a bill image, then normalizes the
    result (see _normalize_bill_dict). Only imported lazily — paddleocr is
    a heavy optional dependency and shouldn't block loading this module
    for callers that only pass in pre-extracted bill dicts."""
    from entry_point.ocr import get_ocr
    from entry_point.data_extractor import parse_invoice

    ocr = get_ocr()
    blocks = ocr.ocr_image(image_path)
    bill_dict = parse_invoice(blocks)
    return _normalize_bill_dict(bill_dict, blocks=blocks, source_file=image_path)


@app.task(bind=True, max_retries=3)
def process_bill_task(self, bill_id: str):
    """
    Process one Bill row: OCR (if needed) -> journal entry -> TDS check
    -> persist. Updates BillModel.status to "processed" or "failed".
    """
    try:
        with get_session() as session:
            bill = session.query(BillModel).filter(BillModel.id == bill_id).first()
            if bill is None:
                return {"status": "error", "message": f"Bill {bill_id} not found"}

            try:
                bill_dict = bill.raw_extracted_data
                if not bill_dict:
                    if not bill.source_file:
                        raise ValueError("Bill has neither raw_extracted_data nor source_file")
                    bill_dict = _run_ocr_and_extract(bill.source_file)
                    bill.raw_extracted_data = bill_dict
                    session.commit()
                else:
                    bill_dict = _normalize_bill_dict(bill_dict, source_file=bill.source_file, fallback_direction=bill.direction)
                    bill.raw_extracted_data = bill_dict

                # Keep BillModel's own columns in sync with what was extracted
                bill.invoice_number = bill.invoice_number or bill_dict.get("invoice_number")
                bill.vendor_name = bill.vendor_name or bill_dict.get("vendor_name")
                bill.direction = bill_dict.get("_direction", bill.direction or "input")

                journal_entry = to_journal_entry(bill_dict)
                if journal_entry is None:
                    bill.status = "failed"
                    bill.error_message = "Could not build a journal entry from this bill (bad/incomplete extraction)."
                    session.commit()
                    return {"status": "failed", "bill_id": bill_id, "reason": bill.error_message}

                tds_engine = TDSEngine(financial_year=fy_label_for_date(journal_entry.date))
                PushLedgerData.prime_tds_engine_aggregates(
                    session=session, 
                    user_id=bill.user_id,
                    deductee_name=bill_dict.get("vendor_name") or bill_dict.get("buyer_name") or "Unknown Vendor",
                    financial_year=tds_engine.financial_year, 
                    tds_engine=tds_engine,
                )

                tds_result = tds_engine.process_bill(bill_dict, journal_entry=journal_entry)
                PushLedgerData.persist_tds_engine_aggregates(
                    session=session, 
                    user_id=bill.user_id,
                    financial_year=tds_engine.financial_year, 
                    tds_engine=tds_engine,
                )

                journal_model = PushLedgerData.push_journal_entry(
                    session=session, 
                    user_id=bill.user_id,
                    journal_entry=tds_result.journal_entry, 
                    bill=bill,
                )
                if journal_model is None:
                    raise RuntimeError("Failed to persist journal entry")

                tds_model = None
                if tds_result.tds_applied and tds_result.tds_entry is not None:
                    tds_model = PushLedgerData.push_tds_entry(
                        session=session, 
                        user_id=bill.user_id,
                        tds_entry=tds_result.tds_entry, 
                        journal_entry_model=journal_model,
                    )

                bill.status = "processed"
                bill.error_message = None
                session.commit()

                return {
                    "status": "success",
                    "bill_id": bill_id,
                    "journal_entry_id": journal_model.entry_id,
                    "tds_applied": bool(tds_model),
                    "tds_entry_id": tds_model.entry_id if tds_model else None,
                    "warnings": tds_result.warnings,
                }

            except Exception as inner_exc:
                bill.status = "failed"
                bill.error_message = str(inner_exc)
                session.commit()
                raise

    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)


@app.task(bind=True, max_retries=3)
def generate_gstr1_task(
    self,
    user_id: str,
    period_label: str,
    period_start: str,   # ISO "YYYY-MM-DD"
    period_end: str,     # ISO "YYYY-MM-DD"
):
    """Rebuild GSTR-1 (B2B/B2C-Large/Nil-rated/HSN) for one user + period."""
    try:
        start = date_.fromisoformat(period_start)
        end = date_.fromisoformat(period_end)

        with get_session() as session:
            bills = (
                session.query(BillModel)
                .filter(
                    BillModel.user_id == user_id,
                    BillModel.direction == "output",
                    BillModel.status == "processed",
                    BillModel.bill_date >= start,
                    BillModel.bill_date <= end,
                )
                .all()
            )
            bill_dicts = [
                _normalize_bill_dict(dict(b.raw_extracted_data), source_file=b.source_file, fallback_direction=b.direction)
                for b in bills if b.raw_extracted_data
            ]

            gstr1 = build_gstr1(bill_dicts, period_label=period_label)

            ok = PushLedgerData.replace_gstr1_period(
                session=session, user_id=user_id, period_label=period_label, gstr1=gstr1,
            )
            return {
                "status": "success" if ok else "failed",
                "period_label": period_label,
                "bills_considered": len(bill_dicts),
                "totals": gstr1.get("totals", {}),
                "warnings": gstr1.get("warnings", []),
            }
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5)

