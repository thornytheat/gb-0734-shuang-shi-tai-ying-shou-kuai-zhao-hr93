from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

from app.clock import parse_known
from app.config import MAX_BYTES
from app.errors import Problem
from app.modules.m8_discount import repository, service
from app.modules.m8_discount.export import export_evidence
from app.store import Store, get_store

router = APIRouter(prefix="/api/settlement", tags=["折扣结算"])


class PublishRequest(BaseModel):
    ar_source_id: str
    term_source_id: str
    receipt_source_id: str
    known: str

    def cutoff(self):
        try:
            return parse_known(self.known)
        except ValueError as exc:
            raise Problem("INVALID_CUT", str(exc)) from exc


def _preview_params(ar_source_id: str, term_source_id: str, receipt_source_id: str, known: str):
    try:
        return ar_source_id, term_source_id, receipt_source_id, parse_known(known)
    except ValueError as exc:
        raise Problem("INVALID_CUT", str(exc)) from exc


@router.post("/sources/{stream}")
def upload_stream(stream: str, file: UploadFile = File(...), store: Store = Depends(get_store)):
    if stream not in service.STREAMS:
        raise Problem("UNKNOWN_STREAM", "来源类型须为 term 或 receipt", 404)
    return service.ingest_stream(store, file.file.read(MAX_BYTES + 1), stream)


@router.get("/sources/{stream}/{source_id}")
def read_source(stream: str, source_id: str, store: Store = Depends(get_store)):
    if stream not in service.STREAMS:
        raise Problem("UNKNOWN_STREAM", "来源类型须为 term 或 receipt", 404)
    return service.summarize_stream(source_id, service.load_stream(store, source_id, stream))


@router.get("/preview")
def preview(ar_source_id: str, term_source_id: str, receipt_source_id: str, known: str,
            store: Store = Depends(get_store)):
    params = _preview_params(ar_source_id, term_source_id, receipt_source_id, known)
    return service.build_snapshot(store, *params)


@router.post("/publish")
def publish(request: PublishRequest, store: Store = Depends(get_store)):
    return service.publish(store, request.ar_source_id, request.term_source_id,
                           request.receipt_source_id, request.cutoff())


@router.get("")
def history(ar_source_id: str | None = None, store: Store = Depends(get_store)):
    with store.connect() as connection:
        return {"runs": repository.history(connection, ar_source_id)}


@router.post("/{run_id}/rerun")
def rerun(run_id: str, store: Store = Depends(get_store)):
    return service.rerun(store, run_id)


@router.get("/{run_id}/export")
def export(run_id: str, store: Store = Depends(get_store)):
    return export_evidence(store, run_id)
