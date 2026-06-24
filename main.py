from __future__ import annotations

import sys
from entry_point.loader import load_bank_statement, load_ledger_csv
from matcher import reconcile


def print_reconciliation_results(results: dict) -> None:
    """Safely prints reconciliation results to the terminal with strict alignment."""
    try:
        print("\n" + "=" * 60)
        print("🏦 BANK RECONCILIATION REPORT")
        print("=" * 60)

        # ── SUMMARY ───────────────────────────────────────────────────────
        summary = results.get("summary", {})
        print("\n📊 SUMMARY:")
        print("-" * 40)
        for key, val in summary.items():
            label = key.replace("_", " ").title()
            print(f"  {label:<26}: {val}")

        # ── EXACT MATCHES ─────────────────────────────────────────────────
        print("\n✅ EXACT MATCHES:")
        print("-" * 40)
        exact = results.get("EXACT_MATCHES", [])
        if not exact:
            print("  No exact matches found.")
        for m in exact:
            lid = m.get("ledger_id", "N/A")
            bid = str(m.get("bank_id", "N/A"))
            amt = float(m.get("amount", 0.0))
            dte = m.get("date", "N/A")
            ref = "✓ Ref" if m.get("reference_matched") else "  Amt"
            print(f"  [{ref}] [Ledger: {lid:>6}] <-> [Bank Row: {bid:>3}] | Amount: {amt:>12,.2f} | Date: {dte}")

        # ── FUZZY MATCHES ─────────────────────────────────────────────────
        print("\n⚠️  FUZZY MATCHES:")
        print("-" * 40)
        fuzzy = results.get("FUZZY_MATCHES", [])
        if not fuzzy:
            print("  No fuzzy matches found.")
        for m in fuzzy:
            lid    = str(m.get("ledger_id", "None"))
            bid    = str(m.get("bank_id", "N/A"))
            atype  = m.get("adjustment_type", "Unknown")
            conf   = m.get("confidence_score", "N/A")
            detail = m.get("details", "")
            print(f"  [Ledger: {lid:>6}] <-> [Bank Row: {bid:>3}] | Type: {atype} ({conf})")
            if detail:
                print(f"      -> Reason: {detail}")

        # ── AI NEW MATCHES (residuals the AI found) ───────────────────────
        print("\n🤖 AI AGENT — New Matches:")
        print("-" * 40)

        ai_1to1 = results.get("AI_MATCHES", [])
        ai_many = [m for m in (results.get("AI_AGENT", []) or []) if "ledger_ids" in m]

        if not ai_1to1 and not ai_many:
            unr_l = results.get("UNRECONCILED_ITEMS", {}).get("ledger", [])
            unr_b = results.get("UNRECONCILED_ITEMS", {}).get("bank",   [])
            if not unr_l and not unr_b:
                print("  ℹ️  Exact + Fuzzy matchers reconciled everything.")
                print("     AI matcher had no residuals to process.")
            else:
                print("  ⚠️  AI matcher ran but found no additional matches.")
        else:
            if ai_1to1:
                print(f"  — 1-to-1 Semantic Matches ({len(ai_1to1)}) —")
            for m in ai_1to1:
                lid  = str(m.get("ledger_id", "N/A"))
                bid  = str(m.get("bank_id",   "N/A"))
                conf = m.get("confidence", 0.0)
                rsn  = m.get("reasoning", "")
                print(f"  [Ledger: {lid:>6}] <-> [Bank Row: {bid:>3}] | Conf: {conf:.0%}")
                if rsn:
                    print(f"      -> {rsn}")

            if ai_many:
                print(f"\n  — 1-to-Many Matches ({len(ai_many)}) —")
            for m in ai_many:
                bid      = str(m.get("bank_id", "N/A"))
                lids_raw = m.get("ledger_ids", [])
                if lids_raw and isinstance(lids_raw[0], dict):
                    lids = ", ".join(x.get("ledger_id", "?") for x in lids_raw)
                else:
                    lids = ", ".join(str(x) for x in lids_raw)
                conf = m.get("confidence", 0.0)
                rsn  = m.get("reasoning", "")
                print(f"  [Ledgers: {lids}] <-> [Bank Row: {bid:>3}] | Conf: {conf:.0%}")
                if rsn:
                    print(f"      -> {rsn}")

        # ── UNRECONCILED LEDGER ───────────────────────────────────────────
        print("\n❌ UNRECONCILED LEDGER (Leftovers):")
        print("-" * 40)
        unrec_ledger = results.get("UNRECONCILED_ITEMS", {}).get("ledger", [])
        if not unrec_ledger:
            print("  ✅ All ledger items reconciled!")
        for gl in unrec_ledger:
            debit  = getattr(gl, "debit_amount",  0.0)
            credit = getattr(gl, "credit_amount", 0.0)
            amt      = debit if debit > 0 else (credit if credit > 0 else 0.0)
            txn_type = "DR (Out)" if debit > 0 else ("CR (In)" if credit > 0 else "N/A")
            dte      = str(getattr(gl, "transaction_date", "N/A"))
            name     = getattr(gl, "account_name", "N/A")
            lid      = getattr(gl, "ledger_id", "N/A")
            print(f"  [{lid:>6}] Date: {dte:<10} | {txn_type:<8} {amt:>12,.2f} | Name: {name}")

        # ── UNRECONCILED BANK ─────────────────────────────────────────────
        print("\n❌ UNRECONCILED BANK (Leftovers):")
        print("-" * 40)
        unrec_bank = results.get("UNRECONCILED_ITEMS", {}).get("bank", [])
        if not unrec_bank:
            print("  ✅ All bank items reconciled!")
        for b in unrec_bank:
            debit  = getattr(b, "debit",  0.0)
            credit = getattr(b, "credit", 0.0)
            amt      = debit if debit > 0 else (credit if credit > 0 else 0.0)
            txn_type = "DR (Out)" if debit > 0 else ("CR (In)" if credit > 0 else "N/A")
            dte      = str(getattr(b, "date", "N/A"))
            narr     = (getattr(b, "narration", "") or "N/A")[:45]
            bid      = getattr(b, "row_index", "N/A")
            print(f"  [Row: {bid:>3}] Date: {dte:<10} | {txn_type:<8} {amt:>12,.2f} | Narration: {narr}")

        # ── WARNINGS ──────────────────────────────────────────────────────
        warnings = results.get("warnings", [])
        if warnings:
            print("\n🚨 WARNINGS:")
            print("-" * 40)
            for w in warnings:
                print(f"  - {w}")

        print("\n" + "=" * 60 + "\n")

    except Exception as e:
        print(f"\n❌ Error printing reconciliation results: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    bank_statement_result = load_bank_statement(filepath=r"Bank.csv")
    ledger_result = load_ledger_csv(filepath=r"Ledger.csv")

    results = reconcile(
        ledger_result=ledger_result,
        bank_result=bank_statement_result,
        all_warnings=(
            ledger_result.get("warnings", []) +
            bank_statement_result.get("warnings", [])
        ),
    )

    print_reconciliation_results(results)
    