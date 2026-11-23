import csv
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
EXPECTED={'measurement_sources.csv': (['source_id', 'lab_id', 'instrument_model', 'metric_code', 'source_unit', 'basis_code', 'schema_version'], 40), 'unit_dictionary.csv': (['unit_code', 'dimension', 'scale', 'offset', 'standard_version', 'valid_from'], 48), 'observations.csv': (['observation_id', 'source_id', 'sample_id', 'metric_code', 'value', 'unit_code', 'basis_code', 'observed_at'], 150), 'basis_rules.csv': (['metric_code', 'basis_code', 'required_fields', 'comparability_group', 'approval_state', 'rule_version'], 48)}

class MaterialTests(unittest.TestCase):
    def test_csv_contract(self):
        for filename,(columns,count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT/"data"/filename).open(encoding="utf-8-sig",newline="") as stream:
                    reader=csv.DictReader(stream); rows=list(reader)
                self.assertEqual(reader.fieldnames,columns)
                self.assertEqual(len(rows),count)
                self.assertTrue(all(None not in row for row in rows))

if __name__=="__main__": unittest.main()
