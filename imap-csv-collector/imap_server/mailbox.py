"""文件型邮箱存储，供样例 IMAP 服务与演示脚本共用。

目录结构：
  <root>/<folder>/meta.json   {"uidvalidity": int, "next_uid": int}
  <root>/<folder>/<uid>.eml   单封 RFC822 邮件
  <root>/<folder>/.lock       文件夹级发布锁（不参与 UID 分配）

发布保证（并发 + 崩溃安全）：
  * 每次分配 UID 都在文件夹级锁（进程内线程锁 + ``fcntl.flock``）内完成，
    先按现有 ``*.eml`` 的最大 UID 修复 ``next_uid``，再写入并推进，
    因此并发追加不会拿到同一个 UID。
  * 邮件先写入同目录临时文件、``fsync`` 后 ``os.replace`` 到 ``<uid>.eml``。
    其它进程只可能看到“不存在”或“完整邮件”，不会读到半封邮件；
    临时文件不以 ``.eml`` 结尾，也不会被 IMAP 服务当作邮件列出。
  * 若进程在“邮件已可见、但 ``meta.json`` 的 next_uid 尚未更新”之间退出，
    下次追加会按磁盘上的最大 UID 修复 next_uid 后再分配，绝不会覆盖
    已经成功投递的邮件；崩溃留下的 UID 空洞是合法的 IMAP 语义。
"""
from __future__ import annotations

import contextlib
import email
import json
import os
import re
import tempfile
import threading
import time
from email.utils import formatdate
from typing import Iterator, Optional

try:  # Linux/macOS；没有 flock 时退化为进程内串行化
    import fcntl
except ImportError:  # pragma: no cover - 仅非 POSIX 平台
    fcntl = None  # type: ignore[assignment]

DEFAULT_ROOT = os.environ.get("MAIL_ROOT", "/srv/mail")
DEFAULT_USER = os.environ.get("MAIL_USER", "instrument@lab.local")
DEFAULT_PASSWORD = os.environ.get("MAIL_PASSWORD", "labpass")

META_NAME = "meta.json"
LOCK_NAME = ".lock"
TMP_PREFIX = ".tmp-"
TMP_SUFFIX = ".part"


def safe_folder(folder: str) -> str:
    folder = folder.strip()
    if not folder or not re.fullmatch(r"[A-Za-z0-9._\-]+", folder):
        raise ValueError(f"非法文件夹名: {folder!r}")
    return folder


def folder_dir(root: str, folder: str) -> str:
    return os.path.join(root, safe_folder(folder))


def _meta_path(root: str, folder: str) -> str:
    return os.path.join(folder_dir(root, folder), META_NAME)


def _message_path(directory: str, uid: int) -> str:
    return os.path.join(directory, f"{uid}.eml")


def folder_exists(root: str, folder: str) -> bool:
    return os.path.isfile(_meta_path(root, folder))


