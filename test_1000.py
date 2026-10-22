"""Real-Db2 test: run 1000 literal 'INSERT INTO ASRATE ...' statements as fast as possible.

Start Db2 first (see run_env.sh), then:  python test_1000.py
"""
import os, random, re, statistics, subprocess, time
from datetime import date, timedelta
import ibm_db

DSN = "DATABASE=testdb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=pass1234;"
N = int(os.environ.get("N", 1000))
RUNS = 3
HERE = os.path.dirname(os.path.abspath(__file__))

DDL = """CREATE TABLE ASRATE (
    RATE_ID   INTEGER NOT NULL PRIMARY KEY,
    CURRENCY  VARCHAR(3) NOT NULL,
    RATE_DATE DATE NOT NULL,
    RATE      DECIMAL(18,6) NOT NULL,
    SOURCE    VARCHAR(20))"""
PREFIX = "INSERT INTO ASRATE (RATE_ID, CURRENCY, RATE_DATE, RATE, SOURCE) VALUES "


def make_rows(n):
    random.seed(1)
    d0 = date(2020, 1, 1)
    return [(i, random.choice(["USD", "EUR", "GBP", "JPY", "INR"]),
             (d0 + timedelta(days=i % 2000)).isoformat(),
             round(random.uniform(0.5, 150), 6), random.choice(["ECB", "FED", "RBI"]))
            for i in range(n)]


rows = make_rows(N)
# the user's situation: N separate statements, each "Insert into asrate ... values (...)"
inserts = [f"{PREFIX}({r[0]}, '{r[1]}', '{r[2]}', {r[3]}, '{r[4]}')" for r in rows]

conn = ibm_db.connect(DSN, "", "")
try:
    ibm_db.exec_immediate(conn, "DROP TABLE ASRATE")
except Exception:
    pass
ibm_db.exec_immediate(conn, DDL)


def truncate():
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_ON)
    ibm_db.exec_immediate(conn, "TRUNCATE TABLE ASRATE IMMEDIATE")


def count():
    s = ibm_db.exec_immediate(conn, "SELECT COUNT(*) FROM ASRATE")
    return int(ibm_db.fetch_tuple(s)[0])


# ---- strategies -----------------------------------------------------------
def a_autocommit_each():              # TOAD / naive script default: commit after every INSERT
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_ON)
    for s in inserts:
        ibm_db.exec_immediate(conn, s)


def b_one_commit():                   # same 1000 statements, one COMMIT at the end
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_OFF)
    for s in inserts:
        ibm_db.exec_immediate(conn, s)
    ibm_db.commit(conn)


def c_compound_block():               # same 1000 statements text, ONE round trip (BEGIN ATOMIC ... END)
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_OFF)
    ibm_db.exec_immediate(conn, "BEGIN ATOMIC " + "; ".join(inserts) + "; END")
    ibm_db.commit(conn)


def d_multirow_values():              # rewrite to INSERT ... VALUES (..),(..),... (one statement)
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_OFF)
    vals = ",".join(re.sub(r"^.*? VALUES ", "", s, flags=re.I) for s in inserts)
    ibm_db.exec_immediate(conn, PREFIX + vals)
    ibm_db.commit(conn)


def e_prepared_loop():                # prepare once, bind+execute 1000x, one commit
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_OFF)
    st = ibm_db.prepare(conn, PREFIX + "(?,?,?,?,?)")
    for r in rows:
        ibm_db.execute(st, r)
    ibm_db.commit(conn)


def f_execute_many():                 # array insert: prepare once, ship all rows in one call
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_OFF)
    st = ibm_db.prepare(conn, PREFIX + "(?,?,?,?,?)")
    ibm_db.execute_many(st, tuple(rows))
    ibm_db.commit(conn)


STRATS = [
    ("A  1000 INSERT stmts, autocommit (commit each)", a_autocommit_each),
    ("B  1000 INSERT stmts, single COMMIT", b_one_commit),
    ("C  same 1000 stmts in one BEGIN ATOMIC block", c_compound_block),
    ("D  one multi-row INSERT ... VALUES (..),(..)", d_multirow_values),
    ("E  prepared stmt looped, single COMMIT", e_prepared_loop),
    ("F  execute_many array insert, single COMMIT", f_execute_many),
]

print(f"Db2 11.5 in docker, table ASRATE, {N} rows, best of {RUNS}")
print(f"{'strategy':52}{'best s':>8}{'median s':>10}{'rows/s':>10}  ok")
results = {}
for name, fn in STRATS:
    times = []
    for _ in range(RUNS):
        truncate()
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
        ok = count() == N
        if not ok:
            break
    results[name] = min(times)
    print(f"{name:52}{min(times):>8.3f}{statistics.median(times):>10.3f}{N/min(times):>10.0f}  {'OK' if ok else 'COUNT MISMATCH'}")

# ---- G/H: feed a .sql file of 1000 'Insert ...;' lines to the db2 CLP inside the container ----
sql_path = os.path.join(HERE, "inserts.sql")
with open(sql_path, "w") as f:
    f.write("\n".join(s + ";" for s in inserts) + "\n")
subprocess.run(["docker", "cp", sql_path, "db2:/tmp/inserts.sql"], check=True)


def clp(flag):
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_ON)
    ibm_db.exec_immediate(conn, "TRUNCATE TABLE ASRATE IMMEDIATE")
    cmd = ("su - db2inst1 -c \"db2 -v connect to testdb >/dev/null; "
           f"START=\\$(date +%s.%N); db2 {flag} -tf /tmp/inserts.sql >/dev/null; "
           + ("db2 commit >/dev/null; " if flag == "+c" else "") +
           "END=\\$(date +%s.%N); echo \\$START \\$END\"")
    out = subprocess.run(["docker", "exec", "db2", "bash", "-c", cmd],
                         capture_output=True, text=True).stdout.split()
    return float(out[-1]) - float(out[-2])


for name, flag in [("G  db2 CLP -tf inserts.sql (autocommit default)", ""),
                   ("H  db2 CLP +c -tf inserts.sql, then COMMIT", "+c")]:
    t = min(clp(flag) for _ in range(RUNS))
    ok = count() == N
    print(f"{name:52}{t:>8.3f}{'':>10}{N/t:>10.0f}  {'OK' if ok else 'COUNT MISMATCH'}")
