"""Contract checks using only generated SIM records; no original patient rows."""
import csv
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("generate_mock", ROOT / "scripts/data/generate_mock.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.generated = ROOT / "data/generated"
        self.generated.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="_test_d2_", dir=self.generated))

    def tearDown(self):
        shutil.rmtree(self.work)

    def generate(self, name="dataset", **kwargs):
        output = self.work / name
        generator.generate(output_dir=output, **kwargs)
        return output

    def read(self, path):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            return reader.fieldnames, list(reader)

    def test_reproducible_bytes_and_changed_seed(self):
        a, b = self.generate("a"), self.generate("b")
        self.assertEqual(sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file()),
                         sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file()))
        for path in a.rglob("*"):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (b / path.relative_to(a)).read_bytes())
        c = self.generate("c", seed=43)
        self.assertNotEqual((a / "mock_his/his_charge.csv").read_bytes(),
                            (c / "mock_his/his_charge.csv").read_bytes())
        with ZipFile(a / "files/finance_excel.xlsx") as archive:
            self.assertTrue(all(item.date_time == (2000, 1, 1, 0, 0, 0) for item in archive.infolist()))
            self.assertIn(b"2000-01-01T00:00:00Z", archive.read("docProps/core.xml"))

    def test_ledger_coverage_sim_relationships_money_and_excel(self):
        from openpyxl import load_workbook
        output = self.generate()
        manifest = json.loads((output / "manifest.json").read_text())
        ledger = [json.loads(line) for line in (output / "dirty_cases.jsonl").read_text().splitlines()]
        self.assertTrue(manifest["synthetic"])
        self.assertEqual(manifest["date_range"], {"start_date": "2025-01-01", "end_date": "2025-12-31"})
        months = {f"2025-{i:02}" for i in range(1, 13)}
        dates = {"his_outpatient": "visit_date", "his_inpatient": "admit_date", "his_charge": "fee_date",
                 "emr_record": "visit_date", "lis_report": "report_date", "finance_excel": "stat_date"}
        clean = {}
        aliases = {row["alias"]: row["dept_code"] for row in self.read(output / "dictionaries/department_aliases.csv")[1]}
        for entry in manifest["files"]:
            path = output / entry["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
            if entry.get("source") not in dates or path.suffix != ".csv":
                continue
            source = entry["source"]
            header, rows = self.read(path)
            self.assertEqual(header, entry["header"])
            self.assertEqual(len(rows), entry["final_rows"])
            self.assertGreaterEqual(entry["baseline_rows"], 100)
            self.assertEqual({row[dates[source]][:7] for row in rows}, months)
            self.assertEqual(set(entry["coverage_months"]), months)
            cases = [case for case in ledger if case["source"] == source]
            counts = Counter(case["kind"] for case in cases)
            expected = entry["baseline_rows"] // 100
            self.assertEqual(counts["null"], expected)
            self.assertEqual(counts["duplicate"], expected)
            self.assertEqual(counts["department_alias"], expected if "dept_code" in header else 0)
            self.assertEqual(dict(counts), manifest["dirty_counts"][source])
            touched = [case["duplicate_of_row"] if case["kind"] == "duplicate" else case["row_number"] for case in cases]
            self.assertEqual(len(touched), len(set(touched)))
            for case in reversed(cases):
                index = case["row_number"] - 1
                if case["kind"] == "duplicate":
                    self.assertEqual(rows[index], rows[case["duplicate_of_row"] - 1])
                    rows.pop(index)
                else:
                    self.assertEqual(rows[index][case["field"]], case["injected_value"])
                    if case["kind"] == "department_alias":
                        self.assertEqual(aliases[case["injected_value"]], case["original_value"])
                    rows[index][case["field"]] = case["original_value"]
            self.assertEqual(len(rows), entry["baseline_rows"])
            for row in rows:
                for field, value in row.items():
                    if field.endswith("_id") or field in ("patient_name", "id_card", "phone", "disease_code"):
                        self.assertTrue(value.startswith("SIM_"))
                    if field in ("fee_amount", "total_fee", "drug_fee", "consumable_fee", "amount", "revenue", "drug_revenue", "consumable_revenue"):
                        self.assertRegex(value, r"^\d+\.\d{2}$")
            clean[source] = rows
        self.assertEqual(set(clean), set(dates))
        visits = {}
        for source in ("his_outpatient", "his_inpatient"):
            for row in clean[source]:
                visits[row["visit_id"]] = row
                for key in ("visit_id", "patient_id", "patient_name", "id_card", "phone"):
                    self.assertTrue(row[key].startswith("SIM_"))
                if source == "his_inpatient":
                    self.assertEqual((date.fromisoformat(row["discharge_date"]) - date.fromisoformat(row["admit_date"])).days, int(row["stay_days"]))
        totals = defaultdict(lambda: Decimal("0.00"))
        finance = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
        components = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
        for row in clean["his_charge"]:
            visit = visits[row["visit_id"]]
            self.assertEqual(row["patient_id"], visit["patient_id"])
            self.assertEqual(row["dept_code"], visit["dept_code"])
            self.assertEqual(row["fee_date"], visit.get("visit_date", visit.get("admit_date")))
            self.assertEqual(row["visit_type"], visit.get("visit_type", "INPATIENT"))
            self.assertRegex(row["amount"], r"^\d+\.\d{2}$")
            amount = Decimal(row["amount"])
            totals[row["visit_id"]] += amount
            components[row["visit_id"]][row["fee_type_code"]] += amount
            key = (row["fee_date"], row["dept_code"])
            finance[key]["revenue"] += amount
            if row["fee_type_code"] in ("DRUG", "CONSUMABLE"):
                finance[key][row["fee_type_code"].lower() + "_revenue"] += amount
        for visit_id, row in visits.items():
            self.assertEqual(totals[visit_id], Decimal(row.get("fee_amount", row.get("total_fee"))))
            if "drug_fee" in row:
                self.assertEqual(Decimal(row["drug_fee"]), components[visit_id]["DRUG"])
                self.assertEqual(Decimal(row["consumable_fee"]), components[visit_id]["CONSUMABLE"])
        self.assertEqual(len(finance), len(clean["finance_excel"]))
        for row in clean["finance_excel"]:
            for field in ("revenue", "drug_revenue", "consumable_revenue"):
                self.assertEqual(Decimal(row[field]), finance[(row["stat_date"], row["dept_code"])][field])
        for source in ("emr_record", "lis_report"):
            self.assertEqual({row["visit_id"] for row in clean[source]}, set(visits))
            for row in clean[source]:
                visit = visits[row["visit_id"]]
                self.assertEqual(row["patient_id"], visit["patient_id"])
                self.assertEqual(row[dates[source]], visit.get("visit_date", visit.get("admit_date")))
                if source == "emr_record":
                    self.assertEqual(row["dept_code"], visit["dept_code"])
                    self.assertTrue(row["disease_code"].startswith("SIM_DX_"))
        header, csv_rows = self.read(output / "files/finance_excel.csv")
        workbook = load_workbook(output / "files/finance_excel.xlsx", read_only=True)
        try:
            excel_rows = list(workbook.active.values)
            self.assertEqual(list(excel_rows[0]), header)
            self.assertEqual([["" if cell is None else str(cell) for cell in row] for row in excel_rows[1:]],
                             [[row[field] for field in header] for row in csv_rows])
        finally:
            workbook.close()

    def test_paths_and_parameters_rejected_without_touching_existing_data(self):
        occupied = self.work / "occupied"
        occupied.mkdir()
        sentinel = occupied / "sentinel"
        sentinel.write_text("keep")
        for target in (occupied, ROOT / "data/mock_his", ROOT / "data/files", ROOT / "data/generated", self.work / "../.." / "escape"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                generator.generate(output_dir=target)
        self.assertEqual(sentinel.read_text(), "keep")
        with tempfile.TemporaryDirectory() as outside:
            link = self.work / "escape_link"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                generator.generate(output_dir=link / "new")
            self.assertEqual(list(Path(outside).iterdir()), [])
        for kwargs in ({"months": 0}, {"months": -1}, {"months": 1.5}, {"months": True},
                       {"seed": "42"}, {"seed": True}, {"start_date": "2025-1-01"},
                       {"start_date": "2025-02-30"}, {"start_date": "2025-01-02"},
                       {"start_date": "9999-12-01", "months": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                generator.generate(output_dir=self.work / "invalid", **kwargs)
        self.assertFalse((self.work / "invalid").exists())

    def test_calendar_leap_and_small_range(self):
        output = self.generate(start_date="2024-02-01", months=1)
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(manifest["date_range"]["end_date"], "2024-02-29")
        for entry in manifest["files"]:
            if entry.get("source"):
                self.assertGreaterEqual(entry["baseline_rows"], 100)
                self.assertEqual(entry["coverage_months"], ["2024-02"])
        empty = self.work / "empty"
        empty.mkdir()
        generator.generate(start_date="2025-02-01", months=1, output_dir=empty)
        self.assertTrue((empty / "manifest.json").is_file())

    def test_baseline_calendar_and_injection_without_excel(self):
        for start_date, months, end_date in (("2025-02-01", 1, "2025-02-28"),
                                             ("2024-02-01", 1, "2024-02-29"),
                                             ("2025-11-01", 3, "2026-01-31")):
            with self.subTest(start_date=start_date):
                start, end = generator.parameters(42, start_date, months)
                self.assertEqual(end.isoformat(), end_date)
                tables, rng = generator.baseline(42, start, months)
                self.assertTrue(generator.reconcile(tables)["passed"])
                for source, rows in tables.items():
                    count = len(rows)
                    self.assertGreaterEqual(count, 100)
                    for row in rows:
                        day = date.fromisoformat(row[generator.DATE_FIELDS[source]])
                        self.assertLessEqual(start, day)
                        self.assertLessEqual(day, end)
                        if source == "his_inpatient":
                            discharge = date.fromisoformat(row["discharge_date"])
                            self.assertGreater(discharge, day)
                            self.assertLessEqual(discharge, end)
                    cases = generator.inject(source, rows, rng)
                    counts = Counter(case["kind"] for case in cases)
                    self.assertEqual(counts["null"], count // 100)
                    self.assertEqual(counts["duplicate"], count // 100)
                    self.assertEqual(len(rows), count + count // 100)


if __name__ == "__main__":
    unittest.main()
