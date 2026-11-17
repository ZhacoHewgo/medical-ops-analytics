"""Node 6 extraction: isolated text fixtures; MySQL requires coordinator injection."""

import csv
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock

import pandas as pd
import pytest
from openpyxl import Workbook

from etl.config import source_config as sources
from etl.jobs import extract_ods as ods


DATES = {
    "his_outpatient": "visit_date", "his_inpatient": "admit_date",
    "his_charge": "fee_date", "emr_record": "visit_date",
    "lis_report": "report_date", "finance_excel": "stat_date",
}
API = {
    "his_outpatient": "extract_his_outpatient", "his_inpatient": "extract_his_inpatient",
    "his_charge": "extract_his_charge", "emr_record": "extract_emr", "lis_report": "extract_lis",
}
BATCH = "2025-12-31"


def records(name, dataset="x6_unit"):
    rows = []
    for number, day in enumerate(("2025-01-01", "2025-01-02", "2025-01-03"), 1):
        row = {key: "SIM_重复 ' \\ 文本 " for key in sources.SOURCES[name]["columns"]}
        row[DATES[name]] = day
        row[sources.SOURCES[name]["columns"][-1]] = ("", "NA", "001.2300")[number - 1]
        row[DATES[name]] = day
        row.update(dataset_id=dataset, source_row_number=number, etl_batch_date=BATCH)
        rows.append(row)
    return rows


def write_csv(info, name, rows):
    path = Path(info["file_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def source_infos():
    sources.GENERATED_ROOT.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="x6_test_", dir=sources.GENERATED_ROOT))
    infos = {}
    try:
        for name, spec in sources.SOURCES.items():
            info = dict(kind="csv", file_path=str(directory / spec["path"]),
                        dataset_id=directory.name, etl_batch_date=BATCH)
            infos[name] = info
            write_csv(info, name, records(name, directory.name))
        yield infos
    finally:
        shutil.rmtree(directory)


def test_source_infos_creates_missing_parent(tmp_path, monkeypatch):
    generated_root = tmp_path / "isolated" / "generated"
    monkeypatch.setattr(sources, "GENERATED_ROOT", generated_root)
    assert not generated_root.exists()
    fixture = source_infos.__wrapped__()
    try:
        infos = next(fixture)
        assert set(infos) == set(sources.SOURCES)
        assert all(Path(info["file_path"]).is_file() for info in infos.values())
    finally:
        fixture.close()
    assert not list(generated_root.iterdir())


@pytest.mark.parametrize("name", list(API))
def test_public_extract_preserves_text_sequence_boundaries_and_empty(source_infos, name):
    extract = getattr(ods, API[name])
    frame = extract("2025-01-02", "2025-01-03", source_info=source_infos[name])
    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == list(sources.SOURCES[name]["columns"] + ods.METADATA)
    assert frame.to_dict("records") == records(name, source_infos[name]["dataset_id"])[1:]
    empty = extract("2024-01-01", "2024-01-01", source_info=source_infos[name])
    assert empty.empty and list(empty.columns) == list(frame.columns)
    assert not empty.attrs


@pytest.mark.parametrize("start,end", [
    (None, "2025-01-01"), ("2025-01-01", None), ("2025-1-01", "2025-01-02"),
    (" 2025-01-01", "2025-01-02"), ("２０２５-01-01", "2025-01-02"),
    ("2025-02-29", "2025-03-01"), ("2025-01-01T00:00:00", "2025-01-02"),
    (date(2025, 1, 1), "2025-01-02"), ("2025-01-03", "2025-01-02"),
])
def test_public_range_rejected_before_source_read(monkeypatch, start, end):
    reader = MagicMock()
    monkeypatch.setattr(ods, "read_source", reader)
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_INVALID$"):
        ods.extract_his_outpatient(start, end)
    reader.assert_not_called()


@pytest.mark.parametrize("name", list(DATES))
def test_bad_date_anywhere_aborts_all_writes(source_infos, monkeypatch, name):
    rows = records(name, source_infos[name]["dataset_id"])
    rows[0][DATES[name]] = "2025-1-01"
    write_csv(source_infos[name], name, rows)
    loader = MagicMock()
    monkeypatch.setattr(ods, "_load_range_to_ods", loader, raising=False)
    with pytest.raises(sources.OdsError, match="^ODS_SOURCE_DATE_INVALID$"):
        ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=source_infos)
    loader.assert_not_called()


