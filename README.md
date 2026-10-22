# Db2 insert benchmark

Compares ways of inserting rows into a Db2 table (`ASRATE`-style scenario).

    pip install ibm_db
    python benchmark.py --db2 "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=u;PWD=p;" --rows 20000
    python benchmark.py --sqlite --rows 20000   # no Db2 needed; shows commit/batch effect only

Edit `DDL`, `COLS`, `make_rows()` in `benchmark.py` to match the real table.
The run also writes `asrate.csv` and `load.clp` for Db2 `IMPORT` / `LOAD`.

Rule of thumb: never commit per row; use `executemany` with `?` markers in
batches of a few thousand rows; for files use `IMPORT ... COMMITCOUNT` or `LOAD`.

## Real Db2 test: 1000 literal `INSERT INTO ASRATE ...` statements

`./run_env.sh` starts Db2 11.5.8 in Docker, then `python test_1000.py` runs the scenario
(1000 rows, best of 3, Db2 on localhost so network latency is ~0 -- remote servers gain even more
from batching).

| Strategy | Time for 1000 rows | vs. baseline |
|---|---|---|
| A. 1000 INSERT statements, commit after each (TOAD/script default) | 1.66 s | 1x |
| B. same 1000 statements, one COMMIT | 1.04 s | 1.6x |
| C. same 1000 statements in one `BEGIN ATOMIC` block | 19.6 s | **slower** |
| D. one multi-row `INSERT ... VALUES (..),(..)` | 0.043 s | 39x |
| E. prepared `?` statement in a loop, one COMMIT | 0.229 s | 7x |
| F. `execute_many` array insert, one COMMIT | **0.022 s** | **75x** |
| G. `db2 -tf inserts.sql` (CLP autocommit default) | 3.02 s | |
| H. `db2 +c -tf inserts.sql` then COMMIT | 1.86 s | |

Why: with literal statements every INSERT is parsed/compiled separately (the text differs each time),
plus a round trip and (in autocommit) a log flush. Prepared/array/multi-row inserts compile once and
send many rows per round trip with one commit. A giant compound block is worst: Db2 must compile
all 1000 statements as one section.
