import sys, time, random, re
sys.path.insert(0, "/home/user/db2-insert-testing")
import ibm_db_dbi
from fast_install_helpers import *
DSN="DATABASE=testdb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=pass1234;"
conn=ibm_db_dbi.connect(DSN,"",""); conn.set_autocommit(False); cur=conn.cursor()
for t in ("T1","T2"):
    try: cur.execute(f"DROP TABLE {t}"); conn.commit()
    except Exception: conn.rollback()
    cur.execute(f"CREATE TABLE {t} (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(60), AMT DECIMAL(12,4), N BIGINT, D DATE, TS TIMESTAMP, NOTE VARCHAR(100))")
    conn.commit()
random.seed(5)
names=["O''Brien","a,b,(c)","semi;colon","plain","it''s (ok), fine","--not comment", ""]
def row(i, table, special=False):
    ts = "CURRENT TIMESTAMP" if special else "'2024-03-05-10.11.12.000000'"
    n = "NULL" if i%7==0 else str(random.randint(-10**9,10**9))
    return (f"INSERT INTO {table} (ID,NAME,AMT,N,D,TS,NOTE) VALUES ({i}, '{random.choice(names)}', "
            f"{random.uniform(-9999,9999):.4f}, {n}, '2024-0{1+i%9}-1{i%9}', {ts}, NULL)")
# 1) all-simple rows
Q=[row(i,"T1") for i in range(3000)]
t=time.perf_counter(); plan=plan_inserts(Q); tp=time.perf_counter()-t
print("plan:", [(s[0], len(s[2]) if s[0]=="many" else 1) for s in plan], f"parse {tp:.3f}s")
t=time.perf_counter(); run_plan(cur,plan); conn.commit(); print(f"fast path insert {time.perf_counter()-t:.3f}s")
# 2) reference: original literal rows one by one into T2
t=time.perf_counter()
for q in Q: cur.execute(q.replace("INTO T1","INTO T2"))
conn.commit(); print(f"reference one-by-one {time.perf_counter()-t:.3f}s")
cur.execute("SELECT ID,NAME,AMT,N,D,TS,NOTE FROM T1 ORDER BY ID"); a=cur.fetchall()
cur.execute("SELECT ID,NAME,AMT,N,D,TS,NOTE FROM T2 ORDER BY ID"); b=cur.fetchall()
print("T1 == T2 identical:", a==b, len(a))
# 3) fallback: one row uses CURRENT TIMESTAMP -> whole group falls back to literal batches
cur.execute("DELETE FROM T1"); conn.commit()
Q2=[row(i,"T1",special=(i==10)) for i in range(300)]
plan=plan_inserts(Q2); print("fallback plan kinds:", sorted({s[0] for s in plan}))
run_plan(cur,plan); conn.commit(); cur.execute("SELECT COUNT(*) FROM T1"); print("fallback rows:", cur.fetchone()[0])
# 4) count_rows
print("count_rows:", count_rows(cur,"SELECT ID FROM T1 WHERE ID < 100 ORDER BY ID WITH UR;"), "(expect 100)")
