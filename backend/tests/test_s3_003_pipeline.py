"""I8 explicit CLI chain with isolated unit and coordinator-only MySQL checks."""
import csv
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from etl.config import source_config as sources
from etl.jobs import build_dims, build_dwd as build, quality_check as quality, run_all as cli
from etl.scheduler import scheduler as scheduling
from test_s2_003_pipeline import BATCH, cleaned, events, isolated, mysql_pipeline, report, sim_csv_rows


EXPECTED = {build.VISIT: 6, 'dwd_charge_detail': 3}


def test_dwd_cli_uses_one_run_and_passes_explicit_range(monkeypatch, capsys):
    seen = []
    def write(start, end, **kwargs):
        context = scheduling._CURRENT.get()
        assert context.env == 'test'
        assert kwargs['dataset_id'] == 'I7_SIM' and kwargs['batch_date'] == BATCH
        seen.append((start, end, context.run_id))
        return dict(batch_date=BATCH, dataset_id='I7_SIM', build_run_id='SIM_BUILD_ID',
                    clean_run_id=cleaned()['clean_run_id'], rows=EXPECTED)
    monkeypatch.setattr(build, '_run_build_locked', write)
    monkeypatch.setattr(build, '_read_quality_report',
                        lambda *a: (dict(mapping_version='SIM', privacy_key_id='SIM-unit-v1'), 'a'*64))
    monkeypatch.setattr(build, '_publish_report', lambda result: None)
    monkeypatch.setattr(build, '_verify_current_locked', lambda *a: dict(build_run_id='SIM_BUILD_ID'))
    assert cli.main(['--env', 'test', '--through', 'dwd',
                     '--start', '2025-01-01', '--end', '2025-01-31']) == 0
    value = json.loads(capsys.readouterr().out)
    assert list(value['tasks']) == ['extract_ods', 'clean_dwd', 'quality_check', 'build_dwd']
    assert value['tasks']['build_dwd'] == EXPECTED
    assert len(seen) == 1 and seen[0][:2] == ('2025-01-01', '2025-01-31')
    assert {r['run_id'] for r in events()} == {seen[0][2]}


@pytest.mark.parametrize('code,attempts', [
    ('N8_TX_RETRYABLE', 4), ('N8_REPORT_WRITE_FAILED', 1),
    ('N8_COMMIT_UNKNOWN', 1), ('N8_RANGE_NOT_FULL', 1),
])
def test_dwd_retry_only_for_confirmed_rollback(code, attempts, monkeypatch, capsys):
    seen = []
    def write(*args, **kwargs):
        context = scheduling._CURRENT.get()
        seen.append((context.run_id, context.pipeline['clean_run_id']))
        raise sources.OdsError(code)
    monkeypatch.setattr(build, '_run_build_locked', write)
    assert cli.main(['--env', 'test', '--through', 'dwd']) == 1
    assert json.loads(capsys.readouterr().out)['code'] == code
    assert len(seen) == attempts and len(set(seen)) == 1


def test_quality_failure_blocks_width_stage(monkeypatch, capsys):
    monkeypatch.setattr(quality, 'generate_quality_report', lambda *a, **kw: report(gate='FAIL'))
    monkeypatch.setattr(build, '_run_build_locked', lambda *a, **kw: pytest.fail('unexpected N8'))
    assert cli.main(['--env', 'test', '--through', 'dwd']) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'DQ_GATE_FAILED'
    assert any(r['name'] == 'build_dwd' and r['status'] == 'BLOCKED' for r in events())


def test_dwd_schedule_is_rejected_before_registration(capsys):
    assert cli.main(['--env', 'test', '--through', 'dwd', '--schedule']) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'ETL_ARGUMENTS_INVALID'
    assert not scheduling._TASKS


def test_new_error_catalog_matches_build():
    assert scheduling._NODE08_CODES == build.SAFE_CODES


