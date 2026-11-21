"""Bounded Chinese question planner over the frozen metric catalogue."""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import time
from unicodedata import normalize
from uuid import UUID

from fastapi import HTTPException

from app.llm import catalog
from app.outpatient import strict_date

_DAY = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_DAY_RANGE = re.compile(rf"(?P<first_day>{_DAY})(?:至|到|~|—)(?P<last_day>{_DAY})")
_MONTH_RANGE = re.compile(
    rf"(?P<year>[0-9]{{4}})年(?P<first_month>[0-9]{{1,2}})月?(?:至|到)"
    rf"(?:(?P<other_year>[0-9]{{4}})年)?(?P<last_month>[0-9]{{1,2}})月",
)
_DAY_SINGLE = re.compile(rf"(?P<day>{_DAY})")
_MONTH_SINGLE = re.compile(r"(?P<year>[0-9]{4})(?:-|年)(?P<month>[0-9]{1,2})月?")
_SCOPE = re.compile(r"(?P<scope>全院|本科室|我的科室|科室[=:][A-Za-z0-9][A-Za-z0-9_.-]{0,63})")
_SERIES = re.compile(r"每个月|每月|各个月|各月")
_FILLER = re.compile(
    r"请问|帮我查一下|帮我查|查一下|请查询|查询|统计|列出|看一下|显示|一共|总共|合计|有多少|是多少|多少|共|为|是|的|呢|吗"
)
_PUNCTUATION = re.compile(r"[\s，。！？、；：,.!?;:]+")
_FORBIDDEN = re.compile(
    r"(?:身份证|联系方式|患者.{0,10}(?:身份|姓名|电话|住址|个人明细|编号)|"
    r"(?<![A-Za-z0-9_])(?:select|insert|update|delete|drop|alter|create|truncate|grant|"
    r"patient_id|patient_name|id_card)(?![A-Za-z0-9_]))", re.IGNORECASE,
)
_FORBIDDEN_SCOPE = ("未授权科室", "其他科室", "其它科室")
_DELEGATED = ("本科室", "我的科室")

MAX_QUESTION = 300
MAX_SERIES_MONTHS = 24
MAX_RANGE_DAYS = 366
BUDGET_SECONDS = 5.0


@dataclass(frozen=True)
class Intent:
    code: str
    first: date
    last: date
    scope: str | None
    series: bool
    growth: bool