def test_unused_discharge_date_kept(source_infos):
    frame = ods.extract_his_inpatient("2025-01-01", "2025-01-01",
                                      source_info=source_infos["his_inpatient"])
    assert frame.iloc[0]["discharge_date"] == "SIM_重复 ' \\ 文本 "


@pytest.mark.parametrize("problem", ["missing", "unknown", "dataset", "batch", "fields"])
def test_config_consistency_fails_before_reads_and_writes(source_infos, monkeypatch, problem):
    if problem == "missing":
        del source_infos["finance_excel"]
    elif problem == "unknown":
        source_infos["unknown"] = source_infos["finance_excel"]
    elif problem == "dataset":
        source_infos["finance_excel"]["dataset_id"] = "other_dataset"
    elif problem == "batch":
        source_infos["finance_excel"]["etl_batch_date"] = "2025-12-30"
    else:
        source_infos["finance_excel"]["extra"] = "SIM_PRIVATE"
    reader, loader = MagicMock(), MagicMock()
    monkeypatch.setattr(ods, "read_source", reader)
    monkeypatch.setattr(ods, "_load_range_to_ods", loader, raising=False)
    with pytest.raises(sources.OdsError):
        ods.run_extract_all(None, None, source_infos=source_infos)
    reader.assert_not_called()
    loader.assert_not_called()


def test_run_preloads_once_full_frames_schema_and_global_span(source_infos, monkeypatch):
    original = ods.read_source
    reader = MagicMock(side_effect=original)
    monkeypatch.setattr(ods, "read_source", reader)
    schema = MagicMock()
    monkeypatch.setattr(ods, "verify_schema", schema)
    calls, events = [], []

    def load(frame, table, start, end):
        assert reader.call_count == 6
        assert len(frame) == 3
        assert (start, end) == ("2025-01-01", "2025-01-03")
        calls.append(table)
        return 3

    monkeypatch.setattr(ods, "_load_range_to_ods", load, raising=False)
    monkeypatch.setattr(ods, "load_to_ods", MagicMock(side_effect=AssertionError("snapshot forbidden")))
    registry = MagicMock(return_value=[dict(name=name, **info) for name, info in source_infos.items()])
    monkeypatch.setattr(ods, "list_sources", registry, raising=False)
    result = ods.run_extract_all(None, None, on_event=lambda *event: events.append(event))
    assert result == dict.fromkeys(sources.SOURCES, 3)
    assert list(result) == list(sources.SOURCES)
    assert calls == [spec["table"] for spec in sources.SOURCES.values()]
    schema.assert_called_once_with()
    registry.assert_called_once_with()
    assert [(e[0], e[1], e[2]) for e in events] == [
        (name, status, count) for name in sources.SOURCES
        for status, count in (("START", None), ("SUCCESS", 3))]
    assert all(len(e) == 4 and type(e[3]) is int and e[3] >= 0 for e in events)


def workbook(source_infos):
    info = source_infos["finance_excel"]
    path = Path(info["file_path"]).with_suffix(".xlsx")
    book = Workbook()
    sheet = book.active
    sheet.title = "finance_excel"
    sheet.append(sources.SOURCES["finance_excel"]["columns"])
    for row in records("finance_excel", info["dataset_id"]):
        sheet.append([row[key] for key in sources.SOURCES["finance_excel"]["columns"]])
    return path, book, sheet


def test_finance_csv_xlsx_equivalent(source_infos):
    path, book, _ = workbook(source_infos)
    book.save(path)
    info = source_infos["finance_excel"]
    csv_frame = ods.extract_finance_excel(info["file_path"], source_info=info)
    xlsx_frame = ods.extract_finance_excel(path, source_info=info)
    pd.testing.assert_frame_equal(csv_frame, xlsx_frame)
    with pytest.raises(sources.OdsError, match="ODS_PATH_INVALID"):
        sources.validate_source("finance_excel", dict(info, file_path=str(path)))


