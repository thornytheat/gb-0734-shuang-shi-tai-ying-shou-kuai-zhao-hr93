from collections import defaultdict
from itertools import groupby

from app.modules.m8_discount.term_book import TermBook


def _append_chain(book: TermBook, invoice, term):
    chain = book.chain_by_invoice[invoice]
    chain.append(term)
    chain.sort(key=lambda item: item.known)
    book.known_starts[invoice] = [item.known for item in chain]


def _apply(book: TermBook, invoice, cohort):
    revisions = defaultdict(list)
    roots = []
    for term in cohort:
        if term.ref:
            parent = book.accepted.get(term.ref)
            if parent is None:
                visible = book.visible.get(term.ref)
                code = ("TERM_SCOPE" if visible and (visible.invoice_id != invoice
                                                      or visible.customer_id != term.customer_id)
                        else "TERM_PARENT")
                book.reject(term, code, [visible] if visible else [])
                continue
            if (parent.invoice_id, parent.customer_id) != (invoice, term.customer_id):
                book.reject(term, "TERM_SCOPE", [parent])
                continue
            revisions[parent].append(term)
        else:
            roots.append(term)
    for parent, siblings in revisions.items():
        if len(siblings) > 1:
            for term in siblings:
                book.reject(term, "TERM_FORK",
                            [parent, *(other for other in siblings if other != term)])
            continue
        term = siblings[0]
        book.accepted[term.term_id] = term
        book.replaced_by[parent.term_id] = term
        _append_chain(book, invoice, term)
    for term in roots:
        chain = book.chain_by_invoice.get(invoice)
        if chain:
            book.reject(term, "TERM_ROOT_CONFLICT", [chain[-1]])
            continue
        book.accepted[term.term_id] = term
        _append_chain(book, invoice, term)


def build_term_book(terms, known):
    """按知悉轴构建条款版本册，只保留 known <= 切点 的条款。"""
    book = TermBook()
    ordered = sorted((term for term in terms if term.known <= known),
                     key=lambda term: (term.known, term.term_id, term.line))
    for _, group in groupby(ordered, key=lambda term: term.known):
        invoices = defaultdict(list)
        for term in group:
            if term.term_id in book.visible:
                book.reject(term, "EVENT_ID_CONFLICT", [book.visible[term.term_id]])
                continue
            book.visible[term.term_id] = term
            invoices[term.invoice_id].append(term)
        for invoice, cohort in sorted(invoices.items()):
            _apply(book, invoice, cohort)
    return book
