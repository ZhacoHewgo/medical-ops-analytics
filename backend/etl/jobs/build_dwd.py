"""Node 8 fixed wide-table schema and typed value primitives.

No import-time I/O or DDL. ALL certification requires report and live freshness.
"""
from datetime import date, datetime
from decimal import Decimal, Context, localcontext
import hashlib
import json

from etl.config.source_config import PROJECT_ROOT, OdsError

LEGACY_SQL = PROJECT_ROOT / 'sql/dwd/dwd_tables.sql'
MODEL_VERSION = 'node08-model-v1'
SCHEMA_VERSION = 'node08-build-v1'
VISIT = 'dwd_visit_detail'
AUDIT = 'dwd_build_audit'
VISIT_SPEC = (
    ('dataset_id','varchar(128)',False), ('visit_type','varchar(64)',False),
    ('visit_id','varchar(128)',False), ('source_row_number','bigint unsigned',False),
    ('source_batch_date','date',False), ('etl_batch_date','date',False),
    ('etl_load_time','datetime(6)',False), ('patient_key','char(64)',True),
    ('visit_date','date',False), ('admit_date','date',True), ('discharge_date','date',True),
    ('dept_code','varchar(64)',False), ('disease_code','varchar(64)',True),
    ('gender','varchar(16)',False), ('age','smallint',True),
    ('source_stay_days','decimal(10,2)',True), ('source_total_fee','decimal(18,2)',True),
    ('source_drug_fee','decimal(18,2)',True), ('source_consumable_fee','decimal(18,2)',True),
    ('residual_fee','decimal(18,2)',True), ('calendar_span_days','int',True),
)
AUDIT_SPEC = tuple(zip(
    'dataset_id build_run_id clean_run_id batch_date requested_start requested_end effective_start effective_end model_version mapping_version privacy_key_id build_scope input_sha256 dim_sha256 visit_sha256 charge_sha256 stats_json updated_at'.split(),
    ('varchar(128)','char(36)','char(36)','date','date','date','date','date','varchar(64)','varchar(64)','varchar(64)','varchar(16)','char(64)','char(64)','char(64)','char(64)','json','datetime(6)'),
    (False,False,False,False,True,True,False,False,False,False,False,False,False,False,True,True,False,False)))
VISIT_COLUMNS = tuple(c for c, _, _ in VISIT_SPEC)
AUDIT_COLUMNS = tuple(c for c, _, _ in AUDIT_SPEC)
INDEXES = {
    VISIT: {'PRIMARY': (0, ('dataset_id','visit_type','visit_id')),
            'ix_visit_date': (1, ('dataset_id','visit_date')),
            'ix_visit_department_date': (1, ('dataset_id','dept_code','visit_date'))},
    AUDIT: {'PRIMARY': (0, ('dataset_id',))},
}


def new_ddl():
    statements = []
    for table, spec in ((VISIT,VISIT_SPEC),(AUDIT,AUDIT_SPEC)):
        parts = [f'`{c}` {t.upper()} ' + ('NULL' if n else 'NOT NULL') for c,t,n in spec]
        for name, (_, columns) in INDEXES[table].items():
            label = 'PRIMARY KEY' if name == 'PRIMARY' else f'KEY `{name}`'
            parts.append(label+' ('+','.join(f'`{c}`' for c in columns)+')')
        statements.append(f'CREATE TABLE IF NOT EXISTS `{table}` (\n  '+',\n  '.join(parts)+'\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')
    return statements


def _typed(value):
    if value is None:
        return ['null']
    if type(value) is Decimal:
        if not value.is_finite():
            raise OdsError('N8_AMOUNT_OVERFLOW') from None
        return ['decimal', format(value, 'f')]
    if type(value) is datetime:
        if value.tzinfo is not None:
            raise OdsError('N8_DATE_INVALID') from None
        return ['datetime', value.isoformat(timespec='microseconds')]
    if type(value) is date:
        return ['date', value.isoformat()]
    if type(value) is str:
        return ['str', value]
    if type(value) is int:
        return ['int', str(value)]
    raise OdsError('N8_INTERNAL_ERROR') from None


def rows_digest(columns, rows, primary_key):
    ordered = sorted(rows, key=lambda row: tuple(str(row[c]).encode('utf-8') for c in primary_key))
    payload = [list(columns), [[_typed(row[c]) for c in columns] for row in ordered]]
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def visit_candidates(outpatients, inpatients):
    """Copy verified N7 values; never clean patient identities or source metadata again."""
    result = []
    seen = set()
    for kind, source in (('OUTPATIENT',outpatients),('INPATIENT',inpatients)):
        for row in source:
            if kind == 'OUTPATIENT' and row['visit_type'] != kind:
                raise OdsError('N8_VISIT_REFERENCE_INVALID') from None
            item = {c: row[c] for c in ('dataset_id','visit_id','source_row_number','source_batch_date','etl_batch_date','etl_load_time','patient_key','dept_code','gender','age')}
            key = (item['dataset_id'],kind,item['visit_id'])
            if key in seen:
                raise OdsError('N8_VISIT_AMBIGUOUS') from None
            seen.add(key)
            inpatient = kind == 'INPATIENT'
            total = row['total_fee'] if inpatient else row['fee_amount']
            drug = row['drug_fee'] if inpatient else None
            consumable = row['consumable_fee'] if inpatient else None
            residual = None
            if inpatient and all(v is not None for v in (total,drug,consumable)):
                if any(type(v) is not Decimal or not v.is_finite() for v in (total,drug,consumable)):
                    raise OdsError('N8_AMOUNT_OVERFLOW') from None
                residual = _residual(total,drug,consumable)
            admit = row['admit_date'] if inpatient else None
            discharge = row['discharge_date'] if inpatient else None
            span = (discharge-admit).days if admit is not None and discharge is not None else None
            if span is not None and span < 0:
                raise OdsError('N8_DATE_INVALID') from None
            item.update(visit_type=kind,visit_date=admit if inpatient else row['visit_date'],admit_date=admit,
                        discharge_date=discharge,disease_code=row['disease_code'] if inpatient else None,
                        source_stay_days=row['stay_days'] if inpatient else None,source_total_fee=total,
                        source_drug_fee=drug,source_consumable_fee=consumable,residual_fee=residual,calendar_span_days=span)
            result.append(item)
    return sorted(result,key=lambda row: tuple(row[c].encode('utf-8') for c in ('dataset_id','visit_type','visit_id')))


