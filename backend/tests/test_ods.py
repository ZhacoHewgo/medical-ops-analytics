"""O5: isolated SIM fixtures; real MySQL requires explicit hospital_test URL."""

import csv
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock

import pytest

from etl.config import source_config as sources
from etl.jobs import extract_ods as ods


ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "data/generated/synthetic-v1_seed42_2025-01-01_12m"
BATCH = "2025-12-31"


def records(name, dataset="ods_unit", count=3):
    fields = sources.SOURCES[name]["columns"]
    result = []
    for number in range(1, count + 1):
        row = {field: "SIM_重复 ' \\ 文本 " for field in fields}
        row[fields[-1]] = ["", "not-a-date-or-amount", "12.3400"][number % 3]
        row.update(dataset_id=dataset, source_row_number=number, etl_batch_date=BATCH)
        result.append(row)
    return result


@pytest.fixture
def sim_dataset():
    directory = Path(tempfile.mkdtemp(prefix="ods_test_", dir=ROOT / "data/generated"))
    for name, spec in sources.SOURCES.items():
        path = directory / spec["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=spec["columns"])
            writer.writeheader()
            writer.writerows({key: row[key] for key in spec["columns"]}
                             for row in records(name))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def config(directory, name):
    return dict(kind="csv", file_path=str(directory / sources.SOURCES[name]["path"]),
                dataset_id=directory.name, etl_batch_date=BATCH)


def test_six_sources_and_finance_csv_only():
    assert len(sources.SOURCES) == 6
    assert len({spec["table"] for spec in sources.SOURCES.values()}) == 6
    assert sources.SOURCES["finance_excel"]["path"] == "files/finance_excel.csv"
    assert all(spec["path"].endswith(".csv") for spec in sources.SOURCES.values())


@pytest.mark.parametrize("name", ["unknown", "his_charge;DROP TABLE source_config", "finance_excel.xlsx"])
def test_source_injection_rejected(name):
    with pytest.raises(sources.OdsError, match="^ODS_SOURCE_INVALID$"):
        sources.validate_source(name, {})


@pytest.mark.parametrize("change", [
    {"password": "SIM_SECRET"}, {"kind": "mysql"}, {"dataset_id": "../escape"},
    {"etl_batch_date": "2025-02-30"}, {"etl_batch_date": "2025-1-01"},
    {"file_path": "mysql://SIM_PRIVATE"}, {"dataset_id": 42},
])
def test_registration_invalid_config(sim_dataset, change):
    info = config(sim_dataset, "his_outpatient")
    info.update(change)
    with pytest.raises(sources.OdsError):
        sources.validate_source("his_outpatient", info)


def test_path_boundaries(sim_dataset):
    name = "his_outpatient"
    info = config(sim_dataset, name)
    for path in [ROOT / "data/mock_his/his_outpatient.csv",
                 sim_dataset / "mock_his/../mock_his/his_outpatient.csv",
                 sim_dataset / "files/finance_excel.xlsx"]:
        with pytest.raises(sources.OdsError, match="^ODS_PATH_INVALID$"):
            sources.validate_source(name, {**info, "file_path": str(path)})
    target = Path(info["file_path"])
    target.unlink()
    target.symlink_to(DEFAULT / sources.SOURCES[name]["path"])
    with pytest.raises(sources.OdsError, match="^ODS_PATH_INVALID$"):
        sources.read_source(name, info)


def test_course_registration_uses_only_canonical_course_files(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sources, "GENERATED_ROOT", tmp_path / "data/generated")
    name = "his_outpatient"
    path = tmp_path / "data" / sources.SOURCES[name]["path"]
    info = dict(kind="csv", file_path=str(path), dataset_id="course-platform-v1",
                etl_batch_date="2026-11-21")
    assert sources.validate_source(name, info)["file_path"] == str(path)
    with pytest.raises(sources.OdsError, match="^ODS_PATH_INVALID$"):
        sources.validate_source(name, {**info, "file_path": str(path.with_name("other.csv"))})
    path.parent.mkdir(parents=True)
    path.symlink_to(path.with_name("other.csv"))
    with pytest.raises(sources.OdsError, match="^ODS_PATH_INVALID$"):
        sources.validate_source(name, info)


@pytest.mark.parametrize("name", list(sources.SOURCES))
def test_csv_and_snapshot_preserve_strings(sim_dataset, name):
    rows = sources.read_source(name, config(sim_dataset, name))
    assert rows == records(name, sim_dataset.name)
    assert ods.validate_snapshot(rows, sources.SOURCES[name]["table"]) == rows


@pytest.mark.parametrize("problem", ["missing_header", "extra_header", "duplicate_header",
                                         "short_row", "long_row", "encoding", "quoted", "empty"])
def test_bad_csv_rejected(sim_dataset, problem):
    name = "lis_report"
    info = config(sim_dataset, name)
    path = Path(info["file_path"])
    columns = list(sources.SOURCES[name]["columns"])
    if problem == "encoding":
        path.write_bytes(b"\xff\xff")
    elif problem == "quoted":
        path.write_text(",".join(columns) + '\n"unterminated', encoding="utf-8")
    elif problem == "empty":
        path.write_text(",".join(columns) + "\n", encoding="utf-8")
    else:
        header = columns[:-1] if problem == "missing_header" else columns[:]
        if problem == "extra_header":
            header += ["extra"]
        if problem == "duplicate_header":
            header[-1] = header[0]
        row = ["SIM"] * (len(header) + (1 if problem == "long_row" else
                                       -1 if problem == "short_row" else 0))
        with path.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows([header, row])
    with pytest.raises(sources.OdsError):
        sources.read_source(name, info)


@pytest.mark.parametrize("problem", ["empty", "missing", "extra", "id", "load_time", "mixed_dataset",
    "mixed_batch", "bad_date", "duplicate_number", "gap", "bool_number", "nan", "none", "oversize"])
def test_invalid_snapshot_rejected_before_connection(monkeypatch, problem):
    rows = records("his_outpatient")
    first = rows[0]
    if problem == "empty": rows = []
    elif problem == "missing": first.pop("phone")
    elif problem in ("extra", "id", "load_time"):
        first[{"extra": "extra", "id": "id", "load_time": "etl_load_time"}[problem]] = "SIM"
    elif problem == "mixed_dataset": first["dataset_id"] = "other"
    elif problem == "mixed_batch": first["etl_batch_date"] = "2025-12-30"
    elif problem == "bad_date": first["etl_batch_date"] = "2025-02-30"
    elif problem == "duplicate_number": first["source_row_number"] = 2
    elif problem == "gap": first["source_row_number"] = 4
    elif problem == "bool_number": first["source_row_number"] = True
    elif problem == "nan": first["phone"] = float("nan")
    elif problem == "none": first["phone"] = None
    elif problem == "oversize": first["phone"] = "汉" * 21846
    connector = MagicMock()
    monkeypatch.setattr(ods, "connect_database", connector)
    with pytest.raises(sources.OdsError):
        ods.load_to_ods(rows, sources.SOURCES["his_outpatient"]["table"])
    connector.assert_not_called()


def test_unknown_table_and_dataframe_adapter():
    with pytest.raises(sources.OdsError, match="^ODS_TABLE_INVALID$"):
        ods.validate_snapshot(records("lis_report"), "ods_lis_report; SELECT 1")
    frame = MagicMock()
    frame.to_dict.return_value = records("lis_report")
    assert ods.validate_snapshot(frame, "ods_lis_report") == records("lis_report")
    frame.to_dict.assert_called_once_with(orient="records")


def test_snapshot_shape_error_counts_fields_without_leaking_input(monkeypatch):
    row = records("lis_report")[0]
    row.pop("dataset_id")
    row.pop("source_row_number")
    row.pop("result_text")
    row["SIM_PRIVATE_UNKNOWN_COLUMN"] = "SIM_PRIVATE_VALUE"
    connector = MagicMock()
    monkeypatch.setattr(ods, "connect_database", connector)
    with pytest.raises(sources.OdsError) as error:
        ods.load_to_ods([row], "ods_lis_report")
    assert error.value.code == "ODS_RECORD_FIELDS_INVALID"
    assert "metadata_missing=dataset_id,source_row_number" in str(error.value)
    assert "business_missing=1 unexpected=1 duplicate=0" in str(error.value)
    assert "table=ods_lis_report business_positions=5 known_extra=-" in str(error.value)
    for name in ("SIM_PRIVATE_UNKNOWN_COLUMN", "SIM_PRIVATE_VALUE", "result_text"):
        assert name not in str(error.value)
    connector.assert_not_called()


def test_dataframe_rejects_duplicate_columns_before_record_conversion(monkeypatch):
    frame = ods.pd.DataFrame(records("lis_report"))
    frame = ods.pd.concat([frame, frame[["result_text"]]], axis=1)
    connector = MagicMock()
    monkeypatch.setattr(ods, "connect_database", connector)
    with pytest.raises(sources.OdsError) as error:
        ods.load_to_ods(frame, "ods_lis_report")
    assert error.value.code == "ODS_RECORD_FIELDS_INVALID"
    assert "duplicate=1" in str(error.value)
    connector.assert_not_called()


def test_snapshot_accepts_complete_out_of_order_sequence():
    rows = records("lis_report")
    assert ods.validate_snapshot(rows[::-1], "ods_lis_report") == rows


@pytest.mark.parametrize("problem", [None, "storage", "column_type", "missing_table", "unique", "registry",
                                         "business_names", "registry_fields", "registry_pk"])
def test_schema_gate_rejects_incompatible_existing_tables(monkeypatch, problem):
    statuses = [[table, "OK", "OK", "OK", "OK", "OK"] for table in ods.TABLES]
    indexes = []
    for table in ods.TABLES:
        indexes.extend([(table, "PRIMARY", 0, "id"),
                        (table, "uq_dataset_row", 0, "dataset_id,source_row_number")])
    registry = [["source_config", "InnoDB", "utf8mb4_bin", "OK", "OK"]]
    if problem == "storage": statuses[0][1] = "FAIL"
    if problem == "column_type": statuses[0][3] = "FAIL"
    if problem == "missing_table": statuses.pop()
    if problem == "unique": indexes.pop()
    if problem == "registry": registry = []
    if problem == "business_names": statuses[0][5] = "FAIL"
    if problem == "registry_fields": registry[0][3] = "FAIL"
    if problem == "registry_pk": registry[0][4] = "FAIL"
    cursor = MagicMock()
    cursor.fetchall.side_effect = [statuses, indexes, registry]
    context = MagicMock()
    context.__enter__.return_value = cursor
    monkeypatch.setattr(ods, "database_cursor", lambda code: context)
    if problem:
        with pytest.raises(sources.OdsError, match="^ODS_SCHEMA_INVALID$"):
            ods.verify_schema()
    else:
        ods.verify_schema()


def test_driver_failure_has_no_original_message(monkeypatch):
    import sys
    import traceback

    driver = MagicMock()
    driver.connect.side_effect = RuntimeError("SIM_PRIVATE_DRIVER_TEXT")
    monkeypatch.setitem(sys.modules, "pymysql", driver)
    monkeypatch.setattr(sources, "database_options", lambda **kwargs: {})
    with pytest.raises(sources.OdsError, match="^ODS_DATABASE_CONNECT_FAILED$") as caught:
        sources.connect_database()
    assert "SIM_PRIVATE_DRIVER_TEXT" not in "".join(traceback.format_exception(caught.value))


def test_registry_parameterization_and_safe_listing(sim_dataset, monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(sources, "connect_database", lambda: connection)
    info = config(sim_dataset, "lis_report")
    sources.register_source("lis_report", info)
    sql, values = cursor.execute.call_args.args
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert str(sim_dataset) not in sql
    assert values == ("lis_report", *info.values(), *info.values())
    connection.commit.assert_called_once()
    connection.close.assert_called_once()
    cursor.fetchall.return_value = [(name, *config(sim_dataset, name).values()) for name in sources.SOURCES]
    listed = sources.list_sources()
    assert len(listed) == 6
    assert all(set(item) == {"name", *sources.CONFIG_FIELDS} for item in listed)


def test_cli_six_counts_with_mock_database_only(monkeypatch, capsys):
    registered = []
    monkeypatch.setattr(ods, "initialize_schema", lambda: None)
    monkeypatch.setattr(ods, "register_source", lambda name, info: registered.append(name))
    monkeypatch.setattr(ods, "test_connection", lambda name: True)
    monkeypatch.setattr(ods, "load_to_ods", lambda rows, table: len(rows))
    assert ods.main(["--dataset-id", DEFAULT.name, "--batch-date", BATCH]) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert registered == list(sources.SOURCES)
    assert [item["count"] for item in output] == [121, 121, 727, 242, 242, 121]
    assert all(set(item) == {"source", "count", "code"} and item["code"] == "ODS_OK" for item in output)


@pytest.mark.parametrize("url", ["", "postgres://SIM", "mysql://SIM", "mysql://SIM@127.0.0.1:3306/hospital",
    "mysql://SIM@remote.invalid:13306/hospital", "mysql://SIM@127.0.0.1:13306/hospital_prod",
    "mysql://SIM@127.0.0.1:13306/hospital?password=SIM_SECRET",
    "mysql://SIM@127.0.0.1:13306/hospital#SIM_SECRET"])
def test_database_url_rejection_safe(monkeypatch, url):
    monkeypatch.setenv("ODS_DATABASE_URL", url)
    with pytest.raises(sources.OdsError) as caught:
        sources.database_options()
    assert str(caught.value) in {"ODS_DATABASE_URL_MISSING", "ODS_DATABASE_URL_INVALID"}


def test_test_url_gate_requires_exact_test_database(monkeypatch):
    monkeypatch.delenv("ODS_TEST_DATABASE_URL", raising=False)
    with pytest.raises(sources.OdsError, match="^ODS_DATABASE_URL_MISSING$"):
        sources.database_options(test_only=True)
    # Explicitly synthetic credentials, never a working connection string.
    monkeypatch.setenv("ODS_TEST_DATABASE_URL", "mysql://SIM:SIM@127.0.0.1:13306/hospital")
    with pytest.raises(sources.OdsError, match="^ODS_TEST_DATABASE_REQUIRED$"):
        sources.database_options(test_only=True)


@pytest.fixture
def private_ods_config(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("ODS_DATABASE_URL", raising=False)
    monkeypatch.delenv("ODS_TEST_DATABASE_URL", raising=False)
    path = tmp_path / ".runtime" / "ods-connection.json"
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps({"ODS_DATABASE_URL": "mysql://SIM:SIM@127.0.0.1:13306/hospital"}))
    path.chmod(0o600)
    return path


def test_database_options_reads_private_file_and_prefers_environment(private_ods_config, monkeypatch):
    assert sources.database_options()["user"] == "SIM"
    monkeypatch.setenv("ODS_DATABASE_URL", "mysql://ENV:SIM@127.0.0.1:13306/hospital")
    assert sources.database_options()["user"] == "ENV"
    monkeypatch.setenv("ODS_DATABASE_URL", "invalid")
    with pytest.raises(sources.OdsError, match="^ODS_DATABASE_URL_INVALID$"):
        sources.database_options()


@pytest.mark.parametrize("invalid", ["mode", "link", "json", "host"])
def test_database_options_rejects_invalid_private_file(private_ods_config, invalid):
    path = private_ods_config
    if invalid == "mode":
        path.chmod(0o644)
    elif invalid == "link":
        original = path.rename(path.with_name("original.json"))
        path.symlink_to(original)
    elif invalid == "json":
        path.write_text("{SIM_PRIVATE")
    else:
        path.write_text(json.dumps({"ODS_DATABASE_URL": "mysql://SIM:SIM@elsewhere.invalid:13306/hospital"}))
    with pytest.raises(sources.OdsError, match="^ODS_DATABASE_URL_INVALID$"):
        sources.database_options()


def test_database_options_private_file_cannot_supply_test_url(private_ods_config):
    with pytest.raises(sources.OdsError, match="^ODS_DATABASE_URL_MISSING$"):
        sources.database_options(test_only=True)


@pytest.mark.parametrize("failure", [None, "insert", "count"])
def test_transaction_commit_rollback_and_close(monkeypatch, failure):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (0 if failure == "count" else 3,)
    if failure == "insert":
        cursor.executemany.side_effect = RuntimeError("SIM_PRIVATE_URL_AND_ROW")
    monkeypatch.setattr(ods, "connect_database", lambda: connection)
    rows = records("lis_report")
    if failure:
        with pytest.raises(sources.OdsError, match="^ODS_LOAD_FAILED$"):
            ods.load_to_ods(rows, "ods_lis_report")
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
    else:
        assert ods.load_to_ods(rows, "ods_lis_report") == 3
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
    connection.begin.assert_called_once()
    connection.close.assert_called_once()
    delete = cursor.execute.call_args_list[0]
    assert delete.args == ("DELETE FROM `ods_lis_report` WHERE dataset_id=%s", ("ods_unit",))


def test_connection_false_with_safe_reason(monkeypatch, caplog):
    monkeypatch.setattr(sources, "get_source", lambda name: (_ for _ in ()).throw(
        sources.OdsError("ODS_SOURCE_NOT_REGISTERED")))
    assert sources.test_connection("lis_report") is False
    assert "ODS_SOURCE_NOT_REGISTERED" in caplog.text


def test_cli_errors_do_not_echo_arguments(capsys):
    assert ods.main(["--dataset-id", "SIM_PRIVATE", "--batch-date", "SIM_PRIVATE"]) == 1
    assert ods.main(["--unknown=SIM_PRIVATE"]) == 1
    output = capsys.readouterr()
    assert "SIM_PRIVATE" not in output.out + output.err
    assert "ODS_" in output.out


def test_default_sim_manifest_hashes_and_counts():
    manifest = json.loads((DEFAULT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    total = 0
    for name, spec in sources.SOURCES.items():
        entry = next(item for item in manifest["files"] if item["path"] == spec["path"])
        assert hashlib.sha256((DEFAULT / spec["path"]).read_bytes()).hexdigest() == entry["sha256"]
        rows = sources.read_source(name, config(DEFAULT, name))
        assert len(rows) == entry["final_rows"]
        total += len(rows)
    assert total == 1574


@pytest.fixture
def mysql(monkeypatch, sim_dataset):
    if not os.environ.get("ODS_TEST_DATABASE_URL"):
        pytest.skip("ODS_TEST_DATABASE_URL_NOT_INJECTED: real MySQL NOT RUN")
    # Fail closed before any connect/DDL/write. Invalid or production URL is a failure, not skip.
    sources.database_options(test_only=True)
    monkeypatch.setenv("ODS_DATABASE_URL", os.environ["ODS_TEST_DATABASE_URL"])
    ods.initialize_schema()
    previous = {item["name"]: item for item in sources.list_sources()}
    datasets = [sim_dataset.name, sim_dataset.name + "_other"]
    try:
        yield sim_dataset, datasets
    finally:
        connection = sources.connect_database(test_only=True)
        try:
            with connection.cursor() as cursor:
                for spec in sources.SOURCES.values():
                    for dataset in datasets:
                        cursor.execute(f"DELETE FROM `{spec['table']}` WHERE dataset_id=%s", (dataset,))
                for name in sources.SOURCES:
                    removed = 0
                    for dataset in datasets:
                        removed += cursor.execute("DELETE FROM source_config WHERE name=%s AND dataset_id=%s",
                                                  (name, dataset))
                    old = previous.get(name)
                    if old and removed:
                        cursor.execute(
                            "INSERT INTO source_config (name,kind,file_path,dataset_id,etl_batch_date) "
                            "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                            "kind=%s,file_path=%s,dataset_id=%s,etl_batch_date=%s",
                            (name, old["kind"], old["file_path"], old["dataset_id"], old["etl_batch_date"],
                             old["kind"], old["file_path"], old["dataset_id"], old["etl_batch_date"]))
            connection.commit()
        finally:
            connection.close()


def fetch(table, dataset, metadata=False):
    spec = next(spec for spec in sources.SOURCES.values() if spec["table"] == table)
    columns = list(spec["columns"]) + ["dataset_id", "source_row_number", "etl_batch_date"]
    if metadata:
        columns += ["id", "etl_load_time"]
    connection = sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {','.join('`' + col + '`' for col in columns)} FROM `{table}` "
                           "WHERE dataset_id=%s ORDER BY source_row_number", (dataset,))
            return [dict(zip(columns, values)) for values in cursor.fetchall()]
    finally:
        connection.close()


def digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                    default=str).encode()).hexdigest()


def test_mysql_six_schema_and_full_sim_roundtrip(mysql):
    directory, datasets = mysql
    connection = sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT TABLE_NAME,ENGINE,TABLE_COLLATION FROM information_schema.TABLES "
                           "WHERE TABLE_SCHEMA=DATABASE()")
            tables = {row[0]: row[1:] for row in cursor.fetchall()}
            for spec in sources.SOURCES.values():
                assert tables[spec["table"]] == ("InnoDB", "utf8mb4_bin")
                cursor.execute("SELECT COLUMN_NAME,DATA_TYPE,COLLATION_NAME,IS_NULLABLE,EXTRA "
                               "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                               "AND TABLE_NAME=%s", (spec["table"],))
                columns = {row[0]: row[1:] for row in cursor.fetchall()}
                for column in spec["columns"]:
                    assert columns[column][:2] == ("text", "utf8mb4_bin")
                for column in ("dataset_id", "source_row_number", "etl_batch_date", "etl_load_time"):
                    assert columns[column][2] == "NO"
                assert "auto_increment" in columns["id"][3]
                cursor.execute("SELECT INDEX_NAME,COLUMN_NAME,SEQ_IN_INDEX FROM information_schema.STATISTICS "
                               "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND NON_UNIQUE=0 "
                               "ORDER BY INDEX_NAME,SEQ_IN_INDEX", (spec["table"],))
                indexes = {}
                for index, column, _ in cursor.fetchall():
                    indexes.setdefault(index, []).append(column)
                assert sorted(indexes.values()) == sorted([["id"], ["dataset_id", "source_row_number"]])
    finally:
        connection.close()
    total = 0
    for name, spec in sources.SOURCES.items():
        rows = sources.read_source(name, config(DEFAULT, name))
        for row in rows:
            row["dataset_id"] = datasets[0]
        assert ods.load_to_ods(rows, spec["table"]) == len(rows)
        assert digest(fetch(spec["table"], datasets[0])) == digest(rows)
        total += len(rows)
    assert total == 1574


def test_finance_summary_rejects_invalid_input_before_connect(monkeypatch):
    connector = MagicMock()
    monkeypatch.setattr(ods, "connect_database", connector)
    sample = {"dept": "SIM_DEPT_01", "amount": 12.25, "etl_batch_date": BATCH}
    for invalid in (
        [sample],
        ods.pd.DataFrame([{**sample, "amount": float("nan")}]),
        ods.pd.DataFrame([sample, {**sample, "etl_batch_date": "2025-2-30"}]),
        ods.pd.DataFrame([{**sample, "unknown": "SIM"}]),
    ):
        with pytest.raises(sources.OdsError):
            ods.load_to_ods(invalid, "ods_finance_excel")
    connector.assert_not_called()


def test_mysql_finance_dataframe_reloads_only_its_batch(mysql):
    directory, datasets = mysql
    manual_dataset = datasets[0]
    table = "ods_finance_excel"
    batch, earlier = "2099-10-02", "2099-10-01"
    projection = f"ods-finance-summary-v1.{batch}"
    other_projection = f"ods-finance-summary-v1.{earlier}"
    assert not fetch(table, projection) and not fetch(table, other_projection)
    datasets.extend((projection, other_projection))
    manual = records("finance_excel", manual_dataset)
    assert ods.load_to_ods(manual, table) == 3
    before = fetch(table, manual_dataset, True)
    frame = ods.pd.DataFrame([
        {"dept": "SIM_DEPT_01", "amount": Decimal("42.50"), "etl_batch_date": ods.pd.Timestamp(batch)},
        {"dept": "SIM_DEPT_01", "amount": -3, "etl_batch_date": ods.pd.Timestamp(batch)},
    ])
    untouched_frame = frame.copy(deep=True)
    assert ods.load_to_ods(frame.assign(etl_batch_date=earlier), table) == 2
    earlier_rows = fetch(table, other_projection, True)
    assert ods.load_to_ods(frame, table) == 2
    expected = [
        {"record_id": "", "stat_date": "", "dept_code": "SIM_DEPT_01", "revenue": amount,
         "drug_revenue": "", "consumable_revenue": "", "dataset_id": projection,
         "source_row_number": index, "etl_batch_date": date.fromisoformat(batch)}
        for index, amount in enumerate(("42.50", "-3"), 1)
    ]
    assert fetch(table, projection) == expected
    assert ods.load_to_ods(frame, table) == 2
    assert fetch(table, projection) == expected
    assert ods.load_to_ods(frame.iloc[:1], table) == 1
    assert fetch(table, projection) == expected[:1]
    assert fetch(table, other_projection, True) == earlier_rows
    assert fetch(table, manual_dataset, True) == before
    mixed = ods.pd.concat([frame, frame.iloc[1:].assign(etl_batch_date=earlier)], ignore_index=True)
    assert ods.load_to_ods(mixed, table) == 3
    assert fetch(table, projection) == expected
    assert fetch(table, other_projection) == [
        {**expected[1], "dataset_id": other_projection, "source_row_number": 1, "etl_batch_date": date.fromisoformat(earlier)}
    ]
    assert ods.load_to_ods(mixed, table) == 3
    assert fetch(table, projection) == expected
    assert fetch(table, manual_dataset, True) == before
    ods.pd.testing.assert_frame_equal(frame, untouched_frame)
    with pytest.raises(sources.OdsError, match="ODS_DATASET_RESERVED"):
        ods.load_to_ods(records("finance_excel", projection), table)
    with pytest.raises(sources.OdsError, match="ODS_DATASET_RESERVED"):
        sources.validate_source("finance_excel", {**config(directory, "finance_excel"), "dataset_id": projection})


def test_mysql_repeat_shrink_isolation_and_rollback(mysql, monkeypatch):
    directory, datasets = mysql
    dataset, other = datasets
    case_dataset = dataset.upper()
    datasets.append(case_dataset)
    table, other_table = "ods_lis_report", "ods_emr_record"
    original = records("lis_report", dataset)
    original[0]["report_id"] = original[1]["report_id"] = "SIM_DUPLICATE"
    ods.load_to_ods(original, table)
    ods.load_to_ods(records("lis_report", other), table)
    ods.load_to_ods(records("emr_record", dataset), other_table)
    ods.load_to_ods(records("lis_report", case_dataset), table)
    untouched = digest(fetch(table, other, True)), digest(fetch(other_table, dataset, True))
    case_before = digest(fetch(table, case_dataset, True))
    before = digest(fetch(table, dataset))
    ods.load_to_ods(original, table)
    assert digest(fetch(table, dataset)) == before
    ods.load_to_ods(original[:1], table)
    assert len(fetch(table, dataset)) == 1
    assert untouched == (digest(fetch(table, other, True)), digest(fetch(other_table, dataset, True)))
    assert digest(fetch(table, case_dataset, True)) == case_before
    before_failure = digest(fetch(table, dataset, True))
    real_connect = ods.connect_database

    class FailingCursor:
        def __init__(self, cursor): self.cursor = cursor
        def __enter__(self): return self
        def __exit__(self, *args): self.cursor.close()
        def execute(self, *args): return self.cursor.execute(*args)
        def fetchone(self): return self.cursor.fetchone()
        def executemany(self, sql, values):
            self.cursor.execute(sql, values[0])
            raise RuntimeError("SIM_INSERT_FAILURE_PRIVATE")

    class FailingConnection:
        def __init__(self): self.connection = real_connect()
        def __getattr__(self, name): return getattr(self.connection, name)
        def cursor(self): return FailingCursor(self.connection.cursor())

    monkeypatch.setattr(ods, "connect_database", FailingConnection)
    with pytest.raises(sources.OdsError, match="^ODS_LOAD_FAILED$"):
        ods.load_to_ods(original, table)
    # fetch opens another real connection: prove database rollback, not mock calls.
    assert digest(fetch(table, dataset, True)) == before_failure
    assert untouched == (digest(fetch(table, other, True)), digest(fetch(other_table, dataset, True)))
    assert digest(fetch(table, case_dataset, True)) == case_before


def test_mysql_registration_persistence_and_bad_file(mysql):
    directory, datasets = mysql
    for name in sources.SOURCES:
        info = config(directory, name)
        sources.register_source(name, info)
        sources.register_source(name, info)
        assert sources.test_connection(name) is True
    listed = sources.list_sources()
    assert len(listed) == 6
    assert {item["name"] for item in listed} == set(sources.SOURCES)
    assert all(set(item) == {"name", "kind", "file_path", "dataset_id", "etl_batch_date"}
               for item in listed)
    assert sources.get_source("lis_report")["dataset_id"] == directory.name
    path = directory / sources.SOURCES["lis_report"]["path"]
    path.write_bytes(b"\xff")
    assert sources.test_connection("lis_report") is False
    path.unlink()
    assert sources.test_connection("lis_report") is False
    table = sources.SOURCES["lis_report"]["table"]
    ods.load_to_ods(records("lis_report", directory.name), table)
    before = digest(fetch(table, directory.name, True))
    replacement = directory.with_name(datasets[1])
    replacement.mkdir()
    try:
        new_path = replacement / sources.SOURCES["lis_report"]["path"]
        new_path.parent.mkdir()
        shutil.copyfile(DEFAULT / sources.SOURCES["lis_report"]["path"], new_path)
        sources.register_source("lis_report", config(replacement, "lis_report"))
        assert sources.get_source("lis_report")["dataset_id"] == replacement.name
        assert sources.test_connection("lis_report") is True
        assert digest(fetch(table, directory.name, True)) == before
    finally:
        shutil.rmtree(replacement)


@pytest.mark.parametrize("table,forward,restore", [
    (spec["table"], f"RENAME COLUMN `{column}` TO `s1_wrong_column`",
     f"RENAME COLUMN `s1_wrong_column` TO `{column}`")
    for name, spec in sources.SOURCES.items()
    for column in [("result_text" if name == "lis_report" else spec["columns"][-1])]
] + [
    ("source_config", "RENAME COLUMN `file_path` TO `s1_wrong_column`",
     "RENAME COLUMN `s1_wrong_column` TO `file_path`"),
    ("source_config", "MODIFY COLUMN `kind` VARCHAR(9) NOT NULL",
     "MODIFY COLUMN `kind` VARCHAR(8) NOT NULL"),
    ("source_config", "MODIFY COLUMN `file_path` TEXT NULL",
     "MODIFY COLUMN `file_path` TEXT NOT NULL"),
    ("source_config", "DROP PRIMARY KEY", "ADD PRIMARY KEY (`name`)"),
    ("source_config", "DROP PRIMARY KEY, ADD PRIMARY KEY (`name`,`kind`)",
     "DROP PRIMARY KEY, ADD PRIMARY KEY (`name`)"),
    ("source_config", "ADD COLUMN `s1_extra` TEXT NULL", "DROP COLUMN `s1_extra`"),
])
def test_mysql_schema_gate_rejects_mutation_and_restores_ddl(monkeypatch, table, forward, restore):
    if not os.environ.get("ODS_TEST_DATABASE_URL"):
        pytest.skip("ODS_TEST_DATABASE_URL_NOT_INJECTED: real MySQL NOT RUN")
    sources.database_options(test_only=True)
    monkeypatch.setenv("ODS_DATABASE_URL", os.environ["ODS_TEST_DATABASE_URL"])
    ods.initialize_schema()
    connection = sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            # Only hospital_test metadata is read; no business rows or dataset deletion.
            cursor.execute(f"SHOW CREATE TABLE `{table}`")
            original_ddl = cursor.fetchone()[1]
            changed = False
            try:
                cursor.execute(f"ALTER TABLE `{table}` {forward}")
                changed = True
                with pytest.raises(sources.OdsError, match="^ODS_SCHEMA_INVALID$"):
                    ods.verify_schema()
            finally:
                if changed:
                    cursor.execute(f"ALTER TABLE `{table}` {restore}")
                cursor.execute(f"SHOW CREATE TABLE `{table}`")
                assert digest(cursor.fetchone()[1]) == digest(original_ddl)
                ods.verify_schema()
    finally:
        connection.close()
