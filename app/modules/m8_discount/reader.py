import csv
import io
from dataclasses import replace

from app.config import MAX_ROWS
from app.errors import Problem
from app.modules.m1_intake.contract import RowError
from app.modules.m1_intake.reader import decode, isolate
from app.modules.m8_discount.contract import CONTRACTS
from app.modules.m8_discount.records import Stream


def read_stream(content: bytes, stream: str) -> Stream:
    columns, normalize, bucket = CONTRACTS[stream]
    text, encoding = decode(content)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    result = Stream(stream, encoding, [], [], [], [], 0, 0, 0)
    header = None
    candidates = {}
    line = 1
    try:
        while True:
            line = reader.line_num + 1
            raw = next(reader, None)
            if raw is None:
                break
            names = tuple(piece.strip().lstrip("﻿") for piece in raw)
            if header is None:
                if not any(names):
                    continue
                if len(names) != len(columns) or set(names) != set(columns):
                    raise Problem("INVALID_HEADER", f"第 {line} 行缺列、重列或包含未知列")
                header = names
                continue
            result.input_rows += 1
            if result.input_rows > MAX_ROWS:
                raise Problem("TOO_MANY_ROWS", "最多接收 5000 条表头后记录", 413)
            if not any(names):
                result.blanks += 1
            elif names == header:
                result.headers += 1
            elif len(raw) != len(header):
                result.quarantine.append(isolate(line, raw, "ROW_WIDTH"))
            else:
                row = dict(zip(header, raw))
                try:
                    record = normalize(line, row)
                except RowError as exc:
                    result.quarantine.append(isolate(line, row, str(exc), _record_id(row, stream)))
                    continue
                key = (record_id(record), record.known)
                candidates.setdefault(key, []).append((record, row))
    except csv.Error as exc:
        raise Problem("MALFORMED_CSV", f"第 {line} 行 CSV 记录损坏") from exc
    if header is None:
        raise Problem("BLANK_FILE", "文件仅包含空行")
    if result.input_rows == result.headers + result.blanks:
        raise Problem("NO_RECORDS", "表头后没有业务记录")
    accepted = []
    for (record_key, _), rows in candidates.items():
        first = rows[0][0]
        if any(replace(record, line=first.line) != first for record, _ in rows[1:]):
            result.quarantine.extend(isolate(record.line, row, "EVENT_ID_CONFLICT", record_key)
                                     for record, row in rows)
        else:
            accepted.append(first)
            result.duplicates.extend(isolate(record.line, row, "DUPLICATE_EVENT", record_key)
                                     for record, row in rows[1:])
    accepted.sort(key=lambda item: item.line)
    result.quarantine.sort(key=lambda item: item["line"])
    result.duplicates.sort(key=lambda item: item["line"])
    setattr(result, bucket, accepted)
    return result


def record_id(record):
    return record.term_id if hasattr(record, "term_id") else record.receipt_id


def _record_id(row, stream):
    return row.get("term_id") if stream == "term" else row.get("receipt_id")