@pytest.mark.parametrize("bad", ["formula", "error", "number", "bool", "date", "sheet", "header", "extra", "empty"])
def test_xlsx_rejects_nontext_or_bad_shape(source_infos, bad):
    path, book, sheet = workbook(source_infos)
    if bad in {"formula", "error", "number", "bool", "date"}:
        sheet["D2"] = {"formula": "=1+1", "error": "#DIV/0!", "number": 1,
                       "bool": True, "date": datetime(2025, 1, 1)}[bad]
    elif bad == "sheet":
        book.create_sheet("extra")["A1"] = "SIM"
    elif bad == "header":
        sheet["A1"] = "wrong"
    elif bad == "extra":
        sheet["G2"] = "SIM"
    else:
        sheet.delete_rows(2, 3)
    book.save(path)
    with pytest.raises(sources.OdsError):
        ods.extract_finance_excel(path, source_info=source_infos["finance_excel"])


@pytest.mark.parametrize("bad", ["outside", "wrong_dataset", "symlink", "traversal"])
def test_finance_path_boundary(source_infos, tmp_path, bad):
    info = source_infos["finance_excel"]
    path = Path(info["file_path"]).with_suffix(".xlsx")
    if bad == "outside":
        path = tmp_path / "finance_excel.xlsx"
    elif bad == "wrong_dataset":
        path = sources.GENERATED_ROOT / "other_dataset/files/finance_excel.xlsx"
    elif bad == "symlink":
        path.symlink_to(tmp_path / "finance_excel.xlsx")
    else:
        path = path.parent / ".." / "files" / path.name
    with pytest.raises(sources.OdsError, match="^ODS_PATH_INVALID$"):
        ods.extract_finance_excel(path, source_info=info)


def database_mock(monkeypatch, old, after=None):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    columns = sources.SOURCES["lis_report"]["columns"] + ods.METADATA + ("id", "etl_load_time")
    cursor.fetchall.side_effect = [
        [tuple(row.get(key, 1 if key == "id" else datetime(2025, 1, 1)) for key in columns) for row in rows]
        for rows in (old, old if after is None else after)]
    monkeypatch.setattr(ods, "connect_database", lambda: connection)
    return connection, cursor


def test_range_transaction_only_deletes_selected_rows_and_checks_full_contents(monkeypatch):
    rows = records("lis_report")
    connection, cursor = database_mock(monkeypatch, rows)
    assert ods._load_range_to_ods(pd.DataFrame(rows), "ods_lis_report", "2025-01-02", "2025-01-02") == 1
    delete = [(sql, args) for sql, args in (call.args for call in cursor.execute.call_args_list)
              if sql.startswith("DELETE")]
    assert delete == [("DELETE FROM `ods_lis_report` WHERE dataset_id=%s AND source_row_number=%s", ("x6_unit", 2))]
    assert "FOR UPDATE" in cursor.execute.call_args_list[0].args[0]
    values = cursor.executemany.call_args.args[1]
    assert len(values) == 1
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("problem", ["outside_change", "old_bad_date", "empty_baseline", "row_shift"])
def test_range_rebase_fails_closed_before_delete(monkeypatch, problem):
    rows, old = records("lis_report"), records("lis_report")
    if problem == "outside_change":
        old[0]["result_text"] = "SIM_changed"
    elif problem == "old_bad_date":
        old[0]["report_date"] = "bad"
    elif problem == "empty_baseline":
        old = []
    else:
        rows = rows[1:]
        for number, row in enumerate(rows, 1):
            row["source_row_number"] = number
    connection, cursor = database_mock(monkeypatch, old)
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_REBASE_REQUIRED$"):
        ods._load_range_to_ods(rows, "ods_lis_report", "2025-01-02", "2025-01-02")
    assert not any(call.args[0].startswith("DELETE") for call in cursor.execute.call_args_list)
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("failure,code", [("insert", "ODS_RANGE_LOAD_FAILED"),
                                          ("verify", "ODS_RANGE_VERIFY_FAILED"),
                                          ("permanent", "ODS_SCHEMA_INVALID"),
                                          ("rollback", "ODS_ROLLBACK_FAILED")])
def test_range_rollback_safe_codes(monkeypatch, failure, code):
    rows = records("lis_report")
    connection, cursor = database_mock(monkeypatch, rows, rows[:1] if failure == "verify" else rows)
    if failure in {"insert", "rollback"}:
        cursor.executemany.side_effect = RuntimeError("SIM_SECRET_原文")
    if failure == "permanent":
        cursor.executemany.side_effect = sources.OdsError("ODS_SCHEMA_INVALID")
    if failure == "rollback":
        connection.rollback.side_effect = RuntimeError("SIM_SECRET_原文")
    with pytest.raises(sources.OdsError, match=f"^{code}$"):
        ods._load_range_to_ods(rows, "ods_lis_report", "2025-01-02", "2025-01-02")
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()


