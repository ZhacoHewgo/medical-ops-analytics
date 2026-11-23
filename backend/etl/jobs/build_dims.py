"""Four dimensions. No patient rows or exception text in public outputs."""
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from uuid import UUID

import pymysql

from etl.config import course_mapping
from etl.config.source_config import COURSE_DATASET, OdsError, PROJECT_ROOT, SOURCES, database_options
from etl.jobs import clean_dwd as clean, quality_check as quality
from etl.scheduler import scheduler

TEMPLATE_VERSION = 'node08-dim-template-v1'
MAPPING_VERSION = course_mapping.SIM_VERSION
SCHEMA_VERSION = 'node08-dimensions-v1'
SAFE_CODES = frozenset(('N8_ARGUMENTS_INVALID N8_SCHEMA_INVALID N8_MIGRATION_REQUIRED '
    'N8_INPUT_EMPTY N8_DATE_INVALID N8_DATE_RANGE_INVALID N8_DIMENSION_INVALID '
    'N8_UPSTREAM_GATE_FAILED N8_STALE N8_RECONCILE_FAILED N8_TX_RETRYABLE N8_TX_FAILED '
    'N8_ROLLBACK_FAILED N8_COMMIT_UNKNOWN N8_REPORT_PATH_INVALID N8_REPORT_IDENTITY_CONFLICT '
    'N8_REPORT_WRITE_FAILED N8_RUN_CONTEXT_ACTIVE N8_INTERNAL_ERROR').split()) | quality.PRIVACY_KEY_ERRORS
_SPECS = {
    'dim_department': [('dept_code','varchar(64)',False),('dept_name','varchar(128)',False),
        ('hierarchy_level','smallint',True),('parent_dept_code','varchar(64)',True),
        ('campus_code','varchar(64)',True),('campus_name','varchar(128)',True),('attribute_status','varchar(16)',False)],
    'dim_date': [('calendar_date','date',False),('calendar_year','smallint unsigned',False),
        ('calendar_month','tinyint unsigned',False),('day_of_month','tinyint unsigned',False),
        ('iso_week_year','smallint unsigned',False),('iso_week','tinyint unsigned',False),
        ('iso_weekday','tinyint unsigned',False),('is_weekend','tinyint unsigned',False),
        ('is_holiday','tinyint unsigned',True),('holiday_status','varchar(16)',False)],
    'dim_disease': [('disease_code','varchar(64)',False),('disease_name','varchar(128)',False),
        ('icd_code','varchar(64)',True),('drg_group','varchar(64)',True),('dip_group','varchar(64)',True),('attribute_status','varchar(16)',False)],
    'dim_fee_type': [('fee_type_code','varchar(64)',False),('fee_type_name','varchar(128)',False),
        ('source_supported','tinyint unsigned',False),('origin','varchar(16)',False)],
}
DATE_COLUMNS = dict(his_outpatient='visit_date',his_inpatient='admit_date',his_charge='fee_date',
                    emr_record='visit_date',lis_report='report_date',finance_excel='stat_date',inpatient_discharge='discharge_date')
_SNAPSHOT_KEYS = set('dataset_id clean_run_id batch_date rules_version mapping_version privacy_key_id source_fingerprint dates'.split())
_REPORT_KEYS = set('schema_version env dataset_id clean_run_id batch_date template_version source_fingerprint node07_report_sha256 dim_sha256 date_min date_max required_date_start required_date_end row_counts source_completeness engineering_status course_status generated_at code'.split())


def _fail(code):
    raise OdsError(code) from None


def _safe(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OdsError as error:
            _fail(error.code if error.code in SAFE_CODES else 'N8_INTERNAL_ERROR')
        except Exception:
            _fail('N8_INTERNAL_ERROR')
    return guarded


def _day(value):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        _fail('N8_DATE_INVALID')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        _fail('N8_DATE_INVALID')
    if result.year < 1000:
        _fail('N8_DATE_RANGE_INVALID')
    return result


def _shift(value, delta):
    year = value.year + delta
    if not 1000 <= year <= 9999:
        _fail('N8_DATE_RANGE_INVALID')
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year,day=28)


