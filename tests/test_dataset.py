import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_measurement_unit_semantics.dataset_engine import (
    DatasetEngine, PolicyError)
from lab_measurement_unit_semantics.loader import load_all
from lab_measurement_unit_semantics.repository import Repository
from lab_measurement_unit_semantics.registry import Registry
from lab_measurement_unit_semantics.validation import (
    ValidationConfig, evaluate)

CONFIG = ROOT / "config"


def rid(source_file, row, column):
    prefix = {
        "lab_a_thermo.csv": "SRC-LAB-A",
        "lab_a_volume.csv": "SRC-LAB-A-VOL",
        "lab_b_sensor.csv": "SRC-LAB-B",
        "lab_c_massspec.csv": "SRC-LAB-C",
        "lab_c_setpoint.csv": "SRC-LAB-C-CTRL",
        "legacy_results.jsonl": "SRC-LEGACY",
    }[source_file]
    return f"{prefix}:{source_file}:r{row}:{column}"


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "lake.db"
        self.repo = Repository(self.db)
        records, _ = load_all(CONFIG / "sources" / "source_config.json")
        self.records = records
        self.repo.insert_raw(records, "2026-07-10T00:00:00Z")
        self.reg = Registry.load(CONFIG / "registry_v1.json")
        cfg = ValidationConfig.load(CONFIG / "validation_rules.json")
        self.rows = evaluate(records, self.reg, cfg)
        self.repo.replace_standardized("REG-2026-01", self.rows)
        self.engine = DatasetEngine(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def register(self):
        return self.engine.register_policy(
            CONFIG / "policies" / "cross_lab_analyte.json",
            "2026-07-11T00:00:00Z")

    def test_policy_registration_detects_same_named_column_collision(self):
        result = self.register()
        collisions = result["column_name_collisions"]
        # temperature 同名列分别承载 TEMP_MEAS / TEMP_DELTA / TEMP_SETPOINT，
        # 策略只允许前两个语义键；冲突检测仍应报告不同语义键
        hit = next(c for c in collisions if c["source_column"] == "temperature")
        self.assertTrue(
            any("TEMP_MEAS" in k for k in hit["semantic_keys"]))
        self.assertTrue(
            any("TEMP_DELTA" in k for k in hit["semantic_keys"]))
        # 设定值键不在策略内，因此不在检测结果中
        self.assertFalse(
            any("TEMP_SETPOINT" in k for k in hit["semantic_keys"]))

    def test_shared_publish_contains_only_converted_rows(self):
        self.register()
        manifest = self.engine.publish(
            "cross-lab-analyte", "POLICY-CROSS-LAB-ANALYTE",
            "REG-2026-01", "shared", "2026-07-12T00:00:00Z")
        self.assertEqual(manifest["dataset_version"], 1)
        out = self.repo.list_dataset_rows(
            "cross-lab-analyte", manifest["dataset_version"])
        self.assertTrue(out)
        self.assertTrue(all(r["disposition"] == "converted" for r in out))
        # 设定值、未知基准行不在共享集
        keys = {r["comparability_key"] for r in out}
        self.assertFalse(any("TEMP_SETPOINT" in k for k in keys))
        # 跨实验室同语义键确实被合流（LAB-01 与 LAB-02 等多实验室）
        labs = {r["lab_id"] for r in out if "TEMP" in r["comparability_key"]}
        self.assertIn("LAB-01", labs)

    def test_restricted_publish_keeps_original_units_with_reasons(self):
        self.register()
        manifest = self.engine.publish(
            "cross-lab-analyte", "POLICY-CROSS-LAB-ANALYTE",
            "REG-2026-01", "restricted", "2026-07-12T00:00:00Z")
        out = self.repo.list_dataset_rows(
            "cross-lab-analyte", manifest["dataset_version"])
        retained = [r for r in out
                    if r["disposition"] == "retained_original"]
        self.assertTrue(retained)
        # 受限行保留原单位且没有标准值
        for r in retained:
            self.assertIsNone(r["standard_value"])
            self.assertTrue(r["raw_unit"])
        # 清单逐行给出受限原因
        reasons = {x["record_id"]
                   for x in manifest["retained_original_records"]}
        self.assertEqual({r["record_id"] for r in retained}, reasons)
        # 排除行只在审计清单留痕，不进数据集。
        # 默认策略不含未知基准键，故该记录先进入“不在策略内”审计
        self.assertFalse(
            {x["record_id"] for x in manifest["not_in_policy_audit"]}
            & {r["record_id"] for r in out})
        self.assertTrue(manifest["not_in_policy_audit"])

    def test_excluded_rows_audited_when_key_authorized(self):
        # 若分析人员显式把未知基准键纳入策略，语义错误行（U001）仍被排除并审计
        broad = {
            "policy_id": "POLICY-BROAD-LEGACY-REVIEW",
            "description": "旧结果复核策略（含未知基准，仅用于审计演示）",
            "registry_version": "REG-2026-01",
            "created_by": "research-analyst-li",
            "allow_comparability_keys": [
                "ANALYTE_DRY|dry-mass|absolute|CG-DRY-MASS",
                "ANALYTE_DRY|unknown|absolute|unknown",
            ],
        }
        policy_path = Path(self.tmp.name) / "broad.json"
        policy_path.write_text(json.dumps(broad, ensure_ascii=False),
                               encoding="utf-8")
        self.engine.register_policy(policy_path, "2026-07-11T00:00:00Z")
        manifest = self.engine.publish(
            "legacy-review", "POLICY-BROAD-LEGACY-REVIEW",
            "REG-2026-01", "restricted", "2026-07-12T00:00:00Z")
        excluded_ids = {x["record_id"]
                        for x in manifest["excluded_records_audit"]}
        self.assertTrue(excluded_ids)
        out_ids = {r["record_id"]
                   for r in self.repo.list_dataset_rows(
                       "legacy-review", manifest["dataset_version"])}
        self.assertFalse(excluded_ids & out_ids)
        # 审计条目保留错误规则号，可追溯为什么被排除
        audit = manifest["excluded_records_audit"][0]
        self.assertIn("U001", {f["code"] for f in audit["findings"]})

    def test_versioned_republish_after_standard_revision(self):
        self.register()
        v1_manifest = self.engine.publish(
            "cross-lab-analyte", "POLICY-CROSS-LAB-ANALYTE",
            "REG-2026-01", "shared", "2026-07-12T00:00:00Z")

        reg_v2 = Registry.load(CONFIG / "registry_v2.json")
        cfg = ValidationConfig.load(CONFIG / "validation_rules.json")
        rows_v2 = evaluate(self.records, reg_v2, cfg)
        self.repo.replace_standardized("REG-2026-02", rows_v2)
        v2_manifest = self.engine.publish(
            "cross-lab-analyte", "POLICY-CROSS-LAB-ANALYTE",
            "REG-2026-02", "shared", "2026-07-20T00:00:00Z")

        self.assertEqual(v1_manifest["dataset_version"], 1)
        self.assertEqual(v2_manifest["dataset_version"], 2)
        versions = self.repo.list_dataset_versions("cross-lab-analyte")
        self.assertEqual([v["dataset_version"] for v in versions], [1, 2])

        # v1 行仍然可查，且气体值保持旧因子结果
        old_rows = self.repo.list_dataset_rows(
            "cross-lab-analyte", 1)
        gas_old = next(r for r in old_rows if r["record_id"] ==
                       rid("lab_b_sensor.csv", 1, "gas_conc"))
        new_rows = self.repo.list_dataset_rows(
            "cross-lab-analyte", 2)
        gas_new = next(r for r in new_rows if r["record_id"] ==
                       rid("lab_b_sensor.csv", 1, "gas_conc"))
        self.assertAlmostEqual(gas_old["standard_value"],
                               820.0 * 22.414 / 1000.0)
        self.assertAlmostEqual(gas_new["standard_value"],
                               820.0 * 24.0 / 1000.0)
        self.assertNotEqual(gas_old["standard_value"],
                            gas_new["standard_value"])

    def test_full_lineage_roundtrip(self):
        self.register()
        self.engine.publish(
            "cross-lab-analyte", "POLICY-CROSS-LAB-ANALYTE",
            "REG-2026-01", "shared",
            "2026-07-12T00:00:00Z")
        record_id = rid("lab_a_thermo.csv", 1, "temperature")
        lineage = self.repo.full_lineage(record_id)
        self.assertEqual(lineage["raw"]["raw_value"], 22.5)
        self.assertEqual(lineage["raw"]["unit_code"], "degC")
        self.assertEqual(len(lineage["standardized"]), 1)
        self.assertEqual(lineage["standardized"][0]["disposition"],
                         "converted")
        self.assertEqual(len(lineage["datasets"]), 1)
        self.assertEqual(lineage["datasets"][0]["disposition"], "converted")

    def test_unknown_policy_rejected(self):
        with self.assertRaises(PolicyError):
            self.engine.publish(
                "ds", "DOES-NOT-EXIST", "REG-2026-01", "shared",
                "2026-07-12T00:00:00Z")

    def test_raw_layer_is_immutable_on_reingest(self):
        before = self.repo.count_raw()
        inserted = self.repo.insert_raw(self.records, "2026-07-13T00:00:00Z")
        self.assertEqual(inserted, 0)
        self.assertEqual(self.repo.count_raw(), before)
        raw = self.repo.get_raw(rid("lab_a_thermo.csv", 1, "temperature"))
        # 原始值与原始单位不被后续处理改写
        self.assertEqual(raw["raw_value"], 22.5)
        self.assertEqual(raw["unit_code"], "degC")


if __name__ == "__main__":
    unittest.main()
