import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from time import perf_counter_ns

from app.clock import utc_text
from app.config import SETTLEMENT_RULE_VERSION
from app.errors import Problem
from app.evidence import canonical, digest
from app.modules.m1_intake.service import load as load_ar
from app.modules.m8_discount.engine import evaluate
from app.modules.m8_discount.reader import read_stream
from app.modules.m8_discount import repository

STREAMS = ("term", "receipt")


def load_stream(store, source_id: str, stream: str):
    with store.connect() as connection:
        row = connection.execute(
            "SELECT content FROM settlement_sources WHERE source_id=? AND stream=?",
            (source_id, stream)).fetchone()
    if row is None:
        raise Problem("STREAM_NOT_FOUND", f"{stream} 来源不存在", 404)
    return read_stream(row["content"], stream)


def ingest_stream(store, content: bytes, stream: str):
    data = read_stream(content, stream)
    source_id = digest(content)
    with store.connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO settlement_sources VALUES (?, ?, ?)",
            (source_id, stream, content))
    return summarize_stream(source_id, data)


def summarize_stream(source_id: str, data):
    bucket = data.stream == "term" and "terms" or "receipts"
    return {
        "stream": data.stream, "source_id": source_id, "encoding": data.encoding,
        "counts": {"input": data.input_rows, "accepted": len(getattr(data, bucket)),
                   "quarantine": len(data.quarantine), "duplicates": len(data.duplicates),
                   "blank": data.blanks, "repeated_headers": data.headers},
        "quarantine": data.quarantine, "duplicates": data.duplicates,
    }


def _invoice_lines(ar_events, terms, receipts, applied_rows, rejected, known_cut):
    applied = defaultdict(list)
    for row in applied_rows:
        applied[row["receipt"]["invoice_id"]].append(row)
    rejected_by = defaultdict(list)
    for row in rejected:
        rejected_by[row["receipt"]["invoice_id"]].append(row)
    face_lookup = {row["receipt"]["invoice_id"]: row["face_cents"] for row in applied_rows}
    for row in rejected:
        face_lookup.setdefault(row["receipt"]["invoice_id"], row["face_cents"])
    invoices = []
    for invoice in sorted(face_lookup.keys()):
        rows = applied[invoice]
        face = face_lookup[invoice]
        discount = sum(r["discount_cents"] for r in rows)
        net = sum(r["applied_cents"] for r in rows)
        receipts_paid = sum(r["receipt"]["paid_cents"] for r in rows)
        invoices.append({
            "invoice_id": invoice,
            "customer_id": (rows[0]["receipt"]["customer_id"] if rows
                            else rejected_by[invoice][0]["receipt"]["customer_id"]),
            "face_cents": face, "discount_cents": discount,
            "net_settled_cents": net, "paid_cents": receipts_paid,
            "outstanding_cents": max(0, face - net),
            "settled_receipts": rows, "held_receipts": rejected_by[invoice],
        })
    return invoices


def build_snapshot(store, ar_source_id, term_source_id, receipt_source_id, known):
    ar = load_ar(store, ar_source_id)
    term_stream = load_stream(store, term_source_id, "term")
    receipt_stream = load_stream(store, receipt_source_id, "receipt")
    ar_book, term_book, applied_rows, rejected = evaluate(
        ar.events, term_stream.terms, receipt_stream.receipts, known)
    invoices = _invoice_lines(ar.events, term_stream.terms, receipt_stream.receipts,
                              applied_rows, rejected, known)
    totals = {
        "face_cents": sum(i["face_cents"] for i in invoices),
        "discount_cents": sum(i["discount_cents"] for i in invoices),
        "net_settled_cents": sum(i["net_settled_cents"] for i in invoices),
        "paid_cents": sum(i["paid_cents"] for i in invoices),
        "outstanding_cents": sum(i["outstanding_cents"] for i in invoices),
        "settled_receipt_count": sum(len(i["settled_receipts"]) for i in invoices),
        "held_receipt_count": sum(len(i["held_receipts"]) for i in invoices),
    }
    return {
        "cut": {"known": utc_text(known)},
        "sources": {"ar_source_id": ar_source_id, "term_source_id": term_source_id,
                    "receipt_source_id": receipt_source_id},
        "rule_version": SETTLEMENT_RULE_VERSION,
        "status": "PUBLISHABLE" if not rejected else "HELD",
        "totals": totals, "invoices": invoices,
        "term_quarantine": term_book.rejected,
        "intake_quarantine": {"term": term_stream.quarantine, "receipt": receipt_stream.quarantine},
        "intake_duplicates": {"term": term_stream.duplicates, "receipt": receipt_stream.duplicates},
        "held": rejected,
    }


def run_key(ar_source_id, term_source_id, receipt_source_id, known):
    return digest(canonical({
        "ar": ar_source_id, "term": term_source_id, "receipt": receipt_source_id,
        "rule_version": SETTLEMENT_RULE_VERSION, "known": utc_text(known)}))


def read_run(store, run_id):
    with store.connect() as connection:
        return repository.get(connection, run_id)


def publish(store, ar_source_id, term_source_id, receipt_source_id, known):
    start = perf_counter_ns()
    result = build_snapshot(store, ar_source_id, term_source_id, receipt_source_id, known)
    if result["status"] != "PUBLISHABLE":
        raise Problem("SETTLEMENT_HELD",
                      f"{result['totals']['held_receipt_count']} 笔回单的折扣无法解释，已推迟发布", 409)
    run_id = run_key(ar_source_id, term_source_id, receipt_source_id, known)
    payload = canonical(result)
    manifest = {
        "run_id": run_id, "ar_source_id": ar_source_id,
        "term_source_id": term_source_id, "receipt_source_id": receipt_source_id,
        "rule_version": SETTLEMENT_RULE_VERSION,
        "elapsed_ms": (perf_counter_ns() - start) // 1_000_000,
        "generated_at": utc_text(datetime.now(timezone.utc)),
        "result_digest": digest(payload),
    }
    try:
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            created = repository.insert(connection, manifest, payload)
            stored = repository.get(connection, run_id)
            if (stored["manifest"]["result_digest"] != manifest["result_digest"]
                    or digest(canonical(stored["result"])) != manifest["result_digest"]):
                raise Problem("SETTLEMENT_MISMATCH", "重算摘要与已发布快照不符", 409)
    except sqlite3.Error as exc:
        raise Problem("SETTLEMENT_WRITE_FAILED", "结算写入失败，旧版折扣已保留，请重试", 503) from exc
    return {"created": created, "manifest": stored["manifest"], "totals": result["totals"],
            "status": result["status"]}


def rerun(store, run_id):
    stored = read_run(store, run_id)
    manifest = stored["manifest"]
    if manifest["rule_version"] != SETTLEMENT_RULE_VERSION:
        raise Problem("RULE_VERSION_CHANGED", "快照使用的规则版本已变更", 409)
    from app.clock import parse_known
    known = parse_known(stored["result"]["cut"]["known"])
    candidate = run_key(manifest["ar_source_id"], manifest["term_source_id"],
                        manifest["receipt_source_id"], known)
    if candidate != run_id:
        raise Problem("SETTLEMENT_MISMATCH", "快照参数与运行编号不符", 409)
    return publish(store, manifest["ar_source_id"], manifest["term_source_id"],
                   manifest["receipt_source_id"], known)
