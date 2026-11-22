from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field

from app.reasons import reason


@dataclass
class TermBook:
    visible: dict = field(default_factory=dict)
    accepted: dict = field(default_factory=dict)
    chain_by_invoice: dict = field(default_factory=lambda: defaultdict(list))
    known_starts: dict = field(default_factory=dict)
    replaced_by: dict = field(default_factory=dict)
    rejected: list = field(default_factory=list)

    def term_at(self, invoice: str, known):
        """按回单知悉时点取当时已知的最新条款版本（知识轴截面）。"""
        starts = self.known_starts.get(invoice)
        if not starts:
            return None
        index = bisect_right(starts, known) - 1
        if index < 0:
            return None
        return self.chain_by_invoice[invoice][index]

    def reject(self, record, code: str, related=()):
        self.rejected.append({"term": record.public(), **reason(code),
                              "related_count": len(related),
                              "related": [item.public() for item in list(related)[:5]]})
