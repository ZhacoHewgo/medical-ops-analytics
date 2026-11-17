import csv

import pytest


@pytest.fixture
def csv_file(tmp_path):
    def write(rows, columns=None):
        path = tmp_path / "synthetic.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=columns or ["visit_id", "visit_date", "dept_code", "private_note"]
            )
            writer.writeheader()
            writer.writerows(rows)
        return path

    return write


def row(visit_id="v1", visit_date="2025-01-01", dept_code="B", private_note="SYNTHETIC_SECRET"):
    return dict(visit_id=visit_id, visit_date=visit_date, dept_code=dept_code, private_note=private_note)


@pytest.fixture
def aggregate_path(csv_file, tmp_path):
    from etl.jobs.build_outpatient import build_outpatient

    source = csv_file([
        row("v1"), row("v2"), row("v3", "2025-01-03", "A"), row("v4", "2025-01-03", ""),
    ])
    target = tmp_path / "outpatient.json"
    build_outpatient(source, target)
    return target


@pytest.fixture
def client(aggregate_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(aggregate_path))
    with TestClient(app) as test_client:
        yield test_client
