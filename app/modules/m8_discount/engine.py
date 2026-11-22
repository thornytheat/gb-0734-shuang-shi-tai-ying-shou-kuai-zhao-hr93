from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from app.modules.m2_versions.rules import build as build_ar_book
from app.modules.m8_discount.term_rules import build_term_book
from app.reasons import reason


def discount_cents(face_cents: int, rate_bp: int) -> int:
    """整数分金额按基点费率计算折扣，用 ROUND_HALF_UP 消除半分钱的二进制舍入误差。"""
    return int((Decimal(face_cents) * Decimal(rate_bp) / Decimal(10_000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def _base_row(receipt, ar_book):
    target = ar_book.accepted_versions.get(receipt.ref)
    return {
        "receipt": receipt.public(),
        "target": target.public() if target else None,
        "term": None, "window_start": None, "window_end": None,
        "face_cents": 0, "expected_discount_cents": 0, "expected_net_cents": 0,
        "discount_cents": 0, "applied_cents": 0,
    }


def _reject(row, code, extra=None):
    payload = {**row, **reason(code)}
    if extra:
        payload.update(extra)
    return payload


def evaluate(ar_events, terms, receipts, known_cut):
    """在统一知悉切点下重算该批回单的折扣结算。"""
    ar_book = build_ar_book(ar_events, known_cut)
    term_book = build_term_book(terms, known_cut)

    applied_rows, rejected = [], []
    spent = defaultdict(int)
    visible = sorted((r for r in receipts if r.known <= known_cut),
                     key=lambda r: (r.pay_date, r.known, r.receipt_id, r.line))
    for receipt in visible:
        row = _base_row(receipt, ar_book)
        target = ar_book.accepted_versions.get(receipt.ref)
        if target is None:
            rejected.append(_reject(row, "TERM_TARGET_INVALID"
                                    if receipt.ref in ar_book.visible else "TERM_TARGET_PENDING"))
            continue
        if (target.invoice_id, target.customer_id) != (receipt.invoice_id, receipt.customer_id):
            rejected.append(_reject(row, "TERM_TARGET_SCOPE"))
            continue
        at_pay = ar_book.active(receipt.invoice_id, receipt.pay_date)
        if at_pay is None:
            rejected.append(_reject(row, "NO_ACTIVE_VERSION"))
            continue
        term = term_book.term_at(receipt.invoice_id, receipt.known)
        face = at_pay.cents
        row["face_cents"] = face
        remaining = face - spent[receipt.invoice_id]
        if term is None or term.customer_id != receipt.customer_id:
            # 知悉时点没有条款：不得确认折扣，实收须正好等于票面（在可冲减余额内）。
            if receipt.paid_cents <= remaining and receipt.paid_cents == face:
                row["applied_cents"] = receipt.paid_cents
                spent[receipt.invoice_id] += receipt.paid_cents
                applied_rows.append(row)
            else:
                rejected.append(_reject(row, "DISCOUNT_MISMATCH",
                                        {"available_cents": max(0, remaining)}))
            continue
        window_start = term.effective
        window_end = term.effective.fromordinal(term.effective.toordinal() + term.window_days)
        row.update({"term": term.public(), "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat()})
        in_window = window_start <= receipt.pay_date <= window_end
        expected_discount = discount_cents(face, term.rate_bp)
        expected_net = face - expected_discount
        row.update({"expected_discount_cents": expected_discount,
                    "expected_net_cents": expected_net})
        if not in_window:
            # 窗口外不享折扣，实收须等于票面。
            if receipt.paid_cents <= remaining and receipt.paid_cents == face:
                row["applied_cents"] = receipt.paid_cents
                spent[receipt.invoice_id] += receipt.paid_cents
                applied_rows.append(row)
            else:
                rejected.append(_reject(row, "DISCOUNT_MISMATCH",
                                        {"available_cents": max(0, remaining)}))
            continue
        if receipt.paid_cents > face or receipt.paid_cents > remaining:
            rejected.append(_reject(row, "EXCEEDS_BALANCE",
                                    {"available_cents": max(0, remaining)}))
            continue
        if receipt.paid_cents == expected_net:
            row["discount_cents"] = expected_discount
            row["applied_cents"] = expected_net
            spent[receipt.invoice_id] += expected_net
            applied_rows.append(row)
        elif receipt.paid_cents == face:
            # 折扣窗口内却按票面全额付款，折扣无法解释，推迟发布。
            rejected.append(_reject(row, "DISCOUNT_WINDOW_MISS"))
        elif receipt.paid_cents < face:
            # 窗口内的部分回款无法确认整笔折扣。
            rejected.append(_reject(row, "DISCOUNT_PARTIAL",
                                    {"expected_net_cents": expected_net}))
        else:
            rejected.append(_reject(row, "DISCOUNT_MISMATCH",
                                    {"expected_net_cents": expected_net}))
    return ar_book, term_book, applied_rows, rejected
