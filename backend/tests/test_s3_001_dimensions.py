"""C8 pure behavior and coordinator-only integration checks."""
from copy import deepcopy
from unittest.mock import patch
import pytest
from etl.jobs import build_dims as dims

def snapshot():
    return dict(dataset_id='C8_SIM', clean_run_id='11111111-1111-4111-8111-111111111111', batch_date='2024-02-29', rules_version='node07-rules-v1', mapping_version='node07-sim-mapping-v1', privacy_key_id='SIM-unit-v1', source_fingerprint='a'*64, dates={k:['2024-02-29'] for k in ('his_outpatient','his_inpatient','his_charge','emr_record','lis_report','finance_excel','inpatient_discharge')})

def test_leap_templates_nulls_and_input_unchanged():
    source=snapshot(); before=deepcopy(source); result=dims.dimension_rows(source)
    assert source == before
    assert result['required_date_start']=='2023-02-28'
    assert result['required_date_end']=='2025-02-28'
    assert result['row_counts']==dict(dim_department=4,dim_date=732,dim_disease=5,dim_fee_type=7)
    assert all(r['is_holiday'] is None and r['holiday_status']=='UNKNOWN' for r in result['rows']['dim_date'])
    fees=result['rows']['dim_fee_type']
    assert sum(r['source_supported'] for r in fees)==3
    assert sum(r['origin']=='COURSE_CATALOG' for r in fees)==4
    assert result['source_completeness']['service_unsplit_rows'] is None
    assert all(r['hierarchy_level'] is None for r in result['rows']['dim_department'])

@pytest.mark.parametrize('value,code', [('1000-01-01','N8_DATE_RANGE_INVALID'),('9999-12-31','N8_DATE_RANGE_INVALID'),('2024-02-30','N8_DATE_INVALID'),(None,'N8_DATE_INVALID'),('', 'N8_DATE_INVALID')])
def test_date_reject(value,code):
    source=snapshot(); source['dates']['his_charge']=[value]
    with pytest.raises(Exception,match='^'+code+'$'): dims.dimension_rows(source)

def test_empty_and_extra_keys():
    source=snapshot(); source['dates']={k:[] for k in source['dates']}
    with pytest.raises(Exception,match='^N8_INPUT_EMPTY$'): dims.dimension_rows(source)
    source=snapshot(); source['dates']['extra']=[]
    with pytest.raises(Exception,match='^N8_ARGUMENTS_INVALID$'): dims.dimension_rows(source)


import base64
from datetime import date, datetime
import json
import os
import socket
import pymysql
from pathlib import Path
from etl.config import source_config as sources
from etl.jobs import clean_dwd as clean, quality_check as q
from etl.scheduler import scheduler as scheduler
from test_s2_003_pipeline import sim_csv_rows

SIM_BYTES=b'SIM_ONLY_C8_NOT_SECRET'.ljust(32,b'x')

def block_unit_connections(monkeypatch):
    """Fail before driver or TCP work, including paths bypassing project wrappers."""
    def blocked(*args, **kwargs):
        raise AssertionError('C8_PURE_NETWORK_BLOCKED')
    for owner, name in (
        (pymysql, 'connect'), (pymysql.connections.Connection, 'connect'),
        (socket, 'create_connection'), (socket.socket, 'connect'),
        (socket.socket, 'connect_ex'),
    ):
        monkeypatch.setattr(owner, name, blocked)


