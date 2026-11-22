import pytest

from app.clock import parse_known
from app.evidence import canonical, digest
from app.modules.m8_discount.engine import discount_cents, evaluate
from app.modules.m8_discount.reader import read_stream
from app.modules.m8_discount.service import build_snapshot, publish, rerun
from app.config import SAMPLE

TERM_FILE = SAMPLE.parent / "discount-terms.csv"
RECEIPT_FILE = SAMPLE.parent / "discount-receipts.csv"
CUT = "2026-02-06T12:00+08:00"


@pytest.fixture(scope="session")
def term_bytes():
    return TERM_FILE.read_bytes()


@pytest.fixture(scope="session")
def receipt_bytes():
    return RECEIPT_FILE.read_bytes()


@pytest.fixture
def sources(client, term_bytes, receipt_bytes):
    ar_id = client.get("/api/intake/sample").json()["source_id"]
    term_id = client.post("/api/settlement/sources/term",
                          files={"file": ("terms.csv", term_bytes)}).json()["source_id"]
    receipt_id = client.post("/api/settlement/sources/receipt",
                             files={"file": ("receipts.csv", receipt_bytes)}).json()["source_id"]
    return ar_id, term_id, receipt_id


def _invoice(snapshot, invoice_id):
    return next(row for row in snapshot["invoices"] if row["invoice_id"] == invoice_id)


def test_round_half_up_discount_in_integer_cents():
    assert discount_cents(125050, 200) == 2501  # 2501 -> 25.01
    assert discount_cents(125050, 150) == 1876  # 1875.75 -> 18.76
    assert discount_cents(60000, 200) == 1200


def test_stream_readers_keep_line_and_duplicate_rules(term_bytes, receipt_bytes):
    terms = read_stream(term_bytes, "term")
    receipts = read_stream(receipt_bytes, "receipt")
    assert terms.encoding == "utf-8-sig"
    assert {term.term_id for term in terms.terms} == {"T1-V1", "T1-V2", "T2-V1", "T3-V1", "T4-V1", "T4-BAD"}
    assert terms.quarantine == [] and terms.input_rows == 6
    assert len(receipts.receipts) == 4


def test_revision_known_next_day_does_not_rewrite_closed_period(client, sources):
    ar_id, term_id, receipt_id = sources
    reply = client.get("/api/settlement/preview",
                       params={"ar_source_id": ar_id, "term_source_id": term_id,
                               "receipt_source_id": receipt_id, "known": CUT})
    assert reply.status_code == 200
    snap = reply.json()
    ar1001 = _invoice(snap, "AR-1001")
    used = ar1001["settled_receipts"][0]
    # 回单知悉(01-10)早于修订(01-11)，按旧条款 2% 确认，实收/未收按旧净额
    assert used["term"]["term_id"] == "T1-V1" and used["term"]["discount_rate_bp"] == 200
    assert used["discount_cents"] == 2501 and used["applied_cents"] == 122549
    assert ar1001["face_cents"] == 125050 and ar1001["discount_cents"] == 2501
    assert ar1001["net_settled_cents"] == 122549 and ar1001["outstanding_cents"] == 2501


def test_same_settlement_is_stable_under_a_later_cut(client, sources):
    ar_id, term_id, receipt_id = sources
    before = client.get("/api/settlement/preview",
                        params={"ar_source_id": ar_id, "term_source_id": term_id,
                                "receipt_source_id": receipt_id, "known": CUT}).json()
    after = client.get("/api/settlement/preview",
                       params={"ar_source_id": ar_id, "term_source_id": term_id,
                               "receipt_source_id": receipt_id,
                               "known": "2026-03-31T23:59+08:00"}).json()
    ar_before = _invoice(before, "AR-1001")
    ar_after = _invoice(after, "AR-1001")
    assert ar_before["net_settled_cents"] == ar_after["net_settled_cents"] == 122549
    assert ar_after["settled_receipts"][0]["term"]["term_id"] == "T1-V1"


