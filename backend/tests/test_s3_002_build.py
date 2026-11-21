"""W8 pure contracts; real execution belongs to coordinator."""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import pandas as pd
import pytest
from etl.jobs import build_dwd as build

OLD_SHA = 'e5bdacfcdd72866f25386daa3b8a99f14f777b0098a874d32823a87f83f0f9a1'

def test_schema_grains():
    assert len(build.VISIT_COLUMNS) == 21
    assert len(build.AUDIT_COLUMNS) == 18
    assert len(build.new_ddl()) == 2


def test_course_dimensions_cover_cleaning_mappings_without_invented_attributes():
    from etl.config import course_mapping
    from etl.config.source_config import COURSE_DATASET
    from etl.jobs import build_dims as dims

    rows = dims._dimension_range(COURSE_DATASET, '2025-04-01', '2025-06-30')['rows']
    assert {row['dept_code'] for row in rows['dim_department']} == set(course_mapping.DEPARTMENTS)
    assert {row['fee_type_code'] for row in rows['dim_fee_type']} == set(course_mapping.FEE_TYPES.values())
    assert {row['disease_code'] for row in rows['dim_disease']} == set(course_mapping.DISEASE_CODES)
    assert all(row['icd_code'] is None and row['drg_group'] is None for row in rows['dim_disease'])
    assert {row['mapping_version'] for items in rows.values() for row in items} == {course_mapping.COURSE_VERSION}

def test_old_prefix_mutation(tmp_path):
    raw = build.LEGACY_SQL.read_bytes()
    prefix = raw[:4366]
    assert hashlib.sha256(prefix).hexdigest() == OLD_SHA
    changed = tmp_path / 'mutated.sql'
    changed.write_bytes(b'X' + prefix[1:])
    assert hashlib.sha256(changed.read_bytes()).hexdigest() != OLD_SHA
    with pytest.raises(AssertionError):
        assert hashlib.sha256(changed.read_bytes()).hexdigest() == OLD_SHA

@pytest.mark.parametrize('value', [None, Decimal('0.00'), Decimal('-1.23')])
def test_hash_null_decimal(value):
    row = {'dataset_id':'SIM', 'visit_type':'OUTPATIENT', 'visit_id':'X', 'amount':value}
    assert build.rows_digest(('dataset_id','visit_type','visit_id','amount'), [row], ('dataset_id','visit_type','visit_id'))
    assert build.rows_digest(('amount',), [{'amount':None}], ()) != build.rows_digest(('amount',), [{'amount':Decimal('0.00')}], ())

def test_charge_microseconds_hash():
    a = {'etl_load_time':datetime(2024,1,1,0,0,0,123456)}
    b = {'etl_load_time':datetime(2024,1,1,0,0,0,123457)}
    assert build.rows_digest(('etl_load_time',),[a],()) != build.rows_digest(('etl_load_time',),[b],())

@pytest.mark.parametrize('total,drug,consumable,expected', [
    ('10.00','7.00','5.00','-2.00'), (None,'7.00','5.00',None),
    ('-10.00','-7.00','5.00','-8.00'), ('10.00',None,'5.00',None)])
def test_typed_visit_metadata_null_and_residual(total, drug, consumable, expected):
    from datetime import date
    def money(v): return None if v is None else Decimal(v)
    common = dict(dataset_id='SIM',visit_id='X',source_row_number=1,
                  source_batch_date=date(2024,1,1),etl_batch_date=date(2024,1,2),
                  etl_load_time=datetime(2024,1,2,0,0,0,123456),patient_key=None,
                  dept_code='SIM_DEPT_01',gender='M',age=None)
    outpatient = dict(common,visit_type='OUTPATIENT',visit_date=date(2024,1,1),fee_amount=None)
    inpatient = dict(common,admit_date=date(2024,1,1),discharge_date=date(2024,1,1),
                     disease_code='SIM_DX_01',stay_days=None,total_fee=money(total),
                     drug_fee=money(drug),consumable_fee=money(consumable))
    rows = build.visit_candidates([outpatient],[inpatient])
    assert len(rows) == 2 and {r['visit_type'] for r in rows} == {'OUTPATIENT','INPATIENT'}
    for row in rows:
        assert set(row) == set(build.VISIT_COLUMNS)
        assert row['etl_load_time'] == common['etl_load_time']
        assert row['patient_key'] is None and row['age'] is None
    ip = next(r for r in rows if r['visit_type'] == 'INPATIENT')
    op = next(r for r in rows if r['visit_type'] == 'OUTPATIENT')
    assert ip['residual_fee'] == money(expected) and ip['calendar_span_days'] == 0
    assert op['disease_code'] is None and op['source_total_fee'] is None
    with pytest.raises(build.OdsError, match='^N8_VISIT_AMBIGUOUS$'):
        build.visit_candidates([outpatient,outpatient],[])


def test_residual_overflow():
    from datetime import date
    row = dict(dataset_id='SIM',visit_id='X',source_row_number=1,
               source_batch_date=date(2024,1,1),etl_batch_date=date(2024,1,2),
               etl_load_time=datetime(2024,1,2),patient_key=None,dept_code='SIM_DEPT_01',
               gender='F',age=None,admit_date=date(2024,1,1),discharge_date=None,
               disease_code='SIM_DX_01',stay_days=None,total_fee=Decimal('9999999999999999.99'),
               drug_fee=Decimal('-9999999999999999.99'),consumable_fee=Decimal('0.00'))
    with pytest.raises(build.OdsError,match='^N8_AMOUNT_OVERFLOW$'):
        build.visit_candidates([],[row])


def test_charge_parent_conflict_and_full_preservation():
    visit = dict(dataset_id='SIM',visit_type='OUTPATIENT',visit_id='V',patient_key='a')
    charge = dict(dataset_id='SIM',visit_type='OUTPATIENT',visit_id='V',patient_key='a',charge_id='C')
    assert build.charge_candidates([charge],[visit]) == [charge]
    assert build.charge_candidates([charge],[visit])[0] is not charge
    with pytest.raises(build.OdsError,match='^N8_PATIENT_MISMATCH$'):
        build.charge_candidates([dict(charge,patient_key='b')],[visit])
    with pytest.raises(build.OdsError,match='^N8_VISIT_REFERENCE_INVALID$'):
        build.charge_candidates([charge],[])


def test_range_rejects_partial_before_writes():
    assert build.full_range(None,None,'2024-01-01','2024-02-01') == ('2024-01-01','2024-02-01')
    with pytest.raises(build.OdsError,match='^N8_RANGE_NOT_FULL$'):
        build.full_range('2024-01-02','2024-02-01','2024-01-01','2024-02-01')


@pytest.mark.parametrize('scope', ['VISIT', 'CHARGE'])
def test_course_build_date_arguments(monkeypatch, scope):
    from etl.jobs import clean_dwd

    def capture(start, end, **options):
        assert options == dict(dataset_id='SIM', batch_date='2024-01-01', env='test', scope=scope)
        return start, end

    monkeypatch.setattr(build, '_public_partial', capture)
    builder = getattr(clean_dwd, f'build_dwd_{scope.lower()}')
    options = dict(dataset_id='SIM', batch_date='2024-01-01', env='test')
    start, end = '2024-01-01', '2024-02-01'
    assert builder(start, **options) == (start, start)
    assert builder(start, end, **options) == (start, end)
    assert builder(None, None, **options) == (None, None)
    assert builder(start, None, **options) == (start, None)
    assert builder(date.fromisoformat(start), **options) == (start, start)
    assert builder((date.fromisoformat(start), datetime.fromisoformat(end)), **options) == (start, end)
    assert builder([start, end], **options) == (start, end)


def test_course_visit_frame_builds_details_without_database(monkeypatch):
    from etl.jobs.clean_dwd import build_dwd_visit

    def unexpected_database(*args, **kwargs):
        pytest.fail('DataFrame build called the database path')

    monkeypatch.setattr(build, '_public_partial', unexpected_database)
    source = pd.DataFrame([
        {'visit_id': 1, 'visit_type': 'OUTPATIENT', 'visit_date': '2024-01-01', 'fee_amount': '9.00'},
        {'visit_id': 2, 'visit_type': 'INPATIENT', 'admit_date': '2024-01-01', 'discharge_date': '2024-01-03',
         'total_fee': '12.00', 'drug_fee': '4.00', 'consumable_fee': '3.00', 'patient_name': 'SIM_PRIVATE'},
    ])
    result = build_dwd_visit(source)
    assert result['visit_id'].tolist() == [1, 2]
    assert result['visit_id'].is_unique
    assert result.loc[1, 'calendar_span_days'] == 2
    assert result.loc[1, 'residual_fee'] == Decimal('5.00')
    assert 'patient_name' not in result
    assert 'residual_fee' not in source
    with pytest.raises(build.OdsError, match='^N8_VISIT_AMBIGUOUS$'):
        build_dwd_visit(pd.concat([source, source]))


def test_course_charge_frame_keeps_unique_charge_rows_and_types():
    from etl.jobs.clean_dwd import build_dwd_charge

    result = build_dwd_charge(pd.DataFrame([
        {'charge_id': 'C1', 'visit_id': 'V1', 'fee_date': '2024-01-02', 'amount': '4.75', 'phone': 'SIM_PRIVATE'},
        {'charge_id': 'C2', 'visit_id': 'V1', 'fee_date': '2024-01-02', 'amount': '2.00'},
    ]))
    assert result['charge_id'].tolist() == ['C1', 'C2']
    assert result['amount'].tolist() == [Decimal('4.75'), Decimal('2.00')]
    assert result['fee_date'].tolist() == [date(2024, 1, 2)] * 2
    assert 'phone' not in result


@pytest.mark.parametrize('start,end,batch,detail', [
    ([], [], None, 'DATE_RANGE_INVALID_TYPE (list, list)'),
    ('2024-01-01', None, None, 'DATE_RANGE_INCOMPLETE'),
    ('2024-01-02', '2024-01-01', None, 'DATE_RANGE_INVALID'),
    ('2024-01-01', '2024-01-01', 'course-check', 'BATCH_DATE_INVALID'),
])
def test_public_build_distinguishes_date_argument_errors(start, end, batch, detail):
    with pytest.raises(build.OdsError) as error:
        build._public_partial(start, end, dataset_id=None, batch_date=batch, env='dev', scope='VISIT')
    assert str(error.value) == f'N8_ARGUMENTS_INVALID: {detail}'


def test_public_build_preserves_database_config_error(monkeypatch):
    monkeypatch.setenv('ODS_DATABASE_URL', 'SIM_INVALID_URL')
    with pytest.raises(build.OdsError, match='^ODS_DATABASE_URL_INVALID$'):
        build._public_partial('2024-01-01', '2024-01-01', dataset_id=None,
                              batch_date=None, env='dev', scope='VISIT')


def test_transaction_typed_retry_and_commit_unknown():
    import pymysql
    class Connection:
        commits = 0
        rollbacks = 0
        def rollback(self): self.rollbacks += 1
        def commit(self): self.commits += 1; raise RuntimeError('SIM_DSN')
    c = Connection()
    with pytest.raises(build.OdsError,match='^N8_TX_RETRYABLE$'):
        build._transaction(c, lambda: (_ for _ in ()).throw(pymysql.err.OperationalError(1213,'SIM_DSN')))
    assert c.rollbacks == 1 and c.commits == 0
    with pytest.raises(build.OdsError,match='^N8_TX_FAILED$'):
        build._transaction(c, lambda: (_ for _ in ()).throw(RuntimeError(1213,'SIM_DSN')))
    with pytest.raises(build.OdsError,match='^N8_COMMIT_UNKNOWN$'):
        build._transaction(c, lambda: {})
    assert c.commits == 1


