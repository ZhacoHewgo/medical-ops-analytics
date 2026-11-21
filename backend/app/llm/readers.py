"""Read-only adapters over the certified outpatient and theme marts."""
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
from uuid import UUID

from fastapi import HTTPException

from app.outpatient import strict_date

_COVERAGE = "Dates must be within certified build coverage"
_RANGE_CODES = {"N10_RANGE_OUTSIDE_BUILD", "N10_DEPARTMENT_UNKNOWN"}
_TWO_PLACES = Decimal("0.01")


def _places(value, places):
    try:
        quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return format(quantized, "f")


def _months(first, last):
    cursor = first.replace(day=1)
    while cursor <= last:
        finish = cursor.replace(day=monthrange(cursor.year, cursor.month)[1])
        yield cursor, min(finish, last)
        cursor = (finish + timedelta(days=1)).replace(day=1)


def _monthly(counter):
    return Counter(counter)


def outpatient(metric, first, last, scope, user, *, series):
    from app.schemas.outpatient import OutpatientQuery
    from app.services.indicator_service import percent_change, query_indicator

    prior = (first - timedelta(days=1)).replace(day=1) if metric == "MOPS-OUT-02" else first
    query = OutpatientQuery(start_date=prior.isoformat(), end_date=last.isoformat(), dept_code=scope)
    try:
        _, _, summary = query_indicator(query, user)
    except HTTPException as error:
        if error.status_code == 422 and error.detail == _COVERAGE:
            raise HTTPException(503, "所问期间或可比上期超出认证数据覆盖") from None
        raise
    data = summary["data"]
    monthly = Counter()
    for row in data["daily_trend"]:
        monthly[row["date"][:7]] += row["visits"]
    meta = data["meta"]
    observation = {"unit": "人次", "source": meta["source"], "batch_date": meta["batch_date"],
                   "build_run_id": meta["build_run_id"], "null_reason": None}
    if metric == "MOPS-OUT-02":
        current = monthly[first.strftime("%Y-%m")]
        before = monthly[prior.strftime("%Y-%m")]
        value, reason = percent_change(current, before)
        observation.update(unit="%", null_reason=reason, chart_type="number", detail=(current, before),
                           points=[{"start_date": first.isoformat(), "end_date": last.isoformat(), "value": value}])
        return observation
    if series:
        points = [{"start_date": start.isoformat(), "end_date": finish.isoformat(),
                   "value": monthly[start.strftime("%Y-%m")]} for start, finish in _months(first, last)]
        observation.update(chart_type="line", points=points)
        return observation
    observation.update(chart_type="number", points=[{
        "start_date": first.isoformat(), "end_date": last.isoformat(), "value": data["outpatient_visits"]}])
    return observation


def _theme_arguments():
    from etl.config.source_config import OdsError, database_options, validate_dataset

    env = os.environ.get("OUTPATIENT_PREVIEW_ENV", "dev")
    dataset = os.environ.get("OUTPATIENT_PREVIEW_DATASET_ID", "")
    batch = os.environ.get("OUTPATIENT_PREVIEW_BATCH_DATE")
    run = os.environ.get("OUTPATIENT_PREVIEW_BUILD_RUN_ID")
    try:
        dataset = validate_dataset(dataset)
        if not batch or not run or str(UUID(run)) != run:
            raise ValueError
        strict_date(batch)
        if database_options()["database"] != {"dev": "hospital", "test": "hospital_test"}.get(env):
            raise ValueError
    except (OdsError, ValueError):
        raise HTTPException(503, "认证集市未就绪或批次标识缺失") from None
    return {"dataset_id": dataset, "batch_date": batch, "build_run_id": run, "env": env}


def _theme_call(loader, *args, **kwargs):
    from etl.config.source_config import OdsError

    try:
        return loader(*args, **kwargs)
    except OdsError as error:
        if error.code in _RANGE_CODES:
            raise HTTPException(422, "日期或科室超出认证数据覆盖") from None
        raise HTTPException(503, f"认证集市不可用或不完整（{error.code}）") from None


