import ibm_db_dbi, random, sys, os, shutil
DSN="DATABASE=testdb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=pass1234;"
BAD = int(sys.argv[1]) if len(sys.argv)>1 else -1      # WR index whose expected count is wrong
c=ibm_db_dbi.connect(DSN,"",""); cu=c.cursor()
TABLES=["RATE","FXQUOTE","CURVE","SPREAD","HOLIDAY","LIMITS"]
for t in TABLES:
    try: cu.execute(f"DROP TABLE {t}"); c.commit()
    except Exception: c.rollback()
    cu.execute(f"CREATE TABLE {t} (ID INTEGER NOT NULL PRIMARY KEY, CCY VARCHAR(3), D DATE, V DECIMAL(18,6), "
               + ", ".join(f"X{i} VARCHAR(30)" for i in range(25)) + ")")
    cu.execute(f"CREATE INDEX {t}_I1 ON {t} (CCY, D)")
    c.commit()
random.seed(int(os.environ.get('SEED','9'))); ins=[]; val=[]; nid={t:0 for t in TABLES}
sizes=[int(x) for x in os.environ.get('SIZES','60,2000,3000,1500,800,2500,1200,3000,400,2000,1800,900').split(',')]
for w,n in enumerate(sizes):
    t=TABLES[w%len(TABLES)]; wr=f"WR-{2000+w}"
    ins.append(f"-- {wr}"); lo=nid[t]+1
    for _ in range(n):
        nid[t]+=1
        extra=", ".join("'"+f"v{random.randint(0,99999)}"+"'" for _ in range(25))
        ins.append(f"INSERT INTO {t} (ID, CCY, D, V, "+", ".join(f"X{i}" for i in range(25))+f") VALUES ({nid[t]}, 'USD', '2024-02-0{1+nid[t]%9}', {random.uniform(0,999):.6f}, {extra});")
    exp = n + (7 if w==BAD else 0)
    val += [f"-- {wr}", f"-- {exp} rows", f"SELECT ID FROM {t} WHERE ID BETWEEN {lo} AND {nid[t]} ORDER BY ID;", ""]
out="/tmp/claude-0/e2e2/out"; shutil.rmtree(out,ignore_errors=True); os.makedirs(out)
open(out+"/Updated_Insert_T2.sql","w").write("\n".join(ins)+"\n")
open(out+"/Validation_T2.sql","w").write("\n".join(val)+"\n")
print("rows:",sum(sizes),"WRs:",len(sizes))
