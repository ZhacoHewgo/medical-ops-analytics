"""Reproducible, explicitly synthetic course sources. Never reads original CSV rows."""
import argparse
import calendar
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from pathlib import Path
import random
import re
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "data/generated"
VERSION = "synthetic-v1"
HEADERS = {
    "his_outpatient": "visit_id,patient_id,patient_name,id_card,phone,gender,age,dept_code,visit_date,visit_type,fee_amount".split(","),
    "his_inpatient": "visit_id,patient_id,patient_name,id_card,phone,gender,age,dept_code,bed_no,admit_date,discharge_date,stay_days,total_fee,drug_fee,consumable_fee,disease_code".split(","),
    "his_charge": "charge_id,visit_id,patient_id,visit_type,dept_code,fee_date,fee_type_code,fee_type_name,amount".split(","),
    "emr_record": "record_id,patient_id,visit_id,dept_code,visit_date,diagnosis,disease_code".split(","),
    "lis_report": "report_id,patient_id,visit_id,item_name,result_text,report_date".split(","),
    "finance_excel": "record_id,stat_date,dept_code,revenue,drug_revenue,consumable_revenue".split(","),
}
DATE_FIELDS = dict(zip(HEADERS, ("visit_date", "admit_date", "fee_date", "visit_date", "report_date", "stat_date")))
NULL_FIELDS = dict(zip(HEADERS, ("fee_amount", "total_fee", "amount", "diagnosis", "result_text", "revenue")))
ALIASES = {f"SIM_科室{i}_别名": f"SIM_DEPT_{i:02}" for i in range(1, 5)}
DEPARTMENTS = list(ALIASES.values())
FEE_TYPES = (("SERVICE", "SIM_服务费"), ("DRUG", "SIM_药品费"), ("CONSUMABLE", "SIM_耗材费"))


