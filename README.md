# Db2 insert benchmark

Compares ways of inserting rows into a Db2 table (`RATE`-style scenario).

    pip install ibm_db
    python benchmark.py --db2 "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=u;PWD=p;" --rows 20000
    python benchmark.py --sqlite --rows 20000   # no Db2 needed; shows commit/batch effect only

Edit `DDL`, `COLS`, `make_rows()` in `benchmark.py` to match the real table.
The run also writes `rate.csv` and `load.clp` for Db2 `IMPORT` / `LOAD`.

Rule of thumb: never commit per row; use `executemany` with `?` markers in
batches of a few thousand rows; for files use `IMPORT ... COMMITCOUNT` or `LOAD`.

## IMPORTANT: benchmark methodology (corrected)

Db2 caches compiled statements by their exact SQL text. Repeating a run with identical literal
`INSERT` text (or "best of 3" on the same text) skips compilation on runs 2-3 and flatters every
literal-text method. A real install executes each statement once, so all numbers below use
**fresh, unique data for every run** (median of 3). The early numbers in this repo's history
(`test_1000.py`, e.g. "multi-row VALUES 39x") were measured the flattering way and overstate
literal-text methods; `executemany` with `?` markers is faster than they suggested.

## Results (Db2 11.5 in Docker, cold/unique statements)

### 5,000 rows, insert phase only (`bench/cold_insert_benchmark.py`)

| Method | 29-col text table | 41-col mixed table |
|---|---|---|
| literal multi-row `VALUES`, batch 500 (previous script) | 2.87 s | 4.89 s |
| literal multi-row `VALUES`, batch 100 | 2.10 s | 3.07 s |
| `executemany` with `?`, chunk 1000 | **0.65 s** | **0.87 s** |
| `ibm_db.execute_many` called directly | 0.89 s | 0.91 s |

### Whole script, same input files (`bench/original_install.py` vs `install_fast.py`)

| Scenario | original | new, 1 worker | new, 4 workers | new, 6 workers |
|---|---|---|---|---|
| 1 WR with 5,000 INSERTs | 2.9 s | 1.3 s | | |
| 12 WRs, 19,160 rows | 11.4 s | 5.0 s | 3.2 s | 2.8 s |

(Local Db2, so no network latency; remote servers gain more from fewer round trips.)

## What `install_fast.py` does

Same flow as the original script (WR blocks, validation, status file, per-WR commit/rollback), plus:

1. **`executemany` with `?` markers** for rows made only of plain literals (strings, numbers, NULL).
   Anything else (`CURRENT TIMESTAMP`, casts, `'a' || 'b'`, `INSERT ... SELECT`) falls back to literal
   multi-row batches of 100. If `executemany` raises, the WR is rolled back and retried the old way
   before being declared failed.
2. **Validation in one round trip**: all of a WR's checks run as one `SELECT SUM(COUNT)` on the server
   instead of `fetchall()` on every row.
3. **Parallel WRs, ordered commit** (`INSTALL_WORKERS`, default 4; 1 = old sequential behaviour).
   WRs run in worker *processes* (threads are slower than sequential with ibm_db: the driver holds the
   GIL), but commit strictly in file order. A failing WR is rolled back and marked FAILED, every later
   WR is rolled back and never committed - identical status file and database to a sequential run
   (tested: validation mismatch, duplicate-key SQL error, resume after failure).
   WRs that write to or read from the same table (found by parsing the INSERT targets and the
   validation `FROM`/`JOIN` tables) wait for each other.
4. `TIMING insert=.. validation=..` per WR, `INSTALL_CHUNK` (rows per `executemany`, default 1000).

Caveats: validation that reads through views or stored procedures is not seen by the table-dependency
check - use `INSTALL_WORKERS=1` for those. Workers use `spawn`, so the script must be run as a file
(it has the `if __name__ == "__main__"` guard). Tested with `ibm_db_dbi`; other drivers need a quick
check of `executemany` and `autocommit`.

## Run the whole comparison on your own machine (one command)

    git clone <this repo> && cd <this repo>
    python local_test/run_local_test.py                 # starts Db2 in Docker (first run downloads ~3 GB)
    python local_test/run_local_test.py --dsn "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=u;PWD=p;"

With `--dsn` it uses your Db2 and only creates/drops tables named `ZZ_BENCH_*`. It installs `ibm_db`,
builds a fake project (`Utils/database.py`, `Config/settings.py`, WR files), runs the original script and
`install_fast.py` (1/2/4/6 workers) on identical-size, freshly generated data, and checks that a failing WR
ends identically in sequential and parallel mode. Docker needs `--privileged` (Docker Desktop on
Windows/Mac works). Reference run (Db2 database activated, so connect/close overhead is small): 5,000-query WR ~1.7 s -> 0.85 s (2.0x); 12 WRs 5.2 s -> 1.4 s with 6 workers (3.6x). Numbers vary with the machine; the ratio is what matters. Tip for local Docker tests: run `db2 activate db testdb` in the container, otherwise every connect/close costs ~1 s.

### Windows: one click
Download the repo as a ZIP (or clone it), unzip into e.g. `C:\Users\<you>\Desktop\DBTEST`, and double-click
`RUN_ME_WINDOWS.bat`. It checks Python and Docker, installs `ibm_db`, starts Db2, runs every scenario and
saves the output to `results.txt`. For your own Db2 run it from a terminal:
`RUN_ME_WINDOWS.bat "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=user;PWD=pw;"`.
