"""Course adapters use disposable generated data; original sources stay read-only."""
import csv
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from openpyxl import load_workbook
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/data/generate_mock.py"
SPEC = importlib.util.spec_from_file_location("course_generator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


@pytest.fixture
def work():
    generator.GENERATED.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="_test_d4_", dir=generator.GENERATED) as folder:
        yield Path(folder)


@pytest.fixture
def finance():
    return pd.DataFrame([
        ["SIM_FIN_1", "2025-01-01", "SIM_DEPT_01", "12.30", "2.00", "1.00"],
        ["SIM_FIN_2", "2025-01-02", "SIM_DEPT_02", "", "0.00", "0.00"],
    ], columns=generator.HEADERS["finance_excel"])


def test_work_fixture_creates_missing_generated_parent(monkeypatch, tmp_path):
    generated = tmp_path / "data/generated"
    monkeypatch.setattr(generator, "GENERATED", generated)
    assert not generated.exists()
    with contextmanager(work.__wrapped__)() as folder:
        assert folder.is_dir()
        assert folder.parent == generated
    assert generated.is_dir()
    assert list(generated.iterdir()) == []


def test_generate_mock_data_forwards_months_first_and_defaults(monkeypatch):
    calls = []
    result = object()

    def generate(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(generator, "generate", generate)
    assert generator.generate_mock_data() is result
    assert calls[-1] == dict(seed=42, start_date="2025-01-01", months=12, output_dir=None)
    assert generator.generate_mock_data(3, seed=7, start_date="2024-11-01", output_dir="data/generated/new") is result
    assert calls[-1] == dict(seed=7, start_date="2024-11-01", months=3, output_dir="data/generated/new")


def test_adapter_old_api_and_cli_have_identical_bytes_without_pandas(work):
    api, old, cli = (work / name for name in ("api", "old", "cli"))
    manifest = generator.generate_mock_data(3, seed=7, start_date="2024-11-01", output_dir=api)
    generator.generate(seed=7, start_date="2024-11-01", months=3, output_dir=old)
    # Blocking pandas proves the old standalone CLI does not require it.
    code = """
import builtins
import runpy
import sys
original_import = builtins.__import__
def without_pandas(name, *args, **kwargs):
    if name.split('.')[0] == 'pandas':
        raise ImportError('pandas intentionally unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_pandas
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    process = subprocess.run([
        sys.executable, "-c", code, str(SCRIPT), "--seed", "7", "--start-date", "2024-11-01",
        "--months", "3", "--output-dir", str(cli),
    ], cwd=ROOT, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["synthetic"] is True
    assert manifest["date_range"] == {"start_date": "2024-11-01", "end_date": "2025-01-31"}
    assert {entry["source"] for entry in manifest["files"] if entry.get("source")} == set(generator.HEADERS)
    for output in (old, cli):
        files = {path.relative_to(api): path.read_bytes() for path in api.rglob("*") if path.is_file()}
        assert files == {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="overwrite refused"):
        generator.generate_mock_data(3, seed=7, start_date="2024-11-01", output_dir=api)


def test_csv_dataframe_and_legacy_positional_keyword_bytes(work):
    frame = pd.DataFrame([
        ["SIM_文字", 1.25, Decimal("2.30"), date(2025, 1, 1), None],
    ], columns=["text", "float", "decimal", "date", "empty"], dtype=object)
    header, rows = list(frame.columns), frame.to_dict(orient="records")
    positional, keywords, mixed, course, course_keywords = (work / name for name in (
        "old.csv", "old_keywords.csv", "old_mixed.csv", "nested/course.csv", "course_keywords.csv",
    ))
    generator.write_csv(positional, header, rows)
    generator.write_csv(path=keywords, header=header, rows=rows)
    generator.write_csv(mixed, header=header, rows=rows)
    generator.write_csv(frame, course)
    generator.write_csv(df=frame, path=course_keywords)
    assert positional.read_bytes() == keywords.read_bytes() == mixed.read_bytes() == course.read_bytes() == course_keywords.read_bytes()


def test_finance_dataframe_reuses_excel_bytes_and_matches_csv(work, finance):
    old, course, csv_path = work / "old.xlsx", work / "nested/course.xlsx", work / "course.csv"
    generator.write_excel(path=old, rows=finance.to_dict(orient="records"))
    generator.write_finance_excel(df=finance, path=course)
    generator.write_csv(finance, csv_path)
    assert old.read_bytes() == course.read_bytes()
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        csv_rows = list(csv.reader(stream))
    workbook = load_workbook(course, read_only=True)
    try:
        assert [["" if cell is None else str(cell) for cell in row] for row in workbook.active.values] == csv_rows
    finally:
        workbook.close()


def test_finance_adapter_preserves_existing_scalar_handling(work, finance):
    frame = finance.astype(object)
    frame.loc[0, "stat_date"] = date(2025, 1, 1)
    frame.loc[0, "revenue"] = Decimal("12.30")
    frame.loc[0, "drug_revenue"] = 2.25
    frame.loc[1, "revenue"] = None
    old, course = work / "old.xlsx", work / "course.xlsx"
    generator.write_excel(old, frame.to_dict(orient="records"))
    generator.write_finance_excel(frame, course)
    assert old.read_bytes() == course.read_bytes()


@pytest.mark.parametrize("writer", ["write_csv", "write_finance_excel"])
def test_course_writers_refuse_overwrite(work, finance, writer):
    target = work / "occupied"
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        getattr(generator, writer)(finance, target)
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("writer", ["write_csv", "write_finance_excel"])
@pytest.mark.parametrize("location", ["raw_his", "raw_files", "outside", "traversal", "boundary"])
def test_course_writers_refuse_unsafe_paths(work, finance, writer, location):
    target = {
        "raw_his": ROOT / "data/mock_his/_test_d4_forbidden",
        "raw_files": ROOT / "data/files/_test_d4_forbidden",
        "outside": ROOT / "_test_d4_forbidden",
        "traversal": work / "../../_test_d4_forbidden",
        "boundary": generator.GENERATED,
    }[location]
    existed = target.exists()
    with pytest.raises(ValueError):
        getattr(generator, writer)(finance, target)
    assert target.exists() == existed


@pytest.mark.parametrize("writer", ["write_csv", "write_finance_excel"])
def test_course_writers_refuse_symlink_paths(work, finance, writer):
    link = work / "raw_link"
    link.symlink_to(ROOT / "data/mock_his", target_is_directory=True)
    with pytest.raises(ValueError):
        getattr(generator, writer)(finance, link / "_test_d4_forbidden")
    target = work / "existing"
    target.write_bytes(b"keep")
    file_link = work / "file_link"
    file_link.symlink_to(target)
    with pytest.raises(ValueError):
        getattr(generator, writer)(finance, file_link)
    assert target.read_bytes() == b"keep"
