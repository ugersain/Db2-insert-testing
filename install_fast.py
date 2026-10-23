import re
import os
import sys
import json
import time
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from Utils.database import get_connection

from Config.settings import OUTPUT_FOLDER as CFG_OUTPUT_FOLDER

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
cursor = conn.cursor()

print("\n=== INSTALL STARTED ===\n")

# ============================
# HELPERS
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


# ---- FAST INSERT / VALIDATION HELPERS (replace batch_insert_queries) ----

INSERT_RE = re.compile(r'^(INSERT\s+INTO\s+.+?\s+VALUES\s*)\((.*)\)\s*$', re.IGNORECASE | re.DOTALL)
STR_RE = re.compile(r"^'(?:[^']|'')*'$")
INT_RE = re.compile(r'^[+-]?\d+$')
DEC_RE = re.compile(r'^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$')


class Unsupported(Exception):
    pass


def split_values(s):
    """Split 'a, 'b,(c)', 3' on top-level commas, honouring '' escaped quotes and parentheses."""
    out, buf, in_q, depth, i = [], [], False, 0, 0
    while i < len(s):
        ch = s[i]
        if in_q:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    buf.append("'"); i += 1
                else:
                    in_q = False
        elif ch == "'":
            in_q = True; buf.append(ch)
        elif ch == "(":
            depth += 1; buf.append(ch)
        elif ch == ")":
            depth -= 1; buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip()); buf = []
        else:
            buf.append(ch)
        i += 1
    if in_q or depth:
        raise Unsupported("unbalanced")
    out.append("".join(buf).strip())
    return out


def to_param(tok):
    if tok.upper() == "NULL":
        return None
    if STR_RE.match(tok):           # strictly one quoted literal ('a' || 'b' does NOT match -> fallback)
        return tok[1:-1].replace("''", "'")
    if INT_RE.match(tok):
        return int(tok)
    if DEC_RE.match(tok):
        return Decimal(tok)
    raise Unsupported(tok)          # CURRENT TIMESTAMP, DATE '..', CAST(..), N'..', X'..'


def _literal_batches(prefix, rows, size):
    return [("sql", prefix + ",".join(f"({r})" for r in rows[i:i + size])) for i in range(0, len(rows), size)]


def plan_inserts(queries, literal_batch=100, many_chunk=1000):
    plan, prefix, norm_prefix, raw_rows = [], None, None, []
    norm = lambda s: re.sub(r"\s+", " ", s.strip().upper())

    def flush():
        nonlocal prefix, norm_prefix, raw_rows
        if raw_rows:
            try:
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


def extract_select_queries(block):
    queries = re.split(r";\s*\n", block)
    result = []

    for q in queries:
        lines = [l for l in q.splitlines() if not l.strip().startswith("--")]
        q_clean = "\n".join(lines).strip()

        if q_clean.upper().startswith("SELECT"):
            result.append(q_clean)

    return result


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


def fail(wr_name):
    status_data[wr_name] = {"status": "FAILED"}
    conn.rollback()
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=4)


# ============================
# MAIN LOOP
# ============================
for i in range(0, len(insert_wrs), 2):

    wr_name = insert_wrs[i]
    if wr_name in status_data and status_data[wr_name]["status"] == "SUCCESS":
        print(f"SKIPPING {wr_name} (Already Installed)")
        continue

    insert_block = insert_wrs[i + 1]
    print(f"\nProcessing {wr_name}")

    insert_queries = extract_insert_queries(insert_block)

    if not insert_queries:
        print(f"FAILED: No INSERTs found for {wr_name}")
        fail(wr_name)
        break

    raw_count = len(insert_queries)
    plan = plan_inserts(insert_queries)
    print(f"  Found {raw_count} INSERT rows -> {len(plan)} step(s) "
          f"({sum(1 for s in plan if s[0] == 'many')} executemany)")

    # ----------------------------
    # STEP 1: INSERTS
    # ----------------------------
    t0 = time.perf_counter()
    try:
        run_plan(cursor, plan)
        print(f"  INSERTS DONE ({raw_count} rows)")

    except Exception as e:
        print(f"FAILED: INSERT error -> {e}")
        fail(wr_name)
        break
    t_insert = time.perf_counter() - t0

    # ----------------------------
    # STEP 2: VALIDATION
    # ----------------------------
    val_block = validation_lookup.get(wr_name.strip().upper())

    if not val_block:
        print(f"FAILED: Validation block missing for {wr_name}")
        fail(wr_name)
        break

    queries = extract_select_queries(val_block)
    match = re.search(r"(\d+)\s+rows", val_block, re.IGNORECASE)
    expected = int(match.group(1)) if match else None

    t0 = time.perf_counter()
    total_rows = 0
    for q in queries:
        total_rows += count_rows(cursor, q)
    t_valid = time.perf_counter() - t0

    print(f"  Expected: {expected}, Actual: {total_rows}")
    mismatch = expected is not None and total_rows != expected

    if mismatch:
        print(f"FAILED: Validation mismatch for {wr_name}")
        fail(wr_name)
        break

    # ----------------------------
    # STEP 3: COMMIT THIS WR ONLY
    # ----------------------------
    t0 = time.perf_counter()
    conn.commit()
    t_commit = time.perf_counter() - t0
    print(f"  {wr_name} PASSED")
    print(f"  TIMING insert={t_insert:.1f}s validation={t_valid:.1f}s commit={t_commit:.1f}s")
    status_data[wr_name] = {"status": "SUCCESS"}
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=4)

else:
    print("\n=== ALL WRs INSTALLED SUCCESSFULLY ===")

conn.close()
