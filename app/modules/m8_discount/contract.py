import re
import unicodedata
from decimal import Decimal

from app.clock import parse_day, parse_known
from app.modules.m1_intake.contract import ID, RowError, amount

TERM_COLUMNS = ("term_id", "invoice_id", "customer_id", "effective", "known",
                "discount_rate", "discount_window_days", "ref", "note")
RECEIPT_COLUMNS = ("receipt_id", "invoice_id", "customer_id", "pay_date", "known",
                   "paid_cny", "ref", "note")
RATE = re.compile(r"(?:[0-9]{1,3})(?:\.[0-9]{1,2})?", re.ASCII)
WINDOW = re.compile(r"[0-9]{1,4}", re.ASCII)
MAX_WINDOW_DAYS = 3660


def _ids(row, *keys):
    values = tuple(row[key].strip() for key in keys)
    if not all(value == "" or ID.fullmatch(value) for value in values):
        raise RowError("INVALID_ID")
    return values


def rate_bp(text: str) -> int:
    value = unicodedata.normalize("NFKC", text).strip()
    if not RATE.fullmatch(value):
        raise RowError("INVALID_AMOUNT")
    basis = int((Decimal(value) * 100).to_integral_value())
    if not 0 < basis <= 10_000:
        raise RowError("INVALID_AMOUNT")
    return basis


def window_days(text: str) -> int:
    value = unicodedata.normalize("NFKC", text).strip()
    if not WINDOW.fullmatch(value):
        raise RowError("INVALID_AMOUNT")
    days = int(value)
    if not 0 < days <= MAX_WINDOW_DAYS:
        raise RowError("INVALID_AMOUNT")
    return days


def normalize_term(line: int, row: dict):
    from app.modules.m8_discount.records import Term
    term_id, invoice, customer, ref = _ids(row, "term_id", "invoice_id", "customer_id", "ref")
    if not (term_id and invoice and customer):
        raise RowError("INVALID_ID")
    try:
        effective = parse_day(row["effective"].strip())
    except ValueError as exc:
        raise RowError("INVALID_DATE") from exc
    try:
        known = parse_known(row["known"].strip())
    except ValueError as exc:
        raise RowError("INVALID_TIMESTAMP") from exc
    return Term(line, term_id, invoice, customer, effective, known,
                rate_bp(row["discount_rate"]), window_days(row["discount_window_days"]),
                ref, row["note"])


def normalize_receipt(line: int, row: dict):
    from app.modules.m8_discount.records import Receipt
    receipt_id, invoice, customer, ref = _ids(row, "receipt_id", "invoice_id", "customer_id", "ref")
    if not (receipt_id and invoice and customer and ref):
        raise RowError("INVALID_ID")
    try:
        pay_date = parse_day(row["pay_date"].strip())
    except ValueError as exc:
        raise RowError("INVALID_DATE") from exc
    try:
        known = parse_known(row["known"].strip())
    except ValueError as exc:
        raise RowError("INVALID_TIMESTAMP") from exc
    return Receipt(line, receipt_id, invoice, customer, pay_date, known,
                   amount(row["paid_cny"]), ref, row["note"])


CONTRACTS = {
    "term": (TERM_COLUMNS, normalize_term, "terms"),
    "receipt": (RECEIPT_COLUMNS, normalize_receipt, "receipts"),
}