@pytest.mark.parametrize('stage',['dimension','visit','charge','audit','recheck'])
def test_core_fault_protocol_rolls_back_once(monkeypatch,stage):
    from etl.jobs import clean_dwd as clean, build_dims as dims
    from contextlib import nullcontext
    from datetime import date
    snapshot={'clean_run_id':'00000000-0000-0000-0000-000000000001','mapping_version':'node07-sim-mapping-v1','privacy_key_id':'SIM','tables':{t:{'audit':{},'dwd':{'rows':[]}} for t in clean.TABLES}}
    class Cursor:
        def execute(self,*args): pass
    class Connection:
        commits=0; rollbacks=0
        def cursor(self): return nullcontext(Cursor())
        def rollback(self): self.rollbacks+=1
        def commit(self): self.commits+=1
    c=Connection(); writes=[]; stored={}
    monkeypatch.setattr(clean,'_connect',lambda:c)
    monkeypatch.setattr(clean,'_close',lambda c:None)
    monkeypatch.setattr(build,'_lock_inputs',lambda *a:('SIM','2024-01-01',()))
    monkeypatch.setattr(build,'_verify_schema',lambda c:None)
    monkeypatch.setattr(clean,'load_quality_snapshot_from_cursor',lambda *a,**k:snapshot)
    monkeypatch.setattr(dims,'_read_report',lambda *a,**k:({},'0'*64))
    monkeypatch.setattr(build,'_read_quality_report',lambda *a:({},'0'*64))
    monkeypatch.setattr(build,'_report_preflight',lambda *a:None)
    monkeypatch.setattr(dims,'_upstream',lambda *a:None)
    monkeypatch.setattr(dims,'_date_snapshot',lambda *a:{})
    monkeypatch.setattr(dims,'dimension_rows',lambda *a:dict(date_min='2024-01-01',date_max='2024-01-01'))
    monkeypatch.setattr(build,'_previous_certificate',lambda *a:None)
    monkeypatch.setattr(build,'_stats',lambda *a:{})
    def dimension(*a,**k):
        writes.append('dimension')
        if stage=='dimension': raise RuntimeError('SIM_DSN_SECRET')
        return {'dim_sha256':'0'*64}
    monkeypatch.setattr(dims,'prepare_dimensions',dimension)
    def replace(cursor,table,columns,rows,dataset):
        label={build.VISIT:'visit','dwd_charge_detail':'charge',build.AUDIT:'audit'}[table]
        writes.append(label)
        stored[table]=rows
        if stage==label: raise RuntimeError('SIM_DSN_SECRET')
    monkeypatch.setattr(build,'_replace',replace)
    monkeypatch.setattr(build,'_read',lambda cursor,table,*a: stored.get(table,[]))
    def registry(*a):
        if stage=='recheck': raise RuntimeError('SIM_DSN_SECRET')
        return ('SIM','2024-01-01',())
    monkeypatch.setattr(clean,'_registry',registry)
    with pytest.raises(build.OdsError) as error:
        build._run_build_locked(env='test')
    assert str(error.value)==error.value.code and 'SECRET' not in str(error.value)
    assert c.rollbacks==1 and c.commits==0
    assert writes[-1]==stage if stage!='recheck' else writes[-1]=='audit'


def test_schema_unknown_rejected():
    class Cursor:
        def execute(self,*a): pass
        def fetchall(self): return []
    with pytest.raises(build.OdsError,match='^N8_SCHEMA_INVALID$'):
        build._verify_new(Cursor())


def test_lock_sequence_current_reads_only():
    from etl.jobs import clean_dwd as clean, build_dims as dims
    from etl.config.source_config import SOURCES
    from datetime import date
    class Cursor:
        queries=[]
        def execute(self,sql,params=None): self.queries.append(sql)
        def fetchall(self):
            if len(self.queries)==1:
                return [(n,'SIM','SIM','SIM',date(2024,1,1)) for n in sorted(SOURCES)]
            return []
    c=Cursor(); assert build._lock_inputs(c,None)[:2]==('SIM','2024-01-01')
    assert all(sql.endswith('FOR UPDATE') for sql in c.queries)
    names=[q.split('`')[1] for q in c.queries[1:]]
    assert names==[clean.AUDIT]+sorted(s['table'] for s in SOURCES.values())+sorted(clean.TABLES)+sorted(dims._SPECS)+[build.VISIT,build.AUDIT]
    assert names.count('dwd_charge_detail')==1


def test_rollback_failure_safe():
    class Connection:
        def rollback(self): raise RuntimeError('SIM_DSN')
    with pytest.raises(build.OdsError,match='^N8_ROLLBACK_FAILED$'):
        build._transaction(Connection(),lambda:(_ for _ in ()).throw(RuntimeError('SIM_DSN')))


def test_bad_audit_never_falls_back(monkeypatch):
    from etl.jobs import build_dims as dims
    calls=[]
    monkeypatch.setattr(build,'_read',lambda *a:[{'dataset_id':'SIM'}])
    monkeypatch.setattr(dims,'_read_report',lambda *a,**k:calls.append(1))
    with pytest.raises(build.OdsError,match='^N8_DIMENSION_INVALID$'):
        build._previous_certificate(None,'SIM','2024-01-01','test')
    assert calls==[]


def test_forged_n7_pass_recomputed_rejected():
    from etl.jobs import build_dims as dims
    with pytest.raises(build.OdsError,match='^N8_UPSTREAM_GATE_FAILED$'):
        dims._upstream({}, {'gate_status':'PASS'}, 'test')


def test_missing_table_driver_is_migration_after_rollback():
    import pymysql
    class Connection:
        rollbacks=0
        def rollback(self): self.rollbacks+=1
    c=Connection()
    with pytest.raises(build.OdsError,match='^N8_MIGRATION_REQUIRED$'):
        build._transaction(c,lambda:(_ for _ in ()).throw(pymysql.err.ProgrammingError(1146,'SIM_DSN')))
    assert c.rollbacks==1


@pytest.mark.parametrize('precision',[5,7,8])
def test_money_context_independent(precision):
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec=precision; ctx.Emax=2
        assert build._residual(Decimal('600'),Decimal('-600'),Decimal('700.03'))==Decimal('499.97')
        assert build._residual(Decimal('9999999999999999.99'),Decimal('0'),Decimal('0'))==Decimal('9999999999999999.99')
        with pytest.raises(build.OdsError,match='^N8_AMOUNT_OVERFLOW$'):
            build._residual(Decimal('9999999999999999.99'),Decimal('-1'),Decimal('0'))



def test_report_reader_missing_no_mkdir(tmp_path,monkeypatch):
    from etl.jobs import build_dims as dims
    monkeypatch.setattr(dims.quality,'_LOG_ROOT',tmp_path/'absent')
    with pytest.raises(build.OdsError): build._read_quality_report('test','2024-01-01')
    assert not (tmp_path/'absent').exists()


def test_all_context_rejects_before_env_config():
    from etl.scheduler import scheduler
    token=scheduler._CURRENT.set(object())
    try:
        with pytest.raises(build.OdsError,match='^N8_RUN_CONTEXT_ACTIVE$'): build.run_build_all(env='test')
    finally: scheduler._CURRENT.reset(token)


def report_fixture():
    from etl.jobs import clean_dwd as clean
    stats={k:{t:0 for t in clean.TABLES} for k in ('ods_rows','base_rows','duplicate_removed','quarantined_rows')}
    stats.update({k:0 for k in ('visit_rows','charge_rows','missing_total_rows','missing_component_rows','negative_residual_rows','missing_calendar_span_rows','disease_not_applicable_rows')})
    stats['association_counts']={k:dict(slots=0,matched=0,not_applicable=0) for k in ('department','date','disease','fee_type','visit')}
    return dict(schema_version='node08-build-v1',dataset_id='W8_SIM',build_run_id='11111111-1111-4111-8111-111111111111',clean_run_id='22222222-2222-4222-8222-222222222222',batch_date='2024-02-29',requested_start=None,requested_end=None,effective_start='2024-02-29',effective_end='2024-02-29',build_scope='ALL',gate_status='PASS',rows={'dwd_visit_detail':0,'dwd_charge_detail':0},hashes={k:'a'*64 for k in ('input','dim','visit','charge')},stats=stats,env='test',generated_at='2024-02-29T12:00:00+00:00',model_version='node08-model-v1',mapping_version='node07-sim-mapping-v1',privacy_key_id='SIM',node07_report_sha256='b'*64,checks={k:'PASS' for k in build.CHECKS},code='N8_BUILD_OK')


@pytest.mark.parametrize('mutation',['extra','bool','nan','uuid','check','hash','scope'])
def test_report_strict_shape(mutation):
    r=report_fixture()
    if mutation=='extra': r['raw_patient']='SIM'
    elif mutation=='bool': r['rows']['dwd_visit_detail']=True
    elif mutation=='nan': r['stats']['visit_rows']=float('nan')
    elif mutation=='uuid': r['build_run_id']='SIM'
    elif mutation=='check': del r['checks']['schema']
    elif mutation=='hash': r['hashes']['charge']='BAD'
    else: r['build_scope']='VISIT'
    with pytest.raises(build.OdsError,match='^N8_STALE$'): build._validate_report(r)


def test_report_publish_read_permissions_identity(tmp_path,monkeypatch):
    import os,json,stat
    from etl.jobs import build_dims as dims
    root=tmp_path/'logs'; monkeypatch.setattr(dims.quality,'_LOG_ROOT',root)
    r=report_fixture(); build._publish_report(r)
    path=root/'test'/build._report_name(r['batch_date'])
    assert json.loads(path.read_bytes())==r
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert build._report_preflight('test',r['batch_date'],r['dataset_id'])==r
    before=path.read_bytes()
    with pytest.raises(build.OdsError,match='^N8_REPORT_IDENTITY_CONFLICT$'):
        build._publish_report(dict(r,dataset_id='OTHER_SIM'))
    assert path.read_bytes()==before
    r['build_run_id']='33333333-3333-4333-8333-333333333333'
    build._publish_report(r); assert json.loads(path.read_bytes())==r


@pytest.mark.parametrize('link',['symlink','hardlink','directory','mode'])
def test_report_unsafe_objects(tmp_path,monkeypatch,link):
    import os
    from etl.jobs import build_dims as dims
    root=tmp_path/'logs'; (root/'test').mkdir(parents=True)
    monkeypatch.setattr(dims.quality,'_LOG_ROOT',root)
    r=report_fixture(); path=root/'test'/build._report_name(r['batch_date'])
    if link=='directory': path.mkdir()
    else:
        other=tmp_path/'other'; other.write_text('SIM'); other.chmod(0o600)
        if link=='symlink': path.symlink_to(other)
        elif link=='hardlink': os.link(other,path)
        else: path.write_text('SIM'); path.chmod(0o644)
    with pytest.raises(build.OdsError,match='^N8_REPORT_PATH_INVALID$'): build._report_preflight('test',r['batch_date'],r['dataset_id'])


