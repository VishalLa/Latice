from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE  = re.compile(r"[^a-z0-9 ]+")


def _normalize_signature(text: Optional[str]) -> str:
    if not text:
        return ""
    t = text.lower()
    t = _NON_ALNUM_RE.sub(" ", t)
    t = _WHITESPACE_RE.sub(" ", t).strip()
    return t

@dataclass
class RecognizedPattern:
    ledger_signature: str
    bank_signature:   str
    match_phase:      str   # "exact" | "fuzzy" | "ai"
    adjustment_type:  Optional[str]
    times_seen:       int = 1

    def to_dict(self) -> dict:
        return {
            "ledger_signature": self.ledger_signature,
            "bank_signature":   self.bank_signature,
            "match_phase":      self.match_phase,
            "adjustment_type":  self.adjustment_type,
            "times_seen":       self.times_seen,
        }

    @staticmethod
    def from_dict(d: dict) -> "RecognizedPattern":
        return RecognizedPattern(
            ledger_signature=d.get("ledger_signature", ""),
            bank_signature=d.get("bank_signature", ""),
            match_phase=d.get("match_phase", "fuzzy"),
            adjustment_type=d.get("adjustment_type"),
            times_seen=int(d.get("times_seen", 1)),
        )

class MemoryBackend(Protocol):
    def load(self) -> Dict[str, dict]: ...
    def save(self, data: Dict[str, dict]) -> None: ...


class JSONFileBackend:

    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: Dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

class InMemoryBackend:

    def __init__(self):
        self._data: Dict[str, dict] = {}

    def load(self) -> Dict[str, dict]:
        return dict(self._data)

    def save(self, data: Dict[str, dict]) -> None:
        self._data = dict(data)


class SQLAlchemyBackend:

    def __init__(self, session_factory, model_cls=None):
        self._session_factory = session_factory
        self._model_cls = model_cls

    def _get_model_cls(self):
        if self._model_cls is not None:
            return self._model_cls
        from database.bank_renc_model import MatchPatternModel
        self._model_cls = MatchPatternModel
        return self._model_cls

    def load(self) -> Dict[str, dict]:
        model_cls = self._get_model_cls()
        session = self._session_factory()
        try:
            rows = session.query(model_cls).all()
            return {row.pattern_key: row.to_dict() for row in rows}
        finally:
            session.close()

    def save(self, data: Dict[str, dict]) -> None:
        model_cls = self._get_model_cls()
        session = self._session_factory()
        try:
            existing = {
                row.pattern_key: row
                for row in session.query(model_cls).all()
            }
            seen_keys = set()

            for key, d in data.items():
                seen_keys.add(key)
                row = existing.get(key)
                if row is not None:
                    row.ledger_signature = d.get("ledger_signature", "")
                    row.bank_signature   = d.get("bank_signature", "")
                    row.match_phase      = d.get("match_phase", "fuzzy")
                    row.adjustment_type  = d.get("adjustment_type")
                    row.times_seen       = int(d.get("times_seen", 1))
                else:
                    session.add(model_cls(
                        pattern_key=key,
                        ledger_signature=d.get("ledger_signature", ""),
                        bank_signature=d.get("bank_signature", ""),
                        match_phase=d.get("match_phase", "fuzzy"),
                        adjustment_type=d.get("adjustment_type"),
                        times_seen=int(d.get("times_seen", 1)),
                    ))

            for key, row in existing.items():
                if key not in seen_keys:
                    session.delete(row)

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _pattern_key(ledger_sig: str, bank_sig: str) -> str:
    return f"{ledger_sig}::{bank_sig}"

class MatchMemory:
    def __init__(self, backend: Optional[MemoryBackend] = None):
        self.backend = backend or InMemoryBackend()
        self._patterns: Dict[str, RecognizedPattern] = {
            key: RecognizedPattern.from_dict(d)
            for key, d in self.backend.load().items()
        }

    def is_recognized(self, ledger_text: Optional[str], bank_text: Optional[str]) -> bool:
        key = _pattern_key(_normalize_signature(ledger_text), _normalize_signature(bank_text))
        return key in self._patterns

    def lookup(self, ledger_text: Optional[str], bank_text: Optional[str]) -> Optional[RecognizedPattern]:
        key = _pattern_key(_normalize_signature(ledger_text), _normalize_signature(bank_text))
        return self._patterns.get(key)

    def record_match(
        self,
        ledger_text:     Optional[str],
        bank_text:       Optional[str],
        match_phase:     str,
        adjustment_type: Optional[str] = None,
    ) -> None:
        ledger_sig = _normalize_signature(ledger_text)
        bank_sig   = _normalize_signature(bank_text)
        if not ledger_sig or not bank_sig:
            return  # nothing stable to key on

        key = _pattern_key(ledger_sig, bank_sig)
        existing = self._patterns.get(key)
        if existing:
            existing.times_seen += 1
            existing.match_phase     = match_phase
            existing.adjustment_type = adjustment_type
        else:
            self._patterns[key] = RecognizedPattern(
                ledger_signature=ledger_sig,
                bank_signature=bank_sig,
                match_phase=match_phase,
                adjustment_type=adjustment_type,
            )

    def record_matches_from_records(
        self,
        matches: List[dict],
        gl_by_id:   Dict[str, str],   
        bank_by_id: Dict[str, str],   
        match_phase: str,
    ) -> None:
        for m in matches:
            lid = str(m.get("ledger_id", ""))
            bid = str(m.get("bank_id", ""))
            ledger_text = gl_by_id.get(lid)
            bank_text   = bank_by_id.get(bid)
            if ledger_text is None or bank_text is None:
                continue
            self.record_match(
                ledger_text, bank_text,
                match_phase=match_phase,
                adjustment_type=m.get("adjustment_type"),
            )

    def stats(self) -> dict:
        return {
            "total_patterns": len(self._patterns),
            "recurring_patterns": sum(
                1 for p in self._patterns.values() if p.times_seen > 1
            ),
        }

    def save(self) -> None:
        self.backend.save({k: p.to_dict() for k, p in self._patterns.items()})