def test_outside_window_pays_face_without_discount(client, sources):
    ar_id, term_id, receipt_id = sources
    snap = client.get("/api/settlement/preview",
                      params={"ar_source_id": ar_id, "term_source_id": term_id,
                              "receipt_source_id": receipt_id, "known": CUT}).json()
    ar1002 = _invoice(snap, "AR-1002")
    row = ar1002["settled_receipts"][0]
    assert row["discount_cents"] == 0 and row["applied_cents"] == 80000
    assert ar1002["outstanding_cents"] == 0


def test_unexplained_discount_holds_publication(client, sources):
    ar_id, term_id, receipt_id = sources
    snap = client.get("/api/settlement/preview",
                      params={"ar_source_id": ar_id, "term_source_id": term_id,
                              "receipt_source_id": receipt_id, "known": CUT}).json()
    assert snap["status"] == "HELD"
    held = {row["receipt"]["receipt_id"]: row["code"] for row in snap["held"]}
    assert held == {"R3-01": "DISCOUNT_PARTIAL"}
    publish_reply = client.post("/api/settlement/publish",
                                json={"ar_source_id": ar_id, "term_source_id": term_id,
                                      "receipt_source_id": receipt_id, "known": CUT})
    assert publish_reply.status_code == 409
    assert publish_reply.json()["code"] == "SETTLEMENT_HELD"
    assert client.get("/api/settlement").json()["runs"] == []


def test_publish_is_idempotent_and_rerun_adds_no_settlement(client, sources, tmp_path):
    ar_id, term_id, _ = sources
    clean_receipts = (
        "receipt_id,invoice_id,customer_id,pay_date,known,paid_cny,ref,note\n"
        "R1-01,AR-1001,00017,2026-01-10,2026-01-10T15:22+08:00,1225.49,A-V1,ok\n"
        "R2-01,AR-1002,17,2026-01-20,2026-01-21T10:00+08:00,800.00,B-V1,ok\n"
        "R4-01,AR-1004,01304,2026-01-21,2026-01-21T13:10+08:00,637.00,D-V1,ok\n"
    ).encode()
    receipt_id = client.post("/api/settlement/sources/receipt",
                             files={"file": ("clean.csv", clean_receipts)}).json()["source_id"]
    body = {"ar_source_id": ar_id, "term_source_id": term_id,
            "receipt_source_id": receipt_id, "known": CUT}
    first = client.post("/api/settlement/publish", json=body).json()
    second = client.post("/api/settlement/publish", json=body).json()
    assert first["created"] is True and second["created"] is False
    assert first["manifest"]["run_id"] == second["manifest"]["run_id"]
    assert client.get("/api/settlement").json()["runs"] == [first["manifest"]]
    rerun_reply = client.post(f"/api/settlement/{first['manifest']['run_id']}/rerun").json()
    assert rerun_reply["created"] is False
    assert rerun_reply["manifest"] == first["manifest"]
    assert len(client.get("/api/settlement").json()["runs"]) == 1


def test_evidence_export_round_trips_and_digest_matches(client, sources):
    ar_id, term_id, _ = sources
    clean_receipts = (
        "receipt_id,invoice_id,customer_id,pay_date,known,paid_cny,ref,note\n"
        "R4-01,AR-1004,01304,2026-01-21,2026-01-21T13:10+08:00,637.00,D-V1,ok\n"
    ).encode()
    receipt_id = client.post("/api/settlement/sources/receipt",
                             files={"file": ("one.csv", clean_receipts)}).json()["source_id"]
    run = client.post("/api/settlement/publish",
                      json={"ar_source_id": ar_id, "term_source_id": term_id,
                            "receipt_source_id": receipt_id, "known": CUT}).json()
    exported = client.get(f"/api/settlement/{run['manifest']['run_id']}/export")
    assert exported.status_code == 200
    bundle = exported.json()
    assert bundle["manifest"]["result_digest"] == digest(canonical(bundle["result"]))
    assert bundle["result"]["status"] == "PUBLISHABLE"
    ar1004 = next(i for i in bundle["result"]["invoices"] if i["invoice_id"] == "AR-1004")
    assert (ar1004["face_cents"], ar1004["discount_cents"], ar1004["net_settled_cents"]) == (65000, 1300, 63700)