@pytest.mark.parametrize('fault',['fsync','replace'])
def test_publish_failure_retains_old_bytes(tmp_path,monkeypatch,fault):
    import os
    from etl.jobs import build_dims as dims
    root=tmp_path/'logs'; monkeypatch.setattr(dims.quality,'_LOG_ROOT',root)
    r=report_fixture(); build._publish_report(r)
    path=root/'test'/build._report_name(r['batch_date']); before=path.read_bytes()
    def fail(*a,**k): raise OSError('SIM_DSN_SECRET')
    monkeypatch.setattr(os,fault,fail)
    with pytest.raises(build.OdsError,match='^N8_REPORT_WRITE_FAILED$'): build._publish_report(r)
    assert path.read_bytes()==before and not list(path.parent.glob('.w8-*.tmp'))


def test_directory_symlink_reader_has_no_side_effect(tmp_path,monkeypatch):
    from etl.jobs import build_dims as dims
    root=tmp_path/'logs'; root.mkdir(); other=tmp_path/'other'; other.mkdir(); (root/'test').symlink_to(other,target_is_directory=True)
    monkeypatch.setattr(dims.quality,'_LOG_ROOT',root)
    with pytest.raises(build.OdsError,match='^N8_REPORT_PATH_INVALID$'): build._read_quality_report('test','2024-01-01')
    assert list(other.iterdir())==[]


def test_public_all_holds_single_lock_through_fresh(monkeypatch):
    from contextlib import contextmanager
    from etl.scheduler import scheduler
    events=[]; active=[]; report=report_fixture()
    monkeypatch.setattr(build,'_validate_public',lambda *a:None)
    @contextmanager
    def lock(*a):
        assert not active; active.append(1); events.append('lock')
        try: yield
        finally: active.pop(); events.append('unlock')
    monkeypatch.setattr(scheduler,'_lock',lock)
    output={k:report[k] for k in build._RETURN_KEYS}; output['gate_status']='NOT_CERTIFIED'
    def core(*a,**k): assert active; events.append('commit'); return output
    def publish(*a): assert active; events.append('publish')
    def fresh(*a): assert active; events.append('fresh'); return {'build_run_id':output['build_run_id']}
    monkeypatch.setattr(build,'_run_build_locked',core)
    monkeypatch.setattr(build,'_read_quality_report',lambda *a:({'mapping_version':report['mapping_version'],'privacy_key_id':'SIM'},'b'*64))
    monkeypatch.setattr(build,'_publish_report',publish)
    monkeypatch.setattr(build,'_verify_current_locked',fresh)
    assert build.run_build_all(env='test')['gate_status']=='PASS'
    assert events==['lock','commit','publish','fresh','unlock']
    def bad(*a): raise build.OdsError('N8_REPORT_WRITE_FAILED')
    monkeypatch.setattr(build,'_publish_report',bad)
    with pytest.raises(build.OdsError,match='^N8_REPORT_WRITE_FAILED$'): build.run_build_all(env='test')
    assert not active


@pytest.mark.parametrize('change',['none','partial','cleanrun','finance','dimension','visit','charge','reporthash','stats','reportidentity'])
def test_fresh_cursor_protocol_recomputes_and_never_controls_transaction(monkeypatch,change):
    from etl.jobs import clean_dwd as clean, build_dims as dims
    from datetime import date
    import json
    report=report_fixture()
    snap=dict(clean_run_id=report['clean_run_id'],mapping_version=report['mapping_version'],privacy_key_id='SIM',tables={t:dict(audit={},dwd={'rows':[]}) for t in clean.TABLES})
    dates={'SIM_dates':['2024-02-29']}
    rows={t:[] for t in dims._SPECS}
    desired=dict(date_min='2024-02-29',date_max='2024-02-29',rows=rows)
    hashes=dict(input=build._input_digest(snap,dates),dim=dims._hash(rows),visit=build.rows_digest(tuple(c for c in build.VISIT_COLUMNS if c!='etl_load_time'),[],('dataset_id','visit_type','visit_id')),charge=build.rows_digest(tuple(clean.TABLES['dwd_charge_detail']['columns']),[],('dataset_id','charge_id')))
    report['hashes']=hashes
    audit=dict(dataset_id='W8_SIM',build_run_id=report['build_run_id'],clean_run_id=report['clean_run_id'],batch_date=date(2024,2,29),requested_start=None,requested_end=None,effective_start=date(2024,2,29),effective_end=date(2024,2,29),mapping_version=report['mapping_version'],privacy_key_id='SIM',build_scope='ALL',stats_json=report['stats'],**{k+'_sha256':v for k,v in hashes.items()})
    monkeypatch.setattr(build,'_verify_schema',lambda *a:None)
    monkeypatch.setattr(clean,'load_quality_snapshot_from_cursor',lambda *a,**k:snap)
    monkeypatch.setattr(build,'_read_quality_report',lambda *a:({},'b'*64))
    evaluated=[]; monkeypatch.setattr(dims,'_upstream',lambda *a:evaluated.append(1))
    monkeypatch.setattr(dims,'_date_snapshot',lambda *a:dates)
    monkeypatch.setattr(dims,'dimension_rows',lambda *a:desired)
    monkeypatch.setattr(dims,'_read_rows',lambda *a:rows)
    monkeypatch.setattr(build,'_previous_certificate',lambda *a:{})
    monkeypatch.setattr(build,'_stats',lambda *a:report_fixture()['stats'])
    def read(cursor,table,*a):
        if table==build.AUDIT: return [audit]
        if change in ('visit','charge') and table==(build.VISIT if change=='visit' else 'dwd_charge_detail'): return [{'SIM':'drift'}]
        return []
    monkeypatch.setattr(build,'_read',read)
    if change=='partial': audit['build_scope']='CHARGE'
    elif change=='cleanrun': snap['clean_run_id']='33333333-3333-4333-8333-333333333333'
    elif change=='finance': dates['SIM_dates'].append('2024-02-28')
    elif change=='dimension': monkeypatch.setattr(dims,'_read_rows',lambda *a:{})
    elif change=='reporthash': report['node07_report_sha256']='c'*64
    elif change=='stats': report['stats']['visit_rows']=1
    elif change=='reportidentity': report['dataset_id']='OTHER_SIM'
    identity_outputs=[]
    if change=='cleanrun': monkeypatch.setattr(build,'_conserve',lambda *a:identity_outputs.append(1))
    class Cursor:
        def __getattr__(self,name): raise AssertionError('unexpected cursor control')
    if change=='none':
        assert build.verify_build_freshness_from_cursor(Cursor(),dataset_id='W8_SIM',batch_date='2024-02-29',env='test',report=report)['gate_status']=='PASS'
        assert evaluated==[1]
    else:
        with pytest.raises(build.OdsError): build.verify_build_freshness_from_cursor(Cursor(),dataset_id='W8_SIM',batch_date='2024-02-29',env='test',report=report)
        if change=='cleanrun': assert identity_outputs==[]


@pytest.fixture
def mysql_w8(monkeypatch):
    import os,json
    from etl.config import source_config as sources
    from etl.jobs import clean_dwd as clean
    import test_s3_001_dimensions as a
    if not os.environ.get('ODS_TEST_DATABASE_URL'):
        pytest.skip('coordinator test URL absent; W8 real NOTRUN')
    assert sources.database_options(test_only=True)['database']=='hospital_test'
    clean._privacy()
    generator=a.mysql_dims.__wrapped__(monkeypatch)
    dataset,batch,other,_=next(generator)
    from etl.jobs import quality_check as q
    path=q._LOG_ROOT/'test'/build._report_name(batch)
    assert not path.exists()
    n8_owned=False; sentinel_owned=False
    try:
        build.initialize_schema(); assert build.verify_schema()
        # Uncertified fixture sentinel only; never supplies the selected dataset's N7 gate.
        connection=sources.connect_database(test_only=True)
        try:
            with connection.cursor() as cursor:
                for table in (build.VISIT,build.AUDIT):
                    cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s',(dataset,)); assert cursor.fetchone()[0]==0
                n8_owned=True
                for table in (build.VISIT,'dwd_charge_detail'):
                    cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s',(other,)); assert cursor.fetchone()[0]==0
                sentinel_owned=True
                columns=clean.TABLES['dwd_outpatient_visit_detail']['columns']
                cursor.execute('SELECT '+','.join('`'+c+'`' for c in columns)+' FROM dwd_outpatient_visit_detail WHERE dataset_id=%s ORDER BY visit_id LIMIT 1',(dataset,))
                op=dict(zip(columns,cursor.fetchone())); sentinel=build.visit_candidates([op],[])[0]; sentinel['dataset_id']=other; sentinel['patient_key']=None
                cursor.execute('INSERT INTO dwd_visit_detail ('+','.join('`'+c+'`' for c in build.VISIT_COLUMNS)+') VALUES ('+','.join(['%s']*21)+')',tuple(sentinel[c] for c in build.VISIT_COLUMNS))
                columns=clean.TABLES['dwd_charge_detail']['columns']
                cursor.execute('SELECT '+','.join('`'+c+'`' for c in columns)+' FROM dwd_charge_detail WHERE dataset_id=%s ORDER BY charge_id LIMIT 1',(dataset,))
                sentinel=dict(zip(columns,cursor.fetchone())); sentinel['dataset_id']=other; sentinel['patient_key']=None
                cursor.execute('INSERT INTO dwd_charge_detail ('+','.join('`'+c+'`' for c in columns)+') VALUES ('+','.join(['%s']*13)+')',tuple(sentinel[c] for c in columns))
            connection.commit()
        finally: connection.close()
        yield dataset,batch,other,path
    finally:
        try:
            if n8_owned or sentinel_owned:
                connection=sources.connect_database(test_only=True)
                try:
                    with connection.cursor() as cursor:
                        if n8_owned:
                            for table in (build.VISIT,build.AUDIT): cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id=%s',(dataset,))
                        if sentinel_owned:
                            for table in (build.VISIT,'dwd_charge_detail'): cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id=%s',(other,))
                    connection.commit()
                finally: connection.close()
            if path.exists() and not path.is_symlink():
                report=json.loads(path.read_text())
                if report.get('dataset_id')==dataset: path.unlink()
                else: pytest.fail('foreign report preserved; fixture will not delete')
        finally: generator.close()


def mysql_rows(dataset):
    from etl.config import source_config as sources
    from etl.jobs import clean_dwd as clean, build_dims as dims
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            result={}
            for table,cols in [(build.VISIT,build.VISIT_COLUMNS),(build.AUDIT,build.AUDIT_COLUMNS),*[(t,s['columns']) for t,s in clean.TABLES.items()],(clean.AUDIT,clean.AUDIT_COLUMNS),*[(t,[c for c,_,_ in dims._columns(t)]) for t in dims._SPECS]]:
                cursor.execute('SELECT '+','.join('`'+c+'`' for c in cols)+f' FROM `{table}` WHERE dataset_id=%s ORDER BY '+','.join(str(i) for i in range(1,len(cols)+1) if cols[i-1]!='stats_json'),(dataset,))
                result[table]=cursor.fetchall()
            return result
    finally: connection.rollback(); connection.close()


def test_mysql_all_full13_second_connection_and_partial(mysql_w8):
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    before=mysql_rows(dataset); sentinel=mysql_rows(other)
    result=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    after=mysql_rows(dataset)
    assert result['gate_status']=='PASS' and result['rows']=={build.VISIT:6,'dwd_charge_detail':3}
    assert after['dwd_charge_detail']==before['dwd_charge_detail']
    for table in (*clean.TABLES,clean.AUDIT): assert after[table]==before[table]
    assert mysql_rows(other)==sentinel
    assert build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')['build_run_id']==result['build_run_id']
    partial=clean.build_dwd_charge(None,None,dataset_id=dataset,batch_date=batch,env='test')
    assert partial['stats']['visit_rows']==6 and partial['rows']=={'dwd_charge_detail':3}
    assert partial['hashes']['visit'] is None and partial['gate_status']=='NOT_CERTIFIED'
    with pytest.raises(build.OdsError): build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')