def _columns(table):
    return [('dataset_id','varchar(128)',False), *_SPECS[table],
            ('mapping_version','varchar(64)',False),('scope','varchar(16)',False)]


def _ddl():
    result=[]
    for table in _SPECS:
        parts=[f'`{col}` {typ.upper()} '+('NULL' if nullable else 'NOT NULL') for col,typ,nullable in _columns(table)]
        parts.append(f'PRIMARY KEY (`dataset_id`,`{_SPECS[table][0][0]}`)')
        result.append(f'CREATE TABLE `{table}` (\n  '+',\n  '.join(parts)+'\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')
    return result


def _verify(cursor, names=None):
    names=tuple(_SPECS if names is None else names)
    if not names: return
    placeholders=','.join(['%s']*len(names))
    where=f'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders})'
    cursor.execute('SELECT TABLE_NAME,ENGINE,TABLE_COLLATION FROM information_schema.TABLES '+where,names)
    if set(cursor.fetchall())!={(t,'InnoDB','utf8mb4_bin') for t in names}: _fail('N8_SCHEMA_INVALID')
    cursor.execute('SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,ORDINAL_POSITION,COLLATION_NAME FROM information_schema.COLUMNS '+where,names)
    expected={(t,c,typ,'YES' if null else 'NO',i,'utf8mb4_bin' if typ.startswith('varchar(') else None)
              for t in names for i,(c,typ,null) in enumerate(_columns(t),1)}
    if {(t,c,typ.lower(),null,i,coll) for t,c,typ,null,i,coll in cursor.fetchall()}!=expected: _fail('N8_SCHEMA_INVALID')
    cursor.execute('SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART,INDEX_TYPE FROM information_schema.STATISTICS '+where,names)
    expected={(t,'PRIMARY',0,i,c,None,'BTREE') for t in names for i,c in enumerate(('dataset_id',_SPECS[t][0][0]),1)}
    if set(cursor.fetchall())!=expected: _fail('N8_SCHEMA_INVALID')


@_safe
def initialize_dim_schema():
    path=PROJECT_ROOT/'sql/dim/dim_tables.sql'
    if path.is_symlink() or path.read_text(encoding='utf-8')!=';\n\n'.join(_ddl())+';\n': _fail('N8_SCHEMA_INVALID')
    connection=clean._connect()
    try:
        with connection.cursor() as cursor:
            names=tuple(_SPECS)
            cursor.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s,%s,%s,%s)',names)
            existing={row[0] for row in cursor.fetchall()}
            _verify(cursor,existing)
            for table,ddl in zip(names,_ddl()):
                if table not in existing: cursor.execute(ddl)
            _verify(cursor)
    finally: clean._close(connection)
    return True


@_safe
def verify_dim_schema():
    connection=clean._connect()
    try:
        with connection.cursor() as cursor: _verify(cursor)
    finally: clean._close(connection)
    return True


def _calendar_values(day):
    year, week, weekday = day.isocalendar()
    return dict(calendar_date=day.isoformat(), calendar_year=day.year,
                calendar_month=day.month, day_of_month=day.day,
                iso_week_year=year, iso_week=week, iso_weekday=weekday,
                is_weekend=int(weekday >= 6), is_holiday=None,
                holiday_status='UNKNOWN')