def parameters(seed, start_date, months):
    if type(seed) is not int or type(months) is not int or months < 1:
        raise ValueError("seed must be an integer; months must be a positive integer")
    if not isinstance(start_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
        raise ValueError("start-date must be strict YYYY-MM-DD")
    try:
        start = date.fromisoformat(start_date)
        if start.day != 1:
            raise ValueError("start-date must be first day of a natural month")
        last_index = (start.year - 1) * 12 + start.month - 1 + months - 1
        last_year, last_month = last_index // 12 + 1, last_index % 12 + 1
        end = date(last_year, last_month, calendar.monthrange(last_year, last_month)[1])
    except (ValueError, OverflowError) as exc:
        raise ValueError("start-date/months must describe complete valid natural months") from exc
    return start, end


def output_path(output_dir, dataset_id):
    target = Path(output_dir) if output_dir is not None else GENERATED / dataset_id
    if not target.is_absolute():
        target = ROOT / target
    # Refuse links even within generated/, so output ownership has no hidden redirection.
    if any(path.is_symlink() for path in (GENERATED, *GENERATED.parents, target, *target.parents)):
        raise ValueError("output path must not contain symbolic links")
    target = target.resolve()
    if not target.is_relative_to(GENERATED.resolve()) or target == GENERATED.resolve():
        raise ValueError("output must be a dataset directory inside data/generated")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("output directory must be absent or empty; overwrite refused")
    return target


def baseline(seed, start, months):
    rng = random.Random(seed)
    tables = {source: [] for source in HEADERS}
    finance = defaultdict(lambda: {field: Decimal("0.00") for field in ("revenue", "drug_revenue", "consumable_revenue")})
    per_month = max(10, (112 + months - 1) // months)
    serial = 0
    for offset in range(months):
        index = (start.year - 1) * 12 + start.month - 1 + offset
        year, month = index // 12 + 1, index % 12 + 1
        for slot in range(per_month):
            visit_date = date(year, month, 1 + (slot // 4) % min(28, calendar.monthrange(year, month)[1] - 1))
            dept = DEPARTMENTS[slot % 4]
            for source, visit_type in (("his_outpatient", "OUTPATIENT"), ("his_inpatient", "INPATIENT")):
                serial += 1
                patient_id, visit_id = f"SIM_PAT_{serial:07}", f"SIM_VIS_{serial:07}"
                parts = [Decimal(rng.randint(100, 50000)) / 100 for _ in FEE_TYPES]
                total = sum(parts, Decimal("0.00"))
                row = {"visit_id": visit_id, "patient_id": patient_id, "patient_name": f"SIM_姓名_{serial:07}",
                       "id_card": f"SIM_ID_NOT_VALID_{serial:07}", "phone": f"SIM_PHONE_NOT_VALID_{serial:07}",
                       "gender": rng.choice(("M", "F")), "age": str(rng.randint(18, 85)), "dept_code": dept}
                disease = f"SIM_DX_{rng.randint(1, 5):02}"
                if source == "his_outpatient":
                    row.update(visit_date=visit_date.isoformat(), visit_type=visit_type, fee_amount=f"{total:.2f}")
                else:
                    stay = rng.randint(1, min(7, calendar.monthrange(year, month)[1] - visit_date.day))
                    row.update(bed_no=f"SIM_BED_{slot % 10:02}", admit_date=visit_date.isoformat(),
                               discharge_date=(visit_date + timedelta(days=stay)).isoformat(), stay_days=str(stay),
                               total_fee=f"{total:.2f}", drug_fee=f"{parts[1]:.2f}", consumable_fee=f"{parts[2]:.2f}", disease_code=disease)
                tables[source].append(row)
                for (code, name), amount in zip(FEE_TYPES, parts):
                    tables["his_charge"].append({"charge_id": f"SIM_CHG_{len(tables['his_charge']) + 1:07}",
                        "visit_id": visit_id, "patient_id": patient_id, "visit_type": visit_type, "dept_code": dept,
                        "fee_date": visit_date.isoformat(), "fee_type_code": code, "fee_type_name": name, "amount": f"{amount:.2f}"})
                    balance = finance[(visit_date.isoformat(), dept)]
                    balance["revenue"] += amount
                    if code in ("DRUG", "CONSUMABLE"):
                        balance[code.lower() + "_revenue"] += amount
                tables["emr_record"].append({"record_id": f"SIM_EMR_{serial:07}", "patient_id": patient_id,
                    "visit_id": visit_id, "dept_code": dept, "visit_date": visit_date.isoformat(),
                    "diagnosis": "SIM_虚构诊断_无临床意义", "disease_code": disease})
                tables["lis_report"].append({"report_id": f"SIM_LIS_{serial:07}", "patient_id": patient_id,
                    "visit_id": visit_id, "item_name": "SIM_虚构检验项目", "result_text": "SIM_虚构结果_非临床",
                    "report_date": visit_date.isoformat()})
    for serial, ((day, dept), values) in enumerate(sorted(finance.items()), 1):
        tables["finance_excel"].append({"record_id": f"SIM_FIN_{serial:07}", "stat_date": day, "dept_code": dept,
                                        **{field: f"{value:.2f}" for field, value in values.items()}})
    return tables, rng


def reconcile(tables):
    by_visit = defaultdict(lambda: Decimal("0.00"))
    by_day = defaultdict(lambda: defaultdict(lambda: Decimal("0.00")))
    visits = {row["visit_id"]: row for source in ("his_outpatient", "his_inpatient") for row in tables[source]}
    for row in tables["his_charge"]:
        visit = visits[row["visit_id"]]
        if row["patient_id"] != visit["patient_id"] or row["dept_code"] != visit["dept_code"] or row["fee_date"] != visit.get("visit_date", visit.get("admit_date")):
            raise ValueError("synthetic baseline relationship mismatch")
        amount = Decimal(row["amount"])
        by_visit[row["visit_id"]] += amount
        key = row["fee_date"], row["dept_code"]
        by_day[key]["revenue"] += amount
        if row["fee_type_code"] in ("DRUG", "CONSUMABLE"):
            by_day[key][row["fee_type_code"].lower() + "_revenue"] += amount
    if any(by_visit[key] != Decimal(row.get("fee_amount", row.get("total_fee"))) for key, row in visits.items()):
        raise ValueError("synthetic baseline visit fees do not reconcile")
    for row in tables["finance_excel"]:
        if any(Decimal(row[field]) != by_day[(row["stat_date"], row["dept_code"])][field] for field in ("revenue", "drug_revenue", "consumable_revenue")):
            raise ValueError("synthetic baseline finance does not reconcile")
    charge_total = sum(by_visit.values(), Decimal("0.00"))
    return {"passed": True, "visit_count": len(visits), "charge_total": f"{charge_total:.2f}",
            "finance_total": f"{sum((Decimal(row['revenue']) for row in tables['finance_excel']), Decimal('0.00')):.2f}",
            "scope": "clean baseline before independent source corruption; finance reconciles charges, never adds to them"}


def inject(source, rows, rng):
    count = len(rows) // 100
    has_dept = "dept_code" in HEADERS[source]
    chosen = rng.sample(range(len(rows)), count * (3 if has_dept else 2))
    cases = []
    for kind, indices in (("null", chosen[:count]), ("department_alias", chosen[count * 2:] if has_dept else [])):
        for index in indices:
            field = NULL_FIELDS[source] if kind == "null" else "dept_code"
            original = rows[index][field]
            value = "" if kind == "null" else next(alias for alias, code in ALIASES.items() if code == original)
            rows[index][field] = value
            cases.append({"source": source, "row_number": index + 1, "kind": kind, "field": field,
                          "original_value": original, "injected_value": value, "duplicate_of_row": None})
    for index in chosen[count:count * 2]:
        rows.append(dict(rows[index]))
        cases.append({"source": source, "row_number": len(rows), "kind": "duplicate", "field": None,
                      "original_value": None, "injected_value": None, "duplicate_of_row": index + 1})
    return cases


def _generated_file_path(path):
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    if any(part.is_symlink() for part in (GENERATED, *GENERATED.parents, target, *target.parents)):
        raise ValueError("output path must not contain symbolic links")
    target = target.resolve()
    if not target.is_relative_to(GENERATED.resolve()) or target == GENERATED.resolve():
        raise ValueError("output file must be inside data/generated")
    return target


def write_csv(df=None, path=None, rows=None, *, header=None):
    """Write a DataFrame, or retain legacy (path, header, rows) calls."""
    if rows is None:
        path = _generated_file_path(path)
        header, rows = list(df.columns), df.to_dict(orient="records")
    elif df is not None:
        path, header = df, path if path is not None else header
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_excel(path, rows):
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.title = "finance_excel"
    workbook.properties.created = workbook.properties.modified = datetime(2000, 1, 1)
    workbook.active.append(HEADERS["finance_excel"])
    for row in rows:
        workbook.active.append([row[field] for field in HEADERS["finance_excel"]])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    # Normalize ZIP timestamps and core modified time for byte-identical manifest hashes.
    with ZipFile(buffer) as source, ZipFile(path, "x") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "docProps/core.xml":
                data = re.sub(rb"(<dcterms:modified[^>]*>).*?(</dcterms:modified>)", rb"\g<1>2000-01-01T00:00:00Z\g<2>", data)
            item.date_time = (2000, 1, 1, 0, 0, 0)
            target.writestr(item, data)


def write_finance_excel(df, path):
    """Write finance DataFrame values through the existing deterministic writer."""
    path = _generated_file_path(path)
    rows = df.to_dict(orient="records")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_excel(path, rows)


def describe(path, target, header, base_count, final_count, source=None, rows=()):
    dates = sorted(row[DATE_FIELDS[source]] for row in rows) if source else []
    result = {"path": path.relative_to(target).as_posix(), "header": header,
              "baseline_rows": base_count, "final_rows": final_count,
              "valid_date_range": {"start_date": dates[0], "end_date": dates[-1]} if dates else None,
              "coverage_months": sorted({day[:7] for day in dates}),
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if source:
        result["source"] = source
    return result


def generate(seed=42, start_date="2025-01-01", months=12, output_dir=None):
    start, end = parameters(seed, start_date, months)
    dataset_id = f"{VERSION}_seed{seed}_{start_date}_{months}m"
    target = output_path(output_dir, dataset_id)
    # Import before any output writes: missing dependency leaves no partial dataset.
    import openpyxl
    tables, rng = baseline(seed, start, months)
    reconciliation = reconcile(tables)
    files, ledger, counts = [], [], {}
    for source, rows in tables.items():
        base_count = len(rows)
        cases = inject(source, rows, rng)
        ledger.extend(cases)
        counts[source] = dict(Counter(case["kind"] for case in cases))
        folder = "files" if source == "finance_excel" else "mock_his"
        path = target / folder / f"{source}.csv"
        write_csv(path, HEADERS[source], rows)
        files.append(describe(path, target, HEADERS[source], base_count, len(rows), source, rows))
        if source == "finance_excel":
            path = path.with_suffix(".xlsx")
            write_excel(path, rows)
            files.append(describe(path, target, HEADERS[source], base_count, len(rows), source, rows))
    path = target / "dictionaries/department_aliases.csv"
    write_csv(path, ["alias", "dept_code"], [{"alias": alias, "dept_code": code} for alias, code in ALIASES.items()])
    files.append(describe(path, target, ["alias", "dept_code"], len(ALIASES), len(ALIASES)))
    path = target / "dirty_cases.jsonl"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for case in ledger:
            stream.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    files.append(describe(path, target, list(ledger[0]) if ledger else [], 0, len(ledger)))
    manifest = {"schema_version": "1.0", "generator_version": VERSION, "dataset_id": dataset_id, "synthetic": True,
                "parameters": {"seed": seed, "start_date": start_date, "months": months, "output_dir": "."},
                "seed": seed, "date_range": {"start_date": start_date, "end_date": end.isoformat()},
                "files": files, "dirty_counts": counts, "standard_departments": DEPARTMENTS,
                "department_aliases": ALIASES, "baseline_reconciliation": reconciliation,
                "dirty_policy": "floor(N/100) nulls, exact duplicates and registered aliases where applicable; disjoint baseline row selections",
                "declaration": "完全合成课程数据；未读取原始患者行；非真实医院、非临床标准。财务CSV与XLSX是同一源，不累加收入。",
                "reproducibility": {"python": "3.10+; verify across runtime versions before claiming byte identity",
                                    "openpyxl": openpyxl.__version__, "xlsx": "text cells match CSV; fixed core properties and ZIP timestamps",
                                    "output_dir": "physical path excluded for reproducibility across output directories"}}
    with (target / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    return manifest


def generate_mock_data(months=12, *, seed=42, start_date="2025-01-01", output_dir=None):
    """Course entry point; keep generation, validation and safe defaults in generate."""
    return generate(seed=seed, start_date=start_date, months=months, output_dir=output_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        manifest = generate(**vars(args))
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(2, f"generation failed: {exc}\n")
    print(json.dumps({"dataset_id": manifest["dataset_id"], "synthetic": True,
                      "rows": {item["source"]: item["final_rows"] for item in manifest["files"] if item["path"].endswith(".csv") and item.get("source")},
                      "dirty_counts": manifest["dirty_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