@pytest.mark.parametrize('change',['finance_inside','dim','visit','visit_time','charge_time','charge_amount','cleanrun','partial_range'])
def test_mysql_fresh_drift_or_range_no_false_pass(mysql_w8,change):
    from etl.config import source_config as sources
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    before=mysql_rows(dataset); sentinel=mysql_rows(other); report_bytes=path.read_bytes()
    if change=='partial_range':
        with pytest.raises(build.OdsError,match='^N8_RANGE_NOT_FULL$'): build.run_build_all('2025-01-02','2025-01-03',dataset_id=dataset,batch_date=batch,env='test')
        assert mysql_rows(dataset)==before and path.read_bytes()==report_bytes
        return
    if change=='cleanrun': mysql_rebuild_n7(dataset,batch)
    else:
        sql={
            'finance_inside':"UPDATE ods_finance_excel SET stat_date='2025-01-01' WHERE dataset_id=%s AND source_row_number=2",
            'dim':"UPDATE dim_department SET dept_name='SIM_DRIFT' WHERE dataset_id=%s AND dept_code='SIM_DEPT_01'",
            'visit':"UPDATE dwd_visit_detail SET age=31 WHERE dataset_id=%s",
            'visit_time':"UPDATE dwd_visit_detail SET etl_load_time=TIMESTAMPADD(MICROSECOND,1,etl_load_time) WHERE dataset_id=%s",
            'charge_time':"UPDATE dwd_charge_detail SET etl_load_time=TIMESTAMPADD(MICROSECOND,1,etl_load_time) WHERE dataset_id=%s",
            'charge_amount':"UPDATE dwd_charge_detail SET amount=11.25 WHERE dataset_id=%s"}[change]
        connection=sources.connect_database(test_only=True)
        try:
            with connection.cursor() as cursor: cursor.execute(sql,(dataset,))
            connection.commit()
        finally: connection.close()
    with pytest.raises(build.OdsError): build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')
    assert mysql_rows(other)==sentinel and path.read_bytes()==report_bytes


@pytest.mark.parametrize('stage',['visit','charge','audit','dimension','recheck'])
def test_mysql_injected_write_failure_real_rollback(mysql_w8,monkeypatch,stage):
    from etl.jobs import build_dims as dims, clean_dwd as clean
    from etl.config import source_config as sources
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor: cursor.execute("UPDATE ods_finance_excel SET stat_date='2023-01-01' WHERE dataset_id=%s AND source_row_number=1",(dataset,))
        connection.commit()
    finally: connection.close()
    before=mysql_rows(dataset); sentinel=mysql_rows(other); old=path.read_bytes(); reached=[]
    if stage=='dimension':
        original=dims._replace_dimension_rows
        def fail(*args): original(*args); reached.append(stage); raise RuntimeError('SIM_WRITE_FAULT')
        monkeypatch.setattr(dims,'_replace_dimension_rows',fail)
    elif stage=='recheck':
        original=clean._registry
        def fail(*args,**kwargs): reached.append(stage); raise RuntimeError('SIM_RECHECK_FAULT')
        # load_quality_snapshot also calls _registry; fail only after audit INSERT.
        original_replace=build._replace
        def replace(*args):
            original_replace(*args)
            if args[1]==build.AUDIT: monkeypatch.setattr(clean,'_registry',fail)
        monkeypatch.setattr(build,'_replace',replace)
    else:
        original=build._replace; target={'visit':build.VISIT,'charge':'dwd_charge_detail','audit':build.AUDIT}[stage]
        def fail(*args):
            original(*args)
            if args[1]==target: reached.append(stage); raise RuntimeError('SIM_WRITE_FAULT')
        monkeypatch.setattr(build,'_replace',fail)
    with pytest.raises(build.OdsError): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    assert reached==[stage] and mysql_rows(dataset)==before and mysql_rows(other)==sentinel and path.read_bytes()==old


@pytest.mark.parametrize('committed',[True,False])
def test_mysql_commit_unknown_actual_outcome_no_retry(mysql_w8,monkeypatch,committed):
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    before=mysql_rows(dataset); original=clean._connect; commits=[]
    class Connection:
        def __init__(self,connection): self.connection=connection
        def __getattr__(self,name): return getattr(self.connection,name)
        def commit(self):
            commits.append(1)
            if committed: self.connection.commit()
            raise OSError('SIM_COMMIT_RESPONSE_LOST')
    monkeypatch.setattr(clean,'_connect',lambda *a,**k:Connection(original(*a,**k)))
    with pytest.raises(build.OdsError,match='^N8_COMMIT_UNKNOWN$'): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    after=mysql_rows(dataset)
    assert commits==[1] and not path.exists()
    if committed: assert len(after[build.AUDIT])==1 and len(after[build.VISIT])==6
    else: assert after==before


@pytest.mark.parametrize('target',['source','empty_visit_gap','registry','charge','dimension_gap','audit_gap'])
def test_mysql_lock_wait_and_empty_gap_bounded(mysql_w8,target):
    from threading import Thread,Event
    from etl.config import source_config as sources
    import pymysql
    dataset,batch,other,path=mysql_w8
    holder=sources.connect_database(test_only=True); started=Event(); done=Event(); outcomes=[]
    def writer():
        conn=None
        try:
            conn=sources.connect_database(test_only=True)
            with conn.cursor() as cursor:
                cursor.execute('SET SESSION innodb_lock_wait_timeout=2')
                started.set()
                if target=='source': cursor.execute("UPDATE ods_finance_excel SET stat_date='2025-01-02' WHERE dataset_id=%s AND source_row_number=1",(dataset,))
                elif target=='registry': cursor.execute('UPDATE source_config SET etl_batch_date=etl_batch_date WHERE dataset_id=%s',(dataset,))
                elif target=='charge': cursor.execute('UPDATE dwd_charge_detail SET amount=11.25 WHERE dataset_id=%s',(dataset,))
                elif target=='dimension_gap': cursor.execute('INSERT INTO dim_department (dataset_id,dept_code,dept_name,attribute_status,mapping_version,scope) VALUES (%s,%s,%s,%s,%s,%s)',(dataset,'SIM_DEPT_01','SIM_SENTINEL','UNKNOWN','node07-sim-mapping-v1','SIM'))
                elif target=='audit_gap':
                    values=(dataset,'11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222',batch,None,None,'2025-01-01','2025-01-04','node08-model-v1','node07-sim-mapping-v1','SIM','ALL','0'*64,'0'*64,'0'*64,'0'*64,'{}',datetime(2025,1,1))
                    cursor.execute('INSERT INTO dwd_build_audit ('+','.join(build.AUDIT_COLUMNS)+') VALUES ('+','.join(['%s']*18)+')',values)
                else:
                    cursor.execute('INSERT INTO dwd_visit_detail (dataset_id,visit_type,visit_id,source_row_number,source_batch_date,etl_batch_date,etl_load_time,visit_date,dept_code,gender) VALUES (%s,%s,%s,1,%s,%s,NOW(6),%s,%s,%s)',(dataset,'OUTPATIENT','SIM_GAP',batch,batch,'2025-01-01','SIM_DEPT_01','M'))
            outcomes.append('unexpected_write')
        except pymysql.err.OperationalError as error: outcomes.append(error.args[0])
        except Exception: outcomes.append('unexpected_error')
        finally:
            if conn is not None: conn.rollback(); conn.close()
            done.set()
    thread=None
    try:
        with holder.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'); cursor.execute('START TRANSACTION')
            build._lock_inputs(cursor,dataset)
            thread=Thread(target=writer); thread.start()
            assert started.wait(5) and done.wait(8)
            # Consistent read begins only after all current-read locks; blocked writer cannot mix generations.
            cursor.execute('SELECT stat_date FROM ods_finance_excel WHERE dataset_id=%s AND source_row_number=1',(dataset,))
            assert cursor.fetchone()[0]=='2025-01-01'
        assert outcomes==[1205]
    finally:
        holder.rollback(); holder.close()
        if thread is not None: thread.join(timeout=10); assert not thread.is_alive()


@pytest.mark.parametrize('api',[build.initialize_schema,build.verify_schema])
def test_schema_driver_error_sanitized(monkeypatch,api):
    from etl.jobs import clean_dwd as clean
    def fail(): raise RuntimeError('SIM_DSN_SECRET')
    monkeypatch.setattr(clean,'_connect',fail)
    with pytest.raises(build.OdsError,match='^N8_SCHEMA_INVALID$'): api()


def test_env_target_and_busy_lock_write_before_db_refusal(monkeypatch):
    from etl.config import source_config as sources
    from etl.scheduler import scheduler
    monkeypatch.setattr(sources,'database_options',lambda:{'database':'hospital'})
    with pytest.raises(build.OdsError,match='^N8_ARGUMENTS_INVALID$'): build.run_build_all(env='test')
    monkeypatch.setattr(sources,'database_options',lambda:{'database':'hospital_test'})
    from contextlib import contextmanager
    @contextmanager
    def busy(*a): raise scheduler.EtlError('ETL_RUN_LOCKED'); yield
    monkeypatch.setattr(scheduler,'_lock',busy)
    with pytest.raises(build.OdsError,match='^N8_RUN_CONTEXT_ACTIVE$'): build.run_build_all(env='test')


def test_publish_identity_race_preserves_other_report(tmp_path,monkeypatch):
    from etl.jobs import build_dims as dims
    import os,json
    root=tmp_path/'logs'; monkeypatch.setattr(dims.quality,'_LOG_ROOT',root)
    r=report_fixture(); build._publish_report(r)
    path=root/'test'/build._report_name(r['batch_date'])
    original=build._read_report_at; count=[]
    def race(directory,name):
        count.append(1)
        if len(count)==2:
            other=dict(r,dataset_id='OTHER_SIM')
            path.write_text(json.dumps(other)); path.chmod(0o600)
        return original(directory,name)
    monkeypatch.setattr(build,'_read_report_at',race)
    with pytest.raises(build.OdsError,match='^N8_REPORT_IDENTITY_CONFLICT$'): build._publish_report(r)
    assert json.loads(path.read_bytes())['dataset_id']=='OTHER_SIM'


def test_explicit_amount_null_and_full13_conservation():
    from decimal import localcontext
    rows=[dict(charge_id='SIM',amount=Decimal('9999999999999999.99'),etl_load_time=datetime(2024,1,1,0,0,0,123456)),dict(charge_id='SIM2',amount=None,etl_load_time=datetime(2024,1,1))]
    with localcontext() as ctx:
        ctx.prec=5; ctx.Emax=2
        build._conserve(('charge_id','amount','etl_load_time'),rows,[dict(r) for r in rows],('charge_id',))
    changed=[dict(r) for r in rows]; changed[1]['amount']=Decimal('0.00')
    with pytest.raises(build.OdsError,match='^N8_RECONCILE_FAILED$'):
        build._conserve(('charge_id','amount','etl_load_time'),changed,rows,('charge_id',))


def mysql_execute(sql,params):
    from etl.config import source_config as sources
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor: cursor.execute(sql,params)
        connection.commit()
    finally: connection.close()


