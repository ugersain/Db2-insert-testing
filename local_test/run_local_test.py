#!/usr/bin/env python3
"""One-command local test: original script vs install_fast.py against a real Db2.

  python local_test/run_local_test.py                 # starts Db2 in Docker (first run downloads ~3 GB)
  python local_test/run_local_test.py --dsn "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=u;PWD=p;"
                                                      # use an existing Db2 (only creates/drops tables named ZZ_BENCH_*)
Options: --rows-scale 1.0 (multiply WR sizes)  --keep-container  --skip-failure-test
Needs: Python 3.9+, pip (installs ibm_db), Docker (only when --dsn is not given).
"""
import argparse, json, os, random, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONTAINER = "db2-insert-test"
DOCKER_DSN = "DATABASE=testdb;HOSTNAME=localhost;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=pass1234;"
TABLES = [f"ZZ_BENCH_T{i}" for i in range(1, 7)]
SIZES = [60, 2000, 3000, 1500, 800, 2500, 1200, 3000, 400, 2000, 1800, 900]
NX = 25


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, **kw)


def ensure_driver():
    try:
        import ibm_db_dbi  # noqa
    except ImportError:
        print("Installing ibm_db ...")
        run([sys.executable, "-m", "pip", "install", "ibm_db"], check=True)


def connect(dsn):
    import ibm_db_dbi
    return ibm_db_dbi.connect(dsn, "", "")


def wait_for_db(dsn, minutes=20):
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            connect(dsn).close()
            return
        except Exception:
            time.sleep(10)
    sys.exit("Db2 did not become ready in time")


def start_docker():
    if not shutil.which("docker"):
        sys.exit("Docker not found. Install Docker Desktop, or pass --dsn to use an existing Db2.")
    out = run(["docker", "ps", "-a", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Status}}"],
              capture_output=True).stdout.strip()
    if not out:
        print("Starting Db2 container (first start takes several minutes) ...")
        run(["docker", "run", "-d", "--name", CONTAINER, "--privileged=true", "-p", "50000:50000",
             "-e", "LICENSE=accept", "-e", "DB2INST1_PASSWORD=pass1234", "-e", "DBNAME=testdb",
             "-e", "AUTOCONFIG=false", "-e", "ARCHIVE_LOGS=false", "ibmcom/db2:11.5.8.0"], check=True)
    elif not out.startswith("Up"):
        run(["docker", "start", CONTAINER], check=True)
    wait_for_db(DOCKER_DSN)
    print("Db2 is ready.")


def reset_tables(dsn):
    c = connect(dsn)
    cur = c.cursor()
    for t in TABLES:
        try:
            cur.execute(f"DROP TABLE {t}")
            c.commit()
        except Exception:
            c.rollback()
        cur.execute(f"CREATE TABLE {t} (ID INTEGER NOT NULL PRIMARY KEY, CCY VARCHAR(3), D DATE, "
                    f"V DECIMAL(18,6), " + ", ".join(f"X{i} VARCHAR(30)" for i in range(NX)) + ")")
        cur.execute(f"CREATE INDEX {t}_I1 ON {t} (CCY, D)")
        c.commit()
    c.close()


def count_rows(dsn):
    c = connect(dsn)
    cur = c.cursor()
    n = 0
    for t in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        n += cur.fetchone()[0]
    c.close()
    return n


def make_files(out, sizes, seed, bad=-1):
    """Updated_Insert_T.sql / Validation_T.sql in the same shape as the real ones (unique text per seed)."""
    rnd = random.Random(seed)
    nid = {t: 0 for t in TABLES}
    ins, val = [], []
    for w, n in enumerate(sizes):
        t = TABLES[w % len(TABLES)]
        wr = f"WR-{2000 + w}"
        ins.append(f"-- {wr}")
        lo = nid[t] + 1
        for _ in range(n):
            nid[t] += 1
            extra = ", ".join("'v%d'" % rnd.randint(0, 10**6) for _ in range(NX))
            ins.append(f"INSERT INTO {t} (ID, CCY, D, V, " + ", ".join(f"X{i}" for i in range(NX)) +
                       f") VALUES ({nid[t]}, 'USD', '2024-02-0{1 + nid[t] % 9}', {rnd.uniform(0, 999):.6f}, {extra});")
        exp = n + (7 if w == bad else 0)
        val += [f"-- {wr}", f"-- {exp} rows",
                f"SELECT ID FROM {t} WHERE ID BETWEEN {lo} AND {nid[t]} ORDER BY ID;", ""]
    out.mkdir(parents=True, exist_ok=True)
    (out / "Updated_Insert_T.sql").write_text("\n".join(ins) + "\n", encoding="utf-8")
    (out / "Validation_T.sql").write_text("\n".join(val) + "\n", encoding="utf-8")


