import json

import pytest


URL = "/api/dashboard/overview"


def assert_error(response, status):
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "msg", "data"}
    assert type(body["code"]) is int and body["code"] != 0
    assert isinstance(body["msg"], str) and body["msg"]
    assert body["data"] is None
    assert "SYNTHETIC_SECRET" not in response.text


def test_health_and_default(client):
    assert client.get("/health").json()["status"] == "ok"
    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0 and body["msg"] == "ok"
    data = body["data"]
    assert set(data) == {"filters", "outpatient_visits", "daily_trend", "department_ranking", "meta"}
    assert data["filters"] == {"start_date": "2025-01-01", "end_date": "2025-01-03", "dept_code": None}
    assert data["outpatient_visits"] == 4
    assert data["daily_trend"] == [{"date": "2025-01-01", "visits": 2}, {"date": "2025-01-03", "visits": 2}]
    assert data["department_ranking"] == [
        {"dept_code": "B", "visits": 2}, {"dept_code": "A", "visits": 1}, {"dept_code": "__UNKNOWN__", "visits": 1},
    ]
    assert data["meta"]["quality_scope"] == "whole_source"
    assert data["meta"]["simulated"] is True
    assert "SYNTHETIC_SECRET" not in response.text


@pytest.mark.parametrize("params,expected", [
    ({"start_date": "2025-01-03", "end_date": "2025-01-03"}, 2),
    ({"dept_code": "B"}, 2), ({"dept_code": "__UNKNOWN__"}, 1),
    ({"start_date": "2025-01-03", "dept_code": "A"}, 1),
    ({"end_date": "2025-01-01"}, 2),
    ({"start_date": "2025-01-02", "end_date": "2025-01-02"}, 0),
    ({"start_date": "2030-01-01", "end_date": "2030-01-31"}, 0),
    ({"dept_code": "B", "start_date": "2025-01-03"}, 0),
])
def test_filters_conserve_totals_and_meta(client, params, expected):
    whole_meta = client.get(URL).json()["data"]["meta"]
    response = client.get(URL, params=params)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["outpatient_visits"] == expected
    assert sum(p["visits"] for p in data["daily_trend"]) == expected
    assert sum(p["visits"] for p in data["department_ranking"]) == expected
    assert data["meta"] == whole_meta
    if not expected:
        assert data["daily_trend"] == data["department_ranking"] == []


@pytest.mark.parametrize("params", [
    {"start_date": "2025-1-1"}, {"end_date": "2025-01-1"},
    {"start_date": "2025-02-29"}, {"end_date": "garbage"}, {"start_date": ""},
    {"start_date": " 2025-01-01"}, {"end_date": "2025-01-01T00:00:00"},
    {"start_date": "2025-01-04", "end_date": "2025-01-01"},
    {"dept_code": "missing"}, {"dept_code": ""},
])
def test_invalid_filters(client, params):
    assert_error(client.get(URL, params=params), 422)


def test_missing_or_unreadable_file(client, aggregate_path):
    aggregate_path.unlink()
    assert_error(client.get(URL), 503)
    aggregate_path.mkdir()
    assert_error(client.get(URL), 503)
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("contents", ["not JSON", "null", "[]", "{}", '{"meta": {}}', "\ud800"])
def test_invalid_file_returns_503(client, aggregate_path, contents):
    aggregate_path.write_bytes(contents.encode("utf-8", errors="surrogatepass"))
    assert_error(client.get(URL), 503)


@pytest.mark.parametrize("path,value", [
    (("counts",), None), (("counts",), {}), (("counts",), []),
    (("counts", 0), None), (("counts", 0, "visits"), True),
    (("counts", 0, "visits"), "2"), (("counts", 0, "visits"), 2.0),
    (("counts", 0, "visits"), 0), (("counts", 0, "visits"), -1),
    (("counts", 0, "visits"), 999), (("counts", 0, "date"), "2025-1-1"),
    (("counts", 0, "dept_code"), None), (("counts", 0, "dept_code"), " B "),
    (("meta",), []), (("meta", "schema_version"), "2.0"),
    (("meta", "simulated"), 1), (("meta", "simulated"), False),
    (("meta", "source_file"), "other.csv"), (("meta", "source_sha256"), "bad"),
    (("meta", "generated_at"), "2025-01-01T00:00:00"),
    (("meta", "available_start_date"), "2024-01-01"),
    (("meta", "available_end_date"), "2025-01-02"),
    (("meta", "department_codes"), ["A"]), (("meta", "department_codes"), "A"),
    (("meta", "quality_scope"), "filtered"), (("meta", "quality"), None),
    (("meta", "quality", "raw_rows"), 5),
    (("meta", "quality", "accepted_visits"), 5),
    (("meta", "quality", "unknown_department_visits"), 0),
    (("meta", "quality", "invalid_id_rows"), True),
    (("meta", "quality", "invalid_date_rows"), -1),
])
def test_corrupt_structure_types_and_conservation(client, aggregate_path, path, value):
    document = json.loads(aggregate_path.read_text())
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    aggregate_path.write_text(json.dumps(document))
    assert_error(client.get(URL), 503)


@pytest.mark.parametrize("mutation", ["missing_quality", "missing_row_key", "duplicate_bucket", "private_field"])
def test_missing_fields_duplicate_buckets_and_privacy(client, aggregate_path, mutation):
    document = json.loads(aggregate_path.read_text())
    if mutation == "missing_quality":
        del document["meta"]["quality"]["raw_rows"]
    elif mutation == "missing_row_key":
        del document["counts"][0]["date"]
    elif mutation == "duplicate_bucket":
        document["counts"].append(document["counts"][0])
        document["meta"]["quality"]["raw_rows"] += 2
        document["meta"]["quality"]["accepted_visits"] += 2
    else:
        document["meta"]["patient_name"] = "SYNTHETIC_SECRET"
    aggregate_path.write_text(json.dumps(document))
    assert_error(client.get(URL), 503)


def test_not_found_and_method_errors_have_same_envelope(client):
    assert_error(client.get("/not-an-endpoint"), 404)
    assert_error(client.post(URL), 405)


def test_file_is_reloaded_without_stale_fallback(client, aggregate_path):
    assert client.get(URL).status_code == 200
    aggregate_path.write_text("broken")
    assert_error(client.get(URL), 503)


def test_escaped_invalid_unicode_returns_503(client, aggregate_path):
    document = json.loads(aggregate_path.read_text())
    document["counts"][0]["dept_code"] = "\ud800"
    document["meta"]["department_codes"] = ["A", "__UNKNOWN__", "\ud800"]
    aggregate_path.write_text(json.dumps(document))
    assert_error(client.get(URL), 503)