def mysql_rebuild_n7(dataset,batch):
    from etl.jobs import clean_dwd as clean,quality_check as q
    clean.run_clean_all(batch,dataset_id=dataset)
    report=q.generate_quality_report(batch,dataset_id=dataset,env='test')
    assert report['gate_status']=='PASS' and report['executed_rules']==34
    return report


def test_mysql_two_expansions_shrinks_old_audit_and_crossbatch(mysql_w8):
    from etl.jobs import build_dims as dims,quality_check as q
    from datetime import date,timedelta
    import json
    dataset,batch,other,path=mysql_w8
    a=dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    a_path=path.parent/('dimension_report_'+batch.replace('-','')+'.json'); a_bytes=a_path.read_bytes()
    first=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test'); runs={first['build_run_id']}
    sentinel=mysql_rows(other)
    for day in ('2023-01-01','2024-01-01','2022-01-01','2025-01-01'):
        mysql_execute('UPDATE ods_finance_excel SET stat_date=%s WHERE dataset_id=%s AND source_row_number=1',(day,dataset))
        output=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
        assert output['build_run_id'] not in runs; runs.add(output['build_run_id'])
        assert output['effective_start']==day
        assert build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')['build_run_id']==output['build_run_id']
        assert mysql_rows(other)==sentinel and a_path.read_bytes()==a_bytes
    newbatch=(date.fromisoformat(batch)+timedelta(days=1)).isoformat()
    newquality=q._LOG_ROOT/'test'/('quality_report_'+newbatch.replace('-','')+'.json')
    newbuild=path.parent/build._report_name(newbatch)
    assert not newquality.exists() and not newbuild.exists()
    try:
        mysql_execute('UPDATE source_config SET etl_batch_date=%s WHERE dataset_id=%s',(newbatch,dataset))
        mysql_rebuild_n7(dataset,newbatch)
        output=build.run_build_all(dataset_id=dataset,batch_date=newbatch,env='test')
        assert output['clean_run_id']!=first['clean_run_id'] and output['build_run_id'] not in runs
        assert build.verify_current_build(dataset_id=dataset,batch_date=newbatch,env='test')['gate_status']=='PASS'
        assert a_path.read_bytes()==a_bytes and mysql_rows(other)==sentinel
    finally:
        mysql_execute('UPDATE source_config SET etl_batch_date=%s WHERE dataset_id=%s',(batch,dataset))
        for p in (newquality,newbuild):
            if p.exists(): assert json.loads(p.read_text())['dataset_id']==dataset; p.unlink()


@pytest.mark.parametrize('mutation',['scope','stats','uuid','date','hash','stats_bool','privacy','mapping'])
def test_mysql_bad_audit_with_valid_a_no_fallback(mysql_w8,monkeypatch,mutation):
    from etl.jobs import build_dims as dims
    import json
    dataset,batch,other,path=mysql_w8
    dims.refresh_dimensions(dataset_id=dataset,batch_date=batch,env='test')
    a_path=path.parent/('dimension_report_'+batch.replace('-','')+'.json'); old_a=a_path.read_bytes()
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    sql={'scope':"UPDATE dwd_build_audit SET build_scope='BAD' WHERE dataset_id=%s",'stats':"UPDATE dwd_build_audit SET stats_json='{}' WHERE dataset_id=%s",'uuid':"UPDATE dwd_build_audit SET build_run_id='BAD' WHERE dataset_id=%s",'date':"UPDATE dwd_build_audit SET effective_start='2026-01-01' WHERE dataset_id=%s",'hash':"UPDATE dwd_build_audit SET dim_sha256=REPEAT('0',64) WHERE dataset_id=%s",'stats_bool':"UPDATE dwd_build_audit SET stats_json=JSON_SET(stats_json,'$.visit_rows',CAST('true' AS JSON)) WHERE dataset_id=%s",'privacy':"UPDATE dwd_build_audit SET privacy_key_id=CONCAT('BAD',CHAR(10),'KEY') WHERE dataset_id=%s",'mapping':"UPDATE dwd_build_audit SET mapping_version='BAD' WHERE dataset_id=%s"}[mutation]
    mysql_execute(sql,(dataset,)); before=mysql_rows(dataset); sentinel=mysql_rows(other); old=path.read_bytes()
    calls=[]; original=dims._read_report
    def read(*a,**k):
        if k.get('dimension'): calls.append(1)
        return original(*a,**k)
    monkeypatch.setattr(dims,'_read_report',read)
    with pytest.raises(build.OdsError,match='^N8_DIMENSION_INVALID$'): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    assert calls==[] and mysql_rows(dataset)==before and mysql_rows(other)==sentinel
    assert path.read_bytes()==old and a_path.read_bytes()==old_a


@pytest.mark.parametrize('fault',['writer','fsync','replace','identity_race','dir_fsync'])
def test_mysql_publish_failure_commit_visible_old_file_preserved(mysql_w8,monkeypatch,fault):
    import os,json
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    first=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test'); before=mysql_rows(dataset); sentinel=mysql_rows(other); old=path.read_bytes()
    reached=[]; original=build._publish_report
    def publish(report):
        reached.append(report['build_run_id'])
        if fault=='writer': raise build.OdsError('N8_REPORT_WRITE_FAILED')
        if fault=='identity_race':
            competing=dict(json.loads(old),dataset_id='OTHER_SIM')
            path.write_text(json.dumps(competing)); path.chmod(0o600)
            return original(report)
        with monkeypatch.context() as patch:
            def fail(*a,**k): raise OSError('SIM_PUBLISH_FAULT')
            if fault=='dir_fsync':
                import stat
                sync=os.fsync
                def fail_directory(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('SIM_DIRECTORY_SYNC_FAULT')
                    return sync(fd)
                patch.setattr(os,'fsync',fail_directory)
            else: patch.setattr(os,fault,fail)
            return original(report)
    monkeypatch.setattr(build,'_publish_report',publish)
    expected='N8_REPORT_IDENTITY_CONFLICT' if fault=='identity_race' else 'N8_REPORT_WRITE_FAILED'
    try:
        with pytest.raises(build.OdsError,match='^'+expected+'$'): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
        after=mysql_rows(dataset)
        assert len(reached)==1 and reached[0]!=first['build_run_id']
        assert after[build.AUDIT][0][1]==reached[0] and after[build.AUDIT]!=before[build.AUDIT]
        for table in (*clean.TABLES,clean.AUDIT): assert after[table]==before[table]
        assert mysql_rows(other)==sentinel
        if fault!='identity_race': assert path.read_bytes()==old
        else: assert json.loads(path.read_text())['dataset_id']=='OTHER_SIM'
        with pytest.raises(build.OdsError): build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')
    finally:
        # Owned fixture file only; restore original ownership before fixture cleanup.
        path.write_bytes(old); path.chmod(0o600)


def test_mysql_same_batch_other_dataset_conflict_zero_dml(mysql_w8,monkeypatch):
    import json
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    old=path.read_bytes(); competing=dict(json.loads(old),dataset_id=other)
    path.write_text(json.dumps(competing)); path.chmod(0o600); foreign=path.read_bytes()
    before=mysql_rows(dataset); sentinel=mysql_rows(other); reached=[]
    from etl.jobs import build_dims as dims
    original=dims.prepare_dimensions
    def write(*a,**k): reached.append(1); return original(*a,**k)
    monkeypatch.setattr(dims,'prepare_dimensions',write)
    try:
        with pytest.raises(build.OdsError,match='^N8_REPORT_IDENTITY_CONFLICT$'): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
        assert not reached and path.read_bytes()==foreign and mysql_rows(dataset)==before and mysql_rows(other)==sentinel
    finally: path.write_bytes(old); path.chmod(0o600)


def test_mysql_exact_third_dim_failure_rolls_back_everything(mysql_w8,monkeypatch):
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    mysql_execute("UPDATE ods_finance_excel SET stat_date='2023-01-01' WHERE dataset_id=%s AND source_row_number=1",(dataset,))
    before=mysql_rows(dataset); sentinel=mysql_rows(other); old=path.read_bytes(); inserted=[]; original=clean._connect
    class Cursor:
        def __init__(self,cursor): self.cursor=cursor
        def __enter__(self): self.cursor.__enter__(); return self
        def __exit__(self,*a): return self.cursor.__exit__(*a)
        def __getattr__(self,name): return getattr(self.cursor,name)
        def executemany(self,sql,params):
            if sql.startswith('INSERT INTO `dim_'):
                inserted.append(sql.split('`')[1])
                if inserted[-1]=='dim_disease': raise RuntimeError('SIM_THIRD_DIM_FAULT')
            return self.cursor.executemany(sql,params)
    class Connection:
        def __init__(self,connection): self.connection=connection
        def __getattr__(self,name): return getattr(self.connection,name)
        def cursor(self): return Cursor(self.connection.cursor())
    monkeypatch.setattr(clean,'_connect',lambda *a,**k:Connection(original(*a,**k)))
    with pytest.raises(build.OdsError): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    assert inserted==['dim_department','dim_date','dim_disease']
    assert mysql_rows(dataset)==before and mysql_rows(other)==sentinel and path.read_bytes()==old


def mysql_rewrite_owned_sources(dataset,transform,*,expect_pass=True):
    """Only randomized fixture CSVs; recapture through existing formal N7 CLI."""
    from etl.config import source_config as sources
    import csv,os,subprocess,sys
    from pathlib import Path
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT name,kind,file_path,dataset_id FROM source_config ORDER BY name')
            registrations=cursor.fetchall()
        connection.rollback()
    finally: connection.close()
    assert len(registrations)==6
    for name,kind,path,selected in registrations:
        assert selected==dataset and kind=='csv'
        target=Path(path)
        assert target.resolve().is_relative_to((sources.GENERATED_ROOT/dataset).resolve()) and not target.is_symlink()
        with target.open(encoding='utf-8-sig',newline='') as stream: rows=list(csv.DictReader(stream))
        rows=transform(name,rows)
        with target.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=sources.SOURCES[name]['columns']); writer.writeheader(); writer.writerows(rows)
    env=os.environ.copy(); env['PYTHONPATH']=str(sources.PROJECT_ROOT/'backend'); env['PYTHONDONTWRITEBYTECODE']='1'
    result=subprocess.run([sys.executable,'-m','etl.jobs.run_all','--env','test','--through','quality'],cwd=sources.PROJECT_ROOT/'backend',env=env,capture_output=True,timeout=90)
    assert (result.returncode==0)==expect_pass,'owned N7 formal pipeline unexpected gate; private output withheld'


def test_mysql_rich_null_decimal_microseconds_full13(mysql_w8):
    from etl.jobs import clean_dwd as clean
    from etl.config import source_config as sources
    import json
    dataset,batch,other,path=mysql_w8
    def transform(name,rows):
        if name=='his_outpatient': rows[0]['fee_amount']=''
        if name=='his_inpatient':
            for r in rows: r['stay_days']='1.25'
            rows[0]['drug_fee']=''; rows[1]['consumable_fee']=''; rows[2]['total_fee']=''
        if name=='his_charge': rows[0]['amount']=''
        return rows
    mysql_rewrite_owned_sources(dataset,transform)
    # This is a permitted metadata-only controlled base update, outside N7 digest.
    mysql_execute("UPDATE dwd_charge_detail SET etl_load_time='2024-02-29 12:34:56.123456' WHERE dataset_id=%s",(dataset,))
    before=mysql_rows(dataset); sentinel=mysql_rows(other)
    result=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test'); after=mysql_rows(dataset)
    assert after['dwd_charge_detail']==before['dwd_charge_detail']
    assert all(row[4].microsecond==123456 for row in after['dwd_charge_detail'])
    cols={c:i for i,c in enumerate(build.VISIT_COLUMNS)}
    ips=[r for r in after[build.VISIT] if r[cols['visit_type']]=='INPATIENT']
    assert all(r[cols['source_stay_days']]==Decimal('1.25') for r in ips)
    assert all(r[cols['residual_fee']] is None for r in ips)
    assert result['stats']['missing_total_rows']==2 and result['stats']['missing_component_rows']==2
    assert sum(r[12] is None for r in after['dwd_charge_detail'])==1
    from decimal import Context,localcontext
    with localcontext(Context(prec=64)):
        assert sum((r[12] for r in after['dwd_charge_detail'] if r[12] is not None),Decimal('0.00'))==Decimal('20.50')
    for t in (*clean.TABLES,clean.AUDIT): assert before[t]==after[t]
    assert mysql_rows(other)==sentinel
    assert build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')['gate_status']=='PASS'