@pytest.fixture(autouse=True)
def isolate(monkeypatch,tmp_path,request):
    if request.node.name.startswith('test_mysql_'):
        if os.environ.get('ODS_TEST_DATABASE_URL'):
            if sources.database_options(test_only=True)['database'] != 'hospital_test':
                pytest.fail('test-only database required; values withheld')
            if os.environ.get('ODS_DATABASE_URL') != os.environ['ODS_TEST_DATABASE_URL']:
                pytest.fail('coordinator must inject identical test URLs; values withheld')
            return
        block_unit_connections(monkeypatch)
        return
    block_unit_connections(monkeypatch)
    for name in ('ODS_DATABASE_URL','ODS_TEST_DATABASE_URL','DWD_PRIVACY_KEY','DWD_PRIVACY_KEY_ID','C7_RUN_MYSQL_TESTS','ODS_S6_INTEGRATION'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('ODS_DATABASE_URL','mysql://SIM:SIM@127.0.0.1:13306/hospital_test')
    monkeypatch.setenv('DWD_PRIVACY_KEY',base64.b64encode(SIM_BYTES).decode())
    monkeypatch.setenv('DWD_PRIVACY_KEY_ID','SIM-unit-v1')
    monkeypatch.setattr(q,'_LOG_ROOT',tmp_path/'logs')
    monkeypatch.setattr(scheduler,'LOG_ROOT',tmp_path/'logs')
    monkeypatch.setattr(clean,'connect_database',lambda:pytest.fail('unit must not connect DB'))
    monkeypatch.setattr(sources,'connect_database',lambda **kw:pytest.fail('unit must not connect DB'))


def n7_snapshot():
    config=clean._config(); result=dict(schema_version='node07-snapshot-v1',dataset_id='C8_SIM',clean_run_id=snapshot()['clean_run_id'],batch_date='2025-01-31',rules_version=config['rules_version'],mapping_version=config['mapping_version'],privacy_key_id='SIM-unit-v1',tables={})
    for table,spec in clean.TABLES.items():
        raw=[dict(row,dataset_id='C8_SIM',source_row_number=i,etl_batch_date=date(2025,1,31)) for i,row in enumerate(sim_csv_rows(spec['source']),1)]
        rows,audit=clean._prepare(table,raw,result['batch_date'],result['dataset_id'],SIM_BYTES,'SIM-unit-v1',result['clean_run_id'],config)
        for row in rows: row['etl_load_time']=datetime(2025,1,31)
        audit['updated_at']=datetime(2025,1,31)
        result['tables'][table]=dict(source=dict(table=sources.SOURCES[spec['source']]['table'],columns=list(sources.SOURCES[spec['source']]['columns']+clean.META),rows=raw),dwd=dict(table=table,columns=spec['columns'],rows=rows),audit=audit)
    return result


def n7_report(monkeypatch,source=None):
    source=source or n7_snapshot()
    monkeypatch.setattr(q,'_load_snapshot',lambda *a,**k:source)
    return source,q.generate_quality_report(source['batch_date'],dataset_id=source['dataset_id'],env='test')


def dim_report():
    result=dims.dimension_rows(snapshot())
    return dict(schema_version=dims.SCHEMA_VERSION,env='test',dataset_id='C8_SIM',clean_run_id=snapshot()['clean_run_id'],batch_date='2024-02-29',template_version=dims.TEMPLATE_VERSION,source_fingerprint='a'*64,node07_report_sha256='b'*64,dim_sha256='c'*64,date_min=result['date_min'],date_max=result['date_max'],required_date_start=result['required_date_start'],required_date_end=result['required_date_end'],row_counts=result['row_counts'],source_completeness=result['source_completeness'],engineering_status='DIM_READY',course_status='HOLD',generated_at='2025-01-01T00:00:00+00:00',code='N8_DIM_OK')


def test_iso_year_date_continuity_and_sorted_exact_columns():
    source=snapshot(); source['dates']={k:[] for k in dims.DATE_COLUMNS}; source['dates']['his_charge']=['2021-01-01']; source['dates']['inpatient_discharge']=[None]
    result=dims.dimension_rows(source); rows=result['rows']['dim_date']
    item=next(r for r in rows if r['calendar_date']=='2021-01-01')
    assert (item['calendar_year'],item['iso_week_year'],item['iso_week'],item['iso_weekday'])==(2021,2020,53,5)
    assert len(rows)==(date(2022,1,1)-date(2020,1,1)).days+1
    for table,items in result['rows'].items():
        assert all(list(row)==[c for c,_,_ in dims._columns(table)] for row in items)
        assert [row[dims._SPECS[table][0][0]] for row in items]==sorted(row[dims._SPECS[table][0][0]] for row in items)
    assert set(result['source_completeness'])==set('organization_attributes disease_grouping statutory_holidays fee_classification unsupported_fee_catalog_rows service_unsplit_rows'.split())


@pytest.mark.parametrize('field,value',[('source_fingerprint','x'),('clean_run_id','SIM_BAD'),('dataset_id',True),('mapping_version','unknown'),('dates',[])])
def test_snapshot_types_and_identity(field,value):
    source=snapshot(); source[field]=value
    with pytest.raises(sources.OdsError,match='^N8_ARGUMENTS_INVALID$'): dims.dimension_rows(source)


def test_schema_sql_exact_and_unknown_rejected():
    assert (sources.PROJECT_ROOT/'sql/dim/dim_tables.sql').read_text()==';\n\n'.join(dims._ddl())+';\n'
    assert all('IF NOT EXISTS' not in sql and 'ALTER' not in sql and 'DROP' not in sql for sql in dims._ddl())
    class Cursor:
        def execute(self,*args): pass
        def fetchall(self): return []
    with pytest.raises(sources.OdsError,match='^N8_SCHEMA_INVALID$'): dims._verify(Cursor())


def test_upstream_real_rules_and_forgery(monkeypatch):
    source,report=n7_report(monkeypatch)
    assert report['gate_status']=='PASS'
    with patch.object(dims.quality, '_validate_snapshot', wraps=dims.quality._validate_snapshot) as validate:
        dims._upstream(source,report,'test')
        validate.assert_called_once()
    for mutate in [lambda r:r.update(score=float('nan')),lambda r:r.update(score=True),lambda r:r.update(planned_rules=33),lambda r:r.update(dataset_id='OTHER'),lambda r:r.update(gate_status='FAIL'),lambda r:r['rules'][0].update(hard_block=True),lambda r:r['rules'].pop(),lambda r:r.update(clean_run_id='22222222-2222-4222-8222-222222222222')]:
        bad=deepcopy(report); mutate(bad)
        with pytest.raises(sources.OdsError,match='^N8_UPSTREAM_GATE_FAILED$'): dims._upstream(source,bad,'test')


@pytest.mark.parametrize('kind',['source','dwd'])
def test_changed_source_or_base_is_stale(monkeypatch,kind):
    source,report=n7_report(monkeypatch)
    row=source['tables']['dwd_charge_detail'][kind]['rows'][0]
    row['amount']='11.00' if kind=='source' else __import__('decimal').Decimal('11.00')
    with pytest.raises(sources.OdsError,match='^N8_STALE$'): dims._upstream(source,report,'test')


def test_report_private_atomic_identity_and_links(monkeypatch,tmp_path):
    report=dim_report(); dims._write_report(report)
    path=q._LOG_ROOT/'test/dimension_report_20240229.json'
    assert path.stat().st_mode&0o777==0o600
    assert json.loads(path.read_text())==report
    assert not set(report)&{'rows','patient_key','visit_id'}
    bad=dict(report,dataset_id='OTHER')
    with pytest.raises(sources.OdsError,match='^N8_REPORT_IDENTITY_CONFLICT$'): dims._write_report(bad)
    before=path.read_bytes()
    monkeypatch.setattr(dims.os,'replace',lambda *a,**k:(_ for _ in ()).throw(OSError('SIM_SECRET')))
    with pytest.raises(sources.OdsError,match='^N8_REPORT_WRITE_FAILED$'): dims._write_report(report)
    assert path.read_bytes()==before and not list(path.parent.glob('*.tmp'))
    path.unlink(); path.symlink_to(tmp_path/'missing')
    with pytest.raises(sources.OdsError,match='^N8_REPORT_PATH_INVALID$'): dims._write_report(report)


def test_report_hardlink_parent_link_missing_and_nan(monkeypatch,tmp_path):
    with pytest.raises(sources.OdsError,match='^N8_UPSTREAM_GATE_FAILED$'): dims._read_report('test','2024-02-29')
    directory=q._LOG_ROOT/'test'; directory.mkdir()
    path=directory/'quality_report_20240229.json'; path.write_text('{"score":NaN}')
    with pytest.raises(sources.OdsError,match='^N8_UPSTREAM_GATE_FAILED$'): dims._read_report('test','2024-02-29')
    os.link(path,directory/'other')
    with pytest.raises(sources.OdsError,match='^N8_REPORT_PATH_INVALID$'): dims._read_report('test','2024-02-29')
    monkeypatch.setattr(q,'_LOG_ROOT',tmp_path/'linked'); (tmp_path/'linked').symlink_to(directory,target_is_directory=True)
    with pytest.raises(sources.OdsError,match='^N8_REPORT_PATH_INVALID$'): dims._read_report('test','2024-02-29')


def test_run_context_lock_busy_and_release(monkeypatch):
    token=scheduler._CURRENT.set(object())
    try:
        with pytest.raises(sources.OdsError,match='^N8_RUN_CONTEXT_ACTIVE$'): dims.refresh_dimensions(env='test')
    finally: scheduler._CURRENT.reset(token)
    with scheduler._lock('test','run'):
        with pytest.raises(sources.OdsError,match='^N8_RUN_CONTEXT_ACTIVE$'): dims.refresh_dimensions(env='test')
    monkeypatch.setattr(dims,'_refresh',lambda *a:(_ for _ in ()).throw(RuntimeError('SIM_SECRET')))
    with pytest.raises(sources.OdsError,match='^N8_INTERNAL_ERROR$'): dims.refresh_dimensions(env='test')
    with scheduler._lock('test','run'): pass


def test_cursor_helper_shape_no_transaction_and_old_wrapper(monkeypatch):
    source=n7_snapshot(); calls=[]
    monkeypatch.setattr(clean,'_registry',lambda *a,**k:('C8_SIM','2025-01-31',()))
    monkeypatch.setattr(clean,'_verify',lambda c:None)
    monkeypatch.setattr(clean,'_audits',lambda *a:{t:v['audit'] for t,v in source['tables'].items()})
    monkeypatch.setattr(clean,'_source',lambda c,t,d:source['tables'][t]['source']['rows'])
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,*args): calls.append(sql)
        def fetchall(self):
            table=next(t for t in clean.TABLES if '`'+t+'`' in calls[-1])
            return [tuple(row[c] for c in clean.TABLES[table]['columns']) for row in source['tables'][table]['dwd']['rows']]
    class Connection:
        def cursor(self): return Cursor()
        def rollback(self): calls.append('ROLLBACK')
        def close(self): calls.append('CLOSE')
    result=clean.load_quality_snapshot_from_cursor(Cursor(),'2025-01-31',dataset_id='C8_SIM')
    assert result==source and all(sql.startswith('SELECT ') for sql in calls)
    calls.clear(); monkeypatch.setattr(clean,'_connect',lambda **k:Connection())
    assert clean.load_quality_snapshot('2025-01-31',dataset_id='C8_SIM')==source
    assert calls[:2]==['SET TRANSACTION ISOLATION LEVEL REPEATABLE READ','START TRANSACTION WITH CONSISTENT SNAPSHOT']
    assert calls[-2:]==['ROLLBACK','CLOSE']


