"""Node 7 fixed five-table cleaning. Snapshots contain private rows: never log them."""
from base64 import b64decode
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import wraps
import hashlib
import hmac
import json
from numbers import Integral
import os
import re
import unicodedata
from uuid import uuid4

import pandas as pd
import pymysql
from etl.config import course_mapping
from etl.config.source_config import (
    OdsError, PROJECT_ROOT, SOURCES, database_options, validate_batch,
    validate_dataset, validate_source,
)

CONFIG_PATH = PROJECT_ROOT / 'backend/etl/config/dwd_rules.json'
COMMON = ('dataset_id', 'source_row_number', 'source_batch_date', 'etl_batch_date', 'etl_load_time', 'patient_key')
SUPPRESSION = ('patient_name', 'id_card', 'phone')
META = ('dataset_id', 'source_row_number', 'etl_batch_date')
REASONS = ('invalid_id_rows', 'conflicting_id_rows', 'invalid_primary_date_rows')
# Explicit schema is also the SQL identifier allowlist; config cannot add tables or columns.
_SPECS = (
    ('outpatient', 'his_outpatient', 'dwd_outpatient_visit_detail', 'visit_id', 'visit_date',
     'visit_id visit_date dept_code visit_type gender age fee_amount patient_name id_card phone', 'dept_code visit_type gender'),
    ('inpatient', 'his_inpatient', 'dwd_inpatient_visit_detail', 'visit_id', 'admit_date',
     'visit_id admit_date discharge_date dept_code gender age stay_days total_fee drug_fee consumable_fee disease_code patient_name id_card phone', 'dept_code gender disease_code'),
    ('charge', 'his_charge', 'dwd_charge_detail', 'charge_id', 'fee_date',
     'charge_id visit_id visit_type dept_code fee_date fee_type_code amount', 'dept_code visit_type fee_type_code'),
    ('emr', 'emr_record', 'dwd_emr_record_detail', 'record_id', 'visit_date',
     'record_id visit_id dept_code visit_date disease_code', 'dept_code disease_code'),
    ('lis', 'lis_report', 'dwd_lis_report_detail', 'report_id', 'report_date',
     'report_id visit_id report_date', ''),
)
TABLES = {t: dict(short=s, source=src, id_column=key, date_column=day,
                  columns=list(COMMON) + cols.split(), mapping_columns=mapping.split())
          for s, src, t, key, day, cols, mapping in _SPECS}
MAPPINGS = {
    'dept_code': {**{f'SIM_DEPT_{i:02}': f'SIM_DEPT_{i:02}' for i in range(1, 5)},
                  **{f'SIM_科室{i}_别名': f'SIM_DEPT_{i:02}' for i in range(1, 5)}},
    'visit_type': {x: x for x in ('OUTPATIENT', 'INPATIENT')},
    'gender': {x: x for x in ('M', 'F')},
    'disease_code': {f'SIM_DX_{i:02}': f'SIM_DX_{i:02}' for i in range(1, 6)},
    'fee_type_code': {x: x for x in ('SERVICE', 'DRUG', 'CONSUMABLE')},
}
RULES = [{'rule_id': f'{s}.{kind}', 'table': t, 'type': kind}
         for s, _, t, *_ in _SPECS
         for kind in ('NULL', 'DUPLICATE', 'VALUE_RANGE', 'VOLUME_WAVE', 'RECONCILE')]
RULES += [{'rule_id': f'{s}.MAPPING', 'table': t, 'type': 'MAPPING'} for s, _, t, *_ in _SPECS if s != 'lis']
RULES += [{'rule_id': f'{s}.PRIVACY', 'table': t, 'type': 'PRIVACY'} for s, _, t, *_ in _SPECS]
AUDIT = 'dwd_clean_audit'
AUDIT_COLUMNS = tuple(('dataset_id table_name clean_run_id batch_date rules_version mapping_version privacy_key_id '
                       'source_rows accepted_rows duplicate_removed quarantined_rows source_sha256 result_sha256 stats_json updated_at').split())
MONEY = frozenset(('fee_amount', 'total_fee', 'drug_fee', 'consumable_fee', 'amount'))
DATES = frozenset(('visit_date', 'admit_date', 'discharge_date', 'fee_date', 'report_date'))
SAFE_CODES = frozenset(('DWD_ARGUMENTS_INVALID DWD_CONFIG_INVALID DWD_SCHEMA_INVALID '
    'DWD_INPUT_EMPTY DWD_SOURCE_MISMATCH DWD_PRIVACY_KEY_MISSING DWD_PRIVACY_KEY_INVALID '
    'DWD_PRIVACY_KEY_CHANGED DWD_TX_RETRYABLE DWD_TX_FAILED DWD_ROLLBACK_FAILED '
    'DWD_COMMIT_UNKNOWN DWD_INTERNAL_ERROR DQ_READ_RETRYABLE DQ_ARGUMENTS_INVALID '
    'DQ_CONFIG_INVALID DQ_SNAPSHOT_MISMATCH DQ_STALE DQ_RULE_ERROR DQ_INTERNAL_ERROR '
    'DQ_REPORT_PATH_INVALID DQ_REPORT_IDENTITY_CONFLICT DQ_REPORT_WRITE_FAILED '
    'DQ_GATE_FAILED DQ_INSUFFICIENT_EVIDENCE ODS_DATABASE_URL_MISSING ODS_DATABASE_URL_INVALID').split())


