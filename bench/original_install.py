import re
import os
import sys
import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from Utils.database import get_connection

from Config.settings import OUTPUT_FOLDER as CFG_OUTPUT_FOLDER

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

try:
    conn = get_connection()
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

conn.autocommit = False
cursor = conn.cursor()


def extract_insert_queries(block):
    queries = []

    for line in block.splitlines():
        line = line.strip()

        if not line or line.startswith("--"):
            continue

        if line.upper().startswith("INSERT"):
            queries.append(line.rstrip(";"))

    return queries


def batch_insert_queries(queries, batch_size=500):
    insert_re = re.compile(
        r'^(INSERT\s+INTO\s+.+?\s+VALUES\s*)\((.*)\)\s*$',
        re.IGNORECASE | re.DOTALL,
    )

    batched = []
    current_prefix = None
    current_rows = []

    def norm(s):
        return re.sub(r"\s+", " ", s.strip().upper())

    def flush():
        nonlocal current_prefix, current_rows
        if current_rows:
            for start in range(0, len(current_rows), batch_size):
                chunk = current_rows[start:start + batch_size]
                batched.append(current_prefix + ",".join(f"({r})" for r in chunk))
        current_prefix = None
        current_rows = []

    for q in queries:
        m = insert_re.match(q.strip())
        if m:
            prefix, row_values = m.group(1), m.group(2)
            if current_prefix is not None and norm(current_prefix) == norm(prefix):
                current_rows.append(row_values)
            else:
                flush()
                current_prefix = prefix
                current_rows = [row_values]
        else:
            flush()
            batched.append(q)

    flush()
    return batched


def extract_select_queries(block):
    queries = re.split(r";\s*\n", block)
    result = []

    for q in queries:
        lines = [l for l in q.splitlines() if not l.strip().startswith("--")]
        q_clean = "\n".join(lines).strip()

        if q_clean.upper().startswith("SELECT"):
            result.append(q_clean)

    return result


def add_with_ur(q):
    q = q.strip().rstrip(";")
    if "WITH UR" not in q.upper():
        q += " WITH UR"
    return q


with open(INSERT_PATH, "r", encoding="utf-8") as f:
    insert_content = f.read().replace("\r\n", "\n")

with open(VALIDATION_PATH, "r", encoding="utf-8") as f:
    validation_content = f.read().replace("\r\n", "\n")

validation_content = validation_content.split("STATIC VALIDATION")[0]

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

for i in range(0, len(insert_wrs), 2):

    wr_name = insert_wrs[i]
    if wr_name in status_data and status_data[wr_name]["status"] == "SUCCESS":
        continue

    insert_block = insert_wrs[i + 1]

    insert_queries = extract_insert_queries(insert_block)

    if not insert_queries:
        status_data[wr_name] = {"status": "FAILED"}
        conn.rollback()
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=4)
        break

    raw_count = len(insert_queries)
    insert_queries = batch_insert_queries(insert_queries, batch_size=500)

    try:
        for q in insert_queries:
            cursor.execute(q)

    except Exception as e:
        print(f"FAILED: INSERT error -> {e}")
        status_data[wr_name] = {"status": "FAILED"}
        conn.rollback()
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=4)
        break

    val_block = validation_lookup.get(wr_name.strip().upper())

    if not val_block:
        status_data[wr_name] = {"status": "FAILED"}
        conn.rollback()
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=4)
        break

    queries = extract_select_queries(val_block)
    match = re.search(r"(\d+)\s+rows", val_block, re.IGNORECASE)
    expected = int(match.group(1)) if match else None

    total_rows = 0
    for q in queries:
        q = add_with_ur(q)
        cursor.execute(q)
        rows = cursor.fetchall()
        total_rows += len(rows)

    mismatch = expected is not None and total_rows != expected

    if mismatch:
        print(f"FAILED: Validation mismatch for {wr_name}")
        status_data[wr_name] = {"status": "FAILED"}
        conn.rollback()
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=4)
        break

    status_data[wr_name] = {"status": "SUCCESS"}
    conn.commit()
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=4)

else:
    print("ALL OK")

conn.close()