@pytest.mark.parametrize('count',[5,2])
def test_mysql_candidate_stats_and_source_shrink(mysql_w8,count):
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    initial=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    before_wide=mysql_rows(dataset)[build.VISIT]; sentinel=mysql_rows(other)
    def transform(name,rows):
        if name=='his_outpatient': return rows[:2 if count==5 else 1]
        if name=='his_inpatient': return rows[:3 if count==5 else 1]
        if name=='his_charge':
            for r in rows: r['visit_id']='SIM_visit_id_1'; r['patient_id']='SIM_PATIENT_1'
        return rows
    mysql_rewrite_owned_sources(dataset,transform)
    # CHARGE uses current candidates, even though old widevisit is stale six-row output.
    output=clean.build_dwd_charge(None,None,dataset_id=dataset,batch_date=batch,env='test')
    assert output['stats']['visit_rows']==count and output['stats']['charge_rows']==3
    assert output['rows']=={'dwd_charge_detail':3} and output['hashes']['visit'] is None and output['gate_status']=='NOT_CERTIFIED'
    assert mysql_rows(dataset)[build.VISIT]==before_wide and mysql_rows(other)==sentinel
    with pytest.raises(build.OdsError): build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')
    final=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    assert final['rows']=={build.VISIT:count,'dwd_charge_detail':3}
    assert len(mysql_rows(dataset)[build.VISIT])==count
    assert build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')['build_run_id']==final['build_run_id']


def owned_visit_csvs(dataset):
    """The two randomized fixture visit CSVs; ownership re-checked before any write."""
    from etl.config import source_config as sources
    from pathlib import Path
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT name,file_path FROM source_config WHERE dataset_id=%s AND name IN (%s,%s) ORDER BY name',(dataset,'his_inpatient','his_outpatient'))
            files=cursor.fetchall()
    finally: connection.rollback(); connection.close()
    assert [name for name,_ in files]==['his_inpatient','his_outpatient']
    result=[]
    for name,target in files:
        target=Path(target)
        assert target.resolve().is_relative_to((sources.GENERATED_ROOT/dataset).resolve()) and not target.is_symlink()
        result.append((name,target))
    return result


def formal_n7_cli():
    """Frozen formal extract->clean->quality CLI; returns (returncode,fixed safe code)."""
    from etl.config import source_config as sources
    import json,os,subprocess,sys
    env=os.environ.copy(); env['PYTHONPATH']=str(sources.PROJECT_ROOT/'backend'); env['PYTHONDONTWRITEBYTECODE']='1'
    completed=subprocess.run([sys.executable,'-m','etl.jobs.run_all','--env','test','--through','quality'],cwd=sources.PROJECT_ROOT/'backend',env=env,capture_output=True,timeout=90)
    lines=[line for line in completed.stdout.decode('utf-8','replace').splitlines() if line.strip()]
    return completed.returncode,json.loads(lines[-1])['code']


def mysql_ods_counts(dataset):
    from etl.config import source_config as sources
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            counts={}
            for name in sorted(sources.SOURCES):
                cursor.execute(f"SELECT COUNT(*) FROM `{sources.SOURCES[name]['table']}` WHERE dataset_id=%s",(dataset,))
                counts[name]=cursor.fetchone()[0]
            return counts
    finally: connection.rollback(); connection.close()


def test_mysql_header_only_sources_rejected_and_old_build_preserved(mysql_w8):
    """A: an emptied external CSV is a collector refusal, not a controlled zero-visit state.

    The frozen empty-source guard must refuse before any ODS/DWD write with the fixed code
    ODS_SNAPSHOT_EMPTY, and the already certified DB snapshot must stay valid and unchanged.
    """
    from etl.config import source_config as sources
    from etl.jobs import quality_check as q
    import csv
    dataset,batch,other,path=mysql_w8
    first=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    quality_path=q._LOG_ROOT/'test'/('quality_report_'+batch.replace('-','')+'.json')
    before=mysql_rows(dataset); sentinel=mysql_rows(other)
    old=path.read_bytes(); old_quality=quality_path.read_bytes(); ods=mysql_ods_counts(dataset)
    files=owned_visit_csvs(dataset); original={}
    try:
        for name,target in files:
            original[name]=target.read_bytes()
            with target.open('w',encoding='utf-8-sig',newline='') as stream:
                csv.DictWriter(stream,fieldnames=sources.SOURCES[name]['columns']).writeheader()
        returncode,code=formal_n7_cli()
        assert (returncode,code)==(1,'ODS_SNAPSHOT_EMPTY'),'header-only source must hit the fixed empty guard'
        assert mysql_ods_counts(dataset)==ods,'refused extraction must not rewrite ODS'
        assert mysql_rows(dataset)==before and mysql_rows(other)==sentinel
        assert path.read_bytes()==old and quality_path.read_bytes()==old_quality
        assert build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')['build_run_id']==first['build_run_id']
    finally:
        for name,target in files: target.write_bytes(original[name])


