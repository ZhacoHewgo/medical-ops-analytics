"""I7 synthetic units and coordinator-injected, self-contained hospital_test CLI cases."""
import base64
from copy import deepcopy
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from etl.config import source_config as sources
from etl.jobs import clean_dwd as clean, extract_ods as extract, quality_check as quality
from etl.jobs import run_all as cli
from etl.scheduler import scheduler as s

BATCH = '2025-01-31'
SIM_KEY = base64.b64encode(b'SIM_ONLY_' + b'x' * 23).decode()
TABLES = tuple(clean.TABLES)
REAL_GENERATE = quality.generate_quality_report


def infos(dataset='I7_SIM', batch=BATCH):
    return {name: dict(kind='csv', file_path=str(sources.GENERATED_ROOT / dataset / spec['path']),
                       dataset_id=dataset, etl_batch_date=batch) for name, spec in sources.SOURCES.items()}


def cleaned(dataset='I7_SIM', batch=BATCH):
    identity = dict(dataset_id=dataset, batch_date=batch, clean_run_id='11111111-1111-4111-8111-111111111111')
    return dict(**identity, tables={t: dict(**identity, table=t, accepted_rows=3) for t in TABLES})


def report(result=None, gate='PASS'):
    result = result or cleaned()
    return dict(**{k: result[k] for k in ('dataset_id', 'batch_date', 'clean_run_id')},
                privacy_key_id='SIM-unit-v1', env='test', gate_status=gate,
                tables={t: dict(accepted_rows=3) for t in TABLES}, score=100)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, request):
    if request.node.name.startswith('test_mysql_'):
        return
    for name in ('ODS_DATABASE_URL', 'ODS_TEST_DATABASE_URL', 'DWD_PRIVACY_KEY', 'DWD_PRIVACY_KEY_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ODS_DATABASE_URL', 'mysql://SIM:SIM@127.0.0.1:13306/hospital_test')
    monkeypatch.setenv('DWD_PRIVACY_KEY', SIM_KEY)
    monkeypatch.setenv('DWD_PRIVACY_KEY_ID', 'SIM-unit-v1')
    monkeypatch.setattr(s, '_TASKS', {})
    monkeypatch.setattr(s, '_SCHEDULERS', {})
    monkeypatch.setattr(s, 'LOG_ROOT', tmp_path / 'logs')
    monkeypatch.setattr(s.time, 'sleep', lambda _: None)
    monkeypatch.setattr(sources, 'connect_database', lambda **kw: pytest.fail('unit opened DB'))
    monkeypatch.setattr(extract, 'list_sources', lambda: [dict(name=n, **v) for n, v in infos().items()])
    monkeypatch.setattr(extract, 'run_extract_all', lambda *a, **kw: dict.fromkeys(s.SOURCES, 3))
    monkeypatch.setattr(clean, 'run_clean_all', lambda *a, **kw: cleaned())
    monkeypatch.setattr(quality, 'generate_quality_report', lambda *a, **kw: report())


def events():
    return [json.loads(line) for p in s.LOG_ROOT.glob('test/etl_*.log') for line in p.read_text().splitlines()]


def test_quality_cli_new_chain_and_flat_results(capsys):
    assert cli.main(['--env', 'test', '--through', 'quality']) == 0
    output = json.loads(capsys.readouterr().out)
    assert list(output['tasks']) == ['extract_ods', 'clean_dwd', 'quality_check']
    assert output['tasks']['quality_check'] == dict.fromkeys(TABLES, 3)
    assert [r['status'] for r in events()] == ['START', 'SUCCESS'] * 3
    assert [r['rows'] for r in events() if r['status'] == 'SUCCESS'] == [18, 15, 15]
    assert len({r['run_id'] for r in events()}) == 1


def test_default_extract_unchanged(monkeypatch):
    monkeypatch.setattr(extract, 'list_sources', lambda: pytest.fail('default added registry call'))
    monkeypatch.setattr(clean, 'run_clean_all', lambda *a, **kw: pytest.fail('default cleaned'))
    assert cli.main(['--env', 'test']) == 0
    assert list(s._TASKS) == ['extract_ods']


