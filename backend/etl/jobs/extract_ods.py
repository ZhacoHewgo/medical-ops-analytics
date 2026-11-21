"""Six-file ODS snapshots and guarded date ranges; no scheduling or cleaning."""

import argparse
from collections import Counter
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from time import monotonic

import pandas as pd

from etl.config.source_config import (
    FINANCE_SUMMARY_PREFIX, GENERATED_ROOT, PROJECT_ROOT, SOURCES, OdsError, connect_database, database_cursor,
    get_source, list_sources, read_source, register_source, test_connection,
    validate_batch, validate_dataset, validate_source, validate_text,
)


TABLES = {spec["table"]: spec["columns"] for spec in SOURCES.values()}
METADATA = ("dataset_id", "source_row_number", "etl_batch_date")
SOURCE_DATES = {
    "his_outpatient": "visit_date", "his_inpatient": "admit_date",
    "his_charge": "fee_date", "emr_record": "visit_date",
    "lis_report": "report_date", "finance_excel": "stat_date",
}


def _strict_date(value, code):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise OdsError(code) from None


def _date_range(start_date, end_date):
    start = _strict_date(start_date, "ODS_RANGE_INVALID")
    end = _strict_date(end_date, "ODS_RANGE_INVALID")
    if start > end:
        raise OdsError("ODS_RANGE_INVALID")
    return start, end


def _read_frame(name, source_info):
    info = validate_source(name, get_source(name) if source_info is None else source_info)
    rows = read_source(name, info)
    rows = validate_snapshot(rows, SOURCES[name]["table"])
    frame = pd.DataFrame(rows, columns=SOURCES[name]["columns"] + METADATA)
    dates = [_strict_date(row[SOURCE_DATES[name]], "ODS_SOURCE_DATE_INVALID") for row in rows]
    return frame, dates


def _extract_range(name, start_date, end_date, source_info):
    start, end = _date_range(start_date, end_date)
    frame, dates = _read_frame(name, source_info)
    return frame.loc[[start <= day <= end for day in dates]].copy()


def extract_his_outpatient(start_date, end_date, *, source_info=None):
    return _extract_range("his_outpatient", start_date, end_date, source_info)


def extract_his_inpatient(start_date, end_date, *, source_info=None):
    return _extract_range("his_inpatient", start_date, end_date, source_info)


def extract_his_charge(start_date, end_date, *, source_info=None):
    return _extract_range("his_charge", start_date, end_date, source_info)


def extract_lis(start_date, end_date, *, source_info=None):
    return _extract_range("lis_report", start_date, end_date, source_info)


def extract_emr(start_date, end_date, *, source_info=None):
    return _extract_range("emr_record", start_date, end_date, source_info)


def extract_finance_excel(path, *, source_info=None):
    """Read approved finance CSV or its text-only XLSX counterpart, without registration."""
    info = validate_source("finance_excel", get_source("finance_excel") if source_info is None else source_info)
    try:
        target = Path(path)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        csv_path = Path(info["file_path"])
        if (".." in target.parts or any(part.is_symlink() for part in (target, *target.parents))
                or target.resolve() not in {csv_path, csv_path.with_suffix(".xlsx")}):
            raise ValueError
    except (TypeError, OSError, ValueError, RuntimeError):
        raise OdsError("ODS_PATH_INVALID") from None
    if target.suffix == ".csv":
        rows = read_source("finance_excel", info)
    else:
        rows = _read_finance_xlsx(target, info)
    rows = validate_snapshot(rows, SOURCES["finance_excel"]["table"])
    return pd.DataFrame(rows, columns=SOURCES["finance_excel"]["columns"] + METADATA)


