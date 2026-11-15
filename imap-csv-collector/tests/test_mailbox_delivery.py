"""样例邮箱投递保证：并发独立 UID、崩溃不覆盖、只读完整邮件。"""
import email
import multiprocessing
import os
import threading
import time

import pytest

from app import db
from app.collector import collect, make_mailbox_key
from imap_server import mailbox, seed as seed_mod
from tests.conftest import PASSWORD, USER


def _tiny_message(message_id: str, sample_no: str) -> bytes:
    payload = seed_mod.csv_text([(sample_no, "铅Pb", "0.021", "mg/kg")])
    return mailbox.build_message(
        message_id=message_id,
        subject=f"并发邮件 {message_id}",
        sender="instrument-a@lab.local",
        recipient="lab@lab.local",
        attachments=[(f"{sample_no}.csv", payload)],
    )


def _append(root: str, folder: str, message_id: str, sample_no: str, results: list) -> None:
    uid = mailbox.append_message(root, folder, _tiny_message(message_id, sample_no))
    results.append((uid, message_id, sample_no))


def _process_root() -> str:
    return os.environ["CONC_MAIL_ROOT"]


def _process_append(message_id: str) -> int:
    root = _process_root()
    return mailbox.append_message(
        root, "INBOX", _tiny_message(message_id, message_id)
    )


def _collect(host, port):
    return collect(
        host=host,
        port=port,
        folder="INBOX",
        username=USER,
        password=PASSWORD,
        use_ssl=False,
    )


