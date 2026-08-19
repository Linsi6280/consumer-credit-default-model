"""
Load the Freddie Mac Single-Family Loan-Level Dataset (2006 sample vintage)
into a local SQLite database.

Field layout verified against Freddie Mac's Single-Family Loan-Level Dataset
General User Guide (Jan 2026) and cross-checked against the actual sample
files:
  - Origination file: columns 1-25 match the official layout exactly.
    Columns 26-31 (Super Conforming Flag ... Interest Only Indicator) are
    constant/sentinel values for every loan in this 2006 vintage (HARP,
    super-conforming, and post-2015 program flags did not exist yet), so
    their exact legacy semantics don't affect modeling - they carry zero
    variance and are kept only for completeness.
  - Performance file: columns 1-32 match the official layout exactly
    (confirmed field-by-field, including the ELTV=999 "unknown" sentinel
    for pre-2017 periods). Columns 33-35 are extra fields appended in this
    sample export beyond the documented 32-column layout: a constant
    reserved code, the servicer name as of that reporting period, and a
    sparsely-populated dollar amount. They are kept as-is but unused.
"""
import re
import sqlite3
import csv
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "freddie_mac.db"

# Every vintage found in data/raw, e.g. {2006: (orig_path, perf_path), 2013: (...), ...}
VINTAGES = {
    int(m.group(1)): (
        RAW_DIR / f"sample_orig_{m.group(1)}.txt",
        RAW_DIR / f"sample_perf_{m.group(1)}.txt",
    )
    for f in RAW_DIR.glob("sample_orig_*.txt")
    if (m := re.match(r"sample_orig_(\d{4})\.txt", f.name))
}

ORIGINATION_COLUMNS = [
    "credit_score",
    "first_payment_date",
    "first_time_homebuyer_flag",
    "maturity_date",
    "msa",
    "mi_pct",
    "number_of_units",
    "occupancy_status",
    "cltv",
    "dti",
    "original_upb",
    "ltv",
    "original_interest_rate",
    "channel",
    "ppm_flag",
    "amortization_type",
    "property_state",
    "property_type",
    "postal_code",
    "loan_sequence_number",
    "loan_purpose",
    "original_loan_term",
    "number_of_borrowers",
    "seller_name",
    "servicer_name",
    "super_conforming_flag",
    "pre_relief_refinance_loan_sequence_number",
    "special_eligibility_program",
    "relief_refinance_indicator",
    "property_valuation_method",
    "interest_only_indicator",
]

PERFORMANCE_COLUMNS = [
    "loan_sequence_number",
    "monthly_reporting_period",
    "current_actual_upb",
    "current_loan_delinquency_status",
    "loan_age",
    "remaining_months_to_maturity",
    "defect_settlement_date",
    "modification_flag",
    "zero_balance_code",
    "zero_balance_effective_date",
    "current_interest_rate",
    "current_non_interest_bearing_upb",
    "ddlpi",
    "mi_recoveries",
    "net_sale_proceeds",
    "non_mi_recoveries",
    "total_expenses",
    "legal_costs",
    "maintenance_preservation_costs",
    "taxes_insurance",
    "misc_expenses",
    "actual_loss_calculation",
    "cumulative_modification_cost",
    "interest_rate_step_indicator",
    "payment_deferral_flag",
    "eltv",
    "zero_balance_removal_upb",
    "delinquent_accrued_interest",
    "delinquency_due_to_disaster",
    "borrower_assistance_status_code",
    "current_month_modification_cost",
    "interest_bearing_upb",
    "reserved_flag",
    "servicer_name_as_of_period",
    "misc_amount",
]

ORIGINATION_SCHEMA = f"""
CREATE TABLE loan_origination (
    vintage_year INTEGER,
    credit_score INTEGER,
    first_payment_date INTEGER,
    first_time_homebuyer_flag TEXT,
    maturity_date INTEGER,
    msa INTEGER,
    mi_pct INTEGER,
    number_of_units INTEGER,
    occupancy_status TEXT,
    cltv INTEGER,
    dti INTEGER,
    original_upb INTEGER,
    ltv INTEGER,
    original_interest_rate REAL,
    channel TEXT,
    ppm_flag TEXT,
    amortization_type TEXT,
    property_state TEXT,
    property_type TEXT,
    postal_code TEXT,
    loan_sequence_number TEXT PRIMARY KEY,
    loan_purpose TEXT,
    original_loan_term INTEGER,
    number_of_borrowers INTEGER,
    seller_name TEXT,
    servicer_name TEXT,
    super_conforming_flag TEXT,
    pre_relief_refinance_loan_sequence_number TEXT,
    special_eligibility_program TEXT,
    relief_refinance_indicator TEXT,
    property_valuation_method TEXT,
    interest_only_indicator TEXT
);
"""