def test_mysql_controlled_zero_visits_three_charges_upstream_blocks_zero_n8_dml(mysql_w8):
    """B: build a real N7-controlled 0-visit/3-charge state instead of assuming one.

    Blank visit ids keep every row and column non-empty and valid, and the existing dedupe
    isolates them, so source_rows>0 with accepted_rows==0 and no N7 rule, denominator or PASS
    is touched. The formal CLI must complete extract+clean, fail at quality (DQ_GATE_FAILED),
    and then N8 CHARGE/ALL must be refused by the recomputed upstream gate with zero N8 DML.
    """
    from etl.config import source_config as sources
    from etl.jobs import clean_dwd as clean,quality_check as q
    import csv,json
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    quality_path=q._LOG_ROOT/'test'/('quality_report_'+batch.replace('-','')+'.json')
    baseline=mysql_rows(dataset); old=path.read_bytes(); sentinel=mysql_rows(other)
    run_index=clean.AUDIT_COLUMNS.index('clean_run_id')
    assert len(baseline[clean.AUDIT])==5 and len(baseline['dwd_charge_detail'])==3
    old_run={row[run_index] for row in baseline[clean.AUDIT]}; assert len(old_run)==1
    files=owned_visit_csvs(dataset); original={}
    try:
        for name,target in files:
            original[name]=target.read_bytes()
            with target.open(encoding='utf-8-sig',newline='') as stream: rows=list(csv.DictReader(stream))
            assert rows and all(row['visit_id'] for row in rows)
            for row in rows: row['visit_id']=''
            with target.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=sources.SOURCES[name]['columns']); writer.writeheader(); writer.writerows(rows)
        returncode,code=formal_n7_cli()
        assert (returncode,code)==(1,'DQ_GATE_FAILED'),'controlled zero-visit clean must fail the fixed N7 gate'
        after_n7=mysql_rows(dataset); report=json.loads(quality_path.read_text())
        runs={row[run_index] for row in after_n7[clean.AUDIT]}
        assert len(after_n7[clean.AUDIT])==5 and len(runs)==1 and runs!=old_run,'five audits must share one new clean run'
        for table in ('dwd_outpatient_visit_detail','dwd_inpatient_visit_detail'): assert not after_n7[table],'blank ids must leave no accepted visit row'
        assert len(after_n7['dwd_charge_detail'])==3,'valid charge source rows must stay accepted'
        assert report['gate_status']=='FAIL' and report['clean_run_id']==next(iter(runs))
        # Zero acceptance must carry source-loss evidence, not just an empty DWD table.
        for table in ('dwd_outpatient_visit_detail','dwd_inpatient_visit_detail'):
            item=report['tables'][table]
            assert item['accepted_rows']==0 and item['source_rows']>0
            assert item['quarantined_rows']+item['duplicate_removed']==item['source_rows']
            assert item['stats_json']['quarantined_by_reason']['invalid_id_rows']==item['source_rows']
            assert item['source_usable_ratio']==0 and item['quarantine_ratio']==1
        charge=report['tables']['dwd_charge_detail']
        assert charge['accepted_rows']==3 and charge['quarantined_rows']==0 and charge['source_rows']==3
        # Zero-N8-DML baseline starts after the committed N7 failure, never before it.
        before_n8=mysql_rows(dataset); report_bytes=path.read_bytes()
        assert before_n8[build.AUDIT]==baseline[build.AUDIT] and before_n8[build.VISIT]==baseline[build.VISIT]
        for request in (lambda: clean.build_dwd_charge(None,None,dataset_id=dataset,batch_date=batch,env='test'),
                        lambda: build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')):
            with pytest.raises(build.OdsError,match='^N8_UPSTREAM_GATE_FAILED$'): request()
        assert mysql_rows(dataset)==before_n8 and mysql_rows(other)==sentinel
        assert path.read_bytes()==report_bytes
    finally:
        for name,target in files: target.write_bytes(original[name])


def test_prepare_blank_visit_ids_quarantined_with_conservation():
    """Pure premise of the real 0/3 case: blank ids are isolated by existing dedupe only.

    No N7 rule, denominator or PASS is touched; only the id differs from the control rows.
    """
    from datetime import date
    from etl.jobs import clean_dwd as clean
    dataset='SIM_W8_UNIT'; batch='2024-02-29'; key=b'\x07'*32; key_id='SIM-unit-w8'; run='00000000-0000-4000-8000-000000000000'
    config=clean._config()
    def row(number,visit_id):
        return dict(visit_id=visit_id,patient_id='SIM_PATIENT_1',patient_name='SIM_NAME',id_card='SIM_ID',phone='SIM_PHONE',gender='M',age='30',dept_code='SIM_DEPT_01',visit_date='2024-02-29',visit_type='OUTPATIENT',fee_amount='12.34',dataset_id=dataset,source_row_number=number,etl_batch_date=date(2024,2,29))
    rows,audit=clean._prepare('dwd_outpatient_visit_detail',[row(n,'') for n in (1,2,3)],batch,dataset,key,key_id,run,config)
    assert rows==[] and audit['accepted_rows']==0 and audit['source_rows']==3
    assert audit['quarantined_rows']==3 and audit['duplicate_removed']==0
    assert audit['stats_json']['quarantined_by_reason']['invalid_id_rows']==3
    assert audit['source_rows']==audit['accepted_rows']+audit['duplicate_removed']+audit['quarantined_rows']
    control,control_audit=clean._prepare('dwd_outpatient_visit_detail',[row(n,'SIM_VISIT_%d'%n) for n in (1,2,3)],batch,dataset,key,key_id,run,config)
    assert len(control)==3 and control_audit['accepted_rows']==3 and control_audit['quarantined_rows']==0


def test_two_visit_three_charge_fixed_n7_pass_without_rule_change(monkeypatch):
    from etl.jobs import clean_dwd as clean,quality_check as q
    import test_s3_001_dimensions as a
    from datetime import date
    snap=a.n7_snapshot(); config=clean._config()
    for table in clean.TABLES:
        entry=snap['tables'][table]; raw=entry['source']['rows']
        if table in ('dwd_outpatient_visit_detail','dwd_inpatient_visit_detail'): raw=raw[:1]
        if table=='dwd_charge_detail':
            raw=[dict(r,visit_id='SIM_visit_id_1',patient_id='SIM_PATIENT_1') for r in raw]
        rows,audit=clean._prepare(table,raw,snap['batch_date'],snap['dataset_id'],a.SIM_BYTES,'SIM-unit-v1',snap['clean_run_id'],config)
        entry['source']['rows']=raw; entry['dwd']['rows']=rows; entry['audit']=audit
    monkeypatch.setattr(clean,'_privacy',lambda:(a.SIM_BYTES,'SIM-unit-v1'))
    import base64
    monkeypatch.setenv('DWD_PRIVACY_KEY',base64.b64encode(a.SIM_BYTES).decode())
    monkeypatch.setenv('DWD_PRIVACY_KEY_ID','SIM-unit-v1')
    monkeypatch.setattr(q,'database_options',lambda:{'database':'hospital_test'})
    monkeypatch.setattr(q,'_load_snapshot',lambda *a,**k:snap)
    monkeypatch.setattr(q,'_write_report',lambda report:None)
    report=q.generate_quality_report(snap['batch_date'],dataset_id=snap['dataset_id'],env='test')
    assert report['gate_status']=='PASS'
    assert report['rule_status_counts']=={'PASS':32,'WARN':0,'FAIL':0,'NOT_EVALUATED':2}
    assert report['score_numerator_half_points']==64 and report['score_denominator_half_points']==68
    # Existing34 denominator remains fixed:64/68=94.12 permits the2/3 control.


@pytest.mark.parametrize('mutation',['type','nullability','order','index','engine'])
def test_new_schema_exact_mutations_readonly(mutation):
    from copy import deepcopy
    names=(build.VISIT,build.AUDIT); specs={build.VISIT:build.VISIT_SPEC,build.AUDIT:build.AUDIT_SPEC}
    replies=[[(t,'InnoDB','utf8mb4_bin') for t in names],[(t,c,typ,'YES' if null else 'NO',i,'utf8mb4_bin' if typ.startswith(('char(','varchar(')) else None) for t in names for i,(c,typ,null) in enumerate(specs[t],1)],[(t,n,u,i,c,None,'BTREE') for t in names for n,(u,cols) in build.INDEXES[t].items() for i,c in enumerate(cols,1)]]
    class Cursor:
        def __init__(self,data): self.data=iter(data); self.sql=[]
        def execute(self,sql,params): assert sql.startswith('SELECT '); self.sql.append(sql)
        def fetchall(self): return next(self.data)
    build._verify_new(Cursor(deepcopy(replies)))
    if mutation=='engine': replies[0][0]=(build.VISIT,'MyISAM','utf8mb4_bin')
    elif mutation=='index': replies[2].append((build.VISIT,'bad',1,1,'visit_id',None,'BTREE'))
    else:
        row=list(replies[1][0]); index={'type':2,'nullability':3,'order':4}[mutation]; row[index]={'type':'varchar(64)','nullability':'YES','order':999}[mutation]; replies[1][0]=tuple(row)
    c=Cursor(replies)
    with pytest.raises(build.OdsError,match='^N8_SCHEMA_INVALID$'): build._verify_new(c)
    assert all(sql.startswith('SELECT ') for sql in c.sql)


def test_mysql_schema_catalog_fault_no_ddl_or_business_write(mysql_w8,monkeypatch):
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    before=mysql_rows(dataset); sentinel=mysql_rows(other); old=path.read_bytes(); statements=[]; mutated=[]; original=clean._connect
    class Cursor:
        def __init__(self,cursor): self.cursor=cursor; self.columns=False
        def __enter__(self): self.cursor.__enter__(); return self
        def __exit__(self,*a): return self.cursor.__exit__(*a)
        def __getattr__(self,name): return getattr(self.cursor,name)
        def execute(self,sql,params=None):
            statements.append(sql); self.columns='information_schema.COLUMNS' in sql and params==(build.VISIT,build.AUDIT)
            return self.cursor.execute(sql,params)
        def fetchall(self):
            rows=self.cursor.fetchall()
            if self.columns:
                altered=[]
                for row in rows:
                    if row[0]==build.VISIT and row[1]=='visit_id':
                        row=list(row); row[2]='varchar(64)'; row=tuple(row); mutated.append(1)
                    altered.append(row)
                return altered
            return rows
    class Connection:
        def __init__(self,connection): self.connection=connection
        def __getattr__(self,name): return getattr(self.connection,name)
        def cursor(self): return Cursor(self.connection.cursor())
    monkeypatch.setattr(clean,'_connect',lambda *a,**k:Connection(original(*a,**k)))
    with pytest.raises(build.OdsError,match='^N8_SCHEMA_INVALID$'): build.verify_schema()
    with pytest.raises(build.OdsError,match='^N8_SCHEMA_INVALID$'): build.initialize_schema()
    with pytest.raises(build.OdsError,match='^N8_SCHEMA_INVALID$'): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    assert len(mutated)==3 and not any(sql.startswith(('INSERT','DELETE','UPDATE','ALTER','DROP','CREATE')) for sql in statements)
    assert mysql_rows(dataset)==before and mysql_rows(other)==sentinel and path.read_bytes()==old
    # Actual catalog is healthy; proxy injects unknown shape. Physical shared-table DDL intentionally untouched.


@pytest.mark.parametrize('did_rollback',[True,False])
def test_mysql_unknown_rollback_fixed_error_terminal_after_close(mysql_w8,monkeypatch,did_rollback):
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    before=mysql_rows(dataset); sentinel=mysql_rows(other); old=path.read_bytes(); original=clean._connect; replace=build._replace; reached=[]; closed=[]; rollbacks=[]
    class Connection:
        def __init__(self,connection): self.connection=connection
        def __getattr__(self,name): return getattr(self.connection,name)
        def rollback(self):
            rollbacks.append(1)
            if did_rollback: self.connection.rollback()
            raise OSError('SIM_ROLLBACK_RESPONSE_UNKNOWN')
        def close(self): self.connection.close(); closed.append(1)
    def fault(*a):
        replace(*a)
        if a[1]==build.VISIT: reached.append(1); raise RuntimeError('SIM_WRITE_FAULT')
    monkeypatch.setattr(clean,'_connect',lambda *a,**k:Connection(original(*a,**k)))
    monkeypatch.setattr(build,'_replace',fault)
    with pytest.raises(build.OdsError,match='^N8_ROLLBACK_FAILED$'): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    assert reached==[1] and rollbacks==[1] and closed==[1]
    # Only after owned connection close and a separate connection reads may terminal state be called old.
    assert mysql_rows(dataset)==before and mysql_rows(other)==sentinel and path.read_bytes()==old


def test_report_stats_internal_conservation_rejects_forged_counts():
    report=report_fixture(); report['stats']['base_rows']['dwd_charge_detail']=1
    with pytest.raises(build.OdsError,match='^N8_STALE$'): build._validate_report(report)


@pytest.mark.parametrize('source',['finance_excel','lis_report','emr_record','inpatient_discharge'])
def test_all_six_date_extrema_feed_dimension_scope(source):
    import test_s3_001_dimensions as a
    from etl.jobs import build_dims as dims
    snapshot=a.snapshot(); snapshot['dates'][source]=['2020-01-01','2028-12-31']
    desired=dims.dimension_rows(snapshot)
    assert (desired['date_min'],desired['date_max'])==('2020-01-01','2028-12-31')
    assert build.full_range(None,None,desired['date_min'],desired['date_max'])==('2020-01-01','2028-12-31')


@pytest.mark.parametrize('fault',['nan','symlink','hardlink','missing'])
def test_current_quality_reader_invalid_evidence_no_write(tmp_path,monkeypatch,fault):
    import os
    from etl.jobs import build_dims as dims
    root=tmp_path/'logs'; directory=root/'test'; directory.mkdir(parents=True)
    path=directory/'quality_report_20240229.json'; other=tmp_path/'other'; other.write_text('{}'); other.chmod(0o600)
    if fault=='nan': path.write_text('{"score":NaN}')
    elif fault=='symlink': path.symlink_to(other)
    elif fault=='hardlink': os.link(other,path)
    monkeypatch.setattr(dims.quality,'_LOG_ROOT',root)
    before=set(directory.iterdir())
    with pytest.raises(build.OdsError): build._read_quality_report('test','2024-02-29')
    assert set(directory.iterdir())==before and other.read_text()=='{}'


def test_association_slots_na_null_span_and_unknown_rejected():
    import test_s3_001_dimensions as a
    from etl.jobs import build_dims as dims
    snapshot=a.n7_snapshot()
    visits=build.visit_candidates(snapshot['tables']['dwd_outpatient_visit_detail']['dwd']['rows'],snapshot['tables']['dwd_inpatient_visit_detail']['dwd']['rows'])
    charges=build.charge_candidates(snapshot['tables']['dwd_charge_detail']['dwd']['rows'],visits)
    desired=dims._dimension_range(snapshot['dataset_id'],'2025-01-01','2025-01-04')
    stats=build._stats(snapshot,visits,charges,desired)
    assert stats['association_counts']==dict(department=dict(slots=9,matched=9,not_applicable=0),date=dict(slots=12,matched=12,not_applicable=0),disease=dict(slots=3,matched=3,not_applicable=3),fee_type=dict(slots=3,matched=3,not_applicable=0),visit=dict(slots=3,matched=3,not_applicable=0))
    from copy import deepcopy
    changed=deepcopy(visits); changed[0]['dept_code']='SIM_DEPT_UNKNOWN'
    with pytest.raises(build.OdsError,match='^N8_UNKNOWN_CODE$'): build._stats(snapshot,changed,charges,desired)
    ip=snapshot['tables']['dwd_inpatient_visit_detail']['dwd']['rows'][0]
    row=build.visit_candidates([],[dict(ip,discharge_date=None)])[0]
    assert row['calendar_span_days'] is None and row['source_stay_days']==ip['stay_days']


@pytest.mark.parametrize('kind',['OUTPATIENT','INPATIENT'])
def test_typed_charge_parent_only_matches_own_domain(kind):
    parents=[dict(dataset_id='A',visit_type=t,visit_id='X',patient_key=t) for t in ('OUTPATIENT','INPATIENT')]
    charge=dict(dataset_id='A',visit_type=kind,visit_id='X',patient_key=kind)
    assert build.charge_candidates([charge],parents)==[charge]
    with pytest.raises(build.OdsError,match='^N8_VISIT_REFERENCE_INVALID$'): build.charge_candidates([dict(charge,dataset_id='B')],parents)


def test_rich_null_source_really_passes_frozen_n7(monkeypatch):
    import base64
    import test_s3_001_dimensions as a
    from etl.jobs import clean_dwd as clean,quality_check as q
    snap=a.n7_snapshot(); config=clean._config()
    for table in clean.TABLES:
        entry=snap['tables'][table]; raw=[dict(r) for r in entry['source']['rows']]
        if table=='dwd_outpatient_visit_detail': raw[0]['fee_amount']=''
        if table=='dwd_inpatient_visit_detail':
            for r in raw: r['stay_days']='1.25'
            raw[0]['drug_fee']=''; raw[1]['consumable_fee']=''; raw[2]['total_fee']=''
        if table=='dwd_charge_detail': raw[0]['amount']=''
        rows,audit=clean._prepare(table,raw,snap['batch_date'],snap['dataset_id'],a.SIM_BYTES,'SIM-unit-v1',snap['clean_run_id'],config)
        entry['source']['rows']=raw; entry['dwd']['rows']=rows; entry['audit']=audit
    monkeypatch.setenv('DWD_PRIVACY_KEY',base64.b64encode(a.SIM_BYTES).decode()); monkeypatch.setenv('DWD_PRIVACY_KEY_ID','SIM-unit-v1')
    monkeypatch.setattr(q,'database_options',lambda:{'database':'hospital_test'})
    monkeypatch.setattr(q,'_load_snapshot',lambda *args,**kwargs:snap); monkeypatch.setattr(q,'_write_report',lambda report:None)
    report=q.generate_quality_report(snap['batch_date'],dataset_id=snap['dataset_id'],env='test')
    assert report['gate_status']=='PASS' and report['rule_status_counts']=={'PASS':30,'WARN':4,'FAIL':0,'NOT_EVALUATED':0}
    visits=build.visit_candidates(snap['tables']['dwd_outpatient_visit_detail']['dwd']['rows'],snap['tables']['dwd_inpatient_visit_detail']['dwd']['rows'])
    ips=[r for r in visits if r['visit_type']=='INPATIENT']
    assert all(r['source_stay_days']==Decimal('1.25') and r['residual_fee'] is None for r in ips)


def test_report_privacy_key_identifier_rejects_control_text():
    report=report_fixture(); report['privacy_key_id']='SIM\nDSN'
    with pytest.raises(build.OdsError,match='^N8_STALE$'): build._validate_report(report)


def test_mysql_visit_partial_only_one_commit_charge_unchanged(mysql_w8,monkeypatch):
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    before=mysql_rows(dataset); sentinel=mysql_rows(other); old=path.read_bytes(); original=clean._connect; commits=[]
    class Connection:
        def __init__(self,connection): self.connection=connection
        def __getattr__(self,name): return getattr(self.connection,name)
        def commit(self): commits.append(1); return self.connection.commit()
    with monkeypatch.context() as patch:
        patch.setattr(clean,'_connect',lambda *a,**k:Connection(original(*a,**k)))
        result=clean.build_dwd_visit(None,None,dataset_id=dataset,batch_date=batch,env='test')
    after=mysql_rows(dataset)
    assert commits==[1] and result['rows']=={build.VISIT:6} and result['hashes']['charge'] is None
    assert result['gate_status']=='NOT_CERTIFIED' and result['stats']['charge_rows']==3
    assert after['dwd_charge_detail']==before['dwd_charge_detail'] and after[clean.AUDIT]==before[clean.AUDIT]
    assert path.read_bytes()==old and mysql_rows(other)==sentinel
    with pytest.raises(build.OdsError): build.verify_current_build(dataset_id=dataset,batch_date=batch,env='test')
    assert build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')['gate_status']=='PASS'


def test_publish_directory_fsync_failure_preserves_previous_pass(tmp_path,monkeypatch):
    import os,stat
    from etl.jobs import build_dims as dims
    root=tmp_path/'logs'; monkeypatch.setattr(dims.quality,'_LOG_ROOT',root)
    report=report_fixture(); build._publish_report(report)
    path=root/'test'/build._report_name(report['batch_date']); previous=path.read_bytes()
    changed=dict(report,build_run_id='33333333-3333-4333-8333-333333333333')
    original=os.fsync; reached=[]
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode): reached.append(1); raise OSError('SIM_DIRECTORY_SYNC_FAULT')
        return original(fd)
    monkeypatch.setattr(os,'fsync',fail_directory)
    with pytest.raises(build.OdsError,match='^N8_REPORT_WRITE_FAILED$'): build._publish_report(changed)
    assert reached and path.read_bytes()==previous