def _last_day(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def _period(text):
    if match := _DAY_RANGE.search(text):
        first, last = strict_date(match["first_day"]), strict_date(match["last_day"])
        return first, last, text[:match.start()] + text[match.end():]
    if match := _MONTH_RANGE.search(text):
        year = int(match["year"])
        other = int(match["other_year"] or year)
        first = date(year, int(match["first_month"]), 1)
        last = _last_day(date(other, int(match["last_month"]), 1))
        return first, last, text[:match.start()] + text[match.end():]
    if match := _DAY_SINGLE.search(text):
        day = strict_date(match["day"])
        return day, day, text[:match.start()] + text[match.end():]
    if match := _MONTH_SINGLE.search(text):
        first = date(int(match["year"]), int(match["month"]), 1)
        return first, _last_day(first), text[:match.start()] + text[match.end():]
    return None


def parse_intent(question):
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise HTTPException(422, "问句须为 1 至 300 字的中文问题")
    normalized = normalize("NFKC", question)
    if _FORBIDDEN.search(normalized):
        raise HTTPException(403, "问数仅开放已授权的聚合指标")
    text = _PUNCTUATION.sub("", normalized)
    try:
        found = _period(text)
    except ValueError:
        raise HTTPException(422, "日期须为有效的日历日期") from None
    if found is None:
        raise HTTPException(422, "请明确统计期间，例如 2025-02 或 2025-01-01 至 2025-02-28")
    first, last, text = found
    if first > last or first.year < 1000:
        raise HTTPException(422, "日期范围无效")
    scope = None
    if match := _SCOPE.search(text):
        scope = match["scope"]
        text = text[:match.start()] + text[match.end():]
    growth = "环比" in text
    series = bool(_SERIES.search(text))
    text = _SERIES.sub("", text)
    text = _FILLER.sub("", text)
    if not text:
        raise HTTPException(422, "请明确要查询的指标，例如门诊人次或出院人次")
    try:
        code = catalog.resolve(text)
    except LookupError:
        raise HTTPException(422, "问句同时命中多个指标，请只指定一个") from None
    if code is None:
        raise HTTPException(422, "当前未开放该指标，仅支持已认证的聚合指标")
    return _seal_intent(code, first, last, scope, series, growth)


def _seal_intent(code, first, last, scope, series, growth):
    """Apply the shared catalogue and boundary rules to a rule or model intent."""
    entry = catalog.entry(code)
    if growth:
        if code == "MOPS-OUT-01":
            code = "MOPS-OUT-02"
            entry = catalog.entry(code)
        elif code != "MOPS-OUT-02" and catalog.is_served(code):
            raise HTTPException(422, "当前仅支持门诊人次自然月环比")
    if series and growth and catalog.is_served(code):
        raise HTTPException(422, "环比不支持按月展开")
    if series and not entry.get("additive"):
        raise HTTPException(422, "该指标不支持按月展开")
    if growth and code == "MOPS-OUT-02" and (first.day != 1 or last != _last_day(last)):
        raise HTTPException(422, "环比须指定一个完整自然月")
    if series and (last.year - first.year) * 12 + last.month - first.month + 1 > MAX_SERIES_MONTHS:
        raise HTTPException(422, "按月展开最多覆盖 24 个自然月")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(422, "单次问数最多覆盖 366 天")
    return Intent(code, first, last, scope, series, growth)


def _enforce_scope(intent, user):
    if user.role == "department" and intent.scope in _DELEGATED and user.department is None:
        raise HTTPException(403, "账号未绑定科室")
    if user.role == "department":
        if intent.scope is None or intent.scope == "全院":
            raise HTTPException(403, "科室范围越权")
        if intent.scope.startswith("科室"):
            code = intent.scope[3:]
            if user.department is None or code != user.department:
                raise HTTPException(403, "科室范围越权")
            return code
        return user.department
    if user.role not in ("admin", "analyst"):
        raise HTTPException(403, "问数仅开放管理员、分析员与科室账号")
    if intent.scope in _DELEGATED:
        raise HTTPException(422, "请指定要查询的科室编码")
    if intent.scope and intent.scope.startswith("科室"):
        return intent.scope[3:]
    return None


SQL_TABLES = {
    "ads_outpatient_theme": ("stat_date", "dept_code", "outpatient_visits"),
    "ads_inpatient_theme": ("stat_date", "dept_code", "discharges", "discharge_stay_days",
                            "stay_status", "discharge_total_fee", "fee_status"),
    "ads_income_theme": ("stat_date", "dept_code", "charge_amount"),
}

_SQL_TEMPLATES = {
    "outpatient_volume": {
        "table": "ads_outpatient_theme", "places": 0, "grouped": True,
        "select": "EXTRACT(YEAR_MONTH FROM stat_date) AS period, SUM(outpatient_visits) AS total",
    },
    "inpatient_discharges": {
        "table": "ads_inpatient_theme", "places": 0, "grouped": True,
        "select": "EXTRACT(YEAR_MONTH FROM stat_date) AS period, SUM(discharges) AS total",
    },
    "inpatient_stay": {
        "table": "ads_inpatient_theme", "places": 2, "grouped": False,
        "select": ("SUM(discharge_stay_days) AS total_days, SUM(discharges) AS total_cases, "
                   "SUM(stay_status IS NOT NULL) AS flagged"),
    },
    "inpatient_cost": {
        "table": "ads_inpatient_theme", "places": 2, "grouped": False,
        "select": ("SUM(discharge_total_fee) AS total_fee, SUM(discharges) AS total_cases, "
                   "SUM(fee_status IS NOT NULL) AS flagged"),
    },
    "income_total": {
        "table": "ads_income_theme", "places": 2, "grouped": True,
        "select": ("EXTRACT(YEAR_MONTH FROM stat_date) AS period, SUM(charge_amount) AS total, "
                   "SUM(charge_amount IS NULL) AS missing"),
    },
}

_SQL_METRICS = {
    "MOPS-OUT-01": "outpatient_volume",
    "MOPS-OUT-02": "outpatient_volume",
    "MOPS-INP-01": "inpatient_discharges",
    "MOPS-INP-02": "inpatient_stay",
    "MOPS-INP-06": "inpatient_cost",
    "MOPS-INC-01": "income_total",
}

_SQL_WHERE = ("dataset_id=%s AND batch_date=%s AND source_build_run_id=%s "
              "AND stat_date BETWEEN %s AND %s")
_SQL_SCOPE_WHERE = " AND dept_code=%s"
_SQL_MONTHLY = ("outpatient_volume", "inpatient_discharges", "income_total")
_SQL_DEPARTMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SQL_RATIO = Decimal("0.000001")


@dataclass(frozen=True)
class SqlPlan:
    """A frozen, parameterised read-only query for one certified metric."""

    metric: str
    template: str
    statement: str
    params: tuple
    tables: tuple
    columns: tuple
    scope: str | None
    series: bool
    growth: bool
    places: int
    first: date
    last: date
    origin: str = "rules"

    def __iter__(self):
        yield self.statement
        yield self.params


def _statement(template, scoped):
    spec = _SQL_TEMPLATES[template]
    sql = f"SELECT {spec['select']} FROM {spec['table']} WHERE {_SQL_WHERE}"
    if scoped:
        sql += _SQL_SCOPE_WHERE
    if spec["grouped"]:
        sql += " GROUP BY period ORDER BY period"
    return sql


def _sql_context():
    from app.llm.readers import _theme_arguments

    context = _theme_arguments()
    return context["dataset_id"], context["batch_date"], context["build_run_id"]


_CANDIDATE_FIELDS = {"indicator", "start_date", "end_date", "scope", "series", "growth"}


def _metric_meta():
    return [{"code": code, "title": item["title"], "unit": item["unit"], "keywords": list(item["keywords"])}
            for code, item in catalog.METRICS.items() if catalog.is_served(code)]


def _dim_schema():
    return {"tables": {name: list(columns) for name, columns in SQL_TABLES.items()},
            "dimensions": ["stat_date", "dept_code"], "metrics": list(_SQL_METRICS)}


def candidate_prompt(question):
    """The prompt handed to an injected model generator; carries no credentials or row data."""
    from app.llm.prompts import build_nl2sql_prompt

    return build_nl2sql_prompt(question, _metric_meta(), _dim_schema())


def parse_candidate(payload):
    """Seal a model-proposed intent; free SQL and unknown fields are refused."""
    if not isinstance(payload, dict):
        raise HTTPException(422, "候选意图须为 JSON 对象")
    if set(payload) - _CANDIDATE_FIELDS:
        raise HTTPException(422, "候选意图含未授权字段")
    indicator = payload.get("indicator")
    code = None
    if isinstance(indicator, str):
        if indicator in catalog.METRICS:
            code = indicator
        else:
            try:
                code = catalog.resolve(indicator)
            except LookupError:
                raise HTTPException(422, "候选意图命中多个指标") from None
    if code is None:
        raise HTTPException(422, "候选意图未给出目录内的指标")
    try:
        first = strict_date(payload["start_date"])
        last = strict_date(payload["end_date"])
    except KeyError:
        raise HTTPException(422, "候选意图缺少起止日期") from None
    except (ValueError, HTTPException):
        raise HTTPException(422, "候选意图的日期不合法") from None
    if first > last:
        raise HTTPException(422, "候选意图的日期范围无效")
    series = payload.get("series", False)
    growth = payload.get("growth", False)
    if not isinstance(series, bool) or not isinstance(growth, bool):
        raise HTTPException(422, "候选意图的时间粒度须为布尔值")
    scope = payload.get("scope")
    if scope is not None and (not isinstance(scope, str) or not _SCOPE.fullmatch(scope)):
        raise HTTPException(422, "候选意图的科室范围不合法")
    return _seal_intent(code, first, last, scope, series, growth)


def _generated_intent(question, generator):
    """Model intent first, deterministic rules as the fallback; never accept raw SQL."""
    try:
        return parse_candidate(generator(question, candidate_prompt(question))), "model"
    except HTTPException:
        return parse_intent(question), "model-fallback"
    except Exception:
        return parse_intent(question), "model-fallback"


def _scope_department(intent, user, department):
    """Resolve the effective department: the signed-in role wins over any override."""
    if user is not None:
        allowed = _enforce_scope(intent, user)
        if department is None:
            return allowed
        if not isinstance(department, str) or not _SQL_DEPARTMENT.fullmatch(department):
            raise HTTPException(422, "科室编码不合法")
        if allowed is not None and department != allowed:
            raise HTTPException(403, "科室范围越权")
        return department
    if department is not None:
        if not isinstance(department, str) or not _SQL_DEPARTMENT.fullmatch(department):
            raise HTTPException(422, "科室编码不合法")
        return department
    if intent.scope in _DELEGATED:
        raise HTTPException(422, "请指定要查询的科室编码")
    if intent.scope and intent.scope.startswith("科室"):
        code = intent.scope[3:]
        if not _SQL_DEPARTMENT.fullmatch(code):
            raise HTTPException(422, "科室编码不合法")
        return code
    return None


def generate_sql(question, user=None, *, department=None, generator=None):
    """Turn one intent into a frozen read-only query over whitelisted mart tables."""
    if isinstance(question, Intent):
        intent, origin = question, "rules"
    elif generator is None:
        intent, origin = parse_intent(question), "rules"
    else:
        intent, origin = _generated_intent(question, generator)
    template = _SQL_METRICS.get(intent.code)
    if template is None:
        raise HTTPException(422, "该指标没有可查询的认证表，仅登记缺口原因")
    scope = _scope_department(intent, user, department)
    dataset, batch, run = _sql_context()
    window = (intent.first - timedelta(days=1)).replace(day=1) if intent.growth else intent.first
    params = (dataset, batch, run, window.isoformat(), intent.last.isoformat())
    if scope is not None:
        params += (scope,)
    spec = _SQL_TEMPLATES[template]
    return SqlPlan(metric=intent.code, template=template, statement=_statement(template, scope is not None),
                   params=params, tables=(spec["table"],), columns=SQL_TABLES[spec["table"]],
                   scope=scope, series=intent.series, growth=intent.growth, places=spec["places"],
                   first=intent.first, last=intent.last, origin=origin)


def _checked(plan):
    """Rebuild the statement from the frozen registry; reject anything else."""
    template = getattr(plan, "template", None)
    if not isinstance(template, str) or template not in _SQL_TEMPLATES:
        raise HTTPException(422, "问数计划不在冻结的指标模板内")
    spec = _SQL_TEMPLATES[template]
    scoped = getattr(plan, "scope", None) is not None
    statement = _statement(template, scoped)
    params = getattr(plan, "params", None)
    if getattr(plan, "statement", None) != statement:
        raise HTTPException(422, "问数计划与冻结的只读语句不一致")
    if tuple(getattr(plan, "tables", ())) != (spec["table"],):
        raise HTTPException(422, "问数计划引用了白名单之外的表")
    if tuple(getattr(plan, "columns", ())) != SQL_TABLES[spec["table"]]:
        raise HTTPException(422, "问数计划引用了白名单之外的列")
    if not isinstance(params, tuple) or len(params) != statement.count("%s"):
        raise HTTPException(422, "问数计划参数与占位符不匹配")
    if any(not isinstance(value, str) for value in params):
        raise HTTPException(422, "问数计划参数必须为字符串")
    try:
        strict_date(params[1])
        UUID(params[2])
        strict_date(params[3])
        strict_date(params[4])
    except (ValueError, HTTPException):
        raise HTTPException(422, "问数计划参数不合法") from None
    if not _SQL_DEPARTMENT.fullmatch(params[0]):
        raise HTTPException(422, "问数计划参数不合法")
    if scoped and not _SQL_DEPARTMENT.fullmatch(params[5]):
        raise HTTPException(422, "问数计划参数不合法")
    return statement, params


def validate_sql(plan):
    """Public read-only guard: returns the frozen statement and its bound parameters."""
    return _checked(plan)


def execute_plan(plan, *, budget=None):
    """Run the frozen statement with bound parameters inside a read-only snapshot."""
    from etl.config.source_config import OdsError, connect_database

    statement, params = validate_sql(plan)
    deadline = time.monotonic() + (BUDGET_SECONDS if budget is None else budget)
    try:
        connection = connect_database()
    except OdsError:
        raise HTTPException(503, "认证集市连接不可用") from None
    try:
        with connection.cursor() as cursor:
            try:
                cursor.execute("SET SESSION max_execution_time=%s",
                               (max(1, int((BUDGET_SECONDS if budget is None else budget) * 1000)),))
            except Exception:
                pass
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            cursor.execute(statement, params)
            rows = cursor.fetchall()
    except OdsError:
        raise HTTPException(503, "认证集市查询失败") from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "认证集市查询失败") from None
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
    if time.monotonic() > deadline:
        raise HTTPException(503, "问数查询超出时限，请缩小查询范围后重试")
    return rows