@_safe
def dimension_rows(snapshot):
    if type(snapshot) is not dict or set(snapshot)!=_SNAPSHOT_KEYS: _fail('N8_ARGUMENTS_INVALID')
    dataset = snapshot['dataset_id']
    version = course_mapping.version(dataset)
    course = dataset == COURSE_DATASET
    if (not quality._id(snapshot['dataset_id']) or not quality._id(snapshot['privacy_key_id'],64)
            or snapshot['rules_version']!='node07-rules-v1' or snapshot['mapping_version']!=version
            or type(snapshot['source_fingerprint']) is not str or not re.fullmatch('[0-9a-f]{64}',snapshot['source_fingerprint'])):
        _fail('N8_ARGUMENTS_INVALID')
    try:
        if str(UUID(snapshot['clean_run_id']))!=snapshot['clean_run_id']: _fail('N8_ARGUMENTS_INVALID')
    except (ValueError,TypeError,AttributeError): _fail('N8_ARGUMENTS_INVALID')
    _day(snapshot['batch_date'])
    dates=snapshot['dates']
    if type(dates) is not dict or set(dates)!=set(DATE_COLUMNS): _fail('N8_ARGUMENTS_INVALID')
    days=[]
    for key,values in dates.items():
        if type(values) is not list: _fail('N8_ARGUMENTS_INVALID')
        for value in values:
            if key=='inpatient_discharge' and value is None: continue
            days.append(_day(value))
    if not days: _fail('N8_INPUT_EMPTY')
    low,high=min(days),max(days); start,end=_shift(low,-1),_shift(high,1)
    rows={t:[] for t in _SPECS}
    def add(table,**values):
        values=dict(dataset_id=dataset,**values,mapping_version=version,scope='COURSE' if course else 'SIM')
        rows[table].append({c:values[c] for c,_,_ in _columns(table)})
    departments = ([(code, code) for code in course_mapping.DEPARTMENTS] if course else
                   [(f'SIM_DEPT_{i:02}', f'SIM_科室{i}') for i in range(1, 5)])
    diseases = ([(code, code) for code in course_mapping.DISEASE_CODES] if course else
                [(f'SIM_DX_{i:02}', f'SIM_病种{i}') for i in range(1, 6)])
    fees = ([(code, name, 1) for code, name in course_mapping.FEE_NAMES.items()] if course else
            [('SERVICE','SIM_服务费',1),('DRUG','SIM_药品费',1),('CONSUMABLE','SIM_耗材费',1),('REGISTRATION','SIM_挂号费',0),('EXAM','SIM_检查费',0),('TREATMENT','SIM_治疗费',0),('BED','SIM_床位费',0)])
    for code, name in departments:
        add('dim_department',dept_code=code,dept_name=name,hierarchy_level=None,parent_dept_code=None,campus_code=None,campus_name=None,attribute_status='UNKNOWN')
    for code, name in diseases:
        add('dim_disease',disease_code=code,disease_name=name,icd_code=None,drg_group=None,dip_group=None,attribute_status='UNKNOWN')
    for code, name, supported in fees:
        origin = 'COURSE' if course else ('SIM' if supported else 'COURSE_CATALOG')
        add('dim_fee_type',fee_type_code=code,fee_type_name=name,source_supported=supported,origin=origin)
    for offset in range((end-start).days+1):
        day=start+timedelta(days=offset)
        add('dim_date',**_calendar_values(day))
    for table in rows: rows[table].sort(key=lambda row:row[_SPECS[table][0][0]])
    return dict(schema_version=SCHEMA_VERSION,dataset_id=snapshot['dataset_id'],template_version=TEMPLATE_VERSION,
                date_min=low.isoformat(),date_max=high.isoformat(),required_date_start=start.isoformat(),required_date_end=end.isoformat(),rows=rows,
                row_counts={t:len(v) for t,v in rows.items()},source_completeness=dict(organization_attributes='UNKNOWN',disease_grouping='UNKNOWN',statutory_holidays='UNKNOWN',fee_classification='PARTIAL',unsupported_fee_catalog_rows=sum(not supported for _, _, supported in fees),service_unsplit_rows=None))


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _read_rows(cursor,dataset):
    result={}
    for table in _SPECS:
        columns=[c for c,_,_ in _columns(table)]
        cursor.execute('SELECT '+','.join(f'`{c}`' for c in columns)+f' FROM `{table}` WHERE dataset_id=%s ORDER BY `{columns[1]}`',(dataset,))
        result[table]=[{c:(v.isoformat() if isinstance(v,date) else v) for c,v in zip(columns,row)} for row in cursor.fetchall()]
    return result


