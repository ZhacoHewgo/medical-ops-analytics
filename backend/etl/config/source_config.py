"""Frozen O5 file-source registry and local MySQL boundary; no patient logging."""

from contextlib import contextmanager
import csv
from datetime import date
import json
import logging
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[3]
GENERATED_ROOT = PROJECT_ROOT / "data/generated"
COURSE_DATASET = "course-platform-v1"
SOURCES = {
    "his_outpatient": {
        "table": "ods_his_outpatient_visit", "path": "mock_his/his_outpatient.csv",
        "columns": tuple("visit_id patient_id patient_name id_card phone gender age dept_code visit_date visit_type fee_amount".split()),
    },
    "his_inpatient": {
        "table": "ods_his_inpatient_visit", "path": "mock_his/his_inpatient.csv",
        "columns": tuple("visit_id patient_id patient_name id_card phone gender age dept_code bed_no admit_date discharge_date stay_days total_fee drug_fee consumable_fee disease_code".split()),
    },
    "his_charge": {
        "table": "ods_his_charge_detail", "path": "mock_his/his_charge.csv",
        "columns": tuple("charge_id visit_id patient_id visit_type dept_code fee_date fee_type_code fee_type_name amount".split()),
    },
    "emr_record": {
        "table": "ods_emr_record", "path": "mock_his/emr_record.csv",
        "columns": tuple("record_id patient_id visit_id dept_code visit_date diagnosis disease_code".split()),
    },
    "lis_report": {
        "table": "ods_lis_report", "path": "mock_his/lis_report.csv",
        "columns": tuple("report_id patient_id visit_id item_name result_text report_date".split()),
    },
    "finance_excel": {
        "table": "ods_finance_excel", "path": "files/finance_excel.csv",
        "columns": tuple("record_id stat_date dept_code revenue drug_revenue consumable_revenue".split()),
    },
}
CONFIG_FIELDS = ("kind", "file_path", "dataset_id", "etl_batch_date")
FINANCE_SUMMARY_PREFIX = "ods-finance-summary-v1."
LOGGER = logging.getLogger(__name__)


class OdsError(Exception):
    """Safe reason codes and internally generated diagnostics."""

    def __init__(self, code, *, detail=None):
        self.code = code
        super().__init__(code if detail is None else f"{code}: {detail}")