# ---------------------------------------------------------------------------
# 原子文件写入
# ---------------------------------------------------------------------------
def _fsync_dir(directory: str) -> None:
    """让 rename / 新建目录项在进程或机器重启后仍然可见。"""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: str, payload: bytes) -> None:
    """同目录临时文件 + fsync + rename，读者只会看到完整内容。"""
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            # 邮箱卷可能被 imap / api / mailctl 以不同用户访问，保持可读
            if hasattr(os, "fchmod"):
                with contextlib.suppress(OSError):
                    os.fchmod(fh.fileno(), 0o644)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _atomic_write_json(path: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _atomic_write(path, data)


# ---------------------------------------------------------------------------
# 文件夹锁
# ---------------------------------------------------------------------------
_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


def _thread_lock_for(directory: str) -> threading.Lock:
    key = os.path.abspath(directory)
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


@contextlib.contextmanager
def _folder_guard(root: str, folder: str, uidvalidity: Optional[int] = None) -> Iterator[str]:
    """在文件夹级互斥锁内执行一段读改写操作。

    同时使用 Python 线程锁和 ``flock``：前者覆盖同进程多线程，
    后者覆盖 ``mailctl`` 容器/进程并发投递同一共享邮箱卷的情况。
    """
    directory = folder_dir(root, folder)
    os.makedirs(directory, exist_ok=True)
    thread_lock = _thread_lock_for(directory)
    with thread_lock:
        lock_path = os.path.join(directory, LOCK_NAME)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if not os.path.isfile(_meta_path(root, folder)):
                _atomic_write_json(
                    _meta_path(root, folder),
                    {
                        "uidvalidity": uidvalidity or int(time.time()),
                        "next_uid": 1,
                    },
                )
            yield directory
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _read_meta_unlocked(root: str, folder: str) -> dict:
    with open(_meta_path(root, folder), encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"meta.json 格式非法: {data!r}")
    return data


def _list_uids_unlocked(root: str, folder: str) -> list[int]:
    directory = folder_dir(root, folder)
    uids: list[int] = []
    if not os.path.isdir(directory):
        return uids
    for name in os.listdir(directory):
        # 只认 <数字>.eml；临时文件、.lock、meta.json 均忽略
        if not name.endswith(".eml"):
            continue
        stem = name[:-4]
        if stem.isdigit():
            uids.append(int(stem))
    return sorted(uids)


def _repair_next_uid_unlocked(root: str, folder: str, meta: dict) -> int:
    """按磁盘上已完整发布的邮件修复 next_uid，避免崩溃后覆盖旧邮件。"""
    uids = _list_uids_unlocked(root, folder)
    max_uid = uids[-1] if uids else 0
    try:
        next_uid = int(meta.get("next_uid", 1))
    except (TypeError, ValueError):
        next_uid = 1
    repaired = max(next_uid, max_uid + 1, 1)
    if repaired != next_uid:
        meta["next_uid"] = repaired
        _atomic_write_json(_meta_path(root, folder), meta)
    return repaired


# ---------------------------------------------------------------------------
# 公共读写 API
# ---------------------------------------------------------------------------
def ensure_folder(root: str, folder: str, uidvalidity: Optional[int] = None) -> None:
    with _folder_guard(root, folder, uidvalidity):
        pass


def read_meta(root: str, folder: str) -> dict:
    with _folder_guard(root, folder):
        meta = _read_meta_unlocked(root, folder)
        # 若上次在“邮件已发布、next_uid 未推进”之间退出，这里顺便修复，
        # 让 IMAP UIDNEXT 与磁盘内容一致。
        _repair_next_uid_unlocked(root, folder, meta)
        return meta


def write_meta(root: str, folder: str, meta: dict) -> None:
    with _folder_guard(root, folder):
        _atomic_write_json(_meta_path(root, folder), meta)


def list_uids(root: str, folder: str) -> list[int]:
    return _list_uids_unlocked(root, folder)


def _cleanup_stale_temp_files(directory: str) -> int:
    """删除崩溃遗留的未发布临时文件（不含 .eml，不影响 UID 列表）。"""
    removed = 0
    with contextlib.suppress(FileNotFoundError):
        for name in os.listdir(directory):
            if name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(directory, name))
                    removed += 1
    return removed


def read_message(root: str, folder: str, uid: int) -> Optional[bytes]:
    path = _message_path(folder_dir(root, folder), int(uid))
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def append_message(
    root: str, folder: str, raw: bytes, *, uid: Optional[int] = None
) -> int:
    """原子发布一封邮件并返回其 UID。

    UID 分配与邮件发布在同一把文件夹锁内完成：
      1. 按现有邮件修复 ``next_uid``；
      2. 选 UID，把完整邮件写入临时文件并 fsync；
      3. rename 成 ``<uid>.eml``（此时邮件才对外可见）；
      4. 原子推进 ``next_uid``。
    任意步骤进程退出都不会覆盖已发布邮件，也不会暴露半封邮件。
    """
    with _folder_guard(root, folder) as directory:
        meta = _read_meta_unlocked(root, folder)
        next_uid = _repair_next_uid_unlocked(root, folder, meta)
        existing = set(_list_uids_unlocked(root, folder))

        if uid is None:
            target_uid = next_uid
        else:
            target_uid = int(uid)
            if target_uid < 1:
                raise ValueError(f"UID 必须为正整数: {target_uid}")
            if target_uid in existing:
                # 显式指定 UID 时不允许悄悄覆盖已有邮件
                raise FileExistsError(f"UID {target_uid} 已存在")
            next_uid = max(next_uid, target_uid + 1)

        _cleanup_stale_temp_files(directory)
        _atomic_write(_message_path(directory, target_uid), raw)

        # 邮件已经完整可见后才推进 next_uid；若此步前退出，
        # 下次 append 会通过 _repair_next_uid_unlocked 从磁盘最大 UID 续号。
        meta["next_uid"] = max(next_uid, target_uid + 1)
        _atomic_write_json(_meta_path(root, folder), meta)
        return target_uid


