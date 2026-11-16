"""来源收件与快照：把「终端交来的一个文件」变成不可变的证据单元。

三条硬规则：
1. 先落快照再解析——原件被覆盖、被清理，扫描件仍在 `source_blobs` 里；
2. 快照不可改写——补件是新增快照，旧快照只记 `superseded_by`；
3. 文件级失败只拒绝这一个来源——空文件、表头坏、编码不识，都记原因码后继续收其余来源。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from pathlib import Path

from ... import config, errors, manifest, reasons
from ..m1_ingest import service as m1_service

# 文件级原因码：这些说明整个文件不可采信，而不是某几行有问题
FILE_LEVEL_CODES = frozenset({
    "E_EMPTY_FILE", "E_HEADER_ONLY", "E_ALL_BLANK", "E_HEADER_INVALID", "E_DECODE",
})


def now_utc() -> dt.datetime:
    return dt.datetime.now(config.UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(config.UTC).isoformat()


def source_key(filename: str) -> str:
    """来源标识由文件名派生：同一终端补件沿用同一 source_id，才能替换而非重复。"""
    stem = Path(filename).name
    return "src-" + hashlib.sha256(stem.encode("utf-8")).hexdigest()[:10]


def new_snapshot_id() -> str:
    stamp = now_utc().strftime("%Y%m%d%H%M%S")
    return f"snap-{stamp}-{uuid.uuid4().hex[:6]}"


def package_key(expected_sources: list[str]) -> str:
    return manifest.digest_parts(["package", *sorted(expected_sources)])[:16]


def resolve_payload(settings, filename: str, payload: bytes | None) -> bytes:
    """给了字节就用字节；没给就按白名单文件名从样例目录读。"""
    if payload is not None:
        return payload
    return m1_service.sample_path(settings, filename).read_bytes()


def receive(settings, store, *, package_id: str, filename: str, payload: bytes,
            source_id: str | None = None, label: str = "",
            terminal: str = "", stamp: str | None = None) -> dict:
    """收一件：存原件、解析、落快照、挂到来源上。返回快照与解析结果。

    同一来源再交一次（补件）→ 新快照，并把旧快照标成被接替；内容完全相同的
    重复提交直接返回原快照，不制造空版本。
    """
    stamp = stamp or iso(now_utc())
    source_id = source_id or source_key(filename)
    digest = hashlib.sha256(payload).hexdigest()
    store.put_blob(digest, payload, stamp)

    existing = _find_identical(store, package_id, source_id, digest)
    if existing is not None:
        return {"snapshot": existing, "ingest": None, "duplicate": True}

    snapshot_id = new_snapshot_id()
    try:
        ingested = m1_service.ingest_bytes(
            payload, name=filename, source_id=source_id,
            source_name=filename, snapshot_id=snapshot_id)
        ingest_code = None
        detail = ""
        status = (config.SNAPSHOT_PARTIAL if ingested.partial
                  else config.SNAPSHOT_ACCEPTED)
    except errors.MeterbackError as exc:
        ingested = None
        ingest_code = exc.code if exc.code in FILE_LEVEL_CODES else "E_HEADER_INVALID"
        detail = exc.message
        status = config.SNAPSHOT_REJECTED

    previous = store.get_source(package_id, source_id)
    if previous is not None and previous.get("current_snapshot_id"):
        # 只标「被新快照接替」，版本号在成功发布时再写，避免发布失败污染历史
        store.supersede_snapshot(previous["current_snapshot_id"], snapshot_id)

    snapshot = {
        "snapshot_id": snapshot_id,
        "package_id": package_id,
        "source_id": source_id,
        "filename": filename,
        "sha256": digest,
        "byte_len": len(payload),
        "status": status,
        "ingest_code": ingest_code,
        "ingest_detail": detail,
        "encoding": ingested.encoding if ingested else None,
        "segments": ingested.segments if ingested else 0,
        "data_lines": ingested.data_lines if ingested else 0,
        "parsed_lines": len(ingested.rows) if ingested else 0,
        "quarantine_count": len(ingested.quarantine) if ingested else 0,
        "received_at": stamp,
        "superseded_by": None,
        "version_no": 0,
    }
    store.insert_snapshot(snapshot)
    scope = previous.get("scope") if previous else None
    store.upsert_source(
        package_id=package_id, source_id=source_id,
        label=label or filename, terminal=terminal or (previous or {}).get("terminal", ""),
        status=config.SOURCE_ACTIVE, current_snapshot_id=snapshot_id,
        stamp=stamp, note=reasons.label(ingest_code) if ingest_code else "",
        scope=scope)
    return {"snapshot": snapshot, "ingest": ingested, "duplicate": False}


def _find_identical(store, package_id: str, source_id: str, digest: str) -> dict | None:
    for snapshot in reversed(store.load_snapshots(package_id, source_id=source_id)):
        if snapshot["sha256"] == digest:
            return snapshot
    return None


def snapshot_status_label(status: str | None) -> str:
    return config.SNAPSHOT_STATUS_LABELS.get(status or "", status or "—")


def ingest_snapshot(settings, store, snapshot: dict, *, source_id: str):
    """按快照里的内容重新解析（从原件快照读字节，不碰工作目录里的同名文件）。"""
    payload = store.get_blob(snapshot["sha256"])
    if payload is None:
        raise errors.Conflict(
            f"原件快照缺失：{snapshot['snapshot_id']}（{snapshot['filename']}）",
            code="E_PUBLISH_FAILED",
            detail={"snapshot_id": snapshot["snapshot_id"]})
    if snapshot["status"] == config.SNAPSHOT_REJECTED:
        return None
    return m1_service.ingest_bytes(
        payload, name=snapshot["filename"], source_id=source_id,
        source_name=snapshot["filename"], snapshot_id=snapshot["snapshot_id"])