def test_empty_range_removes_old_rows_but_keeps_outside_batch(monkeypatch):
    old = records("lis_report")
    rows = old[:2]
    rows = [dict(row, etl_batch_date="2026-01-01") for row in rows]
    connection, cursor = database_mock(monkeypatch, old, old[:2])
    assert ods._load_range_to_ods(rows, "ods_lis_report", "2025-01-03", "2025-01-03") == 0
    cursor.executemany.assert_not_called()
    connection.commit.assert_called_once()


def test_failfast_partial_prefix_and_blocked_callbacks(source_infos, monkeypatch):
    calls, events = [], []
    monkeypatch.setattr(ods, "verify_schema", lambda: None)

    def load(frame, table, start, end):
        calls.append(table)
        if len(calls) == 3:
            raise sources.OdsError("ODS_RANGE_REBASE_REQUIRED")
        return 1

    monkeypatch.setattr(ods, "_load_range_to_ods", load, raising=False)
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_REBASE_REQUIRED$"):
        ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=source_infos,
                            on_event=lambda *event: events.append(event))
    assert len(calls) == 3
    assert [e[0] for e in events if e[1] == "SUCCESS"] == list(sources.SOURCES)[:2]
    assert [(e[0], e[1], e[2]) for e in events[-4:]] == [
        ("his_charge", "FAILED", None), ("emr_record", "BLOCKED", None),
        ("lis_report", "BLOCKED", None), ("finance_excel", "BLOCKED", None)]


@pytest.mark.parametrize("status,expected_writes", [("START", 0), ("SUCCESS", 1)])
def test_callback_failure_aborts_and_does_not_claim_rollback(source_infos, monkeypatch, status, expected_writes):
    loader = MagicMock(return_value=1)
    monkeypatch.setattr(ods, "_load_range_to_ods", loader, raising=False)
    monkeypatch.setattr(ods, "verify_schema", lambda: None)

    def callback(name, event, rows, cost_ms):
        if event == status:
            raise RuntimeError("SIM_SECRET_callback")

    with pytest.raises(sources.OdsError, match="^ODS_EVENT_CALLBACK_FAILED$"):
        ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=source_infos, on_event=callback)
    assert loader.call_count == expected_writes


@pytest.mark.parametrize("info", [[], "SIM_SECRET", 1, {"kind": "csv"}])
def test_malformed_public_config_has_safe_error(info):
    with pytest.raises(sources.OdsError, match="^ODS_CONFIG_INVALID$"):
        ods.extract_lis("2025-01-01", "2025-01-03", source_info=info)


@pytest.mark.parametrize("column,value", [("id", 99), ("etl_load_time", datetime(2026, 1, 1)),
                                         ("etl_batch_date", date(2026, 1, 1)), ("result_text", "SIM_CHANGED")])
def test_postwrite_outside_metadata_or_content_drift_rolls_back(monkeypatch, column, value):
    old = records("lis_report")
    after = [dict(row) for row in old]
    after[0][column] = value
    connection, _ = database_mock(monkeypatch, old, after)
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_VERIFY_FAILED$"):
        ods._load_range_to_ods(old, "ods_lis_report", "2025-01-02", "2025-01-02")
    connection.rollback.assert_called_once()


def test_noop_empty_range_does_not_delete_or_insert(monkeypatch):
    rows = records("lis_report")
    connection, cursor = database_mock(monkeypatch, rows)
    assert ods._load_range_to_ods(rows, "ods_lis_report", "2024-01-01", "2024-01-01") == 0
    assert not any(call.args[0].startswith("DELETE") for call in cursor.execute.call_args_list)
    cursor.executemany.assert_not_called()
    connection.commit.assert_called_once()


