import re, time, random, ibm_db_dbi
DSN="DATABASE=testdb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=pass1234;"
conn=ibm_db_dbi.connect(DSN,"",""); conn.set_autocommit(False); cur=conn.cursor()
NC=40; N=3000
cols=[f"C{i}" for i in range(NC)]
types=[("INTEGER","i"),("VARCHAR(30)","s"),("DECIMAL(18,4)","d"),("TIMESTAMP","t")]
spec=[types[i%4] for i in range(NC)]
for t in ("WIDE",):
    try: cur.execute(f"DROP TABLE {t}"); conn.commit()
    except Exception: conn.rollback()
cur.execute("CREATE TABLE WIDE (ID INTEGER NOT NULL PRIMARY KEY, "+",".join(f"{c} {t}" for c,(t,_) in zip(cols,spec))+")")
cur.execute("CREATE INDEX WIDE_I1 ON WIDE (C1)"); cur.execute("CREATE INDEX WIDE_I2 ON WIDE (C2, C0)"); conn.commit()
random.seed(3)
def val(k,i):
    return {"i":str(random.randint(0,10**6)),"s":"'text_%d_%d'"%(i,random.randint(0,999)),
            "d":"%.4f"%random.uniform(0,1e5),"t":"'2024-01-%02d-10.11.12.000000'"%(1+i%28)}[k]
PRE="INSERT INTO WIDE (ID,"+",".join(cols)+") VALUES "
queries=[PRE+"("+str(i)+","+",".join(val(k,i) for _,k in spec)+");" for i in range(N)]
queries=[q.rstrip(";") for q in queries]

# ---- user's functions, verbatim ----
def batch_insert_queries(queries, batch_size=500):
    insert_re = re.compile(r'^(INSERT\s+INTO\s+.+?\s+VALUES\s*)\((.*)\)\s*$', re.IGNORECASE | re.DOTALL)
    batched=[]; current_prefix=None; current_rows=[]
    def norm(s): return re.sub(r"\s+"," ",s.strip().upper())
    def flush():
        nonlocal current_prefix,current_rows
        if current_rows:
            for start in range(0,len(current_rows),batch_size):
                chunk=current_rows[start:start+batch_size]
                batched.append(current_prefix+",".join(f"({r})" for r in chunk))
        current_prefix=None; current_rows=[]
    for q in queries:
        m=insert_re.match(q.strip())
        if m:
            prefix,row_values=m.group(1),m.group(2)
            if current_prefix is not None and norm(current_prefix)==norm(prefix): current_rows.append(row_values)
            else: flush(); current_prefix=prefix; current_rows=[row_values]
        else: flush(); batched.append(q)
    flush(); return batched

def run(label,fn):
    cur.execute("DELETE FROM WIDE"); conn.commit()
    t=time.perf_counter(); fn(); conn.commit(); dt=time.perf_counter()-t
    cur.execute("SELECT COUNT(*) FROM WIDE"); n=cur.fetchone()[0]
    print(f"{label:46}{dt:8.2f}s  rows={n}")

t=time.perf_counter(); b500=batch_insert_queries(queries,500); print(f"regex batching itself (3000 rows): {time.perf_counter()-t:.3f}s")
print(f"{N} rows x {NC+1} cols, 2 secondary indexes")
run("A  one INSERT per row, single commit", lambda:[cur.execute(q) for q in queries])
for bs in (500,200,100,50,20):
    qs=batch_insert_queries(queries,bs)
    run(f"B  multi-row VALUES, batch={bs}", lambda qs=qs:[cur.execute(q) for q in qs])
# parameterized executemany
rows=[]
random.seed(3)
for i in range(N):
    rows.append(tuple([i]+[ (random.randint(0,10**6) if k=="i" else "text_%d_%d"%(i,random.randint(0,999)) if k=="s" else round(random.uniform(0,1e5),4) if k=="d" else "2024-01-%02d 10:11:12"%(1+i%28)) for _,k in spec]))
sql="INSERT INTO WIDE (ID,"+",".join(cols)+") VALUES ("+",".join("?"*(NC+1))+")"
def em():
    for s in range(0,N,1000): cur.executemany(sql, rows[s:s+1000])
run("C  executemany with ? markers (1000/batch)", em)

# ---- validation: fetchall vs COUNT ----
run("(reload via batch=500)", lambda:[cur.execute(q) for q in b500])
t=time.perf_counter(); cur.execute("SELECT * FROM WIDE WITH UR"); r=cur.fetchall(); t1=time.perf_counter()-t
t=time.perf_counter(); cur.execute("SELECT COUNT(*) FROM (SELECT * FROM WIDE) X WITH UR"); r2=cur.fetchone()[0]; t2=time.perf_counter()-t
print(f"validation: SELECT * + fetchall ({len(r)} rows) {t1:.3f}s   vs  COUNT(*) {t2:.3f}s")