def test_prepare_no_ddl_or_transaction_and_exact_result(monkeypatch):
    captured=[]
    def store(cursor,result):
        captured.append(result)
        return dict(template_version=dims.TEMPLATE_VERSION,required_date_start=result['required_date_start'],required_date_end=result['required_date_end'],row_counts=result['row_counts'],dim_sha256=dims._hash(result['rows']))
    monkeypatch.setattr(dims,'_store',store)
    result=dims.prepare_dimensions(object(),'C8_SIM','2024-02-29','2024-02-29')
    assert set(result)==set('template_version required_date_start required_date_end row_counts dim_sha256'.split())
    assert captured[0]['row_counts']['dim_date']==732


@pytest.mark.parametrize('fault,expected,commits,rollbacks',[
    ('before','N8_TX_FAILED',0,1),('commit','N8_COMMIT_UNKNOWN',1,0),
    ('report','N8_REPORT_WRITE_FAILED',1,0),('rollback','N8_ROLLBACK_FAILED',0,1),
    ('identity','N8_REPORT_IDENTITY_CONFLICT',0,1),('missing','N8_UPSTREAM_GATE_FAILED',0,1)])
def test_refresh_transaction_failure_boundaries(monkeypatch,fault,expected,commits,rollbacks):
    source,report=n7_report(monkeypatch); calls=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,*args): calls.append(sql)
        def fetchall(self): return [('2024-02-29',None)] if 'discharge_date' in calls[-1] else [('2024-02-29',)]
    cursor=Cursor()
    class Connection:
        def cursor(self): return cursor
        def commit(self):
            calls.append('COMMIT')
            if fault=='commit': raise OSError('SIM_SECRET')
        def rollback(self):
            calls.append('ROLLBACK')
            if fault=='rollback': raise OSError('SIM_SECRET')
        def close(self): calls.append('CLOSE')
    monkeypatch.setattr(clean,'_connect',lambda **kw:Connection())
    monkeypatch.setattr(clean,'_registry',lambda *a,**k:('C8_SIM','2025-01-31',()))
    def load(actual,*a,**k):
        assert actual is cursor
        return source
    monkeypatch.setattr(clean,'load_quality_snapshot_from_cursor',load)
    def read(env,batch,dimension=False):
        if fault=='missing' and not dimension: dims._fail('N8_UPSTREAM_GATE_FAILED')
        return (dict(dim_report(),dataset_id='OTHER'),None) if dimension and fault=='identity' else ((None,None) if dimension else (report,'a'*64))
    monkeypatch.setattr(dims,'_read_report',read)
    def store(actual,result,**kwargs):
        assert actual is cursor
        if fault in ('before','rollback'): raise RuntimeError('SIM_SECRET')
        calls.append('STORE')
        return dict(dim_sha256='a'*64)
    monkeypatch.setattr(dims,'_store',store)
    def write(output):
        calls.append('REPORT')
        if fault=='report': dims._fail('N8_REPORT_WRITE_FAILED')
    monkeypatch.setattr(dims,'_write_report',write)
    with pytest.raises(sources.OdsError,match='^'+expected+'$'):
        dims.refresh_dimensions(env='test')
    assert calls.count('COMMIT')==commits and calls.count('ROLLBACK')==rollbacks
    if fault in ('identity','missing'): assert 'STORE' not in calls
    assert calls[-1]=='CLOSE'
    with scheduler._lock('test','run'): pass


