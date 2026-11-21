"""Authenticated outpatient question route with query audit."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.routers.outpatient_router import ERRORS, audit_access
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.llm.kb import kb_answer
from app.llm.nl2sql import run_nl_query
from app.llm.report_gen import analyze_anomaly, gen_monthly_report, gen_weekly_report


router = APIRouter(prefix="/api/llm", tags=["只读问数"])


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=300)


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["weekly", "monthly"]
    week: str | None = None
    month: str | None = None
    dept_code: str | None = Field(default=None, max_length=64)


class AnomalyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Literal["MOPS-OUT-01"] = "MOPS-OUT-01"
    date: str
    dept_code: str | None = Field(default=None, max_length=64)


def _audited(response, user, resource, produce):
    try:
        data = produce()
    except HTTPException as error:
        audit_access(user, "query", user.department, error.status_code, resource=resource)
        raise
    department = data.get("scope", {}).get("department", user.department)
    audit_access(user, "query", department, 200, resource=resource)
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}


@router.post("/query", summary="按权限回答已认证的聚合指标问题", responses=ERRORS)
def ask_question(body: Question, response: Response, user: Annotated[Account, Depends(get_current_user)]):
    return _audited(response, user, "outpatient_qa", lambda: run_nl_query(body.question, user))


@router.post("/report", summary="生成认证门诊四章节周报或月报", responses=ERRORS)
def generate_report(body: ReportRequest, response: Response, user: Annotated[Account, Depends(get_current_user)]):
    def produce():
        if body.type == "weekly" and body.week is not None and body.month is None:
            return gen_weekly_report(body.week, user, department=body.dept_code)
        if body.type == "monthly" and body.month is not None and body.week is None:
            return gen_monthly_report(body.month, user, department=body.dept_code)
        raise HTTPException(422, "请选择周报或月报，并仅填写对应期间")

    return _audited(response, user, "outpatient_report", produce)


@router.post("/anomaly", summary="按科室比较门诊人次波动", responses=ERRORS)
def analyze(body: AnomalyRequest, response: Response, user: Annotated[Account, Depends(get_current_user)]):
    return _audited(response, user, "outpatient_anomaly",
                    lambda: analyze_anomaly(body.metric, body.date, user, department=body.dept_code))


@router.post("/kb", summary="查找可追溯的指标口径", responses=ERRORS)
def ask_kb(body: Question, response: Response, user: Annotated[Account, Depends(get_current_user)]):
    return _audited(response, user, "indicator_kb", lambda: kb_answer(body.question))