def charge_candidates(charges, visits):
    parents = {}
    for row in visits:
        key = tuple(row[c] for c in ('dataset_id','visit_type','visit_id'))
        if key in parents:
            raise OdsError('N8_VISIT_AMBIGUOUS') from None
        parents[key] = row
    result = []
    for row in charges:
        key = tuple(row[c] for c in ('dataset_id','visit_type','visit_id'))
        if row['visit_type'] not in ('OUTPATIENT','INPATIENT') or not row['visit_id'] or key not in parents:
            raise OdsError('N8_VISIT_REFERENCE_INVALID') from None
        parent = parents[key]
        if row['patient_key'] and parent['patient_key'] and row['patient_key'] != parent['patient_key']:
            raise OdsError('N8_PATIENT_MISMATCH') from None
        result.append(dict(row))
    return result


def full_range(start, end, low, high):
    from etl.jobs import build_dims as dims
    if start is None and end is None:
        return low, high
    try:
        if type(start) is not str or type(end) is not str or dims._day(start) > dims._day(end):
            raise ValueError
    except Exception:
        raise OdsError('N8_ARGUMENTS_INVALID') from None
    if start > low or end < high:
        raise OdsError('N8_RANGE_NOT_FULL') from None
    return low, high


def _transaction(connection, work):
    import pymysql
    try:
        result = work()
    except Exception as error:
        try:
            connection.rollback()
        except Exception:
            raise OdsError('N8_ROLLBACK_FAILED') from None
        if isinstance(error,OdsError) and error.code in SAFE_CODES and error.code != 'N8_TX_RETRYABLE':
            raise OdsError(error.code) from None
        if isinstance(error,pymysql.err.ProgrammingError) and error.args and error.args[0]==1146:
            raise OdsError('N8_MIGRATION_REQUIRED') from None
        if isinstance(error,pymysql.err.OperationalError) and error.args and error.args[0] in (1205,1213,2006,2013):
            raise OdsError('N8_TX_RETRYABLE') from None
        raise OdsError('N8_TX_FAILED') from None
    try:
        connection.commit()
    except Exception:
        raise OdsError('N8_COMMIT_UNKNOWN') from None
    return result


SAFE_CODES = frozenset(('N8_ARGUMENTS_INVALID N8_RANGE_NOT_FULL N8_SCHEMA_INVALID N8_MIGRATION_REQUIRED N8_INPUT_EMPTY N8_DATE_INVALID N8_DATE_RANGE_INVALID N8_DIMENSION_INVALID N8_UNKNOWN_CODE N8_VISIT_REFERENCE_INVALID N8_VISIT_AMBIGUOUS N8_PATIENT_MISMATCH N8_UPSTREAM_GATE_FAILED N8_STALE N8_RECONCILE_FAILED N8_AMOUNT_OVERFLOW N8_TX_RETRYABLE N8_TX_FAILED N8_ROLLBACK_FAILED N8_COMMIT_UNKNOWN N8_REPORT_PATH_INVALID N8_REPORT_IDENTITY_CONFLICT N8_REPORT_WRITE_FAILED N8_RUN_CONTEXT_ACTIVE N8_INTERNAL_ERROR').split())


def _verify_new(cursor, names=(VISIT,AUDIT)):
    if not names:
        return
    specs = {VISIT:VISIT_SPEC,AUDIT:AUDIT_SPEC}
    names = tuple(names)
    where = 'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('+','.join(['%s']*len(names))+')'
    cursor.execute('SELECT TABLE_NAME,ENGINE,TABLE_COLLATION FROM information_schema.TABLES '+where,names)
    if set(cursor.fetchall()) != {(t,'InnoDB','utf8mb4_bin') for t in names}:
        raise OdsError('N8_SCHEMA_INVALID') from None
    cursor.execute('SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,ORDINAL_POSITION,COLLATION_NAME FROM information_schema.COLUMNS '+where,names)
    expected = {(t,c,typ,'YES' if null else 'NO',i,'utf8mb4_bin' if typ.startswith(('char(','varchar(')) else None) for t in names for i,(c,typ,null) in enumerate(specs[t],1)}
    if {(t,c,typ.lower(),null,i,coll) for t,c,typ,null,i,coll in cursor.fetchall()} != expected:
        raise OdsError('N8_SCHEMA_INVALID') from None
    cursor.execute('SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART,INDEX_TYPE FROM information_schema.STATISTICS '+where,names)
    expected = {(t,n,u,i,c,None,'BTREE') for t in names for n,(u,cols) in INDEXES[t].items() for i,c in enumerate(cols,1)}
    if set(cursor.fetchall()) != expected:
        raise OdsError('N8_SCHEMA_INVALID') from None


def _verify_schema(cursor):
    from etl.jobs import clean_dwd as clean, build_dims as dims
    try:
        clean._verify(cursor)
        dims._verify(cursor)
        _verify_new(cursor)
    except OdsError:
        raise OdsError('N8_SCHEMA_INVALID') from None


def verify_schema():
    from etl.jobs import clean_dwd as clean
    connection = None
    try:
        connection = clean._connect()
        with connection.cursor() as cursor:
            _verify_schema(cursor)
        connection.rollback()
    except Exception:
        raise OdsError('N8_SCHEMA_INVALID') from None
    finally:
        if connection is not None: clean._close(connection)
    return True


def initialize_schema():
    from etl.jobs import clean_dwd as clean, build_dims as dims
    connection = None
    try:
        connection = clean._connect()
        with connection.cursor() as cursor:
            clean._verify(cursor)
            dims._verify(cursor)
            cursor.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s,%s)',(VISIT,AUDIT))
            existing = {row[0] for row in cursor.fetchall()}
            _verify_new(cursor,existing)
            for table, ddl in zip((VISIT,AUDIT),new_ddl()):
                if table not in existing:
                    cursor.execute(ddl)
            _verify_schema(cursor)
    except Exception:
        raise OdsError('N8_SCHEMA_INVALID') from None
    finally:
        if connection is not None: clean._close(connection)
    return True


