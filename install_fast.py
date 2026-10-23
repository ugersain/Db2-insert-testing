import re
import os
import sys
import json
import time
import multiprocessing as mp
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from Utils.database import get_connection

from Config.settings import OUTPUT_FOLDER as CFG_OUTPUT_FOLDER

# Number of parallel worker PROCESSES (each with its own DB connection).
# 1 = exactly the old sequential behaviour. (Processes, not threads: DB drivers
# hold Python's GIL during executemany, so threads are slower than sequential.)
# WRs run in parallel but COMMIT strictly in file order, so a failed WR still
# stops everything after it (later WRs are rolled back, never committed).
WORKERS = int(os.environ.get("INSTALL_WORKERS", "4"))
MANY_CHUNK = int(os.environ.get("INSTALL_CHUNK", "1000"))   # rows per executemany call


# ============================
# HELPERS (module level: worker processes import these)
# ============================

def extract_insert_queries(block):
    queries = []

    for line in block.splitlines():
        line = line.strip()

        if not line or line.startswith("--"):
            continue

        if line.upper().startswith("INSERT"):
            queries.append(line.rstrip(";"))

    return queries


# ---- FAST INSERT / VALIDATION HELPERS ----

INSERT_RE = re.compile(r'^(INSERT\s+INTO\s+.+?\s+VALUES\s*)\((.*)\)\s*$', re.IGNORECASE | re.DOTALL)
DEC_RE = re.compile(r'^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$')


class Unsupported(Exception):
    pass


_T = r"(?:'(?:[^']|'')*'|[^,'()\s][^,'()]*?)"        # one quoted string, or one plain token
_ROW = re.compile(rf"\s*{_T}\s*(?:,\s*{_T}\s*)*")      # whole VALUES row must be made of these
_TOK = re.compile(rf"\s*({_T})\s*(?:,|$)")


def split_values(s):
    """Split a VALUES row into tokens; raises Unsupported for functions, casts, 'a' || 'b', N'..', etc."""
    if not _ROW.fullmatch(s):
        raise Unsupported("row")
    return _TOK.findall(s)


def to_param(tok):
    if tok[0] == "'":
        body = tok[1:-1]
        return body.replace("''", "'") if "''" in body else body
    try:
        return int(tok)
    except ValueError:
        pass
    if tok.upper() == "NULL":
        return None
    if DEC_RE.match(tok):
        return Decimal(tok)
    raise Unsupported(tok)          # CURRENT TIMESTAMP, DATE '..', CAST(..), X'..'


def _literal_batches(prefix, rows, size):
    return [("sql", prefix + ",".join(f"({r})" for r in rows[i:i + size])) for i in range(0, len(rows), size)]


def plan_inserts(queries, literal_batch=100, many_chunk=1000, allow_many=True):
    plan, prefix, norm_prefix, raw_rows = [], None, None, []
    norm = lambda s: re.sub(r"\s+", " ", s.strip().upper())

    def flush():
        nonlocal prefix, norm_prefix, raw_rows
        if raw_rows:
            try:
                if not allow_many:
                    raise Unsupported("executemany disabled")
                parsed = [[to_param(t) for t in split_values(r)] for r in raw_rows]
                if len({len(p) for p in parsed}) != 1:
                    raise Unsupported("column count differs")
                sql = prefix + "(" + ",".join("?" * len(parsed[0])) + ")"
                for i in range(0, len(parsed), many_chunk):
                    plan.append(("many", sql, parsed[i:i + many_chunk]))
            except Unsupported:
                plan.extend(_literal_batches(prefix, raw_rows, literal_batch))
        prefix, norm_prefix, raw_rows = None, None, []

    for q in queries:
        m = INSERT_RE.match(q.strip())
        if m:
            p, vals = m.group(1), m.group(2)
            if norm_prefix is not None and norm(p) == norm_prefix:
                raw_rows.append(vals)
            else:
                flush(); prefix, norm_prefix, raw_rows = p, norm(p), [vals]
        else:
            flush(); plan.append(("sql", q))
    flush()
    return plan


def run_plan(cursor, plan):
    for step in plan:
        if step[0] == "many":
            cursor.executemany(step[1], step[2])
        else:
            cursor.execute(step[1])