def _read_finance_xlsx(path, info):
    from openpyxl import load_workbook

    columns = SOURCES["finance_excel"]["columns"]
    book = None
    try:
        book = load_workbook(path, read_only=True, data_only=False)
        if "finance_excel" not in book.sheetnames:
            raise OdsError("ODS_XLSX_SHEET_INVALID")
        for sheet in book:
            if sheet.title != "finance_excel" and any(
                    cell.value is not None and cell.value != "" for row in sheet for cell in row):
                raise OdsError("ODS_XLSX_SHEET_INVALID")
        sheet = book["finance_excel"]
        if sheet.max_column != len(columns):
            raise OdsError("ODS_XLSX_HEADER_INVALID")
        iterator = iter(sheet)
        header = next(iterator, ())
        if tuple(cell.value for cell in header) != columns:
            raise OdsError("ODS_XLSX_HEADER_INVALID")
        records = []
        for number, cells in enumerate(iterator, 1):
            values = []
            for cell in cells:
                value = cell.value
                if cell.data_type in {"f", "e"} or (value is not None and not isinstance(value, str)):
                    raise OdsError("ODS_XLSX_TEXT_INVALID")
                value = "" if value is None else value
                validate_text(value)
                values.append(value)
            records.append(dict(zip(columns, values), dataset_id=info["dataset_id"],
                                source_row_number=number, etl_batch_date=info["etl_batch_date"]))
        return records
    except OdsError:
        raise
    except Exception:
        raise OdsError("ODS_XLSX_READ_FAILED") from None
    finally:
        if book is not None:
            book.close()


def _freeze_sources(source_infos):
    if source_infos is None:
        items = list_sources()
        if (not isinstance(items, list) or len(items) != len(SOURCES)
                or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in items)
                or len({item["name"] for item in items}) != len(SOURCES)):
            raise OdsError("ODS_CONFIG_INVALID")
        source_infos = {item["name"]: {key: value for key, value in item.items() if key != "name"}
                        for item in items}
    if not isinstance(source_infos, dict) or set(source_infos) != set(SOURCES):
        raise OdsError("ODS_CONFIG_INVALID")
    frozen = {name: validate_source(name, source_infos[name]) for name in SOURCES}
    if (len({info["dataset_id"] for info in frozen.values()}) != 1
            or len({info["etl_batch_date"] for info in frozen.values()}) != 1):
        raise OdsError("ODS_CONFIG_MIXED")
    return frozen


def _notify(on_event, name, status, rows, started):
    if on_event is None:
        return
    try:
        on_event(name, status, rows, max(0, int((monotonic() - started) * 1000)))
    except OdsError:
        raise
    except Exception:
        raise OdsError("ODS_EVENT_CALLBACK_FAILED") from None


def run_extract_all(start_date, end_date, *, source_infos=None, on_event=None):
    """Preflight all files, then commit each source separately; failures leave a committed prefix."""
    if start_date is not None or end_date is not None:
        _date_range(start_date, end_date)
    if on_event is not None and not callable(on_event):
        raise OdsError("ODS_EVENT_CALLBACK_INVALID")
    infos = _freeze_sources(source_infos)
    prepared = {name: _read_frame(name, infos[name]) for name in SOURCES}
    if start_date is None:
        all_dates = [day for _, dates in prepared.values() for day in dates]
        start_date, end_date = min(all_dates).isoformat(), max(all_dates).isoformat()
    verify_schema()
    result = {}
    names = list(SOURCES)
    for index, name in enumerate(names):
        started = monotonic()
        try:
            _notify(on_event, name, "START", None, started)
            count = _load_range_to_ods(prepared[name][0], SOURCES[name]["table"], start_date, end_date)
            _notify(on_event, name, "SUCCESS", count, started)
            result[name] = count
        except Exception:
            # A failed success callback cannot undo a committed transaction.
            try:
                _notify(on_event, name, "FAILED", None, started)
                for blocked in names[index + 1:]:
                    _notify(on_event, blocked, "BLOCKED", None, monotonic())
            except OdsError:
                # Preserve primary failure and its retry class if failure reporting also fails.
                pass
            raise
    return result


def _partition_rows(rows, date_column, start, end, code):
    inside, outside = {}, {}
    for row in rows:
        day = _strict_date(row[date_column], code)
        target = inside if start <= day <= end else outside
        target[row["source_row_number"]] = row
    return inside, outside


def _logical_rows(rows, columns):
    return {number: tuple(row[column] for column in columns) for number, row in rows.items()}


