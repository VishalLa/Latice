from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from schema import (
    JournalEntry, 
    EntryLine,
    DrCr,
    Account,
    AccountGroup,
    COA
)

from .journal import parse_indian_date, _safe


#  ACCOUNT RESOLVER — maps name strings to Account objects
_COA_BY_NAME: dict[str, Account] = {
    a.name: a
    for attr in dir(COA)
    if isinstance((a := getattr(COA, attr)), Account)
}


# Common expense/income account names → AccountGroup mapping
_KNOWN_GROUPS: dict[str, AccountGroup] = {
    # Direct expenses
    "freight inward":      AccountGroup.DIRECT_EXPENSES,
    "carriage inward":     AccountGroup.DIRECT_EXPENSES,
    "wages":               AccountGroup.DIRECT_EXPENSES,
    "factory expenses":    AccountGroup.DIRECT_EXPENSES,
    # Indirect expenses
    "rent":                AccountGroup.INDIRECT_EXPENSES,
    "salaries":            AccountGroup.INDIRECT_EXPENSES,
    "salary":              AccountGroup.INDIRECT_EXPENSES,
    "electricity":         AccountGroup.INDIRECT_EXPENSES,
    "telephone":           AccountGroup.INDIRECT_EXPENSES,
    "internet":            AccountGroup.INDIRECT_EXPENSES,
    "insurance":           AccountGroup.INDIRECT_EXPENSES,
    "repairs":             AccountGroup.INDIRECT_EXPENSES,
    "maintenance":         AccountGroup.INDIRECT_EXPENSES,
    "stationery":          AccountGroup.INDIRECT_EXPENSES,
    "printing":            AccountGroup.INDIRECT_EXPENSES,
    "advertisement":       AccountGroup.INDIRECT_EXPENSES,
    "depreciation":        AccountGroup.INDIRECT_EXPENSES,
    "bank charges":        AccountGroup.INDIRECT_EXPENSES,
    "bank interest":       AccountGroup.INDIRECT_EXPENSES,
    "professional fees":   AccountGroup.INDIRECT_EXPENSES,
    "audit fees":          AccountGroup.INDIRECT_EXPENSES,
    "legal fees":          AccountGroup.INDIRECT_EXPENSES,
    "miscellaneous":       AccountGroup.INDIRECT_EXPENSES,
    "office expenses":     AccountGroup.INDIRECT_EXPENSES,
    "travelling":          AccountGroup.INDIRECT_EXPENSES,
    "conveyance":          AccountGroup.INDIRECT_EXPENSES,
    "postage":             AccountGroup.INDIRECT_EXPENSES,
    # Income
    "interest received":   AccountGroup.INDIRECT_INCOME,
    "commission received": AccountGroup.INDIRECT_INCOME,
    "rent received":       AccountGroup.INDIRECT_INCOME,
    # Assets
    "furniture":           AccountGroup.FIXED_ASSETS,
    "computer":            AccountGroup.FIXED_ASSETS,
    "machinery":           AccountGroup.FIXED_ASSETS,
    "vehicle":             AccountGroup.FIXED_ASSETS,
    "land":                AccountGroup.FIXED_ASSETS,
    "building":            AccountGroup.FIXED_ASSETS,
    "equipment":           AccountGroup.FIXED_ASSETS,
    # Capital
    "drawings":            AccountGroup.CAPITAL_ACCOUNT,
    "capital":             AccountGroup.CAPITAL_ACCOUNT,
    # Loans
    "loan":                AccountGroup.LOANS_LIABILITY,
    "bank loan":           AccountGroup.LOANS_LIABILITY,
    "overdraft":           AccountGroup.LOANS_LIABILITY,
}


def resolve_account(name: str) -> Account:
    # Exact match first
    if name in _COA_BY_NAME:
        return _COA_BY_NAME[name]

    # Keyword match (case-insensitive)
    name_lower = name.lower()
    for keyword, group in _KNOWN_GROUPS.items():
        if keyword in name_lower:
            return Account(name, group)

    # Fallback: create under Indirect Expenses
    return Account(name, AccountGroup.INDIRECT_EXPENSES)