@pytest.fixture
def mysql_dims(monkeypatch):
    # No hidden second flag: credentials absent means explicitly NOT RUN.
    if not os.environ.get('ODS_TEST_DATABASE_URL'):
        pytest.skip('coordinator hospital_test credentials absent; real DIM NOT RUN')
    assert sources.database_options(test_only=True)['database']=='hospital_test'
    if os.environ.get('ODS_DATABASE_URL')!=os.environ['ODS_TEST_DATABASE_URL']:
        pytest.fail('coordinator must inject identical test URLs; values withheld')
    clean._privacy()
    import test_s2_003_pipeline as prior
    import csv
    import subprocess
    import sys
    generator=prior.mysql_pipeline.__wrapped__(monkeypatch)
    dataset,batch,selected,quality_path=next(generator)
    other=dataset+'_other'; dim_path=q._LOG_ROOT/'test'/('dimension_report_'+batch.replace('-','')+'.json')
    assert not dim_path.exists()
    initialized=False; dims_owned=False
    try:
        dims.initialize_dim_schema(); initialized=True
        for name,info in selected.items():
            path=Path(info['file_path']); assert not path.exists(); path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=sources.SOURCES[name]['columns']); writer.writeheader(); writer.writerows(sim_csv_rows(name))
        env=os.environ.copy(); env['PYTHONPATH']=str(sources.PROJECT_ROOT/'backend'); env['PYTHONDONTWRITEBYTECODE']='1'
        completed=subprocess.run([sys.executable,'-m','etl.jobs.run_all','--env','test','--through','quality'],cwd=sources.PROJECT_ROOT/'backend',env=env,capture_output=True,text=True,timeout=90)
        assert completed.returncode==0 and not completed.stderr, 'self-contained N7 formal CLI failed; output withheld'
        report=json.loads(quality_path.read_text()); assert report['gate_status']=='PASS' and report['executed_rules']==34
        connection=sources.connect_database(test_only=True)
        try:
            with connection.cursor() as cursor:
                for table in dims._SPECS:
                    cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id IN (%s,%s)',(dataset,other)); assert cursor.fetchone()[0]==0
                dims_owned=True
                dims.prepare_dimensions(cursor,other,'2024-02-29','2024-02-29')
            connection.commit()
        finally: connection.close()
        yield dataset,batch,other,dim_path
    finally:
        try:
            if initialized and dims_owned:
                connection=sources.connect_database(test_only=True)
                try:
                    with connection.cursor() as cursor:
                        for table in dims._SPECS: cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id IN (%s,%s)',(dataset,other))
                    connection.commit()
                finally: connection.close()
            if dim_path.exists() and not dim_path.is_symlink():
                report=json.loads(dim_path.read_text())
                if report.get('dataset_id')==dataset: dim_path.unlink()
        finally: generator.close()


def persisted(dataset):
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor: return dims._read_rows(cursor,dataset)
    finally: connection.rollback(); connection.close()


def test_mysql_actual_dimensions_idempotent_nulls_gaps(mysql_dims):
    dataset,batch,other,path=mysql_dims
    sentinel=persisted(other); first=dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    rows=persisted(dataset)
    assert first['engineering_status']=='DIM_READY' and first['course_status']=='HOLD' and first['code']=='N8_DIM_OK'
    assert first['row_counts']==dict(dim_department=4,dim_date=735,dim_disease=5,dim_fee_type=7)
    assert first['required_date_start']=='2024-01-01' and first['required_date_end']=='2026-01-04'
    days=[date.fromisoformat(r['calendar_date']) for r in rows['dim_date']]
    assert days[0]==date(2024,1,1) and days[-1]==date(2026,1,4)
    assert len(days)==(days[-1]-days[0]).days+1 and len(set(days))==len(days)
    assert all(r['is_holiday'] is None and r['holiday_status']=='UNKNOWN' for r in rows['dim_date'])
    assert all(r['icd_code'] is None and r['drg_group'] is None and r['dip_group'] is None for r in rows['dim_disease'])
    assert all(r['hierarchy_level'] is None and r['campus_code'] is None and r['parent_dept_code'] is None for r in rows['dim_department'])
    assert first['source_completeness']==dict(organization_attributes='UNKNOWN',disease_grouping='UNKNOWN',statutory_holidays='UNKNOWN',fee_classification='PARTIAL',unsupported_fee_catalog_rows=4,service_unsplit_rows=3)
    assert {r['fee_type_code'] for r in rows['dim_fee_type']}=={'SERVICE','DRUG','CONSUMABLE','REGISTRATION','EXAM','TREATMENT','BED'}
    assert sum(r['source_supported'] for r in rows['dim_fee_type'])==3
    for i,row in enumerate(rows['dim_department'],1):
        assert row==dict(dataset_id=dataset,dept_code=f'SIM_DEPT_{i:02}',dept_name=f'SIM_科室{i}',hierarchy_level=None,parent_dept_code=None,campus_code=None,campus_name=None,attribute_status='UNKNOWN',mapping_version='node07-sim-mapping-v1',scope='SIM')
    for row in rows['dim_date']:
        day=date.fromisoformat(row['calendar_date']); iso=day.isocalendar()
        assert (row['calendar_year'],row['calendar_month'],row['day_of_month'],row['iso_week_year'],row['iso_week'],row['iso_weekday'],row['is_weekend'])==(day.year,day.month,day.day,iso.year,iso.week,iso.weekday,int(day.weekday()>=5))
    second=dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    assert second['dim_sha256']==first['dim_sha256'] and persisted(dataset)==rows and persisted(other)==sentinel
    assert json.loads(path.read_text())==second and path.stat().st_mode&0o777==0o600


