import hashlib
import json

import pytest

from conftest import row
from app.outpatient import validate_aggregate
from etl.jobs.build_outpatient import build_outpatient


def test_cleaning_conservation_and_no_private_fields(csv_file, tmp_path):
    source = csv_file([
        row(" v1 ", "2025-01-01 ", " B ", " private "),
        row("v1", private_note="private"),
        row("v2", "2025-01-03", "  "),
        row("v3", "2025-01-03", "A"),
        row("conflict", private_note="X"),
        row("conflict", private_note="Y"),
        row("conflict", private_note="X"),
        row(" ", "bad-date"),
        row("v5", "2025-1-1"),
        row("v6", "2025-02-29"),
    ])
    original = source.read_bytes()
    target = tmp_path / "derived" / "outpatient.json"
    result = build_outpatient(source, target)
    assert json.loads(target.read_text()) == result
    assert source.read_bytes() == original
    assert result["meta"]["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["meta"]["quality"] == {
        "raw_rows": 10, "invalid_id_rows": 1, "invalid_date_rows": 2,
        "conflicting_id_rows": 3, "duplicate_rows_removed": 1,
        "accepted_visits": 3, "unknown_department_visits": 1,
    }
    assert result["counts"] == [
        {"date": "2025-01-01", "dept_code": "B", "visits": 1},
        {"date": "2025-01-03", "dept_code": "A", "visits": 1},
        {"date": "2025-01-03", "dept_code": "__UNKNOWN__", "visits": 1},
    ]
    assert set(result) == {"meta", "counts"}
    assert all(set(item) == {"date", "dept_code", "visits"} for item in result["counts"])
    assert not any(word in target.read_text() for word in ("visit_id", "private_note", "SYNTHETIC_SECRET", "conflict" + '"'))


@pytest.mark.parametrize("bad_date", ["2025-1-1", "2025-01-1", "2025-02-29", "2025-13-01", "2025-01-01T00:00:00", "２０２５-01-01", ""])
def test_strict_date_isolation(csv_file, tmp_path, bad_date):
    result = build_outpatient(csv_file([row(), row("bad", bad_date)]), tmp_path / "out.json")
    assert result["meta"]["quality"]["invalid_date_rows"] == 1
    assert result["meta"]["quality"]["accepted_visits"] == 1


def test_calendar_leap_day_and_normalization_order(csv_file, tmp_path):
    result = build_outpatient(csv_file([
        row("v1", " 2024-02-29 ", ""), row("v1", "2024-02-29", "__UNKNOWN__"),
        row("v2", "2024-02-29", "a"), row("v3", "2024-02-29", "A"),
    ]), tmp_path / "out.json")
    assert result["meta"]["quality"]["conflicting_id_rows"] == 2
    assert result["meta"]["department_codes"] == ["A", "a"]


@pytest.mark.parametrize("rows,columns", [
    ([], None), ([row("", "bad")], None),
    ([row("v1", private_note="X"), row("v1", private_note="Y")], None),
    ([{"visit_id": "v1", "visit_date": "2025-01-01"}], ["visit_id", "visit_date"]),
])
def test_failed_build_keeps_previous_output(csv_file, tmp_path, rows, columns):
    target = tmp_path / "out.json"
    target.write_text("previous aggregate")
    with pytest.raises(ValueError):
        build_outpatient(csv_file(rows, columns), target)
    assert target.read_text() == "previous aggregate"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.json", "synthetic.csv"]


@pytest.mark.parametrize("text", [
    "visit_id,visit_date,dept_code\nv1,2025-01-01\n",
    "visit_id,visit_date,dept_code\nv1,2025-01-01,A,extra\n",
    "visit_id,visit_date,dept_code,dept_code\nv1,2025-01-01,A,A\n",
])
def test_malformed_csv_fails_without_private_values(tmp_path, text):
    source = tmp_path / "bad.csv"
    source.write_text(text)
    with pytest.raises(ValueError) as error:
        build_outpatient(source, tmp_path / "out.json")
    assert "v1" not in str(error.value)
    assert not (tmp_path / "out.json").exists()


def test_atomic_replace_failure_preserves_old_file(csv_file, tmp_path, monkeypatch):
    target = tmp_path / "out.json"
    target.write_text("previous aggregate")

    def fail_replace(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("etl.jobs.build_outpatient.os.replace", fail_replace)
    with pytest.raises(OSError):
        build_outpatient(csv_file([row()]), target)
    assert target.read_text() == "previous aggregate"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.json", "synthetic.csv"]


@pytest.mark.parametrize("path,value", [
    (("counts",), []), (("counts", 0, "visits"), True),
    (("counts", 0, "visits"), "2"), (("counts", 0, "visits"), 99),
    (("counts", 0, "date"), "2025-1-1"),
    (("counts", 0, "dept_code"), "\ud800"),
    (("meta", "quality", "raw_rows"), 99),
    (("meta", "quality", "unknown_department_visits"), 0),
    (("meta", "simulated"), 1), (("meta", "source_sha256"), "bad"),
    (("meta", "available_start_date"), "2024-01-01"),
    (("meta", "generated_at"), "2025-01-01T00:00:00"),
])
def test_aggregate_validator_rejects_corrupt_values(aggregate_path, path, value):
    document = json.loads(aggregate_path.read_text())
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    if path == ("counts", 0, "dept_code"):
        document["meta"]["department_codes"] = ["A", "__UNKNOWN__", "\ud800"]
    with pytest.raises(ValueError):
        validate_aggregate(document)
