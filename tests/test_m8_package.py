"""M8 多来源包：坏来源与正常来源同包、表计缺源、补件不覆盖、发布失败保留旧版。

这些测试固定住四条承诺：
1. 坏来源不阻断整包，正常来源仍可发布；
2. 每段数据能追到来源文件，缺源表计能列出来；
3. 补件只替换自己那个来源，别的终端结果不动；
4. 补件发布失败时，旧版采信结果原样保留，原件被覆盖也能从快照复算。
"""
from __future__ import annotations

import base64
from decimal import Decimal

import pytest

from app import config, errors
from app.modules.m8_package import coverage as coverage_mod
from app.modules.m8_package import service as package_service
from app.modules.m8_package import sources as sources_mod

A = "readings_terminal_a.csv"
B = "readings_terminal_b_truncated.csv"
C = "readings_terminal_c_bad_header.csv"


def _expected():
    return [{"filename": A, "terminal": "终端A"},
            {"filename": B, "terminal": "终端B"},
            {"filename": C, "terminal": "终端C"}]


def _open_package(settings, store, *, expected=None, receive=(A, B, C)):
    package = package_service.ensure_package(settings, store,
                                             expected=expected or _expected())
    received = []
    for name in receive:
        received.append(package_service.receive(
            settings, store, package_id=package["package_id"], filename=name))
    return package, received


def _payload(package, store, source_id):
    source = store.get_source(package["package_id"], source_id)
    return store.get_snapshot(source["current_snapshot_id"])


# ---------------------------------------------------------------- 采信结论


def test_bad_source_does_not_block_the_package(settings, store):
    """表头坏的终端 C 只被文件级拒绝，A/B 照常发布，整包不退回。"""
    package, received = _open_package(settings, store)
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    assert result["status"] == "published"
    statuses = {item["source_id"]: item["status"] for item in result["sources"]}
    assert statuses[sources_mod.source_key(C)] == config.SNAPSHOT_REJECTED
    assert statuses[sources_mod.source_key(A)] == config.SNAPSHOT_ACCEPTED
    assert statuses[sources_mod.source_key(B)] == config.SNAPSHOT_PARTIAL
    assert result["totals"]["source_rejected"] == 1


def test_package_with_no_usable_source_is_rejected(settings, store):
    """整包全是坏来源才失败；失败不占版本号、不改已发布版本。"""
    package = package_service.ensure_package(
        settings, store, expected=[{"filename": C}])
    package_service.receive(settings, store, package_id=package["package_id"],
                            filename=C)
    with pytest.raises(errors.UnprocessableInput) as excinfo:
        package_service.publish(settings, store, package_id=package["package_id"])
    assert excinfo.value.code == "E_NO_USABLE_SOURCE"
    refreshed = store.get_package(package["package_id"])
    assert refreshed["latest_version"] == 0
    assert refreshed["published_version"] is None


def test_truncated_source_keeps_good_rows_and_isolates_tail(settings, store):
    package, _ = _open_package(settings, store)
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    tail = [row for row in store.load_quarantine(result["batch_id"])
            if row["code"] == "Q_TRUNCATED_ROW"]
    assert len(tail) == 1
    assert tail[0]["source_id"] == sources_mod.source_key(B)
    assert tail[0]["source_name"] == B
    m0008 = [row for row in result["coverage"] if row["meter_id"] == "M0008"]
    assert m0008[0]["status"] == "covered"


def test_readings_account_counts_data_rows_only(settings, store):
    """被拒文件没有数据行：不能把它算成一行，否则读数账是假的平衡。"""
    package, _ = _open_package(settings, store)
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    totals = result["totals"]
    account = next(item for item in result["reconciliation"]["accounts"]
                   if item["key"] == "readings")
    assert account["balanced"] is True
    assert account["left"] == totals["raw_lines"]
    # 左侧只能由真实数据行构成；文件级拒绝在说明里单列
    assert "文件级拒绝" in account["note"]
    assert int(totals["source_rejected"]) == 1
    data_lines = sum(item["data_lines"] for item in result["sources"])
    assert data_lines == totals["raw_lines"]


# ---------------------------------------------------------------- 来源追溯