def test_mysql_third_dimension_failure_rolls_back_second_connection(mysql_dims,monkeypatch):
    dataset,batch,other,path=mysql_dims
    dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    # Old DIM and its certificate match; Finance dates extend the desired range.
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute('UPDATE ods_finance_excel SET stat_date=%s WHERE dataset_id=%s',('2027-01-01',dataset))
        connection.commit()
    finally: connection.close()
    before=persisted(dataset); sentinel=persisted(other); old_report=path.read_bytes()
    original=clean._connect; inserted=[]
    class CursorProxy:
        def __init__(self,cursor): self.cursor=cursor
        def __enter__(self): self.cursor.__enter__(); return self
        def __exit__(self,*a): return self.cursor.__exit__(*a)
        def __getattr__(self,name): return getattr(self.cursor,name)
        def executemany(self,sql,args):
            if sql.startswith('INSERT INTO `dim_'):
                inserted.append(sql.split('`')[1])
            if sql.startswith('INSERT INTO `dim_disease`'): raise RuntimeError('SIM_THIRD_DIM_FAULT')
            return self.cursor.executemany(sql,args)
    class ConnectionProxy:
        def __init__(self,conn): self.conn=conn
        def __getattr__(self,name): return getattr(self.conn,name)
        def cursor(self): return CursorProxy(self.conn.cursor())
    monkeypatch.setattr(clean,'_connect',lambda *a,**k:ConnectionProxy(original(*a,**k)))
    with pytest.raises(sources.OdsError,match='^N8_TX_FAILED$'): dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    assert inserted==['dim_department','dim_date','dim_disease']
    assert persisted(dataset)==before and persisted(other)==sentinel and path.read_bytes()==old_report


@pytest.mark.parametrize('change',['source','base','empty'])
def test_mysql_stale_and_empty_preserve_four_dimensions(mysql_dims,change):
    dataset,batch,other,path=mysql_dims
    dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test'); before=persisted(dataset); sentinel=persisted(other)
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            if change=='source': cursor.execute('UPDATE ods_his_charge_detail SET amount=%s WHERE dataset_id=%s',('99.00',dataset))
            elif change=='base': cursor.execute('UPDATE dwd_charge_detail SET amount=%s WHERE dataset_id=%s',('99.00',dataset))
            else:
                for spec in sources.SOURCES.values(): cursor.execute(f"DELETE FROM `{spec['table']}` WHERE dataset_id=%s",(dataset,))
        connection.commit()
    finally: connection.close()
    with pytest.raises(sources.OdsError,match='^N8_STALE$'): dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    assert persisted(dataset)==before and persisted(other)==sentinel


def test_mysql_report_failure_after_commit_is_not_rollback(mysql_dims,monkeypatch):
    dataset,batch,other,path=mysql_dims
    sentinel=persisted(other)
    writer=dims._write_report
    monkeypatch.setattr(dims,'_write_report',lambda report:dims._fail('N8_REPORT_WRITE_FAILED'))
    with pytest.raises(sources.OdsError,match='^N8_REPORT_WRITE_FAILED$'): dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    committed=persisted(dataset)
    assert all(committed.values()) and persisted(other)==sentinel and not path.exists()
    monkeypatch.setattr(dims,'_write_report',writer)
    recovered=dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    assert persisted(dataset)==committed and recovered['dim_sha256']==dims._hash(committed)
    assert json.loads(path.read_text())==recovered


@pytest.mark.parametrize('change',['type','order','index','engine'])
def test_schema_checks_all_columns_order_indexes_engine(change):
    names=tuple(dims._SPECS); replies=[]
    replies.append([(t,'InnoDB','utf8mb4_bin') for t in names])
    replies.append([(t,c,typ,'YES' if null else 'NO',i,'utf8mb4_bin' if typ.startswith('varchar(') else None) for t in names for i,(c,typ,null) in enumerate(dims._columns(t),1)])
    replies.append([(t,'PRIMARY',0,i,c,None,'BTREE') for t in names for i,c in enumerate(('dataset_id',dims._SPECS[t][0][0]),1)])
    class Cursor:
        def __init__(self,data): self.data=iter(data)
        def execute(self,sql,args): assert sql.startswith('SELECT ') and set(args)==set(names)
        def fetchall(self): return next(self.data)
    dims._verify(Cursor(deepcopy(replies)))
    if change=='type':
        row=list(replies[1][0]); row[2]='varchar(64)'; replies[1][0]=tuple(row)
    elif change=='order':
        row=list(replies[1][0]); row[4]=999; replies[1][0]=tuple(row)
    elif change=='index': replies[2].append(('dim_department','unknown_index',1,1,'dataset_id',None,'BTREE'))
    else: replies[0][0]=('dim_department','MyISAM','utf8mb4_bin')
    with pytest.raises(sources.OdsError,match='^N8_SCHEMA_INVALID$'): dims._verify(Cursor(replies))


