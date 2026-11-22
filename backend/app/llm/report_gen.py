"""Generate comparable outpatient reporting periods from the same certified ADS snapshot."""
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re

from fastapi import HTTPException

from app.schemas.outpatient import OutpatientQuery
from app.services.indicator_service import compare_yoy_mom, percent_change, query_indicator
from app.outpatient import strict_date


_DATE_COVERAGE = "Dates must be within certified build coverage"


def _month(value):
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}", value or "") is None:
        raise HTTPException(422, "月份须使用 YYYY-MM")
    try:
        first = strict_date(value + "-01")
        if first.year < 1000:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "月份无效") from None
    return first, first.replace(day=monthrange(first.year, first.month)[1])


def _week(value):
    match = re.fullmatch(r"([0-9]{4})-W([0-9]{2})", value or "")
    if match is None:
        raise HTTPException(422, "周次须使用 ISO YYYY-Www")
    try:
        year, week = map(int, match.groups())
        if year < 1000:
            raise ValueError
        first = date.fromisocalendar(year, week, 1)
        return first, first + timedelta(days=6)
    except (ValueError, OverflowError):
        raise HTTPException(422, "周次无效") from None


def rank_changes(current, previous):
    rows = []
    for department in current.keys() | previous.keys():
        now, before = current.get(department, 0), previous.get(department, 0)
        diff = now - before
        if diff == 0:
            continue
        change, reason = percent_change(now, before)
        hint = "建议核对科室发布范围、工作日和服务安排。"
        rows.append({"dept_code": department, "current": now, "previous": before, "difference": diff,
                     "change_pct": change, "null_reason": reason, "hint": hint})
    return sorted(rows, key=lambda row: (-abs(row["difference"]), row["dept_code"]))[:3]


def _dates(first, last):
    return {"start_date": first.isoformat(), "end_date": last.isoformat()}


def _unavailable(error):
    return error.status_code == 503 or error.status_code == 422 and error.detail == _DATE_COVERAGE


def _read_comparison(kind, first, last, user, department, *, operations=False):
    previous_end = first - timedelta(days=1)
    previous = first - timedelta(days=7) if kind == "weekly" else previous_end.replace(day=1)

    def load(start):
        options = {"operations_start": first.isoformat()} if operations else {}
        return query_indicator(OutpatientQuery(**_dates(start, last), dept_code=department), user, **options)

    comparable = True
    try:
        scope, document, _ = load(previous)
    except HTTPException as error:
        if not _unavailable(error):
            raise
        comparable = False
        try:
            scope, document, _ = load(first)
        except HTTPException as current:
            if _unavailable(current):
                raise HTTPException(503, "暂无完整发布的本期认证门诊数据") from None
            raise

    current, prior, daily = Counter(), Counter(), Counter()
    cutoff = first.isoformat()
    for row in document["counts"]:
        if row["date"] < cutoff:
            prior[row["dept_code"]] += row["visits"]
        else:
            current[row["dept_code"]] += row["visits"]
            daily[row["date"]] += row["visits"]
    meta = document["meta"]
    return {
        "indicator_code": "MOPS-OUT-01", "unit": "人次", "scope": {"department": scope},
        "source": meta["source"], "batch_date": meta["batch_date"], "build_run_id": meta["build_run_id"],
        "period": _dates(first, last), "previous_period": _dates(previous, previous_end),
    }, current, prior if comparable else None, daily, document.get("operations")


