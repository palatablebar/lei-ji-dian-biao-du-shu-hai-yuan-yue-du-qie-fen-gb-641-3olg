"""多来源包（作业包）：收件 → 逐来源采信 → 合并 → 计算 → 覆盖 → 原子发布。

为什么不是一个文件一个批次：同一次关账，几个采集终端各交一份文件，正常件与坏件
在同一个包里。整包退回会拖住正常终端，直接放行又会让补件覆盖同一表计。
所以这里把「包」作为发布单位，把「来源快照」作为采信单位：

- 每个来源独立采信：`accepted` / `partial`（坏行进隔离）/ `rejected`（文件级拒绝）；
- 每条读数、每个区间、每条隔离都带来源标识，复核能追到具体文件；
- 补件只是给某个来源新增一个快照，其余来源沿用当前快照重算，不覆盖别的终端；
- 发布写在一个事务里，失败整体回滚，`published_version` 不动，旧版采信结果照旧可用。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from ... import config, errors, manifest, reasons
from ..m1_ingest import service as m1_service
from ..m2_contract import contract as contract_mod
from ..m2_contract import service as m2_service
from ..m3_quality import service as m3_service
from ..m4_intervals import restore as restore_mod
from ..m5_months import allocate as allocate_mod
from ..m6_batches import compare, history
from . import coverage as coverage_mod
from . import sources as sources_mod


def _ms(start: float) -> int:
    return max(1, int((time.perf_counter() - start) * 1000))


def _norm_expected(items: list | None) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"source_id": sources_mod.source_key(item),
                        "filename": item,
                        "label": Path(item).name})
        elif isinstance(item, dict) and item.get("filename"):
            out.append({
                "source_id": item.get("source_id")
                or sources_mod.source_key(item["filename"]),
                "filename": item["filename"],
                "label": item.get("label") or Path(item["filename"]).name,
                "terminal": item.get("terminal", ""),
            })
    return out


def ensure_package(settings, store, *, expected: list | None = None,
                   note: str = "", package_id: str | None = None) -> dict:
    stamp = sources_mod.iso(sources_mod.now_utc())
    if package_id:
        found = store.get_package(package_id)
        if found is None:
            raise errors.NotFound(f"作业包 {package_id} 不存在", code="E_BATCH_NOT_FOUND")
        return found
    expected_items = _norm_expected(expected)
    key = sources_mod.package_key([item["filename"] for item in expected_items])
    existing = store.find_package_by_key(key)
    if existing is not None:
        if expected_items and not existing.get("expected_sources"):
            store.update_package(existing["package_id"], expected_sources=expected_items)
            existing = store.get_package(existing["package_id"]) or existing
        return existing
    ident = "pkg-" + key[:12]
    return store.create_package(
        package_id=ident, package_key=key, status="collecting",
        expected_sources=expected_items, created_at=stamp, note=note)


def receive(settings, store, *, package_id: str, filename: str,
            payload: bytes | None = None, source_id: str | None = None,
            label: str = "", terminal: str = "",
            scope: list[str] | None = None) -> dict:
    """收一份来源文件（从样例目录或给定字节），落快照并返回诊断。"""
    package = store.get_package(package_id)
    if package is None:
        raise errors.NotFound(f"作业包 {package_id} 不存在", code="E_BATCH_NOT_FOUND")
    payload = sources_mod.resolve_payload(settings, filename, payload)
    source_id = source_id or sources_mod.source_key(filename)
    result = sources_mod.receive(
        settings, store, package_id=package_id, filename=filename, payload=payload,
        source_id=source_id, label=label or Path(filename).name, terminal=terminal)
    snapshot = result["snapshot"]
    if scope is not None:
        store.set_source_scope(package_id, source_id, scope)
    elif not result["duplicate"]:
        _derive_scope(store, package_id, source_id, result["ingest"])
    store.update_package(package_id, status="collecting",
                         updated_at=sources_mod.iso(sources_mod.now_utc()))
    return {
        "package_id": package_id,
        "source_id": source_id,
        "duplicate": result["duplicate"],
        "snapshot": _snapshot_payload(snapshot),
        "diagnostics": (result["ingest"].diagnostics
                        if result["ingest"] is not None else []),
        "ingest": None if result["ingest"] is None else {
            "encoding": result["ingest"].encoding,
            "segments": result["ingest"].segments,
            "raw_lines": result["ingest"].data_lines,
            "parsed": len(result["ingest"].rows),
            "quarantine": len(result["ingest"].quarantine),
            "digest": result["ingest"].digest,
        },
    }


def _derive_scope(store, package_id: str, source_id: str, ingested) -> None:
    """首次收件时按文件里出现的表计声明来源范围；之后补件不得越界。"""
    source = store.get_source(package_id, source_id)
    if source is None or source.get("scope"):
        return
    if ingested is None or not ingested.rows:
        return
    meters = sorted({(row["cells"].get("meter_id") or "").strip()
                     for row in ingested.rows} - {""})
    if meters:
        store.set_source_scope(package_id, source_id, meters, derived=True)


def retire_source(store, *, package_id: str, source_id: str) -> dict:
    source = store.get_source(package_id, source_id)
    if source is None:
        raise errors.NotFound(f"来源 {source_id} 不在作业包 {package_id} 中",
                              code="E_SOURCE_NOT_IN_PACKAGE")
    stamp = sources_mod.iso(sources_mod.now_utc())
    store.retire_source(package_id, source_id, stamp)
    return store.get_source(package_id, source_id) or {}


# ---------------------------------------------------------------- 发布


def publish(settings, store, *, package_id: str, note: str = "",
            reason: str = "publish") -> dict:
    started = time.perf_counter()
    package = store.get_package(package_id)
    if package is None:
        raise errors.NotFound(f"作业包 {package_id} 不存在", code="E_BATCH_NOT_FOUND")

    active = store.load_sources(package_id, status=config.SOURCE_ACTIVE)
    snapshots: list[dict] = []
    per_source: list[dict] = []
    for source in active:
        snapshot = store.get_snapshot(source["current_snapshot_id"])
        if snapshot is None:
            raise errors.Conflict(
                f"来源 {source['source_id']} 没有可发布的快照",
                code="E_PUBLISH_FAILED", detail={"source_id": source["source_id"]})
        snapshots.append(snapshot)

    stamp = sources_mod.iso(sources_mod.now_utc())
    version_no = int(package.get("latest_version") or 0) + 1
    digest = manifest.digest_parts(
        [package_id, settings.rule_version,
         *[f"{item['source_id']}:{item['sha256']}:{item['status']}"
           for item in sorted(snapshots, key=lambda s: s["source_id"])]])

    stages: list[dict] = []
    mark = time.perf_counter()

    # ---- 逐来源采信（从原件快照读，绝不从工作目录读） ----
    ingested_by_source: dict[str, object] = {}
    ingest_quarantine: list[dict] = []
    for snapshot in snapshots:
        source_id = snapshot["source_id"]
        ingested = sources_mod.ingest_snapshot(settings, store, snapshot,
                                               source_id=source_id)
        ingested_by_source[source_id] = ingested
        status = snapshot["status"]
        if status == config.SNAPSHOT_REJECTED:
            ingest_quarantine.append({
                "lineno": 0, "segment": 0,
                "code": snapshot.get("ingest_code") or "E_HEADER_INVALID",
                "stage": "ingest", "level": reasons.level(
                    snapshot.get("ingest_code") or "E_HEADER_INVALID"),
                "field": "文件", "raw_value": snapshot["filename"],
                "meter_id": None,
                "detail": snapshot.get("ingest_detail") or "文件级拒绝",
                "source_id": source_id, "source_name": snapshot["filename"],
                "snapshot_id": snapshot["snapshot_id"],
            })
        elif ingested is not None:
            ingest_quarantine.extend(ingested.quarantine)
        per_source.append({
            "source_id": source_id,
            "filename": snapshot["filename"],
            "snapshot_id": snapshot["snapshot_id"],
            "sha256": snapshot["sha256"],
            "byte_len": snapshot["byte_len"],
            "status": status,
            "ingest_code": snapshot.get("ingest_code"),
            "encoding": snapshot.get("encoding"),
            "data_lines": snapshot.get("data_lines", 0),
            "parsed_lines": snapshot.get("parsed_lines", 0),
            "quarantine_count": snapshot.get("quarantine_count", 0),
        })

    usable = [item for item in per_source if item["status"] != config.SNAPSHOT_REJECTED]
    raw_lines = sum(item["data_lines"] for item in per_source)
    parsed_lines = sum(item["parsed_lines"] for item in per_source)
    stages.append({
        "stage": "ingest", "in_count": raw_lines, "out_count": parsed_lines,
        "quarantine_count": len(ingest_quarantine), "status": "ok",
        "elapsed_ms": _ms(mark),
        "note": (f"{len(per_source)} 源："
                 f"{sum(1 for i in per_source if i['status'] == config.SNAPSHOT_ACCEPTED)} 可采信 / "
                 f"{sum(1 for i in per_source if i['status'] == config.SNAPSHOT_PARTIAL)} 部分 / "
                 f"{sum(1 for i in per_source if i['status'] == config.SNAPSHOT_REJECTED)} 拒绝"),
    })
    if not parsed_lines:
        exc = errors.UnprocessableInput(
            "作业包里没有任何可采信的数据行（所有来源都被文件级拒绝）",
            code="E_NO_USABLE_SOURCE",
            detail={"package_id": package_id,
                    "sources": [{"source_id": item["source_id"],
                                 "status": item["status"],
                                 "code": item["ingest_code"]}
                                for item in per_source]})
        _record_failed_attempt(store, package_id=package_id, version_no=version_no,
                               digest=digest, stamp=stamp, stages=stages,
                               per_source=per_source, note=reason, code=exc.code)
        raise exc

    # ---- 合并来源 ----
    mark = time.perf_counter()
    rows: list[dict] = []
    for source_id, ingested in ingested_by_source.items():
        if ingested is None:
            continue
        rows.extend(ingested.rows)
    scopes = {item["source_id"]: _scope_of(store, package_id, item["source_id"])
              for item in per_source}
    rows, out_of_scope = _fence_rows(rows, scopes, per_source)
    stages.append({
        "stage": "merge", "in_count": parsed_lines, "out_count": len(rows),
        "quarantine_count": len(out_of_scope), "status": "ok",
        "elapsed_ms": _ms(mark),
        "note": f"{len(usable)} 个可采信来源合并 · 越界 {len(out_of_scope)} 行",
    })

    # ---- M2 契约 ----
    mark = time.perf_counter()
    try:
        contracts = contract_mod.load(settings)
    except errors.MeterbackError as exc:
        _record_failed_attempt(store, package_id=package_id, version_no=version_no,
                               digest=digest, stamp=stamp, stages=stages,
                               per_source=per_source, note=reason, code=exc.code)
        raise
    records, contract_q, _ = m2_service.interpret_rows(rows, contracts)
    stages.append({"stage": "contract", "in_count": len(rows),
                   "out_count": len(records), "quarantine_count": len(contract_q),
                   "status": "ok", "elapsed_ms": _ms(mark),
                   "note": f"{len(contracts)} 只表"})

    # ---- M3 质量 ----
    mark = time.perf_counter()
    quality = m3_service.evaluate(records, contracts)
    stages.append({"stage": "quality", "in_count": len(records),
                   "out_count": len(quality["valid"]) + len(quality["estimate"]),
                   "quarantine_count": len(quality["quarantine"]), "status": "ok",
                   "elapsed_ms": _ms(mark),
                   "note": f"估抄 {len(quality['estimate'])}"})

    # ---- M4 还原 ----
    mark = time.perf_counter()
    intervals = restore_mod.build_intervals(quality["valid"], quality["estimate"],
                                            contracts)
    interval_stats = restore_mod.totals(intervals, contracts)
    stages.append({"stage": "restore", "in_count": len(quality["valid"]),
                   "out_count": len(intervals), "quarantine_count": 0, "status": "ok",
                   "elapsed_ms": _ms(mark),
                   "note": f"{interval_stats['primary_usage_kwh']} kWh"})

    # ---- M5 切分 ----
    mark = time.perf_counter()
    prices = contract_mod.load_prices(settings)
    allocated = allocate_mod.allocate(intervals, contracts, prices)
    stages.append({"stage": "allocate", "in_count": len(intervals),
                   "out_count": len(allocated["allocations"]), "quarantine_count": 0,
                   "status": "ok", "elapsed_ms": _ms(mark),
                   "note": f"{allocated['stats']['month_count']} 个月"})

    # ---- M6 对账 ----
    mark = time.perf_counter()
    quarantine = ingest_quarantine + contract_q + quality["quarantine"] + out_of_scope
    month_usage = allocated["stats"]["usage_total"]
    bill_total = sum((bill["amount_yuan"] for bill in allocated["bills"]), Decimal("0"))
    component_total = sum(
        (sum((Decimal(item["amount"]) for item in bill["components"]), Decimal("0"))
         for bill in allocated["bills"]), Decimal("0"))
    reconcile = compare.build(
        raw_lines=raw_lines, valid=len(quality["valid"]),
        estimate=len(quality["estimate"]), quarantine=len(quarantine),
        blank=sum(getattr(item, "blank_lines", 0)
                  for item in ingested_by_source.values() if item is not None),
        interval_usage=interval_stats["primary_usage_kwh"], month_usage=month_usage,
        tou_before=allocated["stats"]["tou_residual_before"],
        tou_after=allocated["stats"]["tou_residual_after"],
        tou_adjustments=allocated["stats"]["tou_adjustments"],
        bill_total=bill_total, component_total=component_total,
        bill_count=len(allocated["bills"]),
        file_level_rejects=totals_rejected(per_source))
    stages.append({"stage": "reconcile", "in_count": len(reconcile["accounts"]),
                   "out_count": sum(1 for a in reconcile["accounts"] if a["balanced"]),
                   "quarantine_count": reconcile["diff_count"], "status": "ok",
                   "elapsed_ms": _ms(mark),
                   "note": "四账平衡" if reconcile["all_balanced"] else "四账不平"})

    # ---- 覆盖：哪些表计缺源、哪些只有隔离行 ----
    mark = time.perf_counter()
    coverage_rows = coverage_mod.build(contracts, quality["valid"],
                                       quality["estimate"], quarantine,
                                       intervals, per_source)
    coverage_summary = coverage_mod.summarise(coverage_rows)
    missing_expected = _missing_expected(package, active)
    stages.append({"stage": "coverage", "in_count": len(coverage_rows),
                   "out_count": coverage_summary["by_status"].get("covered", 0),
                   "quarantine_count": coverage_summary["needs_attention"],
                   "status": "ok", "elapsed_ms": _ms(mark),
                   "note": (f"缺源 {coverage_summary['by_status'].get('missing', 0)} · "
                            f"缺件 {len(missing_expected)}")})

    # ---- 组装批次与清单 ----
    batch_key = manifest.digest_parts([package_id, str(version_no), digest])[:16]
    batch = history.build_batch(
        batch_key=batch_key, run_id=history.make_run_id(),
        sample=f"package:{package_id}", status="running", digest=digest,
        rule_version=settings.rule_version,
        params={"package_id": package_id, "version_no": version_no,
                "reason": reason, "sources": [i["source_id"] for i in per_source]},
        created_at=stamp, updated_at=stamp)
    batch_id = batch["batch_id"]

    finished_at = sources_mod.iso(sources_mod.now_utc())
    totals = {
        "raw_lines": raw_lines,
        "valid": len(quality["valid"]),
        "estimate": len(quality["estimate"]),
        "quarantine": len(quarantine),
        "blank": sum(getattr(item, "blank_lines", 0)
                     for item in ingested_by_source.values() if item is not None),
        "interval_usage": interval_stats["primary_usage_kwh"],
        "month_usage": month_usage,
        "bill_amount": bill_total.quantize(config.MONEY_QUANT),
        "interval_count": len(intervals),
        "allocation_count": len(allocated["allocations"]),
        "bill_count": len(allocated["bills"]),
        "month_count": allocated["stats"]["month_count"],
        "meter_count": len(contracts),
        "tou_adjustments": allocated["stats"]["tou_adjustments"],
        "segments": sum(getattr(item, "segments", 0)
                        for item in ingested_by_source.values() if item is not None),
        "encoding": "、".join(sorted({item["encoding"] for item in per_source
                                      if item.get("encoding")})),
        "balanced": "是" if reconcile["all_balanced"] else "否",
        "source_count": len(per_source),
        "source_accepted": sum(1 for i in per_source
                               if i["status"] == config.SNAPSHOT_ACCEPTED),
        "source_partial": sum(1 for i in per_source
                              if i["status"] == config.SNAPSHOT_PARTIAL),
        "source_rejected": sum(1 for i in per_source
                               if i["status"] == config.SNAPSHOT_REJECTED),
        "coverage_missing": coverage_summary["by_status"].get("missing", 0),
        "coverage_gaps": coverage_summary["needs_attention"],
        "missing_sources": len(missing_expected),
        "package_id": package_id,
        "version_no": version_no,
        "reconcile_json": json.dumps(reconcile, ensure_ascii=False, default=str),
    }
    manifest_json = manifest.build(
        run_id=batch["run_id"], batch_id=batch_id, batch_key_value=batch_key,
        settings=settings, sample=batch["sample"], digest=digest,
        inputs=[{"name": item["filename"], "source_id": item["source_id"],
                 "snapshot_id": item["snapshot_id"], "sha256": item["sha256"],
                 "bytes": item["byte_len"], "status": item["status"],
                 "ingest_code": item.get("ingest_code")}
                for item in per_source]
        + [{"name": config.METERS_FILE,
            "sha256": manifest.digest_bytes(
                (Path(settings.sample_dir) / config.METERS_FILE).read_bytes()),
            "bytes": (Path(settings.sample_dir) / config.METERS_FILE).stat().st_size,
            "encoding": "utf-8"}],
        params={"package_id": package_id, "version_no": version_no,
                "reason": reason, "rule_version": settings.rule_version},
        started_at=stamp, finished_at=finished_at,
        elapsed_ms=_ms(started), stages=stages,
        counters=history.counters(
            valid=len(quality["valid"]), estimate=len(quality["estimate"]),
            quarantine=len(quarantine), blank=totals["blank"],
            intervals=len(intervals), allocations=len(allocated["allocations"]),
            bills=len(allocated["bills"])),
        reconcile=reconcile)
    manifest_json["package"] = {
        "package_id": package_id,
        "version_no": version_no,
        "reason": reason,
        "sources": per_source,
        "missing_expected_sources": missing_expected,
        "coverage": coverage_rows,
    }

    readings_rows = [{
        "lineno": item["lineno"], "segment": item["segment"],
        "meter_id": item["meter_id"], "register": item["register"],
        "read_at": item["read_at"], "reading_text": item["reading_text"],
        "unit_raw": item["unit_raw"], "reading_norm": item["reading"],
        "unit_norm": item["unit_norm"], "read_type": item["read_type"],
        "status": "estimate" if item["read_type"] == "E" else "valid",
        "code": None, "flags": item.get("flags", []), "note": item.get("note", ""),
        "source_id": item.get("source_id"),
        "source_name": item.get("source_name"),
        "snapshot_id": item.get("snapshot_id"),
        "source_ids": item.get("source_ids") or [],
    } for item in (quality["valid"] + quality["estimate"])]

    version = {
        "package_id": package_id, "version_no": version_no,
        "batch_id": batch_id, "status": "published",
        "created_at": stamp, "published_at": finished_at, "note": reason,
        "sources": [{**item, "label": next(
            (s.get("label") for s in active if s["source_id"] == item["source_id"]),
            item["filename"])} for item in per_source],
        "digest": digest,
        "current_snapshot_ids": [item["snapshot_id"] for item in snapshots],
        "counters": {
            "sources": len(per_source), "rejected": totals["source_rejected"],
            "valid": len(quality["valid"]), "quarantine": len(quarantine),
            "intervals": len(intervals), "bills": len(allocated["bills"]),
            "coverage_gaps": coverage_summary["needs_attention"],
            "missing_sources": len(missing_expected),
        },
    }

    try:
        store.publish_version(
            package_id=package_id, version_no=version_no,
            batch={**batch, "status": "done", "finished_at": finished_at,
                   "updated_at": finished_at},
            meters=[{
                "meter_id": m.meter_id, "name": m.name, "capacity": m.capacity,
                "multiplier": m.multiplier, "unit": m.unit,
                "registers": list(m.registers),
                "replaced_at": m.replaced_at.isoformat() if m.replaced_at else None,
                "replaced_by": m.replaced_by, "tariff": m.tariff,
            } for m in contracts.values()],
            readings=readings_rows, intervals=intervals,
            allocations=allocated["allocations"], bills=allocated["bills"],
            quarantine=quarantine, stages=stages, totals=totals,
            manifest_json=manifest_json, coverage=coverage_rows,
            version=version, stamp=finished_at)
    except Exception as exc:  # 写库失败：事务已回滚，旧版发布指针与数据保持原样
        attempted = with_stage(stages, "publish", status="failed",
                               note=f"发布失败：{type(exc).__name__}")
        _record_failed_attempt(store, package_id=package_id, version_no=version_no,
                               digest=digest, stamp=finished_at, stages=attempted,
                               per_source=per_source, note=reason,
                               code="E_PUBLISH_FAILED")
        if isinstance(exc, errors.MeterbackError):
            raise
        raise errors.Conflict(f"发布失败，已保留上一版：{exc}",
                              code="E_PUBLISH_FAILED",
                              detail={"package_id": package_id}) from exc

    for snapshot in snapshots:
        # 本版用到的快照记版本号；若是补件接替的旧快照，接替关系已在收件时记下
        store.mark_snapshot_version(snapshot["snapshot_id"], version_no)
    return {
        "status": "published",
        "package_id": package_id,
        "version_no": version_no,
        "batch_id": batch_id,
        "run_id": batch["run_id"],
        "stages": stages,
        "totals": totals,
        "reconciliation": reconcile,
        "sources": per_source,
        "coverage": coverage_rows,
        "coverage_summary": coverage_summary,
        "missing_expected_sources": missing_expected,
    }


def with_stage(stages: list[dict], stage: str, *, status: str, note: str) -> list[dict]:
    return stages + [{"stage": stage, "in_count": 1, "out_count": 0,
                      "quarantine_count": 0, "status": status,
                      "elapsed_ms": 1, "note": note}]


def totals_rejected(per_source: list[dict]) -> int:
    return sum(1 for item in per_source
               if item["status"] == config.SNAPSHOT_REJECTED)


def _scope_of(store, package_id: str, source_id: str) -> list[str] | None:
    source = store.get_source(package_id, source_id)
    if source is None:
        return None
    return source.get("scope")


def _fence_rows(rows: list[dict], scopes: dict[str, list[str] | None],
                per_source: list[dict]) -> tuple[list[dict], list[dict]]:
    """来源范围护栏：补件只能补自己声明过的表计，越界行隔离而不是覆盖别人。

    空范围表示该来源还没交过可解析的件（首个可采信件才定义范围），此时不设限。
    """
    names = {item["source_id"]: item["filename"] for item in per_source}
    kept: list[dict] = []
    out_of_scope: list[dict] = []
    for row in rows:
        source_id = row.get("source_id")
        scope = scopes.get(source_id)
        meter_id = (row["cells"].get("meter_id") or "").strip()
        if scope and meter_id and meter_id not in scope:
            out_of_scope.append({
                "lineno": row["lineno"], "segment": row["segment"],
                "code": "Q_SOURCE_OUT_OF_SCOPE", "stage": "merge",
                "level": reasons.level("Q_SOURCE_OUT_OF_SCOPE"), "field": "meter_id",
                "raw_value": meter_id, "meter_id": meter_id,
                "detail": (f"来源 {names.get(source_id, source_id)} 未声明表计 "
                           f"{meter_id}，补件不得覆盖其他终端"),
                "source_id": source_id,
                "source_name": row.get("source_name"),
                "snapshot_id": row.get("snapshot_id"),
            })
            continue
        kept.append(row)
    return kept, out_of_scope


def _missing_expected(package: dict, active: list[dict]) -> list[dict]:
    have = {item["source_id"] for item in active}
    return [{"source_id": item["source_id"], "filename": item["filename"],
             "label": item.get("label") or item["filename"]}
            for item in package.get("expected_sources") or []
            if item["source_id"] not in have]


def _record_failed_attempt(store, *, package_id: str, version_no: int, digest: str,
                           stamp: str, stages: list[dict], per_source: list[dict],
                           note: str, code: str) -> None:
    """失败尝试只留证据（批次 + 阶段），不占版本号、不碰已发布版本的数据与指针。"""
    batch_key = manifest.digest_parts([package_id, str(version_no), digest, "failed"])
    batch = history.build_batch(
        batch_key=batch_key, run_id=history.make_run_id(),
        sample=f"package:{package_id}", status="failed", digest=digest,
        rule_version="", params={"package_id": package_id, "version_no": version_no,
                                 "reason": note, "sources": per_source},
        created_at=stamp, finished_at=stamp, updated_at=stamp, note=code)
    try:
        store.save_attempt(batch=batch, stages=stages, note=code)
    except Exception:
        # 连失败记录都写不进去，也不能影响已发布版本；错误由调用方抛
        pass


def _snapshot_payload(snapshot: dict | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "source_id": snapshot["source_id"],
        "filename": snapshot["filename"],
        "sha256": snapshot["sha256"],
        "byte_len": snapshot["byte_len"],
        "status": snapshot["status"],
        "status_label": config.SNAPSHOT_STATUS_LABELS.get(snapshot["status"],
                                                          snapshot["status"]),
        "ingest_code": snapshot.get("ingest_code"),
        "ingest_detail": snapshot.get("ingest_detail"),
        "encoding": snapshot.get("encoding"),
        "segments": snapshot.get("segments"),
        "data_lines": snapshot.get("data_lines"),
        "parsed_lines": snapshot.get("parsed_lines"),
        "quarantine_count": snapshot.get("quarantine_count"),
        "received_at": snapshot.get("received_at"),
        "superseded_by": snapshot.get("superseded_by"),
    }


def package_payload(store, package: dict) -> dict:
    sources = store.load_sources(package["package_id"])
    snapshots = store.load_snapshots(package["package_id"])
    current = store.get_version(package["package_id"],
                                package["published_version"]) \
        if package.get("published_version") else None
    coverage_rows = (store.load_coverage(current["batch_id"]) if current else [])
    return {
        "package_id": package["package_id"],
        "package_key": package["package_key"],
        "status": package["status"],
        "created_at": package["created_at"],
        "updated_at": package.get("updated_at"),
        "latest_version": package["latest_version"],
        "published_version": package.get("published_version"),
        "published_batch_id": package.get("published_batch_id"),
        "expected_sources": package.get("expected_sources") or [],
        "missing_expected_sources": _missing_expected(
            package, [s for s in sources if s["status"] == config.SOURCE_ACTIVE]),
        "sources": [_source_payload(item, snapshots) for item in sources],
        "versions": [{
            "version_no": item["version_no"], "batch_id": item["batch_id"],
            "status": item["status"], "created_at": item["created_at"],
            "published_at": item.get("published_at"), "note": item.get("note"),
            "counters": item["counters"],
        } for item in store.load_versions(package["package_id"])],
        "coverage": coverage_rows,
        "coverage_summary": coverage_mod.summarise(coverage_rows),
    }


def _source_payload(source: dict, snapshots: list[dict]) -> dict:
    own = [item for item in snapshots if item["source_id"] == source["source_id"]]
    current = next((item for item in own
                    if item["snapshot_id"] == source.get("current_snapshot_id")), None)
    return {
        "source_id": source["source_id"],
        "label": source.get("label"),
        "terminal": source.get("terminal"),
        "status": source["status"],
        "scope": source.get("scope") or [],
        "scope_derived": source.get("scope_derived"),
        "current": _snapshot_payload(current),
        "history": [_snapshot_payload(item) for item in own],
        "updated_at": source.get("updated_at"),
        "retired_at": source.get("retired_at"),
        "note": source.get("note"),
    }