def inpatient(metric, first, last, scope, user, *, series):
    from etl.jobs.ads_indicators import summarize_inpatient

    arguments = _theme_arguments()
    data = _theme_call(summarize_inpatient, first.isoformat(), last.isoformat(), scope, **arguments)
    meta = data["meta"]
    observation = {"source": meta["source"], "batch_date": meta["batch_date"],
                   "build_run_id": meta["build_run_id"], "null_reason": None}
    if metric == "MOPS-INP-01":
        observation.update(unit="人次")
        if series:
            bucket = Counter()
            for row in data["daily_trend"]:
                bucket[row["date"][:7]] += row["discharges"]
            observation.update(chart_type="line", points=[
                {"start_date": start.isoformat(), "end_date": finish.isoformat(),
                 "value": bucket[start.strftime("%Y-%m")]} for start, finish in _months(first, last)])
            return observation
        observation.update(chart_type="number", points=[{
            "start_date": first.isoformat(), "end_date": last.isoformat(), "value": data["discharges"]}])
        return observation
    key, unit = ("avg_stay_days", "天") if metric == "MOPS-INP-02" else ("avg_cost", "元/人次")
    item = data[key]
    observation.update(unit=unit, chart_type="number", null_reason=item["null_reason"], points=[{
        "start_date": first.isoformat(), "end_date": last.isoformat(),
        "value": None if item["value"] is None else _places(item["value"], 2)}])
    return observation


def income(first, last, scope, user, *, series):
    from etl.jobs.ads_indicators import summarize_theme

    arguments = _theme_arguments()
    data = _theme_call(summarize_theme, "income", first.isoformat(), last.isoformat(), scope, **arguments)
    meta = data["meta"]
    observation = {"unit": "元", "source": meta["source"], "batch_date": meta["batch_date"],
                   "build_run_id": meta["build_run_id"], "null_reason": None}
    if series:
        bucket, reasons = Counter(), {}
        for row in data["rows"]:
            month = row["group"][:7]
            if row["value"] is None:
                reasons[month] = row["null_reason"] or "INCOMPLETE_INPUT"
            else:
                bucket[month] += Decimal(row["value"])
        points, reason = [], None
        for start, finish in _months(first, last):
            month = start.strftime("%Y-%m")
            if month in reasons:
                reason = reasons[month]
                value = None
            else:
                value = _places(bucket[month], 2)
            points.append({"start_date": start.isoformat(), "end_date": finish.isoformat(), "value": value})
        observation.update(chart_type="line", null_reason=reason, points=points)
        return observation
    item = data["metrics"]["charge_amount"]
    observation.update(chart_type="number", null_reason=item["null_reason"], points=[{
        "start_date": first.isoformat(), "end_date": last.isoformat(),
        "value": None if item["value"] is None else _places(item["value"], 2)}])
    return observation


READERS = {
    "MOPS-OUT-01": lambda first, last, scope, user, series: outpatient("MOPS-OUT-01", first, last, scope, user,
                                                                        series=series),
    "MOPS-OUT-02": lambda first, last, scope, user, series: outpatient("MOPS-OUT-02", first, last, scope, user,
                                                                        series=False),
    "MOPS-INP-01": lambda first, last, scope, user, series: inpatient("MOPS-INP-01", first, last, scope, user,
                                                                       series=series),
    "MOPS-INP-02": lambda first, last, scope, user, series: inpatient("MOPS-INP-02", first, last, scope, user,
                                                                       series=False),
    "MOPS-INP-06": lambda first, last, scope, user, series: inpatient("MOPS-INP-06", first, last, scope, user,
                                                                       series=False),
    "MOPS-INC-01": lambda first, last, scope, user, series: income(first, last, scope, user, series=series),
}
