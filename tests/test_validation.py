import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_measurement_unit_semantics.loader import load_all
from lab_measurement_unit_semantics.registry import Registry
from lab_measurement_unit_semantics.validation import (
    ValidationConfig, evaluate)

CONFIG = ROOT / "config"
RECORDS, _ = load_all(CONFIG / "sources" / "source_config.json")
BY_ID = {r.record_id: r for r in RECORDS}


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


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.reg = Registry.load(CONFIG / "registry_v1.json")
        self.cfg = ValidationConfig.load(CONFIG / "validation_rules.json")
        self.rows = {r.record_id: r
                     for r in evaluate(RECORDS, self.reg, self.cfg)}

    def codes(self, record_id):
        return {f.code for f in self.rows[record_id].findings}

    def test_celsius_absolute_converted_to_kelvin(self):
        row = self.rows[rid("lab_a_thermo.csv", 1, "temperature")]
        self.assertEqual(row.disposition, "converted")
        self.assertAlmostEqual(row.standard_value, 22.5 + 273.15)
        self.assertEqual(row.standard_unit, "K")
        self.assertEqual(row.conversion_id, "CV-degC-v1")

    def test_temperature_delta_never_gets_273_offset(self):
        row = self.rows[rid("lab_b_sensor.csv", 1, "temperature")]
        self.assertEqual(row.disposition, "converted")
        self.assertAlmostEqual(row.standard_value, 1.5)
        self.assertEqual(row.standard_unit, "K_delta")

    def test_setpoint_uses_own_comparability_key(self):
        row = self.rows[rid("lab_c_setpoint.csv", 1, "temperature")]
        self.assertEqual(row.disposition, "converted")
        self.assertEqual(row.measurement_kind, "setpoint")
        self.assertIn("TEMP_SETPOINT", row.comparability_key)
        # 与被测温度的语义键不同 -> 不可能被误合并
        meas = self.rows[rid("lab_a_thermo.csv", 1, "temperature")]
        self.assertNotEqual(row.comparability_key, meas.comparability_key)

    def test_missing_basis_condition_retains_original_no_fabrication(self):
        # A03 缺 pressure_kPa：warning，保留原单位，standard_value 必须为 None
        row = self.rows[rid("lab_a_thermo.csv", 3, "temperature")]
        self.assertEqual(row.disposition, "retained_original")
        self.assertIn("B002", self.codes(row.record_id))
        self.assertIsNone(row.standard_value)
        self.assertIsNone(row.standard_unit)
        self.assertEqual(row.raw_unit, "degC")

    def test_gas_conversion_uses_molar_volume_v1(self):
        row = self.rows[rid("lab_b_sensor.csv", 1, "gas_conc")]
        self.assertEqual(row.disposition, "converted")
        self.assertAlmostEqual(row.standard_value, 820.0 * 22.414 / 1000.0)

    def test_gas_missing_reference_pressure_retains_original(self):
        # B03 缺 ref_pressure_kPa：基准必需字段缺失（B002），禁止补造换算
        row = self.rows[rid("lab_b_sensor.csv", 3, "gas_conc")]
        self.assertEqual(row.disposition, "retained_original")
        self.assertIn("B002", self.codes(row.record_id))
        self.assertIsNone(row.standard_value)

    def test_template_missing_parameter_is_c001_defense_in_depth(self):
        # 白盒：基准规则齐备，但模板换算自身所需参数缺失时走 C001，
        # 仍然保留原单位而非抛出异常或补造数值
        import dataclasses
        from lab_measurement_unit_semantics.registry import (
            Registry, UnitDef, MetricDef, BasisRule)
        broken_unit = dataclasses.replace(
            self.reg.unit("mg_per_m3"),
            template_params={})  # 移除摩尔体积常数 -> convert 抛 KeyError
        reg = Registry(
            registry_version="REG-BROKEN",
            published_at="2026-07-01T00:00:00Z",
            units={**self.reg.units, "mg_per_m3": broken_unit},
            metrics=self.reg.metrics,
            basis_rules=self.reg.basis_rules)
        good = BY_ID[rid("lab_b_sensor.csv", 1, "gas_conc")]
        row = evaluate([good], reg, self.cfg)[0]
        self.assertEqual(row.disposition, "retained_original")
        self.assertIn("C001", {f.code for f in row.findings})
        self.assertIsNone(row.standard_value)

    def test_dry_mass_vs_wet_volume_never_share_key(self):
        dry = self.rows[rid("lab_c_massspec.csv", 1, "analyte_ppm")]
        wet = self.rows[rid("lab_b_sensor.csv", 1, "gas_conc")]
        self.assertEqual(dry.disposition, "converted")
        self.assertAlmostEqual(dry.standard_value, 0.125)  # 125 ug/kg -> mg/kg
        self.assertNotEqual(dry.comparability_key, wet.comparability_key)
        self.assertNotEqual(dry.standard_unit, wet.standard_unit)

    def test_missing_drying_humidity_retains_original(self):
        row = self.rows[rid("lab_c_massspec.csv", 3, "analyte_ppm")]
        self.assertEqual(row.disposition, "retained_original")
        self.assertIn("B002", self.codes(row.record_id))

    def test_unknown_unit_excluded(self):
        row = self.rows[rid("legacy_results.jsonl", 1, "reading")]
        self.assertEqual(row.disposition, "excluded")
        self.assertIn("U001", self.codes(row.record_id))

    def test_unknown_basis_retains_original(self):
        # legacy 第二条 reading：单位 vendor_scale 也未登记 -> 仍是 excluded；
        # 这里用 volume_reading（uL 已登记）验证 B001/R001 路径
        row = self.rows[rid("legacy_results.jsonl", 1, "volume_reading")]
        self.assertEqual(row.disposition, "retained_original")
        codes = self.codes(row.record_id)
        # v1 下 unverified-pipette 为 draft -> R001，且 calibration_id 缺失 -> B002
        self.assertIn("R001", codes)
        self.assertIn("B002", codes)
        self.assertIsNone(row.standard_value)

    def test_out_of_bounds_is_info_only(self):
        # 1200 uL = 1.2 mL，在区间内；构造一个超界值走 N001
        from lab_measurement_unit_semantics.models import RawRecord
        import dataclasses
        base = BY_ID[rid("lab_a_volume.csv", 1, "volume")]
        huge = dataclasses.replace(base, raw_value=2.0e7,
                                  record_id=id(base))
        row = evaluate([huge], self.reg, self.cfg)[0]
        self.assertEqual(row.disposition, "converted")
        self.assertIn("N001", {f.code for f in row.findings})

    def test_disposition_counts(self):
        from collections import Counter
        counts = Counter(r.disposition for r in self.rows.values())
        self.assertGreater(counts["converted"], 0)
        self.assertGreater(counts["retained_original"], 0)
        self.assertGreater(counts["excluded"], 0)

    def test_v2_revision_reevaluation_changes_gas_rows_only(self):
        v2 = Registry.load(CONFIG / "registry_v2.json")
        v2_rows = {r.record_id: r
                   for r in evaluate(RECORDS, v2, self.cfg)}
        gas_v1 = self.rows[rid("lab_b_sensor.csv", 1, "gas_conc")]
        gas_v2 = v2_rows[rid("lab_b_sensor.csv", 1, "gas_conc")]
        self.assertNotEqual(gas_v1.standard_value, gas_v2.standard_value)
        self.assertAlmostEqual(gas_v2.standard_value,
                               820.0 * 24.0 / 1000.0)
        self.assertEqual(gas_v2.conversion_id, "CV-mgm3-v2")
        # 摄氏度行不受标准修订影响
        t_v1 = self.rows[rid("lab_a_thermo.csv", 1, "temperature")]
        t_v2 = v2_rows[rid("lab_a_thermo.csv", 1, "temperature")]
        self.assertEqual(t_v1.standard_value, t_v2.standard_value)
        # v2 中 unverified-pipette 已批准，但 calibration_id 仍缺失 => 仍受限
        legacy_v2 = v2_rows[rid("legacy_results.jsonl", 1, "volume_reading")]
        self.assertEqual(legacy_v2.disposition, "retained_original")

    def test_lineage_contains_raw_and_conversion_trace(self):
        row = self.rows[rid("lab_b_sensor.csv", 1, "gas_conc")]
        self.assertEqual(row.lineage["raw_value"], 820.0)
        self.assertEqual(row.lineage["raw_unit"], "mg_per_m3")
        self.assertEqual(row.lineage["registry_version"], "REG-2026-01")
        self.assertEqual(row.lineage["conversion_id"], "CV-mgm3-v1")
        self.assertEqual(row.lineage["standard_version"], "GAS-STD-1982")


if __name__ == "__main__":
    unittest.main()
