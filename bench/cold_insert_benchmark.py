import sys, time, random, statistics, ibm_db, ibm_db_dbi
sys.path.insert(0,"/home/user/db2-insert-testing")
from fast_install_helpers import split_values, to_param
DSN="DATABASE=testdb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=pass1234;"
conn=ibm_db_dbi.connect(DSN,"",""); conn.set_autocommit(False); cur=conn.cursor()
raw_c=ibm_db.connect(DSN,"",""); ibm_db.autocommit(raw_c,ibm_db.SQL_AUTOCOMMIT_OFF)
N=5000; T="BENCH"
tm={"i":"INTEGER","s":"VARCHAR(30)","d":"DECIMAL(18,4)","t":"TIMESTAMP"}
def mk_table(kinds):
    conn.commit()
    try: cur.execute(f"DROP TABLE {T}"); conn.commit()
    except Exception: conn.rollback()
    cur.execute(f"CREATE TABLE {T} (ID INTEGER NOT NULL PRIMARY KEY, "+",".join(f"C{i} {tm[k]}" for i,k in enumerate(kinds))+")")
    cur.execute(f"CREATE INDEX {T}_I1 ON {T} (C1)"); conn.commit()
def lit(k,i,r):
    return {"i":str(r.randint(0,10**6)),"s":"'text_%d_%d'"%(i,r.randint(0,10**6)),"d":"%.4f"%r.uniform(0,1e5),"t":"'2024-01-%02d-10.11.12.000000'"%(1+i%28)}[k]
def data(kinds,seed):
    r=random.Random(seed); cols="ID,"+",".join(f"C{i}" for i in range(len(kinds)))
    prefix=f"INSERT INTO {T} ({cols}) VALUES "
    raw=[f"{i}, "+", ".join(lit(k,i,r) for k in kinds) for i in range(1,N+1)]
    return prefix,raw
def fresh(): conn.commit(); cur.execute(f"TRUNCATE TABLE {T} IMMEDIATE"); conn.commit()
shapes={"TEXT29 (all text columns)":["s"]*28,"MIXED41 (int/text/decimal/timestamp)":["i","s","d","t"]*10}
seed=100
for sname,kinds in shapes.items():
    mk_table(kinds); nc=len(kinds)
    res={}
    def rec(name,dt): res.setdefault(name,[]).append(dt)
    for rep in range(3):
        seed+=1
        for name,fn in [
          ("literal multi-row VALUES, batch 500 (your current)", lambda pre,raw:[cur.execute(pre+",".join(f"({r})" for r in raw[i:i+500])) for i in range(0,N,500)]),
          ("literal multi-row VALUES, batch 100", lambda pre,raw:[cur.execute(pre+",".join(f"({r})" for r in raw[i:i+100])) for i in range(0,N,100)]),
          ("executemany chunk 250",  lambda pre,raw,P=None:None),
        ]:
            pass
        # literal strategies need unique text each time: new data per strategy
        for name,b in (("literal multi-row VALUES, batch 500 (your current)",500),("literal multi-row VALUES, batch 100",100)):
            seed+=1; pre,raw=data(kinds,seed); fresh()
            t=time.perf_counter(); [cur.execute(pre+",".join(f"({r})" for r in raw[i:i+b])) for i in range(0,N,b)]; conn.commit(); rec(name,time.perf_counter()-t)
        for ch in (250,1000,2500,5000):
            seed+=1; pre,raw=data(kinds,seed); P=[[to_param(x) for x in split_values(r)] for r in raw]
            sql=pre+"("+",".join("?"*(nc+1))+")"; fresh()
            t=time.perf_counter(); [cur.executemany(sql,P[i:i+ch]) for i in range(0,N,ch)]; conn.commit(); rec(f"executemany, chunk {ch}",time.perf_counter()-t)
        seed+=1; pre,raw=data(kinds,seed); P=tuple(tuple(to_param(x) for x in split_values(r)) for r in raw)
        st=ibm_db.prepare(raw_c,pre+"("+",".join("?"*(nc+1))+")"); fresh()
        t=time.perf_counter(); ibm_db.execute_many(st,P); ibm_db.commit(raw_c); rec("ibm_db.execute_many direct (no dbi layer)",time.perf_counter()-t)
    print(sname)
    for k,v in res.items(): print(f"  {k:52}{statistics.median(v):7.3f}s  {N/statistics.median(v):8.0f} rows/s")
