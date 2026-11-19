"""C7 synthetic-only units. Real MySQL cases require coordinator injection."""
import base64
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from etl.config import source_config as sources
from etl.jobs import clean_dwd as c

SIM_KEY = b'SIM_ONLY_' + b'x' * 23
assert len(SIM_KEY) == 32
BATCH = '2025-01-31'
DATASET = 'C7_SIM_UNIT'
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, request):
    # Never inspect or print inherited credentials. Real tests use their separate fixture.
    if request.node.name.startswith('test_mysql_'):
        return
    for name in ('ODS_DATABASE_URL', 'ODS_TEST_DATABASE_URL', 'DWD_PRIVACY_KEY', 'DWD_PRIVACY_KEY_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('DWD_PRIVACY_KEY', base64.b64encode(SIM_KEY).decode())
    monkeypatch.setenv('DWD_PRIVACY_KEY_ID', 'SIM-unit-v1')


def frame():
    return pd.DataFrame([
        dict(dataset_id='SIM_A', visit_id='v1', value='same', source_row_number=9, etl_batch_date=BATCH),
        dict(dataset_id='SIM_A', visit_id='v1', value='same', source_row_number=2, etl_batch_date=BATCH),
        dict(dataset_id='SIM_B', visit_id='v1', value='other', source_row_number=1, etl_batch_date=BATCH),
        dict(dataset_id='SIM_A', visit_id='v2', value='x', source_row_number=3, etl_batch_date=BATCH),
        dict(dataset_id='SIM_A', visit_id='v2', value='y', source_row_number=4, etl_batch_date=BATCH),
        dict(dataset_id='SIM_A', visit_id=' ', value='x', source_row_number=5, etl_batch_date=BATCH),
    ])


def test_dedupe_compares_all_business_before_projection_and_preserves_input():
    original = frame()
    before = original.copy(deep=True)
    result, stats = c.dedupe(original, ['visit_id'])
    assert sorted(result.source_row_number.tolist()) == [1, 2]
    assert stats == {'invalid_id_rows': 1, 'conflicting_id_rows': 2, 'duplicate_removed': 1}
    pd.testing.assert_frame_equal(original, before)


def test_fill_mean_median_unknown_real_operations():
    original = pd.DataFrame({'mean': [1, 3, None], 'median': [1, 9, None], 'text': ['', 'SIM', None]})
    out, stats = c.fill_missing(original, {'mean': 'mean', 'median': 'median', 'text': '未知'})
    assert out['mean'].tolist() == [1, 3, 2]
    assert out['median'].tolist() == [1, 9, 5]
    assert out['text'].tolist() == ['未知', 'SIM', '未知']
    assert stats == {'filled_rows': 2, 'filled_cells': 4}
    assert pd.isna(original['mean'].iloc[2])


@pytest.mark.parametrize('df,strategy', [(pd.DataFrame({'x':[None]}), {'x':'mean'}), (pd.DataFrame({'x':['SIM_SECRET', None]}), {'x':'median'}), (pd.DataFrame({'x':[1]}), {'bad':'未知'}), (pd.DataFrame({'x':[None]}), {'x':'zero'})])
def test_fill_fails_closed(df, strategy):
    with pytest.raises(sources.OdsError, match='^DWD_ARGUMENTS_INVALID$'):
        c.fill_missing(df, strategy)


def test_mapping_unknown_never_counts_as_hit_and_no_trim():
    df = pd.DataFrame({'dept_code':['SIM_科室1_别名','SIM_DEPT_01',' SIM_DEPT_01','',None,'SIM_SECRET']})
    out, stats = c.standardize_codes(df, {'SIM_科室1_别名':'SIM_DEPT_01','SIM_DEPT_01':'SIM_DEPT_01'}, 'dept_code')
    assert out.dept_code.tolist() == ['SIM_DEPT_01']*2 + ['UNMAPPED']*4
    assert stats == {'slots':6,'matched':2,'unmapped':4}
    assert df.dept_code.iloc[0] == 'SIM_科室1_别名'


def test_mask_no_partial_values_or_input_mutation():
    df = pd.DataFrame({'patient_name':['SIM_SECRET', '', None]})
    out, stats = c.mask_privacy(df, ['patient_name'])
    assert out.patient_name.tolist() == ['[REDACTED]', None, None]
    assert stats == {'patient_name':{'nonempty':1,'processed':1,'missing':2}}
    assert df.patient_name.iloc[0] == 'SIM_SECRET'


def synthetic(source, count=2, dataset=DATASET):
    rows = []
    for i in range(1, count+1):
        row = {col:'SIM_PRIVATE_DO_NOT_LOG' for col in sources.SOURCES[source]['columns']}
        for col in ('visit_id','charge_id','record_id','report_id'):
            if col in row:
                row[col] = f'SIM_{col}_{i}'
        for col in ('visit_date','admit_date','fee_date','report_date'):
            if col in row:
                row[col] = '2025-01-01'
        if 'discharge_date' in row:
            row['discharge_date'] = '2025-01-02'
        for col in ('fee_amount','total_fee','drug_fee','consumable_fee','amount'):
            if col in row:
                row[col] = '10.25'
        for col, value in [('dept_code','SIM_DEPT_01'),('gender','M'),('age','30'),('stay_days','1'),('visit_type','OUTPATIENT'),('disease_code','SIM_DX_01'),('fee_type_code','SERVICE')]:
            if col in row:
                row[col] = value
        row.update(dataset_id=dataset, source_row_number=i, etl_batch_date=date(2025,1,31))
        rows.append(row)
    return rows


def prepared(table, rows):
    return c._prepare(table, rows, BATCH, DATASET, SIM_KEY, 'SIM-unit-v1', '11111111-1111-4111-8111-111111111111', c._config())


@pytest.mark.parametrize('table', list(c.TABLES))
def test_five_projection_statistics_and_source_digest_oracle(table):
    spec = c.TABLES[table]
    raw = synthetic(spec['source'])
    before = deepcopy(raw)
    rows, audit = prepared(table, raw)
    assert len(rows) == audit['source_rows'] == audit['accepted_rows'] == 2
    assert list(rows[0]) == spec['columns']
    assert 'patient_id' not in rows[0]
    assert not set(('diagnosis','result_text','item_name','fee_type_name','bed_no')) & set(rows[0])
    for col, counts in audit['stats_json']['mapping_counts'].items():
        assert counts == dict(slots=2,matched=2,unmapped=0)
    assert raw == before
    cols = sources.SOURCES[spec['source']]['columns'] + c.META
    payload = [[r[col].isoformat() if type(r[col]) is date else r[col] for col in cols] for r in raw]
    expected = hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    assert audit['source_sha256'] == expected
    safe = json.dumps(audit['stats_json'])
    assert 'SIM_PRIVATE_DO_NOT_LOG' not in safe
    assert all(v >= 0 and type(v) is int for count in audit['stats_json']['field_counts'].values() for v in count.values())


@pytest.mark.parametrize('text,expected,missing,invalid,negative', [
    ('',None,1,0,0), ('  ',None,1,0,0), ('0',Decimal('0.00'),0,0,0),
    ('0001.2',Decimal('1.20'),0,0,0), ('-1.23',Decimal('-1.23'),0,0,1),
    ('9999999999999999.99',Decimal('9999999999999999.99'),0,0,0),
    ('10000000000000000.00',None,0,1,0), ('1.000',None,0,1,0),
    ('1e2',None,0,1,0), ('+1',None,0,1,0), ('NaN',None,0,1,0),
    ('Infinity',None,0,1,0), ('1,000',None,0,1,0), (' 1',None,0,1,0),
])
def test_strict_decimal_distinguishes_missing_invalid_negative(text,expected,missing,invalid,negative):
    table = 'dwd_charge_detail'
    raw = synthetic('his_charge',1)
    raw[0]['amount'] = text
    rows, audit = prepared(table,raw)
    assert rows[0]['amount'] == expected
    assert audit['stats_json']['field_counts']['amount'] == dict(missing=missing,invalid=invalid,filled=0,negative=negative)


@pytest.mark.parametrize('value,invalid', [('0',0),('130',0),('131',1),('-1',1),('1.0',1),(' 30',1)])
def test_age_guard(value,invalid):
    raw = synthetic('his_outpatient',1)
    raw[0]['age'] = value
    rows,audit = prepared('dwd_outpatient_visit_detail',raw)
    assert (rows[0]['age'] is None) == bool(invalid)
    assert audit['stats_json']['field_counts']['age']['invalid'] == invalid


@pytest.mark.parametrize('value,missing,invalid', [('',1,0),('2025-02-30',0,1),('2024-12-31',0,1),('2025-01-01',0,0)])
def test_discharge_missing_not_invalid(value,missing,invalid):
    raw = synthetic('his_inpatient',1)
    raw[0]['discharge_date'] = value
    rows,audit = prepared('dwd_inpatient_visit_detail',raw)
    assert audit['stats_json']['field_counts']['discharge_date'] == dict(missing=missing,invalid=invalid,filled=0,negative=0)


@pytest.mark.parametrize('column,value', [('visit_id',' SIM_bad'),('visit_id','x\x00y'),('visit_id','x'*129),('stay_days','-1'),('stay_days','100000000'),('stay_days','1.001')])
def test_optional_id_and_stay_invalid_are_null(column,value):
    source = 'his_inpatient' if column=='stay_days' else 'his_charge'
    table = 'dwd_inpatient_visit_detail' if column=='stay_days' else 'dwd_charge_detail'
    raw = synthetic(source,1); raw[0][column] = value
    rows,audit = prepared(table,raw)
    assert rows[0][column] is None
    assert audit['stats_json']['field_counts'][column]['invalid'] == 1


def test_mutually_exclusive_source_loss_and_conflict_before_privacy():
    raw = synthetic('his_outpatient',5)
    raw[0]['visit_id']=' '
    raw[0]['visit_date']='invalid'
    raw[1]['visit_id']='SIM_CONFLICT';raw[2]['visit_id']='SIM_CONFLICT'
    raw[2]['patient_name']='SIM_DIFFERENT_SECRET'
    raw[3]['visit_date']='2025-02-30'
    rows,audit=prepared('dwd_outpatient_visit_detail',raw)
    assert audit['stats_json']['quarantined_by_reason'] == dict(invalid_id_rows=1,conflicting_id_rows=2,invalid_primary_date_rows=1)
    assert audit['source_rows']==5 and audit['accepted_rows']==1 and audit['quarantined_rows']==4


def test_hmac_known_message_independent_vector_and_dataset_separation():
    message = b'["node07-patient-v1","C7_SIM_UNIT"," SIM_PATIENT "]'
    expected = hmac.new(SIM_KEY,message,'sha256').hexdigest()
    assert c._patient_key(SIM_KEY,DATASET,' SIM_PATIENT ') == expected
    assert c._patient_key(SIM_KEY,DATASET,' SIM_PATIENT ') != c._patient_key(SIM_KEY,'SIM_OTHER',' SIM_PATIENT ')
    assert c._patient_key(SIM_KEY,DATASET,'SIM_PATIENT') != expected
    assert c._patient_key(SIM_KEY,DATASET,'  ') is None


@pytest.mark.parametrize('encoded', ['bad!',base64.b64encode(b'x'*31).decode(),base64.b64encode(b'x'*33).decode()])
def test_invalid_keys_before_connection(monkeypatch,encoded):
    monkeypatch.setenv('DWD_PRIVACY_KEY',encoded)
    connect=MagicMock();monkeypatch.setattr(c,'connect_database',connect)
    with pytest.raises(sources.OdsError,match='^DWD_PRIVACY_KEY_INVALID$'):
        c.run_clean_all(BATCH)
    connect.assert_not_called()


def test_missing_key_before_connection(monkeypatch):
    monkeypatch.delenv('DWD_PRIVACY_KEY')
    connect=MagicMock();monkeypatch.setattr(c,'connect_database',connect)
    with pytest.raises(sources.OdsError,match='^DWD_PRIVACY_KEY_MISSING$'):
        c.run_clean_all(BATCH)
    connect.assert_not_called()


@pytest.mark.parametrize('table', ['dwd_charge_detail;DROP TABLE x','source_config','unknown',None])
def test_table_allowlist_rejects_before_connection(monkeypatch,table):
    connect=MagicMock();monkeypatch.setattr(c,'connect_database',connect)
    with pytest.raises(sources.OdsError,match='^DWD_ARGUMENTS_INVALID$'):
        c.write_dwd(table,BATCH)
    connect.assert_not_called()


def test_digest_loadtime_independent_decimal_exact_null_differs():
    rows,audit = prepared('dwd_charge_detail',synthetic('his_charge',1))
    before=c.quality_digest('dwd',c.TABLES['dwd_charge_detail']['columns'],rows)
    rows[0]['etl_load_time']=datetime(2000,1,1)
    assert c.quality_digest('dwd',c.TABLES['dwd_charge_detail']['columns'],rows)==before
    rows[0]['amount']=None
    assert c.quality_digest('dwd',c.TABLES['dwd_charge_detail']['columns'],rows)!=before
    cols=sources.SOURCES['his_charge']['columns']+c.META
    source=synthetic('his_charge',1); h1=c.quality_digest('source',cols,source)
    source[0]['amount']=''
    h2=c.quality_digest('source',cols,source)
    source[0]['amount']=None
    assert len({h1,h2,c.quality_digest('source',cols,source)})==3


def mocked_transaction(monkeypatch, *, empty=False):
    connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(c,'connect_database',lambda:connection)
    monkeypatch.setattr(c,'_verify',lambda cursor:None)
    monkeypatch.setattr(c,'_registry',lambda cursor,dataset,lock=False:(DATASET,BATCH,('SIM_REGISTRY',)))
    monkeypatch.setattr(c,'_audits',lambda cursor,dataset:{})
    monkeypatch.setattr(c,'_source',lambda cursor,table,dataset:[] if empty else synthetic(c.TABLES[table]['source'],1))
    cursor.fetchone.return_value=(1,)
    return connection,cursor


def test_transaction_single_commit_no_ddl_and_parameterized_dataset(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    result=c.run_clean_all(BATCH,dataset_id=DATASET)
    assert len(result['tables'])==5
    connection.commit.assert_called_once();connection.rollback.assert_not_called()
    queries=[call.args[0] for call in cursor.execute.call_args_list]
    assert sum(q.startswith('DELETE FROM `dwd_') for q in queries)==10
    assert not any('CREATE' in q or 'ALTER' in q for q in queries)
    assert all(DATASET not in q for q in queries)
    assert queries[:2]==['SET TRANSACTION ISOLATION LEVEL REPEATABLE READ','START TRANSACTION WITH CONSISTENT SNAPSHOT']


def test_third_table_failure_rolls_back_without_patient_exception(monkeypatch,capsys):
    connection,cursor=mocked_transaction(monkeypatch)
    def fail(query,values):
        if '`dwd_charge_detail`' in query:
            raise ValueError('SIM_PRIVATE_DO_NOT_LOG')
    cursor.executemany.side_effect=fail
    with pytest.raises(sources.OdsError,match='^DWD_TX_FAILED$') as error:
        c.run_clean_all(BATCH)
    connection.rollback.assert_called_once();connection.commit.assert_not_called()
    assert error.value.__suppress_context__
    assert 'SIM_PRIVATE' not in str(error.value)+capsys.readouterr().out


def test_commit_unknown_not_retryable_even_if_rollback_succeeds(monkeypatch):
    connection,_=mocked_transaction(monkeypatch)
    connection.commit.side_effect=RuntimeError('SIM_DRIVER_SECRET')
    with pytest.raises(sources.OdsError,match='^DWD_COMMIT_UNKNOWN$'):
        c.run_clean_all(BATCH)
    connection.commit.assert_called_once()


def test_rollback_failure_permanent(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    cursor.executemany.side_effect=RuntimeError('SIM_SECRET')
    connection.rollback.side_effect=RuntimeError('SIM_SECRET')
    with pytest.raises(sources.OdsError,match='^DWD_ROLLBACK_FAILED$'):
        c.run_clean_all(BATCH)


def test_unknown_exception_with_numeric_arg_not_deadlock(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    cursor.executemany.side_effect=ValueError(1213)
    with pytest.raises(sources.OdsError,match='^DWD_TX_FAILED$'):
        c.run_clean_all(BATCH)


def test_all_source_empty_no_delete(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch,empty=True)
    with pytest.raises(sources.OdsError,match='^DWD_INPUT_EMPTY$'):
        c.run_clean_all(BATCH)
    assert not any(call.args[0].startswith('DELETE') for call in cursor.execute.call_args_list)


def test_changed_key_rejected_before_any_delete(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    monkeypatch.setattr(c,'_audits',lambda cur,ds:{'SIM':{'privacy_key_id':'SIM-old'}})
    with pytest.raises(sources.OdsError,match='^DWD_PRIVACY_KEY_CHANGED$'):
        c.run_clean_all(BATCH)
    assert not any(call.args[0].startswith('DELETE') for call in cursor.execute.call_args_list)


def test_schema_checks_types_nullability_pk_and_indexes():
    cursor=MagicMock()
    tables=(*c.TABLES,c.AUDIT)
    statuses=[(t,'InnoDB','utf8mb4_bin') for t in tables]
    columns=[(t,col,typ,'YES' if null else 'NO',i,'utf8mb4_bin' if typ.startswith(('char(','varchar(')) else None) for t in tables for i,(col,typ,null) in enumerate(c._column_spec(t),1)]
    indexes=[(t,n,u,i,col,None,'BTREE') for t in tables for n,(u,cols) in c._indexes(t).items() for i,col in enumerate(cols,1)]
    cursor.fetchall.side_effect=[statuses,columns,indexes]
    c._verify(cursor)
    wrong=deepcopy(columns); wrong[0]=(*wrong[0][:2],'varchar(64)',*wrong[0][3:])
    cursor.fetchall.side_effect=[statuses,wrong,indexes]
    with pytest.raises(sources.OdsError,match='^DWD_SCHEMA_INVALID$'):
        c._verify(cursor)
    cursor.fetchall.side_effect=[statuses,columns,indexes[:-1]]
    with pytest.raises(sources.OdsError,match='^DWD_SCHEMA_INVALID$'):
        c._verify(cursor)


def test_registry_requires_all_six_same_dataset_and_source_batch():
    cursor=MagicMock()
    registry=[(name,'csv',str(sources.GENERATED_ROOT/DATASET/spec['path']),DATASET,date(2025,1,31)) for name,spec in sources.SOURCES.items()]
    cursor.fetchall.return_value=registry
    assert c._registry(cursor,DATASET)[:2]==(DATASET,BATCH)
    with pytest.raises(sources.OdsError,match='^DWD_SOURCE_MISMATCH$'):
        c._registry(cursor,'SIM_OTHER')
    cursor.fetchall.return_value=[*registry[:-1],(*registry[-1][:-1],date(2025,1,30))]
    with pytest.raises(sources.OdsError,match='^DWD_SOURCE_MISMATCH$'):
        c._registry(cursor,None)


def test_registry_changed_before_writes_rolls_back(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    monkeypatch.setattr(c,'_registry',lambda cur,ds,lock=False:(DATASET,BATCH,('NEW' if lock else 'OLD',)))
    with pytest.raises(sources.OdsError,match='^DWD_SOURCE_MISMATCH$'):
        c.run_clean_all(BATCH)
    assert not any(call.args[0].startswith('DELETE') for call in cursor.execute.call_args_list)


def test_snapshot_mixed_run_rejected(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    audits={t:prepared(t,synthetic(s['source'],1))[1] for t,s in c.TABLES.items()}
    audits['dwd_charge_detail']['clean_run_id']='22222222-2222-4222-8222-222222222222'
    monkeypatch.setattr(c,'_audits',lambda cur,ds:audits)
    with pytest.raises(sources.OdsError,match='^DQ_SNAPSHOT_MISMATCH$'):
        c.load_quality_snapshot()
    connection.commit.assert_not_called()


def test_snapshot_exact_keys_single_materialized_view(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    outputs={t:prepared(t,synthetic(s['source'],1)) for t,s in c.TABLES.items()}
    monkeypatch.setattr(c,'_audits',lambda cur,ds:{t:a for t,(_,a) in outputs.items()})
    cursor.fetchall.side_effect=[tuple(tuple(r[col] for col in c.TABLES[t]['columns']) for r in outputs[t][0]) for t in c.TABLES]
    snapshot=c.load_quality_snapshot(BATCH,dataset_id=DATASET)
    assert set(snapshot)=={'schema_version','dataset_id','clean_run_id','batch_date','rules_version','mapping_version','privacy_key_id','tables'}
    assert all(set(item)=={'source','dwd','audit'} for item in snapshot['tables'].values())
    assert all(type(item['dwd']['rows'][0]['etl_load_time']) is datetime for item in snapshot['tables'].values())
    connection.rollback.assert_called_once();connection.commit.assert_not_called()


@pytest.mark.parametrize('number', [1205,1213])
def test_only_driver_deadlocks_retry_after_confirmed_rollback(monkeypatch,number):
    connection,cursor=mocked_transaction(monkeypatch)
    cursor.executemany.side_effect=c.pymysql.err.OperationalError(number,'SIM_DRIVER_TEXT')
    with pytest.raises(sources.OdsError,match='^DWD_TX_RETRYABLE$'):
        c.run_clean_all(BATCH)
    connection.rollback.assert_called_once()


@pytest.mark.parametrize('error,expected', [
    (c.pymysql.err.OperationalError(2003,'SIM_CONNECTION'), 'DWD_TX_RETRYABLE'),
    (c.pymysql.err.OperationalError(1045,'SIM_DENIED'), 'DWD_TX_FAILED'),
    (ValueError(2003), 'DWD_TX_FAILED'),
])
def test_prewrite_connection_error_classification(monkeypatch,error,expected):
    monkeypatch.setattr(c,'connect_database',MagicMock(side_effect=error))
    with pytest.raises(sources.OdsError,match='^'+expected+'$'):
        c.run_clean_all(BATCH)


@pytest.mark.parametrize('changes', [
    {'mappings':{'SIM_SECRET':'arbitrary'}}, {'expression':'1+1'},
    {'approved_complete_intervals':[dict(dataset_id=DATASET,table='dwd_lis_report_detail',start_date='2025-01-01',end_date='2025-01-03',basis_id='SIM_FIXTURE')]},
])
def test_fixed_config_rejects_expressions_dictionary_drift_and_same_version_coverage(monkeypatch,tmp_path,changes):
    config=json.loads(c.CONFIG_PATH.read_text())
    config.update(changes)
    path=tmp_path/'synthetic-config.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(c,'CONFIG_PATH',path)
    with pytest.raises(sources.OdsError,match='^DWD_CONFIG_INVALID$'):
        c._config()


def test_approved_interval_precise_declared_shape_and_new_version(monkeypatch,tmp_path):
    config=json.loads(c.CONFIG_PATH.read_text())
    config['rules_version']='node07-rules-v1-SIM-fixture'
    config['approved_complete_intervals']=[dict(dataset_id=DATASET,table='dwd_lis_report_detail',start_date='2025-01-01',end_date='2025-01-03',basis_id='SIM_FIXTURE')]
    path=tmp_path/'synthetic-config.json';path.write_text(json.dumps(config));monkeypatch.setattr(c,'CONFIG_PATH',path)
    assert len(c._config()['approved_complete_intervals'])==1


def test_nonempty_all_quarantine_writes_empty_output_and_audit(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    def bad(cur,table,dataset):
        raw=synthetic(c.TABLES[table]['source'],1);raw[0][c.TABLES[table]['id_column']]=''
        return raw
    monkeypatch.setattr(c,'_source',bad);cursor.fetchone.return_value=(0,)
    result=c.run_clean_all(BATCH)
    assert all(t['accepted_rows']==0 and t['quarantined_rows']==1 for t in result['tables'].values())
    cursor.executemany.assert_not_called();connection.commit.assert_called_once()


def test_no_raw_value_in_unknown_public_operator_error(capsys,caplog):
    with pytest.raises(sources.OdsError) as error:
        c.fill_missing(pd.DataFrame({'x':['SIM_PRIVATE_DO_NOT_LOG',None]}),{'x':'mean'})
    assert str(error.value)=='DWD_ARGUMENTS_INVALID'
    assert 'SIM_PRIVATE_DO_NOT_LOG' not in str(error.value)+capsys.readouterr().out+caplog.text
    assert error.value.__suppress_context__


def test_ddl_file_fixed_and_separate_from_runtime():
    text=(ROOT/'sql/dwd/dwd_tables.sql').read_text()
    assert text==';\n\n'.join(c._ddl())+';\n'
    assert text.count('CREATE TABLE')==8
    assert not any(word in text.upper() for word in ('DROP ','ALTER ','FOREIGN KEY'))


@pytest.fixture
def mysql_fixture(monkeypatch):
    """Coordinator opt-in only. No unit run reads credentials or opens sockets."""
    if os.environ.get('C7_RUN_MYSQL_TESTS') != '1':
        pytest.skip('hospital_test coordinator injection not requested')
    assert sources.database_options(test_only=True)['database']=='hospital_test'
    c._privacy()  # Requires root-injected key; never substitutes SIM key in a real DB.
    from uuid import uuid4
    dataset='C7_TEST_'+uuid4().hex
    sentinel=dataset+'_other'
    monkeypatch.setattr(c,'connect_database',lambda:sources.connect_database(test_only=True))
    connection=sources.connect_database(test_only=True)
    original={}
    registered=[]
    namespace_owned=False
    upsert=('INSERT INTO source_config (name,kind,file_path,dataset_id,etl_batch_date) '
            'VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE '
            'kind=%s,file_path=%s,dataset_id=%s,etl_batch_date=%s')
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name')
            rows=list(cursor.fetchall())
            names=[row[0] for row in rows]
            if len(set(names))!=len(names) or not set(names)<=set(sources.SOURCES):
                raise sources.OdsError('DWD_SOURCE_MISMATCH') from None
            original={row[0]:tuple(row) for row in rows}
        # Unknown registry names are rejected before DDL or any data mutation.
        c.initialize_schema()
        with connection.cursor() as cursor:
            for t,spec in c.TABLES.items():
                for target in (dataset,sentinel):
                    for sql_table in (t,sources.SOURCES[spec['source']]['table']):
                        cursor.execute(f'SELECT COUNT(*) FROM `{sql_table}` WHERE dataset_id=%s',(target,))
                        assert cursor.fetchone()[0]==0
            for target in (dataset,sentinel):
                cursor.execute('SELECT COUNT(*) FROM dwd_clean_audit WHERE dataset_id=%s',(target,))
                assert cursor.fetchone()[0]==0
            namespace_owned=True
            for name,spec in sources.SOURCES.items():
                row=(name,'csv',str(sources.GENERATED_ROOT/dataset/spec['path']),dataset,BATCH)
                registered.append(name)
                cursor.execute(upsert,(*row,*row[1:]))
            for t,spec in c.TABLES.items():
                src=sources.SOURCES[spec['source']]
                rows=synthetic(spec['source'],2,dataset)
                cols=src['columns']+c.META+('etl_load_time',)
                cursor.executemany(f"INSERT INTO `{src['table']}` ("+','.join(f'`{x}`' for x in cols)+') VALUES ('+','.join(['%s']*len(cols))+')',[tuple(row[x] for x in cols[:-1])+(datetime(2025,1,31),) for row in rows])
                # Sentinel setup only; assertions compare direct SQL snapshots from independent connections.
                output,audit=c._prepare(t,synthetic(spec['source'],1,sentinel),BATCH,sentinel,c._privacy()[0],c._privacy()[1],'99999999-9999-4999-8999-999999999999',c._config())
                c._store(cursor,t,output,audit)
        connection.commit()
        yield dataset,sentinel
    finally:
        try:
            connection.rollback()
            if namespace_owned:
                with connection.cursor() as cursor:
                    for t,spec in c.TABLES.items():
                        for table in (t,sources.SOURCES[spec['source']]['table']):
                            cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id IN (%s,%s)',(dataset,sentinel))
                    cursor.execute('DELETE FROM dwd_clean_audit WHERE dataset_id IN (%s,%s)',(dataset,sentinel))
                    for name in registered:
                        if name in original:
                            row=original[name]
                            cursor.execute(upsert,(*row,*row[1:]))
                        else:
                            cursor.execute('DELETE FROM source_config WHERE name=%s AND dataset_id=%s',(name,dataset))
                connection.commit()
        finally:
            connection.close()


def sql_state(dataset):
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            result={}
            for table in (*c.TABLES,c.AUDIT):
                cursor.execute(f'SELECT * FROM `{table}` WHERE dataset_id=%s ORDER BY 1,2',(dataset,))
                result[table]=cursor.fetchall()
            return result
    finally:
        connection.rollback();connection.close()


def test_mysql_five_tables_nonempty_isolated_idempotent_and_shortened_source(mysql_fixture):
    dataset,sentinel=mysql_fixture
    other_before=sql_state(sentinel)
    first=c.run_clean_all(BATCH,dataset_id=dataset)
    before=c.load_quality_snapshot(BATCH,dataset_id=dataset)
    second=c.run_clean_all(BATCH,dataset_id=dataset)
    after=c.load_quality_snapshot(BATCH,dataset_id=dataset)
    assert first['clean_run_id']!=second['clean_run_id']
    for table in c.TABLES:
        assert after['tables'][table]['audit']['accepted_rows']==2
        a=before['tables'][table]['dwd']['rows']; b=after['tables'][table]['dwd']['rows']
        assert [{k:v for k,v in row.items() if k!='etl_load_time'} for row in a]==[{k:v for k,v in row.items() if k!='etl_load_time'} for row in b]
    assert sql_state(sentinel)==other_before
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            for spec in c.TABLES.values():
                cursor.execute(f"DELETE FROM `{sources.SOURCES[spec['source']]['table']}` WHERE dataset_id=%s AND source_row_number=2",(dataset,))
        connection.commit()
    finally:
        connection.close()
    c.run_clean_all(BATCH,dataset_id=dataset)
    shortened=c.load_quality_snapshot(BATCH,dataset_id=dataset)
    assert all(item['audit']['source_rows']==item['audit']['accepted_rows']==len(item['dwd']['rows'])==1 for item in shortened['tables'].values())
    assert sql_state(sentinel)==other_before


def test_mysql_third_insert_fault_rolls_back_all_five_and_audit(mysql_fixture,monkeypatch):
    dataset,sentinel=mysql_fixture
    c.run_clean_all(BATCH,dataset_id=dataset)
    before=sql_state(dataset);other_before=sql_state(sentinel)
    store=c._store
    def fault(cursor,table,rows,audit):
        if table=='dwd_charge_detail':
            cursor.execute('INSERT INTO dwd_charge_detail (dataset_id) VALUES (%s)',(dataset,))
            raise AssertionError('MySQL strict NOT NULL expected')
        return store(cursor,table,rows,audit)
    monkeypatch.setattr(c,'_store',fault)
    with pytest.raises(sources.OdsError,match='^DWD_TX_FAILED$'):
        c.run_clean_all(BATCH,dataset_id=dataset)
    assert sql_state(dataset)==before
    assert sql_state(sentinel)==other_before


def test_mysql_single_table_mixed_run_rejected(mysql_fixture):
    dataset,_=mysql_fixture
    c.run_clean_all(BATCH,dataset_id=dataset)
    c.write_dwd('dwd_charge_detail',BATCH,dataset_id=dataset)
    with pytest.raises(sources.OdsError,match='^DQ_SNAPSHOT_MISMATCH$'):
        c.load_quality_snapshot(dataset_id=dataset)


def test_mysql_all_empty_preserves_old_then_all_quarantine_clears(mysql_fixture):
    dataset,sentinel=mysql_fixture
    c.run_clean_all(BATCH,dataset_id=dataset)
    before=sql_state(dataset);other_before=sql_state(sentinel)
    connection=sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            for spec in c.TABLES.values():
                cursor.execute(f"DELETE FROM `{sources.SOURCES[spec['source']]['table']}` WHERE dataset_id=%s",(dataset,))
        connection.commit()
        with pytest.raises(sources.OdsError,match='^DWD_INPUT_EMPTY$'):
            c.run_clean_all(BATCH,dataset_id=dataset)
        assert sql_state(dataset)==before
        with connection.cursor() as cursor:
            for spec in c.TABLES.values():
                src=sources.SOURCES[spec['source']];cols=src['columns']+c.META+('etl_load_time',)
                row=synthetic(spec['source'],1,dataset)[0];row[spec['id_column']]=''
                cursor.execute(f"INSERT INTO `{src['table']}` ("+','.join(f'`{x}`' for x in cols)+') VALUES ('+','.join(['%s']*len(cols))+')',tuple(row[x] for x in cols[:-1])+(datetime(2025,1,31),))
        connection.commit()
        result=c.run_clean_all(BATCH,dataset_id=dataset)
        assert all(item['accepted_rows']==0 and item['quarantined_rows']==1 for item in result['tables'].values())
        snapshot=c.load_quality_snapshot(dataset_id=dataset)
        assert all(not item['dwd']['rows'] for item in snapshot['tables'].values())
        assert sql_state(sentinel)==other_before
    finally:
        connection.close()


def test_unknown_ods_error_prefix_is_not_public_code_or_retry(monkeypatch):
    connection,cursor=mocked_transaction(monkeypatch)
    monkeypatch.setattr(c,'_source',MagicMock(side_effect=sources.OdsError('DWD_SIM_PRIVATE_DO_NOT_LOG')))
    with pytest.raises(sources.OdsError,match='^DWD_TX_FAILED$'):
        c.run_clean_all(BATCH)


def test_digest_invalid_kind_column_or_nonfinite_rejected():
    cols=c.TABLES['dwd_charge_detail']['columns']
    rows,_=prepared('dwd_charge_detail',synthetic('his_charge',1))
    with pytest.raises(sources.OdsError,match='^DQ_ARGUMENTS_INVALID$'):
        c.quality_digest('bad',cols,rows)
    with pytest.raises(sources.OdsError,match='^DQ_ARGUMENTS_INVALID$'):
        c.quality_digest('dwd',['SIM_SECRET'],rows)
    rows[0]['amount']=Decimal('NaN')
    with pytest.raises(sources.OdsError,match='^DQ_ARGUMENTS_INVALID$'):
        c.quality_digest('dwd',cols,rows)


def test_snapshot_audit_stats_rejects_raw_extra_fields():
    rows,audit=prepared('dwd_lis_report_detail',synthetic('lis_report',1))
    audit['stats_json']['SIM_PRIVATE_DO_NOT_LOG']='SIM_PRIVATE_DO_NOT_LOG'
    cursor=MagicMock()
    cursor.fetchall.return_value=[tuple(json.dumps(audit[col]) if col=='stats_json' else audit[col] for col in c.AUDIT_COLUMNS)]
    with pytest.raises(sources.OdsError,match='^DWD_SCHEMA_INVALID$'):
        c._audits(cursor,DATASET)


@pytest.mark.parametrize('value', ['', '2025-1-01', '2025-02-29', '2025-01-01T00:00:00', ' 2025-01-01'])
def test_primary_date_strict_never_truncates_or_trims(value):
    raw=synthetic('lis_report',1);raw[0]['report_date']=value
    rows,audit=prepared('dwd_lis_report_detail',raw)
    assert rows==[]
    assert audit['stats_json']['quarantined_by_reason']['invalid_primary_date_rows']==1


@pytest.mark.parametrize('key_id', ['SIM bad','../SIM','SIM'*22])
def test_key_id_strict_before_connect(monkeypatch,key_id):
    monkeypatch.setenv('DWD_PRIVACY_KEY_ID',key_id)
    connect=MagicMock();monkeypatch.setattr(c,'connect_database',connect)
    with pytest.raises(sources.OdsError,match='^DWD_PRIVACY_KEY_INVALID$'):
        c.run_clean_all(BATCH)
    connect.assert_not_called()


def test_identical_raw_business_retains_min_row_but_never_metadata_latest():
    raw=synthetic('his_outpatient',1)
    other=deepcopy(raw[0]);other['source_row_number']=9;other['etl_batch_date']=date(2025,2,1)
    raw[0]['source_row_number']=2
    output,audit=prepared('dwd_outpatient_visit_detail',[other,raw[0]])
    assert output[0]['source_row_number']==2
    assert output[0]['source_batch_date']==date(2025,1,31)
    assert audit['duplicate_removed']==1


def test_mapping_empty_and_unknown_still_have_slots_in_pipeline():
    raw=synthetic('emr_record',1);raw[0]['dept_code']='';raw[0]['disease_code']='SIM_UNKNOWN'
    output,audit=prepared('dwd_emr_record_detail',raw)
    assert output[0]['dept_code']==output[0]['disease_code']=='UNMAPPED'
    assert audit['stats_json']['mapping_counts']=={col:dict(slots=1,matched=0,unmapped=1) for col in ('dept_code','disease_code')}


def test_no_formal_fact_imputation_missing_optional_visit_and_amount():
    raw=synthetic('his_charge',1);raw[0]['visit_id']='';raw[0]['amount']='';raw[0]['patient_id']=''
    output,audit=prepared('dwd_charge_detail',raw)
    assert output[0]['visit_id'] is None and output[0]['amount'] is None and output[0]['patient_key'] is None
    assert audit['stats_json']['filled_rows']==audit['stats_json']['filled_cells']==0
    assert audit['stats_json']['privacy_counts']['patient_id']==dict(nonempty=0,processed=0,missing=1)


def mock_mysql_registry(monkeypatch, original_count, interrupt=None, unknown=False):
    """Exercise actual fixture generator with transactional in-memory registry only."""
    original={name:(name,'csv',f'SIM_OLD/{name}',f'SIM_BEFORE_{i}',date(2020,1,i+1))
              for i,name in enumerate(list(sources.SOURCES)[:original_count])}
    if unknown:
        original['SIM_UNKNOWN']=('SIM_UNKNOWN','csv','SIM_UNTOUCHED','SIM_UNKNOWN_DATASET',date(2020,1,1))
    state={'committed':deepcopy(original),'working':deepcopy(original),'interrupted':False,'setup_upserts':0,'stores':0}
    connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
    def execute(query,params=None):
        if query.startswith('SELECT name,kind,file_path,dataset_id,etl_batch_date'):
            cursor.fetchall.return_value=list(state['working'].values())
        elif query.startswith('SELECT COUNT(*)'):
            cursor.fetchone.return_value=(0,)
        elif query.startswith('INSERT INTO source_config'):
            row=tuple(params[:5]);state['working'][row[0]]=row
            if str(row[3]).startswith('C7_TEST_'):
                state['setup_upserts']+=1
                if interrupt=='upsert' and state['setup_upserts']==3 and not state['interrupted']:
                    state['interrupted']=True
                    raise RuntimeError('SIM_SETUP_INTERRUPTED')
        elif query.startswith('UPDATE source_config'):
            kind,path,ds,batch,name=params
            if name in state['working']:
                state['working'][name]=(name,kind,path,ds,batch)
        elif query.startswith('DELETE FROM source_config'):
            name,ds=params
            if name in state['working'] and state['working'][name][3]==ds:
                del state['working'][name]
    cursor.execute.side_effect=execute
    def rollback():
        state['working']=deepcopy(state['committed'])
    def commit():
        state['committed']=deepcopy(state['working'])
        if interrupt=='commit' and not state['interrupted']:
            state['interrupted']=True
            raise RuntimeError('SIM_SETUP_INTERRUPTED')
    connection.rollback.side_effect=rollback;connection.commit.side_effect=commit
    options=MagicMock(return_value={'database':'hospital_test'})
    connect=MagicMock(return_value=connection)
    initialize=MagicMock()
    monkeypatch.setenv('C7_RUN_MYSQL_TESTS','1')
    monkeypatch.setattr(sources,'database_options',options)
    monkeypatch.setattr(sources,'connect_database',connect)
    monkeypatch.setattr(c,'initialize_schema',initialize)
    def store(cur,table,rows,audit):
        state['stores']+=1
        if interrupt=='store' and state['stores']==3:
            state['interrupted']=True
            raise RuntimeError('SIM_SETUP_INTERRUPTED')
    monkeypatch.setattr(c,'_store',store)
    return original,state,connection,cursor,options,connect,initialize


@pytest.mark.parametrize('original_count',range(7))
@pytest.mark.parametrize('interrupt',[None,'upsert','store','commit'])
def test_registry_fixture_self_contained_and_restores_after_setup_interrupt(monkeypatch,original_count,interrupt):
    original,state,connection,cursor,options,connect,_=mock_mysql_registry(monkeypatch,original_count,interrupt)
    fixture=mysql_fixture.__wrapped__(monkeypatch)
    if interrupt:
        with pytest.raises(RuntimeError,match='^SIM_SETUP_INTERRUPTED$'):
            next(fixture)
        assert state['interrupted']
    else:
        dataset,sentinel=next(fixture)
        assert dataset.startswith('C7_TEST_') and sentinel==dataset+'_other'
        assert set(state['committed'])==set(sources.SOURCES)
        assert all(row[3]==dataset for row in state['committed'].values())
        fixture.close()
    assert state['committed']==original
    options.assert_called_once_with(test_only=True)
    assert all(call.kwargs=={'test_only':True} for call in connect.call_args_list)
    connection.close.assert_called_once()
    deletes=[call for call in cursor.execute.call_args_list if call.args[0].startswith('DELETE FROM source_config')]
    assert all(call.args[1][0] not in original for call in deletes)
    assert all(call.args[1][0] in sources.SOURCES and str(call.args[1][1]).startswith('C7_TEST_') for call in deletes)
    namespace_deletes=[call for call in cursor.execute.call_args_list if call.args[0].startswith('DELETE FROM `') or call.args[0].startswith('DELETE FROM dwd_clean_audit')]
    assert len(namespace_deletes)==11
    assert all(str(call.args[1][0]).startswith('C7_TEST_') and call.args[1][1]==call.args[1][0]+'_other' for call in namespace_deletes)


def test_registry_fixture_unknown_names_reject_without_any_mutation(monkeypatch):
    original,state,connection,cursor,_,_,initialize=mock_mysql_registry(monkeypatch,2,unknown=True)
    fixture=mysql_fixture.__wrapped__(monkeypatch)
    with pytest.raises(sources.OdsError,match='^DWD_SOURCE_MISMATCH$'):
        next(fixture)
    assert state['committed']==state['working']==original
    initialize.assert_not_called()
    assert not any(call.args[0].startswith(('INSERT','UPDATE','DELETE')) for call in cursor.execute.call_args_list)
    connection.commit.assert_not_called();connection.close.assert_called_once()


@pytest.mark.parametrize('table',list(c.TABLES))
def test_field_counts_exclude_all_six_metadata_and_patient_id(table):
    spec=c.TABLES[table]
    _,audit=prepared(table,synthetic(spec['source'],1))
    fields=audit['stats_json']['field_counts']
    assert set(fields)==set(spec['columns'])-set(c.COMMON)-set(c.SUPPRESSION)
    assert not set(c.COMMON)&set(fields)
    assert 'patient_id' not in fields
    assert 'patient_id' in audit['stats_json']['privacy_counts']


def test_registry_fixture_never_deletes_replacement_owned_by_another_dataset(monkeypatch):
    _,state,_,_,_,_,_=mock_mysql_registry(monkeypatch,0)
    fixture=mysql_fixture.__wrapped__(monkeypatch)
    next(fixture)
    name=next(iter(sources.SOURCES))
    replacement=(name,'csv','SIM_OTHER_PATH','SIM_OTHER_OWNER',date(2020,1,1))
    state['committed'][name]=replacement
    fixture.close()
    assert state['committed']=={name:replacement}


@pytest.mark.parametrize('guard',['target','key'])
def test_registry_fixture_keeps_test_only_and_key_checks_before_connection(monkeypatch,guard):
    monkeypatch.setenv('C7_RUN_MYSQL_TESTS','1')
    options=MagicMock(return_value={'database':'hospital' if guard=='target' else 'hospital_test'})
    connect=MagicMock()
    monkeypatch.setattr(sources,'database_options',options)
    monkeypatch.setattr(sources,'connect_database',connect)
    if guard=='key':
        monkeypatch.delenv('DWD_PRIVACY_KEY')
    fixture=mysql_fixture.__wrapped__(monkeypatch)
    if guard=='target':
        with pytest.raises(AssertionError):
            next(fixture)
    else:
        with pytest.raises(sources.OdsError,match='^DWD_PRIVACY_KEY_MISSING$'):
            next(fixture)
    options.assert_called_once_with(test_only=True)
    connect.assert_not_called()


def test_fill_duplicate_index_counts_row_positions_once_across_columns():
    original=pd.DataFrame({'left':['',None], 'right':[None,'']},index=['same','same'])
    before=original.copy(deep=True)
    result,stats=c.fill_missing(original,{'left':'未知','right':'未知'})
    assert stats=={'filled_rows':2,'filled_cells':4}
    expected=pd.DataFrame({'left':['未知','未知'],'right':['未知','未知']},index=original.index)
    pd.testing.assert_frame_equal(result,expected)
    pd.testing.assert_frame_equal(original,before)


def test_fill_duplicate_index_numeric_and_text_keep_behavior_and_input():
    original=pd.DataFrame({'mean':[1,None,None,7],'median':[1,None,None,9],
                           'text':['SIM_KEEP','',None,'SIM_KEEP']},index=[8,8,8,8])
    before=original.copy(deep=True)
    result,stats=c.fill_missing(original,{'mean':'mean','median':'median','text':'未知'})
    assert stats=={'filled_rows':2,'filled_cells':6}
    assert result['mean'].tolist()==[1,4,4,7]
    assert result['median'].tolist()==[1,5,5,9]
    assert result['text'].tolist()==['SIM_KEEP','未知','未知','SIM_KEEP']
    assert result.index.equals(original.index)
    pd.testing.assert_frame_equal(original,before)


def test_mask_and_dedupe_duplicate_index_use_row_positions():
    private=pd.DataFrame({'patient_name':['SIM_ONE','SIM_TWO','']},index=[4,4,4])
    before=private.copy(deep=True)
    output,stats=c.mask_privacy(private,['patient_name'])
    assert output.patient_name.tolist()==['[REDACTED]','[REDACTED]',None]
    assert stats=={'patient_name':{'nonempty':2,'processed':2,'missing':1}}
    assert output.index.equals(private.index)
    pd.testing.assert_frame_equal(private,before)
    raw=frame();raw.index=[4]*len(raw)
    before=raw.copy(deep=True)
    output,stats=c.dedupe(raw,['visit_id'])
    assert sorted(output.source_row_number.tolist())==[1,2]
    assert stats=={'invalid_id_rows':1,'conflicting_id_rows':2,'duplicate_removed':1}
    pd.testing.assert_frame_equal(raw,before)


def assert_ods_insert_covers_ddl(query,values):
    """Independent check of bound INSERT fields against the existing ODS DDL."""
    import re
    match=re.fullmatch(r'INSERT INTO `([a-z_]+)` \(([^)]+)\) VALUES \(([^)]+)\)',query)
    assert match is not None
    table,columns,bindings=match.groups()
    columns=[column.strip('`') for column in columns.split(',')]
    ddl=(ROOT/'sql/ods/ods_tables.sql').read_text()
    body=re.search(r'CREATE TABLE IF NOT EXISTS '+re.escape(table)+r' \((.*?)\) ENGINE=',ddl,re.S)
    assert body is not None
    required={line.split()[0] for line in body.group(1).splitlines()
              if 'NOT NULL' in line and 'AUTO_INCREMENT' not in line and 'DEFAULT' not in line}
    assert required<=set(columns)
    assert len(columns)==len(set(columns))==len(values)==len(bindings.split(','))
    assert all(binding=='%s' for binding in bindings.split(','))
    assert values[columns.index('etl_load_time')]==datetime(2025,1,31)


def test_fixture_seed_ods_inserts_cover_required_ddl_columns(monkeypatch):
    original,state,_,cursor,_,_,_=mock_mysql_registry(monkeypatch,0)
    fixture=mysql_fixture.__wrapped__(monkeypatch)
    try:
        next(fixture)
        calls=cursor.executemany.call_args_list
        assert len(calls)==5
        for call in calls:
            query,rows=call.args
            assert len(rows)==2
            for values in rows:
                assert_ods_insert_covers_ddl(query,values)
    finally:
        fixture.close()
    assert state['committed']==original


def test_fixture_quarantine_reinsert_covers_required_ddl_columns(monkeypatch):
    connection=MagicMock();cursor=connection.cursor.return_value.__enter__.return_value
    connect=MagicMock(return_value=connection)
    monkeypatch.setattr(sources,'connect_database',connect)
    monkeypatch.setattr(c,'run_clean_all',MagicMock(side_effect=[{},sources.OdsError('DWD_INPUT_EMPTY'),
                         {'tables':{table:{'accepted_rows':0,'quarantined_rows':1} for table in c.TABLES}}]))
    monkeypatch.setattr(c,'load_quality_snapshot',lambda **kwargs:{'tables':{table:{'dwd':{'rows':[]}} for table in c.TABLES}})
    monkeypatch.setattr(__import__(__name__,fromlist=['sql_state']),'sql_state',lambda dataset:('SIM_UNCHANGED',))
    test_mysql_all_empty_preserves_old_then_all_quarantine_clears(('C7_TEST_UNIT_LOAD_TIME','C7_TEST_UNIT_LOAD_TIME_other'))
    inserts=[call.args for call in cursor.execute.call_args_list if call.args[0].startswith('INSERT INTO `ods_')]
    assert len(inserts)==5
    for query,values in inserts:
        assert_ods_insert_covers_ddl(query,values)
    connect.assert_called_once_with(test_only=True)
    connection.close.assert_called_once()
