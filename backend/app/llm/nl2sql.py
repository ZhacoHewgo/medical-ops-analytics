"""Parse bounded Chinese outpatient questions and use the certified ADS reader."""
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re
from unicodedata import normalize

from fastapi import HTTPException

from app.api.routers.outpatient_router import OutpatientQuery, _certified_data
from app.outpatient import strict_date


_ISO_DAY = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_MONTH_SERIES = r"(?P<series_year>[0-9]{4})年(?P<first_month>[0-9]{1,2})月?(?:至|到)(?P<last_month>[0-9]{1,2})月"
_QUESTION = re.compile(
    rf"(?:请问|请|查询|统计|列出)?"
    rf"(?:{_MONTH_SERIES}|(?P<first_day>{_ISO_DAY})(?:(?:至|到)(?P<last_day>{_ISO_DAY}))?"
    r"|(?P<month_year>[0-9]{4})(?:-|年)(?P<month>[0-9]{1,2})月?)"
    r"(?P<scope>全院|本科室|我的科室|科室[=:][A-Za-z0-9][A-Za-z0-9_.-]{0,63})?"
    r"(?:每月的?)?(?:有多少|共多少|的)?门诊(?:共多少)?(?:人次|量)"
    r"(?P<mom>环比(?:增长率|变化率)?)?(?:是多少|有多少|多少)?"
)
_FORBIDDEN = re.compile(
    r"(?:身份证|联系方式|患者.{0,10}(?:身份|姓名|电话|住址|个人明细|编号)|"
    r"(?<![A-Za-z0-9_])(?:select|insert|update|delete|drop|alter|create|truncate|grant|"
    r"patient_id|patient_name|id_card)(?![A-Za-z0-9_]))", re.IGNORECASE,
)


@dataclass(frozen=True)
class Intent:
    first: date
    last: date
    scope: str | None
    series: bool
    growth: bool


def _last_day(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def parse_intent(question: str) -> Intent:
    normalized = normalize("NFKC", question)
    if _FORBIDDEN.search(normalized):
        raise HTTPException(403, "问数仅开放已授权的聚合指标")
    text = re.sub(r"\s+", "", normalized).rstrip("?。")
    match = _QUESTION.fullmatch(text)
    if match is None:
        raise HTTPException(422, "请明确日期、科室范围及门诊人次或自然月环比")
    values = match.groupdict()
    series = values["series_year"] is not None
    monthly = values["month_year"] is not None
    growth = values["mom"] is not None
    try:
        if series:
            year = int(values["series_year"])
            first = date(year, int(values["first_month"]), 1)
            last = _last_day(date(year, int(values["last_month"]), 1))
        elif monthly:
            first = date(int(values["month_year"]), int(values["month"]), 1)
            last = _last_day(first)
        else:
            first = strict_date(values["first_day"])
            last = strict_date(values["last_day"] or values["first_day"])
        if first > last or first.year < 1000 or growth and not monthly:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "日期范围无效，环比须指定一个自然月") from None
    return Intent(first, last, values["scope"], series, growth)


def _department_scope(intent, user):
    if intent.scope in ("本科室", "我的科室") and user.role != "department":
        raise HTTPException(422, "请指定要查询的科室编码")
    if intent.scope and intent.scope.startswith("科室"):
        return intent.scope[3:]
    return None


def _series_points(daily, start, end):
    points = []
    while start <= end:
        finish = _last_day(start)
        points.append({"start_date": start.isoformat(), "end_date": finish.isoformat(),
                       "value": daily[start.strftime("%Y-%m")]})
        if finish == end:
            break
        start = finish + timedelta(days=1)
    return points


def run_nl_query(question, user):
    compact = re.sub(r"\s+", "", normalize("NFKC", question))
    if user.role == "department" and any(term in compact for term in ("全院", "未授权科室", "其他科室", "其它科室")):
        raise HTTPException(403, "科室范围越权")
    intent = parse_intent(question)
    department = _department_scope(intent, user)
    prior = (intent.first - timedelta(days=1)).replace(day=1) if intent.growth else intent.first
    query = OutpatientQuery(start_date=prior.isoformat(), end_date=intent.last.isoformat(), dept_code=department)
    try:
        scope, _, summary = _certified_data(query, user)
    except HTTPException as error:
        if error.status_code == 422 and error.detail == "Dates must be within certified build coverage":
            raise HTTPException(503, "所问期间或可比上期超出认证数据覆盖") from None
        raise
    data = summary["data"]
    monthly = Counter()
    for row in data["daily_trend"]:
        monthly[row["date"][:7]] += row["visits"]

    reason = None
    if intent.growth:
        current, previous = monthly[intent.first.strftime("%Y-%m")], monthly[prior.strftime("%Y-%m")]
        if previous == 0:
            value, reason = None, "ZERO_BASE"
            interpretation = "上期门诊人次为 0，无法计算环比。"
        else:
            change = (Decimal(current - previous) * 100 / previous).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            value = format(change, "f")
            interpretation = f"本期 {current} 人次，上期 {previous} 人次，门诊人次环比 {value}%。"
        points = [{"start_date": intent.first.isoformat(), "end_date": intent.last.isoformat(), "value": value}]
    elif intent.series:
        points = _series_points(monthly, intent.first, intent.last)
        interpretation = f"所查 {len(points)} 个自然月共 {sum(point['value'] for point in points)} 人次。"
    else:
        value = data["outpatient_visits"]
        points = [{"start_date": intent.first.isoformat(), "end_date": intent.last.isoformat(), "value": value}]
        interpretation = f"所查期间门诊共 {value} 人次。"

    meta = data["meta"]
    return {
        "indicator_code": "MOPS-OUT-02" if intent.growth else "MOPS-OUT-01",
        "unit": "%" if intent.growth else "人次", "scope": {"department": scope},
        "chart_type": "line" if intent.series else "number",
        "points": points, "interpretation": interpretation, "null_reason": reason,
        "source": meta["source"], "batch_date": meta["batch_date"], "build_run_id": meta["build_run_id"],
    }
