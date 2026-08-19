from __future__ import annotations

from functools import wraps
import logging
import time
from typing import List, Dict, Any, Optional
from datetime import date, datetime

from sqlalchemy.exc import SQLAlchemyError


logger = logging.getLogger(__name__)


def _safe_log_value(value: Any) -> str:
    if isinstance(value, dict):
        redacted = {
            key: "[REDACTED]"
            if key.lower() in {"password", "token", "access_token", "authorization"}
            else value
            for key, value in value.items()
        }
        return repr(redacted)
    
    if isinstance(value, (list, tuple, set)):
        return f"<{type(value).__name__} len={len(value)}>"
    
    if isinstance(value, (str, int, float, bool, type(None))):
        return repr(value)
    
    return f"<{type(value).__name__}>"


def _log_call(fn):
    """Log function entry, completion time, and failures without secrets."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        logger.info(
            "Calling %s.%s args=%s kwargs=%s",
            fn.__module__,
            fn.__qualname__,
            _safe_log_value(args[1:] if args and hasattr(args[0], fn.__name__) else args),
            _safe_log_value(kwargs),
        )
        
        try:
            result = fn(*args, **kwargs)
        except Exception:
            logger.exception(
                "%s.%s failed after %.3fs",
                fn.__module__,
                fn.__qualname__,
                time.perf_counter() - started,
            )
            raise
        
        logger.info(
            "%s.%s completed in %.3fs",
            fn.__module__,
            fn.__qualname__,
            time.perf_counter() - started,
        )
        return result

    return wrapper


def _log_db_errors(action: str):
    def decorator(fn):
        @_log_call
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except SQLAlchemyError as e:
                logger.exception("Database error while %s", action)
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