def test_every_row_carries_its_source(settings, store):
    package, _ = _open_package(settings, store)
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    readings = store.load_readings(result["batch_id"])
    assert readings
    assert all(item["source_id"] for item in readings)
    sources = {item["source_id"] for item in readings}
    assert sources == {sources_mod.source_key(A), sources_mod.source_key(B)}
    for item in store.load_quarantine(result["batch_id"]):
        if item["stage"] == "ingest":
            assert item["source_id"] in sources | {sources_mod.source_key(C)}


def test_interval_records_both_endpoints_sources(settings, store):
    package, _ = _open_package(settings, store)
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    for interval in store.load_intervals(result["batch_id"]):
        assert interval["source_ids"]
        assert interval["cur_source_id"] in interval["source_ids"]
        if interval["prev_source_id"]:
            assert interval["prev_source_id"] in interval["source_ids"]


def test_cross_source_interval_is_flagged(settings, store):
    """上读与下读来自两个不同终端：仍能成段，但必须打标记，复核才知道两端出处。"""
    package = package_service.ensure_package(
        settings, store, expected=[{"filename": "x1.csv"}, {"filename": "x2.csv"}])
    header = "meter_id,register,read_at,reading,unit,read_type,note\n"
    package_service.receive(
        settings, store, package_id=package["package_id"], filename="x1.csv",
        payload=(header + "M0002,TOTAL,2026-01-05T00:00:00+08:00,1200.0,kWh,N,\n"
                 ).encode("utf-8"))
    package_service.receive(
        settings, store, package_id=package["package_id"], filename="x2.csv",
        payload=(header + "M0002,TOTAL,2026-02-05T00:00:00+08:00,1212.5,kWh,N,\n"
                 ).encode("utf-8"))
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    intervals = store.load_intervals(result["batch_id"])
    assert len(intervals) == 1
    item = intervals[0]
    assert item["prev_source_id"] != item["cur_source_id"]
    assert set(item["source_ids"]) == {item["prev_source_id"], item["cur_source_id"]}
    assert "K_CROSS_SOURCE_ANCHOR" in item["flags"]


def test_allocations_inherit_interval_sources(settings, store):
    package, _ = _open_package(settings, store)
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    allocations = store.load_allocations(result["batch_id"])
    assert allocations
    assert all(item["source_ids"] for item in allocations)


def test_missing_meter_is_listed_with_expected_source(settings, store):
    """契约里有、但没有任何来源提供的表计要单独列出来，不混进正常结果。"""
    expected = [{"filename": A, "terminal": "终端A"},
                {"filename": B, "terminal": "终端B"}]
    package = package_service.ensure_package(settings, store, expected=expected)
    package_service.receive(settings, store, package_id=package["package_id"],
                            filename=A)
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    states = {row["meter_id"]: row["status"] for row in result["coverage"]}
    assert states["M0001"] == "covered"
    assert states["M0004"] == "missing"
    assert states["M0005"] == "missing"
    assert result["coverage_summary"]["by_status"]["missing"] >= 3
    # 预期来源 B 没交，要能点名
    expected_ids = {item["source_id"] for item in result["missing_expected_sources"]}
    assert sources_mod.source_key(B) in expected_ids
    assert sources_mod.source_key(A) not in expected_ids


def test_quarantined_only_meter_reported(settings, store):
    package = package_service.ensure_package(
        settings, store, expected=[{"filename": A}])
    bad = ("meter_id,register,read_at,reading,unit,read_type,note\n"
           "M0002,TOTAL,2026-02-20T00:00:00+08:00,-5.0,kWh,N,负数\n")
    package_service.receive(settings, store, package_id=package["package_id"],
                            filename="readings_terminal_a.csv",
                            payload=bad.encode("utf-8"))
    result = package_service.publish(settings, store,
                                     package_id=package["package_id"])
    row = next(item for item in result["coverage"] if item["meter_id"] == "M0002")
    assert row["status"] == "quarantined_only"
    assert row["quarantine_readings"] == 1


# ---------------------------------------------------------------- 补件语义


