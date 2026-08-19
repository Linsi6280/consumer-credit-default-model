# %%
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(ROOT / "data" / "freddie_mac.db")

con.executescript((ROOT / "sql" / "derive_target.sql").read_text())
con.commit()
print("loan_target rebuilt")