PERFORMANCE_SCHEMA = """
CREATE TABLE loan_performance (
    vintage_year INTEGER,
    loan_sequence_number TEXT,
    monthly_reporting_period INTEGER,
    current_actual_upb REAL,
    current_loan_delinquency_status TEXT,
    loan_age INTEGER,
    remaining_months_to_maturity INTEGER,
    defect_settlement_date INTEGER,
    modification_flag TEXT,
    zero_balance_code TEXT,
    zero_balance_effective_date INTEGER,
    current_interest_rate REAL,
    current_non_interest_bearing_upb REAL,
    ddlpi INTEGER,
    mi_recoveries REAL,
    net_sale_proceeds TEXT,
    non_mi_recoveries REAL,
    total_expenses REAL,
    legal_costs REAL,
    maintenance_preservation_costs REAL,
    taxes_insurance REAL,
    misc_expenses REAL,
    actual_loss_calculation REAL,
    cumulative_modification_cost REAL,
    interest_rate_step_indicator TEXT,
    payment_deferral_flag TEXT,
    eltv INTEGER,
    zero_balance_removal_upb REAL,
    delinquent_accrued_interest REAL,
    delinquency_due_to_disaster TEXT,
    borrower_assistance_status_code TEXT,
    current_month_modification_cost REAL,
    interest_bearing_upb REAL,
    reserved_flag TEXT,
    servicer_name_as_of_period TEXT,
    misc_amount REAL,
    FOREIGN KEY (loan_sequence_number) REFERENCES loan_origination(loan_sequence_number)
);
"""

INT_FIELDS_ORIG = {
    "credit_score", "first_payment_date", "maturity_date", "msa", "mi_pct",
    "number_of_units", "cltv", "dti", "original_upb", "ltv",
    "original_loan_term", "number_of_borrowers",
}
FLOAT_FIELDS_ORIG = {"original_interest_rate"}

INT_FIELDS_PERF = {
    "monthly_reporting_period", "loan_age", "remaining_months_to_maturity",
    "defect_settlement_date", "zero_balance_effective_date", "ddlpi", "eltv",
}
FLOAT_FIELDS_PERF = {
    "current_actual_upb", "current_interest_rate",
    "current_non_interest_bearing_upb", "mi_recoveries", "non_mi_recoveries",
    "total_expenses", "legal_costs", "maintenance_preservation_costs",
    "taxes_insurance", "misc_expenses", "actual_loss_calculation",
    "cumulative_modification_cost", "zero_balance_removal_upb",
    "delinquent_accrued_interest", "current_month_modification_cost",
    "interest_bearing_upb", "misc_amount",
}


def _coerce_row(row, columns, int_fields, float_fields):
    out = []
    for col, val in zip(columns, row):
        val = val.strip()
        if val == "":
            out.append(None)
        elif col in int_fields:
            out.append(int(val))
        elif col in float_fields:
            out.append(float(val))
        else:
            out.append(val)
    return out


def load_origination(conn, vintage_year, orig_file):
    print(f"Loading origination file: {orig_file}")
    cur = conn.cursor()

    insert_sql = (
        f"INSERT INTO loan_origination (vintage_year, {', '.join(ORIGINATION_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in range(len(ORIGINATION_COLUMNS) + 1))})"
    )

    batch, n = [], 0
    with open(orig_file, newline="") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            batch.append([vintage_year] + _coerce_row(row, ORIGINATION_COLUMNS, INT_FIELDS_ORIG, FLOAT_FIELDS_ORIG))
            if len(batch) >= 5000:
                cur.executemany(insert_sql, batch)
                n += len(batch)
                batch = []
        if batch:
            cur.executemany(insert_sql, batch)
            n += len(batch)
    conn.commit()
    print(f"  Inserted {n:,} origination rows")


def load_performance(conn, vintage_year, perf_file):
    print(f"Loading performance file: {perf_file} (this takes a few minutes)")
    cur = conn.cursor()

    insert_sql = (
        f"INSERT INTO loan_performance (vintage_year, {', '.join(PERFORMANCE_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in range(len(PERFORMANCE_COLUMNS) + 1))})"
    )

    batch, n = [], 0
    t0 = time.time()
    with open(perf_file, newline="") as f:
        reader = csv.reader(f, delimiter="|")
        for row in reader:
            batch.append([vintage_year] + _coerce_row(row, PERFORMANCE_COLUMNS, INT_FIELDS_PERF, FLOAT_FIELDS_PERF))
            if len(batch) >= 20000:
                cur.executemany(insert_sql, batch)
                n += len(batch)
                batch = []
                if n % 500000 < 20000:
                    print(f"  ...{n:,} rows ({time.time()-t0:.0f}s)")
        if batch:
            cur.executemany(insert_sql, batch)
            n += len(batch)
    conn.commit()
    print(f"  Inserted {n:,} performance rows in {time.time()-t0:.0f}s")


def build_indexes(conn):
    print("Building indexes...")
    cur = conn.cursor()
    cur.execute("CREATE INDEX idx_perf_lsn ON loan_performance(loan_sequence_number);")
    cur.execute("CREATE INDEX idx_perf_lsn_age ON loan_performance(loan_sequence_number, loan_age);")
    conn.commit()
    print("  Done")


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = OFF;")
    conn.execute("PRAGMA synchronous = OFF;")

    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS loan_origination;")
    cur.execute(ORIGINATION_SCHEMA)
    cur.execute("DROP TABLE IF EXISTS loan_performance;")
    cur.execute(PERFORMANCE_SCHEMA)
    conn.commit()

    for vintage_year, (orig_file, perf_file) in sorted(VINTAGES.items()):
        print(f"\n=== Vintage {vintage_year} ===")
        load_origination(conn, vintage_year, orig_file)
        load_performance(conn, vintage_year, perf_file)

    build_indexes(conn)

    conn.close()
    print(f"\nDatabase built at {DB_PATH}")


if __name__ == "__main__":
    main()