def _fixed_terminal_b(settings) -> bytes:
    """把 B 的截断末行补全并加上换行：一个真实的补件。"""
    text = __import__("pathlib").Path(settings.sample_dir, B).read_bytes().decode("utf-8")
    text = text.replace("M0008,VALLEY,2026-04-01T00:00:00+08:00,3",
                        "M0008,VALLEY,2026-04-01T00:00:00+08:00,330.0,kWh,N,补件")
    return (text + "\n").encode("utf-8")


def test_supplement_replaces_only_its_own_source(settings, store):
    """补件改 B 的末行：A 的结果逐位不变，B 的隔离行消失。"""
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])
    before = {(item["meter_id"], item["register"], item["start_at"]):
              item["usage_kwh"]
              for item in store.load_intervals(first["batch_id"])
              if item["source_ids"] == [sources_mod.source_key(A)]}
    assert before

    package_service.receive(settings, store, package_id=package["package_id"],
                            filename=B, payload=_fixed_terminal_b(settings),
                            source_id=sources_mod.source_key(B))
    second = package_service.publish(settings, store, package_id=package["package_id"],
                                     reason="supplement")
    assert second["version_no"] == first["version_no"] + 1
    assert second["batch_id"] != first["batch_id"]

    tails = [row for row in store.load_quarantine(second["batch_id"])
             if row["code"] == "Q_TRUNCATED_ROW"]
    assert tails == []

    after = {(item["meter_id"], item["register"], item["start_at"]):
             item["usage_kwh"]
             for item in store.load_intervals(second["batch_id"])
             if item["source_ids"] == [sources_mod.source_key(A)]}
    assert after == before


def test_supplement_history_keeps_old_snapshot(settings, store):
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])
    source_id = sources_mod.source_key(B)
    old = _payload(package, store, source_id)
    package_service.receive(settings, store, package_id=package["package_id"],
                            filename=B,
                            payload=b"meter_id,register,read_at,reading,unit,read_type,note\n",
                            source_id=source_id)
    snapshots = store.load_snapshots(package["package_id"], source_id=source_id)
    assert len(snapshots) == 2
    old_after = store.get_snapshot(old["snapshot_id"])
    assert old_after["sha256"] == old["sha256"]
    assert old_after["superseded_by"]
    # 旧快照的原件仍可取回，历史版本仍能复算
    assert store.get_blob(old["sha256"]) is not None
    assert first["batch_id"] == store.get_package(package["package_id"])[
        "published_batch_id"]


def test_original_file_overwrite_does_not_affect_recompute(settings, store):
    """原件被覆盖：版本仍从快照复算，不读工作目录。"""
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])
    # 直接改写样例目录里的 A（模拟原件被新导出覆盖且无法恢复）
    original = __import__("pathlib").Path(settings.sample_dir, A).read_bytes()
    try:
        __import__("pathlib").Path(settings.sample_dir, A).write_bytes(
            b"meter_id,register,read_at,reading,unit,read_type,note\n")
        again = package_service.publish(settings, store,
                                        package_id=package["package_id"],
                                        reason="recompute")
        assert again["totals"]["interval_usage"] == first["totals"]["interval_usage"]
        old_readings = store.load_readings(first["batch_id"])
        new_readings = store.load_readings(again["batch_id"])
        assert [str(item["reading_norm"]) for item in old_readings] == \
               [str(item["reading_norm"]) for item in new_readings]
    finally:
        __import__("pathlib").Path(settings.sample_dir, A).write_bytes(original)


def test_duplicate_submit_is_idempotent(settings, store):
    package = package_service.ensure_package(settings, store, expected=[{"filename": A}])
    first = package_service.receive(settings, store, package_id=package["package_id"],
                                    filename=A)
    second = package_service.receive(settings, store, package_id=package["package_id"],
                                     filename=A)
    assert second["duplicate"] is True
    assert second["snapshot"]["snapshot_id"] == first["snapshot"]["snapshot_id"]
    assert len(store.load_snapshots(package["package_id"])) == 1


