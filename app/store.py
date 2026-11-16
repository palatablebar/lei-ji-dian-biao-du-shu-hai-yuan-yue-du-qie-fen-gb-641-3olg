"""SQLite 存储：批次、表计契约、读数、区间、月度分摊、账单、隔离、阶段、合计、清单。

约定：
- 用量与金额一律 TEXT（Decimal 字符串），不落 float；
- 一次运行的所有行在同一个事务里「先删后插」，失败整体回滚，不留半批数据；
- 页面永远只展示一个批次，按 batch_id 过滤。

多来源包（M8）在此之上加了三层：
- 原始文件按 sha256 存进 `source_blobs`，之后只读快照，原件被覆盖也能复算；
- `package_sources` / `source_snapshots` 记录每个终端每次提交，补件追加新快照，
  旧快照保留（`superseded_by` 指向新快照），不覆盖；
- `package_versions` 每个版本指向一个独立的 batch_id，已发布版本不可改写；
  发布在单个事务里写结果 + 翻发布指针，失败整体回滚，旧版本原样保留。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sample TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS meters (
    batch_id TEXT NOT NULL, meter_id TEXT NOT NULL, name TEXT NOT NULL,
    capacity TEXT NOT NULL, multiplier TEXT NOT NULL, unit TEXT NOT NULL,
    registers TEXT NOT NULL, replaced_at TEXT, replaced_by TEXT, tariff TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    meter_id TEXT NOT NULL, register TEXT NOT NULL, read_at TEXT NOT NULL,
    reading_text TEXT NOT NULL, unit_raw TEXT NOT NULL, reading_norm TEXT,
    unit_norm TEXT, read_type TEXT NOT NULL, status TEXT NOT NULL,
    code TEXT, flags_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
    source_id TEXT, source_name TEXT, snapshot_id TEXT,
    source_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS intervals (
    batch_id TEXT NOT NULL, interval_id TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    prev_reading TEXT NOT NULL, cur_reading TEXT NOT NULL, delta TEXT NOT NULL,
    multiplier TEXT NOT NULL, unit TEXT NOT NULL, usage_kwh TEXT NOT NULL,
    days TEXT NOT NULL, method TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '[]',
    prev_source_id TEXT, cur_source_id TEXT,
    source_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS allocations (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, usage_kwh TEXT NOT NULL, exact_share TEXT NOT NULL,
    tou_adjusted INTEGER NOT NULL DEFAULT 0, sources_json TEXT NOT NULL DEFAULT '[]',
    source_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS bills (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    usage_kwh TEXT NOT NULL, amount_yuan TEXT NOT NULL,
    components_json TEXT NOT NULL, adjust_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    code TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL,
    field TEXT, raw_value TEXT, meter_id TEXT, detail TEXT,
    source_id TEXT, source_name TEXT, snapshot_id TEXT
);
CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL, quarantine_count INTEGER NOT NULL,
    status TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_blobs (
    sha256 TEXT PRIMARY KEY, bytes BLOB NOT NULL, byte_len INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY,
    package_key TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_sources_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    latest_version INTEGER NOT NULL DEFAULT 0,
    published_version INTEGER,
    published_batch_id TEXT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS package_sources (
    package_id TEXT NOT NULL, source_id TEXT NOT NULL, label TEXT NOT NULL,
    terminal TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    current_snapshot_id TEXT, created_at TEXT NOT NULL, updated_at TEXT,
    retired_at TEXT, note TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '[]', scope_derived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (package_id, source_id)
);
CREATE TABLE IF NOT EXISTS source_snapshots (
    snapshot_id TEXT PRIMARY KEY, package_id TEXT NOT NULL,
    source_id TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
    byte_len INTEGER NOT NULL, status TEXT NOT NULL, ingest_code TEXT,
    ingest_detail TEXT NOT NULL DEFAULT '', encoding TEXT,
    segments INTEGER NOT NULL DEFAULT 0, data_lines INTEGER NOT NULL DEFAULT 0,
    parsed_lines INTEGER NOT NULL DEFAULT 0,
    quarantine_count INTEGER NOT NULL DEFAULT 0, received_at TEXT NOT NULL,
    superseded_by TEXT, version_no INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source ON source_snapshots(package_id, source_id);
CREATE TABLE IF NOT EXISTS package_versions (
    package_id TEXT NOT NULL, version_no INTEGER NOT NULL, batch_id TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, published_at TEXT,
    note TEXT NOT NULL DEFAULT '', sources_json TEXT NOT NULL DEFAULT '[]',
    digest TEXT NOT NULL DEFAULT '', counters_json TEXT NOT NULL DEFAULT '{}',
    superseded_by INTEGER,
    PRIMARY KEY (package_id, version_no)
);
CREATE TABLE IF NOT EXISTS coverage (
    batch_id TEXT NOT NULL, meter_id TEXT NOT NULL, name TEXT NOT NULL,
    status TEXT NOT NULL, valid_readings INTEGER NOT NULL,
    estimate_readings INTEGER NOT NULL, quarantine_readings INTEGER NOT NULL,
    interval_count INTEGER NOT NULL, sources_json TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT ''
);
"""