def _dimension_report_identity(report, dataset, *, env=None, batch=None):
    try:
        if (type(report) is not dict or set(report) != _REPORT_KEYS
                or report['schema_version'] != SCHEMA_VERSION
                or report['dataset_id'] != dataset
                or report['template_version'] != TEMPLATE_VERSION
                or report['env'] not in ('dev', 'test')
                or env is not None and report['env'] != env
                or batch is not None and report['batch_date'] != batch
                or report['engineering_status'] != 'DIM_READY'
                or report['course_status'] != 'HOLD' or report['code'] != 'N8_DIM_OK'):
            _fail('N8_REPORT_IDENTITY_CONFLICT')
        if str(UUID(report['clean_run_id'])) != report['clean_run_id']:
            _fail('N8_REPORT_IDENTITY_CONFLICT')
        if datetime.fromisoformat(report['generated_at']).tzinfo is None:
            _fail('N8_REPORT_IDENTITY_CONFLICT')
        for key in ('batch_date', 'date_min', 'date_max', 'required_date_start', 'required_date_end'):
            _day(report[key])
        for key in ('source_fingerprint', 'node07_report_sha256', 'dim_sha256'):
            if type(report[key]) is not str or not re.fullmatch('[0-9a-f]{64}', report[key]):
                _fail('N8_REPORT_IDENTITY_CONFLICT')
        counts = report['row_counts']
        if type(counts) is not dict or set(counts) != set(_SPECS) or not all(quality._count(v) for v in counts.values()):
            _fail('N8_REPORT_IDENTITY_CONFLICT')
        completeness = report['source_completeness']
        expected = dict(organization_attributes='UNKNOWN', disease_grouping='UNKNOWN',
                        statutory_holidays='UNKNOWN', fee_classification='PARTIAL',
                        unsupported_fee_catalog_rows=0 if dataset == COURSE_DATASET else 4, service_unsplit_rows=0)
        if type(completeness) is not dict or set(completeness) != set(expected):
            _fail('N8_REPORT_IDENTITY_CONFLICT')
        if not quality._count(completeness['service_unsplit_rows']):
            _fail('N8_REPORT_IDENTITY_CONFLICT')
        expected['service_unsplit_rows'] = completeness['service_unsplit_rows']
        if not _same_shape_value(completeness, expected):
            _fail('N8_REPORT_IDENTITY_CONFLICT')
    except Exception:
        _fail('N8_REPORT_IDENTITY_CONFLICT')


def _certified_prior_rows(report):
    # This certificate authenticates old output only, never the new N7 source.
    try:
        if _day(report['date_min']) > _day(report['date_max']):
            _fail('N8_DIMENSION_INVALID')
        dates = {key: [] for key in DATE_COLUMNS}
        dates['his_outpatient'] = [report['date_min'], report['date_max']]
        prior = dimension_rows(dict(dataset_id=report['dataset_id'], clean_run_id=report['clean_run_id'],
            batch_date=report['batch_date'], rules_version='node07-rules-v1',
            mapping_version=course_mapping.version(report['dataset_id']), privacy_key_id='SIM-prior-output',
            source_fingerprint=report['source_fingerprint'], dates=dates))
        if any(report[key] != prior[key] for key in ('date_min', 'date_max', 'required_date_start', 'required_date_end')):
            _fail('N8_DIMENSION_INVALID')
        if not _same_shape_value(report['row_counts'], prior['row_counts']):
            _fail('N8_DIMENSION_INVALID')
        return prior['rows']
    except Exception:
        _fail('N8_DIMENSION_INVALID')


def _store(cursor, result, *, previous_report=None):
    _verify(cursor)
    existing = _read_rows(cursor, result['dataset_id'])
    if previous_report is not None:
        _dimension_report_identity(previous_report, result['dataset_id'])
    same = _same_shape_value(existing, result['rows'])
    if not same:
        if not any(existing.values()) and previous_report is None:
            pass  # First bootstrap: no data and no historical certificate.
        else:
            if previous_report is None or not all(existing.values()):
                _fail('N8_DIMENSION_INVALID')
            prior = _certified_prior_rows(previous_report)
            if (not _same_shape_value(existing, prior)
                    or _hash(existing) != previous_report['dim_sha256']):
                _fail('N8_DIMENSION_INVALID')
        _replace_dimension_rows(cursor, result)
    return _stored_dimension_result(cursor, result)


