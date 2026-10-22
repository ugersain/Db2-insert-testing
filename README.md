# Db2 insert benchmark

Compares ways of inserting rows into a Db2 table (`ASRATE`-style scenario).

    pip install ibm_db
    python benchmark.py --db2 "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=u;PWD=p;" --rows 20000
    python benchmark.py --sqlite --rows 20000   # no Db2 needed; shows commit/batch effect only

Edit `DDL`, `COLS`, `make_rows()` in `benchmark.py` to match the real table.
The run also writes `asrate.csv` and `load.clp` for Db2 `IMPORT` / `LOAD`.

Rule of thumb: never commit per row; use `executemany` with `?` markers in
batches of a few thousand rows; for files use `IMPORT ... COMMITCOUNT` or `LOAD`.
