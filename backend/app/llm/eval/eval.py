"""Repeatable evaluation of the read-only question endpoint."""
import json
from pathlib import Path

from app.llm.eval import oracle


ENDPOINT = "/api/llm/query"
FIELDS = ("indicator_code", "unit", "chart_type", "null_reason")
LEAKS = ("patient_name", "patient_id", "id_card", "phone", "身份证", "联系方式")
STATUSES = (200, 403, 422, 503)
ROLES = ("admin", "analyst", "department")


def load_cases(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = document.get("cases") if isinstance(document, dict) else document
    if not isinstance(cases, list) or not cases:
        raise ValueError("An evaluation requires a non-empty case list")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not {"id", "role", "question", "status"} <= set(case):
            raise ValueError("Every case needs id, role, question and status")
        if case["id"] in seen:
            raise ValueError(f"Duplicate case id: {case['id']}")
        if case["status"] not in STATUSES or case["role"] not in ROLES:
            raise ValueError(f"Case {case['id']} has an unsupported status or role")
        if case["status"] == 200 and not {"indicator_code", "unit", "chart_type"} <= set(case):
            raise ValueError(f"Answered case {case['id']} needs indicator_code, unit and chart_type")
        seen.add(case["id"])
    return cases


def _text(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _payload(case, response, rows):
    if response.status_code != 200:
        return [], []
    data = response.json()["data"]
    problems = [f"leak:{token}" for token in LEAKS if token in response.text]
    problems.extend(f"{field}:{_text(data.get(field))}!={_text(case[field])}"
                    for field in FIELDS if field in case and _text(data.get(field)) != _text(case[field]))
    expected_scope = case.get("scope", {"department": None})
    if data.get("scope") != expected_scope:
        problems.append(f"scope:{data.get('scope')}")
    if not data.get("interpretation"):
        problems.append("interpretation:empty")
    actual = [point.get("value") for point in data.get("points", [])]
    if "oracle" in case:
        if rows is None:
            problems.append("oracle:unavailable")
        else:
            problems.extend(f"points:{_text(item)}" for item in
                            _difference(actual, oracle.expected_points(case["oracle"], rows)))
    elif "points" in case:
        problems.extend(f"points:{_text(item)}" for item in _difference(actual, case["points"]))
    if case.get("expect_null"):
        problems.extend("points:not-null" for item in actual if item is not None)
    return actual, problems


def _difference(actual, expected):
    if len(actual) != len(expected):
        return list(actual) or list(expected)
    return [value for value, want in zip(actual, expected) if _text(value) != _text(want)]


def _run_case(client, case, headers, rows):
    response = client.post(ENDPOINT, json={"question": case["question"]}, headers=headers[case["role"]])
    actual, problems = _payload(case, response, rows)
    if response.status_code != case["status"]:
        problems.insert(0, f"status:{response.status_code}!={case['status']}")
    return {"id": case["id"], "theme": case.get("theme"), "role": case["role"],
            "passed": not problems, "status": response.status_code,
            "expected_status": case["status"], "values": actual, "problems": problems}


def evaluate(client, cases, *, headers, rows=None, threshold=0.85):
    if not cases or not 0 <= threshold <= 1:
        raise ValueError("An evaluation requires labelled cases and a threshold between 0 and 1")
    results = [_run_case(client, case, headers, rows) for case in cases]
    themes = {}
    for item in results:
        bucket = themes.setdefault(item["theme"] or "unlabelled", {"total": 0, "correct": 0})
        bucket["total"] += 1
        bucket["correct"] += int(item["passed"])
    correct = sum(item["passed"] for item in results)
    accuracy = correct / len(results)
    return {
        "total": len(results), "correct": correct, "accuracy": accuracy,
        "threshold": threshold, "passed": accuracy >= threshold, "themes": themes,
        "cases": results, "failures": [item for item in results if not item["passed"]],
    }
