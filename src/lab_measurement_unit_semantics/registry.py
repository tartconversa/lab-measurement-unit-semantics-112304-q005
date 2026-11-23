"""单位与指标注册表。

注册表是领域数据管理员（Domain Steward）确认的配置，回答三个问题：

1. 一个单位代码表示什么量纲、什么测量性质（绝对量/差值/设定值）？
2. 到标准单位的换算公式是什么（线性/仿射/带条件模板）？
3. 一个指标（metric）允许哪些基准条件，基准条件需要哪些环境字段？

注册表本身带版次（registry_version）。标准修订后发布新版次：
旧版次保留可查，标准化表按新版次重新评估并生成新版本。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RegistryError(ValueError):
    """注册表配置错误（加载期 fail-fast）。"""


@dataclass(frozen=True)
class UnitDef:
    unit_code: str
    name: str
    dimension: str                  # 规范量纲，如 temperature / mass-concentration
    standard_unit: str              # 该量纲的标准单位代码
    measurement_kind: str           # absolute / difference / setpoint
    # 换算公式：standard_value = scale * raw_value + offset
    # difference 量纲要求 offset == 0.0
    scale: float
    offset: float
    conversion_id: str              # 换算条目版次标识，写入血缘
    standard_version: str           # 引用的计量标准号
    valid_from: str
    # 模板换算（需要基准条件参与），None 表示纯仿射换算
    template: str | None = None
    template_params: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class BasisRule:
    """指标 × 基准条件规则。"""

    metric_code: str
    basis_code: str
    required_fields: tuple[str, ...]
    comparability_group: str
    approval_state: str             # approved / draft / retired
    rule_version: str


@dataclass(frozen=True)
class MetricDef:
    metric_code: str
    name: str
    dimension: str
    standard_unit: str
    allowed_kinds: tuple[str, ...]
    bounds: tuple[float | None, float | None]  # 标准单位下的合理区间，None 表示不限


@dataclass(frozen=True)
class Registry:
    registry_version: str
    published_at: str
    units: dict[str, UnitDef]
    metrics: dict[str, MetricDef]
    basis_rules: dict[tuple[str, str], BasisRule]

    # ---------- 查询 ----------
    def unit(self, unit_code: str) -> UnitDef:
        if unit_code not in self.units:
            raise RegistryError(f"单位代码未在注册表登记: {unit_code}")
        return self.units[unit_code]

    def metric(self, metric_code: str) -> MetricDef:
        if metric_code not in self.metrics:
            raise RegistryError(f"指标代码未在注册表登记: {metric_code}")
        return self.metrics[metric_code]

    def basis_rule(self, metric_code: str, basis_code: str) -> BasisRule | None:
        return self.basis_rules.get((metric_code, basis_code))

    def convert(self, unit: UnitDef, raw_value: float,
                conditions: dict[str, Any]) -> float:
        """按单位定义执行换算。模板换算所需条件缺失时抛出 KeyError，由调用方降级。"""
        if unit.template is None:
            return unit.scale * raw_value + unit.offset
        if unit.template == "gas_molar_volume":
            # 气体质量浓度 mg/m3 -> mg/L 按摩尔体积折算：
            # c_mg/L = c_mg/m3 * Vm / 1000，Vm 取自标准规定的摩尔体积（L/mol 常数）
            vm = unit.template_params["molar_volume_L_per_mol"]
            return raw_value * vm / 1000.0
        raise RegistryError(f"未知换算模板: {unit.template}")

    # ---------- 加载与自检 ----------
    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        version = data["registry_version"]
        units: dict[str, UnitDef] = {}
        for item in data["units"]:
            unit = UnitDef(**item)
            if unit.unit_code in units:
                raise RegistryError(f"单位代码重复: {unit.unit_code}")
            if unit.measurement_kind == "difference" and unit.offset != 0.0:
                raise RegistryError(
                    f"差值量单位 {unit.unit_code} 不允许带偏移量（offset 必须为 0）")
            units[unit.unit_code] = unit
        metrics: dict[str, MetricDef] = {}
        for item in data["metrics"]:
            metric = MetricDef(
                metric_code=item["metric_code"],
                name=item["name"],
                dimension=item["dimension"],
                standard_unit=item["standard_unit"],
                allowed_kinds=tuple(item["allowed_kinds"]),
                bounds=(item.get("bounds", [None, None])[0],
                        item.get("bounds", [None, None])[1]),
            )
            if metric.metric_code in metrics:
                raise RegistryError(f"指标代码重复: {metric.metric_code}")
            if metric.standard_unit not in units:
                raise RegistryError(
                    f"指标 {metric.metric_code} 的标准单位 "
                    f"{metric.standard_unit} 未登记")
            std = units[metric.standard_unit]
            if std.dimension != metric.dimension:
                raise RegistryError(
                    f"指标 {metric.metric_code} 量纲 {metric.dimension} "
                    f"与其标准单位量纲 {std.dimension} 不一致")
            metrics[metric.metric_code] = metric
        rules: dict[tuple[str, str], BasisRule] = {}
        for item in data["basis_rules"]:
            rule = BasisRule(
                metric_code=item["metric_code"],
                basis_code=item["basis_code"],
                required_fields=tuple(item["required_fields"]),
                comparability_group=item["comparability_group"],
                approval_state=item["approval_state"],
                rule_version=item["rule_version"],
            )
            key = (rule.metric_code, rule.basis_code)
            if key in rules:
                raise RegistryError(f"基准规则重复: {key}")
            rules[key] = rule
        reg = cls(registry_version=version,
                  published_at=data.get("published_at", ""),
                  units=units, metrics=metrics, basis_rules=rules)
        reg._self_check()
        return reg

    def _self_check(self) -> None:
        """加载期一致性检查：单位量纲与标准单位、指标量纲互相对齐。"""
        for unit in self.units.values():
            if unit.standard_unit not in self.units:
                raise RegistryError(
                    f"单位 {unit.unit_code} 指向的标准单位 "
                    f"{unit.standard_unit} 未登记")
            target = self.units[unit.standard_unit]
            if target.dimension != unit.dimension:
                raise RegistryError(
                    f"单位 {unit.unit_code} 量纲 {unit.dimension} 与标准单位 "
                    f"{target.dimension} 不一致")
            if target.measurement_kind != unit.measurement_kind:
                raise RegistryError(
                    f"单位 {unit.unit_code} 测量性质 {unit.measurement_kind} "
                    f"与标准单位 {target.measurement_kind} 不一致")