def test_six_source_event_protocol_and_lock_whole_chain(monkeypatch):
    def locked():
        with pytest.raises(s.EtlError, match='^ETL_RUN_LOCKED$'):
            with s._lock('test', 'run'):
                pytest.fail('run lock released')
    def extraction(*args, **kwargs):
        locked()
        assert kwargs['source_infos'] == infos()
        for name in s.SOURCES:
            kwargs['on_event'](name, 'START', None, 0)
            kwargs['on_event'](name, 'SUCCESS', 3, 1)
        return dict.fromkeys(s.SOURCES, 3)
    def cleaning(*args, **kwargs):
        locked(); assert args == (BATCH,); assert kwargs == {'dataset_id': 'I7_SIM'}
        return cleaned()
    def checking(*args, **kwargs):
        locked(); assert args == (BATCH,); assert kwargs == {'dataset_id': 'I7_SIM', 'env': 'test'}
        return report()
    monkeypatch.setattr(extract, 'run_extract_all', extraction)
    monkeypatch.setattr(clean, 'run_clean_all', cleaning)
    monkeypatch.setattr(quality, 'generate_quality_report', checking)
    assert cli.main(['--env', 'test', '--through', 'quality']) == 0
    rows = events()
    assert len([r for r in rows if r['name'] in s.SOURCES and r['status'] == 'SUCCESS']) == 6
    assert all(set(r) == {'timestamp','env','run_id','attempt','name','status','code','rows','cost_ms'} for r in rows)


@pytest.mark.parametrize('stage,code', [('extract_ods','ODS_SCHEMA_INVALID'), ('clean_dwd','DWD_COMMIT_UNKNOWN'), ('quality_check','DQ_REPORT_WRITE_FAILED')])
def test_failure_blocks_remaining_and_sentinel(stage, code, monkeypatch, capsys):
    def bad(*args, **kwargs):
        raise sources.OdsError(code)
    module, name = {'extract_ods': (extract,'run_extract_all'), 'clean_dwd': (clean,'run_clean_all'), 'quality_check': (quality,'generate_quality_report')}[stage]
    monkeypatch.setattr(module, name, bad)
    original = s.run_all
    def run(**kwargs):
        s.register_task('sentinel', lambda: pytest.fail('failed gate admitted successor'))
        return original(**kwargs)
    monkeypatch.setattr(s, 'run_all', run)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert json.loads(capsys.readouterr().out)['code'] == code
    names = ['extract_ods','clean_dwd','quality_check','sentinel']
    assert [r['name'] for r in events() if r['status'] == 'BLOCKED'] == names[names.index(stage)+1:]
    assert len([r for r in events() if r['name'] == stage and r['status'] == 'START']) == 1


@pytest.mark.parametrize('gate,code', [('FAIL','DQ_GATE_FAILED'), ('BLOCKED_INSUFFICIENT_EVIDENCE','DQ_INSUFFICIENT_EVIDENCE')])
def test_report_written_before_gate_error(gate, code, monkeypatch, tmp_path, capsys):
    path = tmp_path / 'report.json'
    def generate(*args, **kwargs):
        value = report(gate=gate); path.write_text(json.dumps(value)); return value
    monkeypatch.setattr(quality, 'generate_quality_report', generate)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert path.exists() and json.loads(path.read_text())['gate_status'] == gate
    assert json.loads(capsys.readouterr().out)['code'] == code
    assert not any(r['name'] == 'quality_check' and r['status'] == 'SUCCESS' for r in events())


@pytest.mark.parametrize('stage,code', [('clean','DWD_TX_RETRYABLE'),('quality','DQ_READ_RETRYABLE')])
def test_transient_retry_uses_same_identity(stage, code, monkeypatch):
    calls = []
    def retry(batch, **kwargs):
        calls.append((batch, kwargs.copy()))
        if len(calls) < 4:
            raise sources.OdsError(code)
        return cleaned() if stage == 'clean' else report()
    monkeypatch.setattr(clean if stage == 'clean' else quality, 'run_clean_all' if stage == 'clean' else 'generate_quality_report', retry)
    assert cli.main(['--env','test','--through','quality']) == 0
    assert len(calls) == 4 and all(item == calls[0] for item in calls)


@pytest.mark.parametrize('code', sorted(clean.SAFE_CODES - {'DWD_TX_RETRYABLE','DQ_READ_RETRYABLE'}))
def test_all_new_permanent_codes_preserved_without_retry(code):
    calls = []
    def bad():
        calls.append(1); raise sources.OdsError(code)
    s.register_task('test_stage', bad)
    with pytest.raises(s.EtlError, match='^'+code+'$'):
        s.run_all(env='test')
    assert calls == [1] and events()[-1]['code'] == code


