# Session brief: speeding up the Db2 WR insert installer

## Goal
The user installs "WRs" (blocks of 60-5000+ single-line `INSERT INTO <table> ... VALUES (...)` statements, each with a
validation block) into IBM Db2 with a Python script. Installs take 20-30 minutes. Make the install as fast as
possible while keeping the existing behaviour: one transaction per WR, validation after the inserts, rollback + FAILED
status on any failure, stop at the first failure, resumable via `install_status_*.json`.

## Confidentiality rule
The real table name must never appear in this repo. Always use `RATE` (or `ZZ_BENCH_*`). Do not write the real name
anywhere (code, docs, commit messages). Git history of this repo may still contain the old name; do not rewrite
history or force-push without asking the user first.

## What exists in this repo
- `install_fast.py` - the user's script with the speedups (same imports: `Utils.database.get_connection`,
  `Config.settings.OUTPUT_FOLDER`; same file naming `Updated_Insert_*.sql`, `Validation_*.sql`, `install_status_*.json`).
- `fast_install_helpers.py` - the helper functions (also inlined into `install_fast.py`).
- `bench/original_install.py` - the user's original script, used as the baseline.
- `local_test/run_local_test.py` - one-command test: starts Db2 11.5 in Docker (or uses `--dsn`), builds a fake project,
  runs original vs new (1/2/4/6 workers) on freshly generated data, plus a failure-behaviour check.
- `RUN_ME_WINDOWS.bat` - double-click wrapper that checks Python/Docker and runs the above, saving `results.txt`.
- `bench/cold_insert_benchmark.py`, `bench/make_wr_files.py`, `bench/check_tables.py`, `verify_helpers.py` - extra tests.

## What the new script does
1. Rows made only of plain literals (strings, numbers, NULL) go through ONE prepared `INSERT ... VALUES (?,?,..)` +
   `executemany` (chunk 1000, env `INSTALL_CHUNK`). Anything else (functions like CURRENT TIMESTAMP, casts,
   `'a' || 'b'`, N'..', X'..', INSERT..SELECT) falls back to literal multi-row batches of 100. If `executemany` raises,
   the WR is rolled back and retried the old way before it is declared failed.
2. Validation: all of a WR's SELECT checks are counted on the server in one round trip (`SELECT SUM(COUNT(*))`
   over wrapped subqueries) instead of `fetchall()` on every row.
3. Parallel WRs with ORDERED COMMIT (env `INSTALL_WORKERS`, default 4; 1 = old sequential): worker PROCESSES
   (spawn) do inserts+validation; the main process commits strictly in file order. A failing WR is rolled back and marked
   FAILED; every later WR is rolled back and never committed -> same status file and DB as a sequential run.
   WRs sharing a table (written by one, read/written by the other, found by regex on INSERT targets and validation
   FROM/JOIN) wait for each other.

## Verified results (Linux, Db2 11.5.8 in Docker, local, fresh unique data per run)
- 5,000 rows insert phase, median of 3: literal multi-row batch 500 = 2.9 s (text table) / 4.9 s (mixed types);
  `executemany` chunk 1000 = 0.65 s / 0.87 s.
- Whole script: 1 WR of 5,000 INSERTs 3.0 s -> 1.3 s (2.4x); 12 WRs / 19,160 rows 9.8 s -> 2.4 s with 6 workers (4.1x).
- Failure test (WR #5 fails validation): sequential and 6 workers end identically (exit 1, same status, same rows).
- Also verified: resume after failure, duplicate-key SQL error in a later WR, fallback rows (CURRENT TIMESTAMP,
  `'ab' || 'cd'`), stored data identical to row-by-row literal inserts.

## Lessons / traps (do not repeat)
- Db2 caches compiled statements by exact SQL text. "Best of 3" on identical literal SQL flatters literal-text methods.
  Always benchmark with fresh unique data per run.
- Threads are SLOWER than sequential with ibm_db (executemany holds the GIL, ~5x slower); use processes.
- Parameterized multi-row VALUES, pandas `to_sql` (1.5x slower; `method='multi'` 7x slower), one giant BEGIN ATOMIC
  block (12x slower), Spark (startup cost, no per-WR rollback) were tested/considered and rejected.
- Test harness gotcha: a `rollback()` after a failed `DROP` also undoes an uncommitted `CREATE`; commit after each DDL.
- The test Db2 container has a small log; use `TRUNCATE ... IMMEDIATE` (must be first statement in a transaction)
  instead of big DELETEs between runs.

## Open questions (the real problem is still unmeasured)
The user's real installs take 20-30 minutes while the sandbox needs ~1-3 s for the same row counts, so the insert
method is not the whole story. Likely causes: triggers on target tables, lock waits, slow remote Db2 / network latency,
slow validation queries. Next step: run `install_fast.py` against the user's real (test) environment with
`INSTALL_WORKERS=1` and read the per-WR `TIMING insert=.. validation=.. commit=..` lines, then fix whichever step dominates.
Also unknown: which driver `get_connection()` returns (`ibm_db_dbi`, `pyodbc`, `jaydebeapi`); everything was tested
with `ibm_db_dbi` only. Validation reading through views/stored procedures is not covered by the table-dependency check
(use `INSTALL_WORKERS=1` for those).

## How to run locally (Windows)
1. Install Python (tick "Add to PATH") and Docker Desktop (running), or have a Db2 test database.
2. In this folder: double-click `RUN_ME_WINDOWS.bat`, or `python local_test\run_local_test.py`
   (own Db2: add `--dsn "DATABASE=..;HOSTNAME=..;PORT=50000;PROTOCOL=TCPIP;UID=..;PWD=..;"`, only `ZZ_BENCH_*` tables are touched).
3. Output is saved to `results.txt`.

## Rules for the next session
- Only work in this repo/folder. Do not push to other repos. Ask before force-pushing or deleting history.
- Never invent results; if Docker/Db2 is unavailable, say so.
- Do not run against production data; only test/`ZZ_BENCH_*` tables.