def test_interrupted_write_keeps_previous_version(client, sources):
    ar_id, term_id, _ = sources
    clean = (
        "receipt_id,invoice_id,customer_id,pay_date,known,paid_cny,ref,note\n"
        "R2-01,AR-1002,17,2026-01-20,2026-01-21T10:00+08:00,800.00,B-V1,ok\n"
    ).encode()
    receipt_id = client.post("/api/settlement/sources/receipt",
                             files={"file": ("c.csv", clean)}).json()["source_id"]
    body = {"ar_source_id": ar_id, "term_source_id": term_id,
            "receipt_source_id": receipt_id, "known": CUT}
    first = client.post("/api/settlement/publish", json=body).json()
    # 新批次产生不同 run_id，触发器模拟发布中断，旧版快照须原样保留。
    other = (
        "receipt_id,invoice_id,customer_id,pay_date,known,paid_cny,ref,note\n"
        "R4-01,AR-1004,01304,2026-01-21,2026-01-21T13:10+08:00,637.00,D-V1,ok\n"
    ).encode()
    other_id = client.post("/api/settlement/sources/receipt",
                           files={"file": ("o.csv", other)}).json()["source_id"]
    other_body = {**body, "receipt_source_id": other_id}
    store = client.app.state.store
    with store.connect() as connection:
        connection.execute("CREATE TRIGGER fail_settlement AFTER INSERT ON settlements "
                           "BEGIN SELECT RAISE(ABORT, 'test'); END")
    failure = client.post("/api/settlement/publish", json=other_body)
    assert (failure.status_code, failure.json()["code"]) == (503, "SETTLEMENT_WRITE_FAILED")
    with store.connect() as connection:
        rows = connection.execute("SELECT run_id FROM settlements").fetchall()
        assert [row["run_id"] for row in rows] == [first["manifest"]["run_id"]]
        connection.execute("DROP TRIGGER fail_settlement")
    retry = client.post("/api/settlement/publish", json=other_body).json()
    assert retry["created"] is True
    assert {run["run_id"] for run in client.get("/api/settlement").json()["runs"]} == {
        first["manifest"]["run_id"], retry["manifest"]["run_id"]}


def test_engine_unit_terms_and_target_guards(data):
    terms = read_stream(TERM_FILE.read_bytes(), "term")
    receipts = read_stream(RECEIPT_FILE.read_bytes(), "receipt")
    _, term_book, applied, rejected = evaluate(data.events, terms.terms, receipts.receipts,
                                               parse_known(CUT))
    applied_ids = {row["receipt"]["receipt_id"] for row in applied}
    assert applied_ids == {"R1-01", "R2-01", "R4-01"}
    assert {row["receipt"]["receipt_id"] for row in rejected} == {"R3-01"}
    # 条款隔离区保留缺父版的重复初版，原始记录不丢失
    assert {row["term"]["term_id"] for row in term_book.rejected} == {"T4-BAD"}


def make_term(rows):
    header = "term_id,invoice_id,customer_id,effective,known,discount_rate,discount_window_days,ref,note\n"
    return (header + "".join(",".join(str(c) for c in row) + "\n" for row in rows)).encode()


def make_receipt(rows):
    header = "receipt_id,invoice_id,customer_id,pay_date,known,paid_cny,ref,note\n"
    return (header + "".join(",".join(str(c) for c in row) + "\n" for row in rows)).encode()


