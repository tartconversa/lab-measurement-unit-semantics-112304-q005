"""命令行入口。

用法示例::

    python3 -m lab_measurement_unit_semantics.cli init --db lake.db
    python3 -m lab_measurement_unit_semantics.cli ingest \
        --db lake.db --config config/sources/source_config.json
    python3 -m lab_measurement_unit_semantics.cli validate \
        --db lake.db --registry config/registry_v1.json \
        --rules config/validation_rules.json
    python3 -m lab_measurement_unit_semantics.cli policy \
        --db lake.db --policy config/policies/cross_lab_analyte.json
    python3 -m lab_measurement_unit_semantics.cli publish \
        --db lake.db --dataset cross-lab-analyte --policy POLICY-CROSS-LAB-ANALYTE \
        --registry-version REG-2026-01
    python3 -m lab_measurement_unit_semantics.cli lineage \
        --db lake.db --record-id SRC-LAB-B:lab_b_sensor.csv:r1:gas_conc
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .dataset_engine import DatasetEngine
from .loader import load_all
from .repository import Repository
from .registry import Registry
from .validation import ValidationConfig, evaluate


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(_render_text(payload))


def _render_text(payload: object, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(payload, dict):
        lines = []
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_render_text(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {value}")
        return "\n".join(lines)
    if isinstance(payload, list):
        if not payload:
            return f"{pad}（空）"
        return "\n".join(f"{pad}- " + _render_text(item, indent + 1).lstrip()
                         for item in payload)
    return f"{pad}{payload}"


def cmd_init(args: argparse.Namespace) -> None:
    repo = Repository(args.db)
    repo.close()
    _emit({"status": "ok", "db": args.db,
           "raw_records": 0}, args.json)


def cmd_ingest(args: argparse.Namespace) -> None:
    records, reports = load_all(args.config)
    with Repository(args.db) as repo:
        inserted = repo.insert_raw(records, _now())
    _emit({
        "status": "ok",
        "inserted": inserted,
        "duplicates_skipped": len(records) - inserted,
        "sources": [r.__dict__ for r in reports],
    }, args.json)


def cmd_validate(args: argparse.Namespace) -> None:
    registry = Registry.load(args.registry)
    config = ValidationConfig.load(args.rules)
    with Repository(args.db) as repo:
        raw_rows = repo.conn.execute(
            "SELECT * FROM raw_records ORDER BY record_id").fetchall()
        from .models import RawRecord
        records = [RawRecord(
            record_id=r["record_id"], source_id=r["source_id"],
            lab_id=r["lab_id"], sample_id=r["sample_id"],
            instrument_id=r["instrument_id"],
            instrument_model=r["instrument_model"],
            protocol_id=r["protocol_id"], metric_code=r["metric_code"],
            source_column=r["source_column"], raw_value=r["raw_value"],
            unit_code=r["unit_code"], measurement_kind=r["measurement_kind"],
            basis_code=r["basis_code"],
            conditions=json.loads(r["conditions_json"]),
            observed_at=r["observed_at"], source_file=r["source_file"],
            source_row=r["source_row"]) for r in raw_rows]
        rows = evaluate(records, registry, config)
        repo.replace_standardized(registry.registry_version, rows)
        counts = repo.disposition_counts(registry.registry_version)
        examples = [
            {"record_id": row.record_id, "disposition": row.disposition,
             "findings": [f.__dict__ for f in row.findings]}
            for row in rows if row.findings][:10]
    _emit({
        "status": "ok",
        "registry_version": registry.registry_version,
        "evaluated": len(rows),
        "disposition_counts": counts,
        "findings_examples": examples,
    }, args.json)


def cmd_policy(args: argparse.Namespace) -> None:
    with Repository(args.db) as repo:
        result = DatasetEngine(repo).register_policy(args.policy, _now())
    _emit({"status": "ok", **result}, args.json)


def cmd_publish(args: argparse.Namespace) -> None:
    with Repository(args.db) as repo:
        engine = DatasetEngine(repo)
        manifests = []
        for tier in (["shared", "restricted"] if args.tier == "both"
                     else [args.tier]):
            manifests.append(engine.publish(
                dataset_id=args.dataset, policy_id=args.policy,
                registry_version=args.registry_version, tier=tier,
                published_at=_now()))
    _emit({"status": "ok", "manifests": manifests}, args.json)


def cmd_lineage(args: argparse.Namespace) -> None:
    with Repository(args.db) as repo:
        lineage = repo.full_lineage(args.record_id)
    if lineage["raw"] is None:
        print(f"未找到记录: {args.record_id}", file=sys.stderr)
        sys.exit(1)
    _emit(lineage, args.json)


def cmd_versions(args: argparse.Namespace) -> None:
    with Repository(args.db) as repo:
        versions = [dict(v) for v in
                    repo.list_dataset_versions(args.dataset)]
        for v in versions:
            v["manifest"] = json.loads(v.pop("manifest_json"))
    _emit({"dataset_id": args.dataset, "versions": versions}, args.json)


def cmd_diff_registry(args: argparse.Namespace) -> None:
    """对比同一批记录在两个注册表版次下的处置差异（标准修订影响分析）。"""
    with Repository(args.db) as repo:
        old = {r["record_id"]: r for r in
               repo.list_standardized(args.old_version)}
        new = {r["record_id"]: r for r in
               repo.list_standardized(args.new_version)}
    changes = []
    for record_id in sorted(old):
        before, after = old[record_id], new.get(record_id)
        if after is None:
            changes.append({"record_id": record_id,
                            "change": "missing_in_new_registry"})
            continue
        if (before["disposition"] != after["disposition"]
                or before["standard_value"] != after["standard_value"]
                or before["conversion_id"] != after["conversion_id"]):
            changes.append({
                "record_id": record_id,
                "disposition": [before["disposition"],
                                after["disposition"]],
                "standard_value": [before["standard_value"],
                                   after["standard_value"]],
                "conversion_id": [before["conversion_id"],
                                  after["conversion_id"]],
            })
    _emit({"old_registry": args.old_version,
           "new_registry": args.new_version,
           "changed_records": changes}, args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab_measurement_unit_semantics",
        description="异构科研测量语义化装载与版本化共享数据集工具")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化数据湖数据库")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="按来源配置装载异构测量文件")
    p.add_argument("--db", required=True)
    p.add_argument("--config", required=True)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("validate", help="按注册表版次执行语义校验与标准化")
    p.add_argument("--db", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--rules", default=None)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("policy", help="登记研究分析人员的指标合并策略")
    p.add_argument("--db", required=True)
    p.add_argument("--policy", required=True)
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("publish", help="发布共享/受限数据集新版本")
    p.add_argument("--db", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--registry-version", required=True)
    p.add_argument("--tier", choices=["shared", "restricted", "both"],
                   default="both")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("lineage", help="查询单条记录的完整血缘")
    p.add_argument("--db", required=True)
    p.add_argument("--record-id", required=True)
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("versions", help="列出数据集全部已发布版本")
    p.add_argument("--db", required=True)
    p.add_argument("--dataset", required=True)
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("diff-registry", help="对比两个注册表版次下的评估差异")
    p.add_argument("--db", required=True)
    p.add_argument("--old-version", required=True)
    p.add_argument("--new-version", required=True)
    p.set_defaults(func=cmd_diff_registry)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI 统一错误出口
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