def _lock_inputs(cursor, dataset):
    from etl.jobs import clean_dwd as clean, build_dims as dims
    from etl.config.source_config import SOURCES
    # Current reads precede every consistent read, including identity resolution.
    cursor.execute('SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name FOR UPDATE')
    registry = tuple(tuple(row) for row in cursor.fetchall())
    if len(registry) != 6 or {row[0] for row in registry} != set(SOURCES):
        raise OdsError('N8_UPSTREAM_GATE_FAILED') from None
    identities = {(row[3],str(row[4])) for row in registry}
    if len(identities) != 1:
        raise OdsError('N8_UPSTREAM_GATE_FAILED') from None
    selected,batch = next(iter(identities))
    if dataset is not None and dataset != selected:
        raise OdsError('N8_ARGUMENTS_INVALID') from None
    groups = [(clean.AUDIT,),tuple(sorted(s['table'] for s in SOURCES.values())),tuple(sorted(clean.TABLES)),tuple(sorted(dims._SPECS)),(VISIT,),(AUDIT,)]
    for group in groups:
        for table in group:
            if table==clean.AUDIT: keys=('dataset_id','table_name')
            elif table in clean.TABLES: keys=('dataset_id',clean.TABLES[table]['id_column'])
            elif table in dims._SPECS: keys=('dataset_id',dims._SPECS[table][0][0])
            elif table==VISIT: keys=('dataset_id','visit_type','visit_id')
            elif table==AUDIT: keys=('dataset_id',)
            else: keys=('dataset_id','source_row_number')
            order=','.join(keys)
            cursor.execute(f'SELECT dataset_id FROM `{table}` WHERE dataset_id=%s ORDER BY {order} FOR UPDATE',(selected,))
            cursor.fetchall()
    return selected,batch,registry


def _stats(snapshot, visits, charges, desired):
    from etl.jobs import build_dims as dims
    result = {name:{} for name in ('ods_rows','base_rows','duplicate_removed','quarantined_rows')}
    for table, entry in snapshot['tables'].items():
        a = entry['audit']
        for name,key in (('ods_rows','source_rows'),('base_rows','accepted_rows'),('duplicate_removed','duplicate_removed'),('quarantined_rows','quarantined_rows')):
            result[name][table] = a[key]
        if a['source_rows'] != a['accepted_rows']+a['duplicate_removed']+a['quarantined_rows']:
            raise OdsError('N8_RECONCILE_FAILED') from None
    associations = {k:dict(slots=0,matched=0,not_applicable=0) for k in ('department','date','disease','fee_type','visit')}
    lookups = {k:{r[c] for r in desired['rows'][t]} for k,t,c in [('department','dim_department','dept_code'),('date','dim_date','calendar_date'),('disease','dim_disease','disease_code'),('fee_type','dim_fee_type','fee_type_code')]}
    def match(kind,value):
        associations[kind]['slots'] += 1
        value = value.isoformat() if type(value) is date else value
        if value not in lookups[kind]:
            raise OdsError('N8_UNKNOWN_CODE') from None
        associations[kind]['matched'] += 1
    for row in visits:
        match('department',row['dept_code']); match('date',row['visit_date'])
        if row['visit_type'] == 'INPATIENT':
            match('disease',row['disease_code'])
            if row['discharge_date'] is not None: match('date',row['discharge_date'])
        else: associations['disease']['not_applicable'] += 1
    for row in charges:
        match('department',row['dept_code']); match('date',row['fee_date']); match('fee_type',row['fee_type_code'])
        associations['visit']['slots'] += 1; associations['visit']['matched'] += 1
    ips = [r for r in visits if r['visit_type']=='INPATIENT']
    result.update(visit_rows=len(visits),charge_rows=len(charges),missing_total_rows=sum(r['source_total_fee'] is None for r in visits),missing_component_rows=sum(r['source_drug_fee'] is None or r['source_consumable_fee'] is None for r in ips),negative_residual_rows=sum(r['residual_fee'] is not None and r['residual_fee']<0 for r in ips),missing_calendar_span_rows=sum(r['calendar_span_days'] is None for r in ips),disease_not_applicable_rows=len(visits)-len(ips),association_counts=associations)
    return result


def _replace(cursor,table,columns,rows,dataset):
    cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id=%s',(dataset,))
    if rows:
        cursor.executemany(f'INSERT INTO `{table}` ('+','.join(f'`{c}`' for c in columns)+') VALUES ('+','.join(['%s']*len(columns))+')',[tuple(json.dumps(r[c],ensure_ascii=False,separators=(',',':')) if c=='stats_json' else r[c] for c in columns) for r in rows])


def _read(cursor,table,columns,dataset):
    cursor.execute('SELECT '+','.join(f'`{c}`' for c in columns)+f' FROM `{table}` WHERE dataset_id=%s',(dataset,))
    return [dict(zip(columns,row)) for row in cursor.fetchall()]


def _charge_rows(cursor,snapshot,columns,dataset):
    """Reuse the charge rows this transaction already read when the shape matches.

    load_quality_snapshot_from_cursor selects the same column list from
    dwd_charge_detail on the caller's cursor, and every caller opens the
    transaction with REPEATABLE READ, so both reads resolve to one snapshot of one
    table.  The declared column tuple is compared first: a snapshot that does not
    state the certified columns keeps the separate read instead of being trusted.
    """
    tables=snapshot.get('tables') if isinstance(snapshot,dict) else None
    declared=((tables or {}).get('dwd_charge_detail') or {}).get('dwd') or {}
    if tuple(declared.get('columns') or ())!=columns or 'rows' not in declared:
        return _read(cursor,'dwd_charge_detail',columns,dataset)
    return declared['rows']