def test_preflight_freezes_config_and_file_views(source_infos, monkeypatch):
    schema = MagicMock()
    monkeypatch.setattr(ods, "verify_schema", schema)
    dataset = source_infos["his_outpatient"]["dataset_id"]

    def event(name, status, rows, cost_ms):
        if name == "his_outpatient" and status == "START":
            for info in source_infos.values():
                info["dataset_id"] = "other_dataset"
            # Later sources must use already-read frames, not read this truncated file again.
            Path(source_infos["finance_excel"]["file_path"]).write_text("", encoding="utf-8")

    def load(frame, table, start, end):
        assert set(frame["dataset_id"]) == {dataset}
        assert len(frame) == 3
        return 1

    monkeypatch.setattr(ods, "_load_range_to_ods", load)
    assert ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=source_infos,
                               on_event=event) == dict.fromkeys(sources.SOURCES, 1)


def test_failed_event_callback_does_not_mask_permanent_source_error(source_infos, monkeypatch):
    monkeypatch.setattr(ods, "verify_schema", lambda: None)
    monkeypatch.setattr(ods, "_load_range_to_ods", MagicMock(side_effect=sources.OdsError("ODS_RANGE_REBASE_REQUIRED")))

    def callback(name, status, rows, cost_ms):
        if status == "FAILED":
            raise sources.OdsError("ODS_DATABASE_CONNECT_FAILED")

    with pytest.raises(sources.OdsError, match="^ODS_RANGE_REBASE_REQUIRED$"):
        ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=source_infos, on_event=callback)


@pytest.mark.parametrize("bad", ["2025-02-29", "2025-1-01", " 2025-01-01", "２０２５-01-01", "", "2025-01-01T00:00:00"])
def test_full_source_date_strict_even_outside_filter(source_infos, bad):
    name = "his_outpatient"
    rows = records(name, source_infos[name]["dataset_id"])
    rows[0]["visit_date"] = bad
    write_csv(source_infos[name], name, rows)
    with pytest.raises(sources.OdsError, match="^ODS_SOURCE_DATE_INVALID$"):
        ods.extract_his_outpatient("2025-01-02", "2025-01-02", source_info=source_infos[name])


@pytest.mark.parametrize("problem", ["header", "truncated", "missing", "empty"])
def test_last_file_preflight_failure_makes_no_database_calls(source_infos, monkeypatch, problem):
    path = Path(source_infos["finance_excel"]["file_path"])
    if problem == "header":
        path.write_text("wrong\n", encoding="utf-8")
    elif problem == "truncated":
        path.write_text(",".join(sources.SOURCES["finance_excel"]["columns"]) + "\nSIM\n", encoding="utf-8")
    elif problem == "missing":
        path.unlink()
    else:
        write_csv(source_infos["finance_excel"], "finance_excel", [])
    connect = MagicMock(side_effect=AssertionError("database forbidden"))
    monkeypatch.setattr(ods, "connect_database", connect)
    schema = MagicMock()
    monkeypatch.setattr(ods, "verify_schema", schema)
    with pytest.raises(sources.OdsError):
        ods.run_extract_all(None, None, source_infos=source_infos)
    connect.assert_not_called()
    schema.assert_not_called()


def test_schema_permanent_error_stops_before_source_writes(source_infos, monkeypatch):
    monkeypatch.setattr(ods, "verify_schema", MagicMock(side_effect=sources.OdsError("ODS_SCHEMA_INVALID")))
    loader = MagicMock()
    monkeypatch.setattr(ods, "_load_range_to_ods", loader)
    with pytest.raises(sources.OdsError, match="^ODS_SCHEMA_INVALID$"):
        ods.run_extract_all(None, None, source_infos=source_infos)
    loader.assert_not_called()


def test_public_default_reads_only_requested_registration(source_infos, monkeypatch):
    getter = MagicMock(return_value=source_infos["lis_report"])
    monkeypatch.setattr(ods, "get_source", getter)
    assert len(ods.extract_lis("2025-01-02", "2025-01-02")) == 1
    getter.assert_called_once_with("lis_report")


def test_xlsx_blank_extra_sheet_allowed_and_corrupt_workbook_safe(source_infos):
    path, book, _ = workbook(source_infos)
    book.create_sheet("blank")
    book.save(path)
    assert len(ods.extract_finance_excel(path, source_info=source_infos["finance_excel"])) == 3
    path.write_bytes(b"SIM_SECRET_not_a_zip")
    with pytest.raises(sources.OdsError, match="^ODS_XLSX_READ_FAILED$"):
        ods.extract_finance_excel(path, source_info=source_infos["finance_excel"])