def validate_dataset(dataset):
    if not isinstance(dataset, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", dataset):
        raise OdsError("ODS_DATASET_INVALID")
    return dataset


def validate_batch(batch):
    try:
        if not isinstance(batch, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", batch):
            raise ValueError
        date.fromisoformat(batch)
    except ValueError:
        raise OdsError("ODS_BATCH_INVALID") from None
    return batch


def _private_database_url():
    try:
        directory = os.open(PROJECT_ROOT / ".runtime", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open("ods-connection.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid() or info.st_nlink != 1):
                raise ValueError
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError
        settings = json.loads(data)
        if not isinstance(settings, dict) or not isinstance(settings.get("ODS_DATABASE_URL"), str):
            raise ValueError
        return settings["ODS_DATABASE_URL"]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        raise OdsError("ODS_DATABASE_URL_INVALID") from None


def database_options(*, test_only=False):
    url = os.environ.get("ODS_TEST_DATABASE_URL" if test_only else "ODS_DATABASE_URL")
    if url is None and not test_only:
        url = _private_database_url()
    if not url:
        raise OdsError("ODS_DATABASE_URL_MISSING")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "mysql" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.port != 13306 or parsed.path not in {"/hospital", "/hospital_test"}
                or parsed.query or parsed.fragment or not parsed.username or not parsed.password
                or any(character.isspace() or ord(character) < 32 for character in url)):
            raise ValueError
        username = unquote(parsed.username, errors="strict")
        password = unquote(parsed.password, errors="strict")
        if not username or not password or "\x00" in username + password:
            raise ValueError
    except (ValueError, UnicodeError):
        raise OdsError("ODS_DATABASE_URL_INVALID") from None
    if test_only and parsed.path != "/hospital_test":
        raise OdsError("ODS_TEST_DATABASE_REQUIRED")
    return dict(host=parsed.hostname, port=13306, user=username, password=password,
                database=parsed.path[1:], charset="utf8mb4", autocommit=False,
                connect_timeout=5, read_timeout=30, write_timeout=30,
                init_command="SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION', time_zone='+00:00'")


def connect_database(*, test_only=False):
    options = database_options(test_only=test_only)
    try:
        import pymysql
    except ImportError:
        raise OdsError("ODS_DEPENDENCY_MISSING") from None
    try:
        return pymysql.connect(**options)
    except Exception:
        raise OdsError("ODS_DATABASE_CONNECT_FAILED") from None


@contextmanager
def database_cursor(code, *, write=False):
    connection = connect_database()
    try:
        if write:
            connection.begin()
        with connection.cursor() as cursor:
            yield cursor
        if write:
            connection.commit()
    except Exception:
        if write:
            try:
                connection.rollback()
            except Exception:
                raise OdsError("ODS_ROLLBACK_FAILED") from None
        raise OdsError(code) from None
    finally:
        try:
            connection.close()
        except Exception:
            raise OdsError("ODS_DATABASE_CLOSE_FAILED") from None


def validate_source(name, conn_info):
    if not isinstance(name, str) or name not in SOURCES:
        raise OdsError("ODS_SOURCE_INVALID")
    if (not isinstance(conn_info, dict) or set(conn_info) != set(CONFIG_FIELDS)
            or any(not isinstance(value, str) for value in conn_info.values())
            or conn_info["kind"] != "csv"):
        raise OdsError("ODS_CONFIG_INVALID")
    validate_dataset(conn_info["dataset_id"])
    if conn_info["dataset_id"].startswith(FINANCE_SUMMARY_PREFIX):
        raise OdsError("ODS_DATASET_RESERVED")
    validate_batch(conn_info["etl_batch_date"])
    try:
        path = Path(conn_info["file_path"])
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        dataset = conn_info["dataset_id"]
        source_root = PROJECT_ROOT / "data" if dataset == COURSE_DATASET else GENERATED_ROOT / dataset
        expected = source_root / SOURCES[name]["path"]
        if (".." in path.parts or "\x00" in str(path)
                or any(part.is_symlink() for part in (path, *path.parents))
                or path.resolve() != expected
                or source_root.resolve() != source_root):
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise OdsError("ODS_PATH_INVALID") from None
    return {**conn_info, "file_path": str(expected)}


def validate_text(value):
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > 65535:
            raise ValueError
    except (ValueError, UnicodeError):
        raise OdsError("ODS_TEXT_INVALID") from None


def read_source(name, conn_info):
    info = validate_source(name, conn_info)
    columns = SOURCES[name]["columns"]
    records = []
    try:
        with Path(info["file_path"]).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != list(columns):
                raise OdsError("ODS_CSV_HEADER_INVALID")
            for number, values in enumerate(reader, start=1):
                if len(values) != len(columns):
                    raise OdsError("ODS_CSV_ROW_INVALID")
                for value in values:
                    validate_text(value)
                records.append(dict(zip(columns, values), dataset_id=info["dataset_id"],
                                    source_row_number=number, etl_batch_date=info["etl_batch_date"]))
    except OdsError:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError):
        raise OdsError("ODS_CSV_READ_FAILED") from None
    if not records:
        raise OdsError("ODS_SNAPSHOT_EMPTY")
    return records


def register_source(name, conn_info):
    info = validate_source(name, conn_info)
    values = tuple(info[field] for field in CONFIG_FIELDS)
    with database_cursor("ODS_REGISTER_FAILED", write=True) as cursor:
        cursor.execute(
            "INSERT INTO source_config (name,kind,file_path,dataset_id,etl_batch_date) "
            "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "kind=%s,file_path=%s,dataset_id=%s,etl_batch_date=%s", (name, *values, *values))


def list_sources():
    with database_cursor("ODS_REGISTRY_READ_FAILED") as cursor:
        cursor.execute("SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name")
        result = []
        for name, kind, path, dataset, batch in cursor.fetchall():
            info = validate_source(name, dict(kind=kind, file_path=path, dataset_id=dataset,
                                             etl_batch_date=str(batch)))
            result.append(dict(name=name, **info))
        return result


def get_source(name):
    if not isinstance(name, str) or name not in SOURCES:
        raise OdsError("ODS_SOURCE_INVALID")
    for item in list_sources():
        if item["name"] == name:
            return {key: item[key] for key in CONFIG_FIELDS}
    raise OdsError("ODS_SOURCE_NOT_REGISTERED")


def test_connection(name):
    """Prove registered CSV readability only; not connectivity to a HIS system."""
    try:
        read_source(name, get_source(name))
        return True
    except OdsError as error:
        LOGGER.warning("%s", error.code)
        return False