def _previous_certificate(cursor,dataset,batch,env,audit_rows=None,dim_rows=None):
    from etl.jobs import build_dims as dims
    from uuid import UUID
    import re
    rows = _read(cursor,AUDIT,AUDIT_COLUMNS,dataset) if audit_rows is None else audit_rows
    if rows:
        try:
            if len(rows)!=1: raise ValueError
            a=rows[0]
            if a['dataset_id']!=dataset or a['model_version']!=MODEL_VERSION or a['mapping_version']!=dims.MAPPING_VERSION: raise ValueError
            for key in ('build_run_id','clean_run_id'):
                if type(a[key]) is not str or str(UUID(a[key]))!=a[key]: raise ValueError
            for key in ('batch_date','effective_start','effective_end'):
                if type(a[key]) is not date: raise ValueError
            if a['effective_start']>a['effective_end']: raise ValueError
            if type(a['updated_at']) is not datetime or a['updated_at'].tzinfo is not None: raise ValueError
            if not dims.quality._id(a['privacy_key_id'],64): raise ValueError
            scope=a['build_scope']
            if scope not in ('ALL','VISIT','CHARGE'): raise ValueError
            for key in ('input_sha256','dim_sha256','visit_sha256','charge_sha256'):
                required=key in ('input_sha256','dim_sha256') or scope=='ALL' or (key=='visit_sha256' and scope=='VISIT') or (key=='charge_sha256' and scope=='CHARGE')
                if required:
                    if type(a[key]) is not str or not re.fullmatch('[0-9a-f]{64}',a[key]): raise ValueError
                elif a[key] is not None: raise ValueError
            if (a['requested_start'] is None)!=(a['requested_end'] is None): raise ValueError
            if a['requested_start'] is not None:
                if type(a['requested_start']) is not date or type(a['requested_end']) is not date or a['requested_start']>a['effective_start'] or a['requested_end']<a['effective_end']: raise ValueError
            stats=json.loads(a['stats_json']) if isinstance(a['stats_json'],(str,bytes)) else a['stats_json']
            from etl.jobs import clean_dwd as clean
            keys=set('ods_rows base_rows duplicate_removed quarantined_rows visit_rows charge_rows missing_total_rows missing_component_rows negative_residual_rows missing_calendar_span_rows disease_not_applicable_rows association_counts'.split())
            if type(stats) is not dict or set(stats)!=keys: raise ValueError
            for key in keys-{'association_counts','ods_rows','base_rows','duplicate_removed','quarantined_rows'}:
                if type(stats[key]) is not int or stats[key]<0: raise ValueError
            for key in ('ods_rows','base_rows','duplicate_removed','quarantined_rows'):
                if type(stats[key]) is not dict or set(stats[key])!=set(clean.TABLES) or any(type(v) is not int or v<0 for v in stats[key].values()): raise ValueError
            associations=stats['association_counts']
            if type(associations) is not dict or set(associations)!=set(('department','date','disease','fee_type','visit')): raise ValueError
            for v in associations.values():
                if type(v) is not dict or set(v)!=set(('slots','matched','not_applicable')) or any(type(n) is not int or n<0 for n in v.values()) or v['slots']!=v['matched']: raise ValueError
            _validate_stats_shape(stats)
            old=dims._dimension_range(dataset,a['effective_start'].isoformat(),a['effective_end'].isoformat())
            cert={k:old[k] for k in ('dataset_id','template_version','date_min','date_max','required_date_start','required_date_end','row_counts')}
            cert['dim_sha256']=a['dim_sha256']
            prior=dims._bridge_certificate_rows(cert,dataset)
            if not dims._same_shape_value(dims._read_rows(cursor,dataset) if dim_rows is None else dim_rows,prior): raise ValueError
            return cert
        except Exception:
            raise OdsError('N8_DIMENSION_INVALID') from None
    report,_=dims._read_report(env,batch,dimension=True)
    if report is None: return None
    dims._dimension_report_identity(report,dataset,env=env,batch=batch)
    cert={k:report[k] for k in ('dataset_id','template_version','date_min','date_max','required_date_start','required_date_end','row_counts','dim_sha256')}
    prior=dims._bridge_certificate_rows(cert,dataset)
    if not dims._same_shape_value(dims._read_rows(cursor,dataset) if dim_rows is None else dim_rows,prior):
        raise OdsError('N8_DIMENSION_INVALID') from None
    return cert


def _build_work(cursor,start,end,*,dataset_id,batch_date,env,scope,pipeline=None):
    from etl.jobs import clean_dwd as clean, build_dims as dims
    from uuid import uuid4
    if scope not in ('ALL','VISIT','CHARGE'): raise OdsError('N8_ARGUMENTS_INVALID') from None
    cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
    cursor.execute('START TRANSACTION')
    selected,batch,registry=_lock_inputs(cursor,dataset_id)
    if batch_date is not None and batch_date!=batch: raise OdsError('N8_ARGUMENTS_INVALID') from None
    _verify_schema(cursor)
    if pipeline is not None:
        from etl.config.source_config import CONFIG_FIELDS
        frozen = {name: dict(zip(CONFIG_FIELDS, map(str, values))) for name, *values in registry}
        if frozen != pipeline['source_infos']:
            raise OdsError('N8_STALE') from None
    snapshot=clean.load_quality_snapshot_from_cursor(cursor,batch,dataset_id=selected)
    if pipeline is not None and (
        any(snapshot[key] != pipeline[key] for key in ('clean_run_id','privacy_key_id'))
        or {table: entry['audit']['accepted_rows'] for table, entry in snapshot['tables'].items()}
           != pipeline['accepted_rows']
    ):
        raise OdsError('N8_STALE') from None
    report,_=_read_quality_report(env,batch)
    dims._upstream(snapshot,report,env)
    dates=dims._date_snapshot(cursor,snapshot)
    desired=dims.dimension_rows(dates)
    low,high=full_range(start,end,desired['date_min'],desired['date_max'])
    if scope=='ALL': _report_preflight(env,batch,selected)
    certificate=_previous_certificate(cursor,selected,batch,env)
    tables=snapshot['tables']
    visits=visit_candidates(tables['dwd_outpatient_visit_detail']['dwd']['rows'],tables['dwd_inpatient_visit_detail']['dwd']['rows'])
    charges=charge_candidates(tables['dwd_charge_detail']['dwd']['rows'],visits)
    stats=_stats(snapshot,visits,charges,desired)
    # Bind all audit values, including stats/timestamps, not only N7 result hashes.
    audits=[[t,[[k,_typed(v) if k!='stats_json' else ['json',v]] for k,v in sorted(entry['audit'].items())]] for t,entry in sorted(tables.items())]
    input_hash=dims._hash(dict(audits=audits,dates=dates))
    selected_tables={}
    if scope in ('ALL','VISIT'): selected_tables[VISIT]=(VISIT_COLUMNS,visits,('dataset_id','visit_type','visit_id'))
    charge_columns=tuple(clean.TABLES['dwd_charge_detail']['columns'])
    if scope in ('ALL','CHARGE'): selected_tables['dwd_charge_detail']=(charge_columns,charges,('dataset_id','charge_id'))
    hashes=dict(input=input_hash,dim=None,visit=None,charge=None)
    for table,(columns,rows,pk) in selected_tables.items():
        digest_columns=tuple(c for c in columns if c!='etl_load_time') if table==VISIT else columns
        hashes['visit' if table==VISIT else 'charge']=rows_digest(digest_columns,rows,pk)
    # Preflight complete: all business/association/certificate/range checks precede DML.
    dim=dims.prepare_dimensions(cursor,selected,low,high,previous_certificate=certificate)
    hashes['dim']=dim['dim_sha256']
    for table,(columns,rows,pk) in selected_tables.items():
        _replace(cursor,table,columns,rows,selected)
        actual=_read(cursor,table,columns,selected)
        _conserve(columns,actual,rows,pk)
    run=str(uuid4())
    audit=dict(dataset_id=selected,build_run_id=run,clean_run_id=snapshot['clean_run_id'],batch_date=date.fromisoformat(batch),requested_start=None if start is None else date.fromisoformat(start),requested_end=None if end is None else date.fromisoformat(end),effective_start=date.fromisoformat(low),effective_end=date.fromisoformat(high),model_version=MODEL_VERSION,mapping_version=snapshot['mapping_version'],privacy_key_id=snapshot['privacy_key_id'],build_scope=scope,input_sha256=hashes['input'],dim_sha256=hashes['dim'],visit_sha256=hashes['visit'],charge_sha256=hashes['charge'],stats_json=stats,updated_at=datetime.now().replace(tzinfo=None))
    _replace(cursor,AUDIT,AUDIT_COLUMNS,[audit],selected)
    persisted=_read(cursor,AUDIT,AUDIT_COLUMNS,selected)
    if len(persisted)!=1: raise OdsError('N8_RECONCILE_FAILED') from None
    expected=dict(audit); actual=dict(persisted[0])
    if isinstance(actual['stats_json'],(str,bytes)): actual['stats_json']=json.loads(actual['stats_json'])
    if not dims._same_shape_value(actual,expected): raise OdsError('N8_RECONCILE_FAILED') from None
    if clean._registry(cursor,selected)[2]!=registry: raise OdsError('N8_STALE') from None
    final=clean.load_quality_snapshot_from_cursor(cursor,batch,dataset_id=selected)
    dims._upstream(final,report,env)
    if final!=snapshot or dims._date_snapshot(cursor,final)!=dates: raise OdsError('N8_STALE') from None
    return dict(schema_version=SCHEMA_VERSION,dataset_id=selected,build_run_id=run,clean_run_id=snapshot['clean_run_id'],batch_date=batch,requested_start=start,requested_end=end,effective_start=low,effective_end=high,build_scope=scope,gate_status='NOT_CERTIFIED',rows={t:len(v[1]) for t,v in selected_tables.items()},hashes=hashes,stats=stats)