def _load_range_to_ods(full_df, table, start_date, end_date):
    """Private complete-source input only; guard outside rows before any range mutation."""
    start, end = _date_range(start_date, end_date)
    rows = validate_snapshot(full_df, table)
    name = next(name for name, spec in SOURCES.items() if spec["table"] == table)
    inside, outside = _partition_rows(rows, SOURCE_DATES[name], start, end, "ODS_SOURCE_DATE_INVALID")
    dataset = rows[0]["dataset_id"]
    columns = TABLES[table] + METADATA
    stored_columns = columns + ("id", "etl_load_time")
    outside_columns = TABLES[table] + ("dataset_id", "source_row_number")
    select = (f"SELECT {','.join('`' + column + '`' for column in stored_columns)} FROM `{table}` "
              "WHERE dataset_id=%s ORDER BY source_row_number")
    insert_columns = columns + ("etl_load_time",)
    insert = (f"INSERT INTO `{table}` ({','.join('`' + column + '`' for column in insert_columns)}) "
              f"VALUES ({','.join(['%s'] * len(insert_columns))})")
    connection = connect_database()
    failed = False
    try:
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute(select + " FOR UPDATE", (dataset,))
            previous = [dict(zip(stored_columns, values)) for values in cursor.fetchall()]
            old_inside, old_outside = _partition_rows(
                previous, SOURCE_DATES[name], start, end, "ODS_RANGE_REBASE_REQUIRED")
            if _logical_rows(outside, outside_columns) != _logical_rows(old_outside, outside_columns):
                raise OdsError("ODS_RANGE_REBASE_REQUIRED")
            for number in old_inside:
                cursor.execute(f"DELETE FROM `{table}` WHERE dataset_id=%s AND source_row_number=%s",
                               (dataset, number))
            if inside:
                load_time = datetime.now(timezone.utc).replace(tzinfo=None)
                values = [tuple(row[column] for column in columns) + (load_time,) for row in inside.values()]
                cursor.executemany(insert, values)
            cursor.execute(select, (dataset,))
            after = [dict(zip(stored_columns, values)) for values in cursor.fetchall()]
            new_inside, new_outside = _partition_rows(
                after, SOURCE_DATES[name], start, end, "ODS_RANGE_VERIFY_FAILED")
            # MySQL returns DATE metadata as date objects; business date text stays untouched.
            for row in new_inside.values():
                row["etl_batch_date"] = str(row["etl_batch_date"])
            if (_logical_rows(inside, columns) != _logical_rows(new_inside, columns)
                    or _logical_rows(old_outside, stored_columns) != _logical_rows(new_outside, stored_columns)):
                raise OdsError("ODS_RANGE_VERIFY_FAILED")
        connection.commit()
    except Exception as error:
        failed = True
        try:
            connection.rollback()
        except Exception:
            raise OdsError("ODS_ROLLBACK_FAILED") from None
        if isinstance(error, OdsError):
            raise
        raise OdsError("ODS_RANGE_LOAD_FAILED") from None
    finally:
        try:
            connection.close()
        except Exception:
            if not failed:
                raise OdsError("ODS_DATABASE_CLOSE_FAILED") from None
    return len(inside)


_FINANCE_SUMMARY_COLUMNS = frozenset(("dept", "amount", "etl_batch_date"))
_PUBLIC_COLUMN_ALIASES = frozenset(("id", "etl_load_time", "dept", "amount"))


def _record_fields_error(table, required, present, duplicate=0):
    missing = required - present
    metadata = [name for name in METADATA if name in missing]
    business = [str(index) for index, name in enumerate(TABLES[table], 1) if name in missing]
    extra = present - required
    known_extra = ','.join(sorted(extra & _PUBLIC_COLUMN_ALIASES)) or '-'
    detail = (f"metadata_missing={','.join(metadata) or '-'} business_missing={len(business)} "
              f"unexpected={len(extra)} duplicate={duplicate} table={table} "
              f"business_positions={','.join(business) or '-'} known_extra={known_extra}")
    return OdsError("ODS_RECORD_FIELDS_INVALID", detail=detail)


