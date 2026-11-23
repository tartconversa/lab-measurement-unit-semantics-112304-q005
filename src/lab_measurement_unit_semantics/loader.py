"""异构测量数据可配置装载器。

数据工程人员通过来源配置（JSON）描述每个设备导出文件的结构：
列名与指标语义的绑定必须显式声明，系统绝不因为两个文件列名相同就认为
它们是同一个科学量。装载产物是原始层 :class:`RawRecord`，保留原始数值、
单位、样品、设备、方案与环境条件，不做任何换算。

支持 CSV 与 JSONL 两种文件格式；宽表（一个文件多列指标）会被熔化为长表，
每个测量列生成一条独立记录。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import RawRecord

MISSING_TOKENS = {"", "na", "n/a", "null", "none", "nan"}


class LoaderConfigError(ValueError):
    """来源配置错误。"""


@dataclass
class LoadReport:
    source_id: str
    source_file: str
    total_rows: int = 0
    loaded_records: int = 0
    skipped_missing: int = 0


def _is_missing(value: Any) -> bool:
    return value is None or str(value).strip().lower() in MISSING_TOKENS


def _read_rows(path: Path, fmt: str, encoding: str) -> list[dict[str, str]]:
    if fmt == "csv":
        with path.open(encoding=encoding, newline="") as stream:
            return list(csv.DictReader(stream))
    if fmt == "jsonl":
        rows = []
        with path.open(encoding=encoding) as stream:
            for line in stream:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    raise LoaderConfigError(f"不支持的文件格式: {fmt}（仅支持 csv/jsonl）")


def load_source(source_cfg: dict[str, Any], base_dir: Path) -> tuple[list[RawRecord], LoadReport]:
    """装载单个来源文件，返回原始记录与装载报告。"""
    required = ["source_id", "lab_id", "instrument_id", "instrument_model",
                "protocol_id", "file", "measurements"]
    for key in required:
        if key not in source_cfg:
            raise LoaderConfigError(f"来源配置缺少必填字段: {key}")

    source_id = source_cfg["source_id"]
    file_path = (base_dir / source_cfg["file"]).resolve()
    if not file_path.is_relative_to(base_dir.resolve()):
        raise LoaderConfigError(
            f"来源文件 {file_path} 越出配置目录，拒绝装载（防路径穿越）")
    if not file_path.exists():
        raise LoaderConfigError(f"来源文件不存在: {file_path}")

    fmt = source_cfg.get("format", "csv")
    encoding = source_cfg.get("encoding", "utf-8-sig")
    rows = _read_rows(file_path, fmt, encoding)
    report = LoadReport(source_id=source_id, source_file=str(file_path))

    id_columns = source_cfg.get("columns", {})
    sample_col = id_columns.get("sample_id", "sample_id")
    time_col = id_columns.get("observed_at", "observed_at")
    condition_cols: dict[str, str] = id_columns.get("conditions", {})
    default_conditions: dict[str, Any] = source_cfg.get("default_conditions", {})

    seen_pairs: set[tuple[str, str, str]] = set()
    records: list[RawRecord] = []
    for row_index, row in enumerate(rows, start=1):
        report.total_rows += 1
        for col_name in (sample_col, time_col):
            if col_name not in row:
                raise LoaderConfigError(
                    f"{file_path} 第 {row_index} 行缺少标识列 {col_name}")
        conditions = dict(default_conditions)
        for cond_name, cond_col in condition_cols.items():
            if cond_col in row and not _is_missing(row[cond_col]):
                conditions[cond_name] = _auto_number(row[cond_col])

        for meas in source_cfg["measurements"]:
            column = meas["column"]
            if column not in row:
                raise LoaderConfigError(
                    f"{file_path} 缺少测量列 {column}（来源 {source_id}）")
            raw = row[column]
            if _is_missing(raw):
                report.skipped_missing += 1
                continue
            try:
                raw_value = float(raw)
            except (TypeError, ValueError) as exc:
                raise LoaderConfigError(
                    f"{file_path} 第 {row_index} 行列 {column} "
                    f"数值无法解析: {raw!r}") from exc

            for key in ("metric_code", "unit_code", "measurement_kind", "basis_code"):
                if key not in meas:
                    raise LoaderConfigError(
                        f"来源 {source_id} 测量列 {column} 缺少 {key}")
            pair = (row[sample_col], row[time_col], meas["metric_code"])
            if pair in seen_pairs:
                raise LoaderConfigError(
                    f"来源 {source_id} 存在重复测量 "
                    f"(sample={pair[0]}, time={pair[1]}, metric={pair[2]})")
            seen_pairs.add(pair)

            record_id = f"{source_id}:{file_path.name}:r{row_index}:{column}"
            records.append(RawRecord(
                record_id=record_id,
                source_id=source_id,
                lab_id=source_cfg["lab_id"],
                sample_id=str(row[sample_col]),
                instrument_id=source_cfg["instrument_id"],
                instrument_model=source_cfg["instrument_model"],
                protocol_id=source_cfg["protocol_id"],
                metric_code=meas["metric_code"],
                source_column=column,
                raw_value=raw_value,
                unit_code=meas["unit_code"],
                measurement_kind=meas["measurement_kind"],
                basis_code=meas["basis_code"],
                conditions=conditions,
                observed_at=str(row[time_col]),
                source_file=str(file_path),
                source_row=row_index,
            ))
            report.loaded_records += 1
    return records, report


def load_all(config_path: str | Path) -> tuple[list[RawRecord], list[LoadReport]]:
    """按来源配置装载全部文件。"""
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    base_dir = config_path.parent.resolve()
    all_records: list[RawRecord] = []
    reports: list[LoadReport] = []
    source_ids: set[str] = set()
    for source_cfg in config.get("sources", []):
        if source_cfg["source_id"] in source_ids:
            raise LoaderConfigError(
                f"来源 ID 重复: {source_cfg['source_id']}")
        source_ids.add(source_cfg["source_id"])
        records, report = load_source(source_cfg, base_dir)
        all_records.extend(records)
        reports.append(report)
    return all_records, reports


def _auto_number(value: str) -> Any:
    try:
        return float(value)
    except ValueError:
        return value