def test_supplement_out_of_scope_is_isolated(settings, store):
    """补件带了别的终端的表计：隔离并留痕，不覆盖对方结果。"""
    package, _ = _open_package(settings, store)
    package_service.publish(settings, store, package_id=package["package_id"])
    sneaky = ("meter_id,register,read_at,reading,unit,read_type,note\n"
              "M0001,TOTAL,2026-03-01T00:00:00+08:00,9999.0,kWh,N,越界\n")
    package_service.receive(settings, store, package_id=package["package_id"],
                            filename=B, payload=sneaky.encode("utf-8"),
                            source_id=sources_mod.source_key(B))
    result = package_service.publish(settings, store, package_id=package["package_id"],
                                     reason="supplement")
    rows = [row for row in store.load_quarantine(result["batch_id"])
            if row["code"] == "Q_SOURCE_OUT_OF_SCOPE"]
    assert len(rows) == 1
    assert "M0001" in rows[0]["detail"]


def test_retired_source_is_excluded_from_next_version(settings, store):
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])
    package_service.retire_source(store, package_id=package["package_id"],
                                  source_id=sources_mod.source_key(C))
    second = package_service.publish(settings, store, package_id=package["package_id"],
                                     reason="retire")
    assert len(second["sources"]) == len(first["sources"]) - 1


# ---------------------------------------------------------------- 发布失败


def test_publish_failure_keeps_previous_version(settings, store, monkeypatch):
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])
    first_totals = store.load_totals(first["batch_id"])
    first_counts = store.count_rows(first["batch_id"])

    real = store.publish_version

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "publish_version", boom)
    with pytest.raises(errors.Conflict) as excinfo:
        package_service.publish(settings, store, package_id=package["package_id"],
                                reason="supplement")
    assert excinfo.value.code == "E_PUBLISH_FAILED"
    monkeypatch.setattr(store, "publish_version", real)

    current = store.get_package(package["package_id"])
    assert current["published_version"] == first["version_no"]
    assert current["published_batch_id"] == first["batch_id"]
    assert current["latest_version"] == first["version_no"]
    assert store.load_totals(first["batch_id"]) == first_totals
    assert store.count_rows(first["batch_id"]) == first_counts


def test_publish_failure_mid_transaction_rolls_back_fully(settings, store, monkeypatch):
    """事务写到一半失败：新行全部回滚，发布指针与旧版数据逐字段不变。"""
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])
    first_totals = store.load_totals(first["batch_id"])

    real = store._write_run
    calls = {"n": 0}

    def half_write(conn, **kwargs):
        real(conn, **kwargs)          # 新行确实写进事务
        raise RuntimeError("crash after write")

    monkeypatch.setattr(store, "_write_run", half_write)
    with pytest.raises(errors.Conflict):
        package_service.publish(settings, store, package_id=package["package_id"],
                                reason="supplement")
    monkeypatch.setattr(store, "_write_run", real)

    current = store.get_package(package["package_id"])
    assert current["published_version"] == first["version_no"]
    assert current["published_batch_id"] == first["batch_id"]
    assert [item["version_no"] for item in store.load_versions(
        package["package_id"])] == [first["version_no"]]
    assert store.load_totals(first["batch_id"]) == first_totals
    # 旧版的所有读、区间、账单原样可读
    assert store.load_readings(first["batch_id"])
    assert store.load_intervals(first["batch_id"])
    assert store.load_bills(first["batch_id"])


def test_failed_publish_leaves_orphan_rows_invisible(settings, store, monkeypatch):
    """回滚后库里不残留新批次的读数行（否则下一次统计会重复计数）。"""
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "publish_version", boom)
    with pytest.raises(errors.Conflict):
        package_service.publish(settings, store, package_id=package["package_id"])
    monkeypatch.undo()

    with store.connect() as conn:
        orphan = conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE batch_id<>?",
            (first["batch_id"],)).fetchone()["n"]
    assert orphan == 0


def test_failed_supplement_does_not_consume_version(settings, store, monkeypatch):
    package, _ = _open_package(settings, store)
    first = package_service.publish(settings, store,
                                    package_id=package["package_id"])

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "publish_version", boom)
    with pytest.raises(errors.Conflict):
        package_service.publish(settings, store, package_id=package["package_id"])
    monkeypatch.undo()

    second = package_service.publish(settings, store, package_id=package["package_id"])
    assert second["version_no"] == first["version_no"] + 1
    assert store.get_version(package["package_id"], second["version_no"]) is not None


# ---------------------------------------------------------------- 覆盖模型单元


