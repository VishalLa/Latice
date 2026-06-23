from __future__ import annotations 

from entry_point.loader import load_bank_statement, load_ledger_csv
from schema import BankTemplate
from schema.template import TEMPLATE_REGISTRY

from matcher import reconcile


def main(
    ledger_csv_path: str,
    bank_csv_path: str,
    ledger_date_format: str = "%d-%m-%Y",
    bank_template_key: str = None,
) -> dict:
    all_warnings = []

    # Load ledger
    ledger_result = load_ledger_csv(ledger_csv_path, date_format=ledger_date_format)
    all_warnings.extend(f"[ledger] {w}" for w in ledger_result["warnings"])

    # Load bank statement (with optional template)
    bank_template = None
    if bank_template_key:
        cfg = TEMPLATE_REGISTRY.get(bank_template_key.upper())
        if not cfg:
            return {
                "error": (
                    f"Unknown bank template key '{bank_template_key}'. "
                    f"Known keys: {list(TEMPLATE_REGISTRY)}"
                )
            }
        bank_template = BankTemplate(**cfg)

    bank_result = load_bank_statement(bank_csv_path, template=bank_template)
    all_warnings.extend(f"[bank] {w}" for w in bank_result["warnings"])

    result = reconcile(
        ledger_result=ledger_result,
        bank_result=bank_result,
        all_warnings=all_warnings
    )

    return result


def print_reconciliation_results(results: dict):
    """
    Helper function to cleanly print the reconciliation results to the terminal.
    """
    print("\n" + "="*50)
    print("🏦 BANK RECONCILIATION REPORT")
    print("="*50)

    # 1. SUMMARY
    print("\n📊 SUMMARY:")
    print("-" * 30)
    summary = results.get("summary", {})
    for key, value in summary.items():
        # Format the keys to look nice (e.g., 'unreconciled_ledger' -> 'Unreconciled Ledger')
        clean_key = key.replace("_", " ").title()
        print(f"  {clean_key:<22}: {value}")

    # 2. EXACT MATCHES
    print("\n✅ EXACT MATCHES:")
    print("-" * 30)
    exact = results.get("EXACT_MATCHES", [])
    if not exact:
        print("  No exact matches found.")
    for m in exact:
        print(f"  [Ledger: {m['ledger_id']:>6}] <-> [Bank Row: {m['bank_id']:>3}] | Amount: {m['amount']:>9.2f} | Date: {m['date']}")

    # 3. FUZZY MATCHES
    print("\n⚠️  FUZZY MATCHES:")
    print("-" * 30)
    fuzzy = results.get("FUZZY_MATCHES", [])
    if not fuzzy:
        print("  No fuzzy matches found.")
    for m in fuzzy:
        lid = m['ledger_id'] if m['ledger_id'] else "None"
        print(f"  [Ledger: {lid:>6}] <-> [Bank Row: {m['bank_id']:>3}] | Type: {m['adjustment_type']} ({m['confidence_score']})")
        print(f"      -> Reason: {m['details']}")

    # 4. AI AGENT QUEUE / MATCHES
    print("\n🤖 AI AGENT LAYER:")
    print("-" * 30)
    ai_agent = results.get("AI_AGENT", [])
    if not ai_agent:
        print("  No AI Agent items/matches.")
    for m in ai_agent:
        lid = m.get('ledger_id', 'None')
        reason = m.get('details', m.get('reason', 'No details provided'))
        print(f"  [Ledger: {lid:>6}] <-> [Bank Row: {m.get('bank_id', 'N/A'):>3}] | Type: {m.get('adjustment_type', 'AI Match')}")
        print(f"      -> Reason: {reason}")

    # 5. UNRECONCILED LEDGER
    print("\n❌ UNRECONCILED LEDGER (Leftovers):")
    print("-" * 30)
    unrec_ledger = results.get("UNRECONCILED_ITEMS", {}).get("ledger", [])
    if not unrec_ledger:
        print("  All ledger items reconciled!")
    for gl in unrec_ledger:
        # Check if debit or credit to display amount correctly
        amount = gl.debit_amount if gl.debit_amount > 0 else gl.credit_amount
        txn_type = "DR (In)" if gl.debit_amount > 0 else "CR (Out)"
        print(f"  [{gl.ledger_id:>6}] Date: {str(gl.transaction_date):<10} | {txn_type} {amount:>9.2f} | Name: {gl.account_name}")

    # 6. UNRECONCILED BANK
    print("\n❌ UNRECONCILED BANK (Leftovers):")
    print("-" * 30)
    unrec_bank = results.get("UNRECONCILED_ITEMS", {}).get("bank", [])
    if not unrec_bank:
        print("  All bank items reconciled!")
    for b in unrec_bank:
        amount = b.debit if b.debit > 0 else b.credit
        txn_type = "DR (Out)" if b.debit > 0 else "CR (In)"
        print(f"  [Row: {b.row_index:>3}] Date: {str(b.date):<10} | {txn_type} {amount:>9.2f} | Narration: {b.narration[:40]}")

    # 7. WARNINGS
    warnings = results.get("warnings", [])
    if warnings:
        print("\n🚨 WARNINGS:")
        print("-" * 30)
        for w in warnings:
            print(f"  - {w}")
    
    print("\n" + "="*50 + "\n")


if __name__ == "__main__":
    results = main(
        bank_csv_path=r"Bank.csv",
        ledger_csv_path=r"Ledger.csv",
        ledger_date_format="%Y-%m-%d"
    )
    
    print_reconciliation_results(results)