def test_initialize_partial_known_schema_only_creates_missing(monkeypatch):
    calls=[]; verified=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def execute(self,sql,*args): calls.append(sql)
        def fetchall(self): return [('dim_department',)]
    class Connection:
        def cursor(self): return Cursor()
        def close(self): calls.append('CLOSE')
    monkeypatch.setattr(clean,'_connect',lambda:Connection())
    monkeypatch.setattr(dims,'_verify',lambda cursor,names=None:verified.append(names))
    assert dims.initialize_dim_schema() is True
    assert verified==[{'dim_department'},None]
    creates=[sql for sql in calls if sql.startswith('CREATE ')]
    assert len(creates)==3 and all(not sql.startswith('CREATE TABLE `dim_department`') for sql in creates)
    assert calls[-1]=='CLOSE'


@pytest.mark.parametrize('url',['absent','different'])
def test_real_fixture_guard_before_any_connection(monkeypatch,url):
    if url=='different': monkeypatch.setenv('ODS_TEST_DATABASE_URL','mysql://SIM_OTHER:SIM@127.0.0.1:13306/hospital_test')
    calls=[]
    def forbidden(**kw):
        calls.append(1)
        raise AssertionError('unit attempted DB')
    monkeypatch.setattr(sources,'connect_database',forbidden)
    fixture=mysql_dims.__wrapped__(monkeypatch)
    with pytest.raises(pytest.skip.Exception if url=='absent' else pytest.fail.Exception): next(fixture)
    assert calls==[]


@pytest.mark.parametrize('error,rollback_fails,expected', [
    (RuntimeError(1213, 'SIM_PRIVATE'),False,'N8_TX_FAILED'),
    (ValueError(1205, 'SIM_PRIVATE'),False,'N8_TX_FAILED'),
    (sources.OdsError('N8_TX_RETRYABLE'),False,'N8_TX_FAILED'),
    (pymysql.err.OperationalError(1213,'SIM_PRIVATE'),False,'N8_TX_RETRYABLE'),
    (pymysql.err.OperationalError(1205,'SIM_PRIVATE'),False,'N8_TX_RETRYABLE'),
    (pymysql.err.OperationalError(2006,'SIM_PRIVATE'),False,'N8_TX_RETRYABLE'),
    (pymysql.err.OperationalError(2013,'SIM_PRIVATE'),False,'N8_TX_RETRYABLE'),
    (pymysql.err.OperationalError(1045,'SIM_PRIVATE'),False,'N8_TX_FAILED'),
    (pymysql.err.IntegrityError(1213,'SIM_PRIVATE'),False,'N8_TX_FAILED'),
    (pymysql.err.OperationalError(1213,'SIM_PRIVATE'),True,'N8_ROLLBACK_FAILED'),
])
def test_retry_requires_driver_type_allowed_code_confirmed_rollback(monkeypatch,error,rollback_fails,expected):
    calls=[]
    class Connection:
        def cursor(self): raise error
        def rollback(self):
            calls.append('rollback')
            if rollback_fails: raise RuntimeError('SIM_PRIVATE_ROLLBACK')
        def close(self): calls.append('close')
    monkeypatch.setattr(clean,'_connect',lambda:Connection())
    with pytest.raises(sources.OdsError) as caught: dims._refresh(None,None,'test')
    assert caught.value.code==expected and str(caught.value)==expected
    assert calls==['rollback','close']


@pytest.mark.parametrize('fault,expected', [('commit','N8_COMMIT_UNKNOWN'),('report','N8_REPORT_WRITE_FAILED')])
def test_driver_retry_code_after_commit_never_retryable(monkeypatch,fault,expected):
    source,report=n7_report(monkeypatch); calls=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def execute(self,*a): pass
    class Connection:
        def cursor(self): return Cursor()
        def commit(self):
            calls.append('commit')
            if fault=='commit': raise pymysql.err.OperationalError(1213,'SIM_PRIVATE')
        def rollback(self): calls.append('rollback')
        def close(self): calls.append('close')
    monkeypatch.setattr(clean,'_connect',lambda:Connection())
    monkeypatch.setattr(clean,'_registry',lambda *a,**k:('C8_SIM','2024-02-29',()))
    monkeypatch.setattr(clean,'load_quality_snapshot_from_cursor',lambda *a,**k:source)
    monkeypatch.setattr(dims,'_read_report',lambda *a,dimension=False:(None,None) if dimension else (report,'b'*64))
    monkeypatch.setattr(dims,'_upstream',lambda *a:None)
    monkeypatch.setattr(dims,'_date_snapshot',lambda *a:snapshot())
    monkeypatch.setattr(dims,'_store',lambda *a,**kw:dict(dim_sha256='c'*64))
    def no_report(*a): raise pymysql.err.OperationalError(1213,'SIM_PRIVATE')
    monkeypatch.setattr(dims,'_write_report',no_report)
    with pytest.raises(sources.OdsError,match='^'+expected+'$'): dims._refresh(None,None,'test')
    assert calls==['commit','close']


def memory_store(monkeypatch,stored):
    writes=[]
    class Cursor:
        def execute(self,sql,params):
            assert sql.startswith('DELETE ')
            table=sql.split('`')[1]; writes.append(table); stored[table]=[]
        def executemany(self,sql,params):
            table=sql.split('`')[1]; columns=[c for c,_,_ in dims._columns(table)]
            stored[table]=[dict(zip(columns,row)) for row in params]
    monkeypatch.setattr(dims,'_verify',lambda *a:None)
    monkeypatch.setattr(dims,'_read_rows',lambda *a:deepcopy(stored))
    return Cursor(),writes


