import os
import threading

from fastapi.testclient import TestClient

from app.main import app
from imap_server import mailbox
from tests.conftest import PASSWORD, USER

PAYLOAD = {
    "folder": "INBOX",
    "username": USER,
    "password": PASSWORD,
    "use_ssl": False,
}


def _message(message_id: str, sample_no: str) -> bytes:
    csv = (
        "样品编号,检测项目,结果,单位\r\n"
        f"{sample_no},铅Pb,0.021,mg/kg\r\n"
    ).encode("utf-8")
    return mailbox.build_message(
        message_id=message_id,
        subject=f"仪器结果 {sample_no}",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[(f"{sample_no}.csv", csv)],
    )


def test_collect_exposes_mail_source(imap_server, db_path):
    host, port, _root = imap_server
    client = TestClient(app)
    resp = client.post("/collect", json=dict(PAYLOAD, host=host, port=port))
    assert resp.status_code == 200

    ingested = resp.json()["ingested"]
    assert ingested
    first = ingested[0]
    assert first["message_id"] == "inst-0001@lab-instrument"
    assert first["subject"] == "仪器结果 批次 B1"
    assert first["sender"] == "instrument-a@lab.local"
    assert first["recipient"] == "lab@lab.local"
    assert first["attachments"] == ["batch_b1.csv"]

    # 结果里带出来源，便于从样品追溯到邮件
    result = client.get("/results/S-1001").json()["results"][0]
    assert result["source"]["message_id"] == "inst-0001@lab-instrument"
    assert result["source"]["uid"] == 1

    sources = client.get("/deliveries").json()
    assert sources["count"] == 2
    assert all(item["source"]["from"] for item in sources["deliveries"])

    detail = client.get("/deliveries/inst-0001@lab-instrument")
    assert detail.status_code == 200
    body = detail.json()
    assert body["record_count"] == 3
    assert body["attachment_names"] == ["batch_b1.csv"]
    assert body["source"]["subject"] == "仪器结果 批次 B1"

    assert client.get("/deliveries/no-such@lab").status_code == 404


def test_docs_available(imap_server, db_path):
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path in ["/collect", "/results/{sample_no}", "/deliveries",
                 "/deliveries/{message_id}"]:
        assert path in paths


def test_concurrent_deliveries_ingested_once_through_api(imap_server, db_path):
    host, port, root = imap_server
    outcomes = []

    def deliver(index: int) -> None:
        raw = _message(f"conc-api-{index}@lab", f"S-API{index}")
        uid = mailbox.append_message(root, "INBOX", raw)
        outcomes.append((uid, index))

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    uids = [uid for uid, _ in outcomes]
    assert len(set(uids)) == 8

    # 移除种子坏邮件后采集（种子 UID1/2 与本轮 8 封一起入库）
    os.remove(f"{mailbox.folder_dir(root, 'INBOX')}/3.eml")
    client = TestClient(app)
    body = client.post("/collect", json=dict(PAYLOAD, host=host, port=port)).json()
    assert body["status"] == "completed"
    assert body["total_new_records"] == 5 + 8

    delivered = {uid: index for uid, index in outcomes}
    ingested = {item["uid"]: item for item in body["ingested"]}
    assert set(delivered).issubset(ingested)
    for uid, index in delivered.items():
        assert ingested[uid]["message_id"] == f"conc-api-{index}@lab"

    # 每封邮件的来源都能从 /docs 对应接口查到
    for index in range(8):
        detail = client.get(f"/deliveries/conc-api-{index}@lab")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["record_count"] == 1
        assert payload["source"]["uid"] == next(
            uid for uid, i in outcomes if i == index
        )
        results = client.get(f"/results/S-API{index}").json()
        assert results["count"] == 1

    # 幂等：不重复入库
    again = client.post("/collect", json=dict(PAYLOAD, host=host, port=port)).json()
    assert again["ingested"] == []
    assert client.get("/results/S-API0").json()["count"] == 1