def test_coverage_status_is_derived_from_contract_meters(settings):
    from app.modules.m2_contract import contract as contract_mod
    contracts = contract_mod.load(settings)
    rows = coverage_mod.build(contracts, [], [], [], [], [])
    assert {row["status"] for row in rows} == {"missing"}
    assert len(rows) == len(contracts)
    summary = coverage_mod.summarise(rows)
    assert summary["by_status"]["missing"] == len(contracts)
    assert summary["needs_attention"] == len(contracts)


def test_http_package_flow(client, settings):
    """走一遍接口：建包 → 收三件（含坏件）→ 发布 → 查来源与覆盖。"""
    created = client.post("/api/packages", json={
        "expected_sources": [{"filename": A}, {"filename": B}, {"filename": C}]})
    assert created.status_code == 200, created.text
    package_id = created.json()["package_id"]
    for name in (A, B, C):
        response = client.post(f"/api/packages/{package_id}/sources",
                               json={"sample": name})
        assert response.status_code == 200, response.text
    published = client.post(f"/api/packages/{package_id}/publish", json={})
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["reconcile"]["all_balanced"] is True
    assert body["totals"]["source_rejected"] == "1"
    assert body["coverage_summary"]["needs_attention"] == 0

    sources = client.get(f"/api/packages/{package_id}").json()
    assert len(sources["sources"]) == 3
    assert len(sources["versions"]) == 1
    coverage = client.get("/api/coverage").json()
    assert coverage["summary"]["total"] == 8

    for path in ("/packages", "/sources"):
        page = client.get(path)
        assert page.status_code == 200
        assert "meterback" in page.text


def test_http_upload_bytes_are_snapshotted(client, settings):
    package_id = client.post("/api/packages", json={
        "expected_sources": [{"filename": "one.csv"}]}).json()["package_id"]
    content = ("meter_id,register,read_at,reading,unit,read_type,note\n"
               "M0002,TOTAL,2026-01-05T00:00:00+08:00,1200.0,kWh,N,\n"
               "M0002,TOTAL,2026-02-05T00:00:00+08:00,1212.5,kWh,N,\n")
    response = client.post(f"/api/packages/{package_id}/sources", json={
        "filename": "one.csv", "terminal": "终端X",
        "content_base64": base64.b64encode(content.encode("utf-8")).decode("ascii")})
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["status"] == "accepted"
    published = client.post(f"/api/packages/{package_id}/publish", json={})
    assert published.status_code == 200


def test_http_missing_meters_surface_on_index_and_sources(client, settings):
    """缺源表计消费者第一时间能看见：总览页与来源追溯页都要点名。"""
    ids = [A, B]
    package_id = client.post("/api/packages", json={
        "expected_sources": [{"filename": i} for i in ids]}).json()["package_id"]
    # 只交 A：M0004/M0005/M0006/M0008 缺源
    client.post(f"/api/packages/{package_id}/sources", json={"sample": A})
    published = client.post(f"/api/packages/{package_id}/publish", json={}).json()
    coverage = {row["meter_id"]: row["status"] for row in published["coverage"]}
    assert coverage["M0004"] == "missing"
    index = client.get("/").text
    assert "缺源文件" in index
    sources = client.get("/sources").text
    assert "M0004" in sources
    assert "缺源文件" in sources


def test_http_failed_publish_keeps_previous_result(client, settings, monkeypatch):
    package_id = client.post("/api/packages", json={
        "expected_sources": [{"filename": A}]}).json()["package_id"]
    client.post(f"/api/packages/{package_id}/sources", json={"sample": A})
    first = client.post(f"/api/packages/{package_id}/publish", json={}).json()
    store = client.app.state.store

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "publish_version", boom)
    failed = client.post(f"/api/packages/{package_id}/publish", json={})
    assert failed.status_code == 409
    assert failed.json()["error"] == "E_PUBLISH_FAILED"
    monkeypatch.undo()

    # 页面与导出仍指向旧版；总数逐位一致
    status = client.get("/api/health").json()
    assert status["latest_batch"] == first["batch_id"]
    body = client.get("/api/export/monthly.csv").text
    assert body.startswith("﻿")
    again = client.post(f"/api/packages/{package_id}/publish", json={}).json()
    assert again["totals"]["interval_usage"] == first["totals"]["interval_usage"]
