"""
TDS (Tax Deducted at Source) Engine
=============================================
Indian Income Tax Act, 1961 — Chapter XVII-B

This module sits between journal.py and ledger.py.  It intercepts payments
that attract TDS, adjusts the journal entry so the net payment goes to
Cash/Bank and the TDS amount goes to TDS Payable, and maintains a full
TDS register with section-wise and deductee-wise analysis.

Architecture
------------
                 bill dict / manual_entry dict
                          │
                          ▼
              ┌───────────────────────┐
              │      TDSEngine        │  ← this file
              │  .process_bill()      │
              │  .process_manual()    │
              │  .mark_deposited()    │
              │  .get_register()      │
              │  .build_form_26q()    │
              └───────────┬───────────┘
                          │ returns
              ┌───────────┴───────────────────────┐
              │  TDSResult                        │
              │    .journal_entry  (modified)     │  → post to GeneralLedger
              │    .tds_entry      (TDSEntry)     │  → stored in TDSRegister
              └───────────────────────────────────┘

Journal entry pattern (purchase with TDS, credit to creditor)
--------------------------------------------------------------
    Purchase A/c            Dr.   [taxable]
    Input CGST A/c          Dr.   [cgst]
    Input SGST A/c          Dr.   [sgst]
      To <Vendor> A/c           Cr.   [net_payment]      ← gross − TDS
      To TDS Payable A/c        Cr.   [tds_amount]

Journal entry pattern (payment voucher — immediate cash/bank payment)
----------------------------------------------------------------------
    <Expense> A/c           Dr.   [taxable]
      To Cash/Bank A/c          Cr.   [net_payment]
      To TDS Payable A/c        Cr.   [tds_amount]

TDS Deposit entry (when challan is filed)
-----------------------------------------
    TDS Payable A/c         Dr.   [tds_amount]
      To Bank A/c               Cr.   [tds_amount]

Detection logic
---------------
TDSEngine.detect_section() scans the expense account name, narration, and
section hints in the bill/entry dict against a keyword table.  The caller
can also pass `tds_section` explicitly to bypass auto-detection.

Keyword → Section mapping (conservative — only clear-cut cases auto-detect):
  rent / office rent / building rent → 194I_b
  plant / machinery / equipment rent → 194I_a
  professional / advocate / lawyer / ca / cs / doctor → 194J_b
  technical / it service / software service / call centre → 194J_a
  contractor / sub-contractor / labour / manpower → 194C
  commission / brokerage / agent fee → 194H
  interest (non-bank) → 194A
  salary / salaries / wages → 192

Aggregate threshold tracking
-----------------------------
TDSEngine maintains a running per-deductee, per-section aggregate for the
financial year.  Once a payment causes the running total to cross the
threshold, TDS is applied from that payment onward and a warning is emitted
so earlier (non-deducted) payments can be reviewed.

Section 206AA (no PAN)
-----------------------
If deductee_pan is None or "PANNOTAVBL", the rate is raised to
max(section_rate, 20 %) automatically and rate_enhanced_206aa is set True.

Usage
-----
    from tds import TDSEngine
    from tds_schema import TDS_SECTIONS, DeducteeType

    engine = TDSEngine(financial_year="2025-26")

    # From a bill dict (auto-detected section):
    result = engine.process_bill(bill, deductee_pan="ABCDE1234F")
    if result:
        gl.post_entries([result.journal_entry])

    # Mark TDS deposited after challan is filed:
    engine.mark_deposited(
        entry_id      = result.tds_entry.entry_id,
        deposit_date  = date(2025, 8, 7),
        challan_bsr   = "0000123",
        challan_serial= "00001",
    )

    # Get full register and Form 26Q for Q1:
    register = engine.get_register()
    form     = engine.build_form_26q("Q1", date(2025, 4, 1), date(2025, 6, 30))
    print(register.to_dict())
    print(form.to_dict())
"""