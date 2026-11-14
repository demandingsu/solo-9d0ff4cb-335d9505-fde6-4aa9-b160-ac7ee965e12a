"""FastAPI entry point for the assembly/repair traceability service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import DB_PATH, PRODUCT_PREFIX
from .csvparse import CsvError, parse_csv
from .database import get_conn, init_db
from .replay import Event, trace_batch
from .schemas import (
    BatchOut,
    EventOut,
    Health,
    RejectedUpload,
    TraceResponse,
    UploadResponse,
)
from .service import UploadRejected, upload_events
from .timeutil import parse_utc_or_400, to_iso, to_micros


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="电控板装配追溯 API",
    description=(
        "上传装配/返修事件 CSV（装入 INSTALL / 拆出 REMOVE），"
        "按子件批次与 UTC 时刻追溯仍装有该批次板件的成品（P- 开头序列号）"
        "及多级装配事件路径。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=Health, tags=["ops"])
def health() -> Health:
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
    return Health(status="ok", database=str(DB_PATH), event_count=count)


@app.post(
    "/uploads",
    response_model=UploadResponse,
    status_code=201,
    tags=["upload"],
    responses={400: {"model": RejectedUpload}, 422: {"model": RejectedUpload}},
    summary="上传装配/返修事件 CSV",
)
def upload(file: UploadFile = File(..., description="UTF-8 编码的事件 CSV")) -> UploadResponse:
    raw = file.file.read()
    try:
        incoming = parse_csv(raw)
    except CsvError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "accepted": False,
                "filename": file.filename,
                "received_events": 0,
                "reason": "CSV parsing failed",
                "errors": [str(exc)],
            },
        ) from exc

    try:
        summary = upload_events(file.filename or "upload.csv", incoming)
    except UploadRejected as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "accepted": False,
                "filename": file.filename,
                "received_events": len(incoming),
                "reason": exc.reason,
                "errors": exc.errors,
            },
        ) from exc
    return UploadResponse(**summary)


@app.get(
    "/trace",
    response_model=TraceResponse,
    tags=["trace"],
    summary="按批次与 UTC 时刻追溯受影响成品",
)
def trace(
    batch: str = Query(..., description="子件批次，如 B20261101"),
    at_time: str = Query(
        ...,
        alias="at_time",
        description="UTC 时刻（ISO-8601，如 2026-11-01T09:30:00Z）",
    ),
    child_part_no: str = Query(
        ..., description="子件号（板件物料号），如 EC-100"
    ),
) -> TraceResponse:
    dt = parse_utc_or_400(at_time)
    at_us = to_micros(dt)

    # A single deferred transaction gives a point-in-time snapshot: uploads
    # committing mid-query are invisible until the next request, so the answer
    # always reflects one complete log version.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_no, event_us, event_ts, parent_part_no, "
            "child_part_no, child_batch, action FROM events"
        ).fetchall()
        events = [Event(**dict(r)) for r in rows]

    result = trace_batch(events, child_part_no, batch, at_us)
    products = result["products"]
    return TraceResponse(
        queried_at=to_iso(dt),
        child_part_no=child_part_no,
        child_batch=batch,
        affected_products=products,
        open_chains=result["open_chains"],
        total_affected_products=len(products),
    )


@app.get("/events", response_model=list[EventOut], tags=["query"], summary="列出已接收事件")
def list_events(limit: int = Query(500, ge=1, le=5000)) -> list[EventOut]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_no, event_ts, parent_part_no, child_part_no, "
            "child_batch, action, upload_batch FROM events "
            "ORDER BY event_us, event_no LIMIT ?",
            (limit,),
        ).fetchall()
    return [EventOut(**dict(r)) for r in rows]


@app.get(
    "/uploads",
    response_model=list[BatchOut],
    tags=["upload"],
    summary="上传批次审计记录",
)
def list_uploads() -> list[BatchOut]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, received_at, accepted, new_events, "
            "duplicate_events, detail FROM upload_batches ORDER BY id"
        ).fetchall()
    return [BatchOut(**dict(r)) for r in rows]


@app.get("/help", include_in_schema=False, response_class=HTMLResponse)
def help_page() -> str:
    return f"""
    <html><head><meta charset="utf-8"><title>追溯 API 演示指引</title>
    <style>body{{font-family:sans-serif;max-width:900px;margin:2rem auto;line-height:1.6}}
    code,pre{{background:#f4f4f4;padding:2px 6px;border-radius:4px}}
    pre{{padding:12px;overflow:auto}}</style></head><body>
    <h1>电控板装配追溯 API</h1>
    <p>成品序列号以 <code>{PRODUCT_PREFIX}</code> 开头。交互式文档见 <a href="/docs">/docs</a>。</p>
    <h2>1. 上传 CSV（POST /uploads）</h2>
    <pre>事件号,UTC时间,父件号,子件号,子件批次,动作
E001,2026-11-01T08:00:00Z,SA-200,EC-100,B20261101,装入
E002,2026-11-01T09:00:00Z,P-1001,SA-200,LOT-SA1,装入
E003,2026-11-01T10:00:00Z,SA-200,EC-100,B20261101,拆出</pre>
    <h2>2. 追溯（GET /trace）</h2>
    <p><code>/trace?child_part_no=EC-100&amp;batch=B20261101&amp;at_time=2026-11-01T09:30:00Z</code></p>
    <p>拆出时刻之后的查询不再命中该成品；补录的较早事件会与全量日志一起按事件时间重算校验。</p>
    </body></html>
    """
