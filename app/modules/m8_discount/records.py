from dataclasses import dataclass
from datetime import date, datetime

from app.clock import utc_text


@dataclass(frozen=True)
class Term:
    line: int
    term_id: str
    invoice_id: str
    customer_id: str
    effective: date
    known: datetime
    rate_bp: int
    window_days: int
    ref: str = ""
    note: str = ""

    def public(self):
        return {
            "line": self.line, "term_id": self.term_id, "invoice_id": self.invoice_id,
            "customer_id": self.customer_id, "effective": self.effective.isoformat(),
            "known": utc_text(self.known), "discount_rate_bp": self.rate_bp,
            "discount_window_days": self.window_days, "ref": self.ref, "note": self.note,
        }


@dataclass(frozen=True)
class Receipt:
    line: int
    receipt_id: str
    invoice_id: str
    customer_id: str
    pay_date: date
    known: datetime
    paid_cents: int
    ref: str
    note: str = ""

    def public(self):
        return {
            "line": self.line, "receipt_id": self.receipt_id, "invoice_id": self.invoice_id,
            "customer_id": self.customer_id, "pay_date": self.pay_date.isoformat(),
            "known": utc_text(self.known), "paid_cents": self.paid_cents,
            "ref": self.ref, "note": self.note,
        }


@dataclass
class Stream:
    stream: str
    encoding: str
    terms: list
    receipts: list
    quarantine: list
    duplicates: list
    blanks: int
    headers: int
    input_rows: int