def _run_build_locked(start_date=None,end_date=None,*,dataset_id=None,batch_date=None,env='dev',scope='ALL',pipeline=None):
    """Caller must hold existing env run flock; owns one transaction, never nests public APIs.

    Returns NOT_CERTIFIED even for ALL; public orchestration publishes and rechecks.
    """
    from etl.jobs import clean_dwd as clean
    try:
        connection=clean._connect()
    except Exception:
        raise OdsError('N8_TX_FAILED') from None
    try:
        def work():
            with connection.cursor() as cursor:
                return _build_work(cursor,start_date,end_date,dataset_id=dataset_id,batch_date=batch_date,env=env,scope=scope,pipeline=pipeline)
        return _transaction(connection,work)
    finally:
        clean._close(connection)


def _public_partial(start,end,*,dataset_id,batch_date,env,scope):
    from etl.jobs import build_dims as dims
    from etl.scheduler import scheduler
    from etl.config.source_config import database_options
    try:
        if type(env) is not str or env not in ('dev','test') or (dataset_id is not None and not dims.quality._id(dataset_id)): raise ValueError
        if batch_date is not None: dims._day(batch_date)
        if start is not None or end is not None:
            if start is None or end is None or dims._day(start)>dims._day(end): raise ValueError
        if database_options()['database']!=('hospital_test' if env=='test' else 'hospital'): raise ValueError
    except Exception: raise OdsError('N8_ARGUMENTS_INVALID') from None
    if scheduler._CURRENT.get() is not None: raise OdsError('N8_RUN_CONTEXT_ACTIVE') from None
    try:
        with scheduler._lock(env,'run'):
            return _run_build_locked(start,end,dataset_id=dataset_id,batch_date=batch_date,env=env,scope=scope)
    except scheduler.EtlError: raise OdsError('N8_RUN_CONTEXT_ACTIVE') from None
    except OdsError as error:
        raise OdsError(error.code if error.code in SAFE_CODES else 'N8_INTERNAL_ERROR') from None
    except Exception:
        raise OdsError('N8_INTERNAL_ERROR') from None


_MONEY_CONTEXT = Context(prec=64,Emin=-999999,Emax=999999)


def _residual(total,drug,consumable):
    try:
        with localcontext(_MONEY_CONTEXT):
            result=total-drug-consumable
            if not result.is_finite() or result.copy_abs()>Decimal('9999999999999999.99'):
                raise ValueError
            rounded=result.quantize(Decimal('.01'))
            if rounded!=result: raise ValueError
            return rounded
    except Exception:
        raise OdsError('N8_AMOUNT_OVERFLOW') from None


CHECKS = tuple('schema dimensions keys associations reconcile amount_null_conservation input_freshness output_freshness'.split())
_RETURN_KEYS=set('schema_version dataset_id build_run_id clean_run_id batch_date requested_start requested_end effective_start effective_end build_scope gate_status rows hashes stats'.split())
_REPORT_KEYS=_RETURN_KEYS|set('env generated_at model_version mapping_version privacy_key_id node07_report_sha256 checks code'.split())


def _report_name(batch):
    from etl.jobs import build_dims as dims
    dims._day(batch)
    return 'dwd_build_report_'+batch.replace('-','')+'.json'


