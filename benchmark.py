"""Benchmark different ways of inserting rows into a Db2 table (ASRATE).

Usage:
  # real Db2 (pip install ibm_db)
  python benchmark.py --db2 "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=user;PWD=pw;" --rows 20000

  # no Db2 handy: SQLite stand-in (shows commit/batching effect only, no network latency)
  python benchmark.py --sqlite --rows 20000

Edit COLUMNS / make_rows() to match your real ASRATE table.
"""
import argparse, csv, os, random, sqlite3, tempfile, time
from datetime import date, timedelta

# --- scenario: made-up ASRATE table; adjust to your real columns -----------
DDL = """CREATE TABLE ASRATE_BENCH (
    RATE_ID   INTEGER NOT NULL,
    CURRENCY  VARCHAR(3) NOT NULL,
    RATE_DATE DATE NOT NULL,
    RATE      DECIMAL(18,6) NOT NULL,
    SOURCE    VARCHAR(20)
)"""
TABLE = "ASRATE_BENCH"
COLS = "RATE_ID, CURRENCY, RATE_DATE, RATE, SOURCE"
NCOLS = 5


def make_rows(n):
    random.seed(42)
    cur = ["USD", "EUR", "GBP", "JPY", "INR", "CHF", "AUD", "CAD"]
    d0 = date(2020, 1, 1)
    return [(i, random.choice(cur), (d0 + timedelta(days=i % 2000)).isoformat(),
             round(random.uniform(0.5, 150), 6), random.choice(["ECB", "FED", "RBI"]))
            for i in range(n)]


def lit(v):
    return f"'{v}'" if isinstance(v, str) else str(v)


# --- insert strategies -------------------------------------------------------
def one_by_one_commit_each(conn, rows):
    """What 'Insert into asrate ...' x N in TOAD / a naive script does."""
    c = conn.cursor()
    for r in rows:
        c.execute(f"INSERT INTO {TABLE} ({COLS}) VALUES ({','.join(lit(v) for v in r)})")
        conn.commit()


def one_by_one_single_commit(conn, rows):
    c = conn.cursor()
    for r in rows:
        c.execute(f"INSERT INTO {TABLE} ({COLS}) VALUES ({','.join(lit(v) for v in r)})")
    conn.commit()


def prepared_single_commit(conn, rows):
    """Parameter markers: statement is prepared once, executed N times."""
    c = conn.cursor()
    sql = f"INSERT INTO {TABLE} ({COLS}) VALUES ({','.join(['?'] * NCOLS)})"
    for r in rows:
        c.execute(sql, r)
    conn.commit()


def executemany_batches(conn, rows, batch=5000):
    """Array/batch insert: one round trip per batch."""
    c = conn.cursor()
    sql = f"INSERT INTO {TABLE} ({COLS}) VALUES ({','.join(['?'] * NCOLS)})"
    for i in range(0, len(rows), batch):
        c.executemany(sql, rows[i:i + batch])
    conn.commit()


def multirow_values(conn, rows, batch=500):
    """INSERT ... VALUES (..),(..),(..) -- one statement carries many rows."""
    c = conn.cursor()
    for i in range(0, len(rows), batch):
        vals = ",".join("(" + ",".join(lit(v) for v in r) + ")" for r in rows[i:i + batch])
        c.execute(f"INSERT INTO {TABLE} ({COLS}) VALUES {vals}")
    conn.commit()


STRATEGIES = [
    ("1 row at a time, commit each row", one_by_one_commit_each),
    ("1 row at a time, single commit", one_by_one_single_commit),
    ("prepared stmt (?), single commit", prepared_single_commit),
    ("executemany batches of 5000", executemany_batches),
    ("multi-row VALUES, 500/stmt", multirow_values),
]


# --- connections ---------------------------------------------------------------
def connect_db2(dsn):
    import ibm_db_dbi  # pip install ibm_db
    conn = ibm_db_dbi.connect(dsn, "", "")
    conn.set_autocommit(False)
    return conn


def connect_sqlite():
    path = os.path.join(tempfile.mkdtemp(), "bench.db")
    conn = sqlite3.connect(path)
    return conn


def reset(conn, sqlite):
    c = conn.cursor()
    try:
        c.execute(f"DROP TABLE {TABLE}")
        conn.commit()
    except Exception:
        conn.rollback()
    c.execute(DDL.replace("DECIMAL(18,6)", "REAL") if sqlite else DDL)
    conn.commit()


def write_load_files(rows, outdir):
    """Fastest option for big volumes: Db2 native bulk load. Run on the Db2 side."""
    csvp = os.path.join(outdir, "asrate.csv")
    with open(csvp, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    with open(os.path.join(outdir, "load.clp"), "w") as f:
        f.write(f"""-- db2 -tvf load.clp   (after db2 connect to MYDB)
-- IMPORT: logged, fires triggers, works anywhere (fast with COMMITCOUNT)
IMPORT FROM {csvp} OF DEL COMMITCOUNT 10000 INSERT INTO {TABLE} ({COLS});
-- LOAD: minimal logging, fastest; table locked/tablespace state during load
-- LOAD FROM {csvp} OF DEL INSERT INTO {TABLE} NONRECOVERABLE;
""")
    return csvp


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--db2", help="ibm_db connection string")
    g.add_argument("--sqlite", action="store_true")
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--slow-rows", type=int, default=2000,
                    help="row cap for the slowest strategies so the test finishes")
    a = ap.parse_args()

    rows = make_rows(a.rows)
    conn = connect_sqlite() if a.sqlite else connect_db2(a.db2)
    print(f"{'strategy':38}{'rows':>8}{'secs':>9}{'rows/s':>11}")
    for name, fn in STRATEGIES:
        subset = rows[:a.slow_rows] if name.startswith("1 row at a time, commit") else rows
        reset(conn, a.sqlite)
        t = time.perf_counter()
        fn(conn, subset)
        dt = time.perf_counter() - t
        n = conn.cursor().execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] \
            if a.sqlite else len(subset)
        print(f"{name:38}{len(subset):>8}{dt:>9.2f}{len(subset)/dt:>11.0f}")
    out = os.path.dirname(os.path.abspath(__file__))
    write_load_files(rows, out)
    print(f"\nWrote asrate.csv + load.clp in {out} (Db2 IMPORT/LOAD, run with the db2 CLP).")


if __name__ == "__main__":
    main()
