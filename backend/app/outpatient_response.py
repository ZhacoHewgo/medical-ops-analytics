"""Shared HTTP formatting for outpatient aggregate responses."""
from collections import Counter

from fastapi import HTTPException

from app.outpatient import strict_date


def check_dates(start, end):
    try:
        for day in (start, end):
            if day is not None:
                strict_date(day)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must be valid calendar dates in YYYY-MM-DD format") from None
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")


def summarize_outpatient(document, start_date, end_date, dept_code):
    meta = document["meta"]
    start = meta["available_start_date"] if start_date is None else start_date
    end = meta["available_end_date"] if end_date is None else end_date
    check_dates(start, end)
    if dept_code is not None and dept_code not in meta["department_codes"]:
        raise HTTPException(status_code=422, detail="Unknown dept_code")
    daily, departments = Counter(), Counter()
    for item in document["counts"]:
        if start <= item["date"] <= end and (dept_code is None or item["dept_code"] == dept_code):
            daily[item["date"]] += item["visits"]
            departments[item["dept_code"]] += item["visits"]
    return {"code": 0, "msg": "ok", "data": {
        "filters": {"start_date": start, "end_date": end, "dept_code": dept_code},
        "outpatient_visits": sum(daily.values()),
        "daily_trend": [{"date": day, "visits": count} for day, count in sorted(daily.items())],
        "department_ranking": [
            {"dept_code": code, "visits": count}
            for code, count in sorted(departments.items(), key=lambda item: (-item[1], item[0]))
        ],
        "meta": meta,
    }}
