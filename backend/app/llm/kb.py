"""Read the publishable, versioned extract of the metric handbook."""
import json
from pathlib import Path
import re

from fastapi import HTTPException


HANDBOOK = Path(__file__).with_name("metric_kb.json")
_METRIC_CODE = re.compile(r"(?<![A-Z0-9_])MOPS-[A-Z]+-[0-9]{2}(?![A-Z0-9_])")


def kb_answer(question):
    try:
        handbook = json.loads(HANDBOOK.read_text(encoding="utf-8"))
        entries = handbook["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "暂无可用的指标口径手册") from None
    codes = set(_METRIC_CODE.findall(question.upper()))
    if codes:
        candidate = next(iter(codes)) if len(codes) == 1 else None
    else:
        compact = re.sub(r"\s+", "", question)
        if "门诊人次环比" in compact:
            candidate = "MOPS-OUT-02"
        elif "门诊人次" in compact:
            candidate = "MOPS-OUT-01"
        else:
            candidate = None
    if candidate not in entries:
        return {"status": "NOT_FOUND", "answer": "当前手册摘录未命中该口径，无法回答。", "refs": []}
    item = entries[candidate]
    return {
        "status": "FOUND", "answer": item["answer"], "refs": [{
            "title": handbook["source"], "version": handbook["version"],
            "source_sha256": handbook["source_sha256"], "status": handbook["status"],
            "indicator_code": candidate, "section": item["title"],
            "excerpt_path": "backend/app/llm/metric_kb.json",
        }],
    }
