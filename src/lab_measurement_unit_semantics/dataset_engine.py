"""版本化共享数据集生成引擎。

研究分析人员通过合并策略（merge policy）声明**允许合并**的语义键集合。
引擎只按标准化阶段生成的 ``comparability_key`` 合流，列名从不参与合并判断；
若两个来源使用相同列名却落在不同语义键上，引擎会在清单中给出
``column_name_collision`` 告警，防止“列名相同即合并”的错误。

数据集分两个层级发布：

* ``shared``：仅含 ``converted`` 行，可跨实验室共享分析；
* ``restricted``：额外纳入 ``retained_original`` 行（原单位保留），
  并在清单中逐行标注原因；``excluded`` 行永远不入数据集，只在清单留痕。
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from pathlib import Path

from .repository import Repository


class PolicyError(ValueError):
    """合并策略错误。"""


class DatasetEngine:
    def __init__(self, repo: Repository):
        self.repo = repo

    def register_policy(self, policy_path: str, created_at: str) -> dict[str, Any]:
        """登记合并策略，并在登记期做语义/列名冲突预检。"""
        policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        policy_id = policy["policy_id"]
        allow_keys = policy["allow_comparability_keys"]
        if not allow_keys:
            raise PolicyError(f"策略 {policy_id} 未声明任何允许合并的语义键")

        collisions = self._detect_collisions(policy["registry_version"],
                                             allow_keys)
        self.repo.save_policy(
            policy_id=policy_id,
            description=policy.get("description", ""),
            allow_keys=allow_keys,
            created_by=policy.get("created_by", "research-analyst"),
            created_at=created_at)
        return {"policy_id": policy_id, "allow_keys": allow_keys,
                "column_name_collisions": collisions}

    def _detect_collisions(self, registry_version: str,
                           allow_keys: list[str]) -> list[dict[str, Any]]:
        """同名列、不同语义键 → 冲突告警（信息性，不自动阻断，由分析人员裁决）。"""
        column_map: dict[str, set[str]] = defaultdict(set)
        for row in self.repo.list_standardized(registry_version):
            if row["comparability_key"] not in allow_keys:
                continue
            raw = self.repo.get_raw(row["record_id"])
            if raw is not None:
                column_map[raw["source_column"]].add(row["comparability_key"])
        return [{"source_column": col, "semantic_keys": sorted(keys)}
                for col, keys in sorted(column_map.items()) if len(keys) > 1]

    def publish(self, dataset_id: str, policy_id: str, registry_version: str,
                tier: str, published_at: str) -> dict[str, Any]:
        """发布一个数据集版本，返回版本号与清单。"""
        if tier not in ("shared", "restricted"):
            raise PolicyError("数据集层级只能是 shared 或 restricted")
        policy_row = self.repo.get_policy(policy_id)
        if policy_row is None:
            raise PolicyError(f"合并策略不存在: {policy_id}")
        allow_keys = set(json.loads(policy_row["allow_keys_json"]))

        std_rows = self.repo.list_standardized(registry_version)
        out_rows: list[dict[str, Any]] = []
        stats = {"converted": 0, "retained_original": 0, "excluded": 0,
                 "not_in_policy": 0}
        retained_reasons: list[dict[str, Any]] = []
        excluded_audit: list[dict[str, Any]] = []
        not_in_policy_audit: list[dict[str, Any]] = []
        merged_keys: dict[str, int] = defaultdict(int)

        for row in std_rows:
            key = row["comparability_key"]
            if key not in allow_keys:
                stats["not_in_policy"] += 1
                not_in_policy_audit.append({
                    "record_id": row["record_id"],
                    "comparability_key": key,
                    "reason": "语义键不在研究分析人员授权的合并策略内"})
                continue
            if row["disposition"] == "excluded":
                stats["excluded"] += 1
                excluded_audit.append({
                    "record_id": row["record_id"],
                    "findings": json.loads(row["findings_json"])})
                continue
            if row["disposition"] == "retained_original":
                stats["retained_original"] += 1
                if tier == "shared":
                    continue  # 共享层不接受原单位保留行
                retained_reasons.append({
                    "record_id": row["record_id"],
                    "findings": json.loads(row["findings_json"])})
            else:
                stats["converted"] += 1

            raw = self.repo.get_raw(row["record_id"])
            out_rows.append({
                "record_id": row["record_id"],
                "comparability_key": key,
                "disposition": row["disposition"],
                "standard_value": row["standard_value"],
                "standard_unit": row["standard_unit"],
                "raw_value": row["raw_value"],
                "raw_unit": row["raw_unit"],
                "metric_code": row["metric_code"],
                "basis_code": row["basis_code"],
                "measurement_kind": row["measurement_kind"],
                "conditions": json.loads(row["conditions_json"]),
                "lineage": json.loads(row["lineage_json"]),
                "source_column": raw["source_column"] if raw else None,
                "lab_id": raw["lab_id"] if raw else None,
                "sample_id": raw["sample_id"] if raw else None,
            })
            merged_keys[key] += 1
        manifest = {
            "dataset_id": dataset_id,
            "registry_version": registry_version,
            "policy_id": policy_id,
            "tier": tier,
            "published_at": published_at,
            "row_count": len(out_rows),
            "stats": stats,
            "merged_semantic_keys": dict(sorted(merged_keys.items())),
            "column_name_collisions": self._detect_collisions(
                registry_version, list(allow_keys)),
            "retained_original_records": retained_reasons,
            "excluded_records_audit": excluded_audit,
            "not_in_policy_audit": not_in_policy_audit,
        }
        version = self.repo.publish_dataset(
            dataset_id=dataset_id, registry_version=registry_version,
            policy_id=policy_id, tier=tier, published_at=published_at,
            manifest=manifest, rows=out_rows)
        manifest["dataset_version"] = version
        return manifest