def validate_snapshot(df, table):
    if not isinstance(table, str) or table not in TABLES:
        raise OdsError("ODS_TABLE_INVALID")
    columns = TABLES[table]
    required = set(columns + METADATA)
    if isinstance(df, pd.DataFrame) and not df.columns.is_unique:
        present = set(df.columns)
        raise _record_fields_error(table, required, present, len(df.columns) - len(present))
    try:
        records = df.to_dict(orient="records") if callable(getattr(df, "to_dict", None)) else list(df)
    except Exception:
        raise OdsError("ODS_SNAPSHOT_INVALID") from None
    if not records:
        raise OdsError("ODS_SNAPSHOT_EMPTY")
    result = []
    dataset = batch = None
    for number, row in enumerate(records, start=1):
        if not isinstance(row, dict):
            raise OdsError("ODS_RECORD_FIELDS_INVALID", detail="record_type_invalid")
        present = set(row)
        if present != required:
            raise _record_fields_error(table, required, present)
        validate_dataset(row["dataset_id"])
        validate_batch(row["etl_batch_date"])
        if number == 1:
            dataset, batch = row["dataset_id"], row["etl_batch_date"]
        if row["dataset_id"] != dataset or row["etl_batch_date"] != batch:
            raise OdsError("ODS_SNAPSHOT_MIXED")
        if type(row["source_row_number"]) is not int:
            raise OdsError("ODS_ROW_NUMBER_INVALID")
        for column in columns:
            validate_text(row[column])
        result.append(dict(row))
    result.sort(key=lambda row: row["source_row_number"])
    if [row["source_row_number"] for row in result] != list(range(1, len(result) + 1)):
        raise OdsError("ODS_ROW_NUMBER_INVALID")
    return result


def _finance_summary_batch(value):
    if isinstance(value, datetime) and not pd.isna(value):
        if value.tzinfo is not None or value.time() != time.min or getattr(value, "nanosecond", 0):
            raise OdsError("ODS_BATCH_INVALID")
        value = value.date()
    return validate_batch(value.isoformat() if type(value) is date else value)


def _finance_summary_records(frame, table):
    by_batch = {}
    for row in frame.to_dict(orient="records"):
        batch = _finance_summary_batch(row["etl_batch_date"])
        dept = row["dept"]
        validate_text(dept)
        if not dept.strip():
            raise OdsError("ODS_FINANCE_DEPARTMENT_INVALID")
        raw = row["amount"]
        if type(raw) not in (str, int, float, Decimal):
            raise OdsError("ODS_FINANCE_AMOUNT_INVALID")
        amount = str(raw)
        validate_text(amount)
        try:
            if not Decimal(amount).is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise OdsError("ODS_FINANCE_AMOUNT_INVALID") from None
        records = by_batch.setdefault(batch, [])
        records.append(dict(
            record_id="", stat_date="", dept_code=dept, revenue=amount, drug_revenue="", consumable_revenue="",
            dataset_id=FINANCE_SUMMARY_PREFIX + batch, source_row_number=len(records) + 1, etl_batch_date=batch,
        ))
    if not by_batch:
        raise OdsError("ODS_SNAPSHOT_EMPTY")
    return [row for records in by_batch.values() for row in validate_snapshot(records, table)]