@pytest.mark.parametrize('fault',['orphan','patient','unknown_code'])
def test_mysql_source_validity_and_join_failures_zero_n8_writes(mysql_w8,fault):
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    old=path.read_bytes(); sentinel=mysql_rows(other)
    def transform(name,rows):
        if name=='his_charge':
            if fault=='orphan': rows[0]['visit_id']='SIM_ORPHAN'
            elif fault=='patient': rows[0]['patient_id']='SIM_UNRELATED_PATIENT'
            else: rows[0]['dept_code']='SIM_DEPT_UNKNOWN'
        return rows
    mysql_rewrite_owned_sources(dataset,transform,expect_pass=fault!='unknown_code')
    before=mysql_rows(dataset)
    code={'orphan':'N8_VISIT_REFERENCE_INVALID','patient':'N8_PATIENT_MISMATCH','unknown_code':'N8_UPSTREAM_GATE_FAILED'}[fault]
    with pytest.raises(build.OdsError,match='^'+code+'$'): build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    assert mysql_rows(dataset)==before and mysql_rows(other)==sentinel and path.read_bytes()==old


def test_mysql_typed_inpatient_parent_charge_department_may_differ(mysql_w8):
    dataset,batch,other,path=mysql_w8
    def transform(name,rows):
        if name=='his_charge':
            for row in rows: row['visit_type']='INPATIENT'; row['dept_code']='SIM_DEPT_02'
        return rows
    mysql_rewrite_owned_sources(dataset,transform)
    before=mysql_rows(dataset); output=build.run_build_all(dataset_id=dataset,batch_date=batch,env='test'); after=mysql_rows(dataset)
    assert output['rows']=={build.VISIT:6,'dwd_charge_detail':3}
    assert after['dwd_charge_detail']==before['dwd_charge_detail']
    assert all(row[8]=='INPATIENT' and row[9]=='SIM_DEPT_02' for row in after['dwd_charge_detail'])
    assert {(row[1],row[2]) for row in after[build.VISIT]}=={(kind,'SIM_visit_id_'+str(i)) for kind in ('OUTPATIENT','INPATIENT') for i in range(1,4)}


def test_mysql_registry_wait_precedes_first_consistent_read(mysql_w8,monkeypatch):
    from threading import Thread,Event
    from etl.config import source_config as sources
    from etl.jobs import clean_dwd as clean
    dataset,batch,other,path=mysql_w8
    build.run_build_all(dataset_id=dataset,batch_date=batch,env='test')
    blocker=sources.connect_database(test_only=True); waiting=Event(); finished=Event(); outputs=[]; errors=[]; original=clean._connect
    class Cursor:
        def __init__(self,cursor): self.cursor=cursor
        def __enter__(self): self.cursor.__enter__(); return self
        def __exit__(self,*a): return self.cursor.__exit__(*a)
        def __getattr__(self,name): return getattr(self.cursor,name)
        def execute(self,sql,params=None):
            if sql.startswith('SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config') and sql.endswith('FOR UPDATE'): waiting.set()
            return self.cursor.execute(sql,params)
    class Connection:
        def __init__(self,connection): self.connection=connection
        def __getattr__(self,name): return getattr(self.connection,name)
        def cursor(self): return Cursor(self.connection.cursor())
    def connect(*a,**k):
        conn=original(*a,**k)
        with conn.cursor() as cursor: cursor.execute('SET SESSION innodb_lock_wait_timeout=5')
        return Connection(conn)
    monkeypatch.setattr(clean,'_connect',connect)
    def builder():
        try: outputs.append(build.run_build_all(dataset_id=dataset,batch_date=batch,env='test'))
        except build.OdsError as error: errors.append(error.code)
        except Exception: errors.append('UNEXPECTED_SAFE_FAILURE')
        finally: finished.set()
    thread=None
    try:
        with blocker.cursor() as cursor:
            cursor.execute('SELECT name FROM source_config ORDER BY name FOR UPDATE'); cursor.fetchall()
            cursor.execute("UPDATE ods_finance_excel SET stat_date='2023-01-01' WHERE dataset_id=%s AND source_row_number=1",(dataset,))
        thread=Thread(target=builder); thread.start()
        assert waiting.wait(5) and not finished.wait(.2)
        blocker.commit()
        assert finished.wait(15) and not errors and len(outputs)==1
        assert outputs[0]['gate_status']=='PASS' and outputs[0]['effective_start']=='2023-01-01'
    finally:
        blocker.rollback(); blocker.close()
        if thread is not None: thread.join(timeout=10); assert not thread.is_alive()


def test_n8_explicit_initialize_only_missing_known_object(monkeypatch):
    from etl.jobs import clean_dwd as clean,build_dims as dims
    from contextlib import nullcontext
    calls=[]; checks=[]
    class Cursor:
        def execute(self,sql,params=None): calls.append(sql)
        def fetchall(self): return [(build.VISIT,)]
    class Connection:
        def cursor(self): return nullcontext(Cursor())
        def close(self): calls.append('CLOSE')
    monkeypatch.setattr(clean,'_connect',lambda:Connection())
    monkeypatch.setattr(clean,'_verify',lambda cursor:checks.append('old6'))
    monkeypatch.setattr(dims,'_verify',lambda cursor:checks.append('dim4'))
    monkeypatch.setattr(build,'_verify_new',lambda cursor,names=(build.VISIT,build.AUDIT):checks.append(set(names)))
    assert build.initialize_schema()
    assert checks==['old6','dim4',{build.VISIT},'old6','dim4',{build.VISIT,build.AUDIT}]
    creates=[sql for sql in calls if sql.startswith('CREATE')]
    assert creates==[build.new_ddl()[1]] and not any(sql.startswith(('DROP','ALTER')) for sql in calls)


def test_public_fresh_single_lock_and_read_transaction_rollback(monkeypatch,tmp_path):
    from etl.jobs import clean_dwd as clean
    from etl.scheduler import scheduler
    from contextlib import contextmanager,nullcontext
    import os
    calls=[]; active=[]; report=report_fixture()
    class Cursor:
        def execute(self,sql,params=None): calls.append(sql)
    cursor=Cursor()
    class Connection:
        def cursor(self): return nullcontext(cursor)
        def rollback(self): calls.append('ROLLBACK')
        def close(self): calls.append('CLOSE')
    @contextmanager
    def lock(*a):
        assert not active; active.append(1); calls.append('LOCK')
        try: yield
        finally: active.pop(); calls.append('UNLOCK')
    monkeypatch.setattr(build,'_validate_public',lambda *a:None)
    monkeypatch.setattr(scheduler,'_lock',lock); monkeypatch.setattr(clean,'_connect',lambda:Connection())
    monkeypatch.setattr(build,'_lock_inputs',lambda *a:('W8_SIM','2024-02-29',()))
    monkeypatch.setattr(build,'_open_report_directory_readonly',lambda env:os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY))
    monkeypatch.setattr(build,'_read_report_at',lambda *a:report)
    def verify(actual,**kwargs):
        assert actual is cursor and active and kwargs['report'] is report
        return dict(dataset_id='W8_SIM',build_run_id=report['build_run_id'],clean_run_id=report['clean_run_id'],batch_date='2024-02-29',gate_status='PASS',hashes=report['hashes'])
    monkeypatch.setattr(build,'verify_build_freshness_from_cursor',verify)
    assert build.verify_current_build(env='test')['gate_status']=='PASS'
    assert calls==['LOCK','SET TRANSACTION ISOLATION LEVEL REPEATABLE READ','START TRANSACTION','ROLLBACK','CLOSE','UNLOCK'] and not active