@pytest.mark.parametrize('invalid_amount', [False, True], ids=['clean', 'hard-fail'])
def test_mysql_dwd_cli_self_contained(mysql_pipeline, invalid_amount, monkeypatch):
    dataset, batch, selection, quality_path = mysql_pipeline
    path = quality_path.parent / build._report_name(batch)
    assert not path.exists() and not path.is_symlink()
    assert build_dims.initialize_dim_schema() and build.initialize_schema()
    tables = (*build_dims._SPECS, build.VISIT, build.AUDIT)
    connection = sources.connect_database(test_only=True)
    owned = False
    try:
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s', (dataset,))
                assert cursor.fetchone() == (0,)
        connection.rollback()
        owned = True
        directory = (sources.GENERATED_ROOT / dataset).resolve()
        for name, info in selection.items():
            source = Path(info['file_path'])
            assert source.resolve().is_relative_to(directory)
            source.parent.mkdir(parents=True, exist_ok=True)
            with source.open('x', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]['columns'])
                writer.writeheader()
                writer.writerows(sim_csv_rows(name, invalid_amount=invalid_amount))
        backend = Path(sources.PROJECT_ROOT) / 'backend'
        env = dict(os.environ, PYTHONPATH=str(backend), PYTHONDONTWRITEBYTECODE='1')
        existing_logs = set(scheduling.LOG_ROOT.glob('test/etl_test_*_run_all_*.log'))
        result = subprocess.run(
            [sys.executable, '-m', 'etl.jobs.run_all', '--env', 'test', '--through', 'dwd'],
            cwd=backend, env=env, capture_output=True, text=True, timeout=90, check=False,
        )
        assert result.returncode == int(invalid_amount), 'I8 CLI exit mismatch; raw output withheld'
        assert not result.stderr, 'I8 CLI stderr withheld'
        value = json.loads(result.stdout)
        assert value['code'] == ('DQ_GATE_FAILED' if invalid_amount else 'ETL_OK')
        q = json.loads(quality_path.read_text())
        assert (q['dataset_id'], q['batch_date'], q['executed_rules']) == (dataset, batch, 34)
        logs = set(scheduling.LOG_ROOT.glob('test/etl_test_*_run_all_*.log')) - existing_logs
        assert len(logs) == 1
        events = [json.loads(line) for line in logs.pop().read_text().splitlines()]
        build_events = [event['status'] for event in events if event['name'] == 'build_dwd']
        assert build_events == (['BLOCKED'] if invalid_amount else ['START', 'SUCCESS'])
        if invalid_amount:
            assert q['gate_status'] == 'FAIL' and not path.exists()
            with connection.cursor() as cursor:
                for table in tables:
                    cursor.execute(f'SELECT COUNT(*) FROM `{table}` WHERE dataset_id=%s', (dataset,))
                    assert cursor.fetchone() == (0,)
        else:
            assert q['gate_status'] == 'PASS' and value['tasks']['build_dwd'] == EXPECTED
            built = json.loads(path.read_text())
            assert built['clean_run_id'] == q['clean_run_id'] and built['rows'] == EXPECTED
            fresh = build.verify_current_build(dataset_id=dataset, batch_date=batch, env='test')
            assert fresh['gate_status'] == 'PASS' and fresh['build_run_id'] == built['build_run_id']
            with connection.cursor() as cursor:
                cursor.execute('SELECT visit_type, COUNT(*), SUM(source_total_fee) FROM dwd_visit_detail WHERE dataset_id=%s GROUP BY visit_type', (dataset,))
                assert dict((kind, (count, total)) for kind, count, total in cursor.fetchall()) == {
                    'OUTPATIENT': (3, Decimal('30.75')), 'INPATIENT': (3, Decimal('30.75')),
                }
                cursor.execute('SELECT COUNT(*), SUM(amount) FROM dwd_charge_detail WHERE dataset_id=%s', (dataset,))
                assert cursor.fetchone() == (3, Decimal('30.75'))
                cursor.execute(
                    "SELECT visit_date, dept_code, COUNT(*) FROM dwd_visit_detail "
                    "WHERE dataset_id=%s AND visit_type='OUTPATIENT' GROUP BY visit_date, dept_code",
                    (dataset,),
                )
                reference = cursor.fetchall()
            from fastapi.testclient import TestClient
            from app.main import create_app

            monkeypatch.setenv("OUTPATIENT_PREVIEW_ENABLED", "1")
            monkeypatch.setenv("OUTPATIENT_PREVIEW_ENV", "test")
            monkeypatch.setenv("OUTPATIENT_PREVIEW_DATASET_ID", dataset)
            monkeypatch.setenv("OUTPATIENT_PREVIEW_BATCH_DATE", batch)
            monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", built["build_run_id"])
            with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
                result = client.get("/api/dashboard/outpatient-preview")
                assert result.status_code == 200
                payload = result.json()["data"]
                assert payload["meta"]["build_run_id"] == built["build_run_id"]
                assert payload["outpatient_visits"] == sum(n for _, _, n in reference)
                by_day, by_dept = {}, {}
                for day, dept, n in reference:
                    by_day[day.isoformat()] = by_day.get(day.isoformat(), 0) + n
                    by_dept[dept] = by_dept.get(dept, 0) + n
                assert {r["date"]: r["visits"] for r in payload["daily_trend"]} == by_day
                assert {r["dept_code"]: r["visits"] for r in payload["department_ranking"]} == by_dept
    finally:
        try:
            connection.rollback()
            if owned:
                with connection.cursor() as cursor:
                    for table in tables:
                        cursor.execute(f'DELETE FROM `{table}` WHERE dataset_id=%s', (dataset,))
                connection.commit()
                if path.exists() and not path.is_symlink():
                    if json.loads(path.read_text()).get('dataset_id') == dataset:
                        path.unlink()
        finally:
            connection.close()