@pytest.mark.parametrize('stage', ['clean','quality'])
def test_new_unknown_exceptions_redacted_and_permanent(stage, monkeypatch, capsys):
    calls = []
    def bad(*args, **kwargs):
        calls.append(1); raise RuntimeError('SIM_SECRET mysql://hidden')
    monkeypatch.setattr(clean if stage == 'clean' else quality, 'run_clean_all' if stage == 'clean' else 'generate_quality_report', bad)
    assert cli.main(['--env','test','--through','quality']) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['code'] == ('DWD_INTERNAL_ERROR' if stage == 'clean' else 'DQ_INTERNAL_ERROR')
    assert calls == [1] and 'SIM_SECRET' not in output.out + output.err + json.dumps(events())


@pytest.mark.parametrize('change', ['dataset','batch','path','key'])
def test_frozen_identity_change_refused(change, monkeypatch, capsys):
    def extraction(*args, **kwargs):
        updated = infos('I7_OTHER') if change == 'dataset' else infos(batch='2025-02-01') if change == 'batch' else infos()
        if change == 'path':
            updated['his_charge']['file_path'] += '.other'
        if change == 'key':
            monkeypatch.setenv('DWD_PRIVACY_KEY_ID','SIM-other')
        monkeypatch.setattr(extract, 'list_sources', lambda: [dict(name=n, **v) for n,v in updated.items()])
        return dict.fromkeys(s.SOURCES,3)
    monkeypatch.setattr(extract, 'run_extract_all', extraction)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert json.loads(capsys.readouterr().out)['code'] in {'DWD_SOURCE_MISMATCH','DWD_PRIVACY_KEY_CHANGED'}
    assert events()[-1]['name'] == 'quality_check' and events()[-1]['status'] == 'BLOCKED'


@pytest.mark.parametrize('bad', ['bool','float','zero','missing','identity','run','key','gate'])
def test_bad_report_never_success(bad, monkeypatch):
    value = report()
    if bad in {'bool','float','zero'}: value['tables'][TABLES[0]]['accepted_rows'] = {'bool':True,'float':3.0,'zero':0}[bad]
    elif bad == 'missing': value['tables'].pop(TABLES[0])
    elif bad == 'identity': value['dataset_id'] = 'OTHER'
    elif bad == 'run': value['clean_run_id'] = str(uuid4())
    elif bad == 'key': value['privacy_key_id'] = 'OTHER'
    else: value['gate_status'] = 'UNKNOWN'
    monkeypatch.setattr(quality, 'generate_quality_report', lambda *a, **kw: value)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert not any(r['name'] == 'quality_check' and r['status'] == 'SUCCESS' for r in events())


@pytest.mark.parametrize('bad', ['bool','missing','identity','nested'])
def test_bad_clean_result_refused(bad, monkeypatch):
    value = cleaned()
    if bad == 'bool': value['tables'][TABLES[0]]['accepted_rows'] = True
    elif bad == 'missing': value['tables'].pop(TABLES[0])
    elif bad == 'identity': value['dataset_id'] = 'OTHER'
    else: value['tables'][TABLES[0]]['clean_run_id'] = str(uuid4())
    monkeypatch.setattr(clean, 'run_clean_all', lambda *a, **kw: value)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert events()[-1]['status'] == 'BLOCKED'


def test_duplicate_registration_not_erased():
    s.register_task('extract_ods', lambda: {})
    assert cli.main(['--env','test','--through','quality']) == 1
    assert list(s._TASKS) == ['extract_ods']