def count_rows(cursor, q):
    """Row count of a validation SELECT without shipping the rows to Python."""
    q = re.sub(r"(?is)\s+WITH\s+(UR|CS|RS|RR)\s*;?\s*$", "", q.strip().rstrip(";"))
    cursor.execute(f"SELECT COUNT(*) FROM ({q}) AS V WITH UR")
    return cursor.fetchone()[0]


def count_all(cursor, queries):
    """Total rows of ALL validation SELECTs of a WR in ONE round trip."""
    parts = []
    for q in queries:
        q = re.sub(r"(?is)\s+WITH\s+(UR|CS|RS|RR)\s*;?\s*$", "", q.strip().rstrip(";"))
        parts.append(f"SELECT COUNT(*) AS C FROM ({q}) AS V{len(parts)}")
    if not parts:
        return 0
    cursor.execute("SELECT SUM(C) FROM (" + " UNION ALL ".join(parts) + ") AS T WITH UR")
    return int(cursor.fetchone()[0] or 0)



def extract_select_queries(block):
    queries = re.split(r";\s*\n", block)
    result = []

    for q in queries:
        lines = [l for l in q.splitlines() if not l.strip().startswith("--")]
        q_clean = "\n".join(lines).strip()

        if q_clean.upper().startswith("SELECT"):
            result.append(q_clean)

    return result


TBL = r'([A-Za-z_][\w$#@]*(?:\.[A-Za-z_][\w$#@]*)?)'


def tables_written(queries):
    out = set()
    for q in queries:
        m = re.match(r'\s*INSERT\s+INTO\s+' + TBL, q, re.IGNORECASE)
        out.add(m.group(1).upper() if m else "*UNKNOWN*")
    return out


def tables_read(select_queries):
    out = set()
    for q in select_queries:
        out.update(t.upper() for t in re.findall(r'\b(?:FROM|JOIN)\s+' + TBL, q, re.IGNORECASE))
    return out


def do_work(c, cur, wr_name, iq, sq, val_block):
    """INSERT + VALIDATE one WR on connection c. Never commits. Returns (ok, fail_msg, log_lines)."""
    log = [f"\nProcessing {wr_name}"]
    if not iq:
        return False, f"FAILED: No INSERTs found for {wr_name}", log

    raw_count = len(iq)
    t0 = time.perf_counter()
    plan = plan_inserts(iq, many_chunk=MANY_CHUNK)
    try:
        run_plan(cur, plan)
    except Exception:
        # safety net: retry the old way (literal multi-row batches) before declaring failure
        c.rollback()
        try:
            plan = plan_inserts(iq, allow_many=False)
            run_plan(cur, plan)
        except Exception as e:
            c.rollback()
            return False, f"FAILED: INSERT error -> {e}", log
    t_insert = time.perf_counter() - t0
    log.append(f"  Found {raw_count} INSERT rows -> {len(plan)} step(s) "
               f"({sum(1 for s in plan if s[0] == 'many')} executemany)")
    log.append(f"  INSERTS DONE ({raw_count} rows)")

    if not val_block:
        c.rollback()
        return False, f"FAILED: Validation block missing for {wr_name}", log

    match = re.search(r"(\d+)\s+rows", val_block, re.IGNORECASE)
    expected = int(match.group(1)) if match else None

    t0 = time.perf_counter()
    try:
        total_rows = count_all(cur, sq)                 # all checks in ONE round trip
    except Exception:
        total_rows = sum(count_rows(cur, q) for q in sq)
    t_valid = time.perf_counter() - t0

    log.append(f"  Expected: {expected}, Actual: {total_rows}")
    if expected is not None and total_rows != expected:
        c.rollback()
        return False, f"FAILED: Validation mismatch for {wr_name}", log
    log.append(f"  TIMING insert={t_insert:.1f}s validation={t_valid:.1f}s")
    return True, "", log


