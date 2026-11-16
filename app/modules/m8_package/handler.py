"""M8 入口：作业包收件、补件、作废来源、发布、版本与覆盖查询，以及页面的来源段。

页面只读已发布版本的快照，不读工作目录——原件被覆盖后，复核看到的仍是当时的字节。
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, Request

from ... import config, errors
from ...web import Ctx, get_ctx, render
from ..m1_ingest import service as m1_service
from ..m6_batches import compare
from . import coverage as coverage_mod
from . import service

router = APIRouter(tags=["M8 多来源包"])


def _payload_bytes(body: dict) -> tuple[bytes | None, str | None]:
    """支持三种交件方式：sample（样例白名单）/ content_base64 / content_text。"""
    if body.get("content_base64"):
        try:
            return base64.b64decode(str(body["content_base64"]), validate=True), None
        except Exception as exc:  # noqa: BLE001
            raise errors.BadRequest("content_base64 不是合法 base64") from exc
    if body.get("content_text") is not None:
        return str(body["content_text"]).encode("utf-8"), None
    return None, str(body.get("sample") or "")


@router.post("/api/packages")
def api_create_package(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    expected = body.get("expected_sources") or body.get("sources") or []
    package = service.ensure_package(ctx.settings, ctx.store, expected=expected,
                                     note=str(body.get("note") or ""))
    return service.package_payload(ctx.store, package)


@router.get("/api/packages")
def api_list_packages(limit: int = 20, ctx: Ctx = Depends(get_ctx)) -> dict:
    packages = ctx.store.list_packages(limit=limit)
    return {"packages": [{
        "package_id": item["package_id"],
        "status": item["status"],
        "latest_version": item["latest_version"],
        "published_version": item.get("published_version"),
        "published_batch_id": item.get("published_batch_id"),
        "created_at": item["created_at"],
        "updated_at": item.get("updated_at"),
        "expected_sources": len(item.get("expected_sources") or []),
    } for item in packages]}


@router.get("/api/packages/{package_id}")
def api_package(package_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    package = ctx.store.get_package(package_id)
    if package is None:
        raise errors.NotFound(f"作业包 {package_id} 不存在", code="E_BATCH_NOT_FOUND")
    return service.package_payload(ctx.store, package)


@router.post("/api/packages/{package_id}/sources")
def api_receive_source(package_id: str, payload: dict | None = None,
                       ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    filename = str(body.get("filename") or body.get("sample") or "")
    data, sample = _payload_bytes(body)
    if data is not None:
        filename = filename or sample or "upload.csv"
    elif sample:
        filename = sample
        data = m1_service.read_payload(ctx.settings, sample)
    else:
        raise errors.BadRequest("缺少 filename / sample / content_base64")
    return service.receive(
        ctx.settings, ctx.store, package_id=package_id, filename=filename,
        payload=data, source_id=body.get("source_id") or None,
        label=str(body.get("label") or ""), terminal=str(body.get("terminal") or ""),
        scope=body.get("scope"))


@router.post("/api/packages/{package_id}/sources/{source_id}/retire")
def api_retire_source(package_id: str, source_id: str,
                      ctx: Ctx = Depends(get_ctx)) -> dict:
    service.retire_source(ctx.store, package_id=package_id, source_id=source_id)
    package = ctx.store.get_package(package_id)
    return service.package_payload(ctx.store, package or {})


@router.post("/api/packages/{package_id}/publish")
def api_publish(package_id: str, payload: dict | None = None,
                ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    result = service.publish(ctx.settings, ctx.store, package_id=package_id,
                             note=str(body.get("note") or ""),
                             reason=str(body.get("reason") or "publish"))
    return {
        "status": result["status"],
        "package_id": package_id,
        "version_no": result["version_no"],
        "batch_id": result["batch_id"],
        "run_id": result["run_id"],
        "stages": result["stages"],
        "totals": {key: str(value) for key, value in result["totals"].items()},
        "reconcile": compare.payload(result["reconciliation"]),
        "sources": result["sources"],
        "coverage_summary": result["coverage_summary"],
        "coverage": result["coverage"],
        "missing_expected_sources": result["missing_expected_sources"],
    }


@router.get("/api/packages/{package_id}/versions")
def api_versions(package_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    package = ctx.store.get_package(package_id)
    if package is None:
        raise errors.NotFound(f"作业包 {package_id} 不存在", code="E_BATCH_NOT_FOUND")
    return {"package_id": package_id,
            "published_version": package.get("published_version"),
            "versions": ctx.store.load_versions(package_id)}


@router.get("/api/packages/{package_id}/versions/{version_no}/coverage")
def api_version_coverage(package_id: str, version_no: int,
                         ctx: Ctx = Depends(get_ctx)) -> dict:
    version = ctx.store.get_version(package_id, version_no)
    if version is None:
        raise errors.NotFound(f"版本 v{version_no} 不存在", code="E_VERSION_NOT_FOUND")
    rows = ctx.store.load_coverage(version["batch_id"])
    return {"package_id": package_id, "version_no": version_no,
            "batch_id": version["batch_id"], "status": version["status"],
            "coverage": rows, "summary": coverage_mod.summarise(rows)}


@router.get("/api/packages/{package_id}/sources/{source_id}/snapshots")
def api_snapshots(package_id: str, source_id: str,
                  ctx: Ctx = Depends(get_ctx)) -> dict:
    source = ctx.store.get_source(package_id, source_id)
    if source is None:
        raise errors.NotFound(f"来源 {source_id} 不在作业包中",
                              code="E_SOURCE_NOT_IN_PACKAGE")
    snapshots = ctx.store.load_snapshots(package_id, source_id=source_id)
    return {"package_id": package_id, "source_id": source_id,
            "current_snapshot_id": source.get("current_snapshot_id"),
            "snapshots": [service._snapshot_payload(item) for item in snapshots]}


@router.get("/api/coverage")
def api_coverage(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        return {"batch_id": None, "coverage": [], "summary": None}
    rows = ctx.store.load_coverage(batch["batch_id"])
    return {"batch_id": batch["batch_id"], "coverage": rows,
            "summary": coverage_mod.summarise(rows)}


# ---------------------------------------------------------------- 页面


@router.get("/packages")
def page_packages(request: Request, package_id: str | None = None,
                  ctx: Ctx = Depends(get_ctx)):
    packages = ctx.store.list_packages(limit=20)
    selected = None
    if package_id:
        selected = ctx.store.get_package(package_id)
    if selected is None and packages:
        selected = packages[0]
    payload = service.package_payload(ctx.store, selected) if selected else None
    return render(request, "packages.html", page="packages", page_title="来源与采信",
                  package=payload,
                  packages=packages,
                  snapshot_status_labels=config.SNAPSHOT_STATUS_LABELS,
                  coverage_labels=config.COVERAGE_LABELS,
                  package_samples=m1_service.available_samples(
                      ctx.settings, package_only=True))


@router.get("/sources")
def page_sources(request: Request, package_id: str | None = None,
                 batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)):
    """复核页：当前批次的每段数据来自哪个文件，哪些表计缺源文件。"""
    batch = ctx.current_batch(batch_id)
    coverage_rows: list[dict] = []
    summary = None
    manifest = None
    intervals: list[dict] = []
    if batch is not None:
        coverage_rows = ctx.store.load_coverage(batch["batch_id"])
        summary = coverage_mod.summarise(coverage_rows)
        manifest = ctx.store.load_manifest(batch["batch_id"]) or {}
        intervals = ctx.store.load_intervals(batch["batch_id"])
    package = ctx.store.get_package(package_id) if package_id else None
    return render(request, "sources.html", page="sources", page_title="来源追溯",
                  batch=batch, coverage=coverage_rows, summary=summary,
                  package_block=(manifest or {}).get("package") or {},
                  intervals=intervals, package=package,
                  coverage_labels=config.COVERAGE_LABELS,
                  snapshot_status_labels=config.SNAPSHOT_STATUS_LABELS)