def _report_directory(env):
    import os
    from etl.jobs import build_dims as dims
    if type(env) is not str or env not in ('dev','test'):
        raise OdsError('N8_REPORT_PATH_INVALID') from None
    root=dims._directory()
    try:
        try: os.mkdir(env,0o700,dir_fd=root)
        except FileExistsError: pass
        return os.open(env,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
    except Exception:
        raise OdsError('N8_REPORT_PATH_INVALID') from None
    finally: os.close(root)


def _validate_report(report):
    from uuid import UUID
    import re, math
    from etl.jobs import build_dims as dims
    try:
        if type(report) is not dict or set(report)!=_REPORT_KEYS: raise ValueError
        if report['schema_version']!=SCHEMA_VERSION or report['model_version']!=MODEL_VERSION or report['mapping_version']!=dims.MAPPING_VERSION: raise ValueError
        if report['env'] not in ('dev','test') or not dims.quality._id(report['dataset_id']): raise ValueError
        if report['build_scope']!='ALL' or report['gate_status']!='PASS' or report['code']!='N8_BUILD_OK': raise ValueError
        for k in ('build_run_id','clean_run_id'):
            if type(report[k]) is not str or str(UUID(report[k]))!=report[k]: raise ValueError
        for k in ('batch_date','effective_start','effective_end'): dims._day(report[k])
        if report['effective_start']>report['effective_end']: raise ValueError
        full_range(report['requested_start'],report['requested_end'],report['effective_start'],report['effective_end'])
        if not dims.quality._id(report['privacy_key_id'],64): raise ValueError
        if type(report['checks']) is not dict or report['checks']!={k:'PASS' for k in CHECKS}: raise ValueError
        if type(report['rows']) is not dict or set(report['rows'])!={VISIT,'dwd_charge_detail'} or any(type(v) is not int or v<0 for v in report['rows'].values()): raise ValueError
        if type(report['hashes']) is not dict or set(report['hashes'])!={'input','dim','visit','charge'}: raise ValueError
        for v in [*report['hashes'].values(),report['node07_report_sha256']]:
            if type(v) is not str or not re.fullmatch('[0-9a-f]{64}',v): raise ValueError
        if type(report['generated_at']) is not str or datetime.fromisoformat(report['generated_at']).tzinfo is None: raise ValueError
        _validate_stats_shape(report['stats'])
        if report['rows']!={VISIT:report['stats']['visit_rows'],'dwd_charge_detail':report['stats']['charge_rows']}: raise ValueError
    except Exception:
        raise OdsError('N8_STALE') from None
    return report


def _validate_stats_shape(stats):
    from etl.jobs import clean_dwd as clean
    keys=set('ods_rows base_rows duplicate_removed quarantined_rows visit_rows charge_rows missing_total_rows missing_component_rows negative_residual_rows missing_calendar_span_rows disease_not_applicable_rows association_counts'.split())
    if type(stats) is not dict or set(stats)!=keys: raise ValueError
    for key in keys-{'association_counts','ods_rows','base_rows','duplicate_removed','quarantined_rows'}:
        if type(stats[key]) is not int or stats[key]<0: raise ValueError
    for key in ('ods_rows','base_rows','duplicate_removed','quarantined_rows'):
        if type(stats[key]) is not dict or set(stats[key])!=set(clean.TABLES) or any(type(v) is not int or v<0 for v in stats[key].values()): raise ValueError
    assoc=stats['association_counts']
    if type(assoc) is not dict or set(assoc)!=set(('department','date','disease','fee_type','visit')): raise ValueError
    for value in assoc.values():
        if type(value) is not dict or set(value)!=set(('slots','matched','not_applicable')) or any(type(n) is not int or n<0 for n in value.values()) or value['slots']!=value['matched']: raise ValueError


    for table in clean.TABLES:
        if stats['ods_rows'][table]!=stats['base_rows'][table]+stats['duplicate_removed'][table]+stats['quarantined_rows'][table]: raise ValueError
    op=stats['base_rows']['dwd_outpatient_visit_detail']; ip=stats['base_rows']['dwd_inpatient_visit_detail']
    visits=op+ip; charges=stats['base_rows']['dwd_charge_detail']
    if stats['visit_rows']!=visits or stats['charge_rows']!=charges or stats['disease_not_applicable_rows']!=op: raise ValueError
    if stats['missing_total_rows']>visits or any(stats[k]>ip for k in ('missing_component_rows','negative_residual_rows','missing_calendar_span_rows')): raise ValueError
    slots=dict(department=visits+charges,date=visits+charges+ip-stats['missing_calendar_span_rows'],disease=ip,fee_type=charges,visit=charges)
    for kind,n in slots.items():
        if assoc[kind]!=dict(slots=n,matched=n,not_applicable=op if kind=='disease' else 0): raise ValueError


def _read_report_at(directory,name):
    import os,stat
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>2_000_000:
                raise OdsError('N8_REPORT_PATH_INVALID') from None
            raw=stream.read(2_000_001)
        report=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _validate_report(report)
        return report
    except FileNotFoundError: return None
    except OdsError: raise
    except OSError: raise OdsError('N8_REPORT_PATH_INVALID') from None
    except Exception: raise OdsError('N8_STALE') from None


def _report_preflight(env,batch,dataset):
    import os
    fd=_report_directory(env)
    try:
        prior=_read_report_at(fd,_report_name(batch))
        if prior is not None and (prior['env']!=env or prior['batch_date']!=batch or prior['dataset_id']!=dataset):
            raise OdsError('N8_REPORT_IDENTITY_CONFLICT') from None
        return prior
    finally: os.close(fd)


def _publish_report(report):
    import os,stat
    from uuid import uuid4
    from etl.jobs import build_dims as dims
    _validate_report(report)
    directory=_report_directory(report['env']); nonce=uuid4().hex
    temp='.w8-'+nonce+'.tmp'; backup='.w8-'+nonce+'-old.tmp'; name=_report_name(report['batch_date'])
    pending=set(); replaced=False; prior=None
    try:
        prior=_read_report_at(directory,name)
        if prior is not None and any(prior[k]!=report[k] for k in ('env','dataset_id','batch_date')):
            raise OdsError('N8_REPORT_IDENTITY_CONFLICT') from None
        if prior is not None:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>2_000_000 or stat.S_IMODE(info.st_mode)!=0o600:
                    raise OdsError('N8_REPORT_PATH_INVALID') from None
                previous=stream.read(2_000_001)
            if not dims._same_shape_value(json.loads(previous),prior):
                raise OdsError('N8_REPORT_IDENTITY_CONFLICT') from None
            fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory); pending.add(backup)
            with os.fdopen(fd,'wb') as stream:
                stream.write(previous); stream.flush(); os.fsync(stream.fileno())
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory); pending.add(temp)
        with os.fdopen(fd,'wb') as stream:
            stream.write((json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode())
            stream.flush(); os.fsync(stream.fileno())
        current=_read_report_at(directory,name)
        if current is not None and any(current[k]!=report[k] for k in ('env','dataset_id','batch_date')):
            raise OdsError('N8_REPORT_IDENTITY_CONFLICT') from None
        os.replace(temp,name,src_dir_fd=directory,dst_dir_fd=directory); pending.discard(temp); replaced=True
        os.fsync(directory)
    except Exception as error:
        if replaced:
            # Restore visible old bytes after a failed directory sync, never overwrite foreign evidence.
            try:
                current=_read_report_at(directory,name)
                if dims._same_shape_value(current,report):
                    if prior is not None:
                        os.replace(backup,name,src_dir_fd=directory,dst_dir_fd=directory); pending.discard(backup)
                    else: os.unlink(name,dir_fd=directory)
                    try: os.fsync(directory)
                    except OSError: pass
            except Exception: pass
        if isinstance(error,OdsError) and error.code in SAFE_CODES: raise OdsError(error.code) from None
        raise OdsError('N8_REPORT_WRITE_FAILED') from None
    finally:
        for owned in pending:
            try: os.unlink(owned,dir_fd=directory)
            except OSError: pass
        os.close(directory)


def _input_digest(snapshot,dates):
    from etl.jobs import build_dims as dims
    audits=[[t,[[k,_typed(v) if k!='stats_json' else ['json',v]] for k,v in sorted(entry['audit'].items())]] for t,entry in sorted(snapshot['tables'].items())]
    return dims._hash(dict(audits=audits,dates=dates))


def _summary_from_audit(a,stats,hashes):
    return dict(schema_version=SCHEMA_VERSION,dataset_id=a['dataset_id'],build_run_id=a['build_run_id'],clean_run_id=a['clean_run_id'],batch_date=a['batch_date'].isoformat(),requested_start=None if a['requested_start'] is None else a['requested_start'].isoformat(),requested_end=None if a['requested_end'] is None else a['requested_end'].isoformat(),effective_start=a['effective_start'].isoformat(),effective_end=a['effective_end'].isoformat(),build_scope='ALL',gate_status='PASS',rows={VISIT:stats['visit_rows'],'dwd_charge_detail':stats['charge_rows']},hashes=hashes,stats=stats)


def verify_build_freshness_from_cursor(cursor,*,dataset_id,batch_date,env,report):
    """Caller owns stable transaction/locks; no transaction control, file I/O or DML."""
    from etl.jobs import clean_dwd as clean, build_dims as dims
    try:
        _validate_report(report)
        if report['dataset_id']!=dataset_id or report['env']!=env or report['batch_date']!=batch_date: raise ValueError
        _verify_schema(cursor)
        snapshot=clean.load_quality_snapshot_from_cursor(cursor,batch_date,dataset_id=dataset_id)
        quality_report,quality_hash=_read_quality_report(env,batch_date)
        dims._upstream(snapshot,quality_report,env)
        dates=dims._date_snapshot(cursor,snapshot); desired=dims.dimension_rows(dates)
        visits=visit_candidates(snapshot['tables']['dwd_outpatient_visit_detail']['dwd']['rows'],snapshot['tables']['dwd_inpatient_visit_detail']['dwd']['rows'])
        charges=charge_candidates(snapshot['tables']['dwd_charge_detail']['dwd']['rows'],visits)
        stats=_stats(snapshot,visits,charges,desired)
        actual_dims=dims._read_rows(cursor,dataset_id)
        if not dims._same_shape_value(actual_dims,desired['rows']): raise ValueError
        audit_rows=_read(cursor,AUDIT,AUDIT_COLUMNS,dataset_id)
        if len(audit_rows)!=1: raise ValueError
        a=audit_rows[0]
        if a['build_scope']!='ALL': raise ValueError
        if a['clean_run_id']!=snapshot['clean_run_id'] or a['batch_date'].isoformat()!=batch_date or a['mapping_version']!=snapshot['mapping_version'] or a['privacy_key_id']!=snapshot['privacy_key_id']: raise OdsError('N8_STALE') from None
        # Reuse strict audit identity/certificate check; this branch never falls back.
        _previous_certificate(cursor,dataset_id,batch_date,env,audit_rows,actual_dims)
        columns=tuple(clean.TABLES['dwd_charge_detail']['columns'])
        stored_visit=_read(cursor,VISIT,VISIT_COLUMNS,dataset_id)
        stored_charge=_charge_rows(cursor,snapshot,columns,dataset_id)
        _conserve(VISIT_COLUMNS,stored_visit,visits,('dataset_id','visit_type','visit_id'))
        _conserve(columns,stored_charge,charges,('dataset_id','charge_id'))
        hashes=dict(input=_input_digest(snapshot,dates),dim=dims._hash(actual_dims),visit=rows_digest(tuple(c for c in VISIT_COLUMNS if c!='etl_load_time'),stored_visit,('dataset_id','visit_type','visit_id')),charge=rows_digest(columns,stored_charge,('dataset_id','charge_id')))
        if (a['effective_start'].isoformat(),a['effective_end'].isoformat())!=(desired['date_min'],desired['date_max']): raise ValueError
        if any(a[k+'_sha256']!=v for k,v in hashes.items()): raise ValueError
        audit_stats=json.loads(a['stats_json']) if isinstance(a['stats_json'],(str,bytes)) else a['stats_json']
        if not dims._same_shape_value(audit_stats,stats): raise ValueError
        expected=_summary_from_audit(a,stats,hashes)
        if not dims._same_shape_value({k:report[k] for k in _RETURN_KEYS},expected): raise ValueError
        if report['node07_report_sha256']!=quality_hash: raise ValueError
        if report['mapping_version']!=a['mapping_version'] or report['privacy_key_id']!=a['privacy_key_id']: raise ValueError
        return dict(dataset_id=dataset_id,build_run_id=a['build_run_id'],clean_run_id=a['clean_run_id'],batch_date=batch_date,gate_status='PASS',hashes=hashes)
    except OdsError as error:
        raise OdsError(error.code if error.code in SAFE_CODES else 'N8_STALE') from None
    except Exception: raise OdsError('N8_STALE') from None


def _open_report_directory_readonly(env):
    """Open fixed log root component by component; never ensure/create paths."""
    import os
    from pathlib import Path
    from etl.jobs import build_dims as dims
    if type(env) is not str or env not in ('dev','test'): raise OdsError('N8_REPORT_PATH_INVALID') from None
    root=Path(dims.quality._LOG_ROOT)
    if not root.is_absolute() or '..' in root.parts: raise OdsError('N8_REPORT_PATH_INVALID') from None
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:],env):
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=nxt
        return fd
    except Exception:
        os.close(fd)
        raise OdsError('N8_REPORT_PATH_INVALID') from None


