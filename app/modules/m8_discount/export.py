from fastapi import Response

from app.errors import Problem
from app.evidence import canonical, digest
from app.modules.m8_discount.service import read_run


def export_evidence(store, run_id):
    stored = read_run(store, run_id)
    if digest(canonical(stored["result"])) != stored["manifest"]["result_digest"]:
        raise Problem("SETTLEMENT_MISMATCH", "结算证据摘要不符", 409)
    return Response(canonical(stored), media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="discount-settlement-{run_id[:12]}.json"'})
