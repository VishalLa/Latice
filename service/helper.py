from __future__ import annotations

from functools import wraps
from typing import List, Dict, Any, Optional
from datetime import date, datetime

from sqlalchemy.exc import SQLAlchemyError


def _log_db_errors(action: str):
    def decorator(fn):
        @wraps
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except SQLAlchemyError as e:
                print(f"Database error while {action}: {e}")
                raise

        return wrapper
    return decorator


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _coerce_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    
    if isinstance(value, datetime):
        return value.date()
    
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None

    return None


def _coerce_row_dates(
    data: Dict[str, Any], 
    date_fields: List[str]
) -> Dict[str, Any]:
    out = dict(data)
    for f in date_fields:
        if f in out:
            out[f] = _coerce_date(out[f])
    return out


def fy_label_for_date(d: date) -> str:
    year = d.year if d.month >= 4 else d.year - 1
    return f"{year}-{str(year+1)[-2:]}"

