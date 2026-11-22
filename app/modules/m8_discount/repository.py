import json

from app.errors import Problem

FIELDS = ("run_id, ar_source_id, term_source_id, receipt_source_id, rule_version, "
          "generated_at, elapsed_ms, result_digest")


def get(connection, run_id):
    row = connection.execute(f"SELECT {FIELDS}, payload FROM settlements WHERE run_id=?",
                             (run_id,)).fetchone()
    if row is None:
        raise Problem("SETTLEMENT_NOT_FOUND", "结算快照不存在", 404)
    return {"manifest": {key: row[key] for key in row.keys() if key != "payload"},
            "result": json.loads(row["payload"])}


def insert(connection, manifest, payload):
    values = tuple(manifest[field.strip()] for field in FIELDS.split(","))
    cursor = connection.execute(
        f"INSERT INTO settlements ({FIELDS}, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO NOTHING", (*values, payload))
    return cursor.rowcount == 1


def history(connection, ar_source_id=None):
    rows = connection.execute(
        f"SELECT {FIELDS} FROM settlements WHERE (? IS NULL OR ar_source_id=?) "
        "ORDER BY generated_at DESC, run_id LIMIT 100", (ar_source_id, ar_source_id))
    return [dict(row) for row in rows]
