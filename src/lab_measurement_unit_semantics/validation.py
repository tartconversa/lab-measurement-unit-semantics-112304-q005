"""语义校验与标准化引擎。

校验规则（规则号稳定，严重级别可在规则配置中覆盖）：

========= ============================================================
规则号    含义
========= ============================================================
U001      单位代码未在注册表登记（error，排除）
D001      单位量纲与指标量纲不一致（error，排除）
M001      记录声明的测量性质与单位登记的测量性质冲突（error，排除）
M002      指标不允许该测量性质（如把温度设定值并入被测温度）（error）
B001      指标×基准条件规则未登记，基准不可确认（warning，受限保留）
B002      基准必需的环境条件字段缺失（warning，受限保留，禁止补造换算）
R001      基准规则处于 draft/retired 等未批准状态（warning，受限保留）
C001      模板换算所需条件缺失，无法精确换算（warning，受限保留）
N001      标准化结果超出指标合理区间（info，不阻断）
========= ============================================================

处置原则：
* 任一 error → ``excluded``，不进入任何共享数据集；
* 任一 warning → ``retained_original``，只进入受限数据集，保留原单位，
  绝不补造精确换算值；
* 全部通过且换算成功 → ``converted``，进入可共享数据集。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (RawRecord, StandardizedRow, ValidationFinding,
                     SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)
from .registry import Registry, RegistryError

DEFAULT_SEVERITY: dict[str, str] = {
    "U001": SEVERITY_ERROR,
    "D001": SEVERITY_ERROR,
    "M001": SEVERITY_ERROR,
    "M002": SEVERITY_ERROR,
    "B001": SEVERITY_WARNING,
    "B002": SEVERITY_WARNING,
    "R001": SEVERITY_WARNING,
    "C001": SEVERITY_WARNING,
    "N001": SEVERITY_INFO,
}


@dataclass
class ValidationConfig:
    severity: dict[str, str]

    @classmethod
    def load(cls, path: str | Path | None) -> "ValidationConfig":
        severity = dict(DEFAULT_SEVERITY)
        if path is not None:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for code, level in data.get("severity_overrides", {}).items():
                if code not in DEFAULT_SEVERITY:
                    raise ValueError(f"规则配置引用了未知规则号: {code}")
                if level not in (SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO):
                    raise ValueError(f"规则 {code} 的严重级别非法: {level}")
                severity[code] = level
        return cls(severity=severity)

    def finding(self, code: str, message: str,
                detail: dict[str, Any] | None = None) -> ValidationFinding:
        return ValidationFinding(code=code, severity=self.severity[code],
                                 message=message, detail=detail or {})


def condition_fingerprint(conditions: dict[str, Any]) -> str:
    """环境条件指纹：用于审计与下游按条件筛选，不参与单位换算。"""
    payload = json.dumps(conditions, sort_keys=True, ensure_ascii=False,
                         default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _comparability_key(metric_code: str, basis_code: str, kind: str,
                       group: str | None) -> str:
    # 语义合并键：指标 + 基准条件 + 测量性质 + 可比组。
    # 列名不参与键计算——同名列若语义不同，键自然不同。
    return f"{metric_code}|{basis_code}|{kind}|{group or basis_code}"


def evaluate(records: list[RawRecord], registry: Registry,
             config: ValidationConfig) -> list[StandardizedRow]:
    """在指定注册表版次下评估全部原始记录，产出标准化行。"""
    rows: list[StandardizedRow] = []
    for record in records:
        rows.append(_evaluate_one(record, registry, config))
    return rows


def _evaluate_one(record: RawRecord, registry: Registry,
                  config: ValidationConfig) -> StandardizedRow:
    findings: list[ValidationFinding] = []
    fingerprint = condition_fingerprint(record.conditions)
    lineage = {
        "record_id": record.record_id,
        "source_id": record.source_id,
        "source_file": record.source_file,
        "source_row": record.source_row,
        "source_column": record.source_column,
        "raw_value": record.raw_value,
        "raw_unit": record.unit_code,
        "registry_version": registry.registry_version,
    }

    # ---- 第一阶段：结构性错误（单位/量纲/测量性质）----
    unit = None
    metric = None
    try:
        unit = registry.unit(record.unit_code)
    except RegistryError as exc:
        findings.append(config.finding("U001", str(exc),
                                       {"unit_code": record.unit_code}))
    try:
        metric = registry.metric(record.metric_code)
    except RegistryError as exc:
        findings.append(config.finding("U001", str(exc),
                                       {"metric_code": record.metric_code}))

    if unit is not None and metric is not None:
        if unit.dimension != metric.dimension:
            findings.append(config.finding(
                "D001",
                f"单位 {unit.unit_code} 量纲 {unit.dimension} 与指标 "
                f"{metric.metric_code} 量纲 {metric.dimension} 不一致",
                {"unit_dimension": unit.dimension,
                 "metric_dimension": metric.dimension}))
        if unit.measurement_kind != record.measurement_kind:
            findings.append(config.finding(
                "M001",
                f"记录测量性质 {record.measurement_kind} 与单位登记性质 "
                f"{unit.measurement_kind} 冲突",
                {"record_kind": record.measurement_kind,
                 "unit_kind": unit.measurement_kind}))
        if record.measurement_kind not in metric.allowed_kinds:
            findings.append(config.finding(
                "M002",
                f"指标 {metric.metric_code} 不允许测量性质 "
                f"{record.measurement_kind}（允许：{list(metric.allowed_kinds)}）",
                {"metric_code": metric.metric_code,
                 "kind": record.measurement_kind}))

    has_error = any(f.severity == SEVERITY_ERROR for f in findings)
    if has_error:
        return StandardizedRow(
            record_id=record.record_id,
            registry_version=registry.registry_version,
            metric_code=record.metric_code,
            basis_code=record.basis_code,
            measurement_kind=record.measurement_kind,
            comparability_key=_comparability_key(
                record.metric_code, record.basis_code,
                record.measurement_kind, None),
            raw_value=record.raw_value, raw_unit=record.unit_code,
            standard_value=None, standard_unit=None, conversion_id=None,
            conditions=dict(record.conditions), condition_fingerprint=fingerprint,
            disposition="excluded", findings=findings, lineage=lineage)

    assert unit is not None and metric is not None

    # ---- 第二阶段：基准条件确认（领域管理员规则）----
    rule = registry.basis_rule(record.metric_code, record.basis_code)
    group: str | None = None
    if rule is None:
        findings.append(config.finding(
            "B001",
            f"指标 {record.metric_code} 的基准条件 {record.basis_code} "
            "未登记，语义基准不可确认",
            {"metric_code": record.metric_code,
             "basis_code": record.basis_code}))
    else:
        group = rule.comparability_group
        missing = [f for f in rule.required_fields
                   if f not in record.conditions
                   or record.conditions[f] in (None, "")]
        if missing:
            findings.append(config.finding(
                "B002",
                f"基准 {record.basis_code} 必需环境字段缺失: {missing}，"
                "不得补造精确换算值",
                {"missing_fields": missing,
                 "required_fields": list(rule.required_fields)}))
        if rule.approval_state != "approved":
            findings.append(config.finding(
                "R001",
                f"基准规则状态为 {rule.approval_state}（rule_version="
                f"{rule.rule_version}），仅可进入受限数据集",
                {"approval_state": rule.approval_state,
                 "rule_version": rule.rule_version}))

    has_warning = any(f.severity == SEVERITY_WARNING for f in findings)
    if has_warning:
        # 基准不可确认：保留原单位进入受限数据集，不做换算
        return StandardizedRow(
            record_id=record.record_id,
            registry_version=registry.registry_version,
            metric_code=record.metric_code,
            basis_code=record.basis_code,
            measurement_kind=record.measurement_kind,
            comparability_key=_comparability_key(
                record.metric_code, record.basis_code,
                record.measurement_kind, group),
            raw_value=record.raw_value, raw_unit=record.unit_code,
            standard_value=None, standard_unit=None, conversion_id=None,
            conditions=dict(record.conditions), condition_fingerprint=fingerprint,
            disposition="retained_original", findings=findings, lineage=lineage)

    # ---- 第三阶段：执行版本化换算 ----
    try:
        standard_value = registry.convert(unit, record.raw_value,
                                          record.conditions)
    except KeyError as exc:
        findings.append(config.finding(
            "C001",
            f"单位 {unit.unit_code} 的模板换算缺少条件 {exc}，"
            "无法精确换算，保留原单位",
            {"missing_condition": str(exc).strip("'")}))
        return StandardizedRow(
            record_id=record.record_id,
            registry_version=registry.registry_version,
            metric_code=record.metric_code,
            basis_code=record.basis_code,
            measurement_kind=record.measurement_kind,
            comparability_key=_comparability_key(
                record.metric_code, record.basis_code,
                record.measurement_kind, group),
            raw_value=record.raw_value, raw_unit=record.unit_code,
            standard_value=None, standard_unit=None, conversion_id=None,
            conditions=dict(record.conditions), condition_fingerprint=fingerprint,
            disposition="retained_original", findings=findings, lineage=lineage)

    lineage.update({
        "conversion_id": unit.conversion_id,
        "standard_version": unit.standard_version,
        "formula": ("scale*raw+offset" if unit.template is None
                    else f"template:{unit.template}"),
        "scale": unit.scale, "offset": unit.offset,
        "standard_unit": unit.standard_unit,
    })

    low, high = metric.bounds
    if ((low is not None and standard_value < low)
            or (high is not None and standard_value > high)):
        findings.append(config.finding(
            "N001",
            f"标准化值 {standard_value} 超出指标合理区间 [{low}, {high}]",
            {"standard_value": standard_value, "bounds": [low, high]}))

    return StandardizedRow(
        record_id=record.record_id,
        registry_version=registry.registry_version,
        metric_code=record.metric_code,
        basis_code=record.basis_code,
        measurement_kind=record.measurement_kind,
        comparability_key=_comparability_key(
            record.metric_code, record.basis_code,
            record.measurement_kind, group),
        raw_value=record.raw_value, raw_unit=record.unit_code,
        standard_value=standard_value, standard_unit=unit.standard_unit,
        conversion_id=unit.conversion_id,
        conditions=dict(record.conditions), condition_fingerprint=fingerprint,
        disposition="converted", findings=findings, lineage=lineage)