def _safe(code):
    """No exception text or unknown exception reaches scheduler's retry fallback."""
    def decorate(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except OdsError as error:
                if isinstance(error.code, str) and error.code in SAFE_CODES:
                    raise OdsError(error.code) from None
                raise OdsError(code) from None
            except Exception:
                raise OdsError(code) from None
        return guarded
    return decorate


def _missing(value):
    return value is None or (isinstance(value, str) and not value.strip()) or (
        not isinstance(value, (str, list, dict)) and bool(pd.isna(value)))


def _valid_id(value, *, integer=False):
    if integer and isinstance(value, Integral) and not isinstance(value, bool):
        return True
    return (isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip()
            and not any(unicodedata.category(ch) == 'Cc' for ch in value))


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError
    return date.fromisoformat(value)


def _arguments(batch, dataset=None, *, quality=False):
    try:
        if batch is not None:
            validate_batch(batch)
        if dataset is not None:
            validate_dataset(dataset)
    except Exception:
        raise OdsError('DQ_ARGUMENTS_INVALID' if quality else 'DWD_ARGUMENTS_INVALID') from None


@_safe('DWD_CONFIG_INVALID')
def _config():
    if CONFIG_PATH.is_symlink():
        raise ValueError
    value = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    if (set(value) != {'rules_version', 'mapping_version', 'approved_complete_intervals', 'tables', 'mappings', 'rules'}
            or value['tables'] != TABLES or value['mappings'] != MAPPINGS or value['rules'] != RULES
            or not isinstance(value['rules_version'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value['rules_version'])
            or value['mapping_version'] != course_mapping.SIM_VERSION
            or not isinstance(value['approved_complete_intervals'], list)):
        raise ValueError
    seen = set()
    for item in value['approved_complete_intervals']:
        if set(item) != {'dataset_id', 'table', 'start_date', 'end_date', 'basis_id'}:
            raise ValueError
        validate_dataset(item['dataset_id'])
        if item['table'] not in TABLES or _day(item['start_date']) > _day(item['end_date']):
            raise ValueError
        if not isinstance(item['basis_id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', item['basis_id']):
            raise ValueError
        pair = item['dataset_id'], item['table']
        if pair in seen:
            raise ValueError
        seen.add(pair)
    if value['approved_complete_intervals'] and value['rules_version'] == 'node07-rules-v1':
        raise ValueError  # Coverage evidence changes require a version change.
    return value


@_safe('DWD_ARGUMENTS_INVALID')
def dedupe(df, keys=None, *, with_counts=False):
    if not isinstance(df, pd.DataFrame) or not df.columns.is_unique:
        raise ValueError
    counts = dict(invalid_id_rows=0, conflicting_id_rows=0, duplicate_removed=0)
    if keys is None:
        key = next((name for name in ('charge_id', 'record_id', 'report_id', 'visit_id', 'id')
                    if name in df.columns), None)
        if key is None:
            before = len(df)
            df.drop_duplicates(inplace=True, ignore_index=True)
            counts['duplicate_removed'] = before - len(df)
            return (df, counts) if with_counts else df
        keys = ['dataset_id', key] if 'dataset_id' in df.columns else [key]
    if (not isinstance(keys, (list, tuple)) or not keys
            or any(k not in df.columns for k in keys) or len(set(keys)) != len(keys)):
        raise ValueError
    records = df.astype(object).where(df.notna(), None).to_dict('records')
    groups = {}
    business = [col for col in df.columns if col not in (*META, 'etl_load_time', 'source_batch_date')]
    for index, row in enumerate(records):
        if not all(_valid_id(row[key], integer=key != 'dataset_id') for key in keys):
            counts['invalid_id_rows'] += 1
            continue
        groups.setdefault(tuple(row[key] for key in keys), []).append(index)
    retained = []
    for indexes in groups.values():
        first = records[indexes[0]]
        if any(any(row[col] != first[col] for col in business) for row in (records[i] for i in indexes)):
            if 'updated_at' in df.columns:
                timestamps = pd.to_datetime([records[i]['updated_at'] for i in indexes],
                                            format='ISO8601', utc=True, errors='coerce')
                latest = timestamps == timestamps.max()
                if timestamps.notna().all() and latest.sum() == 1:
                    retained.append(indexes[latest.argmax()])
                    counts['duplicate_removed'] += len(indexes) - 1
                    continue
            counts['conflicting_id_rows'] += len(indexes)
            continue
        chosen = min(indexes, key=lambda i: records[i]['source_row_number']) if 'source_row_number' in df.columns else indexes[0]
        retained.append(chosen)
        counts['duplicate_removed'] += len(indexes) - 1
    result = df.iloc[sorted(retained)].copy(deep=True).reset_index(drop=True)
    return (result, counts) if with_counts else result


@_safe('DWD_ARGUMENTS_INVALID')
def fill_missing(df, strategy):
    if not isinstance(df, pd.DataFrame) or not isinstance(strategy, dict) or not df.columns.is_unique:
        raise ValueError
    result = df.copy(deep=True)
    touched = set()
    cells = 0
    for column, operation in strategy.items():
        if column not in result.columns or operation not in ('mean', 'median', '未知'):
            raise ValueError
        missing = result[column].map(_missing)
        replacement = operation
        if operation != '未知':
            present = [Decimal(str(v)) for v in result.loc[~missing, column]
                       if not isinstance(v, bool)]
            if len(present) != int((~missing).sum()) or not present or any(not v.is_finite() for v in present):
                raise ValueError
            present.sort()
            size = len(present)
            replacement = (sum(present) / size if operation == 'mean' else
                           (present[(size - 1) // 2] + present[size // 2]) / 2)
            # Pandas numeric demo columns retain their numeric dtype.
            if pd.api.types.is_numeric_dtype(result[column]):
                replacement = float(replacement)
        result.loc[missing, column] = replacement
        touched.update(position for position, absent in enumerate(missing) if absent)
        cells += int(missing.sum())
    return result, dict(filled_rows=len(touched), filled_cells=cells)


@_safe('DWD_ARGUMENTS_INVALID')
def standardize_codes(df, mapping, column):
    if (not isinstance(df, pd.DataFrame) or column not in df.columns or not isinstance(mapping, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or v == 'UNMAPPED' for k, v in mapping.items())):
        raise ValueError
    result = df.copy(deep=True)
    hits = [isinstance(v, str) and v in mapping for v in df[column]]
    result[column] = [mapping[v] if hit else 'UNMAPPED' for v, hit in zip(df[column], hits)]
    return result, dict(slots=len(df), matched=sum(hits), unmapped=len(df) - sum(hits))


@_safe('DWD_ARGUMENTS_INVALID')
def mask_privacy(df, columns=None):
    if not isinstance(df, pd.DataFrame) or not df.columns.is_unique:
        raise ValueError
    private_columns = (*SUPPRESSION, 'name', 'id_no', 'id_number', 'id_card_no', 'mobile',
                       'phone_number', '姓名', '身份证', '身份证号', '电话', '手机号')
    implicit = columns is None
    if implicit:
        columns = [col for col in df.columns if col in private_columns]
    if (not isinstance(columns, (list, tuple)) or len(set(columns)) != len(columns)
            or any(col not in private_columns or col not in df.columns for col in columns)):
        raise ValueError
    result = df if implicit else df.copy(deep=True)
    stats = {}
    for column in columns:
        missing = [_missing(value) for value in df[column]]
        result[column] = [None if absent else '[REDACTED]' for absent in missing]
        stats[column] = dict(nonempty=len(df) - sum(missing), processed=len(df) - sum(missing), missing=sum(missing))
    return result if implicit else (result, stats)


@_safe('DWD_PRIVACY_KEY_INVALID')
def _privacy():
    encoded = os.environ.get('DWD_PRIVACY_KEY')
    key_id = os.environ.get('DWD_PRIVACY_KEY_ID')
    if not encoded or not key_id:
        raise OdsError('DWD_PRIVACY_KEY_MISSING') from None
    key = b64decode(encoded, validate=True)
    if len(key) != 32 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', key_id):
        raise ValueError
    return key, key_id


def _patient_key(key, dataset, value):
    if _missing(value):
        return None
    if not isinstance(value, str):
        raise OdsError('DWD_SOURCE_MISMATCH') from None
    message = json.dumps(['node07-patient-v1', dataset, value], ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hmac.new(key, message.encode('utf-8'), digestmod='sha256').hexdigest()


@_safe('DQ_ARGUMENTS_INVALID')
def quality_digest(kind, columns, rows):
    """ODS/DWD snapshot digest, never a CSV hash; no load-time or row hashes."""
    if kind not in ('source', 'dwd') or not isinstance(columns, (list, tuple)) or len(set(columns)) != len(columns):
        raise ValueError
    allowed = ([list(spec['columns'] + META) for spec in SOURCES.values()] if kind == 'source'
               else [spec['columns'] for spec in TABLES.values()])
    if list(columns) not in allowed:
        raise ValueError
    columns = [col for col in columns if col != 'etl_load_time']
    if kind == 'source':
        ordered = sorted(rows, key=lambda row: row['source_row_number'])
    else:
        spec = next(s for s in TABLES.values() if [c for c in s['columns'] if c != 'etl_load_time'] == columns)
        ordered = sorted(rows, key=lambda row: row[spec['id_column']].encode('utf-8'))
    def canonical(value):
        if value is None or type(value) in (str, int):
            return value
        if isinstance(value, Decimal) and value.is_finite() and value == value.quantize(Decimal('.01')):
            return format(value, '.2f')
        if type(value) is date:
            return value.isoformat()
        raise ValueError
    payload = [[canonical(row[col]) for col in columns] for row in ordered]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _decimal(value, *, stay=False):
    if not isinstance(value, str) or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]{1,2})?', value):
        raise ValueError
    number = Decimal(value)
    maximum = Decimal('99999999.99' if stay else '9999999999999999.99')
    if abs(number) > maximum or (stay and number < 0):
        raise ValueError
    return number.quantize(Decimal('.01'))


def _converted(column, value, row):
    if _missing(value):
        return None, 'missing'
    try:
        if column in MONEY or column == 'stay_days':
            return _decimal(value, stay=column == 'stay_days'), None
        if column == 'age':
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value) or not 0 <= int(value) <= 130:
                raise ValueError
            return int(value), None
        if column in DATES:
            day = _day(value)
            if column == 'discharge_date' and day < _day(row['admit_date']):
                raise ValueError
            return day, None
        if column == 'visit_id':
            if not _valid_id(value):
                raise ValueError
            return value, None
        return value, None
    except Exception:
        return None, 'invalid'


def _prepare(table, rows, batch, dataset, key, key_id, run_id, config):
    spec = TABLES[table]
    source_columns = SOURCES[spec['source']]['columns'] + META
    for row in rows:
        if (set(row) != set(source_columns) or row['dataset_id'] != dataset
                or type(row['source_row_number']) is not int or row['source_row_number'] <= 0
                or any(not isinstance(row[col], str) for col in SOURCES[spec['source']]['columns'])
                or type(row['etl_batch_date']) is not date):
            raise OdsError('DWD_SOURCE_MISMATCH') from None
    if len({r['source_row_number'] for r in rows}) != len(rows):
        raise OdsError('DWD_SOURCE_MISMATCH') from None
    frame = pd.DataFrame(rows, columns=source_columns)
    candidates, dedup = dedupe(frame, ['dataset_id', spec['id_column']], with_counts=True)
    quarantine = dict(invalid_id_rows=dedup['invalid_id_rows'], conflicting_id_rows=dedup['conflicting_id_rows'], invalid_primary_date_rows=0)
    business = [col for col in spec['columns'] if col not in COMMON and col not in SUPPRESSION]
    mappings = course_mapping.source_mappings(dataset, spec['source'], config['mappings'])
    fields = {col: dict(missing=0, invalid=0, filled=0, negative=0) for col in business}
    mapping = {col: dict(slots=0, matched=0, unmapped=0) for col in spec['mapping_columns']}
    private_columns = ['patient_id'] + [col for col in SUPPRESSION if col in source_columns]
    privacy = {col: dict(nonempty=0, processed=0, missing=0) for col in private_columns}
    output = []
    load_time = datetime.now(timezone.utc).replace(tzinfo=None)
    for raw in candidates.to_dict('records'):
        try:
            _day(raw[spec['date_column']])
        except Exception:
            quarantine['invalid_primary_date_rows'] += 1
            continue
        result = dict(dataset_id=dataset, source_row_number=raw['source_row_number'],
                      source_batch_date=raw['etl_batch_date'], etl_batch_date=_day(batch),
                      etl_load_time=load_time, patient_key=_patient_key(key, dataset, raw['patient_id']))
        for col in business:
            if col in spec['mapping_columns']:
                hit = raw[col] in mappings[col]
                result[col] = mappings[col].get(raw[col], 'UNMAPPED')
                mapping[col]['slots'] += 1
                mapping[col]['matched' if hit else 'unmapped'] += 1
                if _missing(raw[col]):
                    fields[col]['missing'] += 1
            elif col == spec['id_column']:
                result[col] = raw[col]
            else:
                value, failure = _converted(col, raw[col], raw)
                result[col] = value
                if failure:
                    fields[col][failure] += 1
                if col in MONEY and value is not None and value < 0:
                    fields[col]['negative'] += 1
        for col in private_columns:
            absent = _missing(raw[col])
            privacy[col]['missing' if absent else 'nonempty'] += 1
            privacy[col]['processed'] += int(not absent)
            if col in SUPPRESSION:
                result[col] = None if absent else '[REDACTED]'
        output.append({col: result[col] for col in spec['columns']})
    output.sort(key=lambda row: row[spec['id_column']].encode('utf-8'))
    stats = dict(quarantined_by_reason=quarantine, field_counts=fields, mapping_counts=mapping,
                 privacy_counts=privacy, filled_rows=0, filled_cells=0)
    audit = dict(dataset_id=dataset, table_name=table, clean_run_id=run_id, batch_date=_day(batch),
                 rules_version=config['rules_version'], mapping_version=course_mapping.version(dataset), privacy_key_id=key_id,
                 source_rows=len(rows), accepted_rows=len(output), duplicate_removed=dedup['duplicate_removed'],
                 quarantined_rows=sum(quarantine.values()), source_sha256=quality_digest('source', source_columns, rows),
                 result_sha256=quality_digest('dwd', spec['columns'], output), stats_json=stats, updated_at=load_time)
    if audit['source_rows'] != audit['accepted_rows'] + audit['duplicate_removed'] + audit['quarantined_rows']:
        raise OdsError('DWD_INTERNAL_ERROR') from None
    return output, audit


def _column_spec(table):
    if table == AUDIT:
        types = ('varchar(128)', 'varchar(64)', 'char(36)', 'date', 'varchar(64)', 'varchar(64)', 'varchar(64)',
                 'bigint unsigned', 'bigint unsigned', 'bigint unsigned', 'bigint unsigned', 'char(64)', 'char(64)', 'json', 'datetime(6)')
        return [(col, typ, False) for col, typ in zip(AUDIT_COLUMNS, types)]
    spec = TABLES[table]
    result = []
    for col in spec['columns']:
        typ, nullable = 'varchar(64)', False
        if col == 'dataset_id' or col == spec['id_column'] or col == 'visit_id':
            typ = 'varchar(128)'
            nullable = col == 'visit_id' and col != spec['id_column']
        elif col == 'source_row_number':
            typ = 'bigint unsigned'
        elif col in DATES or col in ('source_batch_date', 'etl_batch_date'):
            typ, nullable = 'date', col == 'discharge_date'
        elif col == 'etl_load_time':
            typ = 'datetime(6)'
        elif col == 'patient_key':
            typ, nullable = 'char(64)', True
        elif col == 'age':
            typ, nullable = 'smallint', True
        elif col in MONEY:
            typ, nullable = 'decimal(18,2)', True
        elif col == 'stay_days':
            typ, nullable = 'decimal(10,2)', True
        elif col in SUPPRESSION:
            typ, nullable = 'varchar(16)', True
        elif col == 'gender':
            typ = 'varchar(16)'
        result.append((col, typ, nullable))
    return result


def _indexes(table):
    if table == AUDIT:
        return {'PRIMARY': (0, ('dataset_id', 'table_name'))}
    spec = TABLES[table]
    result = {'PRIMARY': (0, ('dataset_id', spec['id_column'])),
              f"ix_{spec['short']}_date": (1, ('dataset_id', spec['date_column']))}
    if spec['short'] in ('charge', 'emr', 'lis'):
        result[f"ix_{spec['short']}_visit"] = (1, ('dataset_id', 'visit_id'))
    return result


def _ddl():
    statements = []
    for table in (*TABLES, AUDIT):
        parts = [f'`{col}` {typ.upper()} ' + ('NULL' if nullable else 'NOT NULL') for col, typ, nullable in _column_spec(table)]
        for name, (_, columns) in _indexes(table).items():
            label = 'PRIMARY KEY' if name == 'PRIMARY' else f'KEY `{name}`'
            parts.append(label + ' (' + ','.join(f'`{col}`' for col in columns) + ')')
        statements.append(f'CREATE TABLE IF NOT EXISTS `{table}` (\n  ' + ',\n  '.join(parts) + '\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')
    from etl.jobs.build_dwd import new_ddl
    return statements + new_ddl()


def _verify(cursor):
    names = (*TABLES, AUDIT)
    placeholders = ','.join(['%s'] * len(names))
    cursor.execute('SELECT TABLE_NAME,ENGINE,TABLE_COLLATION FROM information_schema.TABLES '
                   f'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders})', names)
    if set(cursor.fetchall()) != {(t, 'InnoDB', 'utf8mb4_bin') for t in names}:
        raise OdsError('DWD_SCHEMA_INVALID') from None
    cursor.execute('SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,ORDINAL_POSITION,COLLATION_NAME '
                   'FROM information_schema.COLUMNS '
                   f'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders})', names)
    expected = {(t, col, typ, 'YES' if nullable else 'NO', index,
                 'utf8mb4_bin' if typ.startswith(('char(', 'varchar(')) else None)
                for t in names for index, (col, typ, nullable) in enumerate(_column_spec(t), 1)}
    actual = {(t, col, typ.lower(), null, pos, coll) for t, col, typ, null, pos, coll in cursor.fetchall()}
    if actual != expected:
        raise OdsError('DWD_SCHEMA_INVALID') from None
    cursor.execute('SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART,INDEX_TYPE '
                   'FROM information_schema.STATISTICS '
                   f'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders})', names)
    expected_indexes = {(t, name, unique, i, col, None, 'BTREE') for t in names
                        for name, (unique, cols) in _indexes(t).items() for i, col in enumerate(cols, 1)}
    if set(cursor.fetchall()) != expected_indexes:
        raise OdsError('DWD_SCHEMA_INVALID') from None


def connect_database():
    return pymysql.connect(**database_options())


def _connect(quality=False):
    try:
        return connect_database()
    except Exception as error:
        if isinstance(error, pymysql.err.OperationalError) and error.args and error.args[0] in (1040, 1203, 2002, 2003, 2006, 2013):
            raise OdsError('DQ_READ_RETRYABLE' if quality else 'DWD_TX_RETRYABLE') from None
        if isinstance(error, OdsError):
            raise OdsError('DQ_CONFIG_INVALID' if quality else 'DWD_CONFIG_INVALID') from None
        raise OdsError('DQ_INTERNAL_ERROR' if quality else 'DWD_TX_FAILED') from None


def _close(connection):
    try:
        connection.close()
    except Exception:
        pass  # A confirmed COMMIT/ROLLBACK is not undone by socket close failure.


@_safe('DWD_SCHEMA_INVALID')
def initialize_schema():
    """Explicit DDL only. Never called by write or snapshot entrances."""
    statements = _ddl()
    path = PROJECT_ROOT / 'sql/dwd/dwd_tables.sql'
    if path.is_symlink() or path.read_text(encoding='utf-8') != ';\n\n'.join(statements) + ';\n':
        raise OdsError('DWD_SCHEMA_INVALID') from None
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            _verify(cursor)
    finally:
        _close(connection)
    return True


@_safe('DWD_SCHEMA_INVALID')
def verify_schema():
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _verify(cursor)
    finally:
        _close(connection)
    return True


def _registry(cursor, dataset, *, lock=False):
    cursor.execute('SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name' + (' FOR SHARE' if lock else ''))
    records = cursor.fetchall()
    if len(records) != len(SOURCES) or {row[0] for row in records} != set(SOURCES):
        raise OdsError('DWD_SOURCE_MISMATCH') from None
    identities = set()
    try:
        for name, kind, path, selected, batch in records:
            info = validate_source(name, dict(kind=kind, file_path=path, dataset_id=selected, etl_batch_date=str(batch)))
            identities.add((info['dataset_id'], info['etl_batch_date']))
    except Exception:
        raise OdsError('DWD_SOURCE_MISMATCH') from None
    if len(identities) != 1:
        raise OdsError('DWD_SOURCE_MISMATCH') from None
    selected, batch = next(iter(identities))
    if dataset is not None and dataset != selected:
        raise OdsError('DWD_SOURCE_MISMATCH') from None
    return selected, batch, tuple(tuple(row) for row in records)


def _source(cursor, table, dataset):
    spec = TABLES[table]
    src = SOURCES[spec['source']]
    columns = src['columns'] + META
    cursor.execute('SELECT ' + ','.join(f'`{col}`' for col in columns) + f" FROM `{src['table']}` WHERE dataset_id=%s ORDER BY source_row_number", (dataset,))
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _validate_stats(table, stats):
    spec = TABLES[table]
    fields = {col for col in spec['columns'] if col not in COMMON and col not in SUPPRESSION}
    privacy = {'patient_id'} | (set(SUPPRESSION) & set(SOURCES[spec['source']]['columns']))
    if not isinstance(stats, dict) or set(stats) != {'quarantined_by_reason', 'field_counts', 'mapping_counts', 'privacy_counts', 'filled_rows', 'filled_cells'}:
        raise ValueError
    expected = (
        ('quarantined_by_reason', set(REASONS), None),
        ('field_counts', fields, {'missing', 'invalid', 'filled', 'negative'}),
        ('mapping_counts', set(spec['mapping_columns']), {'slots', 'matched', 'unmapped'}),
        ('privacy_counts', privacy, {'nonempty', 'processed', 'missing'}),
    )
    leaves = [stats['filled_rows'], stats['filled_cells']]
    for name, keys, nested in expected:
        if not isinstance(stats[name], dict) or set(stats[name]) != keys:
            raise ValueError
        for value in stats[name].values():
            if nested is None:
                leaves.append(value)
            else:
                if not isinstance(value, dict) or set(value) != nested:
                    raise ValueError
                leaves.extend(value.values())
    if any(type(value) is not int or value < 0 for value in leaves):
        raise ValueError


@_safe('DWD_SCHEMA_INVALID')
def _audits(cursor, dataset):
    cursor.execute('SELECT ' + ','.join(f'`{col}`' for col in AUDIT_COLUMNS) + f' FROM `{AUDIT}` WHERE dataset_id=%s', (dataset,))
    result = {}
    for row in cursor.fetchall():
        item = dict(zip(AUDIT_COLUMNS, row))
        item['stats_json'] = json.loads(item['stats_json']) if isinstance(item['stats_json'], (str, bytes)) else item['stats_json']
        if item['table_name'] not in TABLES or item['table_name'] in result or item['dataset_id'] != dataset:
            raise ValueError
        _validate_stats(item['table_name'], item['stats_json'])
        result[item['table_name']] = item
    return result


def _store(cursor, table, rows, audit):
    columns = TABLES[table]['columns']
    cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id=%s', (audit['dataset_id'],))
    if rows:
        cursor.executemany(f'INSERT INTO `{table}` (' + ','.join(f'`{col}`' for col in columns) + ') VALUES (' + ','.join(['%s'] * len(columns)) + ')', [tuple(row[col] for col in columns) for row in rows])
    cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s', (audit['dataset_id'],))
    if cursor.fetchone()[0] != len(rows):
        raise OdsError('DWD_TX_FAILED') from None
    cursor.execute(f'DELETE FROM `{AUDIT}` WHERE dataset_id=%s AND table_name=%s', (audit['dataset_id'], table))
    values = tuple(json.dumps(audit[col], ensure_ascii=False, separators=(',', ':'), allow_nan=False) if col == 'stats_json' else audit[col] for col in AUDIT_COLUMNS)
    cursor.execute(f'INSERT INTO `{AUDIT}` (' + ','.join(f'`{col}`' for col in AUDIT_COLUMNS) + ') VALUES (' + ','.join(['%s'] * len(AUDIT_COLUMNS)) + ')', values)


def _rollback(connection, error, *, quality=False):
    try:
        connection.rollback()
    except Exception:
        raise OdsError('DQ_INTERNAL_ERROR' if quality else 'DWD_ROLLBACK_FAILED') from None
    if isinstance(error, OdsError) and isinstance(error.code, str) and error.code in SAFE_CODES:
        raise OdsError(error.code) from None
    driver_code = error.args[0] if error.args else None
    if isinstance(error, pymysql.err.OperationalError) and driver_code in (1205, 1213):
        raise OdsError('DQ_READ_RETRYABLE' if quality else 'DWD_TX_RETRYABLE') from None
    raise OdsError('DQ_INTERNAL_ERROR' if quality else 'DWD_TX_FAILED') from None


def _summary(audit):
    return dict(table=audit['table_name'], dataset_id=audit['dataset_id'], clean_run_id=audit['clean_run_id'],
                batch_date=audit['batch_date'].isoformat(), source_rows=audit['source_rows'], accepted_rows=audit['accepted_rows'],
                duplicate_removed=audit['duplicate_removed'], quarantined_rows=audit['quarantined_rows'],
                quarantined_by_reason=audit['stats_json']['quarantined_by_reason'], field_counts=audit['stats_json']['field_counts'])


def _clean(tables, batch, dataset):
    _arguments(batch, dataset)
    if batch is None:
        raise OdsError('DWD_ARGUMENTS_INVALID') from None
    key, key_id = _privacy()  # Missing key fails before even opening a connection.
    config = _config()
    run_id = str(uuid4())
    connection = _connect()
    committing = False
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            selected, _, registry = _registry(cursor, dataset)
            _verify(cursor)
            previous = _audits(cursor, selected)
            if any(item['privacy_key_id'] != key_id for item in previous.values()):
                raise OdsError('DWD_PRIVACY_KEY_CHANGED') from None
            # Read all five even for single-table entry: globally empty input must not clear old output.
            inputs = {table: _source(cursor, table, selected) for table in TABLES}
            if not any(inputs.values()):
                raise OdsError('DWD_INPUT_EMPTY') from None
            prepared = {table: _prepare(table, inputs[table], batch, selected, key, key_id, run_id, config) for table in tables}
            if _registry(cursor, selected, lock=True)[2] != registry:
                raise OdsError('DWD_SOURCE_MISMATCH') from None
            for table, (rows, audit) in prepared.items():
                _store(cursor, table, rows, audit)
        committing = True
        connection.commit()
        return dict(dataset_id=selected, clean_run_id=run_id, batch_date=batch,
                    tables={table: _summary(audit) for table, (_, audit) in prepared.items()})
    except Exception as error:
        if committing:
            # Rollback cannot establish whether server accepted COMMIT; never retry automatically.
            try:
                connection.rollback()
            except Exception:
                pass
            raise OdsError('DWD_COMMIT_UNKNOWN') from None
        _rollback(connection, error)
    finally:
        _close(connection)


@_safe('DWD_INTERNAL_ERROR')
def write_dwd(table, batch_date, *, dataset_id=None):
    if not isinstance(table, str) or table not in TABLES:
        raise OdsError('DWD_ARGUMENTS_INVALID') from None
    return _clean((table,), batch_date, dataset_id)['tables'][table]


@_safe('DWD_INTERNAL_ERROR')
def run_clean_all(batch_date, *, dataset_id=None):
    return _clean(tuple(TABLES), batch_date, dataset_id)


@_safe('DQ_INTERNAL_ERROR')
def load_quality_snapshot(batch_date=None, *, dataset_id=None):
    _arguments(batch_date, dataset_id, quality=True)
    config = _config()
    connection = _connect(quality=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            result = load_quality_snapshot_from_cursor(cursor, batch_date, dataset_id=dataset_id)
        connection.rollback()  # End read-only snapshot without publishing anything.
        return result
    except Exception as error:
        _rollback(connection, error, quality=True)
    finally:
        _close(connection)


def load_quality_snapshot_from_cursor(cursor, batch_date=None, *, dataset_id=None):
    """Read caller-owned snapshot; never controls transaction or cursor lifetime."""
    _arguments(batch_date, dataset_id, quality=True)
    config = _config()
    selected, _, _ = _registry(cursor, dataset_id)
    _verify(cursor)
    audits = _audits(cursor, selected)
    if set(audits) != set(TABLES):
        raise OdsError('DQ_SNAPSHOT_MISMATCH') from None
    identities = {(a['clean_run_id'], a['batch_date'], a['rules_version'], a['mapping_version'], a['privacy_key_id']) for a in audits.values()}
    if len(identities) != 1:
        raise OdsError('DQ_SNAPSHOT_MISMATCH') from None
    run_id, batch, rules, mapping, key_id = next(iter(identities))
    if ((batch_date is not None and str(batch) != batch_date)
            or rules != config['rules_version'] or mapping != course_mapping.version(selected)):
        raise OdsError('DQ_SNAPSHOT_MISMATCH') from None
    _day(str(batch))
    result = dict(schema_version='node07-snapshot-v1', dataset_id=selected, clean_run_id=run_id,
                  batch_date=str(batch), rules_version=rules, mapping_version=mapping,
                  privacy_key_id=key_id, tables={})
    for table, spec in TABLES.items():
        source = SOURCES[spec['source']]
        rows = _source(cursor, table, selected)
        columns = spec['columns']
        cursor.execute('SELECT ' + ','.join(f'`{col}`' for col in columns) + f" FROM `{table}` WHERE dataset_id=%s ORDER BY `{spec['id_column']}`", (selected,))
        dwd_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        result['tables'][table] = dict(source=dict(table=source['table'], columns=list(source['columns'] + META), rows=rows),
                                       dwd=dict(table=table, columns=list(columns), rows=dwd_rows), audit=audits[table])
    return result


_END_DATE_OMITTED = object()


def _build_date_range(start_date, end_date):
    if end_date is _END_DATE_OMITTED:
        if isinstance(start_date, (tuple, list)) and len(start_date) == 2:
            start_date, end_date = start_date
        else:
            end_date = start_date
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    return (start_date.isoformat() if isinstance(start_date, date) else start_date,
            end_date.isoformat() if isinstance(end_date, date) else end_date)


def build_dwd_visit(start_date, end_date=_END_DATE_OMITTED, *, dataset_id=None, batch_date=None, env='dev'):
    from etl.jobs.build_dwd import _public_partial, build_detail_frame
    if isinstance(start_date, pd.DataFrame) and end_date is _END_DATE_OMITTED:
        return build_detail_frame(start_date, dataset_id=dataset_id, scope='VISIT')
    start_date, end_date = _build_date_range(start_date, end_date)
    return _public_partial(start_date, end_date, dataset_id=dataset_id, batch_date=batch_date, env=env, scope='VISIT')


def build_dwd_charge(start_date, end_date=_END_DATE_OMITTED, *, dataset_id=None, batch_date=None, env='dev'):
    from etl.jobs.build_dwd import _public_partial, build_detail_frame
    if isinstance(start_date, pd.DataFrame) and end_date is _END_DATE_OMITTED:
        return build_detail_frame(start_date, dataset_id=dataset_id, scope='CHARGE')
    start_date, end_date = _build_date_range(start_date, end_date)
    return _public_partial(start_date, end_date, dataset_id=dataset_id, batch_date=batch_date, env=env, scope='CHARGE')
