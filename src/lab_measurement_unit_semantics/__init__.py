"""异构科研测量语义化数据湖。

模块划分：
- models: 领域模型与处置枚举
- registry: 单位/指标/基准条件注册表（版次化配置）
- loader: 异构设备文件可配置装载（原始层）
- validation: 语义校验与标准化引擎
- repository: SQLite 分层存储（原始层/标准化层/共享数据集）
- dataset_engine: 合并策略与版本化数据集发布
- cli: 命令行入口
"""
from .models import (RawRecord, StandardizedRow, ValidationFinding,
                     MeasurementKind, Disposition)
from .registry import Registry, UnitDef, MetricDef, BasisRule
from .loader import load_all, load_source, LoadReport
from .validation import ValidationConfig, evaluate
from .repository import Repository
from .dataset_engine import DatasetEngine

__all__ = [
    "RawRecord", "StandardizedRow", "ValidationFinding",
    "MeasurementKind", "Disposition",
    "Registry", "UnitDef", "MetricDef", "BasisRule",
    "load_all", "load_source", "LoadReport",
    "ValidationConfig", "evaluate",
    "Repository", "DatasetEngine",
]
