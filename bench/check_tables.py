import ibm_db_dbi, hashlib
c=ibm_db_dbi.connect("DATABASE=testdb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=pass1234;","","");cu=c.cursor()
h=hashlib.md5(); tot=0
for t in ["RATE","FXQUOTE","CURVE","SPREAD","HOLIDAY","LIMITS"]:
    cu.execute(f"SELECT COUNT(*) FROM {t}"); n=cu.fetchone()[0]; tot+=n
    cu.execute(f"SELECT * FROM {t} ORDER BY ID"); 
    for r in cu.fetchall(): h.update(repr(r).encode())
    print(f"{t}:{n}",end=" ")
print("\nTOTAL",tot,"HASH",h.hexdigest())