def _replace_dimension_rows(cursor, result):
    for table, rows in result['rows'].items():
        columns = [c for c, _, _ in _columns(table)]
        cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id=%s', (result['dataset_id'],))
        cursor.executemany(f'INSERT INTO `{table}` (' + ','.join(f'`{c}`' for c in columns)
            + ') VALUES (' + ','.join(['%s'] * len(columns)) + ')',
            [tuple(row[c] for c in columns) for row in rows])


def _stored_dimension_result(cursor, result):
    actual = _read_rows(cursor, result['dataset_id'])
    if not _same_shape_value(actual, result['rows']) or _hash(actual) != _hash(result['rows']):
        _fail('N8_DIMENSION_INVALID')
    return dict(template_version=TEMPLATE_VERSION, required_date_start=result['required_date_start'],
                required_date_end=result['required_date_end'], row_counts=result['row_counts'], dim_sha256=_hash(actual))


def _dimension_range(dataset_id, date_min, date_max):
    low=_day(date_min); high=_day(date_max)
    if low>high: _fail('N8_ARGUMENTS_INVALID')
    snapshot=dict(dataset_id=dataset_id,clean_run_id='00000000-0000-0000-0000-000000000000',batch_date=date_min,rules_version='node07-rules-v1',mapping_version=course_mapping.version(dataset_id),privacy_key_id='SIM-internal-template',source_fingerprint='0'*64,dates={k:[] for k in DATE_COLUMNS})
    snapshot['dates']['his_outpatient']=[date_min,date_max]
    return dimension_rows(snapshot)


def _bridge_certificate_rows(certificate, dataset_id):
    # Caller authenticates the projection's source; this checks only old output.
    keys = {'dataset_id', 'template_version', 'date_min', 'date_max',
            'required_date_start', 'required_date_end', 'row_counts', 'dim_sha256'}
    try:
        if type(certificate) is not dict or set(certificate) != keys:
            _fail('N8_DIMENSION_INVALID')
        if any(type(certificate[key]) is not str for key in keys - {'row_counts'}):
            _fail('N8_DIMENSION_INVALID')
        if (certificate['dataset_id'] != dataset_id or certificate['template_version'] != TEMPLATE_VERSION
                or not re.fullmatch('[0-9a-f]{64}', certificate['dim_sha256'])):
            _fail('N8_DIMENSION_INVALID')
        counts = certificate['row_counts']
        if (type(counts) is not dict or set(counts) != set(_SPECS)
                or not all(type(value) is int and value >= 0 for value in counts.values())):
            _fail('N8_DIMENSION_INVALID')
        prior = _dimension_range(dataset_id, certificate['date_min'], certificate['date_max'])
        if any(certificate[key] != prior[key] for key in ('date_min', 'date_max', 'required_date_start', 'required_date_end')):
            _fail('N8_DIMENSION_INVALID')
        if (not _same_shape_value(counts, prior['row_counts'])
                or certificate['dim_sha256'] != _hash(prior['rows'])):
            _fail('N8_DIMENSION_INVALID')
        return prior['rows']
    except Exception:
        _fail('N8_DIMENSION_INVALID')


@_safe
def prepare_dimensions(cursor, dataset_id, date_min, date_max, *, previous_certificate=None):
    result = _dimension_range(dataset_id, date_min, date_max)
    if previous_certificate is None:
        return _store(cursor, result)
    prior = _bridge_certificate_rows(previous_certificate, dataset_id)
    try:
        _verify(cursor)
    except OdsError as error:
        if error.code == 'N8_SCHEMA_INVALID':
            _fail('N8_DIMENSION_INVALID')
        raise
    existing = _read_rows(cursor, dataset_id)
    if (not _same_shape_value(existing, prior)
            or _hash(existing) != previous_certificate['dim_sha256']):
        _fail('N8_DIMENSION_INVALID')
    if not _same_shape_value(existing, result['rows']):
        _replace_dimension_rows(cursor, result)
    return _stored_dimension_result(cursor, result)


