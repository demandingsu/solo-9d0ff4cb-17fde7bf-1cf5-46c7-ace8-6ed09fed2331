"""FastAPI 应用：采集触发与样品结果查询。"""
from __future__ import annotations

import json
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db
from .collector import collect


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="仪器邮件 CSV 采集服务",
    description=(
        "从 IMAP 邮箱采集仪器 CSV 附件并入库，支持按样品编号查询检测结果。\n\n"
        "可用接口：\n"
        "- `POST /collect` 触发一轮采集（并发投递的多封邮件各自入库一次）\n"
        "- `GET /results/{sample_no}` 按样品编号查询结果\n"
        "- `GET /deliveries`、`GET /deliveries/{message_id}` 查询邮件来源\n"
        "- `GET /ingest/state`、`GET /ingest/deliveries` 查询采集进度与投递\n"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def _delivery_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    names = item.get("attachment_names")
    if isinstance(names, str):
        try:
            item["attachment_names"] = json.loads(names)
        except ValueError:
            item["attachment_names"] = []
    item["source"] = {
        "message_id": item.get("message_id"),
        "subject": item.get("subject"),
        "from": item.get("sender"),
        "to": item.get("recipient"),
        "date": item.get("message_date"),
        "mailbox_key": item.get("mailbox_key"),
        "uidvalidity": item.get("uidvalidity"),
        "uid": item.get("first_uid"),
    }
    return item


def _results_dict(row: Any) -> dict[str, Any]:
    return dict(row)


class CollectRequest(BaseModel):
    host: str = Field(..., description="IMAP 服务器地址")
    port: int = Field(..., ge=1, le=65535, description="IMAP 端口")
    folder: str = Field(..., description="邮箱文件夹，如 INBOX")
    username: str
    password: str
    use_ssl: bool = Field(False, description="是否使用 IMAP over SSL")


class CollectResponse(BaseModel):
    status: str
    mailbox_key: str
    folder: str
    uidvalidity: int
    resume_from_uid: int
    scanned_uids: list[int]
    ingested: list[dict[str, Any]]
    skipped_duplicates: list[dict[str, Any]]
    stopped: dict[str, Any] | None
    total_new_records: int


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/collect", response_model=CollectResponse)
def trigger_collect(req: CollectRequest) -> dict[str, Any]:
    report = collect(
        host=req.host,
        port=req.port,
        folder=req.folder,
        username=req.username,
        password=req.password,
        use_ssl=req.use_ssl,
    )
    return report.to_dict()


@app.get("/results/{sample_no}")
def get_results(sample_no: str) -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.query_results_by_sample(conn, sample_no)
        deliveries = {
            (row["mailbox_key"], row["message_id"]): _delivery_dict(row)
            for row in db.list_deliveries(conn)
        }
    if not rows:
        raise HTTPException(status_code=404, detail=f"未找到样品 {sample_no} 的结果")
    results = []
    for row in rows:
        item = _results_dict(row)
        delivery = deliveries.get((item["mailbox_key"], item["message_id"]))
        item["source"] = delivery["source"] if delivery else None
        results.append(item)
    return {"sample_no": sample_no, "count": len(rows), "results": results}


@app.get("/samples")
def get_samples() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_samples(conn)
    return {"count": len(rows), "samples": [dict(row) for row in rows]}


@app.get("/ingest/state")
def get_ingest_state() -> dict[str, Any]:
    with db.connect() as conn:
        states = [
            dict(row)
            for row in conn.execute("SELECT * FROM ingest_state ORDER BY mailbox_key")
        ]
    return {"count": len(states), "states": states}


@app.get("/ingest/deliveries")
def get_deliveries() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_deliveries(conn)
    return {
        "count": len(rows),
        "deliveries": [_delivery_dict(row) for row in rows],
    }


@app.get("/deliveries")
def list_delivery_sources() -> dict[str, Any]:
    """列出已入库邮件的来源（Message-ID、主题、发件人、附件、UID）。"""
    return get_deliveries()


@app.get("/deliveries/{message_id}")
def get_delivery_source(message_id: str, mailbox_key: str | None = None) -> dict[str, Any]:
    """按 Message-ID 查询某封邮件的来源及其入库的检测记录。"""
    with db.connect() as conn:
        row = db.find_delivery_detail(conn, message_id, mailbox_key)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"未找到 Message-ID {message_id} 的投递记录",
            )
        results = db.query_results_by_message(conn, message_id)
    delivery = _delivery_dict(row)
    if mailbox_key is not None:
        results = [
            item for item in results
            if item["mailbox_key"] == delivery["mailbox_key"]
        ]
    return {
        "message_id": message_id,
        "source": delivery["source"],
        "attachment_names": delivery["attachment_names"],
        "attachment_digest": delivery["attachment_digest"],
        "ingested_at": delivery["ingested_at"],
        "record_count": len(results),
        "results": [_results_dict(result) for result in results],
    }


@app.post("/admin/reset")
def reset_database() -> dict[str, str]:
    """清空全部业务数据（演示/测试用，生产环境请经网关限制访问）。"""
    db.reset_all(db.get_db_path())
    return {"status": "reset"}