def test_scheduled_runs_refresh_sources_and_batch_but_retry_keeps_frozen(monkeypatch):
    calls = []; identities = []
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2025, 2, 2, 2, tzinfo=tz)
    monkeypatch.setattr(cli,'datetime',Clock)
    def extraction(start,end,**kwargs):
        calls.append((start,end,deepcopy(kwargs['source_infos'])))
        if len(calls) == 1: raise sources.OdsError('ODS_LOAD_FAILED')
        return dict.fromkeys(s.SOURCES,3)
    def cleaning(batch,*,dataset_id):
        identities.append((dataset_id,batch)); return cleaned(dataset_id,batch)
    def checking(batch,*,dataset_id,env): return report(cleaned(dataset_id,batch))
    monkeypatch.setattr(extract,'run_extract_all',extraction)
    monkeypatch.setattr(clean,'run_clean_all',cleaning)
    monkeypatch.setattr(quality,'generate_quality_report',checking)
    class Schedule:
        def start(self):
            s._daily_run('test')
            monkeypatch.setattr(extract,'list_sources',lambda:[dict(name=n,**v) for n,v in infos('I7_NEW','2025-02-28').items()])
            s._daily_run('test')
    monkeypatch.setattr(s,'setup_schedule',lambda **kwargs:Schedule())
    assert cli.main(['--env','test','--through','quality','--schedule']) == 0
    assert calls[0] == calls[1]
    assert all(x[:2] == ('2025-02-01','2025-02-01') for x in calls)
    assert identities == [('I7_SIM', BATCH),('I7_NEW','2025-02-28')]
    assert len({r['run_id'] for r in events()}) == 2


def sim_csv_rows(name, *, invalid_amount=False):
    """Three independently specified, valid SIM days; never read original CSVs."""
    rows = []
    for i in range(1, 4):
        day = f'2025-01-0{i}'
        row = dict.fromkeys(sources.SOURCES[name]['columns'], 'SIM_PRIVATE')
        for field in ('visit_id','charge_id','record_id','report_id'):
            if field in row:
                row[field] = f'SIM_{field}_{i}'
        if 'patient_id' in row:
            row['patient_id'] = f'SIM_PATIENT_{i}'
        for field in ('visit_date','admit_date','fee_date','report_date','stat_date'):
            if field in row:
                row[field] = day
        values = dict(discharge_date=f'2025-01-0{i+1}', dept_code='SIM_DEPT_01', gender='M',
                      age='30', stay_days='1', visit_type='OUTPATIENT', disease_code='SIM_DX_01',
                      fee_type_code='SERVICE', fee_amount='10.25', total_fee='10.25', drug_fee='1.00',
                      consumable_fee='1.00', amount='10.25', revenue='10.25', drug_revenue='1.00', consumable_revenue='1.00')
        row.update({k:v for k,v in values.items() if k in row})
        if invalid_amount and name == 'his_charge' and i == 1:
            row['amount'] = 'SIM_INVALID_DECIMAL'
        rows.append(row)
    return rows


@pytest.fixture
def mysql_pipeline(monkeypatch):
    """Only ODS_TEST_DATABASE_URL enables this fixture; no secondary opt-in flag."""
    if not os.environ.get('ODS_TEST_DATABASE_URL'):
        pytest.skip('hospital_test URL not injected; real CLI NOT RUN')
    assert sources.database_options(test_only=True)['database'] == 'hospital_test'
    # Real tests require coordinator key injection; no SIM key fallback.
    clean._privacy()
    monkeypatch.setenv('ODS_DATABASE_URL', os.environ['ODS_TEST_DATABASE_URL'])
    from datetime import date, timedelta
    dataset = 'I7_SIM_' + uuid4().hex
    batch = (date(3000,1,1) + timedelta(days=int(uuid4().hex[:8],16) % 1000000)).isoformat()
    directory = sources.GENERATED_ROOT / dataset
    assert not directory.exists()
    log_root = Path(quality._LOG_ROOT)
    report_path = log_root / 'test' / ('quality_report_' + batch.replace('-','') + '.json')
    assert not report_path.exists(), 'random test report namespace already occupied'
    selected = infos(dataset, batch)
    original = {}; attempted = []; owned = False
    connection = None
    upsert = ('INSERT INTO source_config (name,kind,file_path,dataset_id,etl_batch_date) VALUES (%s,%s,%s,%s,%s) '
              'ON DUPLICATE KEY UPDATE kind=%s,file_path=%s,dataset_id=%s,etl_batch_date=%s')
    try:
        extract.initialize_schema()
        connection = sources.connect_database(test_only=True)
        with connection.cursor() as cursor:
            cursor.execute('SELECT name,kind,file_path,dataset_id,etl_batch_date FROM source_config ORDER BY name')
            rows = list(cursor.fetchall())
            assert len({row[0] for row in rows}) == len(rows) and {row[0] for row in rows} <= set(s.SOURCES)
            original = {row[0]: tuple(row) for row in rows}
        clean.initialize_schema()
        with connection.cursor() as cursor:
            for table in (*[spec['table'] for spec in sources.SOURCES.values()], *TABLES, 'dwd_clean_audit'):
                cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s', (dataset,))
                assert cursor.fetchone()[0] == 0
        connection.rollback()
        owned = True
        directory.mkdir(parents=True)
        for name, value in selected.items():
            with connection.cursor() as cursor:
                attempted.append(name)
                row = (name, *[value[k] for k in sources.CONFIG_FIELDS])
                cursor.execute(upsert, (*row, *row[1:]))
        connection.commit()
        yield dataset, batch, selected, report_path
    finally:
        if connection is not None:
            try:
                connection.rollback()
                if owned:
                    with connection.cursor() as cursor:
                        for table in (*[spec['table'] for spec in sources.SOURCES.values()], *TABLES, 'dwd_clean_audit'):
                            cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id=%s', (dataset,))
                        for name in attempted:
                            cursor.execute('SELECT kind,file_path,dataset_id,etl_batch_date FROM source_config WHERE name=%s FOR UPDATE', (name,))
                            current = cursor.fetchone()
                            expected = selected[name]
                            if current is None or tuple(map(str,current)) != tuple(expected[k] for k in sources.CONFIG_FIELDS):
                                continue  # Concurrent replacement belongs to its owner.
                            if name in original:
                                row = original[name]
                                cursor.execute(upsert, (*row,*row[1:]))
                            else:
                                cursor.execute('DELETE FROM source_config WHERE name=%s AND dataset_id=%s', (name,dataset))
                    connection.commit()
            finally:
                connection.close()
        if directory.exists():
            shutil.rmtree(directory)
        if report_path.exists():
            value = json.loads(report_path.read_text())
            if value.get('dataset_id') == dataset:
                report_path.unlink()