def scaffold(work):
    proj = work / "proj"
    for d in ("Scripts", "Utils", "Config"):
        (proj / d).mkdir(parents=True, exist_ok=True)
    for d in ("Utils", "Config"):
        (proj / d / "__init__.py").write_text("")
    (proj / "Utils" / "database.py").write_text(
        "import os, ibm_db_dbi\n"
        "def get_connection():\n"
        "    return ibm_db_dbi.connect(os.environ['BENCH_DSN'], '', '')\n")
    (proj / "Config" / "settings.py").write_text(
        "import os\nfrom pathlib import Path\nOUTPUT_FOLDER = Path(os.environ['BENCH_OUT'])\n")
    shutil.copy(REPO / "bench" / "original_install.py", proj / "Scripts" / "original_install.py")
    shutil.copy(REPO / "install_fast.py", proj / "Scripts" / "install_fast.py")
    return proj


def timed_run(proj, script, workers, dsn, out, log):
    status = out / "install_status_T.json"
    if status.exists():
        status.unlink()
    env = dict(os.environ, BENCH_DSN=dsn, BENCH_OUT=str(out), INSTALL_WORKERS=str(workers))
    t = time.perf_counter()
    r = run([sys.executable, str(proj / "Scripts" / script)], env=env, capture_output=True, encoding="utf-8")
    dt = time.perf_counter() - t
    log.write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
    return dt, r.returncode


def scenario(title, sizes, proj, dsn, work, rows_scale, seed_base):
    sizes = [max(1, int(s * rows_scale)) for s in sizes]
    print(f"\n=== {title}: {len(sizes)} WR(s), {sum(sizes)} rows")
    print(f"{'script':34}{'time':>9}{'rows in DB':>12}  result")
    out = work / "out"
    results = {}
    for label, script, workers in [("original script", "original_install.py", 1),
                                   ("install_fast.py, 1 worker", "install_fast.py", 1),
                                   ("install_fast.py, 2 workers", "install_fast.py", 2),
                                   ("install_fast.py, 4 workers", "install_fast.py", 4),
                                   ("install_fast.py, 6 workers", "install_fast.py", 6)]:
        seed_base += 1
        reset_tables(dsn)
        make_files(out, sizes, seed_base)           # fresh text each run => Db2 statement cache can't flatter anyone
        dt, rc = timed_run(proj, script, workers, dsn, out, work / "last_run.log")
        n = count_rows(dsn)
        ok = "OK" if rc == 0 and n == sum(sizes) else f"PROBLEM (exit {rc}) see {work / 'last_run.log'}"
        print(f"{label:34}{dt:8.1f}s{n:12}  {ok}")
        results[label] = dt
    base = results["original script"]
    best = min(v for k, v in results.items() if k != "original script")
    print(f"-> fastest new setup is {base / best:.1f}x faster than the original")


def failure_test(proj, dsn, work, rows_scale):
    print("\n=== Failure behaviour: WR #5 fails validation (sequential vs 6 workers must end identically)")
    sizes = [max(1, int(s * rows_scale)) for s in SIZES]
    out = work / "out"
    snap = {}
    for w in (1, 6):
        reset_tables(dsn)
        make_files(out, sizes, 777, bad=4)
        dt, rc = timed_run(proj, "install_fast.py", w, dsn, out, work / f"fail_{w}.log")
        status = json.loads((out / "install_status_T.json").read_text())
        snap[w] = ({k: v["status"] for k, v in status.items()}, count_rows(dsn), rc)
        print(f"workers={w}: exit={rc} rows_in_db={snap[w][1]} status={list(snap[w][0].values())}")
    print("IDENTICAL -> ordered commit works" if snap[1] == snap[6] else "MISMATCH - please send me work/fail_*.log")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    ap.add_argument("--rows-scale", type=float, default=1.0)
    ap.add_argument("--keep-container", action="store_true")
    ap.add_argument("--skip-failure-test", action="store_true")
    a = ap.parse_args()
    ensure_driver()
    dsn = a.dsn
    if not dsn:
        start_docker()
        dsn = DOCKER_DSN
    else:
        try:
            connect(dsn).close()
        except Exception as e:
            sys.exit(f"Cannot connect with the given --dsn: {e}")
        print("Connected to your Db2 (only ZZ_BENCH_* tables will be created/dropped).")
    work = Path(tempfile.mkdtemp(prefix="db2bench_"))
    proj = scaffold(work)
    print(f"Working folder: {work}")
    scenario("Scenario 1 - one WR with 5000 INSERT queries", [5000], proj, dsn, work, a.rows_scale, 100)
    scenario("Scenario 2 - twelve WRs", SIZES, proj, dsn, work, a.rows_scale, 200)
    if not a.skip_failure_test:
        failure_test(proj, dsn, work, a.rows_scale)
    reset_tables(dsn)
    print("\nDone. Send me the lines above (and any PROBLEM log) if something looks off.")


if __name__ == "__main__":
    main()
