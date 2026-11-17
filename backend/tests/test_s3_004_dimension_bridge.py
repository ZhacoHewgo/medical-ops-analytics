"""C8 strict eight-field output bridge; real cases require coordinator injection."""
from copy import deepcopy
from datetime import date
import base64
import hashlib
import json
import os
from pathlib import Path
import socket

import pymysql
import pytest

from etl.config import source_config as sources
from etl.jobs import build_dims as dims
import test_s3_001_dimensions as a

CERTIFICATE_KEYS = ('dataset_id', 'template_version', 'date_min', 'date_max',
                    'required_date_start', 'required_date_end', 'row_counts', 'dim_sha256')
RESULT_KEYS = {'template_version', 'required_date_start', 'required_date_end', 'row_counts', 'dim_sha256'}
ENV_KEYS = ('ODS_DATABASE_URL', 'ODS_TEST_DATABASE_URL', 'DWD_PRIVACY_KEY', 'DWD_PRIVACY_KEY_ID',
            'C7_RUN_MYSQL_TESTS', 'S6_INTEGRATION', 'ODS_S6_INTEGRATION')


def block_unit_connections(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('C8_BRIDGE_PURE_NETWORK_BLOCKED')
    for owner, name in ((pymysql, 'connect'), (pymysql.connections.Connection, 'connect'),
                        (socket, 'create_connection'), (socket.socket, 'connect'),
                        (socket.socket, 'connect_ex')):
        monkeypatch.setattr(owner, name, denied)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, request):
    if request.node.name.startswith('test_mysql_'):
        if os.environ.get('ODS_TEST_DATABASE_URL'):
            sources.database_options(test_only=True)
            if os.environ.get('ODS_DATABASE_URL') != os.environ['ODS_TEST_DATABASE_URL']:
                pytest.fail('coordinator test URLs must match; values withheld')
            return
        block_unit_connections(monkeypatch)
        return
    block_unit_connections(monkeypatch)
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('DWD_PRIVACY_KEY', base64.b64encode(b'SIM_ONLY_BRIDGE'.ljust(32, b'x')).decode())
    monkeypatch.setenv('DWD_PRIVACY_KEY_ID', 'SIM-bridge-only')


def template(dataset='BRIDGE_SIM', low='2024-02-29', high='2024-02-29'):
    snapshot = a.snapshot()
    snapshot['dataset_id'] = dataset
    snapshot['dates'] = {key: [] for key in dims.DATE_COLUMNS}
    snapshot['dates']['his_outpatient'] = [low, high]
    return dims.dimension_rows(snapshot)


def digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def certificate(result):
    return {**{key: deepcopy(result[key]) for key in CERTIFICATE_KEYS if key != 'dim_sha256'},
            'dim_sha256': digest(result['rows'])}


class MemoryCursor:
    """Caller-owned synthetic state; transaction operations are forbidden here."""
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.writes = []
        self.fault_table = None

    def execute(self, sql, params):
        assert sql.startswith('DELETE FROM `dim_'), 'unexpected SQL/control operation'
        table = sql.split('`')[1]
        self.writes.append(('DELETE', table))
        self.rows[table] = []

    def executemany(self, sql, params):
        assert sql.startswith('INSERT INTO `dim_')
        table = sql.split('`')[1]
        self.writes.append(('INSERT', table))
        if table == self.fault_table:
            raise RuntimeError('SIM_PRIVATE_WRITE_FAILURE')
        columns = [name for name, _, _ in dims._columns(table)]
        self.rows[table] = [dict(zip(columns, values)) for values in params]

    def __getattr__(self, name):
        if name in ('begin', 'commit', 'rollback', 'close'):
            raise AssertionError('helper must not control caller transaction')
        raise AttributeError(name)


def memory(monkeypatch, rows):
    cursor = MemoryCursor(rows)
    monkeypatch.setattr(dims, '_verify', lambda actual: None)
    def read(actual, dataset):
        assert actual is cursor
        return deepcopy(cursor.rows)
    monkeypatch.setattr(dims, '_read_rows', read)
    return cursor