@pytest.mark.parametrize('invalid_amount', [False, True], ids=['clean100', 'hardfail97'])
def test_mysql_formal_cli_quality_self_contained(mysql_pipeline, invalid_amount):
    import subprocess
    import sys
    dataset, batch, selected, report_path = mysql_pipeline
    for name, info in selected.items():
        path = Path(info['file_path'])
        assert not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]['columns'])
            writer.writeheader()
            writer.writerows(sim_csv_rows(name, invalid_amount=invalid_amount))
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    backend = Path(__file__).resolve().parents[1]
    env['PYTHONPATH'] = str(backend)
    previous_logs = set(s.LOG_ROOT.glob('test/etl_test_*_run_all_*.log'))
    completed = subprocess.run([sys.executable, '-m', 'etl.jobs.run_all', '--env', 'test', '--through', 'quality'],
                               cwd=backend, env=env, text=True, capture_output=True, timeout=90)
    assert completed.returncode == (1 if invalid_amount else 0), 'formal CLI unexpected exit; raw output withheld'
    assert not completed.stderr, 'formal CLI stderr must stay empty'
    output = json.loads(completed.stdout)
    assert output['code'] == ('DQ_GATE_FAILED' if invalid_amount else 'ETL_OK')
    result = json.loads(report_path.read_text())
    assert result['dataset_id'] == dataset and result['batch_date'] == batch
    assert result['privacy_key_id'] == os.environ['DWD_PRIVACY_KEY_ID']
    assert result['planned_rules'] == result['executed_rules'] == result['evaluated_rules'] == 34
    assert result['coverage'] == 100 and result['score_denominator_half_points'] == 68
    assert result['gate_status'] == ('FAIL' if invalid_amount else 'PASS')
    assert result['score_numerator_half_points'] == (66 if invalid_amount else 68)
    assert result['rule_status_counts'] == {'PASS':33 if invalid_amount else 34, 'WARN':0, 'FAIL':1 if invalid_amount else 0, 'NOT_EVALUATED':0}
    assert result['score'] == pytest.approx(3300/34 if invalid_amount else 100)
    assert all(result['tables'][t]['accepted_rows'] == 3 for t in TABLES)
    if invalid_amount:
        rule = next(r for r in result['rules'] if r['rule_id'] == 'charge.VALUE_RANGE')
        assert rule['hard_block'] is True
    else:
        assert output['tasks'] == dict(extract_ods=dict.fromkeys(s.SOURCES,3), clean_dwd=dict.fromkeys(TABLES,3), quality_check=dict.fromkeys(TABLES,3))
    connection = sources.connect_database(test_only=True)
    try:
        with connection.cursor() as cursor:
            for table in (*[spec['table'] for spec in sources.SOURCES.values()], *TABLES):
                cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s', (dataset,))
                assert cursor.fetchone()[0] == 3
            cursor.execute('SELECT table_name,clean_run_id,batch_date,accepted_rows FROM dwd_clean_audit WHERE dataset_id=%s', (dataset,))
            audits = cursor.fetchall()
            assert len(audits) == 5
            assert {r[0] for r in audits} == set(TABLES)
            assert all(r[1] == result['clean_run_id'] and str(r[2]) == batch and r[3] == 3 for r in audits)
            cursor.execute('SELECT amount FROM dwd_charge_detail WHERE dataset_id=%s AND charge_id=%s', (dataset,'SIM_charge_id_1'))
            amount = cursor.fetchone()[0]
            assert (amount is None) if invalid_amount else (str(amount) == '10.25')
    finally:
        connection.rollback(); connection.close()
    paths = list(set(s.LOG_ROOT.glob('test/etl_test_*_run_all_*.log')) - previous_logs)
    assert len(paths) == 1
    records = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
    # Only inspect logs created by this child, never other sessions.
    candidates = [r for r in records if r['name'] == 'quality_check' and r['status'] in {'FAILED','SUCCESS'}]
    assert len(candidates) == 1
    assert candidates[0]['status'] == ('FAILED' if invalid_amount else 'SUCCESS')
    assert candidates[0]['code'] == ('DQ_GATE_FAILED' if invalid_amount else 'ETL_OK')
    assert not any('SIM_PRIVATE' in path.read_text() for path in paths)


