"""Authenticated outpatient question route with query audit."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.routers.outpatient_router import ERRORS, audit_access
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.llm.nl2sql import run_nl_query


router = APIRouter(prefix="/api/llm", tags=["只读问数"])


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=300)


@router.post("/query", summary="按权限回答认证门诊聚合问题", responses=ERRORS)
def ask_question(body: Question, response: Response, user: Annotated[Account, Depends(get_current_user)]):
    try:
        data = run_nl_query(body.question, user)
    except HTTPException as error:
        audit_access(user, "query", user.department, error.status_code, resource="outpatient_qa")
        raise
    audit_access(user, "query", data["scope"]["department"], 200, resource="outpatient_qa")
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}
