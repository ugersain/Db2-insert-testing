"""Drop-in helpers to speed up the WR install script.

 plan_inserts(queries)  -> replaces batch_insert_queries(): rows of simple literals are turned into
                           ONE prepared 'INSERT ... VALUES (?,?,..)' + executemany; anything it cannot
                           parse safely (functions, casts, DATE '..', INSERT..SELECT) falls back to
                           small literal multi-row batches, so behaviour never changes.
 run_plan(cursor, plan) -> executes the plan (no commit; your per-WR commit stays as is).
 count_rows(cursor, q)  -> replaces 'execute + fetchall + len': counts on the server.
"""
import re
from decimal import Decimal

INSERT_RE = re.compile(r'^(INSERT\s+INTO\s+.+?\s+VALUES\s*)\((.*)\)\s*$', re.IGNORECASE | re.DOTALL)
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
    if tok.startswith("'") and tok.endswith("'") and len(tok) >= 2:
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