@pytest.mark.parametrize('key', ['clean_run_id', 'privacy_key_id', 'accepted_rows', 'source_infos'])
def test_locked_snapshot_identity_mismatch_refuses_before_dml(key, monkeypatch):
    from etl.jobs import clean_dwd as clean
    from types import SimpleNamespace

    info = {'kind': 'csv', 'file_path': '/sim/old.csv',
            'dataset_id': 'I7_SIM', 'etl_batch_date': BATCH}
    actual = dict(info)
    identity = {'clean_run_id': 'old', 'privacy_key_id': 'SIM-old',
                'accepted_rows': {'dwd_charge_detail': 3}, 'source_infos': {'sim': info}}
    snapshot = {'clean_run_id': 'old', 'privacy_key_id': 'SIM-old',
                'tables': {'dwd_charge_detail': {'audit': {'accepted_rows': 3}}}}
    if key == 'accepted_rows':
        snapshot['tables']['dwd_charge_detail']['audit']['accepted_rows'] = 4
    elif key == 'source_infos':
        actual['file_path'] = '/sim/changed.csv'
    else:
        snapshot[key] = 'changed'
    statements = []
    cursor = SimpleNamespace(execute=lambda sql: statements.append(sql))
    registry = (('sim', *(actual[k] for k in sources.CONFIG_FIELDS)),)
    monkeypatch.setattr(build, '_lock_inputs', lambda *a: ('I7_SIM', BATCH, registry))
    monkeypatch.setattr(build, '_verify_schema', lambda *a: None)
    monkeypatch.setattr(clean, 'load_quality_snapshot_from_cursor', lambda *a, **kw: snapshot)
    monkeypatch.setattr(build, '_read_quality_report', lambda *a: pytest.fail('ran past identity gate'))
    with pytest.raises(sources.OdsError, match='^N8_STALE$'):
        build._build_work(cursor, None, None, dataset_id='I7_SIM', batch_date=BATCH,
                          env='test', scope='ALL', pipeline=identity)
    assert statements == ['SET TRANSACTION ISOLATION LEVEL REPEATABLE READ', 'START TRANSACTION']


def test_publish_after_commit_never_retries_write(monkeypatch, capsys):
    writes = []

    def write(*args, **kwargs):
        writes.append(True)
        return dict(batch_date=BATCH, dataset_id='I7_SIM', build_run_id='SIM_BUILD_ID')

    def publish(_):
        raise sources.OdsError('N8_TX_RETRYABLE')

    monkeypatch.setattr(build, '_run_build_locked', write)
    monkeypatch.setattr(build, '_read_quality_report',
                        lambda *a: (dict(mapping_version='SIM', privacy_key_id='SIM-unit-v1'), 'a'*64))
    monkeypatch.setattr(build, '_publish_report', publish)
    assert cli.main(['--env', 'test', '--through', 'dwd']) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'N8_REPORT_WRITE_FAILED'
    assert len(writes) == 1