def test_private_range_rejects_filtered_frame_and_injection_before_connect(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(ods, "connect_database", connect)
    with pytest.raises(sources.OdsError, match="^ODS_ROW_NUMBER_INVALID$"):
        ods._load_range_to_ods(pd.DataFrame(records("lis_report")[1:]), "ods_lis_report",
                               "2025-01-02", "2025-01-03")
    with pytest.raises(sources.OdsError, match="^ODS_TABLE_INVALID$"):
        ods._load_range_to_ods(records("lis_report"), "ods_lis_report; DROP TABLE source_config",
                               "2025-01-02", "2025-01-03")
    connect.assert_not_called()


@pytest.mark.parametrize("primary", [False, True])
def test_range_close_failure_preserves_permanent_primary(monkeypatch, primary):
    rows = records("lis_report")
    old = [dict(row) for row in rows]
    if primary:
        old[0]["result_text"] = "SIM_OUTSIDE_CHANGED"
    connection, _ = database_mock(monkeypatch, old)
    connection.close.side_effect = RuntimeError("SIM_SECRET_close")
    code = "ODS_RANGE_REBASE_REQUIRED" if primary else "ODS_DATABASE_CLOSE_FAILED"
    with pytest.raises(sources.OdsError, match=f"^{code}$"):
        ods._load_range_to_ods(rows, "ods_lis_report", "2025-01-02", "2025-01-02")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode("utf-8")).hexdigest()


def _fetch(name, dataset, *, metadata=False):
    spec = sources.SOURCES[name]
    columns = spec["columns"] + ods.METADATA + (("id", "etl_load_time") if metadata else ())
    connection = sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {','.join('`' + col + '`' for col in columns)} "
                           f"FROM `{spec['table']}` WHERE dataset_id=%s ORDER BY source_row_number", (dataset,))
            result = [dict(zip(columns, values)) for values in cursor.fetchall()]
        for row in result:
            row["etl_batch_date"] = str(row["etl_batch_date"])
        return result
    finally:
        connection.close()


def _independent_expected(name, info, start="0001-01-01", end="9999-12-31"):
    # Independent standard-library CSV read; no implementation parser/filter reused.
    result = []
    with Path(info["file_path"]).open(encoding="utf-8-sig", newline="") as stream:
        for number, row in enumerate(csv.DictReader(stream), 1):
            if start <= row[DATES[name]] <= end:
                result.append(dict(row, dataset_id=info["dataset_id"], source_row_number=number,
                                   etl_batch_date=info["etl_batch_date"]))
    return result


@pytest.fixture
def mysql_x6(monkeypatch, source_infos):
    if not os.environ.get("ODS_TEST_DATABASE_URL"):
        pytest.skip("ODS_TEST_DATABASE_URL_NOT_INJECTED: real MySQL NOT RUN")
    # Validate exact database before connecting or allowing any write/cleanup.
    sources.database_options(test_only=True)
    monkeypatch.setenv("ODS_DATABASE_URL", os.environ["ODS_TEST_DATABASE_URL"])
    connection = sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE()")
            if cursor.fetchone() != ("hospital_test",):
                pytest.fail("ODS_TEST_DATABASE_REQUIRED", pytrace=False)
            cursor.execute("SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name")
            registry_before = list(cursor.fetchall())
    finally:
        connection.close()
    ods.verify_schema()  # Existing O5 schema only; this fixture never creates or alters tables.
    dataset = source_infos["his_outpatient"]["dataset_id"]
    owned_datasets = (dataset, dataset + "_other")
    try:
        yield source_infos, owned_datasets
    finally:
        connection = sources.connect_database(test_only=True)
        try:
            with connection.cursor() as cursor:
                for spec in sources.SOURCES.values():
                    for owned in owned_datasets:
                        cursor.execute(f"DELETE FROM `{spec['table']}` WHERE dataset_id=%s", (owned,))
                # source_infos injection avoids global registry writes. Restore only an owned
                # registration if a failing test accidentally registered its isolated dataset.
                cursor.execute("SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name")
                registry_after = list(cursor.fetchall())
                previous = {row[0]: row for row in registry_before}
                for row in registry_after:
                    if row[3] in owned_datasets:
                        cursor.execute("DELETE FROM source_config WHERE name=%s AND dataset_id=%s", (row[0], row[3]))
                        if row[0] in previous:
                            cursor.execute("INSERT INTO source_config (name,kind,file_path,dataset_id,etl_batch_date) "
                                           "VALUES (%s,%s,%s,%s,%s)", previous[row[0]])
                cursor.execute("SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name")
                registry_restored = list(cursor.fetchall())
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        assert _digest(registry_restored) == _digest(registry_before), "SOURCE_REGISTRY_CHANGED"


