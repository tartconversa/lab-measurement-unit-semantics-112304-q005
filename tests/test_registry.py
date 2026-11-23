import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_measurement_unit_semantics.registry import Registry, RegistryError

CONFIG = ROOT / "config"


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.reg = Registry.load(CONFIG / "registry_v1.json")

    def test_registry_version_loaded(self):
        self.assertEqual(self.reg.registry_version, "REG-2026-01")
        self.assertIn("degC", self.reg.units)
        self.assertIn("ANALYTE_DRY", self.reg.metrics)

    def test_celsius_absolute_uses_affine_conversion(self):
        unit = self.reg.unit("degC")
        self.assertEqual(unit.measurement_kind, "absolute")
        # 25 °C -> 298.15 K（含偏移的仿射换算）
        self.assertAlmostEqual(self.reg.convert(unit, 25.0, {}), 298.15)

    def test_delta_units_have_no_offset(self):
        unit = self.reg.unit("degC_delta")
        self.assertEqual(unit.measurement_kind, "difference")
        self.assertEqual(unit.offset, 0.0)
        # 温差 1.5 °C -> 1.5 K，绝不加 273.15
        self.assertAlmostEqual(self.reg.convert(unit, 1.5, {}), 1.5)

    def test_setpoint_is_separate_kind(self):
        unit = self.reg.unit("degC_setpoint")
        self.assertEqual(unit.measurement_kind, "setpoint")
        self.assertNotEqual(unit.measurement_kind, "absolute")

    def test_gas_template_conversion_needs_basis(self):
        unit = self.reg.unit("mg_per_m3")
        # v1: Vm=22.414 L/mol，820 mg/m3 -> 18.37948 mg/L
        self.assertAlmostEqual(
            self.reg.convert(unit, 820.0, {"gas_ref_temp_C": 20.0}),
            820.0 * 22.414 / 1000.0)

    def test_dimension_mismatch_config_rejected(self):
        bad = {
            "registry_version": "BAD",
            "units": [
                {"unit_code": "X", "name": "x", "dimension": "volume",
                 "standard_unit": "K", "measurement_kind": "absolute",
                 "scale": 1.0, "offset": 0.0, "conversion_id": "c",
                 "standard_version": "s", "valid_from": "2026-01-01"},
                {"unit_code": "K", "name": "k", "dimension": "temperature",
                 "standard_unit": "K", "measurement_kind": "absolute",
                 "scale": 1.0, "offset": 0.0, "conversion_id": "c",
                 "standard_version": "s", "valid_from": "2026-01-01"},
            ],
            "metrics": [], "basis_rules": [],
        }
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(bad, fh)
            path = fh.name
        with self.assertRaises(RegistryError):
            Registry.load(path)

    def test_v2_revision_changes_gas_factor_only(self):
        v2 = Registry.load(CONFIG / "registry_v2.json")
        self.assertEqual(v2.registry_version, "REG-2026-02")
        old = self.reg.unit("mg_per_m3")
        new = v2.unit("mg_per_m3")
        self.assertNotEqual(old.conversion_id, new.conversion_id)
        self.assertNotEqual(old.scale, new.scale)
        # 同样 820 mg/m3，标准修订后结果不同
        self.assertNotEqual(
            self.reg.convert(old, 820.0, {}),
            v2.convert(new, 820.0, {}))
        # 与气体无关的摄氏度换算保持不变
        self.assertEqual(
            self.reg.convert(self.reg.unit("degC"), 25.0, {}),
            v2.convert(v2.unit("degC"), 25.0, {}))


if __name__ == "__main__":
    unittest.main()
