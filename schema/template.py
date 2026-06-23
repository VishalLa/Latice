from .bank_renc_schema import BankTemplate 
from typing import List, Dict


GENERIC_V1 = {
    "bank_name"        : "Generic Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Date",
    "date_format"      : "%Y-%m-%d",
    "narration_column" : "Narration",
    "debit_column"     : "Debit",
    "credit_column"    : "Credit",
    "txn_id_column"    : "Txn_ID",
    "balance_column"   : "Balance",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Date", "Txn_ID", "Narration", "Debit", "Credit", "Balance"
    }
}


AXIS_V1 = {
    "bank_name"        : "Axis Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Tran Date",
    "date_format"      : "%d-%m-%Y",
    "narration_column" : "PARTICULARS",
    "debit_column"     : "DEBIT",
    "credit_column"    : "CREDIT",
    "txn_id_column"    : "CHQNO",
    "balance_column"   : "BALANCE",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Tran Date", "PARTICULARS", "CHQNO",
        "DEBIT", "CREDIT", "BALANCE"
    }
}


HDFC_V1 = {
    "bank_name"        : "HDFC Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Date",
    "date_format"      : "%d/%m/%y",
    "narration_column" : "Narration",
    "debit_column"     : "Withdrawal Amt.",
    "credit_column"    : "Deposit Amt.",
    "txn_id_column"    : "Chq./Ref.No.",
    "balance_column"   : "Closing Balance",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Date", "Narration", "Chq./Ref.No.",
        "Value Dt", "Withdrawal Amt.",
        "Deposit Amt.", "Closing Balance"
    }
}


ICICI_V1 = {
    "bank_name"        : "ICICI Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Transaction Date",
    "date_format"      : "%d/%m/%Y",
    "narration_column" : "Transaction Remarks",
    "debit_column"     : "Withdrawal Amount (INR )",
    "credit_column"    : "Deposit Amount (INR )",
    "txn_id_column"    : "Transaction Remarks",
    "balance_column"   : "Balance (INR )",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Transaction Date", "Transaction Remarks",
        "Withdrawal Amount (INR )",
        "Deposit Amount (INR )",
        "Balance (INR )"
    }
}

IDBI_V1 = {
    "bank_name"        : "IDBI Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Date",
    "date_format"      : "%d %m %Y",
    "narration_column" : "Description",
    "debit_column"     : None,
    "credit_column"    : None,
    "txn_id_column"    : "Cheque Number",
    "balance_column"   : None,
    "type_column"      : "Type",
    "amount_column"    : "Amount",
    "fingerprint"      : {
        "Date", "Description", "Amount", "Type"
    }
}

KOTAK_V1 = {
    "bank_name"        : "Kotak Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Transaction Date",
    "date_format"      : "%d-%m-%Y",
    "narration_column" : "Description",
    "debit_column"     : "Debit",
    "credit_column"    : "Credit",
    "txn_id_column"    : "Chq/Ref Number",
    "balance_column"   : "Balance",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Transaction Date", "Description",
        "Chq/Ref Number", "Debit",
        "Credit", "Balance"
    }
}

PNB_V1 = {
    "bank_name"        : "Punjab National Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Transction Date",
    "date_format"      : "%d %m %Y",
    "narration_column" : "Narration",
    "debit_column"     : "Withdrawal",
    "credit_column"    : "Deposit",
    "txn_id_column"    : "Cheque Number",
    "balance_column"   : "Balance",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Transction Date", "Cheque Number", "Withdrawal",
        "Deposit", "Balance", "Narration"
    }
}

SBI_V1 = {
    "bank_name"        : "SBI Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Txn Date",
    "date_format"      : "%d %b %Y",
    "narration_column" : "Description",
    "debit_column"     : "Debit",
    "credit_column"    : "Credit",
    "txn_id_column"    : "Ref No./Cheque No.",
    "balance_column"   : "Balance",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Txn Date", "Value Date", "Description",
        "Ref No./Cheque No.", "Debit",
        "Credit", "Balance"
    }
}

YES_V1 = {
    "bank_name"        : "Yes Bank",
    "version"          : "v1",
    "file_type"        : "csv",
    "skip_rows"        : 0,
    "encoding"         : "utf-8",
    "date_column"      : "Transction Date",
    "date_format"      : "%Y %m %d",
    "narration_column" : "Description",
    "debit_column"     : "Debited Amount",
    "credit_column"    : "Credited Amount",
    "txn_id_column"    : "Reference No",
    "balance_column"   : "Balance",
    "type_column"      : None,
    "amount_column"    : None,
    "fingerprint"      : {
        "Reference No", "Transction Date", "Credited Amount",
        "Debited Amount", "Balance",
        "Description"
    }
}


TEMPLATE_REGISTRY: Dict[str, dict] = {
    "AXIS_V1"  : AXIS_V1,
    "HDFC_V1"  : HDFC_V1,
    "ICICI_V1" : ICICI_V1,
    "IDBI_V1"  : IDBI_V1,
    "KOTAK_V1" : KOTAK_V1,
    "PNB_V1"   : PNB_V1,
    "SBI_V1"   : SBI_V1,
    "YES_V1"   : YES_V1,
    "GENERIC_V1": GENERIC_V1
}


def get_all_templates() -> List[BankTemplate]:
    """Instantiate every registered template dict as a BankTemplate object."""
    return [BankTemplate(**cfg) for cfg in TEMPLATE_REGISTRY.values()]