def test_mysql_x6_six_roundtrip_range_idempotency_and_isolation(mysql_x6):
    infos, (dataset, other) = mysql_x6
    assert ods.run_extract_all(None, None, source_infos=infos) == dict.fromkeys(sources.SOURCES, 3)
    for name in sources.SOURCES:
        assert _digest(_fetch(name, dataset)) == _digest(_independent_expected(name, infos[name])), "ROUNDTRIP_MISMATCH"
    before = {name: _fetch(name, dataset, metadata=True) for name in sources.SOURCES}
    for _ in range(2):
        assert ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=infos) == dict.fromkeys(sources.SOURCES, 1)
        for name in sources.SOURCES:
            after = _fetch(name, dataset, metadata=True)
            assert _digest([row for row in after if row[DATES[name]] != "2025-01-02"]) == _digest(
                [row for row in before[name] if row[DATES[name]] != "2025-01-02"]), "OUTSIDE_CHANGED"
            assert _digest(_fetch(name, dataset)) == _digest(_independent_expected(name, infos[name])), "IDEMPOTENCY_MISMATCH"
    name, table = "lis_report", sources.SOURCES["lis_report"]["table"]
    ods.load_to_ods(records(name, other), table)
    other_before = _fetch(name, other, metadata=True)
    other_table_before = _fetch("emr_record", dataset, metadata=True)
    rows = _independent_expected(name, infos[name])
    rows[1]["result_text"] = "SIM_NEW ' \\ 001.2300"
    rows = [dict(row, etl_batch_date="2026-01-01") for row in rows]
    write_csv(infos[name], name, rows)
    frame = pd.DataFrame(rows)
    assert ods._load_range_to_ods(frame, table, "2025-01-02", "2025-01-02") == 1
    changed = _fetch(name, dataset)
    assert changed[1]["etl_batch_date"] == "2026-01-01"
    assert changed[0]["etl_batch_date"] == changed[2]["etl_batch_date"] == BATCH
    assert _digest(_fetch(name, other, metadata=True)) == _digest(other_before), "OTHER_DATASET_CHANGED"
    assert _digest(_fetch("emr_record", dataset, metadata=True)) == _digest(other_table_before), "OTHER_TABLE_CHANGED"


def test_mysql_x6_deletion_empty_rebase_and_header_only(mysql_x6):
    infos, (dataset, _) = mysql_x6
    ods.run_extract_all(None, None, source_infos=infos)
    name = "lis_report"
    original = _fetch(name, dataset, metadata=True)
    rows = _independent_expected(name, infos[name])[:2]
    write_csv(infos[name], name, rows)
    result = ods.run_extract_all("2025-01-03", "2025-01-03", source_infos=infos)
    assert result[name] == 0
    assert _digest(_fetch(name, dataset, metadata=True)) == _digest(original[:2]), "TAIL_DELETE_CHANGED_OUTSIDE"
    no_op = _fetch(name, dataset, metadata=True)
    assert ods.run_extract_all("2024-01-01", "2024-01-01", source_infos=infos) == dict.fromkeys(sources.SOURCES, 0)
    assert _digest(_fetch(name, dataset, metadata=True)) == _digest(no_op), "EMPTY_NOOP_CHANGED_DATA"
    # Remove middle source row: later outside row is renumbered, requiring explicit rebase.
    write_csv(infos[name], name, records(name, dataset))
    ods.run_extract_all(None, None, source_infos=infos)
    full = _fetch(name, dataset, metadata=True)
    shifted = records(name, dataset)[::2]
    write_csv(infos[name], name, shifted)
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_REBASE_REQUIRED$"):
        ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=infos)
    assert _digest(_fetch(name, dataset, metadata=True)) == _digest(full), "REBASE_FAILURE_CHANGED_SOURCE"
    before_all = {name: _fetch(name, dataset, metadata=True) for name in sources.SOURCES}
    write_csv(infos[name], name, [])
    with pytest.raises(sources.OdsError, match="^ODS_SNAPSHOT_EMPTY$"):
        ods.run_extract_all(None, None, source_infos=infos)
    assert _digest({name: _fetch(name, dataset, metadata=True) for name in sources.SOURCES}) == _digest(before_all), "EMPTY_SOURCE_WROTE_DATA"