def _report(kind, first, last, user, department, *, operations=False):
    meta, current, prior, daily, metrics = _read_comparison(kind, first, last, user, department, operations=operations)
    now = sum(current.values())
    before = None if prior is None else sum(prior.values())
    if kind == "monthly":
        current_month = first.isoformat()[:7]
        previous_month = (first - timedelta(days=1)).isoformat()[:7]
        comparison = compare_yoy_mom({previous_month: before, current_month: now})[current_month]
        change, reason = comparison["mom"], comparison["mom_reason"]
    else:
        change, reason = percent_change(now, before)
    trend = []
    for offset in range((last-first).days + 1):
        day = (first + timedelta(days=offset)).isoformat()
        trend.append({"date": day, "visits": daily[day]})
    comparison = [{"dept_code": code, "visits": count}
                  for code, count in sorted(current.items(), key=lambda item: (-item[1], item[0]))]
    changes = [] if prior is None else rank_changes(current, prior)
    chapters = (
        ("overview", "总体概览", {"outpatient_visits": now, "previous_visits": before,
                                "change_pct": change, "null_reason": reason}),
        ("trend", "趋势", trend),
        ("department_comparison", "科室对比", comparison),
        ("anomalies_and_actions", "异常与建议", {
            "status": "PRIOR_UNAVAILABLE" if prior is None else "AVAILABLE", "top_changes": changes,
            "recommendation": "可比上期认证数据不完整，暂无法判断波动。" if prior is None
            else "按绝对变化量排序。结合实际排班与统计覆盖核查波动。",
        }),
    )
    report = {**meta, "type": kind, "sections": [{"id": key, "title": title, "data": data}
                                                for key, title, data in chapters]}
    return _operations_sections(report, metrics) if operations else report


def _operations_sections(report, data):
    sections = {section["id"]: section for section in report["sections"]}
    summary = sections["overview"]["data"]
    trend = sections["trend"]["data"]
    ranking = sections["department_comparison"]
    daily, discharges = Counter(), Counter()
    for row in data["activity"]:
        target = daily if row["kind"] == "OUTPATIENT" else discharges
        target[row["date"]] += row["count"]
    if ({row["dept_code"]: row["visits"] for row in ranking["data"]}
            != {row["code"]: row["outpatient"] for row in data["departments"] if row["outpatient"]}
            or {row["date"]: row["visits"] for row in trend if row["visits"]} != dict(daily)):
        raise HTTPException(503, "本期综合指标与认证门诊数据不一致")
    total = data["charge_total"] if data["daily_charges"] else None
    charges = {row["name"]: row["value"] for row in data["charges"] if row["count"]}
    summary.update(admissions=data["admissions"], discharges=sum(discharges.values()), avg_stay_days=data["avg_stay_days"],
                   charge_amount=total, outpatient_charge=charges.get("OUTPATIENT"), inpatient_charge=charges.get("INPATIENT"))
    fees = data["fee_types"]
    if total is not None and Decimal(total) > 0 and fees and all(
            row["value"] is not None and Decimal(row["value"]) >= 0 for row in fees):
        for name, code in (("drug_share", "DRUG"), ("consumable_share", "CONSUMABLE")):
            amount = sum(Decimal(row["value"]) for row in fees if row["name"] == code)
            summary[name] = format((amount * 100 / Decimal(total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    daily_charges = {row["date"]: row["amount"] for row in data["daily_charges"]}
    for row in trend:
        day = row["date"]
        row.update(discharges=discharges[day], charge_amount=daily_charges.get(day))
    ranking["data"] = [{"dept_code": row["code"], "visits": row["outpatient"],
                        "discharges": row["inpatient"], "charge_amount": row["amount"]}
                       for row in sorted(data["departments"], key=lambda row: (-row["outpatient"], row["code"]))]
    return report


def gen_operations_report(kind, value, user, *, department=None):
    first, last = (_week if kind == "weekly" else _month)(value)
    return _report(kind, first, last, user, department, operations=True)


def gen_weekly_report(week, user, *, department=None):
    return _report("weekly", *_week(week), user, department)


def gen_monthly_report(month, user, *, department=None):
    return _report("monthly", *_month(month), user, department)


def analyze_anomaly(metric, date_string, user, *, department=None):
    if metric != "MOPS-OUT-01":
        raise HTTPException(422, "当前支持门诊人次波动")
    try:
        anchor = strict_date(date_string)
    except ValueError:
        raise HTTPException(422, "日期须为有效的 YYYY-MM-DD") from None
    first, last = _month(f"{anchor.year:04d}-{anchor.month:02d}")
    meta, current, prior, _, _ = _read_comparison("monthly", first, last, user, department)
    if prior is None:
        raise HTTPException(503, "可比上期未完整发布，暂无波动对比")
    return {**meta, "top_changes": rank_changes(current, prior),
            "basis": "本期与上期自然月的科室门诊人次差，按绝对值取前三。"}