def load_to_ods(df, table):
    summary = (isinstance(table, str) and table == "ods_finance_excel" and isinstance(df, pd.DataFrame)
               and df.columns.is_unique and set(df.columns) == _FINANCE_SUMMARY_COLUMNS)
    rows = _finance_summary_records(df, table) if summary else validate_snapshot(df, table)
    if not summary and rows[0]["dataset_id"].startswith(FINANCE_SUMMARY_PREFIX):
        raise OdsError("ODS_DATASET_RESERVED")
    dataset_counts = Counter(row["dataset_id"] for row in rows)
    datasets = sorted(dataset_counts)
    columns = TABLES[table] + METADATA + ("etl_load_time",)
    load_time = datetime.now(timezone.utc).replace(tzinfo=None)
    values = [tuple(row[column] for column in columns[:-1]) + (load_time,) for row in rows]
    # Table/column identifiers originate only from frozen contracts; values are parameters.
    insert = (f"INSERT INTO `{table}` ({','.join('`' + column + '`' for column in columns)}) "
              f"VALUES ({','.join(['%s'] * len(columns))})")
    connection = connect_database()
    try:
        connection.begin()
        with connection.cursor() as cursor:
            for dataset in datasets:
                cursor.execute(f"DELETE FROM `{table}` WHERE dataset_id=%s", (dataset,))
            cursor.executemany(insert, values)
            for dataset in datasets:
                cursor.execute(f"SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s", (dataset,))
                if cursor.fetchone()[0] != dataset_counts[dataset]:
                    raise OdsError("ODS_COUNT_MISMATCH")
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            raise OdsError("ODS_ROLLBACK_FAILED") from None
        raise OdsError("ODS_LOAD_FAILED") from None
    finally:
        try:
            connection.close()
        except Exception:
            raise OdsError("ODS_DATABASE_CLOSE_FAILED") from None
    return len(rows)


def initialize_schema():
    """CREATE IF NOT EXISTS only; DDL is separate from snapshot transactions."""
    try:
        statements = (PROJECT_ROOT / "sql/ods/ods_tables.sql").read_text(encoding="utf-8").split(";")
    except (OSError, UnicodeError):
        raise OdsError("ODS_DDL_READ_FAILED") from None
    with database_cursor("ODS_DDL_FAILED") as cursor:
        for statement in statements:
            if statement.strip():
                cursor.execute(statement)
    verify_schema()


def verify_schema():
    """Fail closed on existing incompatible tables; never ALTER existing data."""
    try:
        statements = (PROJECT_ROOT / "sql/ods/verify_ods.sql").read_text(encoding="utf-8").split(";")
    except (OSError, UnicodeError):
        raise OdsError("ODS_DDL_READ_FAILED") from None
    results = []
    with database_cursor("ODS_SCHEMA_INVALID") as cursor:
        for statement in statements:
            if statement.strip():
                cursor.execute(statement)
                results.append(cursor.fetchall())
    if len(results) != 3:
        raise OdsError("ODS_SCHEMA_INVALID")
    expected_statuses = {(table, "OK", "OK", "OK", "OK", "OK") for table in TABLES}
    expected_indexes = set()
    for table in TABLES:
        expected_indexes.update({(table, "PRIMARY", 0, "id"),
                                 (table, "uq_dataset_row", 0, "dataset_id,source_row_number")})
    if (set(map(tuple, results[0])) != expected_statuses
            or set(map(tuple, results[1])) != expected_indexes
            or list(map(tuple, results[2])) != [("source_config", "InnoDB", "utf8mb4_bin", "OK", "OK")]):
        raise OdsError("ODS_SCHEMA_INVALID")


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise OdsError("ODS_ARGUMENTS_INVALID")


def main(argv=None):
    try:
        parser = SafeParser(description="O5: six approved SIM CSV sources to local MySQL ODS")
        parser.add_argument("--dataset-id", required=True)
        parser.add_argument("--batch-date", required=True)
        args = parser.parse_args(argv)
        validate_dataset(args.dataset_id)
        validate_batch(args.batch_date)
        prepared = []
        for name, spec in SOURCES.items():
            info = dict(kind="csv", file_path=str(GENERATED_ROOT / args.dataset_id / spec["path"]),
                        dataset_id=args.dataset_id, etl_batch_date=args.batch_date)
            prepared.append((name, spec["table"], info, read_source(name, info)))
        initialize_schema()
        for name, table, info, rows in prepared:
            register_source(name, info)
            if not test_connection(name):
                raise OdsError("ODS_SOURCE_CHECK_FAILED")
            count = load_to_ods(rows, table)
            print(json.dumps(dict(source=name, count=count, code="ODS_OK")))
        return 0
    except OdsError as error:
        print(json.dumps(dict(code=error.code)))
        return 1
    except Exception:
        print(json.dumps(dict(code="ODS_UNEXPECTED_FAILURE")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