class _InsertFaultConnection:
    """Wrap a real transaction; fail INSERT after DELETE without exposing driver details."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, key):
        return getattr(self.connection, key)

    def cursor(self):
        return _InsertFaultCursor(self.connection.cursor())


class _InsertFaultCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self.cursor.__exit__(*args)

    def __getattr__(self, key):
        return getattr(self.cursor, key)

    def executemany(self, statement, values):
        raise RuntimeError("SIM_SECRET_INSERT_FAILURE")


def test_mysql_x6_rollback_after_delete_verified_by_separate_connection(mysql_x6, monkeypatch):
    infos, (dataset, _) = mysql_x6
    ods.run_extract_all(None, None, source_infos=infos)
    table = sources.SOURCES["lis_report"]["table"]
    before = _fetch("lis_report", dataset, metadata=True)
    monkeypatch.setattr(ods, "connect_database", lambda: _InsertFaultConnection(sources.connect_database(test_only=True)))
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_LOAD_FAILED$"):
        ods._load_range_to_ods(pd.DataFrame(_independent_expected("lis_report", infos["lis_report"])),
                               table, "2025-01-02", "2025-01-02")
    assert _digest(_fetch("lis_report", dataset, metadata=True)) == _digest(before), "DELETE_ROLLBACK_FAILED"


def test_mysql_x6_third_source_failure_prefix_and_retry(mysql_x6, monkeypatch):
    infos, (dataset, _) = mysql_x6
    ods.run_extract_all(None, None, source_infos=infos)
    before = {name: _fetch(name, dataset, metadata=True) for name in sources.SOURCES}
    real_load = ods._load_range_to_ods
    events, calls = [], []

    def failing_load(frame, table, start, end):
        calls.append(table)
        if table == sources.SOURCES["his_charge"]["table"]:
            raise sources.OdsError("ODS_RANGE_LOAD_FAILED")
        return real_load(frame, table, start, end)

    monkeypatch.setattr(ods, "_load_range_to_ods", failing_load)
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_LOAD_FAILED$"):
        ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=infos,
                            on_event=lambda *event: events.append(event))
    assert len(calls) == 3
    assert [event[0] for event in events if event[1] == "SUCCESS"] == list(sources.SOURCES)[:2]
    for name in list(sources.SOURCES)[:2]:
        assert _digest(_fetch(name, dataset, metadata=True)) != _digest(before[name]), "PREFIX_NOT_COMMITTED"
    for name in list(sources.SOURCES)[2:]:
        assert _digest(_fetch(name, dataset, metadata=True)) == _digest(before[name]), "POST_FAILURE_SOURCE_CHANGED"
    monkeypatch.setattr(ods, "_load_range_to_ods", real_load)
    assert ods.run_extract_all("2025-01-02", "2025-01-02", source_infos=infos) == dict.fromkeys(sources.SOURCES, 1)
    for name in sources.SOURCES:
        assert _digest(_fetch(name, dataset)) == _digest(_independent_expected(name, infos[name])), "RETRY_MISMATCH"


@pytest.mark.parametrize("problem", ["no_baseline", "old_bad_date", "cross_date_move"])
def test_mysql_x6_guard_refuses_unproven_outside(mysql_x6, problem):
    infos, (dataset, _) = mysql_x6
    name, table = "lis_report", sources.SOURCES["lis_report"]["table"]
    rows = records(name, dataset)
    if problem != "no_baseline":
        baseline = [dict(row) for row in rows]
        if problem == "old_bad_date":
            baseline[0]["report_date"] = "invalid"
        ods.load_to_ods(baseline, table)
    before = _fetch(name, dataset, metadata=True)
    if problem == "cross_date_move":
        rows[0]["report_date"] = "2025-01-02"
    with pytest.raises(sources.OdsError, match="^ODS_RANGE_REBASE_REQUIRED$"):
        ods._load_range_to_ods(rows, table, "2025-01-02", "2025-01-02")
    assert _digest(_fetch(name, dataset, metadata=True)) == _digest(before), "GUARD_WROTE_DATA"