def _directory():
    """Open each path component without following symlinks; create log dirs only."""
    root = Path(quality._LOG_ROOT)
    if not root.is_absolute() or ".." in root.parts:
        _fail("N8_REPORT_PATH_INVALID")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(root.parts[1:], 1):
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                # Only the fixed logs root may be missing, not project ancestors.
                if index != len(root.parts) - 1:
                    _fail("N8_REPORT_PATH_INVALID")
                os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        _fail("N8_REPORT_PATH_INVALID")


def _write_report(report):
    directory = None; root = None; temporary = None
    try:
        root = _directory()
        env = report["env"]
        try:
            os.mkdir(env, mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        try:
            directory = os.open(env, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except OSError:
            _fail("N8_REPORT_PATH_INVALID")
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = "dimension_report_" + report["batch_date"].replace("-", "") + ".json"
        old_stat = None
        try:
            old = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            old = None
        except OSError:
            _fail("N8_REPORT_PATH_INVALID")
        if old is not None:
            with os.fdopen(old, "r", encoding="utf-8") as stream:
                old_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(old_stat.st_mode) or old_stat.st_nlink != 1:
                    _fail("N8_REPORT_PATH_INVALID")
                try:
                    if old_stat.st_size > 2_000_000:
                        _fail("N8_REPORT_IDENTITY_CONFLICT")
                    previous = json.load(stream)
                    identity = ("schema_version", "env", "dataset_id", "batch_date", "template_version")
                    if not isinstance(previous, dict) or any(previous.get(k) != report[k] for k in identity):
                        _fail("N8_REPORT_IDENTITY_CONFLICT")
                    if str(UUID(previous.get("clean_run_id"))) != previous.get("clean_run_id"):
                        _fail("N8_REPORT_IDENTITY_CONFLICT")
                except OdsError:
                    raise
                except Exception:
                    _fail("N8_REPORT_IDENTITY_CONFLICT")
        temporary = ".dimensions-" + secrets.token_hex(16) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode=0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(report, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
            temp_stat = os.fstat(stream.fileno())
        published = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode) or published.st_nlink != 1 or (published.st_dev, published.st_ino) != (temp_stat.st_dev, temp_stat.st_ino):
            _fail("N8_REPORT_PATH_INVALID")
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if (old_stat is None) != (current is None) or old_stat is not None and (current.st_dev, current.st_ino, current.st_nlink, current.st_mtime_ns, current.st_size) != (old_stat.st_dev, old_stat.st_ino, 1, old_stat.st_mtime_ns, old_stat.st_size):
            _fail("N8_REPORT_IDENTITY_CONFLICT")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
    except OdsError:
        raise
    except Exception:
        _fail("N8_REPORT_WRITE_FAILED")
    finally:
        if directory is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)
        if root is not None: os.close(root)




def _read_report(env,batch, *, dimension=False):
    root=directory=None
    try:
        root=_directory()
        directory=os.open(env,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
        name=('dimension_report_' if dimension else 'quality_report_')+batch.replace('-','')+'.json'
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>2_000_000: _fail('N8_REPORT_PATH_INVALID')
            raw=stream.read(2_000_001)
        report=json.loads(raw,parse_constant=lambda _: _fail('N8_UPSTREAM_GATE_FAILED'))
        return report,hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        if dimension: return None,None
        _fail('N8_UPSTREAM_GATE_FAILED')
    except OdsError: raise
    except OSError: _fail('N8_REPORT_PATH_INVALID')
    except Exception: _fail('N8_UPSTREAM_GATE_FAILED')
    finally:
        if directory is not None: os.close(directory)
        if root is not None: os.close(root)


def _same_shape_value(actual,expected):
    # Reject bool-as-count, NaN and extra fields before equality comparisons.
    if type(expected) is dict:
        return type(actual) is dict and set(actual)==set(expected) and all(_same_shape_value(actual[k],v) for k,v in expected.items())
    if type(expected) is list:
        return type(actual) is list and len(actual)==len(expected) and all(_same_shape_value(a,b) for a,b in zip(actual,expected))
    if type(expected) is float:
        return type(actual) in (int,float) and math.isfinite(actual) and actual==expected
    return type(actual) is type(expected) and actual==expected


def _upstream(snapshot,report,env,*,config=None,privacy_key=None):
    try:
        if type(report) is not dict or report.get('schema_version') not in quality.REPORT_VERSIONS:
            _fail('N8_UPSTREAM_GATE_FAILED')
        if config is None:
            config=quality._load_config()
        quality._validate_snapshot(snapshot,rules_version=config['rules_version'])
        rules=[]
        for table,kind in quality._CATALOG:
            entry=snapshot['tables'][table]
            if kind=='NULL': rule=quality._null_ratio(table,entry)
            elif kind=='DUPLICATE': rule=quality._duplicate(table,entry)
            elif kind=='VALUE_RANGE': rule=quality._value_range(table,entry)
            elif kind=='VOLUME_WAVE': rule=quality._volume_wave(table,entry,snapshot,config)
            elif kind=='RECONCILE': rule=quality._reconcile(table,entry)
            elif kind=='MAPPING': rule=quality._check_mapping(table,snapshot)
            else: rule=quality._check_privacy(table,snapshot,env=env,key=privacy_key)
            if rule['code']=='STALE': _fail('N8_STALE')
            if kind=='RECONCILE' and rule['status']!='PASS': _fail('N8_RECONCILE_FAILED')
            rules.append(rule)
        expected=quality._summary(snapshot,rules,env,schema_version=report['schema_version'])
        if set(report)!=set(expected): _fail('N8_UPSTREAM_GATE_FAILED')
        try: datetime.fromisoformat(report['generated_at'])
        except (ValueError,TypeError): _fail('N8_UPSTREAM_GATE_FAILED')
        expected['generated_at']=report['generated_at']
        if expected['gate_status']!='PASS' or not _same_shape_value(report,expected): _fail('N8_UPSTREAM_GATE_FAILED')
    except OdsError as error:
        if error.code in SAFE_CODES: raise
        _fail('N8_UPSTREAM_GATE_FAILED')


def _date_snapshot(cursor,snapshot):
    dates={}
    tables=snapshot.get('tables') if isinstance(snapshot,dict) else None
    by_source={spec['source']:name for name,spec in clean.TABLES.items()}
    for source,column in DATE_COLUMNS.items():
        if source=='inpatient_discharge': continue
        entry=tables.get(by_source.get(source)) if tables else None
        loaded=entry['source'].get('rows') if isinstance(entry,dict) and isinstance(entry.get('source'),dict) else None
        if loaded is not None:
            dates[source]=[row[column] for row in loaded]
            if source=='his_inpatient': dates['inpatient_discharge']=[None if row['discharge_date'] in (None,'') else row['discharge_date'] for row in loaded]
            continue
        table=SOURCES[source]['table']
        columns=[column]+(['discharge_date'] if source=='his_inpatient' else [])
        cursor.execute('SELECT '+','.join(f'`{c}`' for c in columns)+f' FROM `{table}` WHERE dataset_id=%s ORDER BY source_row_number',(snapshot['dataset_id'],))
        values=cursor.fetchall()
        dates[source]=[row[0] for row in values]
        if source=='his_inpatient': dates['inpatient_discharge']=[None if row[1] in (None,'') else row[1] for row in values]
    identity={k:snapshot[k] for k in ('dataset_id','clean_run_id','batch_date','rules_version','mapping_version','privacy_key_id')}
    fingerprints={t:{k:entry['audit'][k] for k in ('clean_run_id','source_sha256','result_sha256')}
                  for t,entry in snapshot['tables'].items()}
    return dict(**identity,dates=dates,source_fingerprint=_hash(dict(identity=identity,audits=fingerprints,dates=dates)))


def _refresh(dataset_id,batch_date,env):
    connection=None; committed=False; committing=False; rolled_back=False
    try:
        connection=clean._connect()
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            selected,registered_batch,registry=clean._registry(cursor,dataset_id)
            if batch_date is not None and batch_date!=registered_batch: _fail('N8_ARGUMENTS_INVALID')
            snapshot=clean.load_quality_snapshot_from_cursor(cursor,registered_batch,dataset_id=selected)
            report,report_hash=_read_report(env,registered_batch)
            _upstream(snapshot,report,env)
            dates=_date_snapshot(cursor,snapshot)
            result=dimension_rows(dates)
            service=sum(row['fee_type_code']=='SERVICE' for row in snapshot['tables']['dwd_charge_detail']['dwd']['rows'])
            result['source_completeness']['service_unsplit_rows']=service
            previous,_=_read_report(env,registered_batch,dimension=True)
            if previous is not None:
                _dimension_report_identity(previous, selected, env=env, batch=registered_batch)
            stored=_store(cursor,result,previous_report=previous)
            if clean._registry(cursor,selected,lock=True)!=(selected,registered_batch,registry): _fail('N8_STALE')
            committing=True
            connection.commit()
            committing=False; committed=True
        output=dict(schema_version=SCHEMA_VERSION,env=env,dataset_id=selected,clean_run_id=snapshot['clean_run_id'],batch_date=registered_batch,
                    template_version=TEMPLATE_VERSION,source_fingerprint=dates['source_fingerprint'],node07_report_sha256=report_hash,
                    dim_sha256=stored['dim_sha256'],date_min=result['date_min'],date_max=result['date_max'],
                    required_date_start=result['required_date_start'],required_date_end=result['required_date_end'],row_counts=result['row_counts'],
                    source_completeness=result['source_completeness'],engineering_status='DIM_READY',course_status='HOLD',generated_at=datetime.now(timezone.utc).isoformat(),code='N8_DIM_OK')
        assert set(output)==_REPORT_KEYS
        _write_report(output)
        return output
    except Exception as error:
        if committing: _fail('N8_COMMIT_UNKNOWN')
        if connection is not None and not committed:
            try:
                connection.rollback()
                rolled_back=True
            except Exception: _fail('N8_ROLLBACK_FAILED')
        if (isinstance(error, OdsError) and error.code in SAFE_CODES
                and error.code != 'N8_TX_RETRYABLE'):
            raise
        # Only a confirmed rollback makes transient transaction errors retryable.
        if (rolled_back and isinstance(error, pymysql.err.OperationalError)
                and error.args and error.args[0] in (1205, 1213, 2006, 2013)):
            _fail('N8_TX_RETRYABLE')
        _fail('N8_TX_FAILED' if not committed else 'N8_REPORT_WRITE_FAILED')
    finally:
        if connection is not None: clean._close(connection)


@_safe
def refresh_dimensions(*,dataset_id=None,batch_date=None,env='dev'):
    if type(env) is not str or env not in ('dev','test') or dataset_id is not None and not quality._id(dataset_id): _fail('N8_ARGUMENTS_INVALID')
    if batch_date is not None: _day(batch_date)
    try:
        if database_options()['database']!=('hospital_test' if env=='test' else 'hospital'): _fail('N8_ARGUMENTS_INVALID')
    except OdsError: _fail('N8_ARGUMENTS_INVALID')
    if scheduler._CURRENT.get() is not None: _fail('N8_RUN_CONTEXT_ACTIVE')
    try:
        with scheduler._lock(env,'run'):
            return _refresh(dataset_id,batch_date,env)
    except scheduler.EtlError as error:
        _fail('N8_RUN_CONTEXT_ACTIVE' if error.code=='ETL_RUN_LOCKED' else 'N8_REPORT_PATH_INVALID')
