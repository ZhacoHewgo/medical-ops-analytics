from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException

from app.core.deps import apply_data_scope
from app.outpatient_preview import load_ads
from app.outpatient_response import check_dates, summarize_outpatient
from etl.config.source_config import OdsError


def query_indicator(query, user):
    scope = apply_data_scope(query.dept_code, user)
    check_dates(query.start_date, query.end_date)
    if scope is not None and (not scope.strip() or len(scope) > 64):
        raise HTTPException(422, "Invalid dept_code")
    try:
        document = load_ads(start_date=query.start_date, end_date=query.end_date, dept_code=scope,
                            allowed_department=user.department if user.role == "department" else None)
    except (OdsError, ImportError, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, "Certified outpatient mart unavailable or incomplete") from None
    meta = document["meta"]
    low, high = meta["available_start_date"], meta["available_end_date"]
    if any(day is not None and not low <= day <= high for day in (query.start_date, query.end_date)):
        raise HTTPException(422, "Dates must be within certified build coverage")
    summary = summarize_outpatient(document, query.start_date, query.end_date, scope)
    return scope, document, summary


def percent_change(current, previous):
    if current is None or previous is None:
        return None, "PRIOR_UNAVAILABLE"
    if previous == 0:
        return None, "ZERO_BASE"
    value = ((Decimal(current) - Decimal(previous)) * 100 / Decimal(previous)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )
    return format(value, "f"), None


def compare_yoy_mom(series):
    rows = {}
    for month, current in sorted(series.items()):
        first = date.fromisoformat(month + "-01")
        if first.year < 2:
            raise ValueError("Invalid comparison year")
        previous_month = (first - timedelta(days=1)).isoformat()[:7]
        previous_year = first.replace(year=first.year - 1).isoformat()[:7]
        mom, mom_reason = percent_change(current, series.get(previous_month))
        yoy, yoy_reason = percent_change(current, series.get(previous_year))
        rows[month] = {
            "mom": mom, "mom_reason": mom_reason, "yoy": yoy, "yoy_reason": yoy_reason,
        }
    return rows
