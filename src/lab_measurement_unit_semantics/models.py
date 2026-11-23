"""领域模型与枚举。

本模块只定义数据结构，不包含任何业务逻辑，便于装载、校验与发布各层复用。
所有时间均使用 UTC ISO-8601 字符串。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class MeasurementKind(str, Enum):
    """测量量性质。

    即使量纲相同（例如温差与绝对温度同为 kelvin 量纲），测量性质不同也禁止合并。
    """

    ABSOLUTE = "absolute"   # 绝对量：可做仿射（含偏移）换算，如摄氏温度
    DIFFERENCE = "difference"  # 差值量：只允许线性换算，偏移必须为 0，如温差
    SETPOINT = "setpoint"   # 设定值：设备控制目标，不是被测物理量，禁止与测量值合并


class Disposition(str, Enum):
    """标准化/发布处置状态。"""

    CONVERTED = "converted"          # 已按注册表换算到标准单位
    RETAINED = "retained_original"   # 保留原单位（受限数据集，不补造换算值）
    EXCLUDED = "excluded"            # 排除，不进入共享数据集


# 校验发现的严重级别
SEVERITY_ERROR = "error"        # 阻断进入共享数据集
SEVERITY_WARNING = "warning"    # 可进入受限数据集
SEVERITY_INFO = "info"          # 仅提示


@dataclass(frozen=True)
class RawRecord:
    """原始测量记录（原始层，不可变）。

    每条记录都必须能追溯到原始数值、单位、样品、设备、方案与环境条件。
    """

    record_id: str
    source_id: str
    lab_id: str
    sample_id: str
    instrument_id: str
    instrument_model: str
    protocol_id: str
    metric_code: str
    source_column: str
    raw_value: float
    unit_code: str
    measurement_kind: str
    basis_code: str
    conditions: dict[str, Any]
    observed_at: str
    source_file: str
    source_row: int


@dataclass(frozen=True)
class ValidationFinding:
    """单条语义校验发现。"""

    code: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StandardizedRow:
    """标准化层的一行（某个注册表版次下的评估结果）。"""

    record_id: str
    registry_version: str
    metric_code: str
    basis_code: str
    measurement_kind: str
    comparability_key: str          # 语义合并键，见 dataset_engine
    raw_value: float
    raw_unit: str
    standard_value: float | None    # None 表示无法/不允许换算
    standard_unit: str | None
    conversion_id: str | None       # 使用的单位换算条目
    conditions: dict[str, Any]
    condition_fingerprint: str
    disposition: str
    findings: list[ValidationFinding]
    lineage: dict[str, Any]


def to_plain(obj: Any) -> Any:
    """把 dataclass/枚举递归转成可 JSON 序列化的普通结构。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj
