"""表计覆盖：每只契约表计在这个版本里到底有没有可用的数据来源。

复核要能回答两个问题：「哪些表计缺源文件」「这只表的这段数据来自哪个文件」。
覆盖表按契约表计逐只算，不按收到的行数算——没交表的终端就是缺源。
"""
from __future__ import annotations

from ... import config
from ..m2_contract import contract as contract_mod


def build(contracts: dict[str, contract_mod.MeterContract],
          valid: list[dict], estimates: list[dict], quarantine: list[dict],
          intervals: list[dict], sources: list[dict]) -> list[dict]:
    """返回按表计排序的覆盖行，每行带状态、行数、区间数与来源集合。"""
    source_names = {item["source_id"]: item.get("label") or item["source_id"]
                    for item in sources}
    valid_by: dict[str, list[dict]] = {}
    estimate_by: dict[str, list[dict]] = {}
    quarantine_by: dict[str, list[dict]] = {}
    intervals_by: dict[str, list[dict]] = {}
    for item in valid:
        valid_by.setdefault(item["meter_id"], []).append(item)
    for item in estimates:
        estimate_by.setdefault(item["meter_id"], []).append(item)
    for item in quarantine:
        if item.get("meter_id"):
            quarantine_by.setdefault(item["meter_id"], []).append(item)
    for item in intervals:
        intervals_by.setdefault(item["meter_id"], []).append(item)

    rows: list[dict] = []
    for meter_id in sorted(contracts):
        meter = contracts[meter_id]
        valid_rows = valid_by.get(meter_id, [])
        estimate_rows = estimate_by.get(meter_id, [])
        quarantined = quarantine_by.get(meter_id, [])
        intervals_rows = intervals_by.get(meter_id, [])
        # 主寄存器的锚点数决定「能不能成段」：只有一个锚点算不出区间
        primary_valid = [item for item in valid_rows
                         if item["register"] == meter.primary_register]
        if primary_valid and len(intervals_rows) >= 1:
            status = "covered"
        elif valid_rows or estimate_rows:
            status = "single_reading"
        elif quarantined:
            status = "quarantined_only"
        else:
            status = "missing"

        used: list[str] = []
        for group in (valid_rows, estimate_rows, intervals_rows):
            for item in group:
                for ident in item.get("source_ids") or (
                        [item["source_id"]] if item.get("source_id") else []):
                    if ident not in used:
                        used.append(ident)
        for item in quarantined:
            ident = item.get("source_id")
            if ident and ident not in used:
                used.append(ident)

        detail = _detail(status, primary_valid, quarantined, intervals_rows)
        rows.append({
            "meter_id": meter_id,
            "name": meter.name,
            "status": status,
            "valid_readings": len(valid_rows),
            "estimate_readings": len(estimate_rows),
            "quarantine_readings": len(quarantined),
            "interval_count": len(intervals_rows),
            "sources": used,
            "source_labels": [source_names.get(ident, ident) for ident in used],
            "detail": detail,
        })
    return rows


def _detail(status: str, primary_valid: list[dict], quarantined: list[dict],
            intervals_rows: list[dict]) -> str:
    if status == "covered":
        if quarantined:
            return f"主寄存器 {len(primary_valid)} 个锚点、{len(intervals_rows)} 段；另有隔离行"
        return f"主寄存器 {len(primary_valid)} 个锚点、{len(intervals_rows)} 段"
    if status == "single_reading":
        return "只有一条可用读数，无法与上期构成区间"
    if status == "quarantined_only":
        codes = sorted({item["code"] for item in quarantined})
        return "只有隔离行：" + "、".join(codes)
    return "没有任何来源提供这只表的读数"


def summarise(rows: list[dict]) -> dict:
    counts = {status: 0 for status in config.COVERAGE_STATUSES}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    gaps = [row for row in rows if row["status"] in config.COVERAGE_NEEDS_ATTENTION]
    return {
        "total": len(rows),
        "by_status": counts,
        "needs_attention": len(gaps),
        "gaps": [{"meter_id": row["meter_id"], "name": row["name"],
                  "status": row["status"],
                  "label": config.COVERAGE_LABELS.get(row["status"], row["status"]),
                  "detail": row["detail"]} for row in gaps],
    }