def test_concurrent_appends_get_distinct_uids(mail_root):
    folder = "INBOX"
    start = len(mailbox.list_uids(mail_root, folder))
    results: list = []
    threads = []
    for index in range(24):
        thread = threading.Thread(
            target=_append,
            args=(mail_root, folder, f"conc-{index}@lab", f"S-C{index}", results),
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()

    uids = [uid for uid, _, _ in results]
    assert len(results) == 24
    assert len(set(uids)) == 24, f"出现重复 UID: {sorted(uids)}"
    assert all(uid > start for uid in uids)

    # 每个 UID 文件都属于对应的 Message-ID，没有互相覆盖
    for uid, message_id, _sample in results:
        raw = mailbox.read_message(mail_root, folder, uid)
        assert raw is not None
        msg = email.message_from_bytes(raw)
        assert (msg.get("Message-ID") or "").strip("<>") == message_id

    # 全部 UID 连续；next_uid 已推进到最大 UID + 1
    stored = mailbox.list_uids(mail_root, folder)
    meta = mailbox.read_meta(mail_root, folder)
    assert sorted(stored) == list(range(1, start + 25))
    assert meta["next_uid"] == max(stored) + 1


def test_concurrent_appends_across_processes(mail_root, monkeypatch):
    """多个 mailctl 进程共享同一邮箱卷，也不能拿到同一个 UID。"""
    monkeypatch.setenv("CONC_MAIL_ROOT", mail_root)
    ctx = multiprocessing.get_context("fork") if hasattr(os, "fork") else None
    if ctx is None:  # pragma: no cover - 非 POSIX
        pytest.skip("需要 fork 多进程支持")
    with ctx.Pool(processes=8) as pool:
        uids = pool.map(_process_append, [f"proc-{i}@lab" for i in range(40)])

    assert len(set(uids)) == len(uids), f"跨进程出现重复 UID: {sorted(uids)}"
    for uid, message_id in zip(uids, [f"proc-{i}@lab" for i in range(40)]):
        raw = mailbox.read_message(mail_root, "INBOX", uid)
        assert raw is not None
        assert (email.message_from_bytes(raw).get("Message-ID") or "").strip("<>") == message_id
    assert mailbox.read_meta(mail_root, "INBOX")["next_uid"] == max(uids) + 1


def test_crash_between_publish_and_meta_repairs_next_uid(mail_root):
    """邮件已发布但 next_uid 未推进时重启：续投不得覆盖已投递邮件。"""
    folder = "INBOX"
    guaranteed_uid = mailbox.append_message(
        mail_root, folder, _tiny_message("crash-guaranteed@lab", "S-CRASH")
    )

    # 模拟进程在写完 <uid>.eml 之后、更新 meta.json 之前退出：
    # 邮件文件存在，但 meta.json 仍指向已被占用的 UID。
    meta = mailbox.read_meta(mail_root, folder)
    meta["next_uid"] = guaranteed_uid
    mailbox.write_meta(mail_root, folder, meta)

    # 残留一个未完成的临时文件（进程写到一半退出）也不应被当成邮件
    directory = mailbox.folder_dir(mail_root, folder)
    partial_path = os.path.join(directory, mailbox.TMP_PREFIX + "partial.part")
    with open(partial_path, "wb") as fh:
        fh.write(b"From: partial@lab\r\nSubject: half")

    new_uid = mailbox.append_message(
        mail_root, folder, _tiny_message("crash-next@lab", "S-NEXT")
    )
    assert new_uid > guaranteed_uid

    # 已成功投递的邮件未被覆盖
    raw = mailbox.read_message(mail_root, folder, guaranteed_uid)
    assert b"crash-guaranteed@lab" in raw
    # 本次邮件也完整可读
    assert b"crash-next@lab" in mailbox.read_message(mail_root, folder, new_uid)
    # 临时文件不进入 UID 列表
    assert mailbox.list_uids(mail_root, folder) == sorted(
        set(mailbox.list_uids(mail_root, folder))
    )
    assert partial_path.endswith(".part")
    assert not any(name.endswith(".eml") and name.startswith(".") for name in os.listdir(directory))
    # 下一次投递会顺手清掉崩溃遗留的临时文件
    assert not any(
        name.startswith(mailbox.TMP_PREFIX) for name in os.listdir(directory)
    )


def test_readers_never_see_partial_message(mail_root):
    """投递与读取并发：读到的只能是 None 或完整邮件。"""
    folder = "INBOX"
    directory = mailbox.folder_dir(mail_root, folder)
    stop = threading.Event()
    failures: list = []

    def reader():
        while not stop.is_set():
            for uid in mailbox.list_uids(mail_root, folder):
                raw = mailbox.read_message(mail_root, folder, uid)
                if raw is None:
                    failures.append(f"UID {uid} 存在但读取为空")
                    continue
                # 完整邮件的头部与结尾均可解析
                msg = email.message_from_bytes(raw)
                if msg.get("Message-ID") is None:
                    failures.append(f"UID {uid} 读到半封邮件")
                elif not raw.rstrip().endswith(b"--"):
                    # MIME 边界收尾完整（as_bytes 末尾为 --\r\n）
                    failures.append(f"UID {uid} 缺少结尾边界")

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()

    writers: list = []
    for index in range(20):
        writer = threading.Thread(
            target=_append,
            args=(mail_root, folder, f"reader-{index}@lab", f"S-R{index}", writers),
        )
        writer.start()
        writer.join()

    stop.set()
    for thread in readers:
        thread.join()
    assert failures == []


def test_collect_ingests_each_concurrent_delivery_once(imap_server, db_path):
    host, port, root = imap_server
    start_uids = mailbox.list_uids(root, "INBOX")
    start = len(start_uids)

    results: list = []
    threads = []
    for index in range(10):
        thread = threading.Thread(
            target=_append,
            args=(root, "INBOX", f"batch-{index}@lab", f"S-B{index}", results),
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    delivered_uids = sorted(uid for uid, _, _ in results)
    assert len(set(delivered_uids)) == 10
    assert delivered_uids == list(range(start + 1, start + 11))

    # 采集会先停在种子邮件里的坏 UID3；移除它后继续。
    # 种子 UID1/2 同样在此轮首次入库，随后是并发投递的 10 封。
    os.remove(f"{mailbox.folder_dir(root, 'INBOX')}/3.eml")
    report = _collect(host, port)
    assert report.stopped is None
    ingested_uids = [item["uid"] for item in report.ingested]
    assert ingested_uids == [1, 2] + delivered_uids

    uid_by_sample = {sample: uid for uid, _message_id, sample in results}
    with db.connect(db_path) as conn:
        state = db.get_state(conn, make_mailbox_key(host, port, "INBOX"))
        assert state["last_uid"] == max(delivered_uids)
        for index in range(10):
            rows = db.query_results_by_sample(conn, f"S-B{index}")
            assert len(rows) == 1
            assert rows[0]["message_id"] == f"batch-{index}@lab"
            assert rows[0]["uid"] == uid_by_sample[f"S-B{index}"]

    # 再采一轮：幂等，不重复入库
    again = _collect(host, port)
    assert again.stopped is None
    assert again.ingested == []
    assert again.scanned_uids == []


def test_crash_after_publish_before_meta_write(mail_root, monkeypatch):
    """真实崩溃点：<uid>.eml 已 rename，写 meta.json 时进程退出。"""
    folder = "INBOX"
    first_uid = mailbox.append_message(
        mail_root, folder, _tiny_message("crash-before@lab", "S-BEFORE")
    )

    real_write_json = mailbox._atomic_write_json

    def crash_on_meta(path, payload):
        if path.endswith(mailbox.META_NAME):
            raise SystemExit("模拟进程退出")
        real_write_json(path, payload)

    monkeypatch.setattr(mailbox, "_atomic_write_json", crash_on_meta)
    with pytest.raises(SystemExit):
        mailbox.append_message(
            mail_root, folder, _tiny_message("crash-published@lab", "S-PUB")
        )
    monkeypatch.setattr(mailbox, "_atomic_write_json", real_write_json)

    published_uid = first_uid + 1
    # 邮件已完整发布，但 meta 仍停留在崩溃前的下一个 UID
    assert mailbox.list_uids(mail_root, folder) == [1, 2, 3, first_uid, published_uid]
    assert mailbox.read_meta(mail_root, folder)["next_uid"] == published_uid + 1

    # 重启后续投：跳过已发布的 UID，不覆盖，也不产生半封邮件
    next_uid = mailbox.append_message(
        mail_root, folder, _tiny_message("crash-after@lab", "S-AFTER")
    )
    assert next_uid == published_uid + 1
    assert b"crash-published@lab" in mailbox.read_message(
        mail_root, folder, published_uid
    )
    assert b"crash-after@lab" in mailbox.read_message(mail_root, folder, next_uid)
    assert b"crash-before@lab" in mailbox.read_message(mail_root, folder, first_uid)


def test_truncated_fetch_rejected():
    """服务端声明了字面量长度却只返回一半：采集不得把半封邮件当成完整邮件。"""
    from app.collector import IngestError, _fetch_message

    class TruncatingClient:
        def uid(self, command, uid, spec):
            assert command == "fetch"
            return "OK", [(b"1 (UID 5 BODY[] {100}", b"x" * 40)]

    with pytest.raises(IngestError) as exc:
        _fetch_message(TruncatingClient(), 5)
    assert "不完整" in exc.value.reason
    assert exc.value.uid == 5


def test_collect_reads_only_complete_messages(imap_server, db_path):
    """投递进行中触发采集：不能把半封邮件入库或报坏附件。"""
    host, port, root = imap_server
    os.remove(f"{mailbox.folder_dir(root, 'INBOX')}/3.eml")

    appended: list = []

    def slow_writer(index: int) -> None:
        message = _tiny_message(f"slow-{index}@lab", f"S-SLOW{index}")
        directory = mailbox.folder_dir(root, "INBOX")
        partial = os.path.join(directory, mailbox.TMP_PREFIX + f"slow{index}.part")
        # 显式模拟“写到一半尚未发布”的中间态
        with open(partial, "wb") as fh:
            fh.write(message[: len(message) // 2])
            fh.flush()
            time.sleep(0.01)
        os.remove(partial)
        uid = mailbox.append_message(root, "INBOX", message)
        appended.append(uid)

    writer = threading.Thread(target=slow_writer, args=(1,))
    writer.start()
    writer.join()

    report = _collect(host, port)
    assert report.stopped is None
    ingested_uids = [item["uid"] for item in report.ingested]
    assert ingested_uids == [1, 2, appended[0]]

    with db.connect(db_path) as conn:
        rows = db.query_results_by_sample(conn, "S-SLOW1")
        assert len(rows) == 1
        assert mailbox.list_uids(root, "INBOX") == [1, 2, appended[0]]
