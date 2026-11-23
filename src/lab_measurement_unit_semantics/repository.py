"""数据湖 SQLite 存储库。

分层模型：

* **raw_records（原始层）**：装载即固化，按 record_id 去重，不提供更新/删除接口；
* **standardized_rows（标准化层）**：按注册表版次分区。标准修订后以新版次
  重新评估，得到一组全新的行，旧版次保留可查；
* **datasets / dataset_rows（共享层）**：同一数据集每次发布生成递增版本，
  每行记录 converted / retained_original / excluded 处置与血缘；
* **merge_policies**：研究分析人员配置的指标合并策略。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import RawRecord, StandardizedRow, to_plain

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    record_id        TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    lab_id           TEXT NOT NULL,
    sample_id        TEXT NOT NULL,
    instrument_id    TEXT NOT NULL,
    instrument_model TEXT NOT NULL,
    protocol_id      TEXT NOT NULL,
    metric_code      TEXT NOT NULL,
    source_column    TEXT NOT NULL,
    raw_value        REAL NOT NULL,
    unit_code        TEXT NOT NULL,
    measurement_kind TEXT NOT NULL,
    basis_code       TEXT NOT NULL,
    conditions_json  TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    source_file      TEXT NOT NULL,
    source_row       INTEGER NOT NULL,
    ingested_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS standardized_rows (
    registry_version      TEXT NOT NULL,
    record_id             TEXT NOT NULL,
    metric_code           TEXT NOT NULL,
    basis_code            TEXT NOT NULL,
    measurement_kind      TEXT NOT NULL,
    comparability_key     TEXT NOT NULL,
    raw_value             REAL NOT NULL,
    raw_unit              TEXT NOT NULL,
    standard_value        REAL,
    standard_unit         TEXT,
    conversion_id         TEXT,
    conditions_json       TEXT NOT NULL,
    condition_fingerprint TEXT NOT NULL,
    disposition           TEXT NOT NULL,
    findings_json         TEXT NOT NULL,
    lineage_json          TEXT NOT NULL,
    PRIMARY KEY (registry_version, record_id)
);
CREATE INDEX IF NOT EXISTS idx_std_key
    ON standardized_rows(registry_version, comparability_key);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id        TEXT NOT NULL,
    dataset_version   INTEGER NOT NULL,
    registry_version  TEXT NOT NULL,
    policy_id         TEXT NOT NULL,
    tier              TEXT NOT NULL,
    published_at      TEXT NOT NULL,
    manifest_json     TEXT NOT NULL,
    PRIMARY KEY (dataset_id, dataset_version)
);
CREATE TABLE IF NOT EXISTS dataset_rows (
    dataset_id        TEXT NOT NULL,
    dataset_version   INTEGER NOT NULL,
    record_id         TEXT NOT NULL,
    comparability_key TEXT NOT NULL,
    disposition       TEXT NOT NULL,
    standard_value    REAL,
    standard_unit     TEXT,
    raw_value         REAL NOT NULL,
    raw_unit          TEXT NOT NULL,
    metric_code       TEXT NOT NULL,
    basis_code        TEXT NOT NULL,
    measurement_kind  TEXT NOT NULL,
    lab_id            TEXT,
    sample_id         TEXT,
    source_column     TEXT,
    conditions_json   TEXT NOT NULL,
    lineage_json      TEXT NOT NULL,
    PRIMARY KEY (dataset_id, dataset_version, record_id)
);
CREATE TABLE IF NOT EXISTS merge_policies (
    policy_id       TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    allow_keys_json TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------- 原始层 ----------------
    def insert_raw(self, records: list[RawRecord], ingested_at: str) -> int:
        """写入原始记录，重复 record_id 跳过。返回新写入条数。"""
        sql = """INSERT OR IGNORE INTO raw_records VALUES (
            :record_id,:source_id,:lab_id,:sample_id,:instrument_id,
            :instrument_model,:protocol_id,:metric_code,:source_column,
            :raw_value,:unit_code,:measurement_kind,:basis_code,
            :conditions_json,:observed_at,:source_file,:source_row,:ingested_at)"""
        payload = [{
            **to_plain(r),
            "conditions_json": json.dumps(r.conditions, ensure_ascii=False,
                                          sort_keys=True),
            "ingested_at": ingested_at,
        } for r in records]
        before = self.conn.total_changes
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return self.conn.total_changes - before

    def count_raw(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]

    def get_raw(self, record_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM raw_records WHERE record_id=?",
            (record_id,)).fetchone()

    # ---------------- 标准化层 ----------------
    def replace_standardized(self, registry_version: str,
                             rows: list[StandardizedRow]) -> None:
        """以某注册表版次全量替换该版次的标准化评估结果（重跑幂等）。"""
        self.conn.execute(
            "DELETE FROM standardized_rows WHERE registry_version=?",
            (registry_version,))
        sql = """INSERT INTO standardized_rows VALUES (
            :registry_version,:record_id,:metric_code,:basis_code,
            :measurement_kind,:comparability_key,:raw_value,:raw_unit,
            :standard_value,:standard_unit,:conversion_id,
            :conditions_json,:condition_fingerprint,:disposition,
            :findings_json,:lineage_json)"""
        payload = [{
            "registry_version": r.registry_version,
            "record_id": r.record_id,
            "metric_code": r.metric_code,
            "basis_code": r.basis_code,
            "measurement_kind": r.measurement_kind,
            "comparability_key": r.comparability_key,
            "raw_value": r.raw_value,
            "raw_unit": r.raw_unit,
            "standard_value": r.standard_value,
            "standard_unit": r.standard_unit,
            "conversion_id": r.conversion_id,
            "conditions_json": json.dumps(r.conditions, ensure_ascii=False,
                                          sort_keys=True),
            "condition_fingerprint": r.condition_fingerprint,
            "disposition": r.disposition,
            "findings_json": json.dumps(to_plain(r.findings),
                                        ensure_ascii=False),
            "lineage_json": json.dumps(r.lineage, ensure_ascii=False,
                                       sort_keys=True),
        } for r in rows]
        self.conn.executemany(sql, payload)
        self.conn.commit()

    def get_standardized(self, registry_version: str,
                         record_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM standardized_rows WHERE registry_version=? "
            "AND record_id=?", (registry_version, record_id)).fetchone()

    def list_standardized(self, registry_version: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM standardized_rows WHERE registry_version=? "
            "ORDER BY record_id", (registry_version,)))

    def disposition_counts(self, registry_version: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT disposition, COUNT(*) AS n FROM standardized_rows "
            "WHERE registry_version=? GROUP BY disposition",
            (registry_version,)).fetchall()
        return {r["disposition"]: r["n"] for r in rows}

    # ---------------- 合并策略 ----------------
    def save_policy(self, policy_id: str, description: str,
                    allow_keys: list[str], created_by: str,
                    created_at: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO merge_policies VALUES (?,?,?,?,?)",
            (policy_id, description,
             json.dumps(allow_keys, ensure_ascii=False),
             created_by, created_at))
        self.conn.commit()

    def get_policy(self, policy_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM merge_policies WHERE policy_id=?",
            (policy_id,)).fetchone()

    # ---------------- 共享数据集版本 ----------------
    def next_dataset_version(self, dataset_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(dataset_version) AS v FROM datasets WHERE dataset_id=?",
            (dataset_id,)).fetchone()
        return (row["v"] or 0) + 1

    def publish_dataset(self, dataset_id: str, registry_version: str,
                        policy_id: str, tier: str, published_at: str,
                        manifest: dict[str, Any],
                        rows: list[dict[str, Any]]) -> int:
        version = self.next_dataset_version(dataset_id)
        self.conn.execute(
            "INSERT INTO datasets VALUES (?,?,?,?,?,?,?)",
            (dataset_id, version, registry_version, policy_id, tier,
             published_at, json.dumps(manifest, ensure_ascii=False, indent=2)))
        sql = """INSERT INTO dataset_rows VALUES (
            :dataset_id,:dataset_version,:record_id,:comparability_key,
            :disposition,:standard_value,:standard_unit,:raw_value,:raw_unit,
            :metric_code,:basis_code,:measurement_kind,
            :lab_id,:sample_id,:source_column,
            :conditions_json,:lineage_json)"""
        for i, r in enumerate(rows):
            r = dict(r)
            r.update(dataset_id=dataset_id, dataset_version=version)
            r["conditions_json"] = json.dumps(r.pop("conditions"),
                                               ensure_ascii=False, sort_keys=True)
            r["lineage_json"] = json.dumps(r.pop("lineage"),
                                           ensure_ascii=False, sort_keys=True)
            rows[i] = r
        self.conn.executemany(sql, rows)
        self.conn.commit()
        return version

    def list_dataset_versions(self, dataset_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM datasets WHERE dataset_id=? ORDER BY dataset_version",
            (dataset_id,)))

    def get_dataset_manifest(self, dataset_id: str, version: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT manifest_json FROM datasets WHERE dataset_id=? "
            "AND dataset_version=?", (dataset_id, version)).fetchone()
        return json.loads(row["manifest_json"]) if row else {}

    def list_dataset_rows(self, dataset_id: str,
                          version: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM dataset_rows WHERE dataset_id=? AND dataset_version=? "
            "ORDER BY comparability_key, record_id", (dataset_id, version)))

    def full_lineage(self, record_id: str) -> dict[str, Any]:
        """汇总一条记录从原始值到各注册表版次、各数据集版本的完整血缘。"""
        raw = self.get_raw(record_id)
        result: dict[str, Any] = {
            "raw": dict(raw) if raw is not None else None,
            "standardized": [], "datasets": []}
        for row in self.conn.execute(
                "SELECT * FROM standardized_rows WHERE record_id=? "
                "ORDER BY registry_version", (record_id,)):
            item = dict(row)
            item["findings"] = json.loads(item.pop("findings_json"))
            item["lineage"] = json.loads(item.pop("lineage_json"))
            item["conditions"] = json.loads(item.pop("conditions_json"))
            result["standardized"].append(item)
        for row in self.conn.execute(
                "SELECT * FROM dataset_rows WHERE record_id=? "
                "ORDER BY dataset_id, dataset_version", (record_id,)):
            item = dict(row)
            item["conditions"] = json.loads(item.pop("conditions_json"))
            item["lineage"] = json.loads(item.pop("lineage_json"))
            result["datasets"].append(item)
        return result
