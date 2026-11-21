"""Independent recomputation of evaluation expectations from synthetic seed rows."""
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class OracleError(ValueError):
    pass


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _decimal(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _growth(current, before):
    if before == 0:
        return None
    value = (Decimal(current - before) * 100 / Decimal(before)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(value, "f")


def _mean(total, count):
    if count == 0:
        return None
    return format((total / count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _rows(rows, name):
    value = rows.get(name) or ()
    return [row for row in value if isinstance(row, dict)]


def _scoped(row, dept):
    return dept is None or row.get("dept_code") == dept


def outpatient(rows, first, last, dept=None):
    seen = {row.get("visit_id") for row in _rows(rows, "his_outpatient")
            if _scoped(row, dept) and row.get("visit_type") == "OUTPATIENT"
            and (day := _day(row.get("visit_date"))) is not None and first <= day <= last}
    return len(seen - {None})


def discharges(rows, first, last, dept=None):
    seen = {row.get("visit_id") for row in _rows(rows, "his_inpatient")
            if _scoped(row, dept)
            and (day := _day(row.get("discharge_date"))) is not None and first <= day <= last}
    return len(seen - {None})


def stay_days(rows, first, last, dept=None):
    total, count = Decimal(0), 0
    for row in _rows(rows, "his_inpatient"):
        day = _day(row.get("discharge_date"))
        if not _scoped(row, dept) or day is None or not first <= day <= last:
            continue
        value = _decimal(row.get("stay_days"))
        if value is None:
            continue
        total += value
        count += 1
    return _mean(total, count)


def average_fee(rows, first, last, dept=None):
    total, count = Decimal(0), discharges(rows, first, last, dept)
    for row in _rows(rows, "his_inpatient"):
        day = _day(row.get("discharge_date"))
        if not _scoped(row, dept) or day is None or not first <= day <= last:
            continue
        value = _decimal(row.get("total_fee"))
        if value is not None:
            total += value
    return _mean(total, count)


def income(rows, first, last, dept=None):
    total = Decimal(0)
    for row in _rows(rows, "his_charge"):
        day = _day(row.get("fee_date"))
        if not _scoped(row, dept) or day is None or not first <= day <= last:
            continue
        value = _decimal(row.get("amount"))
        if value is not None:
            total += value
    return format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _month_end(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def _months(first, last):
    cursor = first.replace(day=1)
    while cursor <= last:
        finish = min(_month_end(cursor), last)
        yield cursor, finish
        cursor = (finish + timedelta(days=1)).replace(day=1)


_SINGLE = {
    "MOPS-OUT-01": lambda rows, first, last, dept: outpatient(rows, first, last, dept),
    "MOPS-INP-01": lambda rows, first, last, dept: discharges(rows, first, last, dept),
    "MOPS-INP-02": lambda rows, first, last, dept: stay_days(rows, first, last, dept),
    "MOPS-INP-06": lambda rows, first, last, dept: average_fee(rows, first, last, dept),
    "MOPS-INC-01": lambda rows, first, last, dept: income(rows, first, last, dept),
}


def expected_points(spec, rows):
    metric = spec.get("metric")
    if metric == "MOPS-OUT-02":
        first = date.fromisoformat(spec["start"])
        prior = (first - timedelta(days=1)).replace(day=1)
        current = outpatient(rows, first, _month_end(first), spec.get("dept"))
        before = outpatient(rows, prior, _month_end(prior), spec.get("dept"))
        return [_growth(current, before)]
    if metric not in _SINGLE:
        raise OracleError(f"NO_ORACLE:{metric}")
    first, last, dept = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"]), spec.get("dept")
    if spec.get("form") == "series":
        return [_SINGLE[metric](rows, start, finish, dept) for start, finish in _months(first, last)]
    return [_SINGLE[metric](rows, first, last, dept)]