def test_term_fork_and_stale_parent_are_quarantined(data):
    from app.modules.m8_discount.term_rules import build_term_book
    from app.clock import parse_known
    raw = make_term([
        ("T1", "AR-1001", "00017", "2025-12-05", "2025-12-05T11:00+08:00", "2.00", "45", "", "r"),
        ("T2A", "AR-1001", "00017", "2025-12-05", "2026-01-11T09:00+08:00", "1.00", "10", "T1", "a"),
        ("T2B", "AR-1001", "00017", "2025-12-05", "2026-01-11T09:00+08:00", "0.50", "10", "T1", "b"),
        ("T3", "AR-1001", "00017", "2025-12-05", "2026-01-12T09:00+08:00", "1.00", "10", "GHOST", "s"),
    ])
    terms = read_stream(raw, "term")
    book = build_term_book(terms.terms, parse_known("2026-02-06T12:00+08:00"))
    codes = {row["term"]["term_id"]: row["code"] for row in book.rejected}
    assert codes == {"T2A": "TERM_FORK", "T2B": "TERM_FORK", "T3": "TERM_PARENT"}
    # 同刻分叉时当前条款仍停留在父版 T1
    assert book.term_at("AR-1001", parse_known("2026-01-31T00:00+08:00")).term_id == "T1"


def test_parse_level_term_quarantine_is_preserved():
    raw = make_term([
        ("T1", "AR-1001", "00017", "2025-12-05", "2025-12-05T11:00+08:00", "120.00", "45", "", "bad rate"),
        ("T2", "AR-1001", "00017", "2025-12-05", "2025-12-05T11:00+08:00", "2.00", "0", "", "bad window"),
    ])
    stream = read_stream(raw, "term")
    assert {row["event_id"]: row["code"] for row in stream.quarantine} == {"T1": "INVALID_AMOUNT", "T2": "INVALID_AMOUNT"}
    assert stream.terms == []


def test_receipt_before_any_term_known_pays_face_without_discount(data):
    from app.modules.m8_discount.term_rules import build_term_book  # noqa: F401
    terms = read_stream(make_term([
        ("T1", "AR-1001", "00017", "2025-12-05", "2026-01-15T00:00+08:00", "2.00", "45", "", "late term"),
    ]), "term")
    receipts = read_stream(make_receipt([
        ("P1", "AR-1001", "00017", "2026-01-10", "2026-01-10T15:22+08:00", "1250.50", "A-V1", "before term"),
    ]), "receipt")
    _, _, applied, rejected = evaluate(data.events, terms.terms, receipts.receipts,
                                       parse_known("2026-02-06T12:00+08:00"))
    assert rejected == [] and len(applied) == 1
    row = applied[0]
    assert row["term"] is None and row["discount_cents"] == 0 and row["applied_cents"] == 125050


def test_partial_payment_in_window_is_held_not_partially_discounted(data):
    terms = read_stream(make_term([
        ("T1", "AR-1001", "00017", "2025-12-05", "2025-12-05T11:00+08:00", "2.00", "45", "", "t"),
    ]), "term")
    receipts = read_stream(make_receipt([
        ("P1", "AR-1001", "00017", "2026-01-10", "2026-01-10T15:22+08:00", "1000.00", "A-V1", "partial"),
    ]), "receipt")
    _, _, applied, rejected = evaluate(data.events, terms.terms, receipts.receipts,
                                       parse_known("2026-02-06T12:00+08:00"))
    assert applied == [] and rejected[0]["code"] == "DISCOUNT_PARTIAL"
    assert rejected[0]["expected_net_cents"] == 122549


def test_window_full_face_payment_is_held(data):
    terms = read_stream(make_term([
        ("T1", "AR-1001", "00017", "2025-12-05", "2025-12-05T11:00+08:00", "2.00", "45", "", "t"),
    ]), "term")
    receipts = read_stream(make_receipt([
        ("P1", "AR-1001", "00017", "2026-01-10", "2026-01-10T15:22+08:00", "1250.50", "A-V1", "no discount taken"),
    ]), "receipt")
    _, _, applied, rejected = evaluate(data.events, terms.terms, receipts.receipts,
                                       parse_known("2026-02-06T12:00+08:00"))
    assert applied == [] and rejected[0]["code"] == "DISCOUNT_WINDOW_MISS"