def replace_message(root: str, folder: str, uid: int, raw: bytes) -> None:
    """原子替换指定 UID 的邮件内容（运维修复附件）。

    其它进程只会读到替换前或替换后的完整邮件。
    """
    with _folder_guard(root, folder) as directory:
        target_uid = int(uid)
        if target_uid not in set(_list_uids_unlocked(root, folder)):
            raise FileNotFoundError(f"UID {target_uid} 不存在")
        _atomic_write(_message_path(directory, target_uid), raw)
        meta = _read_meta_unlocked(root, folder)
        if target_uid + 1 > int(meta.get("next_uid", 1)):
            meta["next_uid"] = target_uid + 1
            _atomic_write_json(_meta_path(root, folder), meta)


def reset_uidvalidity(root: str, folder: str, uidvalidity: int) -> None:
    """模拟邮箱重建：换 UIDVALIDITY 并重排 UID（内容保持不变）。

    整个重建在文件夹锁内完成；每封邮件仍以原子 rename 发布，
    IMAP 服务不会读到半封邮件。
    """
    with _folder_guard(root, folder) as directory:
        uids = _list_uids_unlocked(root, folder)
        stored = {uid: read_message(root, folder, uid) for uid in uids}
        keep = len(uids)

        # 先把重排后的目标文件全部原子写入（逐个 rename，内容完整）
        for index, old_uid in enumerate(uids, start=1):
            payload = stored[old_uid] or b""
            _atomic_write(_message_path(directory, index), payload)

        # 清掉旧编号中超出新邮件数量的残留文件
        for old_uid in uids:
            if old_uid > keep:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(_message_path(directory, old_uid))

        _atomic_write_json(
            _meta_path(root, folder),
            {"uidvalidity": int(uidvalidity), "next_uid": keep + 1},
        )
        _fsync_dir(directory)


def replace_attachment(raw: bytes, attachment_name: str, payload: bytes) -> bytes:
    """重建指定附件部件（保持 Message-ID 不变），返回新的 RFC822 字节。

    直接 set_payload 会破坏原部件的 base64 编码，这里整体重建 MIME 部件，
    模拟运维侧“用修复后的 CSV 替换坏附件并重新投递”。
    """
    msg = email.message_from_bytes(raw)
    target = None
    container = None
    for part in msg.walk():
        if part.is_multipart():
            for child in part.get_payload():
                if child.get_filename() == attachment_name:
                    target = child
                    container = part
                    break
        if target is not None:
            break
    if target is None or container is None:
        raise ValueError(f"邮件中找不到附件 {attachment_name}")

    from email.message import EmailMessage

    new_part = EmailMessage()
    new_part.set_content(
        payload,
        maintype="text",
        subtype="csv",
        filename=attachment_name,
    )
    parts = container.get_payload()
    index = parts.index(target)
    parts[index] = new_part
    return msg.as_bytes()


def build_message(
    message_id: str,
    subject: str,
    sender: str,
    recipient: str,
    attachments: list[tuple[str, bytes]],
    *,
    date: Optional[str] = None,
) -> bytes:
    """构造带若干附件的 multipart/mixed RFC822 邮件。"""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = date or formatdate(localtime=True)
    msg.set_content("仪器自动上传的检测结果，详见 CSV 附件。")
    for name, payload in attachments:
        msg.add_attachment(
            payload,
            maintype="text",
            subtype="csv",
            filename=name,
        )
    return msg.as_bytes()