# OPENING BALANCE ENTRY BUILDER
def _build_opening_entry(
    balances:     list[dict],
    period_start: date,
) -> Optional[JournalEntry]:
    if not balances:
        return None

    lines: list[EntryLine] = []
    for item in balances:
        name    = str(item.get("account", "")).strip()
        amount  = _safe(item.get("balance", 0))
        side    = str(item.get("side", "Dr")).strip().upper()
        if not name or amount <= 0:
            continue
        dr_cr   = DrCr.DEBIT if side == "DR" else DrCr.CREDIT
        account = resolve_account(name)
        lines.append(EntryLine(account, dr_cr, amount,
                                f"Opening balance — {name}"))

    if not lines:
        return None

# Calculate imbalance and absorb into Capital A/c
    total_dr = round(sum(l.amount for l in lines if l.dr_cr == DrCr.DEBIT),  2)
    total_cr = round(sum(l.amount for l in lines if l.dr_cr == DrCr.CREDIT), 2)
    diff = round(total_dr - total_cr, 2)

    if abs(diff) > 0.01:
        # If Dr > Cr → Capital is on Credit side (owner's net worth)
        # If Cr > Dr → Capital is on Debit side (technically a deficit)
        if diff > 0:
            lines.append(EntryLine(COA.CAPITAL, DrCr.CREDIT, diff, "Capital — balancing figure (opening)"))
        else:
            lines.append(EntryLine(COA.CAPITAL, DrCr.DEBIT, abs(diff), "Capital deficit — balancing figure (opening)"))

    return JournalEntry(
        date         = period_start,
        voucher_type = "Opening Entry",
        narration    = f"Opening balances as on {period_start.strftime('%d-%m-%Y')}",
        lines        = lines,
    )


# MANUAL ENTRY BUILDER
def _build_manual_entry(entry_dict: dict) -> Optional[JournalEntry]:
    raw_date     = entry_dict.get("date", "")
    voucher_type = entry_dict.get("voucher_type", "Journal Voucher")
    narration    = entry_dict.get("narration", "Manual entry")
    lines_data   = entry_dict.get("lines", [])

    entry_date = parse_indian_date(raw_date)
    lines: list[EntryLine] = []

    for ld in lines_data:
        name   = str(ld.get("account", "")).strip()
        side   = str(ld.get("dr_cr", "Dr")).strip().upper()
        amount = _safe(ld.get("amount", 0))
        if not name or amount <= 0:
            continue
        dr_cr   = DrCr.DEBIT if side in {"DR", "DEBIT"} else DrCr.CREDIT
        account = resolve_account(name)
        lines.append(EntryLine(account, dr_cr, amount, narration))

    if not lines:
        return None

    try:
        return JournalEntry(
            date         = entry_date,
            voucher_type = voucher_type,
            narration    = narration,
            lines        = lines,
        )
    except ValueError as e:
        print(f"  [opening_balances] WARNING: Manual entry '{narration}' is out of balance: {e}")
        return None


def load_opening_balances(
    json_path: Path | str,
) -> tuple[list[JournalEntry], date | None]:
    path = Path(json_path)
    if not path.exists():
        print(f"  [opening_balances] File not found: {path}")
        return [], None

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  [opening_balances] ERROR reading {path}: {e}")
        return [], None
    
    # Parse period start date
    period_start: Optional[date] = None
    raw_start = data.get("period_start", "")
    if raw_start:
        period_start = parse_indian_date(raw_start)

    entries: list[JournalEntry] = []

    # Build opening balance entry
    ob_list = data.get("opening_balances", [])
    if ob_list:
        ob_date = period_start or date.today()
        ob_entry = _build_opening_entry(ob_list, ob_date)
        if ob_entry:
            entries.append(ob_entry)
            print(f"  Opening balances loaded: {len(ob_list)} accounts")

    # Build manual entries
    manual_list = data.get("manual_entries", [])
    manual_ok   = 0
    for me_dict in manual_list:
        me = _build_manual_entry(me_dict)
        if me:
            entries.append(me)
            manual_ok += 1

    if manual_list:
        print(f"  Manual entries loaded: {manual_ok}/{len(manual_list)}")

    # Sort by date (opening entry is already at period_start, so it stays first)
    entries.sort(key=lambda e: e.date)

    return entries, period_start