@pytest.mark.parametrize('case', ['pass','fail','blocked','write_failure'])
def test_real_quality_generator_in_cli_never_falls_back(case, monkeypatch, tmp_path, capsys):
    """Exercise real report generation/writer through CLI, with in-memory SIM snapshot."""
    config = clean._config()
    run_id = cleaned()['clean_run_id']
    snapshot = dict(schema_version='node07-snapshot-v1', dataset_id='I7_SIM', clean_run_id=run_id,
                    batch_date=BATCH, rules_version=config['rules_version'], mapping_version=config['mapping_version'],
                    privacy_key_id='SIM-unit-v1', tables={})
    for table, spec in clean.TABLES.items():
        raw = sim_csv_rows(spec['source'], invalid_amount=case == 'fail')
        if case == 'blocked':
            raw = raw[:1]
        rows = [dict(row, dataset_id='I7_SIM', source_row_number=i, etl_batch_date=clean._day(BATCH)) for i,row in enumerate(raw,1)]
        output, audit = clean._prepare(table, rows, BATCH, 'I7_SIM', base64.b64decode(SIM_KEY), 'SIM-unit-v1', run_id, config)
        for row in output:
            row['etl_load_time'] = datetime(2025,2,1)
        audit['updated_at'] = datetime(2025,2,1)
        snapshot['tables'][table] = dict(source=dict(table=sources.SOURCES[spec['source']]['table'], columns=list(sources.SOURCES[spec['source']]['columns'] + clean.META), rows=rows),
                                       dwd=dict(table=table, columns=spec['columns'], rows=output), audit=audit)
    result = cleaned()
    for table in TABLES:
        result['tables'][table]['accepted_rows'] = len(snapshot['tables'][table]['dwd']['rows'])
    monkeypatch.setattr(clean,'run_clean_all',lambda *a,**kw:result)
    # Restore actual implementation hidden by autouse mock.
    monkeypatch.setattr(quality,'generate_quality_report', REAL_GENERATE)
    monkeypatch.setattr(quality,'_load_snapshot',lambda *a,**kw:snapshot)
    monkeypatch.setattr(quality,'_LOG_ROOT',tmp_path/'quality-logs')
    path = tmp_path/'quality-logs'/'test'/'quality_report_20250131.json'
    if case == 'write_failure':
        def no_write(*args,**kwargs):
            raise sources.OdsError('DQ_REPORT_WRITE_FAILED')
        monkeypatch.setattr(quality,'_write_report',no_write)
    assert cli.main(['--env','test','--through','quality']) == (0 if case == 'pass' else 1)
    code = json.loads(capsys.readouterr().out)['code']
    assert code == {'pass':'ETL_OK','fail':'DQ_GATE_FAILED','blocked':'DQ_INSUFFICIENT_EVIDENCE','write_failure':'DQ_REPORT_WRITE_FAILED'}[case]
    if case == 'write_failure':
        assert not path.exists()
        assert any(r['name'] == 'clean_dwd' and r['status'] == 'SUCCESS' for r in events())
    else:
        stored = json.loads(path.read_text())
        assert stored['gate_status'] == {'pass':'PASS','fail':'FAIL','blocked':'BLOCKED_INSUFFICIENT_EVIDENCE'}[case]