def test_missing_and_bad_target_receipt_codes(data):
    terms = read_stream(make_term([
        ("T1", "AR-1001", "00017", "2025-12-05", "2025-12-05T11:00+08:00", "2.00", "45", "", "t"),
    ]), "term")
    receipts = read_stream(make_receipt([
        ("P1", "AR-1001", "00017", "2026-01-10", "2026-01-10T15:22+08:00", "1225.49", "GHOST", "pending target"),
    ]), "receipt")
    _, _, applied, rejected = evaluate(data.events, terms.terms, receipts.receipts,
                                       parse_known("2026-02-06T12:00+08:00"))
    assert applied == [] and rejected[0]["code"] == "TERM_TARGET_PENDING"


def test_http_stream_validation_and_cut_guards(client, sources):
    ar_id, _, _ = sources
    bad_stream = client.post("/api/settlement/sources/ledger",
                             files={"file": ("x.csv", b"a,b\n1,2\n")})
    assert bad_stream.status_code == 404 and bad_stream.json()["code"] == "UNKNOWN_STREAM"
    bad_cut = client.get("/api/settlement/preview",
                         params={"ar_source_id": ar_id, "term_source_id": "t",
                                 "receipt_source_id": "r", "known": "2026-02-06T12:00"})
    assert bad_cut.status_code == 422 and bad_cut.json()["code"] == "INVALID_CUT"
    missing = client.get("/api/settlement/sources/term/does-not-exist")
    assert missing.status_code == 404 and missing.json()["code"] == "STREAM_NOT_FOUND"
    missing_run = client.post("/api/settlement/nope/rerun")
    assert missing_run.status_code == 404 and missing_run.json()["code"] == "SETTLEMENT_NOT_FOUND"


def test_http_held_then_corrected_publish_flow(client, sources):
    ar_id, term_id, receipt_id = sources
    params = {"ar_source_id": ar_id, "term_source_id": term_id,
              "receipt_source_id": receipt_id, "known": CUT}
    held = client.get("/api/settlement/preview", params=params).json()
    assert held["status"] == "HELD"
    assert client.post("/api/settlement/publish", json=params).status_code == 409
    corrected = dict(zip(
        ["receipt_id", "invoice_id", "customer_id", "pay_date", "known", "paid_cny", "ref", "note"],
        ("R3-01", "AR-1003", "00308", "2026-01-15", "2026-01-17T10:00+08:00", "1674.60", "C-V1", "corrected")))
    import csv as _csv
    import io
    buffer = io.StringIO(newline="")
    writer = _csv.writer(buffer)
    writer.writerow(["receipt_id", "invoice_id", "customer_id", "pay_date", "known", "paid_cny", "ref", "note"])
    for receipt in held["invoices"]:
        for row in receipt["settled_receipts"]:
            r = row["receipt"]
            writer.writerow([r["receipt_id"], r["invoice_id"], r["customer_id"], r["pay_date"],
                             r["known"], f"{r['paid_cents'] / 100:.2f}", r["ref"], r["note"]])
    writer.writerow(list(corrected.values()))
    new_receipt_id = client.post("/api/settlement/sources/receipt",
                                 files={"file": ("fixed.csv", buffer.getvalue().encode())}).json()["source_id"]
    fixed_params = {**params, "receipt_source_id": new_receipt_id}
    preview = client.get("/api/settlement/preview", params=fixed_params).json()
    assert preview["status"] == "PUBLISHABLE" and preview["held"] == []
    published = client.post("/api/settlement/publish", json=fixed_params).json()
    assert published["created"] is True
    run_id = published["manifest"]["run_id"]
    exported = client.get(f"/api/settlement/{run_id}/export").json()
    ar1003 = next(i for i in exported["result"]["invoices"] if i["invoice_id"] == "AR-1003")
    # 1700.10 票面，1.5% 折扣 = 25.50，净 1674.60
    assert (ar1003["face_cents"], ar1003["discount_cents"], ar1003["net_settled_cents"]) == (170010, 2550, 167460)


def test_workbench_serves_settlement_page_and_asset(client):
    home = client.get("/")
    assert home.status_code == 200 and 'data-page="settlement"' in home.text
    assert 'href="#settlement"' in home.text
    asset = client.get("/static/settlement.js")
    assert asset.status_code == 200 and "settlement/preview" in asset.text