def _read_quality_report(env,batch):
    import os,stat
    _report_name(batch)  # Validate strict batch before fixed filename composition.
    # Fixed course file name; no caller-controlled path accepted.
    name='quality_report_'+batch.replace('-','')+'.json'
    directory=_open_report_directory_readonly(env)
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>2_000_000: raise OdsError('N8_REPORT_PATH_INVALID') from None
            raw=stream.read(2_000_001)
        report=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return report,hashlib.sha256(raw).hexdigest()
    except FileNotFoundError: raise OdsError('N8_UPSTREAM_GATE_FAILED') from None
    except OdsError: raise
    except OSError: raise OdsError('N8_REPORT_PATH_INVALID') from None
    except Exception: raise OdsError('N8_UPSTREAM_GATE_FAILED') from None
    finally: os.close(directory)


def _validate_public(dataset_id,batch_date,env,start=None,end=None):
    from etl.scheduler import scheduler
    from etl.jobs import build_dims as dims
    from etl.config.source_config import database_options
    if scheduler._CURRENT.get() is not None: raise OdsError('N8_RUN_CONTEXT_ACTIVE') from None
    try:
        if type(env) is not str or env not in ('dev','test') or dataset_id is not None and not dims.quality._id(dataset_id): raise ValueError
        if batch_date is not None: dims._day(batch_date)
        if start is not None or end is not None:
            if start is None or end is None or dims._day(start)>dims._day(end): raise ValueError
        if database_options()['database']!=('hospital_test' if env=='test' else 'hospital'): raise ValueError
    except Exception: raise OdsError('N8_ARGUMENTS_INVALID') from None