@pytest.mark.parametrize('change', [
    'campus','icd','holiday','scope','mapping','name','code_missing','code_extra',
    'partial_empty','date_empty','date_gap','date_duplicate','date_year','date_weekend',
    'date_mapping','date_invalid','null_as_zero',
])
def test_existing_dimensions_drift_rejected_before_any_dml(monkeypatch,change):
    result=dims.dimension_rows(snapshot()); stored=deepcopy(result['rows'])
    if change=='campus': stored['dim_department'][0]['campus_code']='SIM_EXTERNAL'
    elif change=='icd': stored['dim_disease'][0]['icd_code']='SIM_EXTERNAL'
    elif change=='holiday': stored['dim_date'][0]['is_holiday']=1
    elif change=='scope': stored['dim_fee_type'][0]['scope']='REAL'
    elif change=='mapping': stored['dim_disease'][0]['mapping_version']='UNKNOWN_VERSION'
    elif change=='name': stored['dim_department'][0]['dept_name']='SIM_OTHER_NAME'
    elif change=='code_missing': stored['dim_department'].pop()
    elif change=='code_extra': stored['dim_disease'].append(dict(stored['dim_disease'][0],disease_code='SIM_DX_99'))
    elif change=='partial_empty': stored['dim_fee_type']=[]
    elif change=='date_empty': stored['dim_date']=[]
    elif change=='date_gap': stored['dim_date'].pop(1)
    elif change=='date_duplicate': stored['dim_date'].insert(1,deepcopy(stored['dim_date'][0]))
    elif change=='date_year': stored['dim_date'][0]['calendar_year']=1900
    elif change=='date_weekend': stored['dim_date'][0]['is_weekend']=1-stored['dim_date'][0]['is_weekend']
    elif change=='date_mapping': stored['dim_date'][0]['mapping_version']='UNKNOWN_VERSION'
    elif change=='date_invalid': stored['dim_date'][0]['calendar_date']='SIM_INVALID_DAY'
    elif change=='null_as_zero': stored['dim_disease'][0]['icd_code']=0
    original=deepcopy(stored); cursor,writes=memory_store(monkeypatch,stored)
    with pytest.raises(sources.OdsError,match='^N8_DIMENSION_INVALID$'): dims._store(cursor,result)
    assert stored==original and writes==[]


@pytest.mark.parametrize('state',['initial_empty','idempotent','extend','shrink'])
def test_valid_previous_templates_and_date_range_changes(monkeypatch,state):
    source=snapshot(); old=dims.dimension_rows(source)
    if state=='initial_empty': stored={table:[] for table in dims._SPECS}
    else: stored=deepcopy(old['rows'])
    if state=='extend': source['dates']['his_charge']=['2022-01-01','2026-01-01']
    elif state=='shrink':
        old_source=snapshot(); old_source['dates']['his_charge']=['2022-01-01','2026-01-01']
        stored=deepcopy(dims.dimension_rows(old_source)['rows'])
    expected=dims.dimension_rows(source); cursor,writes=memory_store(monkeypatch,stored)
    certificate=certificate_for(old if state!='shrink' else dims.dimension_rows(old_source)) if state in ('extend','shrink') else None
    result=dims._store(cursor,expected,previous_report=certificate)
    assert stored==expected['rows']
    assert writes==([] if state=='idempotent' else list(dims._SPECS))
    assert result['dim_sha256']==dims._hash(stored)


def test_all_unit_driver_and_tcp_paths_fail_before_contact(monkeypatch):
    # Spies stand for the real entry points; installing the guard must bypass all.
    contacts=[]
    def contact(*a,**kw): contacts.append(1); raise AssertionError('UNGUARDED_CONTACT')
    targets=[(pymysql,'connect'),(pymysql.connections.Connection,'connect'),
             (socket,'create_connection'),(socket.socket,'connect'),(socket.socket,'connect_ex')]
    for owner,name in targets: monkeypatch.setattr(owner,name,contact)
    block_unit_connections(monkeypatch)
    connection=pymysql.connections.Connection(defer_connect=True)
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as tcp:
        actions=[lambda:pymysql.connect(host='127.0.0.1',user='SIM',password='SIM'),
                 connection.connect,lambda:socket.create_connection(('127.0.0.1',13306)),
                 lambda:tcp.connect(('127.0.0.1',13306)),lambda:tcp.connect_ex(('127.0.0.1',13306))]
        for action in actions:
            with pytest.raises(AssertionError,match='^C8_PURE_NETWORK_BLOCKED$'): action()
    assert contacts==[]



def test_driver_error_without_connection_cannot_confirm_rollback(monkeypatch):
    def connect(): raise pymysql.err.OperationalError(1213,'SIM_PRIVATE')
    monkeypatch.setattr(clean,'_connect',connect)
    with pytest.raises(sources.OdsError,match='^N8_TX_FAILED$'):
        dims._refresh(None,None,'test')



def certificate_for(result):
    report=dim_report()
    report.update(dataset_id=result['dataset_id'],dim_sha256=dims._hash(result['rows']),
                  row_counts=deepcopy(result['row_counts']),source_completeness=dict(result['source_completeness'],service_unsplit_rows=0))
    for key in ('date_min','date_max','required_date_start','required_date_end'):
        report[key]=result[key]
    return report


@pytest.mark.parametrize('trim',['single_day','remove_start','remove_end'])
@pytest.mark.parametrize('certified',[False,True])
def test_calendar_boundary_damage_never_repaired(monkeypatch,trim,certified):
    desired=dims.dimension_rows(snapshot()); stored=deepcopy(desired['rows'])
    if trim=='single_day': stored['dim_date']=stored['dim_date'][1:2]
    elif trim=='remove_start': stored['dim_date'].pop(0)
    else: stored['dim_date'].pop()
    before=deepcopy(stored); cursor,writes=memory_store(monkeypatch,stored)
    with pytest.raises(sources.OdsError,match='^N8_DIMENSION_INVALID$'):
        dims._store(cursor,desired,previous_report=certificate_for(desired) if certified else None)
    assert stored==before and writes==[]


