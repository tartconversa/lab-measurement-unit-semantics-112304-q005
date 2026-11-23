import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_measurement_unit_semantics.loader import (
    load_all, LoaderConfigError)

CONFIG = ROOT / "config" / "sources" / "source_config.json"


class LoaderTests(unittest.TestCase):
    def test_loads_all_sources_with_explicit_semantics(self):
        records, reports = load_all(CONFIG)
        source_ids = {r.source_id for r in reports}
        self.assertEqual(source_ids, {
            "SRC-LAB-A", "SRC-LAB-A-VOL", "SRC-LAB-B",
            "SRC-LAB-C", "SRC-LAB-C-CTRL", "SRC-LEGACY"})
        # 4+3+8(宽表4行x2列)+4+3+4(2行x2列) = 26；1 个缺失测量值被跳过
        self.assertEqual(sum(r.loaded_records for r in reports), 26)
        self.assertEqual(sum(r.skipped_missing for r in reports), 1)

    def test_records_keep_full_traceability(self):
        records, _ = load_all(CONFIG)
        first = next(r for r in records
                     if r.source_id == "SRC-LAB-A" and r.source_row == 1)
        self.assertEqual(first.metric_code, "TEMP_MEAS")
        self.assertEqual(first.unit_code, "degC")
        self.assertEqual(first.raw_value, 22.5)
        self.assertEqual(first.instrument_id, "INST-A-THERMO-7")
        self.assertEqual(first.protocol_id, "PROTO-A-v3")
        self.assertEqual(first.conditions, {"pressure_kPa": 101.3})
        # record_id 可定位到文件、行、原始列
        self.assertTrue(first.record_id.endswith("r1:temperature"))

    def test_same_column_name_different_semantics_kept_distinct(self):
        records, _ = load_all(CONFIG)
        temp_columns = [r for r in records if r.source_column == "temperature"]
        kinds = {r.measurement_kind for r in temp_columns}
        # 同名为 temperature 的列分别是绝对温度、温差、设定值
        self.assertEqual(kinds, {"absolute", "difference", "setpoint"})
        metrics = {r.metric_code for r in temp_columns}
        self.assertEqual(metrics,
                         {"TEMP_MEAS", "TEMP_DELTA", "TEMP_SETPOINT"})

    def test_jsonl_legacy_loaded(self):
        records, _ = load_all(CONFIG)
        legacy = [r for r in records if r.source_id == "SRC-LEGACY"]
        self.assertEqual(len(legacy), 4)
        self.assertTrue(all(r.basis_code in ("unknown", "unverified-pipette")
                            for r in legacy))

    def test_path_traversal_rejected(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"sources": [{
                "source_id": "S1", "lab_id": "L", "instrument_id": "I",
                "instrument_model": "m", "protocol_id": "p",
                "file": "../../etc/passwd",
                "measurements": [{"column": "x", "metric_code": "TEMP_MEAS",
                                  "unit_code": "degC",
                                  "measurement_kind": "absolute",
                                  "basis_code": "ambient-air"}]}]}
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaises(LoaderConfigError):
                load_all(path)


if __name__ == "__main__":
    unittest.main()