# 老库补列：只加不删，默认值保证历史批次照常读
MIGRATIONS = {
    "readings": {
        "source_id": "TEXT", "source_name": "TEXT", "snapshot_id": "TEXT",
        "source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "intervals": {
        "prev_source_id": "TEXT", "cur_source_id": "TEXT",
        "source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "allocations": {
        "source_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "quarantine": {
        "source_id": "TEXT", "source_name": "TEXT", "snapshot_id": "TEXT",
    },
}



def _dec(text):
    return None if text is None else Decimal(str(text))


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn) -> None:
        columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(batches)").fetchall()}
        if "updated_at" not in columns:
            conn.execute("ALTER TABLE batches ADD COLUMN updated_at TEXT")
        for table, additions in MIGRATIONS.items():
            present = {row["name"] for row in
                       conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, ddl in additions.items():
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return dict(row) if row else None

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return dict(row) if row else None

    def find_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? ORDER BY created_at DESC LIMIT 1",
                (batch_key,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            order = "COALESCE(updated_at, created_at) DESC, rowid DESC"
            row = conn.execute(
                f"SELECT * FROM batches WHERE status=? ORDER BY {order} LIMIT 1",
                (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT * FROM batches ORDER BY {order} LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY COALESCE(updated_at, created_at) DESC, "
                "rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_attempt(self, *, batch: dict, stages: list[dict], note: str = "") -> None:
        """失败尝试：只写批次与阶段，不动已有批次的数据。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                        finished_at,input_digest,rule_version,params_json,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note, run_id=excluded.run_id,
                     updated_at=excluded.updated_at""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["sample"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch["rule_version"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 note or batch.get("note", ""), batch.get("updated_at")))
            conn.execute("DELETE FROM stages WHERE batch_id=?", (batch["batch_id"],))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))

    def touch_batch(self, batch_id: str, stamp: str) -> None:
        """命中已有批次时刷新「最近一次运行」，页面才会停在这次看的结果上。"""
        with self.connect() as conn:
            conn.execute("UPDATE batches SET updated_at=? WHERE batch_id=?",
                         (stamp, batch_id))

    # ---------- 一次运行整体落库 ----------
    def save_run(self, *, batch: dict, meters: list[dict], readings: list[dict],
                 intervals: list[dict], allocations: list[dict], bills: list[dict],
                 quarantine: list[dict], stages: list[dict], totals: dict,
                 manifest_json: dict, coverage: list[dict] | None = None) -> None:
        with self.connect() as conn:
            self._write_run(conn, batch=batch, meters=meters, readings=readings,
                            intervals=intervals, allocations=allocations, bills=bills,
                            quarantine=quarantine, stages=stages, totals=totals,
                            manifest_json=manifest_json, coverage=coverage or [])

    def _write_run(self, conn, *, batch: dict, meters: list[dict], readings: list[dict],
                   intervals: list[dict], allocations: list[dict], bills: list[dict],
                   quarantine: list[dict], stages: list[dict], totals: dict,
                   manifest_json: dict, coverage: list[dict]) -> None:
        """在一个已打开的事务里写整批结果；调用方负责提交或回滚。"""
        b = batch["batch_id"]
        for table in ("meters", "readings", "intervals", "allocations", "bills",
                      "quarantine", "stages", "totals", "coverage"):
            conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (b,))
        for m in meters:
            conn.execute(
                """INSERT INTO meters (batch_id,meter_id,name,capacity,multiplier,unit,
                    registers,replaced_at,replaced_by,tariff)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (b, m["meter_id"], m["name"], str(m["capacity"]),
                 str(m["multiplier"]), m["unit"], ";".join(m["registers"]),
                 m.get("replaced_at"), m.get("replaced_by"), m["tariff"]))
        for r in readings:
            conn.execute(
                """INSERT INTO readings (batch_id,lineno,segment,meter_id,register,
                    read_at,reading_text,unit_raw,reading_norm,unit_norm,read_type,
                    status,code,flags_json,note,source_id,source_name,snapshot_id,
                    source_ids_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b, r["lineno"], r["segment"], r["meter_id"], r["register"],
                 r["read_at"], r["reading_text"], r["unit_raw"],
                 None if r.get("reading_norm") is None else str(r["reading_norm"]),
                 r.get("unit_norm"), r["read_type"], r["status"], r.get("code"),
                 json.dumps(r.get("flags", []), ensure_ascii=False),
                 r.get("note", ""), r.get("source_id"), r.get("source_name"),
                 r.get("snapshot_id"),
                 json.dumps(r.get("source_ids") or
                            ([r["source_id"]] if r.get("source_id") else []),
                            ensure_ascii=False)))
        for i in intervals:
            conn.execute(
                """INSERT INTO intervals (batch_id,interval_id,meter_id,register,
                    start_at,end_at,prev_reading,cur_reading,delta,multiplier,unit,
                    usage_kwh,days,method,flags_json,prev_source_id,cur_source_id,
                    source_ids_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b, i["interval_id"], i["meter_id"], i["register"], i["start_at"],
                 i["end_at"], str(i["prev_reading"]), str(i["cur_reading"]),
                 str(i["delta"]), str(i["multiplier"]), i["unit"],
                 str(i["usage_kwh"]), str(i["days"]), i["method"],
                 json.dumps(i.get("flags", []), ensure_ascii=False),
                 i.get("prev_source_id"), i.get("cur_source_id"),
                 json.dumps(i.get("source_ids", []), ensure_ascii=False)))
        for a in allocations:
            conn.execute(
                """INSERT INTO allocations (batch_id,month,meter_id,register,usage_kwh,
                    exact_share,tou_adjusted,sources_json,source_ids_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (b, a["month"], a["meter_id"], a["register"], str(a["usage_kwh"]),
                 str(a["exact_share"]), 1 if a.get("tou_adjusted") else 0,
                 json.dumps(a.get("sources", []), ensure_ascii=False),
                 json.dumps(a.get("source_ids", []), ensure_ascii=False)))
        for bill in bills:
            conn.execute(
                """INSERT INTO bills (batch_id,month,meter_id,usage_kwh,amount_yuan,
                    components_json,adjust_cents) VALUES (?,?,?,?,?,?,?)""",
                (b, bill["month"], bill["meter_id"], str(bill["usage_kwh"]),
                 str(bill["amount_yuan"]),
                 json.dumps(bill["components"], ensure_ascii=False),
                 int(bill.get("adjust_cents", 0))))
        for q in quarantine:
            conn.execute(
                """INSERT INTO quarantine (batch_id,lineno,segment,code,stage,level,
                    field,raw_value,meter_id,detail,source_id,source_name,snapshot_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b, q["lineno"], q.get("segment", 1), q["code"], q["stage"],
                 q["level"], q.get("field"), q.get("raw_value"),
                 q.get("meter_id"), q.get("detail"), q.get("source_id"),
                 q.get("source_name"), q.get("snapshot_id")))
        for item in coverage:
            conn.execute(
                """INSERT INTO coverage (batch_id,meter_id,name,status,valid_readings,
                    estimate_readings,quarantine_readings,interval_count,sources_json,
                    detail) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (b, item["meter_id"], item["name"], item["status"],
                 item["valid_readings"], item["estimate_readings"],
                 item["quarantine_readings"], item["interval_count"],
                 json.dumps(item.get("sources", []), ensure_ascii=False),
                 item.get("detail", "")))
        for s in stages:
            conn.execute(
                """INSERT INTO stages (batch_id,stage,in_count,out_count,
                    quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                (b, s["stage"], s["in_count"], s["out_count"],
                 s["quarantine_count"], s["status"], s["elapsed_ms"],
                 s.get("note", "")))
        for key, value in totals.items():
            conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                         (b, key, str(value)))
        conn.execute(
            "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
            "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
            (b, json.dumps(manifest_json, ensure_ascii=False)))
        conn.execute(
            """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                    finished_at,input_digest,rule_version,params_json,note,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(batch_id) DO UPDATE SET
                 status=excluded.status, finished_at=excluded.finished_at,
                 run_id=excluded.run_id, note=excluded.note,
                 updated_at=excluded.updated_at""",
            (b, batch["batch_key"], batch["run_id"], batch["sample"],
             batch["status"], batch["created_at"], batch.get("finished_at"),
             batch["input_digest"], batch["rule_version"],
             json.dumps(batch.get("params", {}), ensure_ascii=False),
             batch.get("note", ""), batch.get("updated_at")))

    # ---------- 读取 ----------
    def load_meters(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM meters WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for r in rows:
            r["capacity"] = _dec(r["capacity"])
            r["multiplier"] = _dec(r["multiplier"])
            r["registers"] = r["registers"].split(";")
        return rows

    def load_readings(self, batch_id: str, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM readings WHERE batch_id=?"
        args: list = [batch_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY lineno"
        rows = self._all(sql, tuple(args))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            r["reading_norm"] = _dec(r["reading_norm"])
            r["source_ids"] = json.loads(r.pop("source_ids_json") or "[]")
        return rows

    def load_intervals(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM intervals WHERE batch_id=? ORDER BY meter_id, register, start_at",
            (batch_id,))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            r["source_ids"] = json.loads(r.pop("source_ids_json") or "[]")
            for key in ("prev_reading", "cur_reading", "delta", "multiplier", "usage_kwh",
                        "days"):
                r[key] = _dec(r[key])
        return rows

    def load_allocations(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM allocations WHERE batch_id=? ORDER BY month, meter_id, register",
            (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["exact_share"] = _dec(r["exact_share"])
            r["sources"] = json.loads(r.pop("sources_json") or "[]")
            r["source_ids"] = json.loads(r.pop("source_ids_json") or "[]")
            r["tou_adjusted"] = bool(r["tou_adjusted"])
        return rows

    def load_bills(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM bills WHERE batch_id=? ORDER BY month, meter_id",
                         (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["amount_yuan"] = _dec(r["amount_yuan"])
            r["components"] = json.loads(r.pop("components_json") or "[]")
        return rows

    def load_quarantine(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM quarantine WHERE batch_id=? ORDER BY lineno", (batch_id,))

    def load_coverage(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM coverage WHERE batch_id=? ORDER BY meter_id",
                         (batch_id,))
        for r in rows:
            r["sources"] = json.loads(r.pop("sources_json") or "[]")
        return rows

    def load_stages(self, batch_id: str) -> list[dict]:
        from . import config
        rows = self._all("SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for row in rows:
            row["label"] = config.STAGE_LABELS.get(row["stage"], row["stage"])
        return rows

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT json FROM manifests WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        tables = ("meters", "readings", "intervals", "allocations", "bills", "quarantine")
        with self.connect() as conn:
            return {t: conn.execute(
                f"SELECT COUNT(*) AS n FROM {t} WHERE batch_id=?", (batch_id,)
            ).fetchone()["n"] for t in tables}

    # ---------- 多来源包：原件快照 ----------
    def put_blob(self, sha256: str, payload: bytes, created_at: str) -> None:
        """原件按内容寻址存一份；同一内容只存一次，永不覆盖。"""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO source_blobs (sha256,bytes,byte_len,created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(sha256) DO NOTHING",
                (sha256, sqlite3.Binary(payload), len(payload), created_at))

    def get_blob(self, sha256: str) -> bytes | None:
        with self.connect() as conn:
            row = conn.execute("SELECT bytes FROM source_blobs WHERE sha256=?",
                               (sha256,)).fetchone()
        return bytes(row["bytes"]) if row else None

    # ---------- 多来源包：包、来源、快照、版本 ----------
    def get_package(self, package_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM packages WHERE package_id=?",
                               (package_id,)).fetchone()
        return _package_row(dict(row)) if row else None

    def find_package_by_key(self, package_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM packages WHERE package_key=? ORDER BY created_at LIMIT 1",
                (package_key,)).fetchone()
        return _package_row(dict(row)) if row else None

    def list_packages(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM packages ORDER BY COALESCE(updated_at, created_at) DESC, "
                "rowid DESC LIMIT ?", (limit,)).fetchall()
        return [_package_row(dict(r)) for r in rows]

    def create_package(self, *, package_id: str, package_key: str, status: str,
                       expected_sources: list[dict], created_at: str,
                       note: str = "") -> dict:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO packages (package_id,package_key,status,
                    expected_sources_json,created_at,updated_at,latest_version,
                    published_version,published_batch_id,note)
                   VALUES (?,?,?,?,?,?,0,NULL,NULL,?)""",
                (package_id, package_key, status,
                 json.dumps(expected_sources, ensure_ascii=False), created_at,
                 created_at, note))
        return self.get_package(package_id) or {}

    def update_package(self, package_id: str, **fields) -> None:
        if not fields:
            return
        if "expected_sources" in fields:
            fields["expected_sources_json"] = json.dumps(
                fields.pop("expected_sources"), ensure_ascii=False)
        columns = ", ".join(f"{name}=?" for name in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE packages SET {columns} WHERE package_id=?",
                         (*fields.values(), package_id))

    def upsert_source(self, *, package_id: str, source_id: str, label: str,
                      terminal: str, status: str, current_snapshot_id: str | None,
                      stamp: str, note: str = "",
                      scope: list[str] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO package_sources (package_id,source_id,label,terminal,
                    status,current_snapshot_id,created_at,updated_at,retired_at,note,
                    scope_json,scope_derived)
                   VALUES (?,?,?,?,?,?,?,?,NULL,?,?,0)
                   ON CONFLICT(package_id,source_id) DO UPDATE SET
                     label=excluded.label, terminal=excluded.terminal,
                     status=excluded.status,
                     current_snapshot_id=excluded.current_snapshot_id,
                     updated_at=excluded.updated_at, note=excluded.note,
                     scope_json=excluded.scope_json""",
                (package_id, source_id, label, terminal, status, current_snapshot_id,
                 stamp, stamp, note,
                 json.dumps(scope or [], ensure_ascii=False)))

    def retire_source(self, package_id: str, source_id: str, stamp: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE package_sources SET status='retired', retired_at=?, updated_at=? "
                "WHERE package_id=? AND source_id=?",
                (stamp, stamp, package_id, source_id))

    def load_sources(self, package_id: str, *, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM package_sources WHERE package_id=?"
        args: list = [package_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY source_id"
        rows = self._all(sql, tuple(args))
        for row in rows:
            row["scope"] = json.loads(row.pop("scope_json") or "[]")
            row["scope_derived"] = bool(row.pop("scope_derived", 0))
        return rows

    def get_source(self, package_id: str, source_id: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM package_sources WHERE package_id=? AND source_id=?",
            (package_id, source_id))
        if not rows:
            return None
        row = rows[0]
        row["scope"] = json.loads(row.pop("scope_json") or "[]")
        row["scope_derived"] = bool(row.pop("scope_derived", 0))
        return row

    def set_source_scope(self, package_id: str, source_id: str,
                         scope: list[str], *, derived: bool = False) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE package_sources SET scope_json=?, scope_derived=? "
                "WHERE package_id=? AND source_id=?",
                (json.dumps(sorted(scope), ensure_ascii=False), 1 if derived else 0,
                 package_id, source_id))

    def insert_snapshot(self, snapshot: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO source_snapshots (snapshot_id,package_id,source_id,
                    filename,sha256,byte_len,status,ingest_code,ingest_detail,encoding,
                    segments,data_lines,parsed_lines,quarantine_count,received_at,
                    superseded_by,version_no)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot["snapshot_id"], snapshot["package_id"], snapshot["source_id"],
                 snapshot["filename"], snapshot["sha256"], snapshot["byte_len"],
                 snapshot["status"], snapshot.get("ingest_code"),
                 snapshot.get("ingest_detail", ""), snapshot.get("encoding"),
                 snapshot.get("segments", 0), snapshot.get("data_lines", 0),
                 snapshot.get("parsed_lines", 0), snapshot.get("quarantine_count", 0),
                 snapshot["received_at"], snapshot.get("superseded_by"),
                 snapshot.get("version_no", 0)))

    def supersede_snapshot(self, snapshot_id: str, by_snapshot_id: str,
                           version_no: int | None = None) -> None:
        """旧快照不删不改内容，只记「被哪一版补件接替」，历史取证仍可追溯。"""
        if version_no is None:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE source_snapshots SET superseded_by=? WHERE snapshot_id=?",
                    (by_snapshot_id, snapshot_id))
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE source_snapshots SET superseded_by=?, version_no=? "
                "WHERE snapshot_id=?", (by_snapshot_id, version_no, snapshot_id))

    def mark_snapshot_version(self, snapshot_id: str, version_no: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE source_snapshots SET version_no=? WHERE snapshot_id=?",
                         (version_no, snapshot_id))

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM source_snapshots WHERE snapshot_id=?",
                               (snapshot_id,)).fetchone()
        return dict(row) if row else None

    def load_snapshots(self, package_id: str, *, source_id: str | None = None,
                       ) -> list[dict]:
        sql = "SELECT * FROM source_snapshots WHERE package_id=?"
        args: list = [package_id]
        if source_id:
            sql += " AND source_id=?"
            args.append(source_id)
        sql += " ORDER BY received_at, rowid"
        return self._all(sql, tuple(args))

    def insert_version(self, version: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO package_versions (package_id,version_no,batch_id,status,
                    created_at,published_at,note,sources_json,digest,counters_json,
                    superseded_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (version["package_id"], version["version_no"], version["batch_id"],
                 version["status"], version["created_at"],
                 version.get("published_at"), version.get("note", ""),
                 json.dumps(version.get("sources", []), ensure_ascii=False),
                 version.get("digest", ""),
                 json.dumps(version.get("counters", {}), ensure_ascii=False),
                 version.get("superseded_by")))

    def mark_version_published(self, package_id: str, version_no: int,
                               stamp: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE package_versions SET status='published', published_at=? "
                "WHERE package_id=? AND version_no=?", (stamp, package_id, version_no))

    def mark_version_status(self, package_id: str, version_no: int, status: str,
                            stamp: str, note: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE package_versions SET status=?, published_at=?, note=? "
                "WHERE package_id=? AND version_no=?",
                (status, stamp, note, package_id, version_no))

    def load_versions(self, package_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM package_versions WHERE package_id=? ORDER BY version_no",
            (package_id,))
        for row in rows:
            row["sources"] = json.loads(row.pop("sources_json") or "[]")
            row["counters"] = json.loads(row.pop("counters_json") or "{}")
        return rows

    def get_version(self, package_id: str, version_no: int) -> dict | None:
        rows = self._all(
            "SELECT * FROM package_versions WHERE package_id=? AND version_no=?",
            (package_id, version_no))
        if not rows:
            return None
        row = rows[0]
        row["sources"] = json.loads(row.pop("sources_json") or "[]")
        row["counters"] = json.loads(row.pop("counters_json") or "{}")
        return row

    def latest_version(self, package_id: str) -> dict | None:
        rows = self.load_versions(package_id)
        return rows[-1] if rows else None

    # ---------- 多来源包：原子发布 ----------
    def publish_version(self, *, package_id: str, version_no: int, batch: dict,
                        meters: list[dict], readings: list[dict], intervals: list[dict],
                        allocations: list[dict], bills: list[dict],
                        quarantine: list[dict], stages: list[dict], totals: dict,
                        manifest_json: dict, coverage: list[dict],
                        version: dict, stamp: str) -> None:
        """一个事务里写「新版本结果 + 发布指针」。任一步失败整体回滚，
        已发布版本的数据与 `published_version` 都保持调用前的样子。"""
        with self.connect() as conn:
            self._write_run(conn, batch=batch, meters=meters, readings=readings,
                            intervals=intervals, allocations=allocations, bills=bills,
                            quarantine=quarantine, stages=stages, totals=totals,
                            manifest_json=manifest_json, coverage=coverage)
            conn.execute(
                """INSERT INTO package_versions (package_id,version_no,batch_id,status,
                    created_at,published_at,note,sources_json,digest,counters_json,
                    superseded_by)
                   VALUES (?,?,?,?,?,?,'',?,?,?,NULL)""",
                (version["package_id"], version_no, batch["batch_id"],
                 version["status"], version["created_at"], version.get("published_at"),
                 json.dumps(version.get("sources", []), ensure_ascii=False),
                 version.get("digest", ""),
                 json.dumps(version.get("counters", {}), ensure_ascii=False)))
            # 快照接替关系也在同一事务里推进：发布失败则历史快照不被改写
            for snapshot_id in version.get("current_snapshot_ids", []):
                conn.execute(
                    "UPDATE source_snapshots SET version_no=COALESCE(version_no, ?) "
                    "WHERE snapshot_id=?", (version_no, snapshot_id))
            previous = conn.execute(
                "SELECT published_version FROM packages WHERE package_id=?",
                (package_id,)).fetchone()
            if previous and previous["published_version"] is not None:
                conn.execute(
                    "UPDATE package_versions SET status='superseded', superseded_by=? "
                    "WHERE package_id=? AND version_no=? AND status='published'",
                    (version_no, package_id, previous["published_version"]))
            conn.execute(
                """UPDATE packages SET latest_version=?, published_version=?,
                    published_batch_id=?, status=?, updated_at=? WHERE package_id=?""",
                (version_no, version_no, batch["batch_id"], version["status"],
                 stamp, package_id))

    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _package_row(row: dict) -> dict:
    row["expected_sources"] = json.loads(row.pop("expected_sources_json") or "[]")
    return row