def _sql_number(value, places):
    if value is None:
        return 0 if places == 0 else "0.00"
    if places == 0:
        return int(value)
    unit = Decimal(1).scaleb(-places)
    return format(Decimal(str(value)).quantize(unit, rounding=ROUND_HALF_UP), "f")


def _sql_growth(current, before):
    if current is None or before is None or not before:
        return None
    ratio = ((Decimal(current) - Decimal(before)) * 100 / Decimal(before)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(ratio, "f")


def _sql_values(plan, rows):
    """Values the generated query yields for the answered points."""
    if plan.template in _SQL_MONTHLY:
        buckets = {}
        for row in rows:
            missing = int(row[2]) if len(row) > 2 and row[2] is not None else 0
            buckets[int(row[0])] = (row[1], missing)
        if plan.series:
            values = []
            cursor = plan.first.replace(day=1)
            while cursor <= plan.last:
                total, missing = buckets.get(cursor.year * 100 + cursor.month, (None, 0))
                values.append(None if missing else _sql_number(total, plan.places))
                cursor = _last_day(cursor) + timedelta(days=1)
            return values
        if plan.growth:
            current, _ = buckets.get(plan.first.year * 100 + plan.first.month, (None, 0))
            prior = (plan.first - timedelta(days=1)).replace(day=1)
            before, _ = buckets.get(prior.year * 100 + prior.month, (None, 0))
            return [_sql_growth(current, before)]
        if any(missing for _, missing in buckets.values()):
            return [None]
        total = sum((Decimal(value) for value, _ in buckets.values() if value is not None), Decimal(0))
        return [_sql_number(total, plan.places)]
    total, cases, flagged = rows[0] if rows else (None, 0, 0)
    if total is None or int(flagged or 0) or not cases:
        return [None]
    ratio = (Decimal(str(total)) / Decimal(int(cases))).quantize(_SQL_RATIO, rounding=ROUND_HALF_UP)
    return [_sql_number(ratio, plan.places)]


def _same_value(actual, expected, places):
    if actual is None or expected is None:
        return actual is None and expected is None
    try:
        unit = Decimal(1).scaleb(-places)
        return Decimal(str(actual)).quantize(unit) == Decimal(str(expected)).quantize(unit)
    except (InvalidOperation, ValueError):
        return str(actual) == str(expected)


def _cross_check(plan, observation, rows):
    expected = _sql_values(plan, rows)
    actual = [point["value"] for point in observation["points"]]
    if len(actual) != len(expected) or not all(
            _same_value(left, right, plan.places) for left, right in zip(actual, expected)):
        raise HTTPException(503, "生成的只读查询与认证汇总不一致，已降级")


def _total(points):
    total = Decimal(0)
    for point in points:
        if point["value"] is None:
            continue
        try:
            total += Decimal(str(point["value"]))
        except (InvalidOperation, ValueError):
            continue
    return format(total.normalize(), "f")


def _interpret(intent, entry, observation):
    points = observation["points"]
    reason = observation.get("null_reason")
    value = points[0]["value"] if points else None
    if reason is not None:
        return f"{entry['title']}本期保留空值（原因：{reason}）。"
    if intent.growth:
        current, before = observation.get("detail", (None, None))
        return f"本期 {current} 人次，上期 {before} 人次，{entry['title']} {value}%。"
    if intent.series:
        return f"所查 {len(points)} 个自然月共 {_total(points)} {entry['unit']}。"
    if intent.code == "MOPS-OUT-01":
        return f"所查期间门诊共 {value} 人次。"
    return f"所查期间{entry['title']}为 {value} {entry['unit']}。"

def _gap_answer(intent, entry, department):
    return {
        "indicator_code": intent.code, "unit": entry["unit"], "scope": {"department": department},
        "chart_type": "number", "points": [{"start_date": intent.first.isoformat(),
                                            "end_date": intent.last.isoformat(), "value": None}],
        "interpretation": f"{entry['title']}当前不可计算：{catalog.GAP_ANSWER}（原因：{entry['reason']}）",
        "null_reason": entry["reason"], "source": catalog.CATALOG_VERSION,
        "batch_date": None, "build_run_id": None,
    }


def run_nl_query(question, user, *, generator=None):
    normalized = normalize("NFKC", question) if isinstance(question, str) else ""
    compact = _PUNCTUATION.sub("", normalized)
    if user.role == "department" and any(term in compact for term in _FORBIDDEN_SCOPE):
        raise HTTPException(403, "科室范围越权")
    intent = parse_intent(question)
    department = _enforce_scope(intent, user)
    entry = catalog.entry(intent.code)
    if not catalog.is_served(intent.code):
        return _gap_answer(intent, entry, department)
    from app.llm import readers

    if generator is None:
        plan = generate_sql(intent, user, department=department)
    else:
        plan = generate_sql(question, user, department=department, generator=generator)
        if plan.metric != intent.code:
            plan = generate_sql(intent, user, department=department)
    rows = execute_plan(plan)
    deadline = time.monotonic() + BUDGET_SECONDS
    observation = readers.READERS[intent.code](intent.first, intent.last, department, user, intent.series)
    if time.monotonic() > deadline:
        raise HTTPException(503, "问数查询超出时限，请缩小查询范围后重试")
    _cross_check(plan, observation, rows)
    return {
        "indicator_code": intent.code, "unit": observation.get("unit", entry["unit"]),
        "scope": {"department": department}, "chart_type": observation.get("chart_type", "number"),
        "points": observation["points"], "interpretation": _interpret(intent, entry, observation),
        "null_reason": observation.get("null_reason"), "source": observation["source"],
        "batch_date": observation["batch_date"], "build_run_id": observation["build_run_id"],
    }