def test_certified_old_new_new2_range_changes_and_immutable_inputs(monkeypatch):
    old = template()
    current = memory(monkeypatch, old['rows'])
    cert = certificate(old)
    original = deepcopy(cert)
    result = dims.prepare_dimensions(current, 'BRIDGE_SIM', '2023-01-01', '2026-01-01', previous_certificate=cert)
    assert set(result) == RESULT_KEYS and cert == original and old == template()
    expected = template(low='2023-01-01', high='2026-01-01')
    assert current.rows == expected['rows'] and result['dim_sha256'] == digest(current.rows)
    next_cert = dict(result, dataset_id='BRIDGE_SIM', date_min='2023-01-01', date_max='2026-01-01')
    result2 = dims.prepare_dimensions(current, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', previous_certificate=next_cert)
    assert current.rows == old['rows'] and result2['dim_sha256'] == cert['dim_sha256']
    assert len(current.writes) == 16


def test_equal_desired_valid_certificate_is_zero_dml(monkeypatch):
    old = template()
    cursor = memory(monkeypatch, old['rows'])
    cert = certificate(old)
    before = deepcopy(cert)
    result = dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', previous_certificate=cert)
    assert set(result) == RESULT_KEYS and result['dim_sha256'] == digest(old['rows'])
    assert cursor.writes == [] and cert == before and cursor.rows == old['rows']


@pytest.mark.parametrize('missing', CERTIFICATE_KEYS)
def test_missing_certificate_field_rejected_before_writes(monkeypatch, missing):
    old = template()
    cert = certificate(old)
    del cert[missing]
    cursor = memory(monkeypatch, old['rows'])
    with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', previous_certificate=cert)
    assert cursor.writes == [] and cursor.rows == old['rows']


@pytest.mark.parametrize('value', [{}, False, 0, '', [], (), set()])
def test_falsey_certificates_never_mean_none(monkeypatch, value):
    old = template()
    cursor = memory(monkeypatch, old['rows'])
    with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', previous_certificate=value)
    assert cursor.writes == []


class OtherDict(dict):
    pass


@pytest.mark.parametrize('field,value', [
    ('extra', 1), ('dataset_id', 'OTHER'), ('dataset_id', False),
    ('template_version', 'unknown'), ('template_version', None),
    ('date_min', '2024-02-30'), ('date_min', date(2024, 2, 29)),
    ('date_min', '2024-03-01'), ('date_min', '1000-01-01'),
    ('date_max', '9999-01-01'), ('date_max', ''), ('date_max', None),
    ('required_date_start', '2023-03-01'), ('required_date_end', '2025-03-01'),
    ('required_date_start', 0), ('dim_sha256', 'A' * 64), ('dim_sha256', '0' * 64),
    ('dim_sha256', ''), ('dim_sha256', None),
    ('row_counts', {}), ('row_counts', []), ('row_counts', False),
])
def test_certificate_shape_types_and_binding_even_when_desired_equal(monkeypatch, field, value):
    old = template()
    cert = certificate(old)
    cert[field] = value
    before = deepcopy(cert)
    cursor = memory(monkeypatch, old['rows'])
    with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', previous_certificate=cert)
    assert cursor.writes == [] and cursor.rows == old['rows'] and cert == before


@pytest.mark.parametrize('change', ['bool', 'float', 'negative', 'wrong', 'extra', 'missing', 'subclass', 'outer_subclass'])
def test_nested_counts_and_exact_dict_types(monkeypatch, change):
    old = template()
    cert = certificate(old)
    if change == 'outer_subclass': cert = OtherDict(cert)
    elif change == 'subclass': cert['row_counts'] = OtherDict(cert['row_counts'])
    elif change == 'extra': cert['row_counts']['unknown'] = 0
    elif change == 'missing': cert['row_counts'].pop('dim_date')
    else: cert['row_counts']['dim_department'] = {'bool': True, 'float': 4.0, 'negative': -1, 'wrong': 3}[change]
    cursor = memory(monkeypatch, old['rows'])
    with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', previous_certificate=cert)
    assert cursor.writes == []


@pytest.mark.parametrize('damage', ['trim_start', 'trim_end', 'single_day', 'all_empty', 'partial_empty', 'campus', 'icd', 'holiday', 'code', 'mapping'])
def test_current_old_output_damage_rejected_before_writes(monkeypatch, damage):
    old = template()
    rows = deepcopy(old['rows'])
    if damage == 'trim_start': rows['dim_date'].pop(0)
    elif damage == 'trim_end': rows['dim_date'].pop()
    elif damage == 'single_day': rows['dim_date'] = rows['dim_date'][:1]
    elif damage == 'all_empty': rows = {table: [] for table in dims._SPECS}
    elif damage == 'partial_empty': rows['dim_disease'] = []
    elif damage == 'campus': rows['dim_department'][0]['campus_code'] = 'SIM_EXTERNAL'
    elif damage == 'icd': rows['dim_disease'][0]['icd_code'] = 'SIM_EXTERNAL'
    elif damage == 'holiday': rows['dim_date'][0]['is_holiday'] = 1
    elif damage == 'code': rows['dim_fee_type'].pop()
    else: rows['dim_date'][0]['mapping_version'] = 'unknown'
    cursor = memory(monkeypatch, rows)
    with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2023-01-01', '2026-01-01', previous_certificate=certificate(old))
    assert cursor.rows == rows and cursor.writes == []


@pytest.mark.parametrize('explicit_none', [False, True])
def test_none_and_old_four_argument_behavior(monkeypatch, explicit_none):
    old = template()
    cursor = memory(monkeypatch, {table: [] for table in dims._SPECS})
    kwargs = {'previous_certificate': None} if explicit_none else {}
    dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', **kwargs)
    assert cursor.rows == old['rows']
    cursor.writes.clear()
    dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', **kwargs)
    assert cursor.writes == []
    with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2023-01-01', '2026-01-01', **kwargs)
    assert cursor.writes == []


def test_missing_table_strict_path_is_safe_dimension_failure(monkeypatch):
    old = template()
    cursor = memory(monkeypatch, old['rows'])
    def missing(actual):
        raise sources.OdsError('N8_SCHEMA_INVALID')
    monkeypatch.setattr(dims, '_verify', missing)
    with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2024-02-29', '2024-02-29', previous_certificate=certificate(old))
    assert cursor.writes == []


def test_helper_does_not_read_files_environment_lock_or_control_transaction(monkeypatch):
    import builtins
    import fcntl
    old = template()
    cursor = memory(monkeypatch, old['rows'])
    cert = certificate(old)
    def forbidden(*args, **kwargs):
        raise AssertionError('C8_BRIDGE_FORBIDDEN_IO')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        patch.setattr(Path, 'read_text', forbidden)
        patch.setattr(Path, 'read_bytes', forbidden)
        patch.setattr(os, 'getenv', forbidden)
        patch.setattr(type(os.environ), '__getitem__', forbidden)
        patch.setattr(type(os.environ), 'get', forbidden)
        patch.setattr(fcntl, 'flock', forbidden)
        patch.setattr(dims.clean, '_connect', forbidden)
        patch.setattr(dims, '_read_report', forbidden)
        result = dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2023-01-01', '2026-01-01', previous_certificate=cert)
    assert set(result) == RESULT_KEYS
    assert all(operation in ('DELETE', 'INSERT') for operation, _ in cursor.writes)


def test_third_table_write_failure_leaves_rollback_to_caller(monkeypatch):
    old = template()
    cursor = memory(monkeypatch, old['rows'])
    cursor.fault_table = 'dim_disease'
    with pytest.raises(sources.OdsError) as error:
        dims.prepare_dimensions(cursor, 'BRIDGE_SIM', '2023-01-01', '2026-01-01', previous_certificate=certificate(old))
    assert error.value.code in dims.SAFE_CODES and str(error.value) == error.value.code
    assert 'SIM_PRIVATE' not in str(error.value)
    assert cursor.writes[-1] == ('INSERT', 'dim_disease')
    # Pure mock leaves partial writes pending; only the caller may roll back.
    assert cursor.rows != old['rows']


def test_network_guard_has_no_socket_contact(monkeypatch):
    contacts = []
    def contact(*args, **kwargs):
        contacts.append(1)
        raise AssertionError('UNGUARDED_CONTACT')
    for owner, name in ((pymysql, 'connect'), (pymysql.connections.Connection, 'connect'),
                        (socket, 'create_connection'), (socket.socket, 'connect'), (socket.socket, 'connect_ex')):
        monkeypatch.setattr(owner, name, contact)
    block_unit_connections(monkeypatch)
    connection = pymysql.connections.Connection(defer_connect=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        actions = [lambda: pymysql.connect(host='127.0.0.1', user='SIM', password='SIM'),
                   connection.connect, lambda: socket.create_connection(('127.0.0.1', 13306)),
                   lambda: tcp.connect(('127.0.0.1', 13306)), lambda: tcp.connect_ex(('127.0.0.1', 13306))]
        for action in actions:
            with pytest.raises(AssertionError, match='^C8_BRIDGE_PURE_NETWORK_BLOCKED$'):
                action()
    assert contacts == []



@pytest.fixture
def mysql_bridge(monkeypatch):
    if not os.environ.get('ODS_TEST_DATABASE_URL'):
        pytest.skip('coordinator hospital_test credentials absent; real bridge NOT RUN')
    assert sources.database_options(test_only=True)['database'] == 'hospital_test'
    if os.environ.get('ODS_DATABASE_URL') != os.environ['ODS_TEST_DATABASE_URL']:
        pytest.fail('coordinator test URLs must match; values withheld')
    dims.clean._privacy()  # Coordinator-injected key only, never a SIM fallback.
    generator = a.mysql_dims.__wrapped__(monkeypatch)
    dataset, batch, other, path = next(generator)
    try:
        report = dims.refresh_dimensions(dataset_id=dataset, batch_date=batch, env='test')
        assert report['engineering_status'] == 'DIM_READY'
        yield dataset, batch, other, path, report
    finally:
        generator.close()


class ObservedCursor:
    def __init__(self, cursor, fail_third=False):
        self.cursor = cursor
        self.writes = []
        self.fail_third = fail_third

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def execute(self, sql, params=None):
        if sql.startswith(('DELETE ', 'INSERT ')):
            self.writes.append(('execute', sql.split('`')[1]))
        return self.cursor.execute(sql, params)

    def executemany(self, sql, params):
        table = sql.split('`')[1]
        self.writes.append(('insert', table))
        if self.fail_third and table == 'dim_disease':
            raise RuntimeError('SIM_PRIVATE_THIRD_DIM_WRITE')
        return self.cursor.executemany(sql, params)


def test_mysql_bridge_report_projection_expand_then_shrink_caller_commits(mysql_bridge):
    dataset, batch, other, path, report = mysql_bridge
    original = a.persisted(dataset)
    sentinel = a.persisted(other)
    report_bytes = path.read_bytes()
    projection = {key: deepcopy(report[key]) for key in CERTIFICATE_KEYS}
    connection = sources.connect_database(test_only=True)
    try:
        assert connection.get_autocommit() is False
        connection.begin()
        with connection.cursor() as cursor:
            dims.clean._registry(cursor, dataset, lock=True)
            result = dims.prepare_dimensions(cursor, dataset, '2024-02-29', '2027-01-01', previous_certificate=projection)
        connection.commit()
        expanded = a.persisted(dataset)  # Distinct connection after caller commit.
        assert expanded == template(dataset, '2024-02-29', '2027-01-01')['rows']
        assert result['dim_sha256'] == digest(expanded)
        assert a.persisted(other) == sentinel and path.read_bytes() == report_bytes
        projection2 = dict(result, dataset_id=dataset, date_min='2024-02-29', date_max='2027-01-01')
        connection.begin()
        with connection.cursor() as cursor:
            dims.clean._registry(cursor, dataset, lock=True)
            result2 = dims.prepare_dimensions(cursor, dataset, report['date_min'], report['date_max'], previous_certificate=projection2)
        connection.commit()
        assert a.persisted(dataset) == original
        assert result2['dim_sha256'] == report['dim_sha256']
        assert a.persisted(other) == sentinel and path.read_bytes() == report_bytes
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize('fault', ['hash', 'count', 'start', 'trim_start', 'trim_end', 'single_day'])
def test_mysql_bridge_bad_certificate_or_boundary_zero_dml_second_connection(mysql_bridge, fault):
    dataset, batch, other, path, report = mysql_bridge
    sentinel = a.persisted(other)
    report_bytes = path.read_bytes()
    projection = {key: deepcopy(report[key]) for key in CERTIFICATE_KEYS}
    connection = sources.connect_database(test_only=True)
    try:
        assert connection.get_autocommit() is False
        if fault in ('trim_start', 'trim_end', 'single_day'):
            rows = a.persisted(dataset)['dim_date']
            connection.begin()
            with connection.cursor() as cursor:
                dims.clean._registry(cursor, dataset, lock=True)
                if fault == 'single_day':
                    cursor.execute('DELETE FROM dim_date WHERE dataset_id=%s AND calendar_date<>%s',
                                   (dataset, rows[1]['calendar_date']))
                else:
                    target = rows[0 if fault == 'trim_start' else -1]['calendar_date']
                    cursor.execute('DELETE FROM dim_date WHERE dataset_id=%s AND calendar_date=%s', (dataset, target))
            connection.commit()
        elif fault == 'hash': projection['dim_sha256'] = '0' * 64
        elif fault == 'count': projection['row_counts']['dim_date'] -= 1
        else: projection['required_date_start'] = '2024-01-02'
        before = a.persisted(dataset)
        connection.begin()
        with connection.cursor() as raw:
            dims.clean._registry(raw, dataset, lock=True)
            observed = ObservedCursor(raw)
            with pytest.raises(sources.OdsError, match='^N8_DIMENSION_INVALID$'):
                dims.prepare_dimensions(observed, dataset, report['date_min'], report['date_max'], previous_certificate=projection)
            assert observed.writes == []
        connection.rollback()
        assert a.persisted(dataset) == before and a.persisted(other) == sentinel
        assert path.read_bytes() == report_bytes
    finally:
        connection.rollback()
        connection.close()


def test_mysql_bridge_third_write_caller_rollback_preserves_all_tables(mysql_bridge):
    dataset, batch, other, path, report = mysql_bridge
    before = a.persisted(dataset)
    sentinel = a.persisted(other)
    report_bytes = path.read_bytes()
    projection = {key: deepcopy(report[key]) for key in CERTIFICATE_KEYS}
    connection = sources.connect_database(test_only=True)
    try:
        assert connection.get_autocommit() is False
        connection.begin()
        with connection.cursor() as raw:
            dims.clean._registry(raw, dataset, lock=True)
            observed = ObservedCursor(raw, fail_third=True)
            with pytest.raises(sources.OdsError, match='^N8_INTERNAL_ERROR$'):
                dims.prepare_dimensions(observed, dataset, '2024-02-29', '2027-01-01', previous_certificate=projection)
            assert [table for kind, table in observed.writes if kind == 'insert'] == ['dim_department', 'dim_date', 'dim_disease']
        connection.rollback()  # Explicit caller responsibility, never helper fallback.
        assert a.persisted(dataset) == before and a.persisted(other) == sentinel
        assert path.read_bytes() == report_bytes
    finally:
        connection.rollback()
        connection.close()