def _verify_current_locked(dataset_id,batch_date,env):
    from etl.jobs import clean_dwd as clean
    import os
    connection=None
    try:
        connection=clean._connect()
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cursor.execute('START TRANSACTION')
            selected,batch,_=_lock_inputs(cursor,dataset_id)
            if batch_date is not None and batch_date!=batch: raise OdsError('N8_ARGUMENTS_INVALID') from None
            directory=_open_report_directory_readonly(env)
            try: report=_read_report_at(directory,_report_name(batch))
            finally: os.close(directory)
            if report is None: raise OdsError('N8_STALE') from None
            result=verify_build_freshness_from_cursor(cursor,dataset_id=selected,batch_date=batch,env=env,report=report)
        connection.rollback()
        return result
    except Exception as error:
        if connection is not None:
            try: connection.rollback()
            except Exception: raise OdsError('N8_ROLLBACK_FAILED') from None
        raise OdsError(error.code if isinstance(error,OdsError) and error.code in SAFE_CODES else 'N8_STALE') from None
    finally:
        if connection is not None: clean._close(connection)


def verify_current_build(*,dataset_id=None,batch_date=None,env='dev'):
    from etl.scheduler import scheduler
    _validate_public(dataset_id,batch_date,env)
    try:
        with scheduler._lock(env,'run'):
            return _verify_current_locked(dataset_id,batch_date,env)
    except scheduler.EtlError: raise OdsError('N8_RUN_CONTEXT_ACTIVE') from None
    except OdsError: raise
    except Exception: raise OdsError('N8_INTERNAL_ERROR') from None


def _run_build_all_locked(start_date,end_date,*,dataset_id,batch_date,env,pipeline=None):
    """Build, publish and certify under the caller's existing environment run lock."""
    from datetime import timezone
    options = {} if pipeline is None else {'pipeline': pipeline}
    output = _run_build_locked(start_date,end_date,dataset_id=dataset_id,batch_date=batch_date,
                               env=env,scope='ALL',**options)
    try:
        # DB commit is complete; publication/fresh failures never claim rollback.
        quality_report,quality_hash=_read_quality_report(env,output['batch_date'])
        report=dict(output,env=env,gate_status='PASS',generated_at=datetime.now(timezone.utc).isoformat(),model_version=MODEL_VERSION,mapping_version=quality_report['mapping_version'],privacy_key_id=quality_report['privacy_key_id'],node07_report_sha256=quality_hash,checks={k:'PASS' for k in CHECKS},code='N8_BUILD_OK')
        _publish_report(report)
        fresh=_verify_current_locked(output['dataset_id'],output['batch_date'],env)
        if fresh['build_run_id']!=output['build_run_id']: raise OdsError('N8_STALE') from None
    except OdsError as error:
        # Commit is already complete, so never repeat the write for publish/fresh errors.
        if type(error.code) is str and error.code == 'N8_TX_RETRYABLE':
            raise OdsError('N8_REPORT_WRITE_FAILED') from None
        raise
    return dict(output,gate_status='PASS',env=env,report_path=str(PROJECT_ROOT/'backend/etl/logs'/env/_report_name(output['batch_date'])))


def run_build_all(start_date=None,end_date=None,*,dataset_id=None,batch_date=None,env='dev'):
    from etl.scheduler import scheduler
    _validate_public(dataset_id,batch_date,env,start_date,end_date)
    try:
        with scheduler._lock(env,'run'):
            return _run_build_all_locked(start_date,end_date,dataset_id=dataset_id,batch_date=batch_date,env=env)
    except scheduler.EtlError: raise OdsError('N8_RUN_CONTEXT_ACTIVE') from None
    except OdsError as error:
        raise OdsError(error.code if error.code in SAFE_CODES else 'N8_INTERNAL_ERROR') from None
    except Exception: raise OdsError('N8_REPORT_WRITE_FAILED') from None


def _conserve(columns,actual,expected,primary_key):
    """Check every typed column and explicit amount/NULL aggregates, independently of hashes."""
    def ordered(rows):
        return sorted(rows,key=lambda r:tuple(str(r[c]).encode('utf-8') for c in primary_key))
    left=ordered(actual); right=ordered(expected)
    if len(left)!=len(right) or any([_typed(a[c]) for c in columns]!=[_typed(b[c]) for c in columns] for a,b in zip(left,right)):
        raise OdsError('N8_RECONCILE_FAILED') from None
    for column in set(columns)&{'amount','source_total_fee','source_drug_fee','source_consumable_fee','residual_fee'}:
        with localcontext(_MONEY_CONTEXT):
            counts=[sum(row[column] is None for row in rows) for rows in (left,right)]
            totals=[sum((row[column] for row in rows if row[column] is not None),Decimal('0.00')) for rows in (left,right)]
        if counts[0]!=counts[1] or totals[0]!=totals[1]:
            raise OdsError('N8_RECONCILE_FAILED') from None