@pytest.mark.parametrize('change',['hash','count','start','end','date_min','date_max','template_relation'])
def test_changed_old_certificate_cannot_authorize_new_range(monkeypatch,change):
    old=dims.dimension_rows(snapshot()); report=certificate_for(old)
    source=snapshot(); source['dates']['finance_excel']=['2027-01-01']; desired=dims.dimension_rows(source)
    if change=='hash': report['dim_sha256']='0'*64
    elif change=='count': report['row_counts']['dim_date']-=1
    elif change=='start': report['required_date_start']='2023-03-01'
    elif change=='end': report['required_date_end']='2025-02-27'
    elif change=='date_min': report['date_min']='2024-03-01'
    elif change=='date_max': report['date_max']='2024-02-28'
    else: report['date_min']=report['date_max']='2025-02-28'
    stored=deepcopy(old['rows']); before=deepcopy(stored); cursor,writes=memory_store(monkeypatch,stored)
    with pytest.raises(sources.OdsError,match='^N8_DIMENSION_INVALID$'):
        dims._store(cursor,desired,previous_report=report)
    assert stored==before and writes==[]


@pytest.mark.parametrize('existing',['all_empty','old_range'])
def test_bootstrap_or_range_change_require_correct_history_context(monkeypatch,existing):
    old=dims.dimension_rows(snapshot()); source=snapshot(); source['dates']['finance_excel']=['2027-01-01']; desired=dims.dimension_rows(source)
    stored={t:[] for t in dims._SPECS} if existing=='all_empty' else deepcopy(old['rows'])
    before=deepcopy(stored); cursor,writes=memory_store(monkeypatch,stored)
    certificate=certificate_for(old) if existing=='all_empty' else None
    with pytest.raises(sources.OdsError,match='^N8_DIMENSION_INVALID$'):
        dims._store(cursor,desired,previous_report=certificate)
    assert stored==before and writes==[]


@pytest.mark.parametrize('history',['missing','current','stale_hash','stale_count'])
def test_equal_trusted_desired_allows_nondestructive_recertification(monkeypatch,history):
    desired=dims.dimension_rows(snapshot()); stored=deepcopy(desired['rows']); report=certificate_for(desired)
    if history=='missing': report=None
    elif history=='stale_hash': report['dim_sha256']='0'*64
    elif history=='stale_count': report['row_counts']['dim_date']=1
    cursor,writes=memory_store(monkeypatch,stored)
    result=dims._store(cursor,desired,previous_report=report)
    assert stored==desired['rows'] and writes==[] and result['dim_sha256']==dims._hash(stored)


@pytest.mark.parametrize('field,value',[('dataset_id','OTHER'),('template_version','unknown'),('schema_version','unknown'),('env','unknown')])
def test_equal_desired_never_bypasses_invalid_report_identity(monkeypatch,field,value):
    desired=dims.dimension_rows(snapshot()); stored=deepcopy(desired['rows']); report=certificate_for(desired); report[field]=value
    cursor,writes=memory_store(monkeypatch,stored)
    with pytest.raises(sources.OdsError,match='^N8_REPORT_IDENTITY_CONFLICT$'):
        dims._store(cursor,desired,previous_report=report)
    assert writes==[] and stored==desired['rows']


def test_prepare_without_certificate_rejects_other_nonempty_range(monkeypatch):
    old=dims.dimension_rows(snapshot()); stored=deepcopy(old['rows']); cursor,writes=memory_store(monkeypatch,stored)
    with pytest.raises(sources.OdsError,match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor,'C8_SIM','2027-01-01','2027-01-01')
    assert writes==[] and stored==old['rows']



@pytest.mark.parametrize('state',['trimmed','deleted','certified_extend','equal_stale_report'])
def test_refresh_passes_old_certificate_into_same_cursor_write_gate(monkeypatch,state):
    n7,report=n7_report(monkeypatch)
    old=dims.dimension_rows(snapshot()); certificate=certificate_for(old)
    certificate.update(batch_date=n7['batch_date'],clean_run_id='33333333-3333-4333-8333-333333333333',source_fingerprint='c'*64)
    current=snapshot(); current.update(batch_date=n7['batch_date'],source_fingerprint='d'*64)
    stored=deepcopy(old['rows'])
    if state=='trimmed': stored['dim_date'].pop()
    elif state=='deleted': stored={t:[] for t in dims._SPECS}
    elif state=='certified_extend': current['dates']['finance_excel']=['2027-01-01']
    else: certificate['dim_sha256']='0'*64
    before=deepcopy(stored); memory,writes=memory_store(monkeypatch,stored); calls=[]; published=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,*args):
            if sql.startswith(('SET TRANSACTION','START TRANSACTION')): return
            return memory.execute(sql,*args)
        def executemany(self,*args): return memory.executemany(*args)
    cursor=Cursor()
    class Connection:
        def cursor(self): return cursor
        def commit(self): calls.append('commit')
        def rollback(self): calls.append('rollback')
        def close(self): calls.append('close')
    monkeypatch.setattr(clean,'_connect',lambda:Connection())
    monkeypatch.setattr(clean,'_registry',lambda *a,**k:('C8_SIM',n7['batch_date'],()))
    monkeypatch.setattr(clean,'load_quality_snapshot_from_cursor',lambda *a,**k:n7)
    monkeypatch.setattr(dims,'_read_report',lambda *a,dimension=False:(certificate,'a'*64) if dimension else (report,'b'*64))
    def date_snapshot(actual,*args):
        assert actual is cursor
        return current
    monkeypatch.setattr(dims,'_date_snapshot',date_snapshot)
    monkeypatch.setattr(dims,'_write_report',published.append)
    if state in ('trimmed','deleted'):
        with pytest.raises(sources.OdsError,match='^N8_DIMENSION_INVALID$'):
            dims.refresh_dimensions(dataset_id='C8_SIM',env='test')
        assert stored==before and writes==[] and calls==['rollback','close'] and published==[]
    else:
        result=dims.refresh_dimensions(dataset_id='C8_SIM',env='test')
        assert result['dim_sha256']==dims._hash(stored) and calls==['commit','close']
        assert stored==dims.dimension_rows(current)['rows'] and published==[result]
        assert writes==([] if state=='equal_stale_report' else list(dims._SPECS))