def worker_main(wid, inbox, outbox):
    try:
        c = get_connection()
        c.autocommit = False
        cur = c.cursor()
    except Exception as e:
        outbox.put(("fatal", wid, f"worker {wid} cannot connect: {e}"))
        return
    while True:
        msg = inbox.get()
        kind = msg[0]
        if kind == "stop":
            break
        if kind == "work":
            _, p, wr_name, iq, sq, val_block = msg
            try:
                ok, text, log = do_work(c, cur, wr_name, iq, sq, val_block)
            except Exception as e:
                try:
                    c.rollback()
                except Exception:
                    pass
                ok, text, log = False, f"FAILED: {e}", [f"\nProcessing {wr_name}"]
            outbox.put(("done", wid, p, ok, text, log))
        else:                                           # "commit" / "rollback"
            err, t0 = None, time.perf_counter()
            try:
                c.commit() if kind == "commit" else c.rollback()
            except Exception as e:
                err = str(e)
            outbox.put(("ack", wid, msg[1], kind, err, time.perf_counter() - t0))
    try:
        c.close()
    except Exception:
        pass


def main():
    # ============================
    # FILES
    # ============================
    OUTPUT_FOLDER = str(CFG_OUTPUT_FOLDER)

    insert_file = sorted(
        [f for f in os.listdir(OUTPUT_FOLDER) if f.lower().startswith("updated_insert")]
    )[-1]

    validation_file = sorted(
        [f for f in os.listdir(OUTPUT_FOLDER) if f.lower().startswith("validation_")]
    )[-1]

    INSERT_PATH = os.path.join(OUTPUT_FOLDER, insert_file)
    VALIDATION_PATH = os.path.join(OUTPUT_FOLDER, validation_file)

    deployment_suffix = insert_file.replace("Updated_Insert_", "").replace(".sql", "")

    STATUS_FILE = os.path.join(OUTPUT_FOLDER, f"install_status_{deployment_suffix}.json")

    print(f"Using Status File: {STATUS_FILE}")

    print(f"Using INSERT: {INSERT_PATH}")
    print(f"Using VALIDATION: {VALIDATION_PATH}")

    # ============================
    # DB CONNECT
    # ============================
    try:
        conn = get_connection()
        print("DB connected")

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    conn.autocommit = False

    print("\n=== INSTALL STARTED ===\n")

    # ============================
    # READ FILES
    # ============================
    with open(INSERT_PATH, "r", encoding="utf-8") as f:
        insert_content = f.read().replace("\r\n", "\n")

    with open(VALIDATION_PATH, "r", encoding="utf-8") as f:
        validation_content = f.read().replace("\r\n", "\n")

    validation_content = validation_content.split("STATIC VALIDATION")[0]

    # ============================
    # SPLIT WR BLOCKS
    # ============================
    insert_wrs = re.split(r"--\s*(WR[-\w]+)", insert_content, flags=re.IGNORECASE)[1:]
    validation_wrs = re.split(r"--\s*(WR[-\w]+)", validation_content, flags=re.IGNORECASE)[
        1:
    ]

    if not insert_wrs or not validation_wrs:
        print("ERROR: WR parsing failed")
        conn.rollback()
        sys.exit(1)

    validation_lookup = {
        validation_wrs[i].strip().upper(): validation_wrs[i + 1]
        for i in range(0, len(validation_wrs), 2)
    }

    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            status_data = json.load(f)
    else:
        status_data = {}

    def save_status():
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=4)

    # ============================
    # BUILD WORK LIST (file order)
    # ============================
    pending = []
    for i in range(0, len(insert_wrs), 2):
        wr_name = insert_wrs[i]
        if wr_name in status_data and status_data[wr_name]["status"] == "SUCCESS":
            print(f"SKIPPING {wr_name} (Already Installed)")
            continue
        pending.append((wr_name, insert_wrs[i + 1]))

    parsed = []
    for wr_name, insert_block in pending:
        iq = extract_insert_queries(insert_block)
        val_block = validation_lookup.get(wr_name.strip().upper())
        sq = extract_select_queries(val_block) if val_block else []
        parsed.append({"iq": iq, "val_block": val_block, "sq": sq,
                       "w": tables_written(iq), "r": tables_read(sq)})

    # dep[p] = last earlier WR sharing a table with p where either one writes it.
    # Such a WR must be COMMITTED before p starts (no lock fights; validation sees
    # exactly what it saw in the sequential run).
    n = len(pending)
    dep = []
    for p in range(n):
        d = -1
        for q in range(p):
            a, b = parsed[q], parsed[p]
            if (a["w"] & (b["w"] | b["r"])) or (b["w"] & a["r"]) or "*UNKNOWN*" in (a["w"] | b["w"]):
                d = q
        dep.append(d)

    t_all = time.perf_counter()
    nworkers = max(1, min(WORKERS, n))
    fail_pos = None

    # ============================
    # SEQUENTIAL (WORKERS=1)
    # ============================
    if nworkers == 1:
        cur = conn.cursor()
        for p in range(n):
            wr_name = pending[p][0]
            info = parsed[p]
            ok, text, log = do_work(conn, cur, wr_name, info["iq"], info["sq"], info["val_block"])
            print("\n".join(log))
            if ok:
                t0 = time.perf_counter()
                conn.commit()
                print(f"  {wr_name} PASSED  (commit {time.perf_counter() - t0:.1f}s)")
                status_data[wr_name] = {"status": "SUCCESS"}
                save_status()
            else:
                print(text)
                conn.rollback()
                status_data[wr_name] = {"status": "FAILED"}
                save_status()
                fail_pos = p
                break
        conn.close()

    # ============================
    # PARALLEL: work in parallel, COMMIT in file order
    # ============================
    else:
        conn.close()                               # workers open their own connections
        ctx = mp.get_context("spawn")
        outbox = ctx.Queue()
        inboxes = [ctx.Queue() for _ in range(nworkers)]
        procs = [ctx.Process(target=worker_main, args=(w, inboxes[w], outbox), daemon=True)
                 for w in range(nworkers)]
        for pr in procs:
            pr.start()

        free = list(range(nworkers))
        results, holder = {}, {}
        state = {"committed": 0, "next": 0, "aborting": False}

        def handle(msg):
            if msg[0] == "fatal":
                raise RuntimeError(msg[2])
            if msg[0] == "done":
                _, wid, p, ok, text, log = msg
                if state["aborting"]:
                    inboxes[wid].put(("rollback", p))
                else:
                    results[p] = (ok, text, log)
                    holder[p] = wid
            elif msg[0] == "ack":
                free.append(msg[1])

        def wait_ack(wid):
            while True:
                msg = outbox.get()
                if msg[0] == "ack" and msg[1] == wid:
                    free.append(wid)
                    return msg
                handle(msg)

        try:
            while state["committed"] < n and fail_pos is None:
                # resolve WRs strictly in file order
                while fail_pos is None and state["committed"] in results:
                    p = state["committed"]
                    ok, text, log = results.pop(p)
                    wid = holder.pop(p)
                    wr_name = pending[p][0]
                    print("\n".join(log))
                    if ok:
                        inboxes[wid].put(("commit", p))
                        ack = wait_ack(wid)
                        if ack[4]:
                            ok, text = False, f"FAILED: commit error -> {ack[4]}"
                        else:
                            print(f"  {wr_name} PASSED  (commit {ack[5]:.1f}s)")
                            status_data[wr_name] = {"status": "SUCCESS"}
                            save_status()
                            state["committed"] += 1
                    else:
                        inboxes[wid].put(("rollback", p))
                        wait_ack(wid)
                    if not ok:
                        print(text)
                        status_data[wr_name] = {"status": "FAILED"}
                        save_status()
                        fail_pos = p
                if fail_pos is not None or state["committed"] >= n:
                    break
                # hand out work in file order; a WR starts only when the earlier WRs it
                # shares tables with are committed
                while free and state["next"] < n and dep[state["next"]] < state["committed"]:
                    p = state["next"]
                    wid = free.pop(0)
                    info = parsed[p]
                    inboxes[wid].put(("work", p, pending[p][0], info["iq"], info["sq"], info["val_block"]))
                    state["next"] += 1
                handle(outbox.get())

            # failure: discard everything that is held or still running
            if fail_pos is not None:
                state["aborting"] = True
                for p, wid in list(holder.items()):
                    inboxes[wid].put(("rollback", p))
                holder.clear()
                results.clear()
                while len(free) < nworkers:
                    handle(outbox.get())
        finally:
            for q in inboxes:
                q.put(("stop",))
            for pr in procs:
                pr.join(timeout=10)

    if fail_pos is None:
        print(f"\n=== ALL WRs INSTALLED SUCCESSFULLY ({time.perf_counter() - t_all:.1f}s, workers={nworkers}) ===")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