def test_unknown_freeze_error_is_new_permanent_boundary(monkeypatch, capsys):
    calls = []
    def broken():
        calls.append(1)
        raise RuntimeError('SIM_SECRET mysql://hidden')
    monkeypatch.setattr(extract, 'list_sources', broken)
    assert cli.main(['--env','test','--through','quality']) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['code'] == 'DWD_INTERNAL_ERROR'
    assert calls == [1] and 'SIM_SECRET' not in output.out + json.dumps(events())
    assert [r['name'] for r in events() if r['status'] == 'BLOCKED'] == ['clean_dwd','quality_check']


def test_shared_codes_exactly_cover_owner_codes():
    assert s._NODE07_CODES == clean.SAFE_CODES
    assert quality._ERRORS <= s._NODE07_CODES
    assert s._NODE07_RETRYABLE == {'DWD_TX_RETRYABLE','DQ_READ_RETRYABLE'}


@pytest.mark.parametrize('url', [None,'mysql://SIM:SIM@127.0.0.1:13306/hospital'])
def test_real_fixture_guard_before_any_connection(url, monkeypatch):
    if url is not None:
        monkeypatch.setenv('ODS_TEST_DATABASE_URL', url)
    fixture = mysql_pipeline.__wrapped__(monkeypatch)
    with pytest.raises(pytest.skip.Exception if url is None else sources.OdsError):
        next(fixture)


@pytest.mark.parametrize('stage', ['clean','quality'])
def test_new_retry_exhaustion_is_four_attempts(stage, monkeypatch, capsys):
    calls = []
    code = 'DWD_TX_RETRYABLE' if stage == 'clean' else 'DQ_READ_RETRYABLE'
    def bad(*args, **kwargs):
        calls.append(1); raise sources.OdsError(code)
    monkeypatch.setattr(clean if stage == 'clean' else quality, 'run_clean_all' if stage == 'clean' else 'generate_quality_report', bad)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert len(calls) == 4 and json.loads(capsys.readouterr().out)['code'] == code
    name = 'clean_dwd' if stage == 'clean' else 'quality_check'
    assert [r['attempt'] for r in events() if r['name'] == name and r['status'] == 'FAILED'] == [1,2,3,4]


@pytest.mark.parametrize('stage', ['clean','quality'])
def test_post_stage_registry_change_refused(stage, monkeypatch, capsys):
    def changed(*args, **kwargs):
        monkeypatch.setattr(extract, 'list_sources', lambda:[dict(name=n,**v) for n,v in infos('OTHER').items()])
        return cleaned() if stage == 'clean' else report()
    monkeypatch.setattr(clean if stage == 'clean' else quality, 'run_clean_all' if stage == 'clean' else 'generate_quality_report', changed)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert json.loads(capsys.readouterr().out)['code'] == ('DWD_SOURCE_MISMATCH' if stage == 'clean' else 'DQ_SNAPSHOT_MISMATCH')


@pytest.mark.parametrize('stage', ['clean','quality'])
def test_unknown_recheck_error_uses_stage_internal_code(stage, monkeypatch, capsys):
    calls = []
    def register():
        calls.append(1)
        # Freeze, clean before, clean after, quality before.
        if len(calls) == (2 if stage == 'clean' else 4):
            raise RuntimeError('SIM_SECRET')
        return [dict(name=n,**v) for n,v in infos().items()]
    monkeypatch.setattr(extract, 'list_sources', register)
    assert cli.main(['--env','test','--through','quality']) == 1
    assert json.loads(capsys.readouterr().out)['code'] == ('DWD_INTERNAL_ERROR' if stage == 'clean' else 'DQ_INTERNAL_ERROR')
